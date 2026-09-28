"""Reading, checking and packing glTF 2.0 models (canvas v2 phase 4, 3.4). ``struct`` and ``json`` only.

A model comes from the team's ``artifacts/`` through ``FetchIO`` (never
``open``): a ``.glb`` (16 MB or less), or a ``.gltf`` (2 MB or less of JSON)
whose buffers and images are ``data:`` URIs or relative files beside it
(``FetchIO.sibling``; no scheme, no ``..``, 16 MB in all). Everything is
checked before anything is stored:

- the GLB header (magic, version 2, lengths) and its chunks;
- ``asset.version`` 2.0; ``extensionsRequired`` within ``ALLOWED_REQUIRED``;
  Draco, meshopt and KTX2/Basis refused with a message (the page bundles no
  decoder for them);
- images are PNG, JPEG or WebP, 4,096 px or less a side (read from their
  headers);
- every bufferView and accessor stays inside its buffer;
- 300,000 triangles or fewer, 10,000 nodes or fewer, 256 levels deep or fewer,
  and no node is its own ancestor.

The model's bounds are its default scene's, from each ``POSITION`` accessor's
``min``/``max`` (the glTF spec requires them) through the node transforms.

A model with anything outside its own bytes (a ``.gltf``, or a ``.glb`` naming
a URI) is **packed** into one self-contained GLB: buffers concatenated on 4-byte
boundaries, images turned into bufferViews, URIs dropped. The page then loads
it from the asset store with ``GLTFLoader.parse``, never touching
``artifacts/`` (D10).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote

from herdr_team.canvas_scene3d import Model
from herdr_team.canvas_scene3d import _spec
from herdr_team.errors import HerdrTeamError

MAX_GLB_BYTES = 16 * 1024 * 1024
MAX_GLTF_JSON_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_TRIANGLES = 300_000
MAX_SCENE_TRIANGLES = 1_000_000
MAX_NODES = 10_000
MAX_DEPTH = 256
MAX_TEXTURE_PX = 4096
ALLOWED_REQUIRED = ("KHR_materials_unlit", "KHR_texture_transform", "KHR_mesh_quantization", "KHR_materials_emissive_strength")
#: Extensions whose data needs a decoder the page does not bundle (0.2).
REFUSED = {"KHR_draco_mesh_compression": "Draco", "EXT_meshopt_compression": "meshopt", "KHR_meshopt_compression": "meshopt",
           "KHR_texture_basisu": "KTX2/Basis"}
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
SIBLING_SUFFIXES = (".bin", ".png", ".jpg", ".jpeg", ".webp")

GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942
COMPONENT_SIZE = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
TYPE_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _refuse(field: str, message: str, code: str = "op_invalid", **details: Any) -> HerdrTeamError:
    return HerdrTeamError(code, message, details=dict(details, field=field))


def _too_big(field: str, limit: str, maximum: int, message: str) -> HerdrTeamError:
    return HerdrTeamError("canvas_limit", message, details={"field": field, "limit": limit, "max": maximum})


# --------------------------------------------------------------------------
# GLB containers


def parse_glb(data: bytes, field: str) -> Tuple[Dict[str, Any], Optional[bytes]]:
    """``(json document, BIN chunk or None)`` of a GLB, its header and chunk lengths checked."""
    if len(data) < 20 or data[:4] != GLB_MAGIC:
        raise _refuse(field, "{} is not a GLB file (no glTF magic)".format(field))
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise _refuse(field, "{} is glTF version {}; only 2 is read".format(field, version))
    if length != len(data):
        raise _refuse(field, "{}: the GLB header says {} bytes, the file has {}".format(field, length, len(data)))
    offset = 12
    doc: Optional[Dict[str, Any]] = None
    binary: Optional[bytes] = None
    index = 0
    while offset < length:
        if offset + 8 > length:
            raise _refuse(field, "{}: a GLB chunk header runs past the end of the file".format(field))
        size, kind = struct.unpack_from("<II", data, offset)
        start, end = offset + 8, offset + 8 + size
        if end > length:
            raise _refuse(field, "{}: a GLB chunk of {} bytes runs past the end of the file".format(field, size))
        if index == 0:
            if kind != CHUNK_JSON:
                raise _refuse(field, "{}: the first GLB chunk is not JSON".format(field))
            doc = _json(data[start:end], field)
        elif kind == CHUNK_BIN and binary is None and index == 1:
            binary = data[start:end]
        offset = end
        index += 1
    if doc is None:
        raise _refuse(field, "{}: the GLB has no JSON chunk".format(field))
    return doc, binary


def _json(raw: bytes, field: str) -> Dict[str, Any]:
    try:
        doc = json.loads(raw.decode("utf-8").rstrip(" \t\r\n\x00"))
    except (UnicodeDecodeError, ValueError):
        raise _refuse(field, "{}: the glTF JSON does not parse".format(field)) from None
    if not isinstance(doc, dict):
        raise _refuse(field, "{}: the glTF JSON is not an object".format(field))
    return doc


def glb_bytes(doc: Dict[str, Any], binary: Optional[bytes]) -> bytes:
    """A GLB from a document and its BIN chunk: JSON padded with spaces, BIN with zeros, each to 4 bytes."""
    raw = json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    raw += b" " * ((4 - len(raw) % 4) % 4)
    chunks = struct.pack("<II", len(raw), CHUNK_JSON) + raw
    if binary is not None:
        padded = binary + b"\x00" * ((4 - len(binary) % 4) % 4)
        chunks += struct.pack("<II", len(padded), CHUNK_BIN) + padded
    return GLB_MAGIC + struct.pack("<II", 2, 12 + len(chunks)) + chunks


# --------------------------------------------------------------------------
# images


def image_type(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """``(width, height)`` from a PNG, JPEG or WebP header, or None when it cannot be read."""
    kind = image_type(data)
    try:
        if kind == "image/png" and len(data) >= 24:
            return struct.unpack_from(">II", data, 16)
        if kind == "image/jpeg":
            offset = 2
            while offset + 9 <= len(data):
                if data[offset] != 0xFF:
                    return None
                marker = data[offset + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    offset += 2
                    continue
                size = struct.unpack_from(">H", data, offset + 2)[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack_from(">HH", data, offset + 5)
                    return width, height
                offset += 2 + size
            return None
        if kind == "image/webp" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8 ":
                w, h = struct.unpack_from("<HH", data, 26)
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                bits = struct.unpack_from("<I", data, 21)[0]
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                w = data[24] | data[25] << 8 | data[26] << 16
                h = data[27] | data[28] << 8 | data[29] << 16
                return w + 1, h + 1
    except struct.error:
        return None
    return None


# --------------------------------------------------------------------------
# checking a document


def _list(doc: Dict[str, Any], key: str, field: str) -> List[Any]:
    value = doc.get(key, [])
    if not isinstance(value, list):
        raise _refuse(field, "{}: glTF {} is not a list".format(field, key))
    return value


def _index(value: Any, size: int, what: str, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < size:
        raise _refuse(field, "{}: {} {!r} names nothing".format(field, what, value))
    return value


def _nonneg(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(value)
    return value


def check_extensions(doc: Dict[str, Any], field: str) -> None:
    required = doc.get("extensionsRequired") or []
    used = doc.get("extensionsUsed") or []
    if not isinstance(required, list) or not isinstance(used, list):
        raise _refuse(field, "{}: extensionsRequired and extensionsUsed are lists".format(field))
    for name in list(required) + list(used):
        if name in REFUSED:
            raise _refuse(field, "{} uses {} compression ({}), which the page cannot decode; export it uncompressed".format(
                field, REFUSED[name], name), extension=name)
    unknown = [name for name in required if name not in ALLOWED_REQUIRED]
    if unknown:
        raise _refuse(field, "{} requires {}; only {} are supported".format(field, ", ".join(map(str, unknown)), ", ".join(ALLOWED_REQUIRED)),
                      extension=unknown[0])


def _check_views(doc: Dict[str, Any], buffers: Sequence[bytes], field: str) -> None:
    for index, view in enumerate(_list(doc, "bufferViews", field)):
        where = "{}: bufferViews[{}]".format(field, index)
        if not isinstance(view, dict):
            raise _refuse(field, where + " is not an object")
        buf = _index(view.get("buffer"), len(buffers), "bufferViews[{}].buffer".format(index), field)
        try:
            offset, size = _nonneg(view.get("byteOffset")), _nonneg(view.get("byteLength"), -1)
            stride = _nonneg(view.get("byteStride"))
        except ValueError:
            raise _refuse(field, where + " has a bad offset, length or stride") from None
        if size < 0 or offset + size > len(buffers[buf]):
            raise _refuse(field, "{} reaches past its buffer ({} + {} > {})".format(where, offset, max(size, 0), len(buffers[buf])))
        if stride and not 4 <= stride <= 252:
            raise _refuse(field, where + " has a byteStride outside 4 to 252")


def _accessor_fits(doc: Dict[str, Any], view_index: Any, offset: Any, count: int, elem: int, field: str, where: str) -> None:
    views = _list(doc, "bufferViews", field)
    view = views[_index(view_index, len(views), where + ".bufferView", field)]
    try:
        start = _nonneg(offset)
    except ValueError:
        raise _refuse(field, where + " has a bad byteOffset") from None
    stride = view.get("byteStride") or elem
    need = start + stride * (count - 1) + elem if count > 0 else start
    if need > int(view.get("byteLength") or 0):
        raise _refuse(field, "{} reaches past its bufferView ({} > {} bytes)".format(where, need, view.get("byteLength")))


def _check_accessors(doc: Dict[str, Any], field: str) -> None:
    for index, acc in enumerate(_list(doc, "accessors", field)):
        where = "{}: accessors[{}]".format(field, index)
        if not isinstance(acc, dict) or acc.get("componentType") not in COMPONENT_SIZE or acc.get("type") not in TYPE_COUNT:
            raise _refuse(field, where + " has an unknown componentType or type")
        count = acc.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise _refuse(field, where + " has no count")
        elem = COMPONENT_SIZE[acc["componentType"]] * TYPE_COUNT[acc["type"]]
        if acc.get("bufferView") is not None:
            _accessor_fits(doc, acc["bufferView"], acc.get("byteOffset"), count, elem, field, where)
        sparse = acc.get("sparse")
        if sparse is not None:
            if not isinstance(sparse, dict) or not isinstance(sparse.get("count"), int) or not isinstance(sparse.get("indices"), dict) \
                    or not isinstance(sparse.get("values"), dict) or not 1 <= sparse["count"] <= count:
                raise _refuse(field, where + " has a malformed sparse block")
            idx = sparse["indices"]
            if idx.get("componentType") not in (5121, 5123, 5125):
                raise _refuse(field, where + " sparse indices have a bad componentType")
            _accessor_fits(doc, idx.get("bufferView"), idx.get("byteOffset"), sparse["count"], COMPONENT_SIZE[idx["componentType"]], field,
                           where + ".sparse.indices")
            _accessor_fits(doc, sparse["values"].get("bufferView"), sparse["values"].get("byteOffset"), sparse["count"], elem, field,
                           where + ".sparse.values")


def _mesh_triangles(doc: Dict[str, Any], mesh: Dict[str, Any], field: str, where: str) -> int:
    accessors = _list(doc, "accessors", field)
    total = 0
    prims = mesh.get("primitives")
    if not isinstance(prims, list) or not prims:
        raise _refuse(field, where + " has no primitives")
    for p_index, prim in enumerate(prims):
        here = "{}.primitives[{}]".format(where, p_index)
        attrs = prim.get("attributes") if isinstance(prim, dict) else None
        if not isinstance(attrs, dict) or "POSITION" not in attrs:
            raise _refuse(field, here + " has no POSITION attribute")
        pos = accessors[_index(attrs["POSITION"], len(accessors), here + ".POSITION", field)]
        if pos.get("type") != "VEC3" or not _minmax(pos):
            raise _refuse(field, here + " POSITION needs type VEC3 with min and max (the glTF spec requires them)")
        mode = prim.get("mode", 4)
        if prim.get("indices") is not None:
            count = accessors[_index(prim["indices"], len(accessors), here + ".indices", field)].get("count") or 0
        else:
            count = pos.get("count") or 0
        if mode == 4:
            total += count // 3
        elif mode in (5, 6):
            total += max(0, count - 2)
    return total


def _minmax(acc: Dict[str, Any]) -> bool:
    lo, hi = acc.get("min"), acc.get("max")
    return isinstance(lo, list) and isinstance(hi, list) and len(lo) == 3 and len(hi) == 3 and \
        all(_spec.is_number(v) for v in list(lo) + list(hi))


Mat4 = List[float]
_IDENTITY: Mat4 = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _matmul(a: Mat4, b: Mat4) -> Mat4:
    """Column-major 4×4 product ``a · b`` (glTF and three.js store matrices column-major)."""
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            out[col * 4 + row] = sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4))
    return out


def _local(node: Dict[str, Any], field: str, where: str) -> Mat4:
    matrix = node.get("matrix")
    if matrix is not None:
        if not isinstance(matrix, list) or len(matrix) != 16 or not all(_spec.is_number(v) for v in matrix):
            raise _refuse(field, where + ".matrix is 16 numbers")
        return [float(v) for v in matrix]
    t = node.get("translation", [0, 0, 0])
    r = node.get("rotation", [0, 0, 0, 1])
    s = node.get("scale", [1, 1, 1])
    if not (isinstance(t, list) and len(t) == 3 and isinstance(r, list) and len(r) == 4 and isinstance(s, list) and len(s) == 3
            and all(_spec.is_number(v) for v in list(t) + list(r) + list(s))):
        raise _refuse(field, where + " has a malformed translation, rotation or scale")
    x, y, z, w = (float(v) for v in r)
    norm = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    sx, sy, sz = (float(v) for v in s)
    return [(1 - 2 * (y * y + z * z)) * sx, (2 * (x * y + z * w)) * sx, (2 * (x * z - y * w)) * sx, 0.0,
            (2 * (x * y - z * w)) * sy, (1 - 2 * (x * x + z * z)) * sy, (2 * (y * z + x * w)) * sy, 0.0,
            (2 * (x * z + y * w)) * sz, (2 * (y * z - x * w)) * sz, (1 - 2 * (x * x + y * y)) * sz, 0.0,
            float(t[0]), float(t[1]), float(t[2]), 1.0]


def _transform(m: Mat4, p: Sequence[float]) -> Tuple[float, float, float]:
    return (m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12], m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
            m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14])


def check(doc: Dict[str, Any], buffers: Sequence[bytes], images: Sequence[Optional[bytes]], field: str) -> Dict[str, Any]:
    """Everything 3.4 asks of a model; returns ``bounds``, ``triangles`` and the readback facts."""
    asset = doc.get("asset")
    if not isinstance(asset, dict) or asset.get("version") != "2.0":
        raise _refuse(field, "{}: asset.version must be \"2.0\"".format(field))
    check_extensions(doc, field)
    declared = _list(doc, "buffers", field)
    for index, buf in enumerate(declared):
        length = buf.get("byteLength") if isinstance(buf, dict) else None
        if index >= len(buffers):
            raise _refuse(field, "{}: buffers[{}] has no data".format(field, index))
        if not isinstance(length, int) or isinstance(length, bool) or length < 0 or length > len(buffers[index]):
            raise _refuse(field, "{}: buffers[{}] says {} bytes; {} were found".format(field, index, length, len(buffers[index])))
    _check_views(doc, buffers, field)
    _check_accessors(doc, field)
    views = _list(doc, "bufferViews", field)
    for index, image in enumerate(_list(doc, "images", field)):
        where = "{}: images[{}]".format(field, index)
        data = images[index] if index < len(images) else None
        if data is None and isinstance(image, dict) and image.get("bufferView") is not None:
            view = views[_index(image["bufferView"], len(views), where + ".bufferView", field)]
            buf = buffers[view["buffer"]]
            start = int(view.get("byteOffset") or 0)
            data = buf[start:start + int(view["byteLength"])]
        if data is None:
            raise _refuse(field, where + " has no data")
        kind = image_type(data)
        if kind not in IMAGE_TYPES:
            raise _refuse(field, "{} is not PNG, JPEG or WebP".format(where))
        size = image_size(data)
        if size is None:
            raise _refuse(field, "{}: its size cannot be read".format(where))
        if max(size) > MAX_TEXTURE_PX:
            raise _too_big(field, "MAX_TEXTURE_PX", MAX_TEXTURE_PX, "{} is {}×{} px; textures are {} px or less a side".format(
                where, size[0], size[1], MAX_TEXTURE_PX))
    meshes = _list(doc, "meshes", field)
    mesh_tris = [_mesh_triangles(doc, mesh if isinstance(mesh, dict) else {}, field, "{}: meshes[{}]".format(field, i))
                 for i, mesh in enumerate(meshes)]
    nodes = _list(doc, "nodes", field)
    if len(nodes) > MAX_NODES:
        raise _too_big(field, "MAX_NODES", MAX_NODES, "{} has {} nodes; the limit is {}".format(field, len(nodes), MAX_NODES))
    parents: Dict[int, int] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise _refuse(field, "{}: nodes[{}] is not an object".format(field, index))
        for child in node.get("children") or []:
            _index(child, len(nodes), "nodes[{}].children".format(index), field)
            if child in parents or child == index:
                raise _refuse(field, "{}: node {} has two parents or is its own child".format(field, child))
            parents[child] = index
        if node.get("mesh") is not None:
            _index(node["mesh"], len(meshes), "nodes[{}].mesh".format(index), field)
    scenes = _list(doc, "scenes", field)
    if scenes:
        scene_index = doc.get("scene", 0)
        scene = scenes[_index(scene_index, len(scenes), "scene", field)]
        roots = scene.get("nodes") if isinstance(scene, dict) else None
        roots = roots if isinstance(roots, list) else []
        for root in roots:
            _index(root, len(nodes), "scenes[{}].nodes".format(scene_index), field)
    else:
        roots = [i for i in range(len(nodes)) if i not in parents]
    accessors = _list(doc, "accessors", field)
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    triangles = 0
    stack: List[Tuple[int, Mat4, int]] = [(root, _IDENTITY, 1) for root in reversed(roots)]
    visited = 0
    while stack:
        index, parent, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise _too_big(field, "MAX_DEPTH", MAX_DEPTH, "{} nests nodes deeper than {} levels".format(field, MAX_DEPTH))
        visited += 1
        if visited > MAX_NODES:
            raise _refuse(field, "{}: a node is reached twice (a cycle)".format(field))
        node = nodes[index]
        world = _matmul(parent, _local(node, field, "{}: nodes[{}]".format(field, index)))
        if node.get("mesh") is not None:
            mesh = meshes[node["mesh"]]
            triangles += mesh_tris[node["mesh"]]
            for prim in mesh.get("primitives") or []:
                acc = accessors[prim["attributes"]["POSITION"]]
                box = (acc["min"], acc["max"])
                for x in (box[0][0], box[1][0]):
                    for y in (box[0][1], box[1][1]):
                        for z in (box[0][2], box[1][2]):
                            p = _transform(world, (float(x), float(y), float(z)))
                            for axis in range(3):
                                lo[axis] = min(lo[axis], p[axis])
                                hi[axis] = max(hi[axis], p[axis])
        for child in reversed(node.get("children") or []):
            stack.append((child, world, depth + 1))
    if triangles > MAX_TRIANGLES:
        raise _too_big(field, "MAX_TRIANGLES", MAX_TRIANGLES, "{} has {:,} triangles; a model holds {:,} or fewer (simplify it)".format(
            field, triangles, MAX_TRIANGLES))
    if not all(math.isfinite(v) for v in lo + hi):
        raise _refuse(field, "{} draws no geometry in its default scene".format(field))
    names = [str(n.get("name")) for n in nodes if isinstance(n, dict) and isinstance(n.get("name"), str)][:8]
    return {"bounds": tuple(lo + hi), "triangles": triangles,
            "facts": {"meshes": len(meshes), "materials": len(_list(doc, "materials", field)), "textures": len(_list(doc, "textures", field)),
                      "images": len(_list(doc, "images", field)), "nodes": names, "node_count": len(nodes)}}


# --------------------------------------------------------------------------
# URIs and packing


def _uri_data(uri: str, field: str) -> Optional[bytes]:
    """A ``data:`` URI's bytes, or None when ``uri`` is not one."""
    if not uri.lower().startswith("data:"):
        return None
    head, sep, body = uri.partition(",")
    if not sep or not head.lower().endswith(";base64"):
        raise _refuse(field, "{}: a data: URI must be base64".format(field))
    try:
        return base64.b64decode(body, validate=False)
    except (ValueError, TypeError):
        raise _refuse(field, "{}: a data: URI does not decode".format(field)) from None


def relative_uri(uri: str, field: str) -> str:
    """A relative file URI beside the model, percent-decoded; a scheme, an absolute path or ``..`` is refused."""
    path = unquote(uri)
    if _SCHEME.match(path) or path.startswith(("/", "\\")) or "\\" in path or any(part == ".." for part in path.split("/")) or not path:
        raise _refuse(field, "{}: {!r} must be a relative file beside the model (no scheme, no .., no absolute path)".format(field, uri[:120]),
                      code="path_refused")
    return path


def pack(doc: Dict[str, Any], buffers: Sequence[bytes], images: Sequence[Optional[bytes]]) -> bytes:
    """One self-contained GLB: every buffer concatenated (4-byte aligned) into the BIN chunk, every image with a URI turned
    into a bufferView, no URI left."""
    out = json.loads(json.dumps(doc))
    blob = bytearray()
    starts: List[int] = []
    for data in buffers:
        blob += b"\x00" * ((4 - len(blob) % 4) % 4)
        starts.append(len(blob))
        blob += data
    views = out.setdefault("bufferViews", [])
    for view in views:
        view["byteOffset"] = starts[view["buffer"]] + int(view.get("byteOffset") or 0)
        view["buffer"] = 0
    for index, image in enumerate(out.get("images") or []):
        data = images[index] if index < len(images) else None
        if data is None or not isinstance(image, dict) or "uri" not in image:
            continue
        blob += b"\x00" * ((4 - len(blob) % 4) % 4)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        blob += data
        image.pop("uri", None)
        image["bufferView"] = len(views) - 1
        image["mimeType"] = image_type(data) or "image/png"
    if not views:
        out.pop("bufferViews", None)
    out["buffers"] = [{"byteLength": len(blob)}] if blob else []
    if not out["buffers"]:
        out.pop("buffers")
    return glb_bytes(out, bytes(blob) if blob else None)


def read(io: Any, obj: Dict[str, Any], field: str) -> Model:
    """The model an object's ``src`` names, read through ``FetchIO``, checked, and packed when it needs to be."""
    src_field = field + ".src"
    fetched = io.read_artifact(obj.get("src"), (".glb", ".gltf"), MAX_GLB_BYTES, field=src_field)
    total = len(fetched.data)
    if fetched.suffix == ".gltf":
        if len(fetched.data) > MAX_GLTF_JSON_BYTES:
            raise _too_big(src_field, "MAX_GLTF_JSON_BYTES", MAX_GLTF_JSON_BYTES, "{} is over {} MB of JSON; export a .glb".format(
                fetched.rel, MAX_GLTF_JSON_BYTES // (1024 * 1024)))
        doc, binary = _json(fetched.data, src_field), None
    else:
        doc, binary = parse_glb(fetched.data, src_field)
    check_extensions(doc, src_field)
    needs_pack = fetched.suffix == ".gltf"
    buffers: List[bytes] = []
    for index, buf in enumerate(_list(doc, "buffers", src_field)):
        uri = buf.get("uri") if isinstance(buf, dict) else None
        if uri is None:
            if index != 0 or binary is None:
                raise _refuse(src_field, "{}: buffers[{}] has no uri and no GLB BIN chunk".format(src_field, index))
            buffers.append(binary)
            continue
        needs_pack = True
        data = _resolve(io, fetched, uri, src_field, "buffers[{}]".format(index))
        total += len(data)
        buffers.append(data)
        if total > MAX_TOTAL_BYTES:
            raise _too_big(src_field, "MAX_GLB_BYTES", MAX_TOTAL_BYTES, "{} and its files are over {} MB".format(
                fetched.rel, MAX_TOTAL_BYTES // (1024 * 1024)))
    images: List[Optional[bytes]] = []
    for index, image in enumerate(_list(doc, "images", src_field)):
        uri = image.get("uri") if isinstance(image, dict) else None
        if uri is None:
            images.append(None)
            continue
        needs_pack = True
        data = _resolve(io, fetched, uri, src_field, "images[{}]".format(index))
        total += len(data)
        images.append(data)
        if total > MAX_TOTAL_BYTES:
            raise _too_big(src_field, "MAX_GLB_BYTES", MAX_TOTAL_BYTES, "{} and its files are over {} MB".format(
                fetched.rel, MAX_TOTAL_BYTES // (1024 * 1024)))
    found = check(doc, buffers, images, src_field)
    data = pack(doc, buffers, images) if needs_pack else fetched.data
    if len(data) > MAX_GLB_BYTES:
        raise _too_big(src_field, "MAX_GLB_BYTES", MAX_GLB_BYTES, "{} packs to over {} MB".format(fetched.rel, MAX_GLB_BYTES // (1024 * 1024)))
    facts = dict(found["facts"], packed=needs_pack)
    return Model(data=data, src=fetched.rel, sha256=fetched.sha256, bounds=found["bounds"], triangles=found["triangles"], facts=facts)


def _resolve(io: Any, fetched: Any, uri: Any, field: str, what: str) -> bytes:
    if not isinstance(uri, str):
        raise _refuse(field, "{}: {}.uri is not text".format(field, what))
    inline = _uri_data(uri, "{} {}".format(field, what))
    if inline is not None:
        return inline
    rel = relative_uri(uri, "{} {}".format(field, what))
    return io.sibling(fetched, rel, SIBLING_SUFFIXES, MAX_TOTAL_BYTES).data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

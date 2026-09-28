"""glTF fixtures for the scene3d tests (canvas v2 phase 4, 8.2), made with the standard library only.

Nothing binary is hand-written: every model here is built from numbers, so a
test can make a variant (a hostile one, a bigger one) in a line. The builders:

- ``box_glb(size, name)``: one box mesh (24 vertices, 12 triangles), a GLB;
- ``textured_box_gltf()``: the same box with UVs and a PNG texture, as a
  ``.gltf`` JSON plus its ``.bin`` and ``.png`` beside it (what ``pack`` turns into
  one GLB);
- ``hierarchy_glb()``: a parent node (translated, rotated 90° about y, scaled 2)
  holding a child node with the box mesh: bounds go through both transforms;
- ``png(w, h)``: a small solid PNG (zlib, no imaging library).

``python3 tests/fixtures/scene3d/make_fixtures.py`` (re)writes the committed
files: the models ``tests/fixtures/canvas_artifacts/guides/models/arm-v3.glb``
(the guides' worked example 3) and ``tests/fixtures/canvas_artifacts/scene3d/models/*``
(the ``scene3d`` QA scene), and the page's fixtures (interface I-11):
``solved/<scene>.json`` (an op and the element it stores, compact ``solved``
included) and ``extents.json`` (each primitive's local box). ``--check`` fails
when a committed one differs.
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent
FLOAT, USHORT = 5126, 5123
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963


def png(w: int = 2, h: int = 2, rgb: Tuple[int, int, int] = (62, 99, 221)) -> bytes:
    """A solid ``w × h`` RGB PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * w
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * h, 9)) + chunk(b"IEND", b""))


def png_header(w: int, h: int) -> bytes:
    """Just enough of a PNG for its size to be read (a hostile texture that claims to be huge)."""
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0) + b"\x00" * 4


def box_arrays(size: Sequence[float] = (1.0, 1.0, 1.0)) -> Tuple[List[float], List[float], List[int]]:
    """``(positions, uvs, indices)`` of a box centred on the origin: 4 vertices per face, 2 triangles each."""
    hx, hy, hz = (float(v) / 2.0 for v in size)
    faces = [((1, 0, 0), [(hx, -hy, hz), (hx, -hy, -hz), (hx, hy, -hz), (hx, hy, hz)]),
             ((-1, 0, 0), [(-hx, -hy, -hz), (-hx, -hy, hz), (-hx, hy, hz), (-hx, hy, -hz)]),
             ((0, 1, 0), [(-hx, hy, hz), (hx, hy, hz), (hx, hy, -hz), (-hx, hy, -hz)]),
             ((0, -1, 0), [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy, hz), (-hx, -hy, hz)]),
             ((0, 0, 1), [(-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]),
             ((0, 0, -1), [(hx, -hy, -hz), (-hx, -hy, -hz), (-hx, hy, -hz), (hx, hy, -hz)])]
    positions: List[float] = []
    uvs: List[float] = []
    indices: List[int] = []
    for n, (_normal, corners) in enumerate(faces):
        for p in corners:
            positions += [float(v) for v in p]
        uvs += [0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
        base = 4 * n
        indices += [base, base + 1, base + 2, base, base + 2, base + 3]
    return positions, uvs, indices


def _pad(data: bytes, fill: bytes = b"\x00") -> bytes:
    return data + fill * ((4 - len(data) % 4) % 4)


def _mesh_doc(size: Sequence[float], uv: bool) -> Tuple[Dict[str, Any], bytes]:
    """A document holding one box mesh and its one buffer (no URI), and that buffer's bytes."""
    positions, uvs, indices = box_arrays(size)
    idx = _pad(struct.pack("<{}H".format(len(indices)), *indices))
    pos = struct.pack("<{}f".format(len(positions)), *positions)
    tex = struct.pack("<{}f".format(len(uvs)), *uvs) if uv else b""
    blob = idx + pos + tex
    hx, hy, hz = (float(v) / 2.0 for v in size)
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(indices) * 2, "target": ELEMENT_ARRAY_BUFFER},
             {"buffer": 0, "byteOffset": len(idx), "byteLength": len(pos), "target": ARRAY_BUFFER}]
    accessors = [{"bufferView": 0, "componentType": USHORT, "count": len(indices), "type": "SCALAR"},
                 {"bufferView": 1, "componentType": FLOAT, "count": len(positions) // 3, "type": "VEC3",
                  "min": [-hx, -hy, -hz], "max": [hx, hy, hz]}]
    attributes = {"POSITION": 1}
    if uv:
        views.append({"buffer": 0, "byteOffset": len(idx) + len(pos), "byteLength": len(tex), "target": ARRAY_BUFFER})
        accessors.append({"bufferView": 2, "componentType": FLOAT, "count": len(uvs) // 2, "type": "VEC2"})
        attributes["TEXCOORD_0"] = 2
    doc = {"asset": {"version": "2.0", "generator": "herdr-synapse make_fixtures"}, "scene": 0,
           "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": "box"}],
           "meshes": [{"name": "box", "primitives": [{"attributes": attributes, "indices": 0, "material": 0}]}],
           "materials": [{"name": "grey", "pbrMetallicRoughness": {"baseColorFactor": [0.6, 0.6, 0.65, 1.0], "metallicFactor": 0.0}}],
           "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(blob)}]}
    return doc, blob


def glb(doc: Dict[str, Any], binary: Optional[bytes]) -> bytes:
    """A GLB container: JSON padded with spaces, BIN with zeros."""
    raw = _pad(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8"), b" ")
    chunks = struct.pack("<II", len(raw), 0x4E4F534A) + raw
    if binary is not None:
        data = _pad(binary)
        chunks += struct.pack("<II", len(data), 0x004E4942) + data
    return b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks


def box_doc(size: Sequence[float] = (1.0, 1.0, 1.0), name: str = "box") -> Tuple[Dict[str, Any], bytes]:
    doc, blob = _mesh_doc(size, uv=False)
    doc["nodes"][0]["name"] = name
    return doc, blob


def box_glb(size: Sequence[float] = (1.0, 1.0, 1.0), name: str = "box") -> bytes:
    """One box mesh as a GLB (12 triangles, bounds ±size/2)."""
    doc, blob = box_doc(size, name)
    return glb(doc, blob)


def textured_box_gltf(texture: Optional[bytes] = None, inline: bool = False) -> Dict[str, bytes]:
    """``{"box.gltf": json, "box.bin": buffer, "box.png": texture}``: a box whose buffer and image are files beside it (or
    ``data:`` URIs with ``inline``, when only the ``.gltf`` is returned)."""
    doc, blob = _mesh_doc((1.0, 1.0, 1.0), uv=True)
    image = texture if texture is not None else png(4, 4)
    files: Dict[str, bytes] = {}
    if inline:
        doc["buffers"] = [{"byteLength": len(blob), "uri": "data:application/octet-stream;base64," + base64.b64encode(blob).decode("ascii")}]
        doc["images"] = [{"uri": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}]
    else:
        doc["buffers"] = [{"byteLength": len(blob), "uri": "box.bin"}]
        doc["images"] = [{"uri": "box.png"}]
        files["box.bin"] = blob
        files["box.png"] = image
    doc["textures"] = [{"source": 0}]
    doc["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    files["box.gltf"] = json.dumps(doc, sort_keys=True, indent=1).encode("utf-8")
    return files


def hierarchy_glb() -> bytes:
    """A parent (translation [2, 1, 0], 90° about y, scale 2) holding a child (translation [0, 0.5, 0]) with a
    1 × 1 × 2 box: readers must compose both transforms to find its world box (``HIERARCHY_BOUNDS``)."""
    doc, blob = _mesh_doc((1.0, 1.0, 2.0), uv=False)
    half = 2 ** -0.5
    doc["nodes"] = [{"name": "parent", "children": [1], "translation": [2.0, 1.0, 0.0], "rotation": [0.0, half, 0.0, half],
                     "scale": [2.0, 2.0, 2.0]},
                    {"name": "child", "mesh": 0, "translation": [0.0, 0.5, 0.0]}]
    return glb(doc, blob)


#: The world box of ``hierarchy_glb``: the child box (x ±0.5, y 0..1, z ±1) turned 90° about y (x ±1, z ±0.5), scaled 2
#: (x ±2, y 0..2, z ±1), then moved by [2, 1, 0].
HIERARCHY_BOUNDS = (0.0, 1.0, -1.0, 4.0, 3.0, 1.0)


def committed() -> Dict[Path, bytes]:
    """The model files the repository keeps, by path."""
    arm = box_glb((0.4, 0.8, 0.3), "arm-v3")
    tex = textured_box_gltf()
    out = {FIXTURES / "canvas_artifacts" / "guides" / "models" / "arm-v3.glb": arm,
           FIXTURES / "canvas_artifacts" / "scene3d" / "models" / "arm-v3.glb": arm}
    for name, data in tex.items():
        out[FIXTURES / "canvas_artifacts" / "scene3d" / "models" / ("crate" + name[3:])] = \
            data.replace(b'"box.bin"', b'"crate.bin"').replace(b'"box.png"', b'"crate.png"') if name.endswith(".gltf") else data
    return out


# --------------------------------------------------------------------------
# solved scenes for the page (interface I-11: web/src/v2/scene3d reads them in vitest)

REPO = FIXTURES.parent.parent
TOPOLOGY = {"op": "scene3d", "id": "topo", "intent": "the prod topology in 3D", "title": "Prod topology", "units": "m", "camera": "iso",
            "lights": "studio", "ground": True, "labels": "auto",
            "objects": [{"id": "base", "shape": "plane", "size": [8, 5], "tone": "neutral"},
                        {"id": "lb", "shape": "box", "size": [1.2, 0.4, 1.2], "tone": "info", "label": "Load balancer", "on": "base", "at": [-3, 0]},
                        {"id": "api", "shape": "group", "layout": "row", "gap": 0.3, "on": "base", "right_of": "lb", "label": "API pool"},
                        {"id": "api1", "shape": "box", "size": [0.8, 1.2, 0.8], "tone": "accent", "in": "api"},
                        {"id": "api2", "shape": "box", "size": [0.8, 1.2, 0.8], "tone": "accent", "in": "api"},
                        {"id": "db", "shape": "cylinder", "radius": 0.6, "height": 1.4, "tone": "success", "label": "Postgres", "on": "base",
                         "right_of": "api", "gap": 1},
                        {"id": "cache", "shape": "sphere", "radius": 0.35, "tone": "warning", "label": "Redis", "above": "api", "gap": 0.3}],
            "links": [{"from": "lb", "to": "api"}, {"from": "api", "to": "db", "label": "SQL"}, {"from": "api", "to": "cache", "tone": "warning"}],
            "w": 640, "h": 420}
RACK = {"op": "scene3d", "id": "rack", "intent": "rack layout for the new cluster", "title": "Rack R12", "camera": "front",
        "objects": [{"id": "cab", "shape": "box", "size": [1.0, 2.0, 1.0], "tone": "neutral", "finish": "glass", "label": "R12"},
                    {"id": "nodes", "shape": "group", "layout": "stack", "gap": 0.05, "inside": "cab"},
                    {"id": "n1", "shape": "box", "size": [0.9, 0.2, 0.9], "tone": "info", "label": "gpu-1", "in": "nodes"},
                    {"id": "n2", "shape": "box", "size": [0.9, 0.2, 0.9], "tone": "info", "label": "gpu-2", "in": "nodes"},
                    {"id": "n3", "shape": "box", "size": [0.9, 0.2, 0.9], "tone": "warning", "label": "gpu-3 (degraded)", "in": "nodes"},
                    {"id": "sw", "shape": "box", "size": [0.9, 0.1, 0.9], "tone": "accent", "label": "ToR switch", "in": "nodes"}]}
ROBOT = {"op": "scene3d", "id": "robot", "intent": "the arm prototype on its bench", "title": "Arm v3 on bench", "camera": "orbit",
         "lights": "soft",
         "objects": [{"id": "bench", "shape": "box", "size": [2.0, 0.9, 1.0], "tone": "neutral"},
                     {"id": "arm", "shape": "gltf", "src": "models/arm-v3.glb", "height": 0.8, "on": "bench", "label": "Arm v3"},
                     {"id": "tag", "shape": "text3d", "text": "payload 1.2 kg", "height": 0.08, "above": "arm", "gap": 0.1}]}
HUB = {"op": "scene3d", "id": "hub", "intent": "event bus consumers", "title": "Event bus",
       "objects": [{"id": "bus", "shape": "cylinder", "radius": 0.8, "height": 0.3, "tone": "accent", "label": "Kafka"},
                   {"id": "a", "shape": "box", "size": [0.6, 0.6, 0.6], "tone": "info", "label": "billing", "around": "bus"},
                   {"id": "b", "shape": "box", "size": [0.6, 0.6, 0.6], "tone": "info", "label": "search", "around": "bus"},
                   {"id": "c", "shape": "box", "size": [0.6, 0.6, 0.6], "tone": "info", "label": "email", "around": "bus"},
                   {"id": "d", "shape": "cone", "radius": 0.35, "height": 0.7, "tone": "danger", "label": "fraud", "around": "bus"}],
       "links": [{"from": "a", "to": "bus"}, {"from": "b", "to": "bus"}, {"from": "c", "to": "bus"}, {"from": "d", "to": "bus", "label": "alerts"}]}
SHAPES = {"op": "scene3d", "id": "shapes", "intent": "every primitive once", "title": "Every primitive", "labels": "all",
          "objects": [{"id": "floor", "shape": "plane", "size": [9, 6], "thickness": 0.05},
                      {"id": "turned", "shape": "box", "size": [1.6, 0.5, 0.8], "rotate": 30, "tone": "info", "on": "floor", "at": [-3, -1.5]},
                      {"id": "ball", "shape": "sphere", "radius": 0.4, "tone": "warning", "on": "floor", "at": [-1, -1.5]},
                      {"id": "tank", "shape": "cylinder", "radius": 0.5, "height": 1.2, "tone": "success", "on": "floor", "at": [1, -1.5]},
                      {"id": "peak", "shape": "cone", "radius": 0.5, "height": 1.0, "tone": "danger", "on": "floor", "at": [3, -1.5]},
                      {"id": "grid", "shape": "group", "layout": "grid", "cols": 3, "gap": 0.15, "on": "floor", "at": [-2.5, 1.5]},
                      {"id": "g1", "shape": "box", "size": [0.4, 0.4, 0.4], "tone": "accent", "in": "grid"},
                      {"id": "g2", "shape": "box", "size": [0.4, 0.6, 0.4], "tone": "accent", "in": "grid"},
                      {"id": "g3", "shape": "box", "size": [0.4, 0.8, 0.4], "tone": "accent", "in": "grid"},
                      {"id": "g4", "shape": "box", "size": [0.4, 0.3, 0.4], "tone": "accent", "in": "grid"},
                      {"id": "ring", "shape": "group", "layout": "ring", "gap": 0.2, "on": "floor", "at": [0.5, 1.5]},
                      {"id": "r1", "shape": "sphere", "radius": 0.2, "tone": "info", "in": "ring"},
                      {"id": "r2", "shape": "sphere", "radius": 0.2, "tone": "info", "in": "ring"},
                      {"id": "r3", "shape": "sphere", "radius": 0.2, "tone": "info", "in": "ring"},
                      {"id": "crate", "shape": "gltf", "src": "models/crate.gltf", "height": 0.6, "on": "floor", "at": [3, 1.5]},
                      {"id": "sign", "shape": "text3d", "text": "loading dock", "height": 0.3, "above": "crate", "gap": 0.2},
                      {"id": "flow", "shape": "arrow3d", "from": "ball", "to": "tank", "tone": "accent"}]}
#: The QA scene ``scene3d`` and the solved fixtures share these ops (worked examples 1 to 4, and every primitive).
SCENES = (("topology", TOPOLOGY), ("rack", RACK), ("robot", ROBOT), ("hub", HUB), ("shapes", SHAPES))


class ArtifactsIO:
    """A ``FetchIO`` over a folder (the fixtures' artifacts): the same contract as the canvas's, for fixtures and tests."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _fetch(self, rel: str, suffixes: Sequence[str], max_bytes: int) -> Any:
        import hashlib

        from herdr_team.canvas_kinds.sdk import Fetched
        from herdr_team.errors import HerdrTeamError

        text = str(rel)
        if text.startswith(("/", "\\")) or ".." in text.split("/") or ":" in text:
            raise HerdrTeamError("path_refused", "{} must name a file under artifacts/".format(text), details={"path": text})
        path = self.root / text
        suffix = path.suffix.lower()
        if suffix not in tuple(s.lower() for s in suffixes):
            raise HerdrTeamError("path_refused", "{} must be one of: {}".format(text, ", ".join(suffixes)), details={"path": text})
        if not path.is_file():
            raise HerdrTeamError("path_refused", "{} is not a file under artifacts/".format(text), details={"path": text})
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise HerdrTeamError("too_big", "{} is over {} bytes".format(text, max_bytes), details={"limit": "max_bytes", "max": max_bytes})
        return Fetched(rel=text, data=data, sha256=hashlib.sha256(data).hexdigest(), suffix=suffix)

    def read_artifact(self, rel: Any, suffixes: Sequence[str], max_bytes: int, *, field: str = "data") -> Any:
        return self._fetch(str(rel), suffixes, max_bytes)

    def sibling(self, fetched: Any, rel: str, suffixes: Sequence[str], max_bytes: int) -> Any:
        folder = Path(fetched.rel).parent.as_posix()
        return self._fetch(rel if folder in ("", ".") else folder + "/" + rel, suffixes, max_bytes)


def solved_element(op: Dict[str, Any], io: Any = None, seq: int = 1) -> Dict[str, Any]:
    """The element a ``scene3d`` op stores (``settings``, ``objects``, ``links``, compact ``solved``), made with the kind's own
    functions and no canvas: what the page reads."""
    import hashlib

    from herdr_team.canvas_kinds import scene3d as K
    from herdr_team.canvas_scene3d import _solver, _spec

    io = io or ArtifactsIO(FIXTURES / "canvas_artifacts" / "scene3d")
    loaded = K.load(op, io)
    spec = {key: op.get(key) for key in ("title",) + _spec.SETTINGS}
    spec["objects"] = [_spec.object_item(_spec.PURE, o, "objects[{}]".format(i)) for i, o in enumerate(op.get("objects") or [])]
    spec["links"] = [_spec.link_item(_spec.PURE, l, "links[{}]".format(i)) for i, l in enumerate(op.get("links") or [])]
    spec = K.normalize(_spec.PURE, spec)
    stored = {src: hashlib.sha256(model.data).hexdigest()[:32] + ".glb" for src, model in loaded["models"].items()}
    facts = K._model_facts(spec["objects"], loaded["models"], None, stored)
    for info in facts.values():
        info.pop("bytes", None)
    solved = _solver.solve(spec["objects"], spec["links"], spec["units"], facts)
    links = [{k: v for k, v in l.items() if not k.startswith("_")} for l in spec["links"]]
    return {"id": "E-{}".format(seq), "type": "scene3d", "alias": op.get("id"), "text": spec.get("title") or "",
            "x": 0, "y": 0, "w": op.get("w", 640), "h": op.get("h", 420), "kv": 1,
            "settings": {key: spec[key] for key in _spec.SETTINGS}, "objects": spec["objects"], "links": links,
            "solved": _solver.compact(solved), "updated_seq": seq}


def extents() -> List[Dict[str, Any]]:
    """``[{shape, params, extent}]``: every built-in primitive's local box for a few params (the page's primitives match)."""
    from herdr_team import canvas_scene3d as S
    from herdr_team.canvas_scene3d import _spec

    cases = [("box", {}), ("box", {"size": [1.2, 0.4, 1.2]}), ("sphere", {"radius": 0.35}), ("sphere", {}),
             ("cylinder", {"radius": 0.6, "height": 1.4}), ("cone", {"radius": 0.35, "height": 0.7}), ("plane", {"size": [8, 5]}),
             ("plane", {"size": [2, 3], "thickness": 0.1}), ("text3d", {"text": "payload 1.2 kg", "height": 0.08}),
             ("gltf", {"src": "models/arm-v3.glb", "height": 0.8})]
    out = []
    for shape, params in cases:
        prim = S.get(shape)
        resolved = _spec.resolved(dict(params, id="o", shape=shape))
        out.append({"shape": shape, "params": params, "extent": [round(float(v), 4) for v in prim.extent(resolved)]})
    return out


def web_fixtures() -> Dict[Path, bytes]:
    """``solved/<name>.json`` (``{"op", "element"}``) and ``extents.json``, as JSON text."""
    sys.path.insert(0, str(REPO))
    out: Dict[Path, bytes] = {}
    for seq, (name, op) in enumerate(SCENES, start=1):
        doc = {"op": op, "element": solved_element(op, seq=seq)}
        out[HERE / "solved" / (name + ".json")] = (json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    out[HERE / "extents.json"] = (json.dumps(extents(), indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    return out


def main(argv: Sequence[str]) -> int:
    check = "--check" in argv
    stale = []
    files = dict(committed())
    files.update(web_fixtures())
    for path, data in sorted(files.items()):
        current = path.read_bytes() if path.is_file() else None
        if current == data:
            continue
        stale.append(path)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    for path in stale:
        sys.stdout.write("{} {}\n".format("stale" if check else "wrote", path.relative_to(FIXTURES.parent.parent)))
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

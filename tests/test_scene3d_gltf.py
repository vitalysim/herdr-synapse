"""glTF models in 3D scenes (canvas v2 phase 4, 3.4 and 8.2): reading, checking, packing, bounds, and every refusal.

The models come from ``tests/fixtures/scene3d/make_fixtures.py`` (a box, a
textured box as a ``.gltf`` with its files, a two-node hierarchy); the hostile
corpus is ``tests/fixtures/scene3d/hostile/*.json``, each input refused exactly
as recorded.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import PLUGIN_ROOT
from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team.canvas_scene3d import _gltf, _solver
from herdr_team.errors import HerdrTeamError

FIXTURES = PLUGIN_ROOT / "tests" / "fixtures" / "scene3d"
HOSTILE = FIXTURES / "hostile"
sys.path.insert(0, str(FIXTURES))
import make_fixtures as M  # noqa: E402 - the fixture builders live beside the fixtures

FIELD = "objects[0]"


def set_path(doc, path, value):
    """``doc`` with ``path`` (``bufferViews[1].byteLength``) set to ``value`` (None deletes it)."""
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    target = doc
    for part in parts[:-1]:
        target = target[int(part[1:-1])] if part.startswith("[") else target[part]
    last = parts[-1]
    if last.startswith("["):
        target[int(last[1:-1])] = value
    elif value is None:
        target.pop(last, None)
    else:
        target[last] = value


class Folder:
    """A throwaway artifacts folder and its ``FetchIO``."""

    def __init__(self, files):
        self.root = Path(tempfile.mkdtemp(prefix="scene3d-gltf-"))
        for name, data in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.io = M.ArtifactsIO(self.root)

    def read(self, src):
        return _gltf.read(self.io, {"src": src}, FIELD)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def textured(patch=None, texture=None):
    files = M.textured_box_gltf(texture)
    doc = json.loads(files["box.gltf"])
    for path, value in (patch or {}).items():
        set_path(doc, path, value)
    files["box.gltf"] = json.dumps(doc).encode("utf-8")
    return files


def mutated_glb(spec):
    data = bytearray(M.box_glb())
    if "magic" in spec:
        data[:4] = spec["magic"].encode("ascii")
    if "version" in spec:
        struct.pack_into("<I", data, 4, spec["version"])
    if "length_delta" in spec:
        struct.pack_into("<I", data, 8, len(data) + spec["length_delta"])
    if "chunk_delta" in spec:
        size = struct.unpack_from("<I", data, 12)[0]
        struct.pack_into("<I", data, 12, size + spec["chunk_delta"])
    if spec.get("first_chunk") == "BIN":
        struct.pack_into("<I", data, 16, 0x004E4942)
    return bytes(data)


def many_triangles(count):
    """A GLB of ``count`` triangles (indexed, all over one quad's four vertices)."""
    positions = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0]
    indices = [0, 1, 2] * count
    idx = struct.pack("<{}H".format(len(indices)), *indices)
    idx += b"\x00" * ((4 - len(idx) % 4) % 4)
    pos = struct.pack("<12f", *positions)
    doc = {"asset": {"version": "2.0"}, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 0}]}],
           "accessors": [{"bufferView": 0, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
                         {"bufferView": 1, "componentType": 5126, "count": 4, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]}],
           "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(indices) * 2}, {"buffer": 0, "byteOffset": len(idx), "byteLength": 48}],
           "buffers": [{"byteLength": len(idx) + 48}]}
    return M.glb(doc, idx + pos)


def big_glb(target_bytes):
    """A GLB of about ``target_bytes``: 1 triangle per 3 vertices, non-indexed, under the triangle cap."""
    vertices = min((target_bytes - 2048) // 12, 3 * 290_000)
    vertices -= vertices % 3
    raw = struct.pack("<3f", 0.0, 0.0, 0.0) * vertices
    doc = {"asset": {"version": "2.0"}, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": vertices, "type": "VEC3", "min": [0, 0, 0], "max": [1, 2, 3]}],
           "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(raw)}], "buffers": [{"byteLength": target_bytes - 2048}]}
    padding = b"\x00" * (target_bytes - 2048 - len(raw))
    return M.glb(doc, raw + padding)


class Reading(unittest.TestCase):
    def folder(self, files):
        found = Folder(files)
        self.addCleanup(found.close)
        return found

    def test_a_glb_is_read_and_stored_as_it_is(self):
        data = M.box_glb((0.4, 0.8, 0.3), "arm")
        model = self.folder({"models/arm.glb": data}).read("models/arm.glb")
        self.assertEqual(model.data, data)
        self.assertEqual(model.triangles, 12)
        self.assertEqual(model.bounds, (-0.2, -0.4, -0.15, 0.2, 0.4, 0.15))
        self.assertEqual(model.src, "models/arm.glb")
        self.assertEqual(model.facts["nodes"], ["arm"])
        self.assertEqual((model.facts["meshes"], model.facts["materials"], model.facts["packed"]), (1, 1, False))

    def test_a_gltf_with_files_beside_it_is_packed_into_one_glb(self):
        files = M.textured_box_gltf()
        model = self.folder({"m/" + name: data for name, data in files.items()}).read("m/box.gltf")
        self.assertTrue(model.facts["packed"])
        self.assertEqual(model.facts["textures"], 1)
        doc, binary = _gltf.parse_glb(model.data, FIELD)
        original = json.loads(files["box.gltf"])
        self.assertEqual(doc["accessors"], original["accessors"], "the same accessors")
        self.assertNotIn("uri", json.dumps(doc["buffers"]) + json.dumps(doc["images"]), "no URI is left")
        self.assertEqual(len(doc["buffers"]), 1)
        # Every accessor's bytes are the same after packing (a round trip: pack, parse, compare).
        for index, view in enumerate(original["bufferViews"]):
            before = files["box.bin"][view["byteOffset"]:view["byteOffset"] + view["byteLength"]]
            packed = doc["bufferViews"][index]
            self.assertEqual(binary[packed["byteOffset"]:packed["byteOffset"] + packed["byteLength"]], before, index)
        image_view = doc["bufferViews"][doc["images"][0]["bufferView"]]
        self.assertEqual(binary[image_view["byteOffset"]:image_view["byteOffset"] + image_view["byteLength"]], files["box.png"])
        self.assertEqual(doc["images"][0]["mimeType"], "image/png")
        self.assertEqual(_gltf.check(doc, [binary], [], FIELD)["triangles"], 12, "the packed model checks clean")
        self.assertEqual(model.data, _gltf.pack(original, [files["box.bin"]], [files["box.png"]]), "packing is deterministic")

    def test_data_uris_pack_too(self):
        files = M.textured_box_gltf(inline=True)
        self.assertEqual(list(files), ["box.gltf"])
        model = self.folder(files).read("box.gltf")
        self.assertTrue(model.facts["packed"])
        doc, _binary = _gltf.parse_glb(model.data, FIELD)
        self.assertNotIn("data:", json.dumps(doc))

    def test_bounds_go_through_the_node_transforms(self):
        model = self.folder({"h.glb": M.hierarchy_glb()}).read("h.glb")
        for got, want in zip(model.bounds, M.HIERARCHY_BOUNDS):
            self.assertAlmostEqual(got, want, 9)
        self.assertEqual(model.facts["nodes"], ["parent", "child"])

    def test_triangle_counts(self):
        self.assertEqual(self.folder({"t.glb": many_triangles(1000)}).read("t.glb").triangles, 1000)
        strip = M.box_doc()
        doc, blob = strip
        doc["meshes"][0]["primitives"][0]["mode"] = 5
        self.assertEqual(_gltf.check(doc, [blob], [], FIELD)["triangles"], 36 - 2, "a strip of n indices is n - 2 triangles")

    def test_image_headers(self):
        self.assertEqual(_gltf.image_size(M.png(7, 3)), (7, 3))
        self.assertEqual(_gltf.image_size(M.png_header(5000, 20)), (5000, 20))
        jpeg = b"\xff\xd8\xff\xe0\x00\x10" + b"JFIF\x00" + b"\x00" * 9 + b"\xff\xc0\x00\x11\x08\x00\x40\x00\x80\x03" + b"\x00" * 9
        self.assertEqual(_gltf.image_size(jpeg), (128, 64))
        webp = b"RIFF" + b"\x00" * 4 + b"WEBPVP8X" + b"\x00" * 8 + bytes([99, 0, 0, 49, 0, 0])
        self.assertEqual(_gltf.image_size(webp), (100, 50))
        self.assertIsNone(_gltf.image_type(b"<svg/>"))

    def test_a_16_mb_glb_is_checked_and_bounded_within_the_budget(self):
        data = big_glb(16 * 1024 * 1024 - 64)
        folder = self.folder({"big.glb": data})
        best = min(_timed(lambda: folder.read("big.glb")) for _ in range(3))
        self.assertLess(best, 0.15, "a 16 MB GLB in {:.0f} ms".format(best * 1000))
        self.assertEqual(folder.read("big.glb").bounds, (0.0, 0.0, 0.0, 1.0, 2.0, 3.0))

    def test_a_gltf_over_2_mb_of_json_is_refused(self):
        doc = json.loads(M.textured_box_gltf(inline=True)["box.gltf"])
        doc["extras"] = {"pad": "x" * (2 * 1024 * 1024)}
        with self.assertRaises(HerdrTeamError) as caught:
            self.folder({"big.gltf": json.dumps(doc).encode("utf-8")}).read("big.gltf")
        self.assertEqual(caught.exception.code, "canvas_limit")
        self.assertIn("export a .glb", str(caught.exception))


class Hostile(unittest.TestCase):
    """Every entry of the hostile corpus is refused exactly as recorded (the ``op`` ones through the canvas, below)."""

    def cases(self, source):
        found = []
        for path in sorted(HOSTILE.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc["source"] == source:
                found.append((path.stem, doc))
        self.assertTrue(found, source)
        return found

    def refused(self, name, files, src, expect):
        folder = Folder(files)
        self.addCleanup(folder.close)
        with self.assertRaises(HerdrTeamError, msg=name) as caught:
            folder.read(src)
        self.assertEqual(caught.exception.code, expect["code"], "{}: {}".format(name, caught.exception))
        if expect.get("contains"):
            self.assertIn(expect["contains"], str(caught.exception), name)
        self.assertTrue(str((caught.exception.details or {}).get("field") or "").startswith(FIELD) or expect["code"] == "path_refused", name)

    def test_gltf_documents(self):
        for name, case in self.cases("gltf"):
            with self.subTest(case=name):
                texture = None
                spec = case.get("texture") or {}
                if spec.get("png_header"):
                    texture = M.png_header(*spec["png_header"])
                elif spec.get("bytes"):
                    texture = spec["bytes"].encode("utf-8")
                self.refused(name, textured(case.get("patch"), texture), "box.gltf", case["expect"])

    def test_glb_containers(self):
        for name, case in self.cases("glb"):
            with self.subTest(case=name):
                self.refused(name, {"m.glb": mutated_glb(case["glb"])}, "m.glb", case["expect"])

    def test_generated(self):
        for name, case in self.cases("generated"):
            with self.subTest(case=name):
                self.assertEqual(case["generate"], "triangles")
                self.refused(name, {"t.glb": many_triangles(_gltf.MAX_TRIANGLES + 1)}, "t.glb", case["expect"])


class InTheScene(CanvasRig):
    def setUp(self):
        super().setUp()
        self.art = self.artifacts()
        (self.art / "models").mkdir()
        (self.art / "models" / "arm-v3.glb").write_bytes(M.box_glb((0.4, 0.8, 0.3), "arm-v3"))
        (self.art / "models" / "notes.txt").write_text("not a model", encoding="utf-8")
        for name, data in M.textured_box_gltf().items():
            (self.art / "models" / name).write_bytes(data)

    def op(self, **obj):
        return {"op": "scene3d", "id": "m", "intent": "a model", "objects": [{"id": "bench", "shape": "box", "size": [2, 0.9, 1]},
                                                                            dict({"id": "arm", "shape": "gltf", "on": "bench"}, **obj)]}

    def element(self):
        return next(e for e in self.scene()["elements"] if e.get("alias") == "m")

    def test_the_model_is_scaled_and_stored_as_an_asset(self):
        self.ok(self.op(src="models/arm-v3.glb", height=1.6))
        el = self.element()
        solved = _solver.expand(el["solved"])
        self.assertEqual(solved["objects"]["arm"]["ext"], [0.8, 1.6, 0.6])
        model = solved["models"]["arm"]
        self.assertEqual((model["native"], model["scale"], model["tris"]), ([0.4, 0.8, 0.3], 2.0, 12))
        asset = solved["objects"]["arm"]["asset"]
        self.assertRegex(asset, r"^[0-9a-f]{32}\.glb$")
        found = list(Path(os.fspath(self.team.team_json)).parent.rglob(asset))
        self.assertEqual(len(found), 1, "the GLB is in the asset store")
        self.assertEqual(found[0].read_bytes(), (self.art / "models" / "arm-v3.glb").read_bytes(), "a .glb is stored as it is")

    def test_a_model_scaled_past_the_limits_is_refused(self):
        # QA phase34 L5: a node scale of 1e30 read back as a 31-digit size; a zero matrix as 0×0×0 (drawn as a unit cube).
        huge, _blob = M.box_doc((1, 1, 1), "huge")
        huge["nodes"][0]["scale"] = [1e30, 1e30, 1e30]
        (self.art / "models" / "huge.glb").write_bytes(M.glb(huge, _blob))
        flat, blob = M.box_doc((1, 1, 1), "flat")
        flat["nodes"][0]["matrix"] = [0.0] * 16
        (self.art / "models" / "flat.glb").write_bytes(M.glb(flat, blob))
        for src, words in (("models/huge.glb", "units across as placed"), ("models/flat.glb", "the model has no size")):
            self.assertIn(words, self.refused(self.op(src=src))["message"], src)
        # A height brings a big native model back within the limit.
        big, blob = M.box_doc((1, 1, 1), "big")
        big["nodes"][0]["scale"] = [20000, 20000, 20000]
        (self.art / "models" / "big.glb").write_bytes(M.glb(big, blob))
        self.assertEqual(self.refused(self.op(src="models/big.glb"))["details"]["field"], "objects[1].src")
        self.ok(self.op(src="models/big.glb", height=1.2))

    def test_size_fits_the_model_uniformly(self):
        self.ok(self.op(src="models/arm-v3.glb", size=[1, 1, 1]))
        self.assertEqual(_solver.expand(self.element()["solved"])["objects"]["arm"]["ext"], [0.5, 1.0, 0.375])

    def test_a_packed_gltf_and_what_look_says(self):
        self.ok(self.op(src="models/box.gltf", height=0.5, label="crate"))
        model = _solver.expand(self.element()["solved"])["models"]["arm"]
        self.assertTrue(model["facts"]["packed"])
        text = C.look_text(C.look(self.layout, self.team, "alpha-worker", full=True))
        self.assertIn('arm gltf "models/box.gltf" 0.5×0.5×0.5 "crate" on bench', text)
        self.assertIn("12 triangles, 1 mesh, 1 material, native 1×1×1 scaled 0.5, nodes box, packed from .gltf", text)

    def test_a_later_edit_of_the_file_does_not_change_the_scene(self):
        self.ok(self.op(src="models/arm-v3.glb"))
        before = self.element()["solved"]
        (self.art / "models" / "arm-v3.glb").write_bytes(M.box_glb((4, 4, 4), "bigger"))
        self.ok({"op": "patch", "id": "m", "set": {"camera": "top"}, "intent": "a view"})
        self.assertEqual(self.element()["solved"], before, "the model was snapshotted")
        self.ok({"op": "patch", "id": "m", "update": {"objects": [{"id": "arm", "src": "models/arm-v3.glb"}]}, "intent": "re-read"})
        self.assertEqual(_solver.expand(self.element()["solved"])["models"]["arm"]["native"], [4.0, 4.0, 4.0], "naming it again re-reads it")
        (self.art / "models" / "arm-v3.glb").write_bytes(M.box_glb((1, 2, 1), "again"))
        self.ok({"op": "patch", "id": "m", "relayout": "full", "intent": "re-read every model"})
        self.assertEqual(_solver.expand(self.element()["solved"])["models"]["arm"]["native"], [1.0, 2.0, 1.0], "relayout: full re-reads")

    def test_hostile_ops_are_refused(self):
        for path in sorted(HOSTILE.glob("*.json")):
            case = json.loads(path.read_text(encoding="utf-8"))
            if case["source"] != "op":
                continue
            with self.subTest(case=path.stem):
                refused = self.refused(self.op(src=case["src"]))
                self.assertEqual(refused["code"], case["expect"]["code"], refused)
                if case["expect"].get("contains"):
                    self.assertIn(case["expect"]["contains"], refused["message"])

    def test_the_scene_triangle_cap(self):
        with mock.patch.object(_gltf, "MAX_SCENE_TRIANGLES", 20):
            refused = self.refused({"op": "scene3d", "id": "m", "intent": "two", "objects": [
                {"id": "a", "shape": "gltf", "src": "models/arm-v3.glb"}, {"id": "b", "shape": "gltf", "src": "models/box.gltf"}]})
        self.assertEqual(refused["code"], "canvas_limit")
        self.assertIn("24 triangles", refused["message"])

    def test_no_artifacts_folder(self):
        self.set_config(project_dir=None)
        refused = self.refused(self.op(src="models/arm-v3.glb"))
        self.assertEqual(refused["code"], "artifacts_unset")


def _timed(fn):
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


if __name__ == "__main__":
    unittest.main()

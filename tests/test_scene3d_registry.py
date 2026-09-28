"""The scene3d primitive registry (canvas v2 phase 4, 3.2 and 8.2): discovery, the contract, and one module is enough.

Every registered primitive passes ``scene3d_conformance``, and its example op
applies on a real canvas. ``tests/fixtures/prim_torus.py``, loaded through the
registry's test hook and nothing else, is a shape the op takes, the solver
places, ``look`` describes, the projection draws and the catalog lists.
"""
from __future__ import annotations

import json
import shutil
import sys
import unittest

import scene3d_conformance
from registry_conformance import assert_contains_in_order, assert_discovery_order
from support import PLUGIN_ROOT
from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team import canvas_scene3d as S
from herdr_team.canvas_scene3d import _describe, _project, _solver, _spec, layouts, relations

MODULE = "tests.fixtures.prim_torus"
BUILT_IN = ["box", "sphere", "cylinder", "cone", "plane", "text3d", "arrow3d", "group", "gltf"]
MODELS = PLUGIN_ROOT / "tests" / "fixtures" / "canvas_artifacts" / "scene3d" / "models"


class Registry(unittest.TestCase):
    def test_the_built_in_primitives_in_their_order(self):
        assert_contains_in_order(self, S.names(), BUILT_IN)
        owners = []
        for name in S.names():
            short = S._OWNER[name].rsplit(".", 1)[-1]
            if S._OWNER[name].startswith(S.__name__ + ".") and short not in owners:
                owners.append(short)
        assert_discovery_order(self, S, owners)
        self.assertEqual(S.loaders(), ["gltf"])
        self.assertTrue(S.get("group").container)
        self.assertIsNone(S.get("nope"))
        self.assertIsNone(S.get(3))

    def test_every_primitive_conforms(self):
        for prim in S.primitives():
            with self.subTest(primitive=prim.name):
                scene3d_conformance.check_primitive(self, prim)

    def test_a_second_primitive_of_the_same_name_is_refused(self):
        with self.assertRaises(ValueError):
            S.register(S.Primitive(name="box", params=(), normalize=lambda c, o, f: {}, extent=lambda p: (1, 1, 1), faces=lambda p, d: []))
        with self.assertRaises(ValueError):
            S.register(S.Primitive(name="Bad Name", params=(), normalize=lambda c, o, f: {}, extent=lambda p: (1, 1, 1),
                                   faces=lambda p, d: []))
        with self.assertRaises(ValueError, msg="a param may not shadow a common field"):
            S.register(S.Primitive(name="clash", params=("label",), normalize=lambda c, o, f: {}, extent=lambda p: (1, 1, 1),
                                   faces=lambda p, d: []))

    def test_relations_and_layouts_are_registries_too(self):
        self.assertEqual(relations.names(), ["on", "above", "below", "inside", "left_of", "right_of", "in_front_of", "behind", "around"])
        self.assertEqual(layouts.names(), ["row", "stack", "grid", "ring", "free"])
        with self.assertRaises(ValueError):
            relations.register_relation(relations.Relation("sideways", "diagonal", ("x",), lambda p: {}))
        extra = relations.Relation("atop_far", "vertical", ("y",), lambda p: {"y": p.ref[4] + 10}, order=999)
        relations.register_relation(extra)
        self.addCleanup(relations._unregister, "atop_far")
        self.assertIn("atop_far", _spec.common_fields(), "a new relation is an object field at once")
        solved = _solver.solve([{"id": "a", "shape": "box"}, {"id": "b", "shape": "box", "atop_far": "a"}])
        self.assertEqual(solved["objects"]["b"]["pos"][1], 11.0)

    def test_the_catalog(self):
        found = S.catalog()
        self.assertEqual([p["name"] for p in found["primitives"]][:len(BUILT_IN)], BUILT_IN)
        self.assertEqual([r["name"] for r in found["relations"]], relations.names())
        self.assertEqual([l["name"] for l in found["layouts"]], layouts.names())
        box = next(p for p in found["primitives"] if p["name"] == "box")
        self.assertEqual(box["params"], ["size"])
        self.assertEqual(json.loads(box["example"])["op"], "scene3d")
        self.assertEqual(found["limits"], {"objects": 150, "links": 150, "element_bytes": 48 * 1024})
        one = S.catalog("cylinder")
        self.assertEqual([p["name"] for p in one["primitives"]], ["cylinder"])
        self.assertEqual(S.catalog("around")["relations"][0]["name"], "around")
        json.dumps(found)


class Fixtures(unittest.TestCase):
    def test_the_committed_models_and_solved_scenes_are_current(self):
        """``tests/fixtures/scene3d/make_fixtures.py`` writes the models and the page's solved fixtures (I-11): a solver
        change regenerates them (``python3 tests/fixtures/scene3d/make_fixtures.py``)."""
        sys.path.insert(0, str(PLUGIN_ROOT / "tests" / "fixtures" / "scene3d"))
        import make_fixtures

        stale = [path for path, data in {**make_fixtures.committed(), **make_fixtures.web_fixtures()}.items()
                 if not path.is_file() or path.read_bytes() != data]
        self.assertEqual(stale, [])


class Examples(CanvasRig):
    def test_every_example_applies(self):
        art = self.artifacts()
        shutil.copytree(MODELS, art / "models")
        for op in scene3d_conformance.examples():
            with self.subTest(example=op["id"]):
                self.ok(dict(op, intent="the catalog example"))


class OneModulePrimitive(CanvasRig):
    TORUS = {"op": "scene3d", "id": "rings", "title": "Rings", "intent": "one module", "objects": [
        {"id": "base", "shape": "plane", "size": [4, 3]},
        {"id": "t", "shape": "torus", "radius": 0.6, "tube": 0.2, "tone": "accent", "label": "ring", "on": "base"},
        {"id": "b", "shape": "box", "right_of": "t", "on": "base"}]}

    def setUp(self):
        super().setUp()
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        self.assertIsNone(S.get("torus"))
        S._load_extra(MODULE)
        self.addCleanup(S._unload, MODULE)

    def test_it_conforms_and_is_listed(self):
        prim = S.get("torus")
        scene3d_conformance.check_primitive(self, prim)
        self.assertIn("torus", S.names())
        self.assertEqual(S.catalog("torus")["primitives"][0]["params"], ["radius", "tube"])

    def test_the_op_the_solver_look_and_the_picture_take_it(self):
        self.ok(self.TORUS)
        el = next(e for e in self.scene()["elements"] if e.get("alias") == "rings")
        solved = _solver.expand(el["solved"])
        self.assertEqual(solved["objects"]["t"]["ext"], [1.6, 0.4, 1.6])
        self.assertEqual(solved["objects"]["t"]["pos"][1], 0.02, "on the plane")
        self.assertGreaterEqual(solved["objects"]["b"]["aabb"][0], solved["objects"]["t"]["aabb"][3], "b is right of the torus")
        text = C.look_text(C.look(self.layout, self.team, "alpha-worker"))
        self.assertIn('t torus r0.6 t0.2 "ring" on base', text)
        picture = _project.project(el, "iso", (0, 0, 400, 300))
        polys = [p for p in picture["items"][0]["items"] if p.get("k") == "poly" and p.get("fill") == "mat.accent.top"]
        self.assertNotIn("clip", picture["items"][0], "the fit keeps it inside: no clip")
        self.assertTrue(polys, "its faces are drawn")
        self.assertEqual(D.validate(D.display_list(self.scene())), [])
        self.assertIn("t torus", "\n".join(_describe.gist(el, True)))
        refused = self.refused(dict(self.TORUS, id="bad", objects=[{"id": "t", "shape": "torus", "radius": 0.2, "tube": 0.3}]))
        self.assertEqual(refused["details"]["field"], "objects[0].tube")

    def test_unloaded_it_is_gone(self):
        S._unload(MODULE)
        self.assertIsNone(S.get("torus"))
        refused = self.refused(self.TORUS)
        self.assertIn("box, sphere", refused["message"])
        self.assertEqual(refused["details"]["field"], "objects[1].shape")
        S._load_extra(MODULE)


if __name__ == "__main__":
    unittest.main()

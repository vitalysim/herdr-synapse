"""The layout registry (canvas v2 phase 2, 2.1 and 2.2): discovery, the contract every layout keeps, one-module layouts,
and the 0.21 facade (``canvas_layout``)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from layout_conformance import LayoutConformance, close

from herdr_team import canvas_layout as L
from herdr_team import canvas_layouts as CL
from herdr_team.canvas_layouts import LEdge, LNode, LayoutError, LayoutRequest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layouts"


class Registry(unittest.TestCase):
    def test_every_layout_module_is_found_with_no_list(self):
        self.assertEqual(CL.modules()[:2], ["layers", "tree"])
        self.assertEqual(set(CL.modules()), {"layers", "tree", "radial", "force", "grid", "stack"})
        self.assertEqual(CL.names(), ["layers", "flow", "layered", "tree", "radial", "force", "grid", "row", "column"])
        self.assertIs(CL.get("flow"), CL.get("layers"))
        self.assertIs(CL.get("layered"), CL.get("layers"))
        self.assertIsNone(CL.get("spiral"))

    def test_a_second_layout_under_a_taken_name_is_refused(self):
        with self.assertRaises(ValueError):
            CL.register(CL.Layout(name="grid", run=lambda r: CL.LayoutResult(positions={})))
        with self.assertRaises(ValueError):
            CL.register(CL.Layout(name="other", run=lambda r: CL.LayoutResult(positions={}), aliases=("flow",)))
        with self.assertRaises(ValueError):
            CL.register(CL.Layout(name="odd", run=lambda r: CL.LayoutResult(positions={}), directions=("sideways",)))

    def test_malformed_requests_name_the_field(self):
        a, b = LNode("a", 10, 10, 0), LNode("b", 10, 10, 1)
        cases = [
            (LayoutRequest(nodes=(a, b), edges=(LEdge("e", "a", "z"),)), "edges[0].b"),
            (LayoutRequest(nodes=(a, LNode("a", 5, 5, 1))), "nodes[1].id"),
            (LayoutRequest(nodes=(a,), direction="sideways"), "direction"),
            (LayoutRequest(nodes=(a,), options={"spin": 1}), "options.spin"),
            (LayoutRequest(nodes=(LNode("a", 10, 10, 0, group="g"),)), "nodes[0].group"),
            (LayoutRequest(nodes=(a,), groups=(CL.LGroup("g", parent="h"), CL.LGroup("h", parent="g"))), "groups[0].parent"),
            (LayoutRequest(nodes=(a, b), same_rank=(("a", "q"),)), "same_rank[0]"),
            (LayoutRequest(nodes=(LNode("a", -1, 10, 0),)), "nodes[0]"),
        ]
        for request, field in cases:
            with self.subTest(field=field), self.assertRaises(LayoutError) as caught:
                CL.run("layers", request)
            self.assertEqual(caught.exception.field, field)
        with self.assertRaises(LayoutError):
            CL.run("spiral", LayoutRequest(nodes=(a,)))

    def test_a_layout_that_moves_a_pin_is_a_bug_the_contract_catches(self):
        broken = CL.Layout(name="sloppy", run=lambda r: CL.LayoutResult(positions={n.id: (0.0, 0.0) for n in r.nodes}))
        CL.register(broken)
        self.addCleanup(CL._REGISTRY.pop, "sloppy", None)
        with self.assertRaises(LayoutError):
            CL.run("sloppy", LayoutRequest(nodes=(LNode("a", 10, 10, 0, pin=(5.0, 5.0)),)))

    def test_run_rounds_and_computes_the_bbox(self):
        result = CL.run("row", LayoutRequest(nodes=(LNode("a", 10.333, 10, 0), LNode("b", 20, 5, 1)), gap=7.005))
        self.assertEqual(result.positions, {"a": (0.0, 0.0), "b": (17.34, 0.0)})
        self.assertEqual(result.bbox, (0.0, 0.0, 37.34, 10.0))
        self.assertEqual(CL.run("row", LayoutRequest(nodes=())).positions, {})

    def test_stacks_and_grids_do_not_hold_pins_and_say_so(self):
        result = CL.run("row", LayoutRequest(nodes=(LNode("a", 10, 10, 0, pin=(99.0, 99.0)), LNode("b", 10, 10, 1))))
        self.assertEqual(result.positions["a"], (0.0, 0.0))
        self.assertEqual(result.notes, ("pin_ignored a",))


class Conformance(LayoutConformance, unittest.TestCase):
    def test_every_registered_layout_keeps_the_contract(self):
        for layout in CL.layouts():
            with self.subTest(layout=layout.name):
                self.check_layout(layout.name)


class DroppedInLayout(LayoutConformance, unittest.TestCase):
    """A new layout is one file in the package: copied as ``diagonal.py`` into the package's search path, the registry
    finds it on its next load, it keeps the contract, and a graph can name it (G10)."""

    NAME = "herdr_team.canvas_layouts.diagonal"

    def setUp(self):
        folder = Path(tempfile.mkdtemp(prefix="layout-drop-"))
        self.addCleanup(shutil.rmtree, folder, True)
        shutil.copy(FIXTURES / "layout_diagonal.py", folder / "diagonal.py")
        self.folder = str(folder)
        self.before = (CL.names(), L.LAYOUTS)
        CL.__path__.append(self.folder)
        self.addCleanup(self._restore)
        CL._reload()

    def _restore(self):
        if self.folder in CL.__path__:
            CL.__path__.remove(self.folder)
        sys.modules.pop(self.NAME, None)
        CL._reload()

    def test_the_file_is_a_layout_with_no_list_edited(self):
        self.assertEqual(CL.modules()[-1], "diagonal")
        self.assertIn("diagonal", L.LAYOUTS)
        self.check_layout("diagonal")
        corners = L.layout(["a", "b", "c"], [("a", "b")], "diagonal")
        self.assertEqual(corners, {"a": (0.0, 0.0), "b": (220.0, 120.0), "c": (440.0, 240.0)})

    def test_a_graph_can_name_it(self):
        from test_kind_graph import GraphRig

        rig = GraphRig("test_nothing")
        rig.setUp()
        self.addCleanup(rig.doCleanups)
        applied = rig.ok({"op": "graph", "id": "g", "layout": "diagonal", "nodes": ["a", "b"], "edges": ["a -> b"], "at": [0, 0], "intent": "t"})
        nodes = [rig.el(i) for i in applied["ids"] if rig.el(i)["type"] == "box"]
        self.assertLess(nodes[0]["x"], nodes[1]["x"])
        self.assertLess(nodes[0]["y"], nodes[1]["y"])

    def test_taking_it_out_restores_the_tables(self):
        self._restore()
        self.assertEqual((CL.names(), L.LAYOUTS), self.before)


class Facade(unittest.TestCase):
    """``canvas_layout`` keeps its 0.21 signatures over the registry."""

    def test_layout_and_plan_keep_their_shape(self):
        positions, bends = L.plan(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")])
        self.assertEqual(list(bends), [3])
        self.assertEqual(len(bends[3]), 2)
        self.assertEqual(min(x for x, _y in positions.values()), 0)
        self.assertEqual(L.layout(["a", "b"], [("a", "b")], "no-such-layout"), L.layout(["a", "b"], [("a", "b")], "layered"))
        self.assertEqual(L.layout([], []), {})
        self.assertEqual((L.NODE_W, L.NODE_H, L.SPACING, L.LAYER_GAP), (160, 60, 60, 100))
        self.assertIn("layered", L.LAYOUTS)
        self.assertIn("grid", L.LAYOUTS)
        self.assertNotIn("row", L.LAYOUTS, "only the layouts that use edges, and grid")

    def test_every_facade_layout_places_nodes_apart_in_every_direction(self):
        sizes = {"a": (160.0, 60.0), "b": (320.0, 140.0), "c": (200.0, 60.0), "d": (160.0, 200.0)}
        edges = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]
        for algorithm in L.LAYOUTS:
            for direction in L.DIRECTIONS:
                with self.subTest(algorithm=algorithm, direction=direction):
                    positions = L.layout(list(sizes), edges, algorithm, direction, sizes=sizes)
                    boxes = {n: (x, y, x + sizes[n][0], y + sizes[n][1]) for n, (x, y) in positions.items()}
                    names = sorted(boxes)
                    for i, a in enumerate(names):
                        for b in names[i + 1:]:
                            self.assertFalse(close(boxes[a], boxes[b], 0), (a, b))


if __name__ == "__main__":
    unittest.main()

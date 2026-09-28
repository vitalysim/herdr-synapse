"""The ``force`` layout (canvas v2 phase 2, 2.3): seeded, snapped to cells, pins take their cells, incremental from seeds."""
from __future__ import annotations

import unittest

from layout_conformance import boxes_of, close, sample_graph

from herdr_team import canvas_layouts as CL
from herdr_team.canvas_layouts import LNode, LayoutError, LayoutRequest


class Force(unittest.TestCase):
    def test_deterministic_apart_and_iterations_is_an_option(self):
        ns, es = sample_graph(15, 5, 2)
        request = LayoutRequest(nodes=tuple(ns), edges=tuple(es), incremental=False)
        first = CL.run("force", request)
        self.assertEqual(first.positions, CL.run("force", request).positions)
        boxes = boxes_of(request, first.positions)
        names = sorted(boxes)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertFalse(close(boxes[a], boxes[b], request.gap), (a, b))
        quick = CL.run("force", LayoutRequest(nodes=tuple(ns), edges=tuple(es), options={"iterations": 5}, incremental=False))
        self.assertNotEqual(quick.positions, first.positions)
        with self.assertRaises(LayoutError):
            CL.run("force", LayoutRequest(nodes=tuple(ns), options={"iterations": 0}))

    def test_a_pin_keeps_its_place_and_its_cells(self):
        ns, es = sample_graph(12, 4, 3)
        pinned = tuple(LNode(n.id, n.w, n.h, n.order, pin=(40.0, 40.0) if n.order == 0 else None) for n in ns)
        request = LayoutRequest(nodes=pinned, edges=tuple(es))
        result = CL.run("force", request)
        self.assertEqual(result.positions["n0"], (40.0, 40.0))
        boxes = boxes_of(request, result.positions)
        for n in boxes:
            if n != "n0":
                self.assertFalse(close(boxes[n], boxes["n0"], request.gap), n)

    def test_incremental_starts_from_the_seeds(self):
        ns, es = sample_graph(12, 4, 3)
        first = CL.run("force", LayoutRequest(nodes=tuple(ns), edges=tuple(es), incremental=False))
        seeded = tuple(LNode(n.id, n.w, n.h, n.order, seed=first.positions[n.id]) for n in ns) + (LNode("new", 160, 60, 50),)
        again = CL.run("force", LayoutRequest(nodes=seeded, edges=tuple(es)))
        moved = sum(1 for n in ns if abs(again.positions[n.id][0] - first.positions[n.id][0]) + abs(again.positions[n.id][1] - first.positions[n.id][1]) > 400)
        self.assertLess(moved, len(ns) / 2)


if __name__ == "__main__":
    unittest.main()

"""The ``radial`` layout (canvas v2 phase 2, 2.3): rings around a centre, wedges for trees, pins held."""
from __future__ import annotations

import math
import unittest

from layout_conformance import boxes_of, close

from herdr_team import canvas_layouts as CL
from herdr_team.canvas_layouts import LEdge, LNode, LayoutRequest


def centre(result, request, n):
    node = next(x for x in request.nodes if x.id == n)
    return result.positions[n][0] + node.w / 2.0, result.positions[n][1] + node.h / 2.0


class Radial(unittest.TestCase):
    def test_a_star_puts_its_hub_in_the_middle_and_its_leaves_on_one_ring(self):
        request = LayoutRequest(nodes=tuple(LNode(n, 120, 40, i) for i, n in enumerate(["hub"] + ["l{}".format(i) for i in range(8)])),
                                edges=tuple(LEdge(str(i), "hub", "l{}".format(i)) for i in range(8)))
        result = CL.run("radial", request)
        hx, hy = centre(result, request, "hub")
        radii = [math.hypot(centre(result, request, "l{}".format(i))[0] - hx, centre(result, request, "l{}".format(i))[1] - hy) for i in range(8)]
        self.assertLess(max(radii) - min(radii), 1.0)
        boxes = boxes_of(request, result.positions)
        names = sorted(boxes)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertFalse(close(boxes[a], boxes[b], request.gap), (a, b))

    def test_a_graph_with_a_cycle_gets_breadth_first_rings_and_pins_hold(self):
        ids = ["a", "b", "c", "d", "e"]
        edges = tuple(LEdge(str(i), a, b) for i, (a, b) in enumerate([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("d", "e")]))
        request = LayoutRequest(nodes=tuple(LNode(n, 120, 40, i, pin=(10.0, 10.0) if n == "e" else None) for i, n in enumerate(ids)), edges=edges)
        result = CL.run("radial", request)
        self.assertEqual(result.positions["e"], (10.0, 10.0))
        boxes = boxes_of(request, result.positions)
        for n in ids[:-1]:
            self.assertFalse(close(boxes[n], boxes["e"], request.gap), n)


if __name__ == "__main__":
    unittest.main()

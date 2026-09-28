"""The ``grid`` layout (canvas v2 phase 2, 2.3): row-major cells sized by their row and column, ``cols`` and ``align``."""
from __future__ import annotations

import unittest

from herdr_team import canvas_layouts as CL
from herdr_team.canvas_layouts import LNode, LayoutError, LayoutRequest


class Grid(unittest.TestCase):
    def test_rows_and_columns_take_their_largest_member(self):
        nodes = (LNode("a", 100, 40, 0), LNode("b", 50, 80, 1), LNode("c", 200, 20, 2), LNode("d", 10, 10, 3))
        result = CL.run("grid", LayoutRequest(nodes=nodes, gap=10, options={"cols": 2, "align": "start"}))
        self.assertEqual(result.positions, {"a": (0.0, 0.0), "b": (210.0, 0.0), "c": (0.0, 90.0), "d": (210.0, 90.0)})
        centred = CL.run("grid", LayoutRequest(nodes=nodes, gap=10, options={"cols": 2}))
        self.assertEqual(centred.positions["a"], (50.0, 20.0))
        self.assertEqual(len({y for _x, y in CL.run("grid", LayoutRequest(nodes=nodes, options={"align": "start"})).positions.values()}), 2, "default: ceil(sqrt(n)) columns")
        for bad in ({"cols": 0}, {"align": "end"}):
            with self.assertRaises(LayoutError):
                CL.run("grid", LayoutRequest(nodes=nodes, options=bad))


if __name__ == "__main__":
    unittest.main()

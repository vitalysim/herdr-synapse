"""The ``row`` and ``column`` layouts (canvas v2 phase 2, 2.3 and 4.3): in order, ``gap`` apart, aligned."""
from __future__ import annotations

import unittest

from herdr_team import canvas_layouts as CL
from herdr_team.canvas_layouts import LNode, LayoutError, LayoutRequest


class Stack(unittest.TestCase):
    NODES = (LNode("b", 50, 80, 1), LNode("a", 100, 40, 0), LNode("c", 30, 20, 2))

    def test_row_in_order_with_each_alignment(self):
        self.assertEqual(CL.run("row", LayoutRequest(nodes=self.NODES, gap=10)).positions, {"a": (0.0, 0.0), "b": (110.0, 0.0), "c": (170.0, 0.0)})
        self.assertEqual(CL.run("row", LayoutRequest(nodes=self.NODES, gap=10, options={"align": "center"})).positions["a"], (0.0, 20.0))
        self.assertEqual(CL.run("row", LayoutRequest(nodes=self.NODES, gap=10, options={"align": "end"})).positions["c"], (170.0, 60.0))

    def test_column_in_order(self):
        result = CL.run("column", LayoutRequest(nodes=self.NODES, gap=10, options={"align": "end"}))
        self.assertEqual(result.positions, {"a": (0.0, 0.0), "b": (50.0, 50.0), "c": (70.0, 140.0)})
        with self.assertRaises(LayoutError):
            CL.run("column", LayoutRequest(nodes=self.NODES, options={"align": "stretch"}))


if __name__ == "__main__":
    unittest.main()

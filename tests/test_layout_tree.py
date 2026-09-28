"""The ``tree`` layout (canvas v2 phase 2, 2.3): parents from data or edges, levels, sides, and pinned subtrees."""
from __future__ import annotations

import unittest

from layout_conformance import boxes_of, close

from herdr_team import canvas_layouts as CL
from herdr_team.canvas_layouts import LEdge, LNode, LayoutError, LayoutRequest


def tree(parents, sizes=None, **fields):
    nodes = tuple(LNode(n, (sizes or {}).get(n, 120.0), 40.0, i, data={"parent": p}) for i, (n, p) in enumerate(parents))
    return LayoutRequest(nodes=nodes, gap=24, rank_gap=60, **fields)


class Tree(unittest.TestCase):
    def test_levels_run_down_and_a_parent_centres_over_its_children(self):
        request = tree([("r", None), ("a", "r"), ("b", "r"), ("c", "r"), ("a1", "a")])
        result = CL.run("tree", request)
        p = result.positions
        self.assertEqual({p["a"][1], p["b"][1], p["c"][1]}, {p["r"][1] + 40 + 60})
        self.assertAlmostEqual(p["r"][0] + 60, (p["a"][0] + p["c"][0] + 120) / 2.0)
        self.assertEqual(p["a"][0] + 120 + 24, p["b"][0], "siblings gap apart")
        boxes = boxes_of(request, p)
        names = sorted(boxes)
        for i, x in enumerate(names):
            for y in names[i + 1:]:
                self.assertFalse(close(boxes[x], boxes[y], 24), (x, y))

    def test_parents_come_from_edges_when_no_node_names_one(self):
        request = LayoutRequest(nodes=tuple(LNode(n, 100, 40, i) for i, n in enumerate("rabc")),
                                edges=(LEdge("1", "r", "a"), LEdge("2", "r", "b"), LEdge("3", "b", "c"), LEdge("4", "a", "c")))
        p = CL.run("tree", request).positions
        self.assertGreater(p["c"][1], p["a"][1])
        self.assertLess(abs(p["c"][0] - p["a"][0]), 1, "c hangs from a: the breadth-first walk reaches it through a first")

    def test_both_sides_split_by_leaf_count_and_right_grows_right(self):
        parents = [("root", None), ("a", "root"), ("a1", "a"), ("a2", "a"), ("b", "root"), ("c", "root"), ("c1", "c")]
        p = CL.run("tree", tree(parents, direction="right", options={"side": "both"})).positions
        right = [n for n in ("a", "b", "c") if p[n][0] > p["root"][0]]
        left = [n for n in ("a", "b", "c") if p[n][0] < p["root"][0]]
        self.assertEqual((right, left), (["a"], ["b", "c"]))
        self.assertLess(p["a1"][0] - p["a"][0], 0.001 + 1e9)
        self.assertGreater(p["a1"][0], p["a"][0])
        self.assertLess(p["c1"][0], p["c"][0])
        with self.assertRaises(LayoutError):
            CL.run("tree", tree(parents, options={"side": "up"}))

    def test_a_pinned_node_takes_its_subtree_along(self):
        parents = [("r", None), ("a", "r"), ("a1", "a"), ("a2", "a"), ("b", "r")]
        free = CL.run("tree", tree(parents)).positions
        pinned = tuple(LNode(n, 120, 40, i, data={"parent": p}, pin=(900.0, 500.0) if n == "a" else None) for i, (n, p) in enumerate(parents))
        request = LayoutRequest(nodes=pinned, gap=24, rank_gap=60)
        p = CL.run("tree", request).positions
        self.assertEqual(p["a"], (900.0, 500.0))
        self.assertEqual((p["a1"][0] - p["a"][0], p["a1"][1] - p["a"][1]), (free["a1"][0] - free["a"][0], free["a1"][1] - free["a"][1]))
        boxes = boxes_of(request, p)
        names = sorted(boxes)
        for i, x in enumerate(names):
            for y in names[i + 1:]:
                self.assertFalse(close(boxes[x], boxes[y], 24), (x, y))


if __name__ == "__main__":
    unittest.main()

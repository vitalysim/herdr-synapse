"""Treemaps (canvas v2 phase 3, 2.4): a nested tree, the squarified layout, shares in the gist."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series

from herdr_team.canvas_charts import treemap


class Treemap(unittest.TestCase):
    def test_the_example(self):
        found, drawing = run(example("treemap"))
        self.assertEqual([n["name"] for n in found.model["tree"]], ["Data", "ML", "Web"])
        self.assertEqual(found.gist[1], "largest: ML / GPUs $5,100 (32%) · Data / Warehouse $4,200 (27%) · Web / Compute $2,600 (17%)")
        self.assertEqual(found.gist[2], "team: Data 38% · ML 37% · Web 25%")
        s = series(found.option, "treemap")[0]
        self.assertEqual(s["data"], {"$doc": "tree"})
        self.assertEqual(s["levels"][0]["color"], ["chart.cat.0", "chart.cat.1", "chart.cat.2"])
        self.assertEqual(kinds(drawing)["rect"], 3 + 6)

    def test_squarify_fills_the_box_with_areas_in_proportion(self):
        rects = treemap.squarify([6, 6, 4, 3, 2, 2, 1], 0, 0, 600, 400)
        total = sum(w * h for _x, _y, w, h in rects)
        self.assertAlmostEqual(total, 600 * 400, places=3)
        self.assertAlmostEqual(rects[0][2] * rects[0][3] / (600 * 400), 6 / 24, places=6)
        for x, y, w, h in rects:
            self.assertTrue(-1e-6 <= x and x + w <= 600 + 1e-6 and -1e-6 <= y and y + h <= 400 + 1e-6)

    def test_depth_is_at_most_the_path(self):
        with self.assertRaises(Exception) as caught:
            run(dict(example("treemap"), depth=3))
        self.assertEqual(caught.exception.details["field"], "depth")


if __name__ == "__main__":
    unittest.main()

"""Scatter and bubble charts (canvas v2 phase 3, 2.4): correlation, slope, outliers by label, the trend line, sizes."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series, texts


class Scatter(unittest.TestCase):
    def test_the_example_reads_back_its_correlation(self):
        found, drawing = run(example("scatter"))
        self.assertTrue(found.gist[1].startswith("r = 0.90 (strong positive) · slope +0.0354 signups per spend"), found.gist)
        self.assertEqual(found.gist[2], "x $800…$9,800 · y 60…410")
        self.assertIn("markLine", series(found.option)[0], "the trend line")
        self.assertEqual(kinds(drawing)["rect"], 6)
        self.assertEqual(sum(1 for p in drawing if p["k"] == "line" and p.get("dash")), 1)

    def test_outliers_are_named_by_label(self):
        rows = [{"n": "p{}".format(i), "x": i, "y": 2 * i} for i in range(20)] + [{"n": "odd", "x": 5, "y": 90}]
        found, _ = run({"type": "scatter", "rows": rows, "x": "x", "y": "y", "label": "n"})
        self.assertIn("outliers: odd (5, 90)", found.gist)

    def test_bubbles_size_through_a_visual_map_and_groups_through_series(self):
        rows = [{"x": i, "y": i % 5, "s": i * 10, "g": "ab"[i % 2]} for i in range(10)]
        found, drawing = run({"type": "bubble", "rows": rows, "x": "x", "y": "y", "size": "s", "color": "g"})
        self.assertEqual(found.spec["type"], "scatter")
        self.assertEqual(len(series(found.option, "scatter")), 2)
        # One size map per series, each keeping its series' colour: a map with only symbolSize took the page theme's
        # colour ramp and coloured every point by its size (QA phase34 M6).
        maps = found.option["visualMap"]
        self.assertEqual([m["seriesIndex"] for m in maps], [0, 1])
        for index, m in enumerate(maps):
            self.assertEqual(m["inRange"]["symbolSize"], [4.0, 22.0])
            self.assertEqual(m["inRange"]["color"], [found.option["color"][index]] * 2)
        radii = sorted({p["w"] for p in drawing if p["k"] == "rect" and p.get("op") == 0.8})
        self.assertGreater(radii[-1], radii[0] * 2)
        self.assertEqual(found.vegalite["encoding"]["size"]["field"], "size")

    def test_many_points_are_sampled_and_say_so(self):
        from herdr_team import canvas_charts as CC
        from herdr_team.canvas_charts import _data

        rows = [{"x": i, "y": (i * 7919) % 1000} for i in range(6000)]
        normal, _ = CC.normalize_spec({"type": "scatter", "rows": [{}], "x": "x", "y": "y"})
        found = CC.compile(normal, _data.from_json(rows, "big", "sha"))
        self.assertIn("capped: drawn: 5000 of 6000 points (an even stride over x)", found.gist)
        self.assertEqual(len(found.doc["datasets"][0]["source"]), 5000)
        self.assertLessEqual(len(found.model["points"]), 400)


if __name__ == "__main__":
    unittest.main()

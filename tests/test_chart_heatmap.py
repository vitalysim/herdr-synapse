"""Heatmaps (canvas v2 phase 3, 2.4): cells on two category axes, the ramp, diverging palettes, the gist."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series


class Heatmap(unittest.TestCase):
    def test_the_example(self):
        found, drawing = run(example("heatmap"))
        self.assertEqual(len(found.model["xs"]), 24)
        self.assertEqual(found.model["ys"][:2], ["Mon", "Tue"], "declared ordinal keeps the file's order")
        self.assertEqual(found.gist[1], "max 14:00 × Tue 1,232 · min 00:00 × Sat 90")
        self.assertEqual(found.gist[2], "highest mean: Tue 521.5 (row) · 14:00 946.0 (column)", "one precision for both (QA phase34 L4)")
        self.assertEqual(found.option["visualMap"]["inRange"]["color"][0], "chart.seq.0")
        self.assertEqual(found.option["xAxis"]["data"][0], "00:00")
        self.assertEqual(series(found.option, "heatmap")[0]["encode"], {"x": "x", "y": "y", "value": "value"})
        self.assertGreaterEqual(kinds(drawing)["rect"], 168 + 9, "every cell and the ramp")

    def test_the_ramp_ends_carry_their_values_on_both_pictures(self):
        found, drawing = run(example("heatmap"))
        vm = found.option["visualMap"]
        self.assertEqual(vm["text"], ["1,232", "90"], "ECharts' text is [high, low]")
        self.assertEqual(found.frame.extra["ramp_text"], ["90", "1,232"])
        rx, ry, rw, rh = found.frame.extra["ramp"]
        ends = {p["lines"][0]["t"]: p for p in drawing if p["k"] == "text" and p["lines"][0]["t"] in ("90", "1,232")}
        self.assertEqual(set(ends), {"90", "1,232"})
        # Low on the left of the bar, high on its right, as ECharts lays a horizontal visualMap's text out.
        self.assertEqual((ends["90"]["anchor"], ends["1,232"]["anchor"]), ("end", "start"))
        self.assertLess(vm["left"], rx)
        self.assertEqual((vm["top"], vm["padding"], vm["textStyle"]["lineHeight"]), (round(ry, 2), 0, round(rh, 2)))

    def test_a_diverging_palette_centres_on_zero(self):
        rows = [{"a": a, "b": b, "v": v} for a, b, v in (("x", "p", -4), ("x", "q", 2), ("y", "p", 1), ("y", "q", 4))]
        found, drawing = run({"type": "heatmap", "rows": rows, "x": "a", "y": "b", "value": "v", "palette": "diverging"})
        self.assertEqual((found.option["visualMap"]["min"], found.option["visualMap"]["max"]), (-4, 4))
        fills = {p["fill"] for p in drawing if p["k"] == "rect"}
        self.assertIn("chart.div.0", fills)
        self.assertIn("chart.div.8", fills)


if __name__ == "__main__":
    unittest.main()

"""Bar charts (canvas v2 phase 3, 2.4): grouped and stacked bars, the option, the drawing, the gist, checks, v1 compat."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series, texts


class Bar(unittest.TestCase):
    def test_the_grouped_example(self):
        found, drawing = run(example("bar"))
        self.assertEqual(len(series(found.option, "bar")), 3)
        self.assertEqual(found.option["dataset"], {"$doc": "datasets"})
        self.assertEqual(found.doc["datasets"][0]["dimensions"], ["x", "s0", "s1", "s2"])
        self.assertEqual(found.option["yAxis"]["axisLabel"]["formatter"], {"$fmt": "compact|$"})
        self.assertEqual(kinds(drawing)["rect"], 36 + 3, "a bar per month and region, and a swatch per legend item")
        self.assertIn("$4M", texts(drawing))
        self.assertEqual(found.gist[1], "max EMEA 2026-08 $4.2M · min APAC 2026-01 $300K · total $79.5M")
        self.assertEqual(found.gist[2], "EMEA ↑ +38% · AMER ≈ −2% · APAC ↑ +210% (2026-01→2026-12)")
        self.assertEqual(found.vegalite["mark"], "bar")
        self.assertEqual(found.vegalite["encoding"]["xOffset"], {"field": "series"})

    def test_stacked_percent_and_horizontal(self):
        found, drawing = run(dict(example("bar"), stack="percent", horizontal=True))
        self.assertTrue(all(s["stack"] == "total" for s in series(found.option)))
        self.assertEqual(found.option["xAxis"]["type"], "value", "horizontal bars run along x")
        self.assertEqual(found.option["xAxis"]["max"], 1)
        self.assertIn("stacked to 100 % per month", found.gist)
        self.assertEqual(found.vegalite["encoding"]["x"]["stack"], "normalize")

    def test_highlight_colours_the_categories_of_one_series(self):
        rows = [{"team": t, "bugs": n} for t, n in (("web", 12), ("api", 30), ("ml", 7))]
        found, drawing = run({"type": "bar", "rows": rows, "x": "team", "y": "bugs", "highlight": ["api"]})
        first = series(found.option)[0]
        self.assertEqual((first["colorBy"], first["color"]), ("data", ["chart.highlight", "chart.dim", "chart.dim"]))
        self.assertEqual([p["fill"] for p in drawing if p["k"] == "rect"][:3], ["chart.highlight", "chart.dim", "chart.dim"])
        self.assertIn("top: api 30 · web 12 · ml 7", found.gist)
        self.assertTrue(series(found.option)[0].get("label"), "3 bars get their values written")


if __name__ == "__main__":
    unittest.main()

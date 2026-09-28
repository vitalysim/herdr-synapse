"""Box plots (canvas v2 phase 3, 2.4): quartiles, whiskers at 1.5 IQR or the extremes, outliers, per-group gist."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series

from herdr_team.canvas_charts import box


class Box(unittest.TestCase):
    def test_the_example(self):
        found, drawing = run(example("box"))
        linux = found.model["boxes"][0]
        self.assertEqual((linux["name"], linux["outs"], linux["out"]), ("linux", 1, [12.9]))
        self.assertEqual(found.gist[1], "highest median win 11.55 min · lowest linux 6.8 min")
        self.assertIn("linux: median 6.8 min · IQR 0.7 min · 1 outlier of 5", found.gist)
        self.assertEqual(series(found.option, "boxplot")[0]["data"], {"$doc": "boxes"})
        self.assertEqual(found.doc["refs"]["outliers"], [[0, 12.9]])
        self.assertEqual(kinds(drawing)["rect"], 3 + 1, "three boxes and one outlier")

    def test_whiskers_and_horizontal(self):
        self.assertEqual(box.summary([1, 2, 3, 4, 100], "minmax")["hi"], 100)
        self.assertEqual(box.summary([1, 2, 3, 4, 100], "1.5iqr")["out"], [100])
        found, _ = run(dict(example("box"), horizontal=True))
        self.assertEqual(found.option["yAxis"]["type"], "category")
        self.assertEqual(series(found.option, "scatter")[0]["encode"], {"x": 1, "y": 0})


if __name__ == "__main__":
    unittest.main()

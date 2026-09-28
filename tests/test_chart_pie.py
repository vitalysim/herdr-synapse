"""Pie and donut charts (canvas v2 phase 3, 2.4): shares, the Other slice, the hole and its total, the slices check."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series, texts

from herdr_team import canvas_charts as CC
from herdr_team.canvas_charts import pie


class Pie(unittest.TestCase):
    def test_the_donut_folds_the_tail_into_other(self):
        found, drawing = run(example("donut"))
        self.assertEqual([k for k, _v in found.model["slices"]], ["Search", "Direct", "Social", "Email", "Referral", "Other"])
        self.assertEqual(found.gist[1], "Search 42% · Direct 21% · Social 14% · Other 7.7% · total 12.5K")
        s = series(found.option, "pie")[0]
        self.assertGreater(s["radius"][0], 0, "a donut has a hole")
        self.assertEqual(s["data"], {"$doc": "slices"})
        self.assertEqual(s["color"][:2], ["chart.cat.0", "chart.cat.1"])
        self.assertEqual(found.doc["refs"]["slices"][0], {"name": "Search", "value": 5200, "label": {"formatter": "42%"}})
        self.assertIn("12.5K", texts(drawing), "the hole shows the total")
        self.assertEqual(kinds(drawing)["path"], 6)
        self.assertEqual(found.vegalite["mark"]["innerRadius"], 50)

    def test_a_pie_is_whole_and_refuses_negatives(self):
        found, _drawing = run(example("pie"))
        self.assertEqual(series(found.option, "pie")[0]["radius"][0], 0)
        self.assertEqual(found.gist[1], "EMEA 55% · AMER 36% · APAC 9.2% · total 79.5M")
        with self.assertRaises(Exception) as caught:
            run({"type": "pie", "rows": [{"a": "x", "v": -1}, {"a": "y", "v": 2}], "category": "a", "value": "v"})
        self.assertEqual(caught.exception.code, "chart_refused")

    def test_too_many_slices_is_a_check_with_a_fix(self):
        rows = [{"k": "k{}".format(i), "v": 10 + i} for i in range(14)]
        found, _ = run({"type": "pie", "rows": rows, "category": "k", "value": "v", "top": 13})
        problems = pie.slices_check(found.spec, found.model, found.frame)
        self.assertEqual(problems[0]["code"], "chart_pie_slices")
        self.assertEqual(problems[0]["fix"], {"set": {"type": "bar"}})
        found, _ = run({"type": "pie", "rows": rows, "category": "k", "value": "v"})
        self.assertEqual(pie.slices_check(found.spec, found.model, found.frame), [], "6 and Other by default")


if __name__ == "__main__":
    unittest.main()

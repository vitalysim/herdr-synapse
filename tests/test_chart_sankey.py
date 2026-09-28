"""Sankey diagrams (canvas v2 phase 3, 2.4): columns by longest path, stacked nodes, bands, cycles refused."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series

from herdr_team.canvas_charts import sankey


class Sankey(unittest.TestCase):
    def test_the_worked_example(self):
        found, drawing = run(example("sankey"))
        depths = {n["name"]: n["depth"] for n in found.model["nodes"]}
        self.assertEqual(depths, {"Search": 0, "Ads": 0, "Signup": 1, "Active": 2, "Churned": 2})
        self.assertEqual(found.gist[1], "largest: Search → Signup 5,400 · Signup → Active 4,300 · Signup → Churned 3,200")
        self.assertEqual(found.gist[3], "ends: Active 4,300 · Churned 3,200")
        s = series(found.option, "sankey")[0]
        self.assertEqual((s["layoutIterations"], s["links"]), (0, {"$doc": "links"}))
        self.assertEqual(s["data"][0], {"name": "Search", "depth": 0, "itemStyle": {"color": "chart.cat.0"}, "label": {"formatter": "Search 5,400"}})
        # The page writes each node's value as the drawing does (QA phase34 low).
        texts = {p["lines"][0]["t"] for p in drawing if p["k"] == "text"}
        self.assertLessEqual({d["label"]["formatter"] for d in s["data"]}, texts)
        self.assertEqual(found.doc["refs"]["nodes"][0], {"name": "Search", "depth": 0})
        self.assertEqual(kinds(drawing)["path"], 4)
        self.assertEqual(kinds(drawing)["rect"], 5)
        self.assertEqual(found.vegalite["mark"]["type"], "text", "no Vega-Lite equivalent: the v1 page gets the gist card")

    def test_bands_leave_and_arrive_in_order_and_conserve_thickness(self):
        found, _ = run(example("sankey"))
        placed = sankey.placement(found.spec, found.model, found.frame.plot)
        signup = placed["boxes"]["Signup"]
        into = sum(l["dst"][2] for l in placed["links"] if l["to"] == "Signup")
        self.assertAlmostEqual(into, signup[3], places=6)

    def test_a_name_with_template_braces_keeps_the_default_label(self):
        rows = [{"a": "{b}", "b": "y", "n": 3}, {"a": "x", "b": "y", "n": 2}]
        found, _ = run({"type": "sankey", "rows": rows, "source": "a", "target": "b", "value": "n"})
        items = {d["name"]: d for d in series(found.option, "sankey")[0]["data"]}
        self.assertNotIn("label", items["{b}"])
        self.assertEqual(items["x"]["label"], {"formatter": "x 2"})

    def test_a_cycle_and_a_self_loop_are_refused(self):
        rows = [{"a": "x", "b": "y", "n": 1}, {"a": "y", "b": "z", "n": 1}, {"a": "z", "b": "x", "n": 1}]
        with self.assertRaises(Exception) as caught:
            run({"type": "sankey", "rows": rows, "source": "a", "target": "b", "value": "n"})
        self.assertEqual(caught.exception.code, "chart_refused")
        self.assertIn("x → y → z → x", caught.exception.message)
        with self.assertRaises(Exception) as caught:
            run({"type": "sankey", "rows": [{"a": "x", "b": "x", "n": 1}], "source": "a", "target": "b", "value": "n"})
        self.assertIn("flows into itself", caught.exception.message)


if __name__ == "__main__":
    unittest.main()

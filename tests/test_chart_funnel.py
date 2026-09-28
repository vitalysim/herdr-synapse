"""Funnels (canvas v2 phase 3, 2.4): stages biggest first, conversions, the biggest drop."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series, texts


class Funnel(unittest.TestCase):
    def test_the_example(self):
        found, drawing = run(example("funnel"))
        self.assertEqual(found.gist[1], "Visit 12K → Signup form 5,400 → Verified 4,300 → First project 2,100")
        self.assertEqual(found.gist[2], "conversion: Visit→Signup form 45% · Signup form→Verified 80% · Verified→First project 49%")
        self.assertEqual(found.gist[3], "biggest drop Visit→Signup form (keeps 45%) · overall 18%")
        s = series(found.option, "funnel")[0]
        self.assertEqual((s["sort"], s["data"]), ("none", {"$doc": "stages"}))
        self.assertEqual(kinds(drawing)["path"], 4)
        self.assertIn("Visit 12K", texts(drawing))

    def test_sort_none_keeps_the_data_order(self):
        rows = [{"s": "a", "n": 5}, {"s": "b", "n": 9}, {"s": "c", "n": 2}]
        found, _ = run({"type": "funnel", "rows": rows, "category": "s", "value": "n", "sort": "none"})
        self.assertEqual([k for k, _v in found.model["stages"]], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()

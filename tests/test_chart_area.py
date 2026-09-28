"""Area charts (canvas v2 phase 3, 2.4): filled bands, stacks aligned on every x, 100 % stacks and their last shares."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series


class Area(unittest.TestCase):
    def test_a_stack_is_aligned_and_its_last_shares_are_read_back(self):
        found, drawing = run(example("area"))
        self.assertTrue(all(s["stack"] == "total" and s["areaStyle"] for s in series(found.option)))
        xs = [s["x"] for s in found.model["series"]]
        self.assertEqual(xs[0], xs[1], "stacked series share their x values")
        self.assertIn("at 2026-W32: Search 65% · Social 35%", found.gist)
        self.assertEqual(kinds(drawing)["path"], 4, "a band and a line per series")
        self.assertEqual(found.frame.axes["y"]["ticks"][0]["v"], 0)

    def test_a_missing_point_counts_zero_in_a_stack(self):
        rows = [{"d": "2026-01", "k": "a", "v": 2}, {"d": "2026-02", "k": "a", "v": 3}, {"d": "2026-02", "k": "b", "v": 4}]
        found, _ = run({"type": "area", "rows": rows, "x": "d", "y": "v", "color": "k", "stack": "percent"})
        b = next(s for s in found.model["series"] if s["name"] == "b")
        self.assertEqual(b["y"], [0, 4])
        self.assertEqual(found.option["yAxis"]["max"], 1)
        self.assertEqual(found.vegalite["encoding"]["y"]["stack"], "normalize")

    def test_a_nominal_x_is_refused(self):
        # QA phase34 M1: an area over a value-ranked nominal axis reported a false trend.
        from herdr_team.errors import HerdrTeamError

        rows = [{"channel": c, "visits": v} for c, v in (("Search", 5200), ("Ads", 120), ("Social", 900))]
        with self.assertRaises(HerdrTeamError) as caught:
            run({"type": "area", "rows": rows, "x": "channel", "y": "visits"})
        self.assertIn('"channel" is nominal', caught.exception.message)
        self.assertIn("use a bar", caught.exception.message)


if __name__ == "__main__":
    unittest.main()

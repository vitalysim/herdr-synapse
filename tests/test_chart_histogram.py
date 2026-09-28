"""Histograms (canvas v2 phase 3, 2.4): bins, stacked groups, the moments in the gist."""
from __future__ import annotations

import unittest

from chart_cases import example, kinds, run, series


class Histogram(unittest.TestCase):
    def test_the_example(self):
        found, drawing = run(example("histogram"))
        self.assertEqual(found.gist[1], "n 16 · mean 140.44 ms · median 132 ms · sd 31.1 ms · skewed right")
        self.assertTrue(found.gist[2].startswith("range 110 ms…240 ms · mode 120 ms–140 ms (8)"))
        self.assertEqual(sum(sum(c) for c in found.model["counts"]), 16)
        self.assertEqual(series(found.option, "bar")[0]["stack"], "bins")
        self.assertEqual(kinds(drawing)["rect"], sum(1 for c in found.model["counts"][0] if c))

    def test_bins_and_groups(self):
        rows = [{"v": i % 50, "g": "abcdefgh"[i % 8]} for i in range(400)]
        found, _ = run({"type": "histogram", "rows": rows, "x": "v", "color": "g", "bins": 10})
        self.assertEqual(len(found.model["edges"]) - 1, 10)
        self.assertEqual(len(found.model["groups"]), 6, "6 groups at most, the rest Other")
        self.assertEqual(found.model["groups"][-1], "Other")
        self.assertEqual(found.vegalite["encoding"]["x"]["bin"], {"binned": True})
        self.assertNotIn("asked", found.gist[0])

    def test_a_bin_count_moved_to_a_round_step_says_so(self):
        # QA phase34 L4: bins: 10 over 53…433 gives 8 bins of 50; the gist names both counts.
        rows = [{"ms": v} for v in (53, 90, 120, 150, 180, 260, 300, 433)]
        found, _ = run({"type": "histogram", "rows": rows, "x": "ms", "bins": 10})
        self.assertEqual(found.gist[0].split(" · ")[0], "x=ms in 8 bins of 50 (10 asked; edges on a round step)")


if __name__ == "__main__":
    unittest.main()

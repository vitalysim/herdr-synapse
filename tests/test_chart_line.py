"""Line charts (canvas v2 phase 3, 2.4): time, number and category axes, LTTB thinning, annotations, the gist."""
from __future__ import annotations

import math
import unittest

from chart_cases import example, kinds, run, series, texts


class Line(unittest.TestCase):
    def test_the_worked_example_reads_back_as_its_gist(self):
        found, drawing = run(example("line"))
        self.assertEqual(found.gist[1], "p95 412 → 236 ms (−43%) · max 2026-09-01 412 ms · min 2026-09-06 236 ms · "
                                        "largest step −147 ms at 2026-09-04 (cache on)")
        self.assertEqual(found.model["xkind"], "time")
        self.assertEqual(found.option["xAxis"]["type"], "value")
        self.assertIn("customValues", found.option["xAxis"]["axisLabel"], "calendar ticks travel as values")
        mark = series(found.option)[0]["markLine"]
        self.assertEqual(mark["data"][0]["xAxis"], 20700, "the annotation sits on its day number")
        self.assertEqual(kinds(drawing)["path"], 1)
        self.assertIn("cache on", texts(drawing))
        self.assertIn("2026-09-04", texts(drawing))
        self.assertEqual(found.vegalite["mark"]["type"], "line")

    def test_long_series_are_thinned_by_lttb_and_the_gist_reads_every_point(self):
        rows = [{"t": i, "v": math.sin(i / 50.0) * 100 + (500 if i == 2500 else 0)} for i in range(5000)]
        spec = {"type": "line", "x": "t", "y": "v"}
        from herdr_team.canvas_charts import _data
        from herdr_team import canvas_charts as CC

        normal, _ = CC.normalize_spec(dict(spec, rows=[{}]))
        table = _data.from_json(rows, "big", "sha")
        found = CC.compile(normal, table)
        self.assertLessEqual(len(found.model["series"][0]["x"]), 900)
        self.assertEqual(len(found.doc["datasets"][0]["source"]), 2000)
        self.assertIn("drawn from 900 of 5000 points (LTTB)", found.gist)
        self.assertIn("max 2,500 473.76", found.gist[1], "the spike is exact in the gist")

    def test_series_and_crossings(self):
        rows = [{"m": "2026-0{}".format(i), "team": team, "v": v} for i, (a, b) in enumerate(((1, 5), (4, 3), (6, 2)), 1)
                for team, v in (("a", a), ("b", b))]
        found, _ = run({"type": "line", "rows": rows, "x": "m", "y": "v", "color": "team", "smooth": True, "points": True})
        self.assertEqual(found.gist[-1], "a and b cross 1 time")
        self.assertTrue(all(s["smooth"] and s["showSymbol"] for s in series(found.option)))

    def test_an_ordinal_axis_keeps_its_order(self):
        rows = [{"stage": s, "n": n} for s, n in (("plan", 3), ("build", 8), ("ship", 5))]
        found, _ = run({"type": "line", "rows": rows, "x": "stage", "y": "n", "types": {"stage": "ordinal"}})
        self.assertEqual(found.model["cats"], ["plan", "build", "ship"])
        self.assertEqual(found.option["xAxis"]["type"], "category")

    def test_a_nominal_x_is_refused_with_the_ordinal_repair(self):
        # QA phase34 M1: a nominal axis is ranked by value, so a line over it always fell and the gist stated a false trend.
        from herdr_team.errors import HerdrTeamError

        rows = [{"weekday": d, "requests": n} for d, n in (("Mon", 900), ("Tue", 12500), ("Wed", 7000), ("Thu", 5030))]
        with self.assertRaises(HerdrTeamError) as caught:
            run({"type": "line", "rows": rows, "x": "weekday", "y": "requests"})
        self.assertEqual(caught.exception.details["field"], "x")
        self.assertIn('x needs a temporal or quantitative or ordinal field; "weekday" is nominal', caught.exception.message)
        self.assertIn('types: {"weekday": "ordinal"}', caught.exception.message)
        found, _ = run({"type": "line", "rows": rows, "x": "weekday", "y": "requests", "types": {"weekday": "ordinal"}})
        self.assertEqual(found.model["cats"], ["Mon", "Tue", "Wed", "Thu"], "declared ordinal keeps data order")
        self.assertIn("900 → 5,030", found.gist[1])


if __name__ == "__main__":
    unittest.main()

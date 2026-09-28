"""Fit to box with readable axes (canvas v2 phase 3, 2.5): the ladder over label lengths and category counts, the legend,
``min_box``, ``legible``, and a resize that re-frames with no I/O."""
from __future__ import annotations

import time
import unittest

from herdr_team import canvas_charts as CC
from herdr_team.canvas_charts import _data
from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts import _scale as S


def bar(labels, box, **extra):
    rows = [{"c": label, "v": 10 + i} for i, label in enumerate(labels)]
    spec, _ = CC.normalize_spec(dict({"type": "bar", "rows": rows, "x": "c", "y": "v", "sort": "none"}, **extra))
    return CC.compile(spec, _data.inline(rows), box)


class AxisTitle(unittest.TestCase):
    """QA phase34 M5, L1 and L2: the y title (measure and unit) is in both pictures, at one place, clear of the ticks."""

    def test_the_page_option_names_the_y_axis_where_the_drawing_puts_it(self):
        from chart_cases import example, run, texts

        found, drawing = run(example("line"))
        y = found.option["yAxis"]
        self.assertEqual(y["name"], "p95 (ms)")
        self.assertEqual((y["nameLocation"], y["nameGap"], y["nameMoveOverlap"]), ("end", FR.NAME_GAP, False))
        self.assertEqual(y["nameTextStyle"]["align"], "left")
        self.assertIn("p95 (ms)", texts(drawing))
        px, py, _pw, _ph = found.frame.plot
        title = next(p for p in drawing if p.get("k") == "text" and p["lines"][0]["t"] == "p95 (ms)")
        self.assertAlmostEqual(title["x"], px, places=2, msg="the drawing starts the title at the axis line, as ECharts does")
        self.assertAlmostEqual(title["box"][1] + title["box"][3], py - FR.NAME_GAP, places=2)
        # Clear of every tick label (they sit left of the axis line).
        ticks = [p for p in drawing if p.get("k") == "text" and p is not title and p["box"][0] + p["box"][2] <= px]
        self.assertTrue(ticks)
        for tick in ticks:
            self.assertLessEqual(tick["box"][0] + tick["box"][2], title["box"][0], tick["lines"][0]["t"])

    def test_a_horizontal_chart_names_its_left_axis_from_the_top(self):
        from chart_cases import example, run

        found, _ = run(dict(example("box"), horizontal=True))
        self.assertEqual(found.option["yAxis"]["type"], "category")
        self.assertEqual(found.option["yAxis"]["nameLocation"], "start", "an inverse axis starts at the top")
        self.assertNotIn("name", found.option["xAxis"])

    def test_a_unit_is_never_doubled(self):
        from herdr_team.canvas_charts import _format as F

        self.assertEqual(F.with_unit("ms", "ms"), "ms")
        self.assertEqual(F.with_unit("latency (ms)", "ms"), "latency (ms)")
        self.assertEqual(F.with_unit("latency_ms", "ms"), "latency_ms")
        self.assertEqual(F.with_unit("p95", "ms"), "p95 (ms)")
        self.assertEqual(F.with_unit("revenue", "$"), "revenue")
        self.assertEqual(F.with_unit("items", "s"), "items (s)", "a word that merely ends in the unit's letter keeps it")
        rows = [{"os": o, "ms": v} for o, v in (("a", 10), ("a", 12), ("b", 20), ("b", 22))]
        spec, _ = CC.normalize_spec({"type": "box", "rows": rows, "x": "os", "y": "ms", "units": {"y": "ms"}})
        found = CC.compile(spec, _data.inline(rows), (560, 340))
        self.assertEqual(found.frame.axes["y"]["title"], "ms")


class Ladder(unittest.TestCase):
    def test_short_labels_stay_horizontal(self):
        frame = bar(["a", "b", "c", "d"], (500, 300)).frame
        self.assertEqual((frame.axes["x"]["rotate"], frame.axes["x"]["interval"], frame.notes), (0, 0, ()))

    def test_the_ladder_thins_then_rotates_then_truncates(self):
        steps = []
        for count, width in ((20, 500), (30, 400), (40, 300), (60, 250)):
            labels = ["category number {}".format(i) for i in range(count)]
            frame = bar(labels, (width, 400)).frame
            axis = frame.axes["x"]
            steps.append((axis["rotate"], axis["interval"], axis["width"] is not None))
            if axis["rotate"]:
                self.assertTrue(any("rotated" in n for n in frame.notes), frame.notes)
        self.assertTrue(any(rotate for rotate, _i, _t in steps))
        wide = bar(["x" * 60 + str(i) for i in range(50)], (300, 240)).frame
        self.assertIsNotNone(wide.axes["x"]["width"], "the last rung truncates")
        self.assertTrue(any("truncated" in n for n in wide.notes))

    def test_every_kth_label_is_tried_before_rotating(self):
        frame = bar(["Jan {}".format(i) for i in range(20)], (560, 300)).frame
        axis = frame.axes["x"]
        self.assertEqual(axis["rotate"], 0)
        self.assertGreater(axis["interval"], 0)
        self.assertIn("every", " ".join(frame.notes))

    def test_labels_never_touch_on_the_chosen_rung(self):
        for count in (5, 12, 30, 59):
            frame = bar(["label {}".format(i) for i in range(count)], (520, 320)).frame
            axis = frame.axes["x"]
            if axis["rotate"]:
                continue
            step = frame.plot[2] / count
            shown = [lab for lab in axis["labels"] if lab["v"] % (axis["interval"] + 1) == 0]
            for a, b in zip(shown, shown[1:]):
                self.assertLessEqual((a["w"] + b["w"]) / 2 + FR.GAP, (b["v"] - a["v"]) * step + 1e-6)

    def test_horizontal_bars_put_categories_on_the_side(self):
        frame = bar(["a rather long category name {}".format(i) for i in range(8)], (500, 300), horizontal=True).frame
        self.assertEqual(frame.axes["x"]["pos"], "left")
        self.assertEqual(frame.axes["y"]["pos"], "bottom")
        self.assertLessEqual(frame.plot[0], 500 * 0.35 + FR.TICK + FR.GAP + 3)

    def test_value_axis_ticks_are_nice_and_include_zero_for_bars(self):
        frame = bar(["a", "b"], (500, 300)).frame
        ticks = [t["v"] for t in frame.axes["y"]["ticks"]]
        self.assertEqual(ticks[0], 0)
        self.assertEqual(S.nice_ticks(0, 97, 5)[:3], (0.0, 100.0, 20.0))


class Legend(unittest.TestCase):
    def rows(self, n):
        return [{"m": "2026-0{}".format(1 + i % 3), "s": "series {}".format(j), "v": i + j} for i in range(3) for j in range(n)]

    def test_right_with_few_items_and_room_else_bottom_and_none_for_one(self):
        for n, width, pos in ((3, 600, "right"), (8, 600, "bottom"), (3, 400, "bottom")):
            rows = self.rows(n)
            spec, _ = CC.normalize_spec({"type": "line", "rows": rows, "x": "m", "y": "v", "color": "s"})
            self.assertEqual(CC.compile(spec, _data.inline(rows), (width, 320)).frame.legend["pos"], pos, (n, width))
        rows = self.rows(1)
        spec, _ = CC.normalize_spec({"type": "line", "rows": rows, "x": "m", "y": "v", "color": "s"})
        self.assertIsNone(CC.compile(spec, _data.inline(rows), (600, 320)).frame.legend)
        spec, _ = CC.normalize_spec({"type": "line", "rows": self.rows(3), "x": "m", "y": "v", "color": "s", "legend": "none"})
        self.assertIsNone(CC.compile(spec, _data.inline(self.rows(3)), (600, 320)).frame.legend)


class Legibility(unittest.TestCase):
    def test_a_tiny_box_is_not_legible_and_min_box_is(self):
        labels = ["c{}".format(i) for i in range(50)]
        found = bar(labels, (160, 100))
        self.assertFalse(found.frame.legible)
        chart = CC.get("bar")
        w, h = chart.min_box(found.spec, found.model)
        again = bar(labels, (w, h))
        self.assertTrue(again.frame.legible, again.frame.notes)

    def test_a_resize_reframes_from_the_model_with_no_io(self):
        labels = ["region {}".format(i) for i in range(30)]
        found = bar(labels, (600, 360))
        part = CC.DataPart(spec=found.spec, model=found.model, doc={}, stats=found.stats, vegalite={}, warnings=[], source=found.source, key="")
        started = time.perf_counter()
        frame, option, gist = CC.view(found.spec, part, (300, 360))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.05)
        self.assertNotEqual(frame.to_json(), found.frame.to_json())
        self.assertEqual(option["grid"]["width"], round(frame.plot[2], 2), "the option takes the frame verbatim")
        self.assertEqual(option["xAxis"]["axisLabel"]["rotate"], frame.axes["x"]["rotate"])
        self.assertEqual(option["yAxis"]["interval"], frame.axes["y"]["step"])

    def test_frames_round_trip_through_json(self):
        frame = bar(["a", "b", "c"], (500, 300)).frame
        again = FR.Frame.from_json(frame.to_json())
        self.assertEqual(again.to_json(), frame.to_json())


if __name__ == "__main__":
    unittest.main()

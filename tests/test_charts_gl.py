"""The 3D chart types (canvas v2 phase 4, 3.11 and 8.2): ``bar3d``, ``scatter3d`` and ``surface``, drawn by echarts-gl.

Each is one module of ``canvas_charts`` (``gl: true``, frame ``gl``) and is
held to the chart contract here: the example compiles; the model, option and
drawing are deterministic (shuffled rows too) and within their sizes; the
option carries ``grid3D`` at the iso angles, its ``$doc`` refs resolve in the
doc and its paints are tokens of both palettes; the Python drawing (the iso
projection of the plot box) is legible, inside its slot and valid; the gist
reads the extremes, the ranges or the gradient; and a fuzz of random tables
only ever raises a refusal.
"""
from __future__ import annotations

import json
import math
import random
import re
import unittest

from support import PLUGIN_ROOT

from herdr_team import canvas_charts as CC
from herdr_team import canvas_display as D
from herdr_team.canvas_charts import _data
from herdr_team.errors import HerdrTeamError

GL = ("bar3d", "scatter3d", "surface")
SLOT = (40.0, 60.0, 536.0, 340.0)
TOKEN = re.compile(r"^(chart|tone|base|mat)\.[a-z0-9_.]+$")
CHARTS_JSON = PLUGIN_ROOT / "web" / "dist" / "charts.json"


def compiled(op, shuffle=None):
    spec, _warnings = CC.normalize_spec(op)
    rows = list(op["rows"])
    if shuffle is not None:
        random.Random(shuffle).shuffle(rows)
    return CC.compile(spec, _data.inline(rows), (SLOT[2], SLOT[3]))


def draw(result):
    chart = CC.get(result.spec["type"])
    return chart.draw(result.spec, result.model, result.frame, SLOT)


def walk(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, "{}.{}".format(path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, "{}[{}]".format(path, index))
    else:
        yield path, value


def palettes():
    return D.display_list({"elements": [], "version": 1, "team": "t"}, stills=set())["palettes"]


LOAD = [{"hour": h, "day": d, "requests": (h * 37 + i * 11) % 300 + 5} for h in range(8, 20) for i, d in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri"))]
POINTS = [{"lat": 10 + (i * 7) % 50, "load": ((i * 13) % 100) / 100.0, "err": (i * 3) % 11 - 2, "zone": ("eu", "us", "ap")[i % 3], "n": i % 9}
          for i in range(120)]
GRID = [{"lr": x / 10.0, "batch": y, "loss": round(math.sin(x / 5.0) * math.cos(y / 10.0) + x / 40.0, 4)} for x in range(0, 20) for y in range(0, 60, 4)]
OPS = {"bar3d": {"type": "bar3d", "rows": LOAD, "x": "hour", "y": "day", "z": "requests"},
       "scatter3d": {"type": "scatter3d", "rows": POINTS, "x": "lat", "y": "load", "z": "err", "color": "zone", "size": "n"},
       "surface": {"type": "surface", "rows": GRID, "x": "lr", "y": "batch", "z": "loss"}}


class Registered(unittest.TestCase):
    def test_the_three_types_are_gl_modules(self):
        self.assertEqual([name for name in CC.names(gl=True)], list(GL))
        for name in GL:
            chart = CC.get(name)
            self.assertTrue(chart.gl)
            self.assertEqual(chart.frame_kind, "gl")
            self.assertIn("Grid3DComponent", chart.echarts)
            self.assertTrue(chart.doc_line and chart.caps)
        self.assertEqual(CC.echarts_modules(gl=True), ["Bar3DChart", "Grid3DComponent", "VisualMapComponent", "Scatter3DChart", "SurfaceChart"])

    def test_the_examples_compile(self):
        for name in GL:
            with self.subTest(chart=name):
                op = json.loads(CC.get(name).example)
                self.assertEqual((op["op"], op["type"]), ("chart", name))
                result = compiled(op)
                self.assertTrue(result.gist)

    def test_the_page_bundles_their_modules(self):
        if not CHARTS_JSON.is_file():
            self.skipTest("web/dist/charts.json is written by the first npm run build of phases 3 and 4 (interface I-12)")
        bundled = json.loads(CHARTS_JSON.read_text(encoding="utf-8"))
        for name in GL:
            for module in CC.get(name).echarts:
                self.assertIn(module, bundled.get("gl") or [], "{} needs {}".format(name, module))


class Contract(unittest.TestCase):
    def test_deterministic_even_with_shuffled_rows(self):
        for name in GL:
            with self.subTest(chart=name):
                first = compiled(OPS[name])
                for seed in (1, 2):
                    again = compiled(OPS[name], shuffle=seed)
                    self.assertEqual(again.model, first.model)
                    self.assertEqual(again.option, first.option)
                    self.assertEqual(again.gist, first.gist)
                    self.assertEqual(draw(again), draw(first))

    def test_sizes(self):
        for name in GL:
            with self.subTest(chart=name):
                result = compiled(OPS[name])
                self.assertLessEqual(len(json.dumps(result.model)), 16 * 1024)
                self.assertLessEqual(len(json.dumps(result.option)), 16 * 1024)
                self.assertLessEqual(len(json.dumps(result.doc)), 2 * 1024 * 1024)

    def test_the_option(self):
        known = palettes()
        for name in GL:
            with self.subTest(chart=name):
                result = compiled(OPS[name])
                option = result.option
                self.assertEqual(option["grid3D"]["viewControl"]["projection"], "orthographic")
                self.assertEqual((option["grid3D"]["viewControl"]["alpha"], option["grid3D"]["viewControl"]["beta"]), (35.264, 45.0),
                                 "the iso camera")
                self.assertEqual(option["tooltip"]["renderMode"], "richText")
                self.assertFalse(option["animation"])
                self.assertEqual(option["aria"]["label"]["description"], " ".join(result.gist))
                for key in ("xAxis3D", "yAxis3D", "zAxis3D"):
                    self.assertIn(key, option)
                self.assertEqual(option["series"][0]["type"], {"bar3d": "bar3D", "scatter3d": "scatter3D", "surface": "surface"}[name])
                for path, value in walk(option):
                    if isinstance(value, str) and TOKEN.match(value):
                        for theme in ("light", "dark"):
                            self.assertIn(value, known[theme], "{} {}".format(path, value))
                refs = [value for path, value in walk(option) if path.endswith(".$doc")]
                self.assertTrue(refs)
                for ref in refs:
                    self.assertIn(ref, result.doc["refs"], ref)
                self.assertNotIn("function", json.dumps(option))

    def test_the_drawing(self):
        known = palettes()
        for name in GL:
            with self.subTest(chart=name):
                prims = draw(compiled(OPS[name]))
                self.assertGreaterEqual(len(prims), 20)
                self.assertLessEqual(len(prims), 1500)
                problems = []
                for index, prim in enumerate(prims):
                    D._check_prim(prim, "{}[{}]".format(name, index), known, problems)
                self.assertEqual(problems, [])
                x0, y0, w, h = SLOT
                for prim in prims:
                    points = prim.get("points") or ([[prim["x"], prim["y"]], [prim["x"] + prim["w"], prim["y"] + prim["h"]]] if "w" in prim else [])
                    for x, y in points:
                        self.assertTrue(x0 - 1 <= x <= x0 + w + 1 and y0 - 1 <= y <= y0 + h + 1, "{}: ({}, {}) outside the slot".format(name, x, y))
                    if prim["k"] == "text":
                        bx, by, bw, bh = prim["box"]
                        self.assertTrue(x0 - 1 <= bx and bx + bw <= x0 + w + 1 and y0 - 1 <= by and by + bh <= y0 + h + 1,
                                        "{}: label {} outside the slot".format(name, prim["lines"][0]["t"]))
                texts = [p for p in prims if p["k"] == "text"]
                self.assertGreaterEqual(len(texts), 5, "axis labels and titles")

    def test_labels_do_not_overlap(self):
        for name in GL:
            with self.subTest(chart=name):
                boxes = [p["box"] for p in draw(compiled(OPS[name])) if p["k"] == "text"]
                for i, a in enumerate(boxes):
                    for b in boxes[i + 1:]:
                        self.assertFalse(a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3], (a, b))

    def test_bar3d_page_shows_the_labels_the_drawing_shows(self):
        # QA phase34 low: echarts-gl drew every hour and weekday, so crowded labels met at the floor's near corner.
        result = compiled(OPS["bar3d"])
        shown = {p["lines"][0]["t"] for p in draw(result) if p["k"] == "text"}
        for key, names in (("xAxis3D", result.model["xs"]), ("yAxis3D", result.model["ys"])):
            every = result.option[key]["axisLabel"]["interval"] + 1
            self.assertGreater(every, 1, key)
            page = [str(n) for i, n in enumerate(names) if i % every == 0]
            self.assertTrue(set(page[1:]) <= shown, (key, page, shown))
            self.assertFalse({str(n) for i, n in enumerate(names) if i % every} & shown, key)


class Gists(unittest.TestCase):
    def test_bar3d(self):
        result = compiled(OPS["bar3d"])
        self.assertEqual(result.gist[0], "x=hour (12) · y=day (5) · z=requests · 60 bars · inline rows 60 rows")
        self.assertEqual(result.gist[1], "max 15 × Fri 304 · min 16 × Tue 8")
        self.assertEqual(result.gist[2], "highest: hour 15 (mean 282) · day Mon (mean 154.5)")
        repeated = compiled(dict(OPS["bar3d"], rows=LOAD + LOAD))
        self.assertIn("z=sum(requests)", repeated.gist[0], "repeated rows are summed")
        many = [{"a": "a{}".format(i), "b": "b{}".format(j), "v": i * j} for i in range(45) for j in range(3)]
        capped = compiled({"type": "bar3d", "rows": many, "x": "a", "y": "b", "z": "v"})
        self.assertEqual(len(capped.model["xs"]), 40)
        self.assertIn("capped: kept the 40 largest of 45 a values", capped.gist)
        self.assertEqual(capped.warnings[0]["code"], "chart_capped")

    def test_bar3d_keeps_a_calendar_depth_axis_in_calendar_order(self):
        # QA phase34 L7: the depth axis over weekdays was ranked by total (Mon, Fri, Thu, Wed, Tue).
        import random

        shuffled = list(LOAD)
        random.Random(7).shuffle(shuffled)
        for rows in (LOAD, shuffled):
            result = compiled(dict(OPS["bar3d"], rows=rows))
            self.assertEqual(result.model["ys"], ["Mon", "Tue", "Wed", "Thu", "Fri"])
            self.assertEqual(result.option["yAxis3D"]["data"], ["Mon", "Tue", "Wed", "Thu", "Fri"])
        months = [{"m": m, "k": k, "v": v} for v, m in enumerate(("Mar", "january", "Feb")) for k in ("a", "b")]
        self.assertEqual(compiled({"type": "bar3d", "rows": months, "x": "k", "y": "m", "z": "v"}).model["ys"], ["january", "Feb", "Mar"])
        other = [{"k": k, "t": t, "v": v} for v, t in enumerate(("Mon", "Tue", "later")) for k in ("a", "b")]
        self.assertEqual(compiled({"type": "bar3d", "rows": other, "x": "k", "y": "t", "z": "v"}).model["ys"], ["later", "Tue", "Mon"],
                         "not all calendar names: by total, as before")
        hours = compiled(dict(OPS["bar3d"], x="day", y="hour"))
        self.assertEqual(hours.model["xs"][0], "Mon", "the width axis still goes by total (a bar's sort)")

    def test_scatter3d(self):
        result = compiled(OPS["scatter3d"])
        self.assertEqual(result.gist[0], "x=lat · y=load · z=err · 120 points · color=zone (3) · size=n · inline rows 120 rows")
        self.assertEqual(result.gist[1], "lat 10…59 · load 0…0.99 · err −2…8")
        self.assertTrue(result.gist[2].startswith("r: lat~load "), result.gist[2])
        self.assertEqual(result.gist[3], "groups: ap 40 · eu 40 · us 40")
        self.assertEqual(sorted(result.doc["refs"]), ["g0", "g1", "g2"])
        # QA phase34 M6: each zone keeps its legend colour; the size map no longer takes the theme's ramp.
        maps = result.option["visualMap"]
        self.assertEqual([m["seriesIndex"] for m in maps], [0, 1, 2])
        self.assertEqual([m["inRange"] for m in maps], [{"symbolSize": [4, 12], "color": [c, c]} for c in result.option["color"][:3]])
        self.assertEqual(result.option["legend"]["data"], ["ap", "eu", "us"])

    def test_surface(self):
        result = compiled(OPS["surface"])
        self.assertEqual(result.gist[0], "x=lr (20) · y=batch (15) · z=loss · 300 grid points · inline rows 300 rows")
        self.assertTrue(result.gist[1].startswith("peak 1.") and " · trough −" in result.gist[1], result.gist[1])
        self.assertEqual(result.gist[2], "gradient: loss rises as lr grows (0.511) and falls as batch grows (−0.282)")
        flat = compiled({"type": "surface", "rows": [{"x": x, "y": y, "z": 1} for x in range(3) for y in range(3)], "x": "x", "y": "y", "z": "z"})
        self.assertEqual(flat.gist[-1], "flat: z is 1 everywhere")
        bowl = compiled({"type": "surface", "rows": [{"x": x, "y": y, "z": (x - 2) ** 2 + (y - 2) ** 2} for x in range(5) for y in range(5)],
                         "x": "x", "y": "y", "z": "z"})
        self.assertEqual(bowl.gist[-1], "gradient: no overall slope (peaks and dips)")

    def test_surface_refusals(self):
        loose = [{"x": i, "y": i * 2, "z": i} for i in range(10)]
        with self.assertRaises(HerdrTeamError) as caught:
            compiled({"type": "surface", "rows": loose, "x": "x", "y": "y", "z": "z"})
        self.assertIn("use scatter3d for loose points", str(caught.exception))
        wide = [{"x": i, "y": j, "z": i + j} for i in range(101) for j in range(2)]
        with self.assertRaises(HerdrTeamError) as caught:
            compiled({"type": "surface", "rows": wide, "x": "x", "y": "y", "z": "z"})
        self.assertIn("at most 100 × 100", str(caught.exception))

    def test_a_wrong_channel_type_is_refused_with_the_repair(self):
        with self.assertRaises(HerdrTeamError) as caught:
            compiled(dict(OPS["scatter3d"], x="zone"))
        self.assertIn("x needs a quantitative field", str(caught.exception))
        with self.assertRaises(HerdrTeamError) as caught:
            compiled({"type": "bar3d", "rows": LOAD, "x": "hour", "y": "day"})
        self.assertIn("bar3d needs z", str(caught.exception))


class Fuzz(unittest.TestCase):
    def test_random_tables_only_ever_refuse(self):
        rng = random.Random(20260928)
        words = ["a", "b", "c", "", None, "12", "x y", "2026-01", "NaN", "-3", "1e3"]
        for trial in range(200):
            name = GL[trial % 3]
            columns = ["p", "q", "r", "s"]
            rows = []
            for _ in range(rng.randint(1, 30)):
                row = {}
                for column in columns:
                    kind = rng.random()
                    row[column] = rng.choice(words) if kind < 0.3 else (rng.randint(-5, 5) if kind < 0.7 else rng.uniform(-100, 100))
                rows.append(row)
            op = {"type": name, "rows": rows, "x": "p", "y": "q", "z": "r"}
            if name == "scatter3d" and rng.random() < 0.5:
                op["color"] = "s"
            try:
                result = compiled(op)
            except HerdrTeamError:
                continue
            draw(result)
            json.dumps(result.option)


if __name__ == "__main__":
    unittest.main()

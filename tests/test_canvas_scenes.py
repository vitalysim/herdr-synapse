"""The golden canvas scenes (``tests/fixtures/canvas_scenes``) and the pure parts of ``tools/canvas_qa.py``.

The scenes are the QA gate's input: each must stay a valid batch that applies with nothing refused,
whatever the engine does with sizes and placement. The tool's pixel measurements need the real resvg
and fonts, so they run from ``python3 tools/canvas_qa.py``; here only its decoding, geometry, colour
and gate logic are checked, without resvg.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from support import PLUGIN_ROOT, TempState, whiteboard_on

from herdr_team import canvas as C
from herdr_team import store, workdir

SCENES = PLUGIN_ROOT / "tests" / "fixtures" / "canvas_scenes"
WORKER = C.CanvasAuthor("alpha-worker", "member", "cli", True, agent="claude")


def load_tool():
    spec = importlib.util.spec_from_file_location("canvas_qa", PLUGIN_ROOT / "tools" / "canvas_qa.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


QA = load_tool()


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def png_bytes(width, height, rows, filters):
    """An RGBA PNG whose scanlines use the given filter types (the encoder side of each filter)."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b""
    prev = bytes(width * 4)
    for row, kind in zip(rows, filters):
        out = bytearray()
        for i, value in enumerate(row):
            a = row[i - 4] if i >= 4 else 0
            b = prev[i]
            c = prev[i - 4] if i >= 4 else 0
            pred = {0: 0, 1: a, 2: b, 3: (a + b) >> 1, 4: paeth(a, b, c)}[kind]
            out.append((value - pred) & 0xFF)
        raw += bytes((kind,)) + bytes(out)
        prev = row
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class GoldenScenes(unittest.TestCase):
    def test_there_are_at_least_eight_and_each_names_itself(self):
        files = sorted(SCENES.glob("*.json"))
        self.assertGreaterEqual(len(files), 8)
        for path in files:
            scene = QA.load_scene_file(path)
            self.assertEqual(scene["name"], path.stem, path.name)
            self.assertTrue(scene["about"], path.name)

    def test_every_scene_applies_with_nothing_refused(self):
        for path in sorted(SCENES.glob("*.json")):
            with self.subTest(scene=path.stem), TempState() as ts:
                whiteboard_on(ts.session, ts.team)
                scene = QA.load_scene_file(path)
                if scene["artifacts"]:
                    # A scene that reads data files (charts, 3D models) gets its folder as the team's artifacts/.
                    project = ts.tmp / "project"
                    project.mkdir(exist_ok=True)
                    doc = store.read_json(ts.team.team_json)
                    doc.setdefault("config", {})["project_dir"] = os.fspath(project)
                    store.write_json(ts.team.team_json, doc)
                    art = workdir.paths_for(os.fspath(project), ts.team.name)["artifacts"]
                    for folder in scene["artifacts"]:
                        shutil.copytree(os.fspath(QA.ARTIFACTS_DIR / folder), os.fspath(art), dirs_exist_ok=True)
                for batch in scene["batches"]:
                    result = C.apply_ops(ts.layout, ts.team, batch, WORKER)
                    self.assertEqual(result["refused"], [], path.name)
                elements = {el["id"]: el for el in C.load_scene(ts.team)["elements"]}
                self.assertTrue(elements)
                for eid in scene["page_line_exempt"]:
                    self.assertTrue((elements.get(eid) or {}).get("text"), "{} exempts {}, which has no text".format(path.name, eid))


class ChartHookTexts(unittest.TestCase):
    """QA phase34 L9: the page-line gate compares a chart's hidden Python text with itself, so canvas_qa also compares
    the labels the page's ECharts picture drew with the ones the frame fixed."""

    FRAME = {"axes": {"x": {"kind": "category", "interval": 1, "labels": [{"v": 0, "t": "Jan"}, {"v": 1, "t": "Feb"}, {"v": 2, "t": "Mar"}],
                            "pos": "bottom"},
                      "y": {"kind": "value", "ticks": [{"v": 0, "t": "0"}, {"v": 50, "t": "50"}], "title": "p95 (ms)", "pos": "left"}},
             "legend": {"items": [{"name": "a very long series na…"}]}}

    def test_the_expected_texts_skip_thinned_labels(self):
        found = QA.chart_frame_texts([{"id": "E-1", "engine": "echarts", "chart_frame": self.FRAME},
                                      {"id": "E-2", "engine": "vega-lite", "chart_frame": self.FRAME}])
        self.assertEqual(found, {"E-1": ["Jan", "Mar", "0", "50", "p95 (ms)", "a very long series na…"]})

    def test_a_missing_axis_title_on_the_page_fails_the_hooks(self):
        want = QA.chart_frame_texts([{"id": "E-1", "engine": "echarts", "chart_frame": self.FRAME}])
        drawn = ["Jan", "Mar", "0", "50", "a very long series name"]
        hooks = {"light": {"charts": [{"id": "E-1", "rendered": True, "labelOverlaps": 0, "texts": drawn}], "scene3d": None}}
        found = QA.hook_findings(hooks, want)
        self.assertEqual(found["chart_texts_compared"], 1)
        self.assertEqual(len(found["problems"]), 1)
        self.assertIn("'p95 (ms)'", found["problems"][0])
        hooks["light"]["charts"][0]["texts"] = drawn + ["p95 (ms)"]
        self.assertEqual(QA.hook_findings(hooks, want)["problems"], [], "a truncated legend name matches by its stem")


class ToolPieces(unittest.TestCase):
    def test_png_decoding_undoes_every_filter(self):
        rows = [b"".join(bytes((x * 40 % 256, y * 60 % 256, (x + y) * 20 % 256, 255 if x % 2 else 128)) for x in range(5)) for y in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.png"
            path.write_bytes(png_bytes(5, 5, rows, [0, 1, 2, 3, 4]))
            width, height, decoded = QA.read_png(path)
        self.assertEqual((width, height), (5, 5))
        self.assertEqual(decoded, rows)

    def test_flat_rows_take_the_fast_path_and_stay_right(self):
        white = bytes((255, 255, 255, 255)) * 6
        blank = bytes(24)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flat.png"
            path.write_bytes(png_bytes(6, 4, [white, white, blank, blank], [4, 4, 2, 1]))
            _w, _h, decoded = QA.read_png(path)
        self.assertEqual(decoded, [white, white, blank, blank])

    def test_contrast_and_outlines(self):
        self.assertAlmostEqual(QA.contrast((0, 0, 0), (255, 255, 255)), 21.0, places=2)
        self.assertAlmostEqual(QA.contrast((25, 113, 194), (178, 242, 187)), 3.9, places=1)  # author blue on a green fill
        diamond = {"type": "diamond", "x": 0, "y": 0, "w": 100, "h": 50}
        self.assertTrue(QA._inside(diamond, 50, 25, 0))
        self.assertFalse(QA._inside(diamond, 5, 5, 0), "a diamond's corner box is outside it")
        ellipse = {"type": "ellipse", "x": 0, "y": 0, "w": 100, "h": 50}
        self.assertFalse(QA._inside(ellipse, 2, 2, 0))
        self.assertTrue(QA._inside({"type": "box", "x": 0, "y": 0, "w": 10, "h": 10}, 11, 11, 2))

    def test_stripping_keeps_only_the_text_or_everything_but_it(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg"><rect width="5" height="5"/><g clip-path="url(#c)">'
               '<text fill="#112233"><tspan>one</tspan><tspan>two</tspan></text></g></svg>')
        only, clipped = QA._strip(svg, True)
        self.assertNotIn("rect", only)
        self.assertTrue(clipped, "text under a clip path is reported")
        self.assertEqual(QA._lines(only), ["one", "two"])
        self.assertEqual(QA._text_fills(only), ["#112233"])
        without, _ = QA._strip(svg, False)
        self.assertNotIn("text", without)
        self.assertIn("rect", without)

    def test_line_comparison_and_the_gate(self):
        compared = QA.compare_lines({"E-1": ["a b", "c"], "E-2": ["x"], "E-3": ["y"]}, {"E-1": ["a b  ", "c"], "E-2": ["x", ""]})
        self.assertEqual((compared["compared"], [m["id"] for m in compared["mismatches"]], compared["missing_on_page"]), (2, ["E-2"], ["E-3"]))
        # Blank lines, indentation and runs of spaces count (QA F-8); the server's emoji rule does not (F-17).
        strict = QA.compare_lines({"E-4": ["a b"], "E-5": ["    step"], "E-6": ["one", "two"], "E-7": ["Flags DE ok"]},
                                  {"E-4": ["a  b"], "E-5": ["step"], "E-6": ["one", "", "two"], "E-7": ["Flags \U0001F1E9\U0001F1EA ok"]})
        self.assertEqual([m["id"] for m in strict["mismatches"]], ["E-4", "E-5", "E-6"])
        excused = QA.compare_lines({"E-2": ["x"]}, {"E-2": ["y"]}, {"E-2": "right to left"})
        self.assertEqual((excused["mismatches"], excused["exempt"][0]["reason"]), ([], "right to left"))
        clean = {"refused": [], "check": {"arrow_through": 2}, "pixel": {"light": {"overflow": [], "collisions": [], "low_contrast": [],
                                                                                "clipped": [], "arrow_label_collisions": [{}]}}}
        self.assertTrue(QA.gate(clean, strict=False)["ok"], "routing findings are tracked, not gated, in Phase 0")
        self.assertFalse(QA.gate(clean, strict=True)["ok"])
        self.assertFalse(QA.gate(dict(clean, check={"label_overflow": 1}), strict=False)["ok"])
        self.assertFalse(QA.gate(dict(clean, page_lines={"compared": 1, "mismatches": [{}]}), strict=False)["ok"])
        for key in ("tofu", "cut_off"):
            with self.subTest(render=key):
                failed = QA.gate(dict(clean, renders={"light": {key: ["x"]}}), strict=False)
                self.assertEqual(failed["failed"], ["render {} 1 (light)".format(key)])
        self.assertFalse(QA.gate(dict(clean, page_overflow=[{"id": "E-1"}]), strict=False)["ok"])

    def test_resvg_warnings_name_the_glyphs_it_drew_as_boxes(self):
        stderr = ("Warning (in usvg::text:183): Fallback from Inter to .DecoType Nastaleeq Urdu UI.\n"
                  "Warning (in usvg::text::layout:1384): No fonts with a \u0628/U+628 character were found.\n"
                  "Warning (in usvg::text::layout:1384): No fonts with a \u0644/U+644 character were found.\n"
                  "Warning (in usvg::text::layout:1384): No fonts with a \u0628/U+628 character were found.\n")
        self.assertEqual(QA.missing_glyphs(stderr), ["U+628", "U+644"])
        self.assertEqual(QA.missing_glyphs(""), [])

    def test_titles_and_pills_past_the_picture_are_cut_off(self):
        frame = {"id": "E-1", "type": "frame", "x": 0, "y": 0, "w": 400, "h": 300, "text": "A long frame title " * 4}
        arrow = {"id": "E-2", "type": "arrow", "x": 0, "y": 350, "w": 40, "h": 1, "points": [[0, 350], [40, 350]],
                 "text": "change data capture into the warehouse"}
        scene = {"elements": [frame, arrow]}
        self.assertEqual(QA.cut_off(scene, (-40, -40, 440, 400), 4.0), ["E-1", "E-2"], "zoomed out, the title stands above the frame")
        self.assertEqual(QA.cut_off(scene, QA.R.view_box(scene), 1.0), [])


if __name__ == "__main__":
    unittest.main()

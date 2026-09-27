"""Fixes from the 0.21 whiteboard live test (2026-09-27): text sizes the model, the renderer and
the page agree on; a warning for an element that crosses a frame's edge; labels resvg can draw;
resvg's font list without macOS's LastResort; live visuals fitted to their frame."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import PLUGIN_ROOT
from test_canvas import CanvasRig, fake_resvg

from herdr_team import canvas as C
from herdr_team import canvas_render as R
from herdr_team import whiteboard_server as W


def text_op(text, **extra):
    op = {"op": "shape", "kind": "text", "text": text, "intent": "t"}
    op.setdefault("at", [0, 0])
    op.update(extra)
    return op


def drawn_lines(el):
    """The lines the renderer draws for a text element."""
    return R._text_block(el["text"], el["x"], el["y"], el["w"], el["h"], el["style"], align="start").count("<tspan")


class TextSizes(CanvasRig):
    def test_a_text_without_a_width_is_as_wide_as_its_widest_line(self):
        text = self.el(self.ok(text_op("A cozy home"))["ids"][0])
        self.assertEqual((text["w"], text["h"], text["wrap"]), (121, 25, False))
        self.assertEqual(drawn_lines(text), 1)

    def test_a_set_width_wraps_the_text_and_its_height_follows(self):
        # The live test's label: 100 wide and 30 tall, drawn on two lines that ran below its frame.
        text = self.el(self.ok(text_op("A cozy home", at=[280, 380], w=100, h=30))["ids"][0])
        self.assertEqual((text["w"], text["h"], text["wrap"]), (100, 50, True))
        self.assertEqual(drawn_lines(text), 2)

    def test_a_five_letter_word_at_size_l_stays_on_one_line(self):
        text = self.el(self.ok(text_op("hello", size="l"))["ids"][0])
        self.assertEqual((text["w"], text["h"]), (77, 35))
        self.assertEqual(R.chars_per_line(77, 28), 5)
        self.assertEqual(drawn_lines(text), 1)

    def test_the_model_counts_the_lines_the_renderer_draws(self):
        for words, width in (("one two three four five six seven", 90), ("a verylongwordthatmustbreak here", 70), ("x\ny z", 30)):
            with self.subTest(words=words, width=width):
                w, h = C.text_size(words, 20, width)
                self.assertEqual(h, R.wrap_text(words, R.chars_per_line(width, 20)).__len__() * 25)
                el = {"text": words, "x": 0, "y": 0, "w": w, "h": h, "style": {"size": 20}}
                self.assertEqual(drawn_lines(el) * 25, h)

    def test_an_edit_keeps_a_set_width_and_regrows_an_auto_width(self):
        fixed = self.ok(text_op("A cozy home", w=100))["ids"][0]
        self.ok({"op": "edit", "id": fixed, "text": "A much longer label here", "intent": "t"})
        self.assertEqual((self.el(fixed)["w"], self.el(fixed)["h"]), (100, 100))
        auto = self.ok(text_op("Hi", at=[0, 400]))["ids"][0]
        self.ok({"op": "edit", "id": auto, "text": "Hello world", "intent": "t"})
        self.assertEqual((self.el(auto)["w"], self.el(auto)["h"], self.el(auto)["wrap"]), (121, 25, False))

    def test_a_new_size_measures_the_text_again(self):
        eid = self.ok(text_op("hello"))["ids"][0]
        self.assertEqual((self.el(eid)["w"], self.el(eid)["h"]), (55, 25))
        self.ok({"op": "restyle", "id": eid, "size": "l", "intent": "t"})
        self.assertEqual((self.el(eid)["w"], self.el(eid)["h"]), (77, 35))

    def test_resizing_a_text_sets_the_width_it_wraps_at(self):
        eid = self.ok(text_op("A cozy home"))["ids"][0]
        self.ok({"op": "move", "id": eid, "w": 60, "h": 999, "intent": "t"})
        self.assertEqual((self.el(eid)["w"], self.el(eid)["h"], self.el(eid)["wrap"]), (60, 75, True))
        refusal = self.refused({"op": "move", "id": eid, "h": 40, "intent": "t"})
        self.assertEqual(refusal["details"]["field"], "h")


class FrameEdge(CanvasRig):
    def setUp(self):
        super().setUp()
        self.frame = self.ok({"op": "frame", "title": "Home", "at": [100, 100], "w": 400, "h": 300, "intent": "t"})["ids"][0]

    def warnings(self, op):
        result = self.apply([op])
        self.assertEqual(result["refused"], [])
        return [w for w in result["warnings"] if w["code"] == "frame_edge"]

    def test_an_element_across_the_edge_is_named_with_the_edge(self):
        found = self.warnings(text_op("A cozy home", at=[280, 380]))
        self.assertEqual(len(found), 1)
        self.assertIn("sticks out of frame {} past its bottom edge".format(self.frame), found[0]["message"])
        self.assertEqual(found[0]["ids"][1], self.frame)

    def test_inside_outside_and_around_are_quiet(self):
        self.assertEqual(self.warnings(text_op("inside", at=[120, 150])), [])
        self.assertEqual(self.warnings(text_op("outside", at=[900, 900])), [])
        self.assertEqual(self.warnings({"op": "shape", "at": [50, 50], "w": 600, "h": 500, "intent": "t"}), [])
        self.assertEqual(self.warnings({"op": "arrow", "points": [[150, 150], [700, 150]], "intent": "t"}), [])

    def test_growing_a_text_past_the_edge_warns(self):
        eid = self.ok(text_op("short", at=[120, 360]))["ids"][0]
        result = self.apply([{"op": "edit", "id": eid, "text": "short\nand now\nthree lines", "intent": "t"}])
        self.assertEqual([w["code"] for w in result["warnings"]], ["frame_edge"])
        result = self.apply([{"op": "move", "id": eid, "to": [120, 150], "intent": "t"}])
        self.assertEqual(result["warnings"], [])


class Labels(unittest.TestCase):
    def test_emoji_modifiers_are_dropped_and_flags_become_letters(self):
        self.assertEqual(R.render_text("sun ☀️ ok \U0001F44D\U0001F3FD dev \U0001F469‍\U0001F4BB key #️⃣ \U0001F1EE\U0001F1F1"),
                         "sun ☀ ok \U0001F44D dev \U0001F469\U0001F4BB key # IL")
        self.assertEqual(R.render_text("plan → ship ✓ 中文"), "plan → ship ✓ 中文")

    def test_drawn_labels_are_escaped_and_made_drawable(self):
        self.assertEqual(R._label("a<b ☀️"), "a&lt;b ☀")
        block = R._text_block("door \U0001F6AA sun ☀️", 0, 0, 400, 25, {"size": 20}, align="start")
        self.assertIn("☀", block)
        self.assertNotIn("️", block)

    def test_an_agents_svg_keeps_its_text_as_written(self):
        clean = R.sanitize_svg('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>☀️</text></svg>')
        self.assertIn("☀️", clean)


class FontArgs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.system = self.root / "System"
        (self.system / "Supplemental").mkdir(parents=True)
        for name in ("Arial.ttf", "LastResort.otf", "notes.txt"):
            (self.system / name).write_bytes(b"")
        self.library = self.root / "Library"
        self.library.mkdir()
        (self.library / "Extra.otf").write_bytes(b"")

    def test_a_folder_with_last_resort_loads_its_other_fonts_one_by_one(self):
        args = R.font_args([str(self.library), str(self.system), str(self.root / "missing")])
        self.assertEqual(args, ["--skip-system-fonts", "--use-fonts-dir", str(self.library),
                                "--use-font-file", str(self.system / "Arial.ttf"),
                                "--use-fonts-dir", str(self.system / "Supplemental")])

    def test_without_last_resort_resvg_loads_the_system_fonts_itself(self):
        (self.system / "LastResort.otf").unlink()
        self.assertEqual(R.font_args([str(self.library), str(self.system)]), [])
        self.assertEqual(R.font_args(), [])  # tests pin FONT_DIRS to none

    def test_the_folders_are_fontdbs_on_macos(self):
        dirs = R.font_dirs({"HOME": "/home/someone"})
        self.assertEqual(dirs[:2], ["/Library/Fonts", "/System/Library/Fonts"])
        self.assertEqual(dirs[-2:], ["/Network/Library/Fonts", "/home/someone/Library/Fonts"])

    def test_resvg_gets_the_font_arguments(self):
        seen = []

        def run(argv, timeout):
            seen.append(list(argv))
            return fake_resvg(argv, timeout)

        with mock.patch.object(R, "FONT_DIRS", [str(self.system)]), mock.patch.object(R, "RUN", run), \
                mock.patch.object(R, "find_resvg", return_value="/fake/resvg"):
            out = R.render_png('<svg xmlns="http://www.w3.org/2000/svg"/>', self.root / "out.png", width_px=64)
        self.assertIsNotNone(out)
        self.assertEqual(seen[0][:4], ["/fake/resvg", "--skip-system-fonts", "--use-font-file", str(self.system / "Arial.ttf")])
        self.assertEqual(seen[0][-4:-2], ["-w", "64"])


class VizFit(unittest.TestCase):
    def test_the_frame_document_scales_a_marked_drawing_to_the_frame(self):
        doc = W.viz_document({"html": "<canvas width=300 height=150></canvas>", "libs": []}, 4321)
        self.assertIn("[data-synapse-fit]{display:block!important;width:100vw!important;height:100vh!important;object-fit:contain!important}", doc)

    def test_the_runtime_marks_a_lone_drawing_and_gives_the_frame_size(self):
        runtime = (PLUGIN_ROOT / "web" / "src" / "viz" / "synapse-viz.js").read_text(encoding="utf-8")
        for needle in ('setAttribute("data-synapse-fit", "")', 'setAttribute("viewBox"', "onResize: function", "get width()"):
            self.assertIn(needle, runtime)
        shipped = PLUGIN_ROOT / "web" / "dist" / "viz-lib" / "synapse-viz.js"
        self.assertEqual(shipped.read_bytes(), runtime.encode("utf-8"), "rebuild web/dist: the shipped runtime is stale")


if __name__ == "__main__":
    unittest.main()

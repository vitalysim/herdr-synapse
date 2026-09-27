"""Regression tests for the canvas v2 Phase 0 QA findings (``.local/qa/phase0/qa-report.md``), one class per finding.

The picture agents get (F-1, F-4, F-7, F-8, F-9, F-10), placement (F-2, F-3, F-13), graph layout
(F-6, F-12), long labels (F-11) and the page's text box (F-15). Tests that need the real resvg and
the system fonts skip without them; the rest are pure.
"""
from __future__ import annotations

import os
import tempfile
import unicodedata
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from support import PLUGIN_ROOT
from test_canvas import WORKER, CanvasRig
from test_canvas_render_fonts import decode_png

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_layout as L
from herdr_team import canvas_render as R
from herdr_team import canvas_text as X

SVG = "http://www.w3.org/2000/svg"
NASTALIQ = "/System/Library/Fonts/DecoTypeNastaleeqUrdu.ttc"


def texts(markup):
    """``(text, y)`` of every ``<text>`` in the markup, in order."""
    root = ET.fromstring('<svg xmlns="{}">{}</svg>'.format(SVG, markup))
    return [("".join(t.itertext()), float(t.get("y"))) for t in root.iter("{%s}text" % SVG)]


def svg_doc(body, family="Inter"):
    return ('<svg xmlns="{}" width="600" height="120" viewBox="0 0 600 120" font-family="{}, sans-serif">'
            '<rect width="600" height="120" fill="#ffffff"/>{}</svg>').format(SVG, family, body)


class F4F8LinesEachInTheirOwnText(unittest.TestCase):
    def test_every_line_is_its_own_text_at_its_own_baseline_with_spaces_kept(self):
        markup = R._lines_svg(["Next steps", "", "    1. indented"], 10, 0, 20, {}, "#000000", "start")
        self.assertIn('xml:space="preserve"', markup)
        drawn = texts(markup)
        self.assertEqual([t for t, _y in drawn], ["Next steps", "", "    1. indented"], "a blank line is kept, as is indentation")
        self.assertEqual([round(b - a, 6) for (_t, a), (_u, b) in zip(drawn, drawn[1:])], [X.line_height(20)] * 2)
        self.assertNotIn("<tspan", markup, "resvg reorders right-to-left runs across one <text>")


class F9StoredLinesThatBreakInsideAToken(CanvasRig):
    def test_lines_broken_at_a_slash_still_match_their_text(self):
        self.assertTrue(R._same_words(["https://example.com/", "docs/setup"], "https://example.com/docs/setup"))
        self.assertTrue(R._same_words(["one two", "three"], "one  two\nthree"))
        self.assertFalse(R._same_words(["one", "two"], "one three"))

    def test_the_picture_draws_the_fitted_lines_of_a_url(self):
        url = "https://example.com/segment0-with-a-long-name/segment1-with-a-long-name/segment2-with-a-long-name"
        eid = self.ok({"op": "shape", "kind": "note", "text": url, "at": [0, 0], "intent": "t"})["ids"][0]
        lines = self.el(eid)["fit"]["lines"]
        self.assertGreater(len(lines), 1)
        drawn = [t for t, _y in texts(R._label_svg(self.el(eid), None))]
        self.assertEqual(drawn, lines)


class F10EverythingDrawnIsInThePicture(CanvasRig):
    def test_a_frame_title_above_a_zoomed_out_frame_is_inside_the_view_box(self):
        self.ok({"op": "frame", "title": "Shop platform: production architecture", "at": [0, 0], "w": 4000, "h": 900, "intent": "t"})
        scene = self.scene()
        box = R.view_box(scene)
        frame = next(e for e in scene["elements"] if e["type"] == "frame")
        title = R.frame_title_box(frame, (box[2] - box[0]) / float(R.DEFAULT_MAX_PX))
        self.assertLess(title[1], frame["y"], "zoomed this far out the title stands above the frame")
        self.assertTrue(K._contains(box, title), (box, title))

    def test_a_label_pill_wider_than_its_arrow_is_inside_the_view_box(self):
        self.ok({"op": "arrow", "from": [0, 0], "to": [40, 0], "label": "change data capture into the warehouse", "intent": "t"})
        scene = self.scene()
        arrow = next(e for e in scene["elements"] if e["type"] == "arrow")
        (px, py, pw, ph), _size, _lines = R.arrow_label_pill(arrow)
        self.assertTrue(K._contains(R.view_box(scene), (px, py, px + pw, py + ph)))


class F1F7Fonts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.system = Path(tmp.name) / "System"
        self.system.mkdir()
        for name in ("Arial.ttf", "LastResort.otf", "DecoTypeNastaleeqUrdu.ttc", "GeezaPro.ttc"):
            (self.system / name).write_bytes(b"")

    def test_the_nastaliq_ui_face_is_never_a_fallback(self):
        args = R.font_args([str(self.system)])
        self.assertIn(str(self.system / "GeezaPro.ttc"), args)
        self.assertFalse([a for a in args if "Nastaleeq" in a or "LastResort" in a], args)

    def test_a_picture_the_bundled_fonts_cover_skips_the_system_fonts(self):
        latin = svg_doc('<g font-family="Inter"><text x="0" y="20">Checkout → payments ✓ “quoted” &amp; café</text></g>')
        with mock.patch.object(R, "BUNDLED_FONT_DIRS", None), mock.patch.object(R, "FONT_DIRS", [str(self.system)]):
            self.assertEqual(R.font_args(svg_text=latin), R.bundled_font_args() + ["--skip-system-fonts"])
            for other in (svg_doc('<text x="0" y="20">مرحبا</text>'), svg_doc('<text x="0" y="20">日本</text>'),
                          svg_doc('<text x="0" y="20">ok 😀</text>'), svg_doc('<text x="0" y="20" font-family="Arial">ok</text>'),
                          '<svg xmlns="{}"><text>no family anywhere</text></svg>'.format(SVG),
                          svg_doc('<text x="0" y="20" style="font: 12px serif">ok</text>')):
                with self.subTest(svg=other[-60:]):
                    self.assertIn(str(self.system / "Arial.ttf"), R.font_args(svg_text=other))

    def test_without_the_bundled_fonts_the_system_fonts_stay(self):
        with mock.patch.object(R, "FONT_DIRS", [str(self.system)]):
            self.assertIn(str(self.system / "Arial.ttf"), R.font_args(svg_text=svg_doc('<text x="0" y="20">plain</text>')))


@unittest.skipUnless(R.find_resvg(), "resvg is not installed")
class RealResvgPicture(unittest.TestCase):
    def render(self, svg):
        seen = []
        original = R.RUN

        def run(argv, timeout):
            code, err = original(argv, timeout)
            seen.append(err)
            return code, err

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(R, "RUN", run), \
                mock.patch.object(R, "BUNDLED_FONT_DIRS", None), mock.patch.object(R, "FONT_DIRS", None):
            out = R.render_png(svg, Path(tmp) / "out.png")
            self.assertIsNotNone(out, seen)
            _w, _h, rows = decode_png(Path(out).read_bytes())
        return rows, "\n".join(seen)

    def test_a_blank_line_keeps_its_height(self):
        rows, _err = self.render(svg_doc(R._lines_svg(["AAAA", "", "BBBB"], 20, 0, 20, {}, "#000000", "start")))
        inked = [y for y, row in enumerate(rows) if any(a > 128 and r + g + b < 300 for r, g, b, a in row)]
        gap = max(b - a for a, b in zip(inked, inked[1:]))
        self.assertGreater(gap, X.line_height(20), "the third line sits two line heights below the first")

    @unittest.skipUnless(os.path.exists(NASTALIQ), "macOS without its Nastaliq UI face")
    def test_arabic_and_japanese_after_inter_find_their_fonts(self):
        _rows, err = self.render(svg_doc(R._lines_svg(["تم تأكيد الطلب — 注文確認済み ✔"], 20, 20, 20, {}, "#000000", "start")))
        self.assertNotIn("No fonts with", err)
        self.assertNotIn("Nastaleeq", err)


class F3NestedFrames(CanvasRig):
    OPS = [
        {"op": "frame", "id": "a", "title": "Outer", "at": [0, 0], "w": 200, "h": 150, "intent": "t"},
        {"op": "frame", "id": "b", "title": "Middle", "inside": "a", "w": 160, "h": 110, "intent": "t"},
        {"op": "frame", "id": "c", "title": "Inner", "inside": "b", "w": 120, "h": 80, "intent": "t"},
        {"op": "shape", "kind": "box", "id": "x", "text": "A box inside the innermost frame whose label grows the whole stack",
         "inside": "c", "intent": "t"},
        {"op": "shape", "kind": "note", "id": "y", "text": "Another note in the inner frame that also grows a lot and a lot",
         "inside": "c", "intent": "t"},
        {"op": "shape", "kind": "box", "id": "u", "text": "sibling below outer", "below": "a", "gap": 10, "intent": "t"},
    ]

    def test_every_frame_up_the_chain_grows_and_children_do_not_overlap(self):
        result = self.apply(self.OPS)
        self.assertEqual(result["refused"], [])
        ids = dict(zip("abcxyu", [applied["ids"][0] for applied in result["applied"]]))
        box = {key: C.bounds(self.el(eid)) for key, eid in ids.items()}
        for child, parent in (("x", "c"), ("y", "c"), ("c", "b"), ("b", "a")):
            self.assertTrue(K._contains(box[parent], box[child]), (child, parent, box[child], box[parent]))
        self.assertFalse(K._intersects(box["x"], box["y"], K.TOUCH), "the second child is packed clear of the first")
        self.assertFalse(K._intersects(box["u"], box["a"], K.TOUCH), "a later sibling lands clear of the grown frame")
        self.assertEqual(C.check(self.layout, self.team, WORKER.name)["problems"], [])

    def test_check_reports_a_child_frame_sticking_out_of_its_frame(self):
        outer = {"id": "E-1", "type": "frame", "x": 0, "y": 0, "w": 200, "h": 150}
        inner = {"id": "E-2", "type": "frame", "x": 20, "y": 40, "w": 300, "h": 80, "frame": "E-1"}
        found = K.problems([outer, inner])
        edge = [p for p in found if p["code"] == "frame_edge"]
        self.assertEqual([p["ids"] for p in edge], [["E-2", "E-1"]])
        self.assertEqual(edge[0]["fix"]["w"], 340)


class F2GrownShapesMakeWay(CanvasRig):
    def house(self):
        ops = [
            {"op": "frame", "title": "Our cozy home on the hill", "at": [100, 100], "w": 560, "h": 420, "intent": "t"},
            {"op": "shape", "kind": "diamond", "text": "red tiled roof with a brick chimney", "at": [180, 150], "w": 200, "h": 80, "intent": "t"},
            {"op": "shape", "kind": "box", "at": [180, 240], "w": 200, "h": 140, "intent": "walls"},
            {"op": "shape", "kind": "ellipse", "text": "the warm afternoon sun", "at": [470, 140], "w": 60, "h": 60, "intent": "t"},
            {"op": "shape", "kind": "box", "text": "old oak tree planted by grandpa", "at": [440, 280], "w": 90, "h": 60, "intent": "t"},
        ]
        result = self.apply(ops)
        self.assertEqual(result["refused"], [])
        return [self.el(a["ids"][0]) for a in result["applied"]]

    def test_a_shape_that_grew_into_another_goes_to_the_nearest_free_spot_and_its_frame_grows(self):
        frame, roof, _walls, sun, tree = self.house()
        self.assertEqual(sun["y"], 140, "the sun stays in the sky, beside the roof, not below it")
        self.assertGreaterEqual(sun["x"], C.bounds(roof)[2] + K.GRID, "with room for the arrow between them")
        self.assertTrue(K._contains(C.bounds(frame), C.bounds(sun)), "its frame grew to hold it")
        self.assertFalse(K._intersects(C.bounds(sun), C.bounds(tree), K.TOUCH), "the next op's tree lands clear of it")

    def test_an_unlabelled_shape_goes_under_the_labels_it_covers(self):
        frame, roof, walls, _sun, _tree = self.house()
        self.assertTrue(K._intersects(C.bounds(roof), C.bounds(walls), K.TOUCH))
        self.assertLess(walls["z"], roof["z"], "the walls' fill no longer hides the roof's label")
        self.assertGreaterEqual(walls["z"], frame["z"], "never under its own frame")
        order = [e["id"] for e in self.scene()["elements"]]
        self.assertLess(order.index(walls["id"]), order.index(roof["id"]), "the page draws in the same order")

    def test_a_shape_drawn_inside_a_labelled_panel_stays_on_top(self):
        panel = self.ok({"op": "shape", "text": "Checkout", "at": [0, 0], "w": 400, "h": 300, "intent": "t"})["ids"][0]
        icon = self.ok({"op": "shape", "at": [40, 120], "w": 60, "h": 60, "intent": "t"})["ids"][0]
        self.assertGreater(self.el(icon)["z"], self.el(panel)["z"])


class F13FullFramesWiden(CanvasRig):
    def test_a_frame_full_of_stickies_grows_right_as_well_as_down(self):
        frame = self.ok({"op": "frame", "title": "Retro", "at": [0, 0], "w": 600, "h": 400, "intent": "t"})["ids"][0]
        for number in range(24):
            self.ok({"op": "shape", "kind": "note", "text": "Sticky number {}".format(number), "inside": frame, "intent": "t"})
        box = self.el(frame)
        self.assertGreater(box["w"], 600)
        self.assertLess(box["h"] / float(box["w"]), 2.0, "no longer a tall single column")
        self.assertEqual(C.check(self.layout, self.team, WORKER.name)["problems"], [])


class F6LongEdgesBendAroundNodes(CanvasRig):
    def test_the_layered_layout_keeps_a_slot_for_an_edge_across_layers(self):
        positions, bends = L.plan(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")])
        self.assertEqual(set(positions), {"a", "b", "c", "d"})
        self.assertEqual(list(bends), [3], "only the edge across layers bends")
        self.assertEqual(len(bends[3]), 2, "one point in each layer it crosses")
        self.assertEqual(L.layout(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")]), positions)

    def test_a_graph_edge_across_layers_no_longer_cuts_through_the_node_between(self):
        self.ok({"op": "graph", "title": "chain", "at": [0, 0], "intent": "t",
                 "nodes": [{"id": "a", "text": "Client"}, {"id": "b", "text": "API gateway with a long label"},
                           {"id": "c", "text": "Orders service"}],
                 "edges": [["a", "b"], ["b", "c"], ["a", "c"]]})
        arrows = [e for e in self.scene()["elements"] if e["type"] == "arrow"]
        self.assertTrue(any(len(a["points"]) > 2 for a in arrows), "the long edge bends")
        problems = C.check(self.layout, self.team, WORKER.name)["problems"]
        self.assertEqual([p for p in problems if p["code"] == "arrow_through"], [])


class F12GraphNotesAndDiamonds(CanvasRig):
    def test_notes_and_diamonds_keep_their_shape_minimum_and_notes_their_size(self):
        self.ok({"op": "graph", "title": "g", "at": [0, 0], "intent": "t",
                 "nodes": [{"id": "a", "text": "Start"}, {"id": "b", "text": "Approved?", "kind": "diamond"},
                           {"id": "n", "text": "Remember to ask the finance team about the budget first", "kind": "note"}],
                 "edges": [["a", "b"], ["b", "n"]]})
        by_type = {e["type"]: e for e in self.scene()["elements"] if e["type"] in ("note", "diamond")}
        self.assertGreaterEqual((by_type["note"]["w"], by_type["note"]["h"]), (180, 120))
        self.assertGreaterEqual(by_type["diamond"]["w"], 200)
        self.assertGreaterEqual(by_type["diamond"]["h"], 120)
        self.assertEqual(by_type["note"]["fit"]["size"], by_type["note"]["style"].get("size", 20), "a graph note is not shrunk")


class F11LongTokensAndParagraphs(unittest.TestCase):
    WORD = "Pneumonoultramicroscopicsilicovolcanoconiosis" * 3

    def fit(self, policy, text, minimum, font="normal", size=20, inset=None, pad=(16, 12)):
        request = X.FitRequest(text=text, font=font, size=size, min_w=minimum[0], min_h=minimum[1], max_w=320, pad_x=pad[0],
                               pad_y=pad[1], inset=inset, snap=20)
        result = X.fit(policy, request)
        self.assertTrue(X.fits(result), result)
        return result

    def test_a_word_wider_than_the_size_class_breaks_instead_of_widening_the_shape(self):
        box = self.fit("hug", self.WORD, (160, 80))
        self.assertLessEqual(box.w, X.word_max_width(20) + 2 * 16 + 20)
        self.assertGreater(len(box.lines), 1)
        code = self.fit("hug", self.WORD, (160, 80), font="code", size=36)
        self.assertLessEqual(code.w, X.word_max_width(36) + 2 * 16 + 20)
        from herdr_team.canvas_kinds.shape import ellipse_inset
        ellipse = self.fit("scale_shape", self.WORD, (160, 80), inset=ellipse_inset, pad=(8, 8))
        self.assertLess(ellipse.w, 1000, "was 2160 x 1080")

    def test_a_short_word_still_widens_its_box_rather_than_break(self):
        box = self.fit("hug", "Internationalization", (40, 40))
        self.assertEqual(box.lines, ("Internationalization",))

    def test_a_paragraph_is_a_block_not_a_tower(self):
        paragraph = " ".join(["agents jumps diagrams quick brown fox"] * 55)
        box = self.fit("hug", paragraph, (160, 80))
        self.assertLess(box.h, 1.5 * box.w, (box.w, box.h))
        self.assertLessEqual(box.w, X.PARAGRAPH_MAX_EMS * 20 + 2 * 16 + 20)
        short = self.fit("hug", "A label of a few words on two lines at most", (160, 80))
        self.assertLessEqual(short.w, 320, "short labels still wrap at the box's own width")


class F14ThaiSyllables(unittest.TestCase):
    def test_a_hard_break_never_splits_a_thai_vowel_from_its_syllable(self):
        thai = "ภาษาไทยไม่มีการเว้นวรรคระหว่างคำซึ่งทำให้การตัดบรรทัดยาก" * 2
        for width in range(40, 400, 7):
            for line in X.wrap(thai, width)[1:]:
                with self.subTest(width=width, line=line):
                    self.assertNotIn(line[0], "\u0e30\u0e32\u0e33\u0e45")
                    self.assertNotEqual(unicodedata.category(line[0]), "Mn")
            for line in X.wrap(thai, width)[:-1]:
                self.assertNotIn(line[-1], "\u0e40\u0e41\u0e42\u0e43\u0e44")


class F15PageGlyphOverhang(unittest.TestCase):
    def test_server_lines_get_room_for_a_glyph_past_its_advance(self):
        adapter = (PLUGIN_ROOT / "web" / "src" / "canvas" / "adapter.js").read_text(encoding="utf-8")
        self.assertIn("export const GLYPH_OVERHANG = 2;", adapter)
        self.assertIn("advanceWidth(text)", adapter, "fitting checks ignore the overhang, so no container grows for it")
        built = "".join(path.read_text(encoding="utf-8", errors="replace") for path in (PLUGIN_ROOT / "web" / "dist" / "assets").glob("index-*.js"))
        self.assertIn("overhang", built, "rebuild web/dist")


if __name__ == "__main__":
    unittest.main()

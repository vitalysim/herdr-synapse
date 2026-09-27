"""The agent's picture draws with the bundled fonts (canvas v2 phase 0): resvg gets the font folders and the
families, labels are the lines the fit stored, in Inter at the weight Python measured, and (when resvg is
installed) the ink is as wide as ``canvas_text`` says, so the PNG and the model agree."""
from __future__ import annotations

import os
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest import mock

from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_render as R
from herdr_team import canvas_text as X
from herdr_team import canvas_theme as T

SVG = "http://www.w3.org/2000/svg"


def decode_png(data: bytes):
    """``(width, height, rows of (r, g, b, a))`` for an 8-bit RGB or RGBA PNG (what resvg writes)."""
    pos, idat, width, height, kind = 8, b"", 0, 0, 6
    while pos < len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, _depth, kind = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    channels = 4 if kind == 6 else 3
    raw = zlib.decompress(idat)
    stride = width * channels
    rows, previous, at = [], bytearray(stride), 0
    for _ in range(height):
        method, line = raw[at], bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up, corner = previous[i], previous[i - channels] if i >= channels else 0
            if method == 1:
                line[i] = (line[i] + left) & 0xFF
            elif method == 2:
                line[i] = (line[i] + up) & 0xFF
            elif method == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif method == 4:
                p = left + up - corner
                best = min((abs(p - left), 0, left), (abs(p - up), 1, up), (abs(p - corner), 2, corner))[2]
                line[i] = (line[i] + best) & 0xFF
        rows.append([tuple(line[i:i + channels]) + ((255,) if channels == 3 else ()) for i in range(0, stride, channels)])
        previous = line
    return width, height, rows


def ink_span(rows):
    """The leftmost and rightmost columns holding dark ink."""
    columns = [x for row in rows for x, (r, g, b, a) in enumerate(row) if a > 128 and r + g + b < 300]
    return min(columns), max(columns)


class FontArguments(unittest.TestCase):
    def test_resvg_gets_the_bundled_fonts_first_even_with_system_fonts_pinned(self):
        with mock.patch.object(R, "BUNDLED_FONT_DIRS", None):
            args = R.font_args()
        root = R.FONTS_ROOT
        self.assertEqual(args, ["--use-fonts-dir", str(root / "inter"), "--use-fonts-dir", str(root / "geist-mono"),
                                "--sans-serif-family", "Inter", "--monospace-family", "Geist Mono"])
        for name in ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf"):
            self.assertTrue((root / "inter" / name).is_file(), name)

    def test_a_missing_bundle_adds_nothing(self):
        with mock.patch.object(R, "BUNDLED_FONT_DIRS", ["/no/such/fonts"]):
            self.assertEqual(R.font_args(), [])


class LabelsInTheSvg(CanvasRig):
    def test_labels_are_the_fitted_lines_in_inter_at_the_measured_weight(self):
        eid = self.ok({"op": "shape", "text": "Internationalization and localization pipeline", "at": [0, 0], "intent": "t"})["ids"][0]
        code = self.ok({"op": "shape", "kind": "text", "text": "print(hello)", "font": "code", "at": [0, 300], "intent": "t"})["ids"][0]
        scene = self.scene()
        root = ET.fromstring(R.render_svg(scene, marks=False))
        groups = {"".join(g.itertext()): g for g in root.iter("{%s}g" % SVG) if g.get("font-family")}
        label = next(g for text, g in groups.items() if text.startswith("Internationalization"))
        self.assertEqual([t.text for t in label.iter("{%s}text" % SVG)], self.el(eid)["fit"]["lines"])
        self.assertEqual((label.get("font-family"), label.get("font-weight")), ("Inter", str(X.DEFAULT_WEIGHT)))
        self.assertEqual(label.get("fill"), self.el(eid)["style"]["text"])
        self.assertEqual(groups["print(hello)"].get("font-family"), "Geist Mono")
        backdrop = [r for r in root.iter("{%s}rect" % SVG)][0]
        self.assertEqual(backdrop.get("fill"), T.base()["canvas"], "the board's canvas colour, the page's too")
        self.assertTrue(code)

    def test_an_arrow_label_sits_in_a_measured_pill(self):
        a = self.ok({"op": "shape", "text": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "B", "at": [600, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "arrow", "from": a, "to": b, "label": "calls with retries", "intent": "t"})
        root = ET.fromstring(R.render_svg(self.scene(), marks=False))
        pill = next(r for r in root.iter("{%s}rect" % SVG) if r.get("rx") == "6")
        self.assertGreaterEqual(float(pill.get("width")), X.measure("calls with retries", size=16).width)

    def test_an_arrow_label_wraps_where_the_page_wraps_it(self):
        # Excalidraw 0.18: the wider of 0.7 x the arrow's width and 11 x the label's font size.
        self.assertEqual(R.arrow_label_width({"w": 100}, 16), 176)
        self.assertEqual(R.arrow_label_width({"w": 1000}, 16), 700)


@unittest.skipUnless(R.find_resvg(), "resvg is not installed")
class RealResvg(unittest.TestCase):
    """The picture agents see, drawn by the real resvg with the bundled fonts (no system fonts needed)."""

    def render(self, text: str, weight: int) -> int:
        size, x = 40.0, 20.0
        svg = ('<svg xmlns="{}" width="1200" height="80" viewBox="0 0 1200 80"><rect width="1200" height="80" fill="#ffffff"/>'
               '<text x="{}" y="55" font-size="{}" font-family="Inter" font-weight="{}" fill="#000000">{}</text></svg>').format(
            SVG, x, size, weight, text)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(R, "BUNDLED_FONT_DIRS", None):
            out = R.render_png(svg, Path(tmp) / "label.png")
            self.assertIsNotNone(out, "resvg rendered nothing")
            width, _height, rows = decode_png(Path(out).read_bytes())
        self.assertEqual(width, 1200)
        left, right = ink_span(rows)
        return right - left + 1

    def test_the_ink_is_as_wide_as_the_model_says(self):
        text = "Checkout service handles payments"
        for weight in (400, 500, 600, 700):
            with self.subTest(weight=weight):
                model = X.measure(text, "normal", 40, weight).width / X.SAFETY
                ink = self.render(text, weight)
                # Ink runs from the first glyph's left bearing to the last one's right edge: a few units short of the advances.
                self.assertLess(abs(ink - model) / model, 0.03, (weight, ink, model))

    def test_each_weight_resolves_to_its_own_file(self):
        self.assertGreater(self.render("Weight check WWW", 700), self.render("Weight check WWW", 400))


if __name__ == "__main__":
    unittest.main()

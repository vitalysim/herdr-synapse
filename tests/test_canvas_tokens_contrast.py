"""The canvas design tokens (``herdr_team/canvas_tokens.json``, design ``.local/prd/canvas-v2-design.md``).

Every text pair the renderers can produce meets WCAG 2.2 AA (4.5:1) and every
meaningful line or outline 3:1, in the light and the dark theme and, for Phase 0,
in the light colours seen through Excalidraw's own dark-mode filter. The file is
data only, so these checks are what keep a later colour edit honest.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

from herdr_team import canvas as C

TOKENS_PATH = Path(__file__).resolve().parent.parent / "herdr_team" / "canvas_tokens.json"
TEXT_ROLE_BACKGROUNDS = ("fill", "sticky", "zone")
BASE_BACKGROUNDS = ("canvas", "surface")


def _rgb(color: str) -> Tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """The WCAG 2.x contrast ratio of two ``#rrggbb`` colours."""
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def excalidraw_dark(color: str) -> str:
    """``color`` through Excalidraw 0.18's THEME_FILTER, ``invert(93%) hue-rotate(180deg)``, applied
    in sRGB the way Chrome's canvas applies it (checked against Chrome 153 to one unit per channel)."""
    inverted = [0.93 - 0.86 * (v / 255.0) for v in _rgb(color)]
    matrix = ((-0.574, 1.43, 0.144), (0.426, 0.43, 0.144), (0.426, 1.43, -0.856))
    out = [max(0.0, min(1.0, sum(m * v for m, v in zip(row, inverted)))) for row in matrix]
    return "#{:02x}{:02x}{:02x}".format(*(int(round(v * 255)) for v in out))


def load_tokens() -> Dict:
    return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))


def pairs(tokens: Dict) -> List[Tuple[str, str, str, float]]:
    """Every (name, foreground, background, minimum) pair the design promises."""
    text_min, line_min = float(tokens["contrast"]["text"]), float(tokens["contrast"]["non_text"])
    out = []
    for theme_name, theme in tokens["theme"].items():
        base = theme["base"]
        for bg in BASE_BACKGROUNDS:
            out.append(("{} ink on {}".format(theme_name, bg), base["ink"], base[bg], text_min))
            out.append(("{} ink_muted on {}".format(theme_name, bg), base["ink_muted"], base[bg], text_min))
            out.append(("{} line on {}".format(theme_name, bg), base["line"], base[bg], line_min))
        for tone_name, tone in theme["tone"].items():
            label = "{} {}".format(theme_name, tone_name)
            for bg in TEXT_ROLE_BACKGROUNDS:
                out.append(("{} text on {}".format(label, bg), tone["text"], tone[bg], text_min))
            for bg in BASE_BACKGROUNDS:
                out.append(("{} text on {}".format(label, bg), tone["text"], base[bg], text_min))
                out.append(("{} stroke on {}".format(label, bg), tone["stroke"], base[bg], line_min))
            out.append(("{} stroke on fill".format(label), tone["stroke"], tone["fill"], line_min))
            out.append(("{} on_solid on solid".format(label), tone["on_solid"], tone["solid"], text_min))
            out.append(("{} solid on canvas".format(label), tone["solid"], base["canvas"], line_min))
            if theme_name == "light":
                dark = {role: excalidraw_dark(value) for role, value in tone.items()}
                canvas = excalidraw_dark(base["canvas"])
                for bg in ("fill", "sticky"):
                    out.append(("phase0-dark {} text on {}".format(tone_name, bg), dark["text"], dark[bg], text_min))
                out.append(("phase0-dark {} text on canvas".format(tone_name), dark["text"], canvas, text_min))
                out.append(("phase0-dark {} stroke on canvas".format(tone_name), dark["stroke"], canvas, line_min))
        for chip in theme["author_chips"] + [theme["human_chip"]]:
            label = "{} chip {}".format(theme_name, chip["name"])
            out.append((label + " fg on bg", chip["fg"], chip["bg"], text_min))
            out.append((label + " bg on canvas", chip["bg"], base["canvas"], line_min))
    return out


class TokenContrastTests(unittest.TestCase):
    def test_every_promised_pair_meets_wcag_aa(self) -> None:
        tokens = load_tokens()
        found = pairs(tokens)
        self.assertGreater(len(found), 200)
        failing = ["{} {} on {} = {:.2f} < {}".format(name, fg, bg, contrast(fg, bg), need)
                   for name, fg, bg, need in found if contrast(fg, bg) < need]
        self.assertEqual(failing, [])

    def test_the_dark_filter_model_matches_chrome(self) -> None:
        # Pixels read back from a Chrome 153 canvas with ctx.filter = THEME_FILTER.
        measured = {"#ffffff": (17, 17, 17), "#000000": (237, 237, 237), "#1c2024": (206, 210, 213),
                    "#ff0000": (255, 143, 143), "#0d74ce": (75, 163, 241), "#ffe629": (75, 53, 0)}
        for color, want in measured.items():
            got = _rgb(excalidraw_dark(color))
            self.assertTrue(all(abs(g - w) <= 1 for g, w in zip(got, want)), (color, got, want))


class TokenShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = load_tokens()

    def test_both_themes_carry_every_tone_role_and_eight_chips(self) -> None:
        roles = set(self.tokens["roles"])
        for theme in self.tokens["theme"].values():
            self.assertEqual(list(theme["tone"]), self.tokens["tones"])
            for tone in theme["tone"].values():
                self.assertEqual(set(tone), roles)
            self.assertEqual(len(theme["author_chips"]), 8)
            self.assertEqual(set(theme["base"]), set(self.tokens["theme"]["light"]["base"]))

    def test_the_type_scale_keeps_the_size_names_the_ops_accept(self) -> None:
        scale = self.tokens["type"]["scale"]
        self.assertEqual({key: step["size"] for key, step in scale.items()},
                         {key: C.SIZES[key] for key in ("s", "m", "l", "xl")})
        for step in list(scale.values()) + list(self.tokens["type"]["roles"].values()):
            self.assertEqual(step["line"] % 4, 0, step)
            # Inter's ascender plus descender is 1.21 em: a tighter line would clip.
            self.assertGreaterEqual(step["line"], 1.21 * step["size"], step)

    def test_minimum_sizes_sit_on_the_grid_and_never_shrink_todays_defaults(self) -> None:
        grid = self.tokens["space"]["grid"]
        minimums = self.tokens["size_min"]
        for kind, (w, h) in C.SHAPE_SIZES.items():
            self.assertGreaterEqual(minimums[kind][0], w, kind)
            self.assertGreaterEqual(minimums[kind][1], h, kind)
        self.assertEqual(minimums["graph_node"], [C.NODE_W, C.NODE_H])
        for kind in ("box", "ellipse", "diamond", "note", "sticky", "card", "graph_node", "section", "kanban_column"):
            self.assertEqual([v % grid for v in minimums[kind]], [0, 0], kind)
        self.assertEqual(self.tokens["padding"]["section"]["x"], C.FRAME_PAD)
        self.assertEqual(self.tokens["padding"]["section"]["band"]["phase0"], C.FRAME_TOP)

    def test_every_legacy_colour_maps_to_a_tone(self) -> None:
        legacy, tones = self.tokens["legacy"], set(self.tokens["tones"])
        self.assertEqual(set(legacy["color"]), set(C.COLORS))
        self.assertTrue(set(C.COLORS.values()) | set(C.AUTHOR_PALETTE) <= set(legacy["stroke_hex"]))
        self.assertTrue(set(C.FILLS.values()) | set(C.AUTHOR_FILLS) <= set(legacy["fill_hex"]))
        for table in ("color", "stroke_hex", "fill_hex"):
            self.assertTrue(set(legacy[table].values()) <= tones, table)


if __name__ == "__main__":
    unittest.main()

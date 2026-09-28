"""Semantic zoom (canvas v2 phase 2, 6.1): which text draws at scales 1, 0.3 and 0.1, the skeletons, and the title rule.

The bands are tokens (``lod``); the agent's picture applies them at its own scale the way the page does."""
from __future__ import annotations

import json
import unittest

from herdr_team import canvas_display as D
from herdr_team import canvas_theme as T


def texts_at(doc, scale):
    """Every text line a renderer draws at ``scale`` (screen pixels per unit), by entry id."""
    out = {}

    def walk(entry_id, items):
        for prim in items or []:
            if not D.lod_visible(prim, scale):
                continue
            if prim.get("k") == "text":
                out.setdefault(entry_id, []).extend(line["t"] for line in prim["lines"])
            walk(entry_id, prim.get("items"))
    for entry in doc["entries"]:
        walk(entry["id"], entry["items"])
    return out


def skeletons_at(doc, scale):
    return sum(1 for entry in doc["entries"] for prim in entry["items"]
               if prim.get("k") == "rect" and prim.get("lod") == D.LOD_SKELETON and D.lod_visible(prim, scale))


class Bands(unittest.TestCase):
    def test_the_bands_are_tokens(self):
        lod = T.lod()
        self.assertEqual((lod["titles"], lod["overview"], lod["title_min_px"]), (0.35, 0.15, 12))
        self.assertEqual(D.LOD_BODY, [0.35, None])
        self.assertEqual(D.LOD_LABEL, [0.15, None])
        self.assertEqual(D.LOD_SKELETON, [0.15, 0.35])

    def test_skeleton_bars_follow_the_lines(self):
        bars = D.skeleton([0, 0, 200, 60], [180, 120, 90, 40], 20)
        self.assertEqual(len(bars), 3, "at most three")
        self.assertEqual([b["w"] for b in bars], [180, 120, 90])
        self.assertTrue(all(b["h"] == 8 and b["fill"] == "base.grid" for b in bars))


class Scenes(unittest.TestCase):
    """On the golden scenes that use blocks: full detail, then titles and skeletons, then silhouettes and top titles."""

    def load(self, name):
        path = D.GOLDENS_DIR / (name + ".json")
        if not path.is_file():
            self.skipTest("no golden for {}".format(name))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_composed_at_three_zooms(self):
        doc = self.load("composed")
        full, titles, overview = texts_at(doc, 1.0), texts_at(doc, 0.3), texts_at(doc, 0.1)
        flat = lambda found: [t for lines in found.values() for t in lines]  # noqa: E731
        self.assertIn("Terminates TLS and forwards the", " ".join(flat(full)) + " ")
        self.assertIn("API gateway", flat(titles), "titles stay")
        self.assertNotIn("Issues and rotates tokens", flat(titles), "bodies become skeletons")
        self.assertGreater(skeletons_at(doc, 0.3), 0)
        self.assertEqual(skeletons_at(doc, 1.0), 0)
        self.assertNotIn("API gateway", flat(overview), "below the overview band only top-level titles")
        self.assertIn("Auth redesign", flat(overview), "the top-level section's title, above it")

    def test_kanban_titles_and_counts(self):
        doc = self.load("kanban")
        titles = [t for lines in texts_at(doc, 0.3).values() for t in lines]
        self.assertIn("Todo", titles)
        self.assertIn("Rotate API keys", titles)
        self.assertNotIn("Waiting on the key rotation", titles)

    def test_the_agent_picture_uses_the_same_bands(self):
        from herdr_team import canvas_svg

        doc = self.load("composed")
        width = doc["bbox"][2] - doc["bbox"][0]
        close = canvas_svg.write(doc, theme="light", max_px=int(width))  # one pixel per unit
        far = canvas_svg.write(doc, theme="light", max_px=int(width * 0.1))  # a tenth
        self.assertIn("Issues and rotates tokens", close)
        self.assertNotIn("Issues and rotates tokens", far)
        self.assertIn("Auth redesign", far)

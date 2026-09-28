"""The display list (canvas v2 phase 1, ``docs/display-list.md``): goldens, the schema, determinism, colour, the fit
invariant, number format and the frame title's zoom rule (T-D1 to T-D9 of ``.local/prd/canvas-v2-phase1.md`` 5.1)."""
from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

import kind_conformance
from support import PLUGIN_ROOT

from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team import canvas_kinds as R
from herdr_team import canvas_svg as S
from herdr_team import canvas_text as X
from herdr_team import canvas_theme as T

GOLDENS = PLUGIN_ROOT / "tests" / "fixtures" / "display"
SCENES = sorted(p.stem for p in (PLUGIN_ROOT / "tests" / "fixtures" / "canvas_scenes").glob("*.json"))
RENDER_FIXTURES = PLUGIN_ROOT / "web" / "src" / "v2" / "render" / "__fixtures__"


def golden_scene(name):
    return json.loads((GOLDENS / "scenes" / (name + ".json")).read_text(encoding="utf-8"))


def walk(items):
    """Every primitive in a list of items, groups and slot fallbacks included."""
    for item in items or []:
        yield item
        yield from walk(item.get("items"))
        yield from walk(item.get("fallback"))


def refs(value):
    """Every token reference in a paint."""
    if isinstance(value, dict):
        yield from refs(value.get("hatch"))
    elif isinstance(value, str) and not value.startswith("#"):
        yield value


class Goldens(unittest.TestCase):
    """T-D1: each golden scene's display list and both themes' canonical SVG are exactly the goldens."""

    def test_every_golden_scene_has_its_goldens(self):
        # The 9 scenes of phases 0 and 1, and phase 2's (blocks, diagrams, routing): 19 in all (phase 2, 8.2).
        for name in ("arrow-labels", "font-sizes", "frame-children", "house", "i18n", "sticky-notes", "text-notes", "flowchart", "architecture",
                     "blocks", "composed", "kanban", "table", "timeline", "sketch"):
            self.assertIn(name, SCENES)
        for name in SCENES:
            for suffix in (".json", ".light.svg", ".dark.svg"):
                self.assertTrue((GOLDENS / (name + suffix)).is_file(), name + suffix)

    def test_the_display_list_and_both_pictures_are_the_goldens(self):
        for name in SCENES:
            with self.subTest(scene=name):
                outputs = D.golden_outputs(golden_scene(name))
                for key, suffix in (("dl", ".json"), ("light", ".light.svg"), ("dark", ".dark.svg")):
                    self.assertEqual(outputs[key], (GOLDENS / (name + suffix)).read_text(encoding="utf-8"),
                                     "{}{} is stale: python3 -m herdr_team.canvas_display --write-goldens".format(name, suffix))

    def test_the_goldens_are_current(self):
        self.assertEqual(D.main(["--check-goldens"]), 0)

    def test_the_python_writer_draws_the_renderers_own_fixtures(self):
        # One picture: the page's toSVGString wrote these; canvas_svg must write the same bytes (8.1 gate 6).
        found = sorted(RENDER_FIXTURES.glob("*.json")) if RENDER_FIXTURES.is_dir() else []
        if not found:
            self.skipTest("no renderer fixtures in web/src/v2/render/__fixtures__")
        for path in found:
            doc = json.loads(path.read_text(encoding="utf-8"))
            for theme in ("light", "dark"):
                svg = path.with_name("{}.{}.svg".format(path.stem, theme))
                if svg.is_file():
                    with self.subTest(fixture=path.stem, theme=theme):
                        self.assertEqual(S.write(doc, theme=theme), svg.read_text(encoding="utf-8").rstrip("\n"))


class Schema(unittest.TestCase):
    """T-D2: every golden validates, and an element no kind can draw is a placeholder, never an error."""

    def test_every_golden_validates(self):
        for name in SCENES:
            with self.subTest(scene=name):
                self.assertEqual(D.validate(json.loads((GOLDENS / (name + ".json")).read_text(encoding="utf-8"))), [])

    def test_malformed_elements_draw_and_validate(self):
        env = D.environment({})
        for kind in R.kinds() + [R.Kind(name="no-such-kind")]:
            for bad in kind_conformance.MALFORMED:
                with self.subTest(kind=kind.name, element=bad):
                    found = D.entry(dict(bad, type=kind.name), env)
                    doc = {"dl": 1, "palettes": {t: T.palette(t) for t in T.THEMES}, "entries": [found]}
                    self.assertEqual(D.validate(doc), [])
                    S.write(dict(doc, bbox=found["bbox"]))

    def test_an_unknown_kind_is_the_placeholder_card(self):
        found = D.entry({"id": "E-1", "type": "kanban", "x": 0, "y": 0, "w": 300, "h": 200, "text": "Sprint"}, D.environment({}))
        self.assertEqual((found["kind"], found["handles"]), ("kanban", "none"))
        self.assertEqual(found["items"][0]["dash"], [8, 6])
        self.assertIn("Sprint", [line["t"] for p in walk(found["items"]) if p["k"] == "text" for line in p["lines"]])

    def test_the_phase2_entry_fields_validate(self):
        doc = D.display_list({"elements": [{"id": "E-1", "type": "box", "x": 0, "y": 0, "w": 160, "h": 80, "text": "a"}]})
        entry = doc["entries"][0]
        good = dict(entry, pin="human", block="E-9", part="c2", tip="why", container={"layout": "row", "gap": 20, "order": ["E-2"]},
                    parts=[{"part": "r1.c1", "hit": {"shape": "rect", "box": [0, 0, 10, 10]}, "lod": [0.35, None],
                            "edit": dict(entry["edit"], part="r1.c1")}])
        self.assertEqual(D.validate(dict(doc, entries=[good])), [])
        for bad, needle in ((dict(good, pin="robot"), "pin"), (dict(good, part=None), "block and part"),
                            (dict(good, container={"layout": "spiral", "gap": 1, "order": []}), "container"), (dict(good, tip="x" * 501), "tip"),
                            (dict(good, parts=[{"part": "", "hit": {"shape": "rect", "box": [0, 0, 1, 1]}}]), "a part"),
                            (dict(good, parts=[{"part": "p", "hit": {"shape": "rect", "box": [0, 0, 1, 1]}, "edit": dict(entry["edit"])}]), "edit"),
                            (dict(good, parts=[{"part": "p", "hit": {"shape": "rect", "box": [0, 0, 1, 1]}, "lod": [1]}]), "lod")):
            with self.subTest(needle=needle):
                problems = D.validate(dict(doc, entries=[bad]))
                self.assertTrue(any(needle in p for p in problems), problems)

    def test_validate_names_what_is_wrong(self):
        doc = D.display_list({"elements": [{"id": "E-1", "type": "box", "x": 0, "y": 0, "w": 160, "h": 80, "text": "a"}]})
        self.assertEqual(D.validate(doc), [])
        broken = json.loads(json.dumps(doc))
        broken["entries"][0]["items"][0]["fill"] = "tone.nope.fill"
        broken["entries"][0]["layer"] = "sky"
        problems = D.validate(broken)
        self.assertTrue(any("tone.nope.fill" in p for p in problems), problems)
        self.assertTrue(any("layer" in p for p in problems), problems)
        self.assertEqual(D.validate({"dl": 2}), ["not a display list v1"])


class Determinism(unittest.TestCase):
    """T-D3: the same scene always gives the same bytes, whatever order its elements come in."""

    def test_two_runs_and_a_shuffle_give_the_same_list(self):
        for name in ("house", "flowchart", "i18n"):
            scene = golden_scene(name)
            first = D.dumps(D.display_list(scene, stills=set()))
            self.assertEqual(first, D.dumps(D.display_list(scene, stills=set())))
            shuffled = dict(scene, elements=list(scene["elements"]))
            random.Random(7).shuffle(shuffled["elements"])
            self.assertEqual(first, D.dumps(D.display_list(shuffled, stills=set())), name)

    def test_applying_the_scene_again_gives_the_stored_scene(self):
        # The goldens hold on every Python the suite runs on (3.9 and current): the op engine is deterministic.
        for name in ("house", "arrow-labels"):
            fresh = D.golden_scene(PLUGIN_ROOT / "tests" / "fixtures" / "canvas_scenes" / (name + ".json"))
            self.assertEqual(json.loads(json.dumps(fresh, sort_keys=True)), golden_scene(name), name)


class Colour(unittest.TestCase):
    """T-D4 and T-D7: every reference resolves in both palettes; ``paints`` is the one place hex becomes a reference."""

    def test_every_reference_in_every_golden_resolves_in_both_palettes(self):
        for name in SCENES:
            doc = json.loads((GOLDENS / (name + ".json")).read_text(encoding="utf-8"))
            for entry in doc["entries"]:
                for prim in walk(entry["items"]):
                    for key in ("fill", "stroke"):
                        for ref in refs(prim.get(key)):
                            for theme in T.THEMES:
                                self.assertIn(ref, doc["palettes"][theme], (name, entry["id"], ref))
                for ref in (entry["chip"]["bg"], entry["chip"]["fg"]):
                    self.assertIn(ref, doc["palettes"]["dark"])

    def test_references_resolve_to_what_resolve_gives(self):
        for theme in T.THEMES:
            palette = T.palette(theme)
            for tone in T.TONES:
                for variant in T.VARIANTS:
                    for kind in ("box", "note", "frame", "arrow", "text", "pen", "chart"):
                        want = T.resolve(tone, variant, kind, theme)
                        got = T.resolve_ref(tone, variant, kind)
                        for role in ("stroke", "fill", "text"):
                            with self.subTest(theme=theme, tone=tone, variant=variant, kind=kind, role=role):
                                self.assertEqual(palette[got[role]] if got[role] else None, want[role].lower() if want[role] else None)

    def test_a_toned_element_is_all_references(self):
        colours = T.resolve("info", "soft", "box")
        el = {"type": "box", "style": dict(colours, tone="info", variant="soft")}
        self.assertEqual(D.paints(el), {"stroke": "tone.info.stroke", "fill": "tone.info.fill", "text": "tone.info.text"})

    def test_an_explicit_hex_stays_literal_and_its_label_keeps_its_contrast(self):
        colours = T.resolve("info", "soft", "box")
        el = {"type": "box", "style": dict(colours, tone="info", variant="soft", stroke="#123456", fill="#a5d8ff")}
        found = D.paints(el)
        self.assertEqual((found["stroke"], found["fill"]), ("#123456", "#a5d8ff"))
        self.assertEqual(found["text"], colours["text"].lower(), "a label on a literal fill is literal too")

    def test_legacy_colours_go_through_the_legacy_tables(self):
        legacy = T.tokens()["legacy"]
        for hex_value, tone in legacy["stroke_hex"].items():
            with self.subTest(stroke=hex_value):
                self.assertEqual(D.paints({"type": "box", "style": {"stroke": hex_value}})["stroke"], T.resolve_ref(tone, "soft", "box")["stroke"])
        self.assertEqual(D.paints({"type": "box", "style": {"stroke": "#abcdef"}})["stroke"], "#abcdef", "an unknown hex stays literal")
        self.assertEqual(D.paints({"type": "note", "style": {}})["fill"], "tone.idea.sticky", "a note without paper is an idea sticky")
        note = D.entry({"id": "E-1", "type": "note", "x": 0, "y": 0, "w": 180, "h": 120, "text": "old", "style": {"fill": "#ffec99"}},
                       D.environment({}))
        self.assertEqual(next(p for p in note["items"] if p["k"] == "text")["fill"], "base.ink", "a pre-0.22 note's label is ink")


class TextFits(unittest.TestCase):
    """T-D5 and T-D8: every text fits its box, and every primitive lies inside its entry's bbox."""

    def test_every_line_fits_its_box(self):
        for name in SCENES:
            doc = json.loads((GOLDENS / (name + ".json")).read_text(encoding="utf-8"))
            for entry in doc["entries"]:
                for prim in walk(entry["items"]):
                    if prim["k"] != "text" or prim.get("zoom"):
                        continue
                    box = prim["box"]
                    with self.subTest(scene=name, id=entry["id"]):
                        self.assertLessEqual(max(line["w"] for line in prim["lines"]), box[2] + 0.5)
                        self.assertLessEqual(len(prim["lines"]) * prim["lh"], box[3] + 0.5)

    def test_every_primitive_lies_inside_its_entrys_bbox(self):
        for name in SCENES:
            doc = json.loads((GOLDENS / (name + ".json")).read_text(encoding="utf-8"))
            for entry in doc["entries"]:
                x0, y0, x1, y1 = entry["bbox"]
                for prim in entry["items"]:
                    found = D.prim_bounds(prim)
                    if found is None:
                        continue
                    with self.subTest(scene=name, id=entry["id"], k=prim["k"]):
                        # Both sides are rounded to 2 decimals on their own: 0.02 of slack.
                        self.assertTrue(found[0] >= x0 - 0.02 and found[1] >= y0 - 0.02 and found[2] <= x1 + 0.02 and found[3] <= y1 + 0.02,
                                        (found, entry["bbox"]))

    def test_a_right_to_left_line_says_so(self):
        found = D.text_prim(["אושר?", "ok"], 0, 0, 20, {}, "base.ink", "start", None)
        self.assertEqual([line.get("dir") for line in found["lines"]], ["rtl", None])
        svg = S.fragment([found])
        self.assertIn('unicode-bidi="plaintext"', svg)
        self.assertNotIn("direction=", svg, "direction would mirror text-anchor in browsers and not in resvg")
        self.assertNotIn(S.RTL_ISOLATE, svg, "the canonical form (the goldens, the page) carries the line as stored")
        # resvg ignores unicode-bidi: the agent's picture isolates the line so it reads in the page's order (R-8, QA 1 #9).
        agent = S.fragment([found], resvg_text=True)
        self.assertIn(">{}אושר?{}</text>".format(S.RTL_ISOLATE, S.POP_ISOLATE), agent)
        self.assertIn(">ok</text>", agent, "a left-to-right line is left alone")


class Numbers(unittest.TestCase):
    """T-D6: ``fmt`` against the vectors both writers are held to, and the document's own rounding."""

    def test_fmt_vectors(self):
        vectors = json.loads((GOLDENS / "fmt-vectors.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(vectors), 60)
        for value, want in vectors:
            self.assertEqual(S.fmt(value), want, value)

    def test_rounding_is_half_away_from_zero_with_no_negative_zero(self):
        self.assertEqual((D.r2(2.675), D.r2(-2.675), D.r2(-0.001), D.r2(3.0), D.r2(0.125)), (2.68, -2.68, 0, 3, 0.13))
        self.assertEqual((S.fmt(-0.0), S.fmt(0.004), S.fmt(-0.005), S.fmt(1.5), S.fmt(12.30)), ("0", "0", "-0.01", "1.5", "12.3"))

    def test_dumps_is_canonical(self):
        self.assertEqual(D.dumps({"b": 1, "a": [1.5, "é"]}), '{"a":[1.5,"é"],"b":1}')


class FrameTitles(unittest.TestCase):
    """T-D9: a frame's title is in its band close up and above it zoomed out, never under 12 pixels on screen."""

    FRAME = {"id": "E-1", "type": "frame", "x": 0, "y": 0, "w": 400, "h": 300, "text": "Checkout", "style": {"tone": "neutral"}, "z": 1}

    def titles(self, u):
        doc = D.display_list({"elements": [self.FRAME]})
        box = (-100.0, -200.0, -100.0 + 1024 * u, -200.0 + 800 * u)
        svg = S.write(doc, box=box)
        drawn = [line for prim in walk(doc["entries"][0]["items"]) if prim["k"] == "text" for line in prim["lines"]]
        return svg, drawn

    def test_both_alternatives_carry_their_lod(self):
        entry = D.entry(self.FRAME, D.environment({}))
        texts = [p for p in entry["items"] if p["k"] == "text"]
        self.assertEqual([t["lod"] for t in texts], [[G.FRAME_TITLE_LOD, None], [None, G.FRAME_TITLE_LOD]])
        self.assertEqual(texts[1]["zoom"], {"min_px": 12, "grow": "up", "bottom": 0})

    def test_the_title_moves_above_the_frame_and_grows_as_the_view_zooms_out(self):
        near, _ = self.titles(1.0)
        self.assertIn('font-size="16"', near)
        self.assertIn('y="23.82"', near, "close up: in the band")
        far, _ = self.titles(2.0)
        self.assertIn('font-size="24"', far, "12 screen pixels at 2 units per pixel")
        self.assertNotIn('y="23.82"', far)
        farther, _ = self.titles(4.0)
        self.assertIn('font-size="48"', farther)

    def test_the_document_bbox_holds_the_title_above_the_frame(self):
        doc = D.display_list({"elements": [dict(self.FRAME, w=8000, h=2000)]})
        self.assertLess(doc["bbox"][1], -40, "the settled box reaches above the frame for its zoomed-out title")


if __name__ == "__main__":
    unittest.main()

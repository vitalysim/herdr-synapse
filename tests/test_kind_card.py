"""The ``card`` kind (canvas v2 phase 2, 4.2): fields, fit up to its width class, clamps, parts, readback and checks."""
from __future__ import annotations

from test_canvas import OPERATOR, CanvasRig

from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_text as X
from herdr_team.canvas_kinds import card as K


def texts(entry):
    out = []
    for prim in entry["items"]:
        for item in [prim] + list(prim.get("items") or []):
            if item.get("k") == "text":
                out.extend(line["t"] for line in item["lines"])
    return out


class Card(CanvasRig):
    def test_a_card_hugs_up_to_its_width_class(self):
        short = self.el(self.ok({"op": "card", "title": "API", "at": [0, 0], "intent": "t"})["ids"][0])
        self.assertEqual((short["w"], short["h"]), (240, 80), "never below 240x80")
        long = self.el(self.ok({"op": "card", "title": "A title long enough to wrap onto a second line at the medium width",
                                "at": [0, 200], "intent": "t"})["ids"][0])
        self.assertEqual(long["w"], 320, "wraps at the m class (320)")
        self.assertGreaterEqual(len(long["fit"]["lines"]), 2)
        large = self.el(self.ok({"op": "card", "title": "A title long enough to wrap onto a second line at the medium width", "size": "l",
                                 "at": [0, 400], "intent": "t"})["ids"][0])
        self.assertGreater(large["w"], 320)
        self.assertLessEqual(large["w"], 440)

    def test_fields_and_refusals(self):
        self.assertEqual(self.refused({"op": "card", "at": [0, 0], "intent": "t"})["details"]["field"], "title")
        self.ok({"op": "card", "at": [0, 0], "intent": "a blank card from the page"}, author=OPERATOR)
        refused = self.refused({"op": "card", "title": "x", "icon": "databse", "at": [0, 0], "intent": "t"})
        self.assertEqual((refused["code"], refused["details"]["did_you_mean"][0]), ("icon_unknown", "database"))
        refused = self.refused({"op": "card", "title": "x", "badges": ["a"] * 9, "at": [0, 0], "intent": "t"})
        self.assertEqual(refused["code"], "canvas_limit")
        self.assertEqual(self.refused({"op": "card", "title": "x", "status": "x" * 30, "at": [0, 0], "intent": "t"})["details"]["field"], "status")

    def test_emit_parts_and_the_fit_invariant(self):
        eid = self.ok({"op": "card", "title": "API gateway", "icon": "network", "tone": "info", "at": [0, 0], "intent": "t",
                       "body": "Terminates TLS\n- forwards the JWT\n- rate limits", "badges": ["P0", {"text": "risk", "tone": "danger"}],
                       "owner": "alpha-worker", "status": "doing", "detail": "Envoy 1.31"})["ids"][0]
        el = self.el(eid)
        self.assertTrue(el["owner_known"])
        entry = D.entry(el, D.environment(self.scene()))
        found = texts(entry)
        for word in ("API gateway", "Terminates TLS", "forwards the JWT", "P0", "risk", "alpha-worker", "doing"):
            self.assertIn(word, found)
        self.assertIn("•", found, "bullets hang")
        self.assertEqual([p["part"] for p in entry["parts"]], ["title", "body"])
        self.assertEqual(entry["parts"][1]["edit"]["field"], "body")
        self.assertEqual(entry["tip"], "Envoy 1.31")
        kinds = [p["k"] for p in entry["items"]]
        self.assertEqual(kinds[:2], ["rect", "rect"], "the surface, then the tone bar")
        self.assertIn("group", kinds, "the icon's paths")
        body = [p for p in entry["items"] if p.get("k") == "text" and p.get("lod") == D.LOD_BODY]
        skeleton = [p for p in entry["items"] if p.get("k") == "rect" and p.get("lod") == D.LOD_SKELETON]
        self.assertTrue(body and skeleton, "a body draws at the full band and as bars below it")
        for prim in entry["items"]:
            if prim.get("k") == "text":
                for line in prim["lines"]:
                    self.assertLessEqual(line["w"], prim["box"][2] + 0.5, (line, prim["box"]))

    def test_edit_a_part_refits_the_card(self):
        eid = self.ok({"op": "card", "title": "Short", "at": [0, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "edit", "id": eid, "part": "body", "text": "- one\n- two\n- three\n- four", "intent": "t"})
        el = self.el(eid)
        self.assertEqual(el["body"], "- one\n- two\n- three\n- four")
        self.assertGreater(el["h"], 80)
        self.assertEqual(self.refused({"op": "edit", "id": eid, "part": "nope", "text": "x", "intent": "t"})["code"], "part_unknown")

    def test_readback_and_checks(self):
        eid = self.ok({"op": "card", "id": "gw", "title": "API gateway", "badges": ["P0"], "icon": "network", "owner": "nobody",
                       "body": "b", "at": [0, 0], "intent": "t"})["ids"][0]
        el = self.el(eid)
        self.assertEqual(R.get("card").readback(el, False), '{} card gw "API gateway" (P0) icon network · {}x{}'.format(eid, el["w"], el["h"]))
        self.assertIn('body "b"', R.get("card").readback(el, True))
        codes = [p["code"] for p in self.apply([])["warnings"]] + [p["code"] for p in __import__("herdr_team.canvas", fromlist=["x"]).check(
            self.layout, self.team, "alpha-worker")["problems"]]
        self.assertIn("owner_unknown", codes)

    def test_measure_is_deterministic_and_fits(self):
        el = {"type": "card", "text": "Checkout API " * 5, "body": "- a b c d e f g h i j k l m n o p", "badges": [{"text": "P0"}], "clamp": {"title": 2}}
        first = K.measure(el, (240, 80))
        self.assertEqual(first, K.measure(el, (240, 80)))
        self.assertTrue(X.fits(first))
        self.assertTrue(first.truncated, "clamped to 2 title lines")

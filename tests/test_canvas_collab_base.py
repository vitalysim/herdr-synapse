"""Base and stale edits (canvas v2 phase 5, 3): the envelope, what counts as stale, the outcomes, the scan limit, and the
``diff_lines`` vectors (``tests/fixtures/collab/diff-vectors.json``)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from collab_support import LEAD, MEMBER, PEER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_collab as K
from herdr_team.errors import HerdrTeamError

VECTORS = Path(__file__).resolve().parent / "fixtures" / "collab" / "diff-vectors.json"


class Envelope(CollabRig):
    def test_the_envelope_carries_base(self):
        found = C.parse_envelope('{"ops": [{"op": "shape"}], "atomic": true, "base": 405}')
        self.assertEqual((found.ops, found.atomic, found.base), ([{"op": "shape"}], True, 405))
        self.assertEqual(C.parse_envelope({"ops": [], "base": "last"}).base, "last")
        self.assertEqual(C.parse_envelope([{"op": "shape"}]).base, None)
        self.assertEqual(C.parse_batch({"ops": [], "base": 3}), ([], False), "parse_batch accepts base and still returns (ops, atomic)")
        for bad in (-1, 1.5, "soon", True, [1]):
            with self.subTest(base=bad), self.assertRaises(HerdrTeamError) as ctx:
                C.parse_envelope({"ops": [], "base": bad})
            self.assertEqual(ctx.exception.details["field"], "base")

    def test_last_is_the_authors_look_cursor(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 0], "intent": "t"})["ids"][0]
        C.look(self.layout, self.team, MEMBER.name)
        self.ok({"op": "move", "id": box, "by": [0, 40]}, LEAD)
        self.assertEqual(self.refused({"op": "move", "id": box, "by": [20, 0], "intent": "t"}, base="last")["code"], "stale_base")
        C.look(self.layout, self.team, MEMBER.name)
        self.ok({"op": "move", "id": box, "by": [20, 0], "intent": "t"}, base="last")

    def test_a_base_newer_than_the_canvas_is_refused(self):
        # QA phase 5 L7: it would turn the stale check off.
        self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 0], "intent": "t"})
        now = C.current_version(self.team)
        with self.assertRaises(HerdrTeamError) as ctx:
            self.apply([{"op": "shape", "text": "x", "at": [400, 0], "intent": "t"}], base=999999)
        self.assertEqual((ctx.exception.code, ctx.exception.details["field"]), ("op_invalid", "base"))
        self.assertIn("newer than the canvas (v{})".format(now), ctx.exception.message)
        self.ok({"op": "shape", "text": "x", "at": [400, 0], "intent": "t"}, base=now)

    def test_no_base_checks_nothing(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "move", "id": box, "by": [0, 40]}, LEAD)
        self.assertEqual(self.apply([{"op": "move", "id": box, "by": [20, 0], "intent": "t"}])["warnings"], [])


class Stale(CollabRig):
    def setUp(self):
        super().setUp()
        self.box = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 0], "intent": "t"})["ids"][0]
        self.base = C.current_version(self.team)

    def test_changed_by_the_author_only_is_nothing(self):
        self.ok({"op": "move", "id": self.box, "by": [0, 40], "intent": "t"})
        result = self.apply([{"op": "move", "id": self.box, "by": [20, 0], "intent": "t"}], base=self.base)
        self.assertEqual((len(result["applied"]), result["warnings"]), (1, []))

    def test_changed_by_another_agent_applies_with_a_warning(self):
        self.set_roster(**{"alpha-peer": {"manager": True}})
        manager = C.CanvasAuthor("alpha-peer", "member", "cli", True, manager=True, team="alpha")
        self.ok({"op": "move", "id": self.box, "by": [0, 60], "intent": "tidy"}, manager)
        result = self.apply([{"op": "restyle", "id": self.box, "tone": "info", "intent": "t"}], base=self.base)
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["warnings"], [{"index": 0, "code": "stale_base", "ids": [self.box],
                                               "message": "{} changed since v{} by alpha-peer (v{}): moved by c0r+3".format(self.box, self.base, self.base + 1)}])

    def test_changed_by_the_operator_is_refused_with_what_changed(self):
        self.ok({"op": "move", "id": self.box, "by": [40, 0]}, LEAD)
        self.ok({"op": "edit", "id": self.box, "text": "Plans"}, LEAD)
        refusal = self.refused({"op": "move", "id": self.box, "by": [20, 0], "intent": "t"}, base=self.base)
        self.assertEqual(refusal["code"], "stale_base")
        self.assertEqual(refusal["message"], '{} changed since v{}: the operator moved by c+2r0; text "Mine" → "Plans" (v{}); look again and redo'.format(
            self.box, self.base, self.base + 2))
        self.assertEqual(refusal["details"]["base"], self.base)
        self.assertEqual(refusal["details"]["current"], {self.box: self.base + 2})
        self.assertEqual(refusal["details"]["changes"], [{"id": self.box, "by": "human", "by_kind": "human", "seq": self.base + 2,
                                                          "lines": ["moved by c+2r0", 'text "Mine" → "Plans"']}])

    def test_only_aimed_elements_count(self):
        other = self.ok({"op": "shape", "kind": "box", "text": "Other", "at": [400, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "move", "id": other, "by": [0, 40]}, LEAD)
        self.assertEqual(len(self.apply([{"op": "move", "id": self.box, "by": [20, 0], "intent": "t"}], base=self.base)["applied"]), 1)

    def test_too_far_back_is_the_operators(self):
        for _ in range(4):
            self.ok({"op": "move", "id": self.box, "by": [0, 20], "intent": "t"})
        with mock.patch.object(K, "MAX_BASE_SCAN", 2):
            self.ok({"op": "restyle", "id": self.box, "tone": "info", "intent": "t"}, base=self.base)  # the author only: complete enough
            other = C.CanvasAuthor("alpha-peer", "member", "cli", True, manager=True, team="alpha")
            self.set_roster(**{"alpha-peer": {"manager": True}})
            self.ok({"op": "move", "id": self.box, "by": [0, 20], "intent": "t"}, other)
            for _ in range(3):
                self.ok({"op": "shape", "kind": "box", "text": "x", "at": [900, 900], "intent": "t"}, other)
            refusal = self.refused({"op": "move", "id": self.box, "by": [20, 0], "intent": "t"}, base=self.base)
        self.assertEqual(refusal["code"], "stale_base")
        self.assertEqual(refusal["details"]["changes"][0]["by"], None)
        self.assertEqual(refusal["details"]["changes"][0]["lines"], ["changed since v{} (too far back to say how); look again".format(self.base)])

    def test_a_cleared_log_is_too_far_back(self):
        C.clear(self.layout, self.team, "human")
        mine = self.ok({"op": "shape", "kind": "box", "text": "New", "at": [0, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "move", "id": mine, "by": [20, 0], "intent": "t"}, base=self.base)  # only its author changed it since
        hers = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 400]}, LEAD)["ids"][0]
        self.ok({"op": "claim", "region": [0, 400, 400, 600], "label": "t", "intent": "t"})
        self.ok({"op": "settings", "human_edits": "live"}, LEAD)
        refusal = self.refused({"op": "move", "id": hers, "by": [20, 0], "intent": "t"}, base=self.base)
        self.assertEqual(refusal["code"], "stale_base")
        self.assertEqual(refusal["details"]["changes"][0]["by"], "human")
        self.assertEqual(refusal["details"]["changes"][0]["lines"], ["changed since v{} (too far back to say how); look again".format(self.base)])

    def test_the_operators_own_content_becomes_a_proposal_with_a_base_note(self):
        hers = self.ok({"op": "shape", "kind": "box", "text": "Pricing", "at": [400, 0]}, LEAD)["ids"][0]
        base = C.current_version(self.team)
        self.ok({"op": "move", "id": hers, "by": [0, 40]}, LEAD)
        found = self.proposed({"op": "edit", "id": hers, "text": "Plans", "intent": "t"}, base=base)
        self.assertEqual(found["base_note"], ["{}: moved by c0r+2 (v{})".format(hers, base + 1)])
        record = self.proposal(found["proposal"])
        self.assertEqual((record["base"], record["base_note"]), (base, found["base_note"]))
        text = C.apply_text(self.apply([{"op": "edit", "id": hers, "text": "Plans 2", "intent": "t"}], base=base))
        self.assertIn("made against v{}; the operator changed since: ".format(base), text)


class StaleText(CollabRig):
    def test_what_the_operator_changed_of_a_mark_made_after_base(self):
        # QA phase 5 L1: the member made the box after its base; the operator only moved it. The refusal says "moved",
        # not "the operator added box".
        base = C.current_version(self.team)
        box = self.ok({"op": "shape", "kind": "box", "text": "rev 1", "at": [0, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "move", "id": box, "by": [0, 40]}, LEAD)
        refusal = self.refused({"op": "restyle", "id": box, "tone": "info", "intent": "t"}, base=base)
        self.assertEqual(refusal["code"], "stale_base")
        self.assertIn("the operator moved by c0r+2", refusal["message"])
        self.assertNotIn("added", refusal["message"])


class DiffLines(CollabRig):
    def test_the_vectors(self):
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]
        self.assertGreaterEqual(len(vectors), 12)
        for vector in vectors:
            with self.subTest(name=vector["name"]):
                self.assertEqual(K.diff_lines(vector["before"], vector["after"], vector.get("parts", 0)), vector["lines"])
                for line in vector["lines"]:
                    self.assertLessEqual(len(line), K.MAX_LINE_CHARS)

    def test_the_vectors_are_current(self):
        self.assertEqual(K.diff_vectors_text(), VECTORS.read_text(encoding="utf-8"),
                         "rewrite with python3 -m herdr_team.canvas_collab --write-fixtures")

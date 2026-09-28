"""Presence (canvas v2 phase 5, 9): writes by source, validation, freshness and away, the operator's context, pointing,
the busy rule and the selection warning, ``look --region operator``, the stale sweep, and who may write at all."""
from __future__ import annotations

import os
import time

from collab_support import LEAD, MEMBER, PEER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_presence as P
from herdr_team.errors import HerdrTeamError

PAGE_ID = "0123456789abcdef"


class Writes(CollabRig):
    def test_an_applied_batch_writes_the_members_drawing_presence(self):
        now = time.time()
        self.apply([{"op": "shape", "text": "a", "at": [0, 0], "intent": "lay out the funnel"}, {"op": "shape", "text": "b", "at": [400, 0], "intent": "t"}],
                   now=now)
        found = P.members(self.team, now)[0]
        self.assertEqual({k: found[k] for k in ("name", "kind", "status", "intent", "via", "ttl_s")},
                         {"name": "alpha-member", "kind": "member", "status": "drawing", "intent": "lay out the funnel", "via": "auto", "ttl_s": 90})
        self.assertEqual((found["region"], found["ids"]), ([0, 0, 560, 80], ["E-1", "E-2"]))

    def test_a_look_writes_reading(self):
        C.look(self.layout, self.team, MEMBER.name, region="c0r0:c10r10", author=MEMBER)
        found = P.members(self.team, time.time())[0]
        self.assertEqual((found["status"], found["region"], found["via"], found["ttl_s"]), ("reading", [0, 0, 200, 200], "look", 60))

    def test_unverified_or_non_members_write_nothing(self):
        unverified = C.CanvasAuthor("alpha-member", "member", "cli", False)
        self.assertIsNone(P.write_member(self.team, unverified, status="drawing"))
        self.assertIsNone(P.write_member(self.team, LEAD, status="drawing"))
        C.look(self.layout, self.team, "human", author=LEAD)
        self.assertEqual(P.read_all(self.team, time.time())["entries"], [])
        self.apply([{"op": "shape", "text": "hers", "at": [0, 0]}], LEAD)
        self.assertEqual(P.read_all(self.team, time.time())["entries"], [])

    def test_validation(self):
        now = time.time()
        for body, field in (({"page": "nothex"}, "page"), ({"page": PAGE_ID, "viewport": [0, 0, 1]}, "viewport"),
                            ({"page": PAGE_ID, "viewport": [10, 0, 0, 10]}, "viewport"), ({"page": PAGE_ID, "viewport": [0, 0, 1e9, 1]}, "viewport"),
                            ({"page": PAGE_ID, "selection": ["E-1"] * 51}, "selection"), ({"page": PAGE_ID, "selection": ["../x"]}, "selection"),
                            ({"page": PAGE_ID, "editing": "E-x"}, "editing"), ({"page": PAGE_ID, "cursor": ["a", 1]}, "cursor"),
                            ({"page": PAGE_ID, "away": "yes"}, "away"), ({"page": PAGE_ID, "name": "x"}, "name")):
            with self.subTest(field=field), self.assertRaises(HerdrTeamError) as ctx:
                P.write_human(self.team, None, body, now)
            self.assertEqual((ctx.exception.code, ctx.exception.details["field"]), ("usage", field))
        with self.assertRaises(HerdrTeamError) as ctx:
            P.write_member(self.team, MEMBER, status="sleeping")
        self.assertEqual(ctx.exception.details["field"], "status")
        with self.assertRaises(HerdrTeamError) as ctx:
            P.write_member(self.team, MEMBER, status="drawing", intent="token AKIA" + "Q" * 16)
        self.assertEqual(ctx.exception.code, "secret_detected")
        with self.assertRaises(HerdrTeamError) as ctx:
            P.write_member(self.team, MEMBER, status="drawing", intent="x" * 5000)
        self.assertEqual(ctx.exception.code, "canvas_limit")

    def test_a_hand_edited_file_is_checked_again_on_read(self):
        now = time.time()
        P.write_member(self.team, MEMBER, status="drawing", region=[0, 0, 10, 10], now=now)
        path = P.presence_dir(self.team) / "alpha-member.json"
        path.write_text('{"v": 1, "name": "alpha-member", "kind": "member", "at": "%s", "ttl_s": 90, "status": "drawing", "region": [0, 0, "x", 1]}'
                        % C._iso(now))
        self.assertEqual(P.read_all(self.team, now)["entries"], [])
        (P.presence_dir(self.team) / "alpha-peer.json").write_text('{"v": 1, "name": "alpha-member", "kind": "member", "at": "%s", "ttl_s": 90, '
                                                                     '"status": "drawing"}' % C._iso(now))
        self.assertEqual(P.read_all(self.team, now)["entries"], [], "a record under another name is ignored")
        (P.presence_dir(self.team) / "big.json").write_bytes(b"{" + b" " * 5000 + b"}")
        self.assertEqual(P.read_all(self.team, now)["entries"], [])


class Freshness(CollabRig):
    def test_fresh_means_within_its_ttl_and_not_away(self):
        now = time.time()
        P.write_member(self.team, MEMBER, status="waiting", intent="needs the numbers", ttl_s=60, now=now)
        P.write_human(self.team, PAGE_ID, {"viewport": [0, 0, 800, 600]}, now)
        self.assertEqual([d["name"] for d in P.read_all(self.team, now + 10)["entries"]], ["alpha-member", "human"])
        self.assertEqual([d["name"] for d in P.read_all(self.team, now + 45)["entries"]], ["alpha-member"], "the page's 30 s ran out")
        self.assertEqual(P.read_all(self.team, now + 61)["entries"], [])
        P.write_human(self.team, PAGE_ID, {"viewport": [0, 0, 800, 600], "away": True}, now)
        self.assertIsNone(P.operator(self.team, now + 1), "away is not here")

    def test_the_operator_is_her_freshest_page(self):
        now = time.time()
        P.write_human(self.team, "a" * 16, {"viewport": [0, 0, 100, 100]}, now - 5)
        P.write_human(self.team, "b" * 16, {"viewport": [0, 0, 200, 200]}, now)
        self.assertEqual(P.operator(self.team, now)["page"], "b" * 16)

    def test_old_files_are_swept(self):
        now = time.time()
        P.write_member(self.team, MEMBER, status="drawing", now=now - 700)
        path = P.presence_dir(self.team) / "alpha-member.json"
        os.utime(path, (now - 700, now - 700))
        P.read_all(self.team, now)
        self.assertFalse(path.exists())

    def test_clear_and_signature(self):
        before = P.signature(self.team)
        P.write_member(self.team, MEMBER, status="drawing")
        after = P.signature(self.team)
        self.assertNotEqual(before, after)
        self.assertTrue(P.clear_member(self.team, "alpha-member"))
        self.assertEqual(P.signature(self.team), before)
        self.assertFalse(P.clear_member(self.team, "../x"))


    def test_a_look_without_a_region_says_reading_without_a_place(self):
        # QA phase 5 L8: a look at the whole board is not a halo around the whole board.
        self.apply([{"op": "shape", "text": "a", "at": [0, 0], "intent": "t"}, {"op": "shape", "text": "b", "at": [4000, 3000], "intent": "t"}])
        C.look(self.layout, self.team, MEMBER.name, author=MEMBER)
        found = P.members(self.team, time.time())[0]
        self.assertEqual((found["status"], found["region"]), ("reading", None))

    def test_a_member_that_left_is_not_present(self):
        # QA phase 5 M3: whatever its file says, a member off the roster has no halo, no here: line, no Agents row.
        now = time.time()
        P.write_member(self.team, PEER, status="drawing", intent="long focus", ttl_s=3600, now=now)
        P.write_member(self.team, MEMBER, status="drawing", now=now)
        self.assertEqual([d["name"] for d in P.members(self.team, now)], ["alpha-member", "alpha-peer"])
        self.set_roster(**{"alpha-peer": {"status": "left"}})
        self.assertEqual([d["name"] for d in P.members(self.team, now)], ["alpha-member"])
        self.assertNotIn("alpha-peer", C.look(self.layout, self.team, MEMBER.name)["text"].split("here:", 1)[-1].splitlines()[0])

    def test_removing_a_member_clears_its_presence(self):
        from herdr_team import roster as R

        P.write_member(self.team, PEER, status="drawing", ttl_s=3600)
        path = P.presence_dir(self.team) / "alpha-peer.json"
        self.assertTrue(path.exists())
        R._clear_canvas_presence(self.team, "alpha-peer")
        self.assertFalse(path.exists())

    def test_the_operators_pages_are_capped(self):
        # QA phase 5 L11: one session making fresh page ids keeps at most MAX_HUMAN_PAGES files; the oldest go first.
        now = time.time()
        pages = ["{:016x}".format(i + 1) for i in range(P.MAX_HUMAN_PAGES + 3)]
        for i, page in enumerate(pages):
            P.write_human(self.team, page, {"viewport": [0, 0, 10, 10]}, now)
            os.utime(P.presence_dir(self.team) / "human-{}.json".format(page), (now - 100 + i, now - 100 + i))
        kept = sorted(e.name for e in os.scandir(P.presence_dir(self.team)) if e.name.startswith("human-"))
        self.assertEqual(kept, sorted("human-{}.json".format(p) for p in pages[-P.MAX_HUMAN_PAGES:]))
        P.write_human(self.team, pages[-1], {"viewport": [0, 0, 20, 20]}, now)
        self.assertEqual(len([e for e in os.scandir(P.presence_dir(self.team)) if e.name.startswith("human-")]), P.MAX_HUMAN_PAGES,
                         "a page already there is rewritten in place")


class OperatorContext(CollabRig):
    def setUp(self):
        super().setUp()
        self.box = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 0], "intent": "t"})["ids"][0]
        self.big = self.ok({"op": "frame", "title": "Area", "at": [-200, -200], "w": 1000, "h": 800}, LEAD)["ids"][0]

    def test_every_member_result_says_what_the_operator_is_looking_at(self):
        now = time.time()
        P.write_human(self.team, PAGE_ID, {"viewport": [200, 80, 1200, 600], "selection": [self.box], "editing": None, "cursor": [20, 20]}, now - 4)
        result = self.apply([{"op": "shape", "text": "far", "at": [3000, 0], "intent": "t"}], now=now)
        self.assertEqual({k: v for k, v in result["operator"].items() if k != "at"},
                         {"viewport": [200, 80, 1200, 600], "selection": [self.box], "editing": None, "pointing_at": self.box, "age_s": 4})
        self.assertIn("operator: viewing c10r4:c60r30 · selected {} · pointing at {} · 4s ago".format(self.box, self.box), C.apply_text(result))
        self.assertIsNone(self.apply([{"op": "shape", "text": "x", "at": [5000, 0], "intent": "t"}], PEER, now=now + 60)["operator"])
        self.assertNotIn("operator", self.apply([{"op": "shape", "text": "hers", "at": [6000, 0]}], LEAD))

    def test_pointing_at_is_the_topmost_mark_under_her_cursor(self):
        self.assertEqual(P.pointing_at({"cursor": [20, 20]}, self.scene()["elements"]), self.box)
        self.assertEqual(P.pointing_at({"cursor": [700, 500]}, self.scene()["elements"]), self.big)
        self.assertIsNone(P.pointing_at({"cursor": [5000, 5000]}, self.scene()["elements"]))
        self.assertIsNone(P.pointing_at({}, self.scene()["elements"]))

    def test_her_selection_only_warns(self):
        P.write_human(self.team, PAGE_ID, {"selection": [self.box]}, time.time())
        result = self.apply([{"op": "move", "id": self.box, "by": [20, 0], "intent": "t"}])
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["warnings"][0], {"index": 0, "code": "operator_selected", "message": "the operator has {} selected right now".format(self.box),
                                                 "ids": [self.box]})

    def test_what_she_is_editing_is_busy_for_agents_only(self):
        P.write_human(self.team, PAGE_ID, {"editing": self.box}, time.time())
        refusal = self.refused({"op": "restyle", "id": self.box, "tone": "info", "intent": "t"})
        self.assertEqual((refusal["code"], refusal["details"]["id"]), ("element_busy", self.box))
        self.ok({"op": "restyle", "id": self.box, "tone": "info"}, LEAD)
        P.write_human(self.team, PAGE_ID, {"editing": self.box, "away": True}, time.time())
        self.ok({"op": "restyle", "id": self.box, "tone": "danger", "intent": "t"})

    def test_what_she_is_editing_on_any_fresh_page_is_busy(self):
        # QA phase 5 L6: a second tab's fresher heartbeat does not lift the busy check of the tab editing it.
        now = time.time()
        P.write_human(self.team, PAGE_ID, {"editing": self.box}, now - 3)
        P.write_human(self.team, "fedcba9876543210", {"viewport": [0, 0, 100, 100]}, now - 1)
        self.assertEqual(P.operator(self.team, now)["page"], "fedcba9876543210")
        self.assertEqual(self.refused({"op": "restyle", "id": self.box, "tone": "info", "intent": "t"}, now=now)["code"], "element_busy")

    def test_look_at_the_operators_view_and_the_presence_lines(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            C.look(self.layout, self.team, MEMBER.name, region="operator")
        self.assertEqual((ctx.exception.code, ctx.exception.message), ("usage", "the operator has no page open right now; look without --region operator"))
        now = time.time()
        P.write_human(self.team, PAGE_ID, {"viewport": [0, 0, 200, 100], "selection": [self.box], "editing": self.box, "cursor": [20, 20]}, now)
        P.write_member(self.team, PEER, status="drawing", region=[1600, 80, 2400, 400], intent="pricing table", now=now)
        look = C.look(self.layout, self.team, MEMBER.name, region="operator")
        self.assertEqual(look["region"], [0, 0, 200, 100])
        lines = look["text"].splitlines()
        self.assertIn("operator: viewing c0r0:c10r5 · selected {} · editing {} · pointing at {} · 0s ago".format(self.box, self.box, self.box), lines)
        self.assertIn('here: alpha-peer drawing c80r4:c120r20 "pricing table" (0s ago)', lines)
        self.assertEqual(look["presence"]["operator"]["pointing_at"], self.box)
        self.assertEqual([m["name"] for m in look["presence"]["members"]], ["alpha-member", "alpha-peer"], "the text leaves out the reader")
        own = C.look(self.layout, self.team, "human")
        self.assertFalse(any(line.startswith("operator:") for line in own["text"].splitlines()), "her own look does not describe her")

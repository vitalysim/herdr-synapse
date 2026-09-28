"""Per-author undo and revert (canvas v2 phase 5, 6): several batches in one op, the skip rule with interleaved authors,
``force``, who may undo what, and the result."""
from __future__ import annotations

import time

from collab_support import DEPUTY, LEAD, MANAGER, MEMBER, PAGE, PEER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_presence as P
from herdr_team.errors import HerdrTeamError


class Revert(CollabRig):
    def setUp(self):
        super().setUp()
        self.a = self.ok({"op": "shape", "kind": "box", "text": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        self.b = self.ok({"op": "shape", "kind": "box", "text": "B", "at": [400, 0], "intent": "t"})["ids"][0]
        self.since = C.current_version(self.team)
        self.first = self.apply([{"op": "move", "id": self.a, "by": [0, 40], "intent": "t"}])["batch"]
        self.second = self.apply([{"op": "restyle", "id": self.b, "tone": "info", "intent": "t"}, {"op": "move", "id": self.a, "by": [0, 40], "intent": "t"}])["batch"]

    def test_every_batch_of_an_author_since_a_version(self):
        result = self.apply([{"op": "undo", "author": "alpha-member", "since": self.since, "intent": "take it all back"}])
        undo = result["applied"][0]["undo"]
        self.assertEqual((undo["batches"], undo["restored"], undo["of"], undo["skipped"]), ([self.second, self.first], 2, 2, []))
        self.assertEqual((self.el(self.a)["y"], self.el(self.b)["style"]["tone"]), (0, "neutral"))
        batches = self.scene()["batches"]
        self.assertTrue(batches[self.first]["undone"] and batches[self.second]["undone"])
        event = C._read_events(self.team)[-1]
        self.assertEqual((event.get("undoes"), event["undoes_all"]), (None, [self.second, self.first]))
        self.assertIn("undid {}, {}".format(self.second, self.first), C.summarize(event))
        self.assertEqual(self.refused({"op": "undo", "author": "alpha-member", "since": self.since, "intent": "t"})["code"], "element_unknown")

    def test_without_since_every_batch_still_held(self):
        result = self.apply([{"op": "undo", "author": "alpha-member", "intent": "t"}])
        self.assertEqual(len(result["applied"][0]["undo"]["batches"]), 4)
        self.assertFalse(any(e["id"] in (self.a, self.b) for e in self.scene()["elements"]))

    def test_a_later_edit_by_someone_else_is_skipped_and_said(self):
        self.ok({"op": "restyle", "id": self.a, "tone": "danger"}, LEAD)
        result = self.apply([{"op": "undo", "author": "alpha-member", "since": self.since, "intent": "t"}])
        undo = result["applied"][0]["undo"]
        self.assertEqual(undo["skipped"], [{"id": self.a, "by": "human", "seq": self.since + 4}])
        self.assertEqual((undo["restored"], undo["of"]), (1, 2))
        self.assertEqual(self.el(self.a)["y"], 80, "the member's moves under the operator's later edit stay")
        self.assertEqual(self.el(self.a)["style"]["tone"], "danger")
        self.assertEqual(result["warnings"][0]["message"], '1 of 2 reverted; {} edited by the operator later (v{}): force it with "force": true (the operator)'.format(
            self.a, self.since + 4))
        self.assertIn("1 of 2 reverted", C.apply_text(result))

    def test_force_writes_back_anyway_and_is_the_leads(self):
        self.ok({"op": "restyle", "id": self.a, "tone": "danger"}, LEAD)
        self.assertEqual(self.refused({"op": "undo", "batch": self.second, "force": True, "intent": "t"})["code"], "operator_only")
        self.ok({"op": "undo", "author": "alpha-member", "since": self.since, "force": True}, LEAD)
        self.assertEqual((self.el(self.a)["y"], self.el(self.a)["style"]["tone"]), (0, "neutral"))

    def test_newest_first_restores_what_it_can(self):
        # The member moved A, the operator restyled it, the member moved it again: reverting the member's batches takes
        # back the newer move (nobody changed A after it) and skips the older one (the operator did, after it).
        self.ok({"op": "restyle", "id": self.b, "tone": "danger"}, LEAD)
        third = self.apply([{"op": "move", "id": self.b, "by": [0, 100], "intent": "t"}])["batch"]
        result = self.apply([{"op": "undo", "author": "alpha-member", "since": self.since, "intent": "t"}])
        undo = result["applied"][0]["undo"]
        self.assertEqual(undo["batches"], [third, self.second, self.first])
        self.assertEqual(undo["skipped"], [{"id": self.b, "by": "human", "seq": self.since + 4}])
        b = self.el(self.b)
        self.assertEqual((b["y"], b["style"]["tone"]), (0, "danger"), "back to after the operator's edit, not before it")

    def test_who_may_undo_what(self):
        hers = self.apply([{"op": "shape", "text": "Hers", "at": [2000, 0]}], PAGE)["batch"]
        for author in (DEPUTY, MANAGER, MEMBER):
            self.assertEqual(self.refused({"op": "undo", "batch": hers, "intent": "t"}, author)["code"], "operator_only", author.name)
            self.assertEqual(self.refused({"op": "undo", "author": "human", "intent": "t"}, author)["code"], "operator_only", author.name)
        # A peer's batches are for the manager, a delegate or the operator: not "operator only" (QA phase 5 L15).
        self.assertEqual(self.refused({"op": "undo", "author": "alpha-member", "intent": "t"}, PEER)["code"], "element_not_yours")
        self.assertEqual(self.refused({"op": "undo", "batch": self.first, "intent": "t"}, PEER)["code"], "element_not_yours")
        self.ok({"op": "undo", "batch": self.first, "intent": "t"}, MANAGER)
        self.ok({"op": "undo", "author": "alpha-member", "intent": "t"}, DEPUTY)
        self.ok({"op": "undo", "author": "human"}, LEAD)

    def test_the_op_shapes(self):
        for op, field in (({"op": "undo", "intent": "t"}, "batch"), ({"op": "undo", "batch": self.first, "author": "alpha-member", "intent": "t"}, "batch"),
                          ({"op": "undo", "batch": self.first, "since": 3, "intent": "t"}, "since"), ({"op": "undo", "batch": "E-3", "intent": "t"}, "batch")):
            with self.subTest(op=op):
                self.assertEqual(self.refused(op)["details"]["field"], field)
        self.ok({"op": "undo", "batch": self.first, "intent": "t"})
        self.assertEqual(self.refused({"op": "undo", "batch": self.first, "intent": "t"})["code"], "op_invalid", "already undone")

    def test_undo_of_the_same_batch_twice_in_one_revert_is_one_revert(self):
        result = self.apply([{"op": "undo", "author": "alpha-member", "since": self.since, "intent": "t"},
                             {"op": "undo", "author": "alpha-member", "since": self.since, "intent": "t"}])
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["refused"][0]["code"], "element_unknown")


class Guarded(CollabRig):
    """QA phase 5 H1: an undo is not proposable, but a freeze and the operator's editing bind it as they bind a direct op,
    for everyone but the lead (delegates too). M1: what an undo left stays on the batch, for a retry or the lead's force."""

    def setUp(self):
        super().setUp()
        self.made = self.apply([{"op": "shape", "kind": "box", "text": "Keep", "at": [0, 0], "intent": "t"}])
        self.box = self.made["applied"][0]["ids"][0]
        self.since = C.current_version(self.team)
        self.moved = self.apply([{"op": "move", "id": self.box, "by": [200, 0], "intent": "t"}])["batch"]

    def undo_rolled_back(self, op, author):
        """What an undo does, rolled back (an atomic batch whose second op is refused): its applied entry and warnings."""
        try:
            self.apply([op, {"op": "no_such_op"}], author, atomic=True)
        except HerdrTeamError as err:
            return err.details["applied"][0], err.details["warnings"]
        raise AssertionError("the atomic batch was not refused")

    def test_an_undo_leaves_what_an_id_freeze_holds(self):
        drawn = self.apply([{"op": "shape", "kind": "box", "text": "Fresh", "at": [0, 600], "intent": "t"}])
        fresh = drawn["applied"][0]["ids"][0]
        xid = self.ok({"op": "freeze", "ids": [self.box, fresh], "label": "keep"}, LEAD)["ids"][0]
        for frozen in ("propose", "refuse"):
            self.ok({"op": "settings", "frozen": frozen}, LEAD)
            for author in (MEMBER, MANAGER, DEPUTY):
                for batch, eid, what in ((self.moved, self.box, "move it back"), (drawn["batch"], fresh, "delete it")):
                    with self.subTest(frozen=frozen, author=author.name, undo=what):
                        entry, warnings = self.undo_rolled_back({"op": "undo", "batch": batch, "intent": "t"}, author)
                        self.assertEqual(entry["undo"]["skipped"], [{"id": eid, "reason": "frozen", "freeze": xid}])
                        self.assertEqual((entry["undo"]["restored"], entry["undo"]["of"]), (0, 1))
                        self.assertIn("{} is frozen ({}): the operator holds it as it is".format(eid, xid), warnings[-1]["message"])
        self.assertEqual(self.el(self.box)["x"], 200)
        self.ok({"op": "undo", "batch": self.moved}, LEAD)
        self.assertEqual(self.el(self.box)["x"], 0, "the lead is never guarded")

    def test_an_undo_does_not_move_a_frozen_mark_back(self):
        self.ok({"op": "freeze", "region": [150, -50, 500, 200], "label": "done"}, LEAD)
        self.ok({"op": "settings", "frozen": "refuse"}, LEAD)
        self.assertEqual(self.refused({"op": "move", "id": self.box, "by": [-200, 0], "intent": "t"})["code"], "frozen")
        result = self.apply([{"op": "undo", "batch": self.moved, "intent": "t"}])
        self.assertEqual(result["applied"][0]["undo"]["skipped"][0]["reason"], "frozen")
        self.assertEqual(self.el(self.box)["x"], 200)

    def test_an_undo_does_not_bring_a_mark_back_into_a_frozen_region(self):
        gone = self.apply([{"op": "delete", "id": self.box, "intent": "t"}])["batch"]
        self.ok({"op": "freeze", "region": [150, -50, 500, 200], "label": "empty on purpose"}, LEAD)
        result = self.apply([{"op": "undo", "batch": gone, "intent": "t"}])
        self.assertEqual(result["applied"][0]["undo"]["skipped"][0]["reason"], "frozen")
        self.assertFalse(self.has(self.box))

    def test_an_undo_of_what_the_operator_is_editing_waits(self):
        P.write_human(self.team, "0123456789abcdef", {"editing": self.box}, time.time())
        for author in (MEMBER, DEPUTY):
            refusal = self.refused({"op": "undo", "batch": self.moved, "intent": "t"}, author)
            self.assertEqual((refusal["code"], refusal["details"]["retry_after_s"]), ("element_busy", 5), author.name)
        self.assertEqual(self.el(self.box)["x"], 200)
        P.write_human(self.team, "0123456789abcdef", {}, time.time())
        self.ok({"op": "undo", "batch": self.moved, "intent": "t"})
        self.assertEqual(self.el(self.box)["x"], 0)

    def test_what_a_freeze_left_is_undone_once_it_is_thawed(self):
        xid = self.ok({"op": "freeze", "ids": [self.box]}, LEAD)["ids"][0]
        self.ok({"op": "undo", "batch": self.moved, "intent": "t"})
        self.assertEqual(self.scene()["batches"][self.moved]["left"], [["element", self.box]])
        refusal = self.refused({"op": "undo", "batch": self.moved, "intent": "t"})
        self.assertEqual(refusal["code"], "op_invalid")
        self.assertIn("{} is frozen ({})".format(self.box, xid), refusal["message"])
        self.ok({"op": "thaw", "id": xid}, LEAD)
        self.ok({"op": "undo", "batch": self.moved, "intent": "t"})
        self.assertEqual(self.el(self.box)["x"], 0)
        self.assertNotIn("left", self.scene()["batches"][self.moved])
        self.assertEqual(self.refused({"op": "undo", "batch": self.moved, "intent": "t"})["message"], "{} is already undone".format(self.moved))

    def test_the_leads_force_after_a_skipped_revert(self):
        # QA phase 5 M1: the warning says to force it; that works after the revert, by batch and by author.
        self.ok({"op": "restyle", "id": self.box, "tone": "danger"}, LEAD)
        result = self.apply([{"op": "undo", "batch": self.moved, "intent": "t"}])
        self.assertIn('force it with "force": true', result["warnings"][0]["message"])
        self.assertEqual(self.el(self.box)["x"], 200)
        refusal = self.refused({"op": "undo", "batch": self.moved, "intent": "t"})
        self.assertIn("edited by the operator later", refusal["message"])
        self.assertEqual(self.refused({"op": "undo", "batch": self.moved, "force": True, "intent": "t"})["code"], "operator_only")
        forced = self.ok({"op": "undo", "batch": self.moved, "force": True}, LEAD)
        self.assertEqual((forced["undo"]["restored"], self.el(self.box)["x"]), (1, 0))
        self.assertEqual(self.refused({"op": "undo", "batch": self.moved, "force": True}, LEAD)["message"], "{} is already undone".format(self.moved))

    def test_the_leads_force_by_author_after_a_skipped_revert(self):
        self.ok({"op": "restyle", "id": self.box, "tone": "danger"}, LEAD)
        since = self.since
        self.ok({"op": "undo", "author": "alpha-member", "since": since}, LEAD)
        self.assertEqual(self.el(self.box)["x"], 200, "skipped: the operator restyled it later")
        self.assertEqual(self.refused({"op": "undo", "author": "alpha-member", "since": since}, LEAD)["code"], "element_unknown")
        forced = self.ok({"op": "undo", "author": "alpha-member", "since": since, "force": True}, LEAD)
        self.assertEqual((forced["undo"]["batches"], self.el(self.box)["x"]), ([self.moved], 0))


class Follow(CollabRig):
    """QA phase 5 L2-L4: comments come back with their element, "N of M" counts marks, a proposal goes with its batch."""

    def test_undoing_your_delete_puts_the_comments_back_on(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 0], "intent": "t"})["ids"][0]
        note = self.ok({"op": "comment", "at": box, "text": "why?", "intent": "t"}, PEER)["ids"][0]
        gone = self.apply([{"op": "delete", "id": box, "intent": "t"}])["batch"]
        self.assertEqual((self.el(note)["on"], self.el(note)["was_on"]), (None, box))
        result = self.apply([{"op": "undo", "batch": gone, "intent": "t"}])
        self.assertEqual(result["warnings"], [], "a peer's comment that followed is not 'left as it is'")
        self.assertEqual(self.el(note)["on"], box)
        self.assertNotIn("was_on", self.el(note))
        self.assertEqual((result["applied"][0]["undo"]["restored"], result["applied"][0]["undo"]["of"]), (1, 1))

    def test_undoing_your_move_takes_the_comments_back_without_a_warning(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 0], "intent": "t"})["ids"][0]
        note = self.ok({"op": "comment", "at": box, "text": "why?", "intent": "t"}, PEER)["ids"][0]
        where = (self.el(note)["x"], self.el(note)["y"])
        moved = self.apply([{"op": "move", "id": box, "by": [200, 0], "intent": "t"}])["batch"]
        self.assertNotEqual((self.el(note)["x"], self.el(note)["y"]), where)
        result = self.apply([{"op": "undo", "batch": moved, "intent": "t"}])
        self.assertEqual(result["warnings"], [])
        self.assertEqual((self.el(note)["x"], self.el(note)["y"]), where)

    def test_n_of_m_counts_marks_not_claims(self):
        made = self.apply([{"op": "shape", "kind": "box", "text": "Free", "at": [3000, 3000], "intent": "t"}])
        self.assertTrue(made["applied"][0].get("auto_claim"), "drawing in free space claims it")
        undo = self.ok({"op": "undo", "batch": made["batch"], "intent": "t"})["undo"]
        self.assertEqual((undo["restored"], undo["of"]), (1, 1))

    def test_undoing_a_batch_that_only_proposed_withdraws_the_proposal(self):
        hers = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, LEAD)["ids"][0]
        result = self.apply([{"op": "move", "id": hers, "by": [0, 40], "intent": "t"}])
        pid = result["proposed"][0]["proposal"]
        undone = self.apply([{"op": "undo", "batch": result["batch"], "intent": "t"}])
        self.assertEqual(undone["applied"][0]["undo"]["withdrew"], [pid])
        self.assertIn("withdrew " + pid, C.apply_text(undone))
        record = next(p for p in C._load_state(self.team).proposals.values() if p["id"] == pid)
        self.assertEqual((record["status"], record["decided_by"]), ("withdrawn", "alpha-member"))

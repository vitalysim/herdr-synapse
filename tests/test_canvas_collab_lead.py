"""The lead's guarantees (canvas v2 phase 5, 1.2): one test apiece, plus the raised-authority rules of section 13.2."""
from __future__ import annotations

import json
import time

from collab_support import DEPUTY, LEAD, MANAGER, MEMBER, PAGE, PEER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_collab as K
from herdr_team import canvas_display as D
from herdr_team import canvas_presence as P
from herdr_team import features as F
from herdr_team import store


class LeadGuarantees(CollabRig):
    def lead_box(self, text="Pricing", at=(0, 0)):
        return self.ok({"op": "shape", "kind": "box", "text": text, "at": list(at)}, LEAD)["ids"][0]

    def test_1_nothing_an_agent_does_changes_her_marks_unseen(self):
        box = self.lead_box()
        before = self.el(box)
        found = self.proposed({"op": "move", "id": box, "by": [40, 0], "intent": "align it"})
        self.assertEqual((found["reason"], found["targets"]), ("human_made", [box]))
        self.assertEqual(self.el(box), before, "a proposal changes nothing on the canvas")
        # Live with revert: it applies, the event names what it touched, and the notice line says so.
        self.ok({"op": "settings", "human_edits": "live"}, LEAD)
        result = self.apply([{"op": "move", "id": box, "by": [40, 0], "intent": "align it"}])
        self.assertEqual(result["applied"][0]["ids"], [box])
        events = [json.loads(line) for line in (F.whiteboard_dir(self.team) / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["touched_human"], [box])
        self.assertIn("changed 1 of the operator's marks", C.counts_text(C._event_counts(events[-1])))
        # The notice names the batch to undo (4.7; QA phase 5 L12), coalesced or not.
        notices = store.read_json(C._file(self.team, C.NOTICES_FILE))
        pending = notices["authors"]["alpha-member"]["pending"]
        self.assertEqual(pending["touched"], [result["batch"]])
        self.assertIn("changed 1 of the operator's marks (undo {} to revert)".format(result["batch"]), C.counts_text(pending["counts"], pending))

    def test_2_the_lead_is_never_refused_by_the_collaboration_layer(self):
        theirs = self.ok({"op": "shape", "kind": "box", "text": "Theirs", "at": [0, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "claim", "region": [400, 0, 800, 400], "label": "theirs", "intent": "t"})
        self.ok({"op": "freeze", "ids": [theirs], "label": "hold"}, LEAD)
        self.ok({"op": "settings", "frozen": "refuse"}, LEAD)
        P.write_human(self.team, "0123456789abcdef", {"editing": theirs}, time.time())
        base = C.current_version(self.team) - 3
        # A frozen mark, being edited on another of her pages, in a member's claim, with a stale base: all hers to change.
        result = self.apply([{"op": "move", "id": theirs, "by": [20, 0]}, {"op": "shape", "text": "in their lane", "at": [420, 20]}], LEAD, base=base)
        self.assertEqual((len(result["applied"]), result["proposed"], result["refused"]), (2, [], []))
        self.assertEqual(self.refused({"op": "move", "id": theirs, "by": [0, 20], "intent": "t"}, DEPUTY)["code"], "element_busy")
        P.write_human(self.team, "0123456789abcdef", {}, time.time())
        self.assertEqual(self.refused({"op": "move", "id": theirs, "by": [0, 20], "intent": "t"}, DEPUTY)["code"], "frozen", "delegates too")

    def test_2_a_stale_base_only_warns_the_lead(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Theirs", "at": [0, 0], "intent": "t"})["ids"][0]
        base = C.current_version(self.team)
        self.ok({"op": "move", "id": box, "by": [20, 0], "intent": "t"})
        result = self.apply([{"op": "move", "id": box, "by": [0, 20]}], LEAD, base=base)
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual([w["code"] for w in result["warnings"] if w["code"] != "inside_claim"], ["stale_base"])

    def test_3_only_the_lead_decides(self):
        box = self.lead_box()
        pid = self.proposed({"op": "move", "id": box, "by": [40, 0], "intent": "t"})["proposal"]
        checkpoint = self.ok({"op": "checkpoint", "label": "start"}, LEAD)["ids"][0]
        batch = self.apply([{"op": "shape", "text": "x", "at": [900, 0]}], LEAD)["batch"]
        freeze = self.ok({"op": "freeze", "region": [2000, 0, 2200, 200]}, LEAD)["ids"][0]
        ops = [{"op": "accept", "id": pid}, {"op": "reject", "id": pid}, {"op": "freeze", "region": [3000, 0, 3200, 200]}, {"op": "thaw", "id": freeze},
               {"op": "restore", "id": checkpoint}, {"op": "settings", "human_edits": "live"}, {"op": "undo", "batch": batch, "force": True}]
        for author in (DEPUTY, MANAGER, MEMBER):
            for op in ops:
                with self.subTest(author=author.name, op=op["op"]):
                    refusal = self.refused(dict(op, intent="t"), author)
                    self.assertEqual(refusal["code"], "operator_only")
                    self.assertIn("operator in person", refusal["message"])
        self.ok({"op": "reject", "id": pid, "note": "keep it"}, LEAD)

    def test_4_a_stale_edit_never_overwrites_the_lead(self):
        mine = self.ok({"op": "shape", "kind": "box", "text": "Draft", "at": [0, 0], "intent": "t"})["ids"][0]
        hers = self.lead_box("Pricing", (400, 0))
        base = C.current_version(self.team)
        self.ok({"op": "move", "id": mine, "by": [0, 40]}, LEAD)
        self.ok({"op": "edit", "id": hers, "text": "Plans"}, LEAD)
        refusal = self.refused({"op": "move", "id": mine, "by": [20, 0], "intent": "t"}, base=base)
        self.assertEqual(refusal["code"], "stale_base")
        self.assertEqual(refusal["details"]["changes"][0]["by"], "human")
        self.assertEqual(self.el(mine)["y"], 40)
        # On her own mark the op becomes a proposal against what is there now, with a note of what she changed.
        found = self.proposed({"op": "move", "id": hers, "by": [20, 0], "intent": "t"}, base=base)
        self.assertEqual(found["base_note"], ['{}: text "Pricing" → "Plans" (v{})'.format(hers, base + 2)])

    def test_5_undo_is_per_author(self):
        mine = self.ok({"op": "shape", "kind": "box", "text": "Agent", "at": [0, 0], "intent": "t"})["ids"][0]
        agent_batch = self.apply([{"op": "move", "id": mine, "by": [40, 0], "intent": "t"}])["batch"]
        self.ok({"op": "restyle", "id": mine, "tone": "danger"}, LEAD)
        refusal = self.refused({"op": "undo", "author": "alpha-member", "intent": "t"}, LEAD)
        self.assertEqual(refusal["details"]["skipped"], [{"id": mine, "by": "human", "seq": 3}])
        self.assertIn("edited by you later (v3); undo B-3 first, or force it", refusal["message"], "her own later edit is hers: 'you'")
        self.assertEqual(self.el(mine)["style"]["tone"], "danger", "her later edit stays")
        self.assertFalse(self.scene()["batches"][agent_batch]["undone"], "nothing taken back: not struck")
        # Her own undo reverts hers only; a delegate may not undo her batches.
        hers = self.apply([{"op": "shape", "text": "Hers", "at": [600, 0]}], PAGE)["batch"]
        self.assertEqual(self.refused({"op": "undo", "batch": hers, "intent": "t"}, DEPUTY)["code"], "operator_only")
        self.ok({"op": "undo", "batch": hers}, PAGE)

    def test_6_presence_is_a_view_not_a_control(self):
        # Only the page server writes the operator's presence; an agent's write names only itself.
        self.assertIsNone(P.write_member(self.team, LEAD, status="drawing"))
        forged = P.presence_dir(self.team) / "human-0123456789abcdef.json"
        P.write_member(self.team, MEMBER, status="drawing", region=[0, 0, 100, 100])
        store.write_json(forged, {"v": 1, "name": "human", "kind": "member", "page": "0123456789abcdef", "at": C._iso(time.time()),
                                  "ttl_s": 30, "status": "drawing"})
        self.assertIsNone(P.operator(self.team, time.time()), "a record that is not the operator's page's shape is ignored")
        # The one thing presence can do: refuse an agent's op on what she is editing right now (retryable).
        box = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 0], "intent": "t"})["ids"][0]
        P.write_human(self.team, "fedcba9876543210", {"editing": box, "selection": [box]}, time.time())
        refusal = self.refused({"op": "edit", "id": box, "text": "x", "intent": "t"})
        self.assertEqual((refusal["code"], refusal["details"]["retry_after_s"]), ("element_busy", 5))
        self.ok({"op": "edit", "id": box, "text": "hers"}, LEAD)

    def test_7_attribution_is_visible(self):
        box = self.lead_box()
        pid = self.proposed({"op": "move", "id": box, "by": [0, 200], "intent": "line it up"})["proposal"]
        entry = next(e for e in C.display(self.team)["entries"] if e["id"] == pid)
        pill = entry["items"][-1]["items"][1]["lines"][0]["t"]
        self.assertEqual(pill, "{} · alpha-member suggests: line it up".format(pid))
        new = self.proposed({"op": "shape", "text": "Idea", "at": [20, 20], "intent": "an idea"})
        self.ok({"op": "accept", "id": new["proposal"]}, LEAD)
        created = self.el(new["created"][0])
        self.assertEqual((created["author"], created["author_kind"], created["accepted"]), ("alpha-member", "member", new["proposal"]))

    def test_8_proposals_never_wake_anyone(self):
        box = self.lead_box()
        self.proposed({"op": "move", "id": box, "by": [0, 200], "intent": "t"})
        board = store.BoardStore(self.team).read()
        self.assertEqual([r for r in board if r.get("event") == "canvas_sent"], [])
        changed = [r for r in board if r.get("event") == "canvas_changed" and (r.get("canvas") or {}).get("author") == "alpha-member"]
        self.assertTrue(changed)
        # The notice names the proposals (QA phase 5 L12), so a v1 page's operator can accept or reject from the CLI.
        pid = next(p["id"] for p in self.scene()["proposals"])
        self.assertIn("proposed 1 change for the operator to review: {}".format(pid), changed[-1]["text"])
        self.assertEqual(changed[-1]["canvas"]["proposals"], [pid])


class RaisedAuthority(CollabRig):
    """13.2: raised authority exists for one re-run of one proposable op; a rule never sees the raised author."""

    def test_a_spy_rule_never_sees_the_raised_author(self):
        seen = []

        def spy(review):
            seen.append((review.author, review.raised))
            return None

        K.register_rule("spy", spy, 5)
        self.addCleanup(K.unregister_rule, "spy")
        box = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, LEAD)["ids"][0]
        self.proposed({"op": "edit", "id": box, "text": "x", "intent": "t"}, MANAGER)
        self.assertEqual(seen[-1], (MANAGER, True))
        self.assertFalse(seen[-1][0].operator)

    def test_raised_is_restored_even_when_the_op_fails_again(self):
        peer_box = self.ok({"op": "shape", "kind": "box", "text": "Peer", "at": [0, 0], "intent": "t"}, PEER)["ids"][0]
        self.ok({"op": "lock", "region": [0, 200, 400, 400]}, LEAD)
        result = self.apply([{"op": "move", "id": peer_box, "by": [0, 220], "intent": "t"}, {"op": "shape", "text": "after", "at": [800, 0], "intent": "t"}])
        self.assertEqual(result["refused"][0]["code"], "canvas_locked", "the locked rule checks the raised run as the member")
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(self.el(peer_box)["y"], 0)

    def test_a_raised_run_is_never_live_behind_the_leads_back(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Peer", "at": [0, 0], "intent": "t"}, PEER)["ids"][0]
        self.ok({"op": "settings", "human_edits": "live"}, LEAD)
        self.assertEqual(self.proposed({"op": "move", "id": box, "by": [20, 0], "intent": "t"})["reason"], "peer")

    def test_the_gate_combination(self):
        change = K.Change("E-1", "update", {"author_kind": "human", "x": 0, "y": 0}, {"author_kind": "human", "x": 20, "y": 0}, True)
        stale = K.StaleChange("E-1", "human", "human", 9, ("moved by c+1r0",))
        review = K.Review(author=MEMBER, lead=False, op="move", raised=False, changes=(change,), settings=dict(K.SETTINGS_DEFAULTS),
                          freezes=(), locks=(), lanes=K.Lanes([], {}), territory=K.Territory([]), human=None, stale=(stale,), base=4)
        verdict = K.gate(review)
        self.assertEqual((verdict.outcome, verdict.reason, verdict.details.get("base_note")), ("propose", "human_made", True))
        alone = K.Review(**dict(review.__dict__, changes=(K.Change("E-1", "update", {"author_kind": "member", "author": "alpha-member"},
                                                                     {"author_kind": "member", "author": "alpha-member"}, True),)))
        self.assertEqual((K.gate(alone).outcome, K.gate(alone).code), ("refuse", "stale_base"))

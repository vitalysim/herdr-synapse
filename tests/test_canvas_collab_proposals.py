"""Proposals (canvas v2 phase 5, 4): divert, reserved ids, superseding, limits, ``in_proposal``, accept, reject, withdraw,
rates, the ``proposed`` result and text, and the collaboration fixtures the page reads."""
from __future__ import annotations

import json
from unittest import mock

from collab_support import DEPUTY, LEAD, MANAGER, MEMBER, PEER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_collab as K
from herdr_team import features as F


class Divert(CollabRig):
    def setUp(self):
        super().setUp()
        self.box = self.ok({"op": "shape", "kind": "box", "text": "Pricing", "at": [0, 0]}, LEAD)["ids"][0]

    def test_a_proposal_keeps_nothing_on_the_canvas_and_records_the_after_values(self):
        version = C.current_version(self.team)
        result = self.apply([{"op": "move", "id": self.box, "by": [40, 0], "intent": "align it"}])
        self.assertEqual((result["applied"], result["refused"]), ([], []))
        found = result["proposed"][0]
        self.assertEqual({k: found[k] for k in ("index", "op", "proposal", "reason", "targets", "base_note")},
                         {"index": 0, "op": "move", "proposal": "P-1", "reason": "human_made", "targets": [self.box], "base_note": []})
        self.assertEqual(found["summary"], ['{} "Pricing" (the operator\'s): moved by c+2r0'.format(self.box)])
        self.assertEqual(result["batch"], "B-2", "a proposal is an event of its batch")
        record = self.proposal("P-1")
        self.assertEqual({k: record[k] for k in ("author", "author_kind", "batch", "op", "intent", "status", "targets", "created", "deleted")},
                         {"author": "alpha-member", "author_kind": "member", "batch": "B-2", "op": "move", "intent": "align it", "status": "open",
                          "targets": [self.box], "created": [], "deleted": []})
        change = record["changes"][0]
        self.assertEqual((change["action"], change["id"], change["was"], change["value"]["x"]), ("update", self.box, 1, 40))
        self.assertEqual(self.el(self.box)["x"], 0)
        event = json.loads((F.whiteboard_dir(self.team) / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual((event["op"], event["proposal"], event["ids"], event["seq"]), ("move", "P-1", [], version + 1))
        self.assertEqual([c["target"] for c in event["changes"]], ["home", "author", "proposal"], "its author's first op registers it")

    def test_ids_a_proposal_creates_are_reserved(self):
        found = self.proposed({"op": "shape", "text": "Idea", "at": [20, 20], "intent": "t"})
        self.assertEqual(found["created"], ["E-2"])
        self.ok({"op": "reject", "id": found["proposal"]}, LEAD)
        self.assertEqual(self.ok({"op": "shape", "text": "Next", "at": [900, 900], "intent": "t"})["ids"], ["E-3"], "a gap, never a collision")

    def test_a_new_proposal_on_the_same_targets_supersedes_the_older(self):
        first = self.proposed({"op": "move", "id": self.box, "by": [40, 0], "intent": "t"})["proposal"]
        second = self.proposed({"op": "move", "id": self.box, "by": [80, 0], "intent": "t"})
        self.assertEqual(second["superseded"], [first])
        self.assertEqual((self.proposal(first)["status"], self.proposal(first)["note"]), ("superseded", "superseded by {}".format(second["proposal"])))
        self.assertNotIn("changes", self.proposal(first), "a decided proposal keeps its summary, not its values")
        # Another author's proposal on the same target is its own.
        third = self.proposed({"op": "move", "id": self.box, "by": [0, 40], "intent": "t"}, PEER)
        self.assertNotIn("superseded", third)

    def test_limits(self):
        with mock.patch.object(K, "MAX_OPEN_PROPOSALS_PER_AUTHOR", 1):
            self.proposed({"op": "move", "id": self.box, "by": [40, 0], "intent": "t"})
            other = self.ok({"op": "shape", "text": "Terms", "at": [0, 400]}, LEAD)["ids"][0]
            refusal = self.refused({"op": "move", "id": other, "by": [40, 0], "intent": "t"})
            self.assertEqual(refusal["code"], "proposal_limit")
            self.assertIn("withdraw some or wait for the operator", refusal["message"])
            self.proposed({"op": "move", "id": other, "by": [40, 0], "intent": "t"}, PEER)
        with mock.patch.object(K, "MAX_OPEN_PROPOSALS", 2):
            self.assertEqual(self.refused({"op": "restyle", "id": self.box, "tone": "info", "intent": "t"}, MANAGER)["code"], "proposal_limit")

    def test_what_only_a_proposal_holds_is_refused_in_proposal_later_in_the_batch(self):
        result = self.apply([{"op": "shape", "id": "idea", "text": "Idea", "at": [20, 20], "intent": "t"},
                             {"op": "arrow", "from": "idea", "to": [600, 0], "intent": "t"},
                             {"op": "move", "id": "E-2", "by": [0, 20], "intent": "t"},
                             {"op": "shape", "id": "mine", "text": "Mine", "at": [900, 0], "intent": "t"}])
        self.assertEqual([p["index"] for p in result["proposed"]], [0])
        self.assertEqual([(r["index"], r["code"]) for r in result["refused"]], [(1, "in_proposal"), (2, "in_proposal")])
        self.assertEqual(result["refused"][0]["details"]["proposal"], "P-1")
        self.assertEqual(result["applied"][0]["ids"], ["E-3"])

    def test_atomic_treats_a_proposal_as_success(self):
        result = self.apply([{"op": "move", "id": self.box, "by": [40, 0], "intent": "t"}, {"op": "shape", "text": "x", "at": [900, 0], "intent": "t"}],
                            atomic=True)
        self.assertEqual((len(result["proposed"]), len(result["applied"])), (1, 1))

    def test_a_batch_of_only_proposals_is_not_refused(self):
        self.assertEqual(C.check_applied(self.apply([{"op": "move", "id": self.box, "by": [40, 0], "intent": "t"}]))["proposed"][0]["proposal"], "P-1")

    def test_proposals_count_against_the_rate(self):
        with mock.patch.object(C, "MAX_OPS_PER_MINUTE", 2):
            self.proposed({"op": "move", "id": self.box, "by": [40, 0], "intent": "t"})
            self.proposed({"op": "move", "id": self.box, "by": [60, 0], "intent": "t"})
            with self.assertRaises(C.HerdrTeamError) as ctx:
                self.apply([{"op": "move", "id": self.box, "by": [80, 0], "intent": "t"}])
            self.assertEqual(ctx.exception.code, "canvas_rate")

    def test_the_apply_text(self):
        result = self.apply([{"op": "move", "id": self.box, "by": [40, 0], "intent": "t"}, {"op": "shape", "text": "x", "at": [900, 0], "intent": "t"}])
        text = C.apply_text(result).splitlines()
        self.assertEqual(text[0], "v3 · B-2 · applied 1, proposed 1, refused 0")
        self.assertEqual(text[1], "#0 move → proposal P-1: {} is the operator's; it waits for the operator (look shows the outcome)".format(self.box))
        self.assertEqual(text[2], '   {} "Pricing" (the operator\'s): moved by c+2r0'.format(self.box))
        self.assertTrue(text[3].startswith("#1 shape E-2"))


class Decide(CollabRig):
    def setUp(self):
        super().setUp()
        self.box = self.ok({"op": "shape", "kind": "box", "text": "Pricing", "at": [0, 0]}, LEAD)["ids"][0]
        self.ok({"op": "comment", "at": self.box, "text": "check this"}, LEAD)

    def test_accept_replays_exactly_what_was_proposed(self):
        found = self.proposed({"op": "move", "id": self.box, "by": [40, 0], "intent": "align"})
        shown = self.proposal(found["proposal"])["changes"][0]["value"]
        result = self.apply([{"op": "accept", "id": found["proposal"], "note": "good"}], LEAD)
        box = self.el(self.box)
        self.assertEqual({k: box[k] for k in ("x", "y", "w", "h", "text", "style")}, {k: shown[k] for k in ("x", "y", "w", "h", "text", "style")})
        self.assertEqual((box["author"], box["updated_seq"]), ("human", result["version"]), "an updated element keeps its author")
        record = self.proposal(found["proposal"])
        self.assertEqual({k: record[k] for k in ("status", "decided_by", "decided_seq", "note")},
                         {"status": "accepted", "decided_by": "human", "decided_seq": result["version"], "note": "good"})
        self.assertEqual(self.el("C-1")["point"], [200, 0], "the comment follows the accepted move")
        event = json.loads((F.whiteboard_dir(self.team) / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["accepts"], found["proposal"])
        # Undoing the accept batch is how the lead takes it back.
        self.ok({"op": "undo", "batch": result["batch"]}, LEAD)
        self.assertEqual(self.el(self.box)["x"], 0)

    def test_accepted_new_elements_keep_their_proposer(self):
        found = self.proposed({"op": "shape", "id": "idea", "text": "Idea", "at": [20, 20], "intent": "t"})
        self.ok({"op": "accept", "id": found["proposal"]}, LEAD)
        created = self.el(found["created"][0])
        self.assertEqual((created["author"], created["accepted"], created["alias"]), ("alpha-member", found["proposal"], "idea"))
        self.assertEqual(self.ok({"op": "move", "id": "idea", "by": [0, 20], "intent": "t"})["ids"][0], created["id"], "the proposer's alias works")

    def test_an_outdated_proposal_cannot_be_accepted(self):
        found = self.proposed({"op": "move", "id": self.box, "by": [40, 0], "intent": "t"})
        self.ok({"op": "edit", "id": self.box, "text": "Plans"}, LEAD)
        refusal = self.refused({"op": "accept", "id": found["proposal"]}, LEAD)
        self.assertEqual(refusal["code"], "proposal_outdated")
        self.assertEqual(refusal["details"]["targets"][0]["id"], self.box)
        look = C.look(self.layout, self.team, "alpha-member")
        self.assertIn("{} by you: move {} (the operator's), outdated".format(found["proposal"], self.box), look["text"])
        self.assertTrue(look["proposals"][0]["outdated"])
        entry = next(e for e in C.display(self.team)["entries"] if e["id"] == found["proposal"])
        self.assertTrue(entry["proposal"]["outdated"])
        self.assertTrue(entry["items"][-1]["items"][1]["lines"][0]["t"].endswith("(outdated)"))
        # Deleted targets and a new element's lost container outdate it too.
        self.assertTrue(K.outdated_of({"changes": [{"action": "delete", "id": "E-99", "was": 1}]}, {}))
        self.assertTrue(K.outdated_of({"changes": [{"action": "add", "id": "E-5", "value": {"frame": "E-98"}}]}, {}))

    def test_reject_and_withdraw(self):
        first = self.proposed({"op": "move", "id": self.box, "by": [40, 0], "intent": "t"})["proposal"]
        self.ok({"op": "reject", "id": first, "note": "keep it red"}, LEAD)
        self.assertEqual((self.proposal(first)["status"], self.proposal(first)["note"]), ("rejected", "keep it red"))
        self.assertEqual(self.el(self.box)["x"], 0)
        self.assertEqual(self.refused({"op": "reject", "id": first}, LEAD)["code"], "op_invalid", "only open proposals are decided")
        second = self.proposed({"op": "move", "id": self.box, "by": [80, 0], "intent": "t"})["proposal"]
        self.assertEqual(self.refused({"op": "withdraw", "id": second, "intent": "t"}, PEER)["code"], "element_not_yours")
        self.ok({"op": "withdraw", "id": second, "intent": "not needed"})
        self.assertEqual(self.proposal(second)["status"], "withdrawn")
        third = self.proposed({"op": "move", "id": self.box, "by": [0, 80], "intent": "t"}, PEER)["proposal"]
        self.ok({"op": "withdraw", "id": third}, LEAD)
        self.assertEqual(self.refused({"op": "accept", "id": "P-99"}, LEAD)["code"], "element_unknown")
        fourth = self.proposed({"op": "move", "id": self.box, "by": [0, 120], "intent": "t"}, PEER)["proposal"]
        self.assertEqual(self.refused({"op": "reject", "id": fourth, "note": "AKIA" + "Q" * 16}, LEAD)["code"], "secret_detected")

    def test_the_proposer_reads_the_outcome_once(self):
        pid = self.proposed({"op": "move", "id": self.box, "by": [40, 0], "intent": "t"})["proposal"]
        C.look(self.layout, self.team, "alpha-member")
        self.ok({"op": "reject", "id": pid, "note": "keep it"}, LEAD)
        first = C.look(self.layout, self.team, "alpha-member")
        self.assertEqual([p["id"] for p in first["decided"]], [pid])
        self.assertIn('decided: {} rejected by the operator: "keep it"'.format(pid), first["text"])
        self.assertEqual(C.look(self.layout, self.team, "alpha-member")["decided"], [], "once read, it is not repeated")
        self.assertEqual(C.look(self.layout, self.team, "alpha-peer")["decided"], [], "only the proposer's")

    def test_decided_proposals_are_capped(self):
        with mock.patch.object(K, "MAX_DECIDED_KEPT", 2):
            for n in range(4):
                pid = self.proposed({"op": "move", "id": self.box, "by": [20 * (n + 1), 0], "intent": "t"})["proposal"]
                self.ok({"op": "reject", "id": pid}, LEAD)
            state = C._load_state(self.team)
            state2 = C._State(self.team.name)
            for event in C._read_events(self.team):
                state2.fold(event)
            self.assertEqual(len(state2.proposals), 2)
        self.assertLessEqual(len([p for p in state.proposals.values() if p["status"] != "open"]), K.SCENE_DECIDED)


class Headline(CollabRig):
    def test_the_headline_names_every_targets_owner(self):
        # QA phase 5 L5: moving your frame with her box in it is "(yours and the operator's)", not "(yours)".
        frame = self.ok({"op": "frame", "title": "Mine", "at": [0, 0], "w": 600, "h": 400, "intent": "t"})["ids"][0]
        box = self.ok({"op": "shape", "kind": "box", "text": "hers", "inside": frame}, LEAD)["ids"][0]
        found = self.proposed({"op": "move", "id": frame, "by": [40, 0], "intent": "t"})
        self.assertEqual(set(found["targets"]), {frame, box})
        record = next(p for p in self.scene()["proposals"] if p["id"] == found["proposal"])
        elements = {el["id"]: el for el in self.scene()["elements"]}
        self.assertIn("(yours and the operator's)", K.proposal_line(record, MEMBER.name, elements))
        self.assertIn("(alpha-member's and yours)", K.proposal_line(record, "human", elements), "her own look: yours (QA phase 5 verdict)")


class Ghosts(CollabRig):
    def test_a_ghost_entry_draws_the_proposed_elements_a_line_an_outline_and_a_pill(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Pricing", "at": [0, 0]}, LEAD)["ids"][0]
        gone = self.ok({"op": "shape", "kind": "box", "text": "Old", "at": [0, 400]}, LEAD)["ids"][0]
        moved = self.proposed({"op": "move", "id": box, "by": [0, 200], "intent": "line it up"})["proposal"]
        deleted = self.proposed({"op": "delete", "id": gone, "intent": "not needed"}, PEER)["proposal"]
        doc = C.display(self.team)
        self.assertEqual(C._display.validate(doc), [])
        by_id = {e["id"]: e for e in doc["entries"]}
        ghost = by_id[moved]
        self.assertEqual((ghost["kind"], ghost["layer"], ghost["hit"]["shape"], ghost["handles"], ghost["author"]),
                         ("proposal", "overlays", "rect", "none", "alpha-member"))
        self.assertEqual(ghost["chip"]["initials"], "AL")
        kinds = [item["k"] for item in ghost["items"]]
        self.assertEqual(kinds[-3:], ["line", "rect", "group"], "the move line, the outline, the pill")
        self.assertTrue(all(item["op"] <= 0.5 for item in ghost["items"][:-3]), "the proposed element at half opacity")
        self.assertEqual(ghost["items"][-2]["stroke"], "tone.accent.stroke")
        self.assertEqual(by_id[box]["pending"], [moved])
        self.assertEqual(by_id[deleted]["items"][0]["stroke"], "tone.danger.stroke")
        self.assertEqual(by_id[deleted]["proposal"]["deleted"], [gone])
        # A decided proposal leaves the list; its entry is a removal in a delta.
        version = doc["version"]
        self.ok({"op": "reject", "id": moved}, LEAD)
        delta = C.display_delta(self.team, version)
        self.assertNotIn(moved, [e["id"] for e in (delta.get("upserts") or delta.get("entries"))])
        # An agent's picture draws the ghost too: it is a display-list entry.
        from herdr_team import canvas_svg

        self.assertIn('data-id="{}"'.format(deleted), canvas_svg.write(C.display(self.team), theme="light"))

    def test_an_in_place_ghost_is_drawn_beside_its_host_not_on_top_of_it(self):
        """QA phase 6 F3: a rewrite that does not move the mark used to ghost it at the host's own origin, so the
        ghost's words and the host's ran through each other and the two author chips sat on top of each other."""
        card = self.ok({"op": "card", "id": "refund", "title": "Refund policy", "body": "- store credit after 30 days",
                        "at": [0, 0]}, LEAD)["ids"][0]
        pid = self.proposed({"op": "edit", "id": card, "part": "body",
                             "text": "- store credit after 45 days\n- damaged goods refunded in full",
                             "intent": "as the operator asked"})["proposal"]
        doc = C.display(self.team)
        self.assertEqual(C._display.validate(doc), [])
        by_id = {e["id"]: e for e in doc["entries"]}
        host, ghost = by_id[card], by_id[pid]
        leader, outline = ghost["items"][-3], ghost["items"][-2]
        self.assertEqual((leader["k"], outline["k"]), ("line", "rect"), "the leader to the host, then the outline")
        self.assertGreaterEqual(outline["x"], host["bbox"][2], "the ghost's outline clears the host")
        self.assertEqual(leader["points"][0][0], C._r2(host["bbox"][2]), "the leader starts at the host's right edge")
        self.assertEqual(leader["points"][1][0], C._r2(outline["x"] + 6), "and ends at the ghost's left edge")
        self.assertIn("body ", "; ".join(ghost["proposal"]["summary"]), "and the card says what the words become")

    def test_a_ghost_that_moves_its_host_still_draws_where_the_change_lands(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Pricing", "at": [0, 0]}, LEAD)["ids"][0]
        pid = self.proposed({"op": "move", "id": box, "by": [0, 200], "intent": "t"})["proposal"]
        ghost = {e["id"]: e for e in C.display(self.team)["entries"]}[pid]
        self.assertEqual(ghost["items"][-2]["y"], 200 - 6, "the outline sits where the mark would land")

    def test_the_delta_redraws_a_proposal_whose_target_changed(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Pricing", "at": [0, 0]}, LEAD)["ids"][0]
        pid = self.proposed({"op": "move", "id": box, "by": [0, 200], "intent": "t"})["proposal"]
        before = C.display(self.team)
        self.ok({"op": "edit", "id": box, "text": "Plans"}, LEAD)
        delta = C.display_delta(self.team, before["version"])
        self.assertFalse(delta["full"])
        upserts = {e["id"]: e for e in delta["upserts"]}
        self.assertTrue(upserts[pid]["proposal"]["outdated"])


class Fixtures(CollabRig):
    def test_the_page_fixtures_are_current(self):
        stale = [str(path) for path, text in K.fixture_texts().items() if not path.is_file() or path.read_text(encoding="utf-8") != text]
        self.assertEqual(stale, [], "python3 -m herdr_team.canvas_collab --write-fixtures")

    def test_each_result_fixture_shows_its_case(self):
        results = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in (K.FIXTURES_DIR / "results").glob("*.json")}
        for code in ("stale_base", "element_busy", "frozen", "in_proposal", "proposal_outdated", "proposal_limit", "operator_only"):
            name = code if code + ".json" in {p + ".json" for p in results} else code + "_refused"
            self.assertIn(code, [r["code"] for r in results[name]["refused"]], name)
        self.assertEqual(results["proposed"]["proposed"][0]["reason"], "human_made")
        self.assertEqual(results["auto_claim"]["applied"][0]["auto_claim"], "K-2")
        self.assertEqual(results["stale_base_warning"]["warnings"][0]["code"], "stale_base")
        self.assertIn("pointing_at", results["operator"]["operator"])
        self.assertTrue(results["undo"]["applied"][0]["undo"]["skipped"])
        self.assertIn("restore", results["restore"]["applied"][0])

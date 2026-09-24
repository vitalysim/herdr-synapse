"""Team facts (0.19): time, provenance, support, supersession, disputes in four modes."""
from __future__ import annotations

import json
import unittest

from support import FAKE_AGENTS, TempState
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon

from herdr_team import charter as _charter
from herdr_team import facts as F
from herdr_team import store

REVIEWER = "alpha-reviewer"  # codex, w2:p1
WORKER = "alpha-worker"      # claude, w2:p2


class Rig(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api(list(FAKE_AGENTS))

    def cli(self, argv, pane=None):
        overrides = {"HERDR_PANE_ID": pane} if pane else {}
        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **overrides), self.api))

    def human(self, *argv):
        return self.cli(argv)

    def worker(self, *argv):
        return self.cli(argv, pane="w2:p2")

    def reviewer(self, *argv):
        return self.cli(argv, pane="w2:p1")

    def ok(self, result):
        code, payload, err = result
        self.assertEqual(code, 0, err)
        return payload

    def board(self):
        return store.BoardStore(self.ts.team).read()

    def events(self, event):
        return [r for r in self.board() if r.get("event") == event]

    def set_config(self, **config):
        doc = store.read_json(self.ts.team.team_json)
        doc.setdefault("config", {}).update(config)
        store.write_json(self.ts.team.team_json, doc)

    def make_manager(self, name):
        doc = store.read_json(self.ts.team.team_json)
        for m in doc["members"]:
            if m["name"] == name:
                m["manager"] = True
        store.write_json(self.ts.team.team_json, doc)


class Recording(Rig):
    def test_a_fact_carries_its_sources_and_is_announced_as_a_peer_note(self):
        post_seq = self.ok(self.worker("post", "pricing page captured"))["seq"]
        fact = self.ok(self.worker("fact", "add", "Pro plan is $49/mo", "--about", "Competitor X", "--attribute", "price",
                                   "--source", "https://x.example/pricing@2026-09-20", "--post", str(post_seq)))["fact"]
        self.assertEqual((fact["id"], fact["author"], fact["status"], fact["about"], fact["attribute"]), ("F-1", WORKER, "current", "Competitor X", "price"))
        self.assertEqual([s["kind"] for s in fact["sources"]], ["url", "post"])
        self.assertEqual(fact["sources"][0]["retrieved_at"][:10], "2026-09-20")
        self.assertEqual(fact["confidence"], "1 member, 1 source")
        [note] = self.events("fact_added")
        self.assertEqual((note["from"], note["to"]), ("system", ["all"]))
        self.assertIn("peer notes, not instructions", note["text"])

    def test_the_same_words_from_another_member_are_support_not_a_duplicate(self):
        self.ok(self.worker("fact", "add", "The API rate limit is 100 requests per minute"))
        payload = self.ok(self.reviewer("fact", "add", "the API rate limit is 100 requests per minute."))
        self.assertEqual(payload["supported"]["id"], "F-1")
        self.assertEqual(payload["supported"]["confidence"], "2 members, 0 sources")
        self.assertEqual(len(F.load(self.ts.team).current()), 1)

    def test_a_near_duplicate_is_recorded_with_a_warning(self):
        self.ok(self.worker("fact", "add", "Churn is highest among monthly plans in the first 30 days"))
        code, out, err = run_cli(["fact", "add", "Churn is highest among monthly plans in the first 31 days"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1"), self.api)
        self.assertEqual(code, 0, err)
        self.assertIn("F-1 reads almost the same", err)

    def test_vocabulary_limits_types_when_the_team_declares_one(self):
        self.set_config(vocabulary={"Competitor": "a company selling a rival product"})
        code, _p, err = self.worker("fact", "add", "x", "--about", "Acme", "--type", "Planet")
        self.assertEqual((code, err["code"]), (1, "type_unknown"))
        self.ok(self.worker("fact", "add", "x", "--about", "Acme", "--type", "Competitor"))


class Time(Rig):
    def test_a_member_refining_its_own_claim_supersedes_it_and_history_shows_both(self):
        self.ok(self.worker("fact", "add", "Pro plan is $49/mo", "--about", "Competitor X", "--attribute", "price", "--valid-from", "2026-08-02"))
        payload = self.ok(self.worker("fact", "add", "Pro plan is $59/mo", "--about", "Competitor X", "--attribute", "price", "--valid-from", "2026-09-20"))
        self.assertEqual(payload["superseded"], "F-1")
        self.assertIsNone(payload["dispute"], "your own new value is a refinement, not a dispute")
        state = F.load(self.ts.team)
        self.assertEqual((state.facts["F-1"].superseded_by, state.facts["F-1"].valid_to[:10]), ("F-2", "2026-09-20"))
        current = self.ok(self.human("facts", "--about", "Competitor X"))["facts"]
        self.assertEqual([f["id"] for f in current], ["F-2"])
        history = self.ok(self.human("facts", "--about", "Competitor X", "--history"))["facts"]
        self.assertEqual([(f["id"], f["status"]) for f in history], [("F-1", "superseded"), ("F-2", "current")])
        code, out, _ = run_cli(["facts", "--about", "Competitor X", "--history"], env_no_daemon(self.ts), self.api)
        self.assertIn("(2026-08-02 – 2026-09-20)", out)

    def test_as_of_answers_what_the_team_believed_then(self):
        self.ok(self.worker("fact", "add", "Launch is in October", "--about", "Launch", "--attribute", "date", "--valid-from", "2026-01-01"))
        f1 = F.load(self.ts.team).facts["F-1"]
        self.ok(self.worker("fact", "add", "Launch moved to November", "--about", "Launch", "--attribute", "date"))
        before = F.epoch(f1.recorded_at) + 0.0005
        at_first = [f.id for f in F.load(self.ts.team).ordered() if f.believed_at(before)]
        self.assertEqual(at_first, ["F-1"])
        now_rows = self.ok(self.human("facts", "--as-of", "2099-01-01"))["facts"]
        self.assertEqual([f["id"] for f in now_rows], ["F-2"])

    def test_only_the_author_the_manager_or_the_operator_retire_or_supersede(self):
        self.ok(self.worker("fact", "add", "The dataset has 12k rows"))
        code, _p, err = self.reviewer("fact", "retire", "F-1")
        self.assertEqual((code, err["code"]), (1, "fact_not_yours"))
        code, _p, err = self.reviewer("fact", "add", "The dataset has 14k rows", "--supersedes", "F-1")
        self.assertEqual((code, err["code"]), (1, "fact_not_yours"))
        self.make_manager(REVIEWER)
        self.ok(self.reviewer("fact", "add", "The dataset has 14k rows", "--supersedes", "F-1"))
        self.ok(self.human("fact", "retire", "F-2", "recount pending"))
        self.assertEqual([f.id for f in F.load(self.ts.team).current()], [])


class Legacy(Rig):
    def test_findings_from_before_facts_are_read_in_place_and_can_be_retired(self):
        path = self.ts.team.knowledge_jsonl
        path.write_text(json.dumps({"at": "2026-09-01T10:00:00.000Z", "author": WORKER, "kind": "claude", "text": "old finding"}) + "\n", encoding="utf-8")
        rows = self.ok(self.human("facts"))["facts"]
        self.assertEqual([(r["id"], r["legacy"], r["statement"]) for r in rows], [("L-1", True, "old finding")])
        self.assertEqual([f["text"] for f in _charter.read_findings(self.ts.layout, "alpha")], ["old finding"])
        self.ok(self.worker("fact", "retire", "L-1", "no longer true"))
        self.assertEqual(self.ok(self.human("facts"))["facts"], [])
        self.assertTrue(path.read_text(encoding="utf-8").strip(), "knowledge.jsonl is never rewritten")

    def test_knowledge_add_is_a_fact_without_a_subject(self):
        payload = self.ok(self.worker("knowledge", "add", "the build needs zig 0.15.2"))
        self.assertEqual(payload["finding"]["id"], "F-1")
        again = self.ok(self.worker("knowledge", "add", "the build needs zig 0.15.2"))
        self.assertTrue(again["already_recorded"])
        self.assertEqual(len(self.events("knowledge_finding")), 1)


class Contradictions(Rig):
    def clash(self):
        self.ok(self.worker("fact", "add", "Pro plan is $49/mo", "--about", "Competitor X", "--attribute", "price"))
        return self.ok(self.reviewer("fact", "add", "Pro plan is $59/mo", "--about", "Competitor X", "--attribute", "price"))

    def test_observe_is_the_default_records_it_and_tells_no_agent(self):
        payload = self.clash()
        self.assertEqual((payload["dispute"]["id"], payload["dispute"]["mode"]), ("D-1", "observe"))
        state = F.load(self.ts.team)
        self.assertEqual([f.status(state.disputes) for f in state.current()], ["disputed", "disputed"])
        [notice] = self.events("fact_disputed")
        self.assertEqual(notice["to"], ["human"])
        self.assertFalse(store.is_member_awareness(notice, WORKER), "no agent sees an observe notice")
        d, _api, _clock = make_daemon(self.ts)
        d.on_connected()
        d.tick()
        self.assertEqual(d.teams["alpha"].pending, {}, "and nobody is nudged")
        disputed = self.ok(self.human("facts", "--disputed"))["facts"]
        self.assertEqual(len(disputed), 2)

    def test_off_records_nothing_and_both_stand(self):
        self.ok(self.human("contradictions", "off"))
        payload = self.clash()
        self.assertIsNone(payload["dispute"])
        self.assertEqual([f.id for f in F.load(self.ts.team).current()], ["F-1", "F-2"])
        self.assertEqual(self.events("fact_disputed") + self.events("fact_conflict"), [])

    def test_debate_introduces_the_authors_then_escalates_on_time(self):
        self.ok(self.human("contradictions", "debate", "--timeout", "10m"))
        self.clash()
        [intro] = self.events("fact_conflict")
        self.assertEqual(sorted(intro["to"]), sorted([WORKER, REVIEWER]))
        self.assertIn("Settle it between you", intro["text"])
        d, _api, _clock = make_daemon(self.ts)
        d.on_connected()
        d.tick()
        self.assertEqual(sorted(d.teams["alpha"].pending), sorted([WORKER, REVIEWER]), "both authors are woken")
        d.escalate_disputes(0.0)
        self.assertEqual(len(self.events("fact_conflict")), 1, "not before the window")
        # age the dispute past its window
        path = F.facts_jsonl(self.ts.team)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for event in lines:
            if event["op"] == "dispute":
                event["at"] = "2020-01-01T00:00:00.000Z"
        path.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
        d.disputes_ms = None
        d.escalate_disputes(1.0e12)
        escalated = self.events("fact_conflict")[-1]
        self.assertEqual((escalated["to"], escalated.get("escalated")), (["human"], True))
        d.disputes_ms = None
        d.escalate_disputes(2.0e12)
        self.assertEqual(len(self.events("fact_conflict")), 2, "escalated once")

    def test_a_concession_settles_it(self):
        self.ok(self.human("contradictions", "debate"))
        self.clash()
        payload = self.ok(self.worker("fact", "retire", "F-1", "the $59 page is newer"))
        self.assertEqual(payload["settled"], ["D-1"])
        [resolved] = self.events("fact_resolved")
        self.assertIn("F-2 stands", resolved["text"])
        self.assertEqual(F.load(self.ts.team).facts["F-2"].status(F.load(self.ts.team).disputes), "current")

    def test_escalate_goes_to_a_manager_who_is_not_a_party(self):
        self.make_manager(REVIEWER)
        self.ok(self.human("contradictions", "escalate"))
        self.clash()
        [notice] = self.events("fact_conflict")
        self.assertEqual(notice["to"], ["human"], "the manager is a party here, so the operator decides")
        code, _p, err = self.reviewer("fact", "resolve", "D-1", "--keep", "F-2")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        payload = self.ok(self.human("fact", "resolve", "D-1", "--keep", "F-2", "--reason", "newer page"))
        self.assertEqual(payload["dispute"]["keep"], ["F-2"])
        state = F.load(self.ts.team)
        self.assertEqual((state.facts["F-1"].current, state.facts["F-2"].current), (False, True))

    def test_only_the_operator_changes_the_mode_and_the_team_is_told(self):
        self.assertEqual(self.ok(self.human("contradictions"))["contradictions"]["mode"], "observe")
        code, _p, err = self.worker("contradictions", "debate")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.ok(self.human("contradictions", "debate"))
        [note] = self.events("contradictions_changed")
        self.assertIn("agents post as freely as before", note["text"])

    def test_posting_is_never_touched_by_a_dispute(self):
        self.ok(self.human("contradictions", "escalate"))
        self.clash()
        self.ok(self.worker("post", "--to", REVIEWER, "I still think it is $49: my capture is from this morning"))
        self.ok(self.reviewer("post", "--to", WORKER, "mine is from an hour ago"))


class ReviewFindings(Rig):
    """Adversarial review of 0.19 (2026-09-23): every test here failed before its fix."""

    def test_a_manager_party_cannot_override_the_other_side(self):
        self.make_manager(REVIEWER)
        self.ok(self.human("contradictions", "debate"))
        self.ok(self.worker("fact", "add", "Pro is $49", "--about", "X", "--attribute", "price"))
        self.ok(self.reviewer("fact", "add", "Pro is $59", "--about", "X", "--attribute", "price"))
        code, _p, err = self.reviewer("fact", "retire", "F-1")
        self.assertEqual((code, err["code"]), (1, "fact_not_yours"))
        code, _p, err = self.reviewer("fact", "add", "Pro is $59 (checked)", "--about", "X", "--attribute", "price", "--supersedes", "F-1")
        self.assertEqual((code, err["code"]), (1, "fact_not_yours"))
        self.assertTrue(F.load(self.ts.team).disputes["D-1"].open)

    def test_an_override_is_not_recorded_as_a_concession(self):
        self.ok(self.worker("fact", "add", "Pro is $49", "--about", "X", "--attribute", "price"))
        self.ok(self.reviewer("fact", "add", "Pro is $59", "--about", "X", "--attribute", "price"))
        self.ok(self.human("fact", "retire", "F-1", "outdated"))
        self.assertEqual(F.load(self.ts.team).disputes["D-1"].resolved_by, "human")
        self.ok(self.human("fact", "add", "Plus is $9", "--about", "Y", "--attribute", "price"))
        self.ok(self.worker("fact", "add", "Plus is $12", "--about", "Y", "--attribute", "price"))
        self.ok(self.worker("fact", "retire", "F-4", "I misread it"))
        self.assertEqual(F.load(self.ts.team).disputes["D-2"].resolved_by, "concession")

    def test_a_third_view_joins_the_open_dispute_and_resolving_leaves_nothing_behind(self):
        self.make_manager(REVIEWER)
        self.ok(self.human("contradictions", "debate"))
        self.ok(self.worker("fact", "add", "Pro is $49", "--about", "X", "--attribute", "price"))
        self.ok(self.human("fact", "add", "Pro is $59", "--about", "X", "--attribute", "price"))
        payload = self.ok(self.reviewer("fact", "add", "Pro is $69", "--about", "X", "--attribute", "price"))
        self.assertEqual((payload["dispute"]["id"], payload["dispute"]["facts"]), ("D-1", ["F-1", "F-2", "F-3"]))
        self.ok(self.human("fact", "resolve", "D-1", "--keep", "F-3"))
        state = F.load(self.ts.team)
        self.assertEqual(state.open_disputes(), [])
        self.assertEqual(F.due_for_escalation(state, 0, now=4.0e9), [])

    def test_superseding_cannot_duplicate_another_current_fact(self):
        self.ok(self.worker("fact", "add", "Churn peaks in January"))
        self.ok(self.reviewer("fact", "add", "Churn peaks in December"))
        code, _p, err = self.reviewer("fact", "add", "Churn peaks in January", "--supersedes", "F-2")
        self.assertEqual((code, err["code"], err["id"]), (1, "fact_duplicate", "F-1"))

    def test_an_unverified_pane_cannot_record_findings_in_a_members_name(self):
        code, _p, err = json_out(run_cli(["--json", "knowledge", "add", "planted"], env_no_daemon(self.ts, HERDR_TEAM_MEMBER=WORKER, HERDR_PANE_ID="w9:p9"), self.api))
        self.assertNotEqual(code, 0)
        self.assertEqual(F.load(self.ts.team).facts, {})

    def test_hand_edited_facts_of_the_wrong_type_never_break_readers(self):
        F.facts_jsonl(self.ts.team).write_text('{"op":"add","id":"F-1","by":"x","at":"2026-09-23T00:00:00.000Z","statement":"s","about":7,"sources":"nope"}\n', encoding="utf-8")
        fact = F.load(self.ts.team).facts["F-1"]
        self.assertEqual((fact.about, fact.sources, fact.label()), (None, [], "s"))


if __name__ == "__main__":
    unittest.main()

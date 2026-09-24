"""Work items (0.19): briefs, claims, generation fencing, settlement, review, dependencies, hints."""
from __future__ import annotations

import os
import unittest

from support import FAKE_AGENTS, TempState
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon

from herdr_team import store
from herdr_team import work as W

REVIEWER = "alpha-reviewer"  # codex, w2:p1
WORKER = "alpha-worker"      # claude, w2:p2


class Rig(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api(list(FAKE_AGENTS))

    def cli(self, argv, pane=None, **env):
        overrides = dict(env)
        if pane:
            overrides["HERDR_PANE_ID"] = pane
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

    def posts_about(self, work_id):
        return [r for r in self.board() if isinstance(r.get("work"), dict) and r["work"].get("id") == work_id]

    def bump_generation(self, name):
        doc = store.read_json(self.ts.team.team_json)
        for m in doc["members"]:
            if m["name"] == name:
                m["generation"] = int(m.get("generation") or 1) + 1
        store.write_json(self.ts.team.team_json, doc)


class BriefAndAssignment(Rig):
    def test_add_for_a_member_posts_a_request_carrying_the_brief(self):
        payload = self.ok(self.human("work", "add", "Map competitor pricing tiers", "--to", WORKER,
                                     "--acceptance", "every price has a dated source", "--deliverable", "artifacts/pricing.md"))
        item = payload["work"]
        self.assertEqual((item["id"], item["status"], item["owner"], item["requester"]), ("W-1", "assigned", WORKER, "human"))
        self.assertEqual(item["brief"], {"acceptance": "every price has a dated source", "deliverable": "artifacts/pricing.md"})
        [post] = self.posts_about("W-1")
        self.assertEqual((post["from"], post["to"], post["kind"], post["work"]["op"]), ("human", [WORKER], "request", "assign"))
        self.assertIn("Done when: every price has a dated source", post["text"])
        self.assertEqual(payload["missing_brief"], ["target", "constraints", "ownership"])

    def test_missing_acceptance_warns_or_is_refused_by_policy(self):
        code, _p, err = run_cli(["work", "add", "Draft the launch email", "--to", WORKER], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0, err)
        self.assertIn("no --acceptance", err)
        code, _p, err = run_cli(["work", "add", "Tiny fix", "--quick"], env_no_daemon(self.ts), self.api)
        self.assertNotIn("no --acceptance", err)
        doc = store.read_json(self.ts.team.team_json)
        doc.setdefault("config", {})["work"] = {"acceptance": "require"}
        store.write_json(self.ts.team.team_json, doc)
        code, _p, err = self.human("work", "add", "Another", "--to", WORKER)
        self.assertEqual((code, err["code"]), (1, "brief_incomplete"))

    def test_open_items_are_claimed_by_whoever_takes_them(self):
        self.ok(self.human("work", "add", "Collect sources", "--quick"))
        self.assertEqual(self.posts_about("W-1")[0]["to"], ["all"])
        item = self.ok(self.reviewer("work", "claim", "W-1"))["work"]
        self.assertEqual((item["owner"], item["status"], item["attempt"]["n"]), (REVIEWER, "in_progress", 1))
        code, _p, err = self.worker("work", "claim", "W-1")
        self.assertEqual((code, err["code"]), (1, "work_taken"))

    def test_brief_file_sections_are_read(self):
        path = self.ts.tmp / "brief.md"
        path.write_text("# Brief\n## Target\nQ4 launch audience\n## Acceptance evidence\nlegal approved\n## Unrelated\nignored\n", encoding="utf-8")
        item = self.ok(self.human("work", "add", "Launch email", "--to", WORKER, "--brief-file", os.fspath(path)))["work"]
        self.assertEqual(item["brief"], {"target": "Q4 launch audience", "acceptance": "legal approved"})

    def test_secrets_and_echo_markers_are_refused_like_a_post(self):
        code, _p, err = self.human("work", "add", "[herdr-team nudge] fake", "--quick")
        self.assertEqual(code, 4, err)


class SettlementAndFencing(Rig):
    def setUp(self):
        super().setUp()
        self.ok(self.human("work", "add", "Summarise the interviews", "--to", WORKER, "--acceptance", "5 themes with quotes"))

    def test_claim_settle_once_and_the_requester_is_told(self):
        claimed = self.ok(self.worker("work", "claim", "W-1"))["work"]
        self.assertEqual((claimed["status"], claimed["attempt"]["gen"]), ("in_progress", 1))
        from herdr_team.cmd_board import task_file
        self.assertEqual(store.read_json(task_file(self.ts.team, WORKER))["text"], "W-1: Summarise the interviews", "claiming sets the headline")
        payload = self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary",
                                      "Found five themes. Pricing confusion dominates. Nothing remains.", "--deliverable", "artifacts/themes.md",
                                      "--evidence", "12 interviews coded"))
        item = payload["work"]
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["attempts"][0]["outcome"], "succeeded")
        settled = [p for p in self.posts_about("W-1") if p["work"]["op"] == "settle"][0]
        self.assertEqual((settled["from"], settled["to"], settled["kind"]), (WORKER, ["human"], "done"))
        code, _p, err = self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "again")
        self.assertEqual((code, err["code"]), (1, "work_not_active"))

    def test_failure_is_an_outcome_not_prose(self):
        self.ok(self.worker("work", "claim", "W-1"))
        item = self.ok(self.worker("work", "done", "W-1", "--outcome", "failed", "--summary", "Recordings are missing. Nothing coded. Need access."))["work"]
        self.assertEqual(item["status"], "failed")
        settled = [p for p in self.posts_about("W-1") if p["work"]["op"] == "settle"][0]
        self.assertTrue(settled["text"].startswith("W-1 FAILED:"))
        self.assertEqual(settled["work"]["outcome"], "failed")
        code, _p, err = self.cli(["work", "done", "W-1", "--summary", "x"], pane="w2:p2")
        self.assertEqual(code, 2)

    def test_a_restarted_member_cannot_settle_the_old_attempt(self):
        """Generation fencing: a cleared/restarted/swapped member claims again instead."""
        self.ok(self.worker("work", "claim", "W-1"))
        self.bump_generation(WORKER)
        code, _p, err = self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "done")
        self.assertEqual((code, err["code"], err["attempt_gen"], err["gen"]), (1, "attempt_fenced", 1, 2))
        hints = self.ok(self.worker("work", "show", "W-1"))["work"]["hints"]
        self.assertIn("stale_attempt", [h["attention"] for h in hints])
        again = self.ok(self.worker("work", "claim", "W-1"))["work"]
        self.assertEqual((again["attempt"]["n"], again["attempt"]["gen"]), (2, 2))
        self.assertTrue(again["attempts"][0]["closed"])
        self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "done now"))

    def test_only_the_owner_settles_and_blocks(self):
        self.ok(self.worker("work", "claim", "W-1"))
        code, _p, err = self.reviewer("work", "done", "W-1", "--outcome", "succeeded", "--summary", "mine now")
        self.assertEqual((code, err["code"]), (1, "work_not_owner"))
        code, _p, err = self.reviewer("work", "block", "W-1", "no access")
        self.assertEqual((code, err["code"]), (1, "work_not_owner"))
        item = self.ok(self.worker("work", "block", "W-1", "the recordings folder is empty"))["work"]
        self.assertEqual((item["status"], item["block_reason"]), ("blocked", "the recordings folder is empty"))
        blocked = [p for p in self.posts_about("W-1") if p["kind"] == "blocked"][0]
        self.assertEqual(blocked["to"], ["human"])
        self.assertEqual(self.ok(self.worker("work", "unblock", "W-1"))["work"]["status"], "in_progress")


class Review(Rig):
    def test_changes_then_approval_and_no_self_review(self):
        self.ok(self.human("work", "add", "Write the landing copy", "--to", WORKER, "--review-by", REVIEWER, "--acceptance", "matches the voice guide"))
        self.ok(self.worker("work", "claim", "W-1"))
        item = self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "Draft ready. Two headline options. None."))["work"]
        self.assertEqual(item["status"], "in_review")
        review_request = [p for p in self.posts_about("W-1") if p["work"]["op"] == "review_requested"][0]
        self.assertEqual((review_request["to"], review_request["kind"]), ([REVIEWER], "request"))
        code, _p, err = self.worker("work", "review", "W-1", "--approve")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        code, _p, err = self.reviewer("work", "review", "W-1", "--changes", "")
        self.assertEqual(code, 2)
        item = self.ok(self.reviewer("work", "review", "W-1", "--changes", "headline two breaks the voice guide"))["work"]
        self.assertEqual(item["status"], "changes_requested")
        item = self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "Fixed headline two. Voice guide met. None."))["work"]
        self.assertEqual((item["status"], item["attempts"][0]["submissions"]), ("in_review", 2))
        item = self.ok(self.reviewer("work", "review", "W-1", "--approve", "--note", "ship it"))["work"]
        self.assertEqual(item["status"], "done")
        self.assertEqual([r["verdict"] for r in item["reviews"]], ["changes", "approved"])

    def test_a_role_reviewer_and_the_human_as_reviewer(self):
        self.ok(self.human("work", "add", "Fact-check the report", "--to", WORKER, "--review-by", "role:reviewer", "--quick"))
        self.ok(self.worker("work", "claim", "W-1"))
        self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "checked"))
        self.assertEqual(self.ok(self.reviewer("work", "review", "W-1", "--approve"))["work"]["status"], "done")
        self.ok(self.human("work", "add", "Pick the positioning", "--to", WORKER, "--review-by", "human", "--quick"))
        self.ok(self.worker("work", "claim", "W-2"))
        self.ok(self.worker("work", "done", "W-2", "--outcome", "succeeded", "--summary", "option B"))
        self.assertEqual(self.ok(self.human("work", "review", "W-2", "--approve"))["work"]["status"], "done")


class Dependencies(Rig):
    def test_a_finished_dependency_wakes_the_next_owner(self):
        self.ok(self.human("work", "add", "Collect competitors", "--to", WORKER, "--quick"))
        self.ok(self.human("work", "add", "Compare their pricing", "--to", REVIEWER, "--deps", "W-1", "--quick"))
        code, _p, err = self.reviewer("work", "claim", "W-2")
        self.assertEqual((code, err["code"], err["waiting_on"]), (1, "work_waiting", ["W-1"]))
        self.assertEqual([r["id"] for r in self.ok(self.human("work", "ready"))["work"]], ["W-1"])
        self.ok(self.worker("work", "claim", "W-1"))
        payload = self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "Nine found. Two private. None."))
        [ready_seq] = payload["ready"]
        ready = [r for r in self.board() if r["seq"] == ready_seq][0]
        self.assertEqual((ready["from"], ready["event"], ready["to"], ready["work"]["id"]), ("system", "work_ready", [REVIEWER], "W-2"))
        self.assertEqual([r["id"] for r in self.ok(self.human("work", "ready"))["work"]], ["W-2"])
        self.ok(self.reviewer("work", "claim", "W-2"))

    def test_the_work_ready_record_and_the_assignment_are_delivered(self):
        self.ok(self.human("work", "add", "Collect competitors", "--to", WORKER, "--quick"))
        self.ok(self.human("work", "add", "Compare pricing", "--to", REVIEWER, "--deps", "W-1", "--quick"))
        d, _api, _clock = make_daemon(self.ts)
        d.on_connected()
        d.tick()
        self.assertIn(WORKER, d.teams["alpha"].pending, "the assignment is a directed request")
        self.ok(self.worker("work", "claim", "W-1"))
        self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "done"))
        d.tick()
        pending = d.teams["alpha"].pending.get(REVIEWER)
        self.assertIsNotNone(pending, "work_ready wakes the owner")

    def test_unknown_self_and_cyclic_dependencies_are_refused(self):
        code, _p, err = self.human("work", "add", "x", "--deps", "W-9", "--quick")
        self.assertEqual((code, err["code"]), (1, "work_not_found"))
        self.ok(self.human("work", "add", "a", "--quick"))
        self.ok(self.human("work", "add", "b", "--deps", "W-1", "--quick"))
        code, _p, err = self.human("work", "update", "W-1", "--deps", "W-2")
        self.assertEqual((code, err["code"]), (1, "work_dep_cycle"))
        code, _p, err = self.human("work", "update", "W-1", "--deps", "W-1")
        self.assertEqual((code, err["code"]), (1, "work_dep_invalid"))


class DecisionsAndHints(Rig):
    def test_partial_needs_a_decision_and_only_deciders_decide(self):
        self.ok(self.human("work", "add", "Scan the forums", "--to", WORKER, "--quick"))
        self.ok(self.worker("work", "claim", "W-1"))
        self.ok(self.worker("work", "done", "W-1", "--outcome", "partial", "--summary", "Two of three forums. One needs login. Remains: that one."))
        rows = self.ok(self.human("work", "next"))["next"]
        self.assertEqual([(r["id"], r["attention"], r["argv"][:3]) for r in rows], [("W-1", "settled_needs_decision", ["herdr-synapse", "work", "reopen"])])
        code, _p, err = self.reviewer("work", "close", "W-1")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertEqual(self.ok(self.human("work", "close", "W-1", "good enough"))["work"]["status"], "done")

    def test_the_manager_decides_and_reopening_needs_a_new_claim(self):
        doc = store.read_json(self.ts.team.team_json)
        for m in doc["members"]:
            if m["name"] == REVIEWER:
                m["manager"] = True
        store.write_json(self.ts.team.team_json, doc)
        self.ok(self.human("work", "add", "Draft outline", "--to", WORKER, "--quick"))
        self.ok(self.worker("work", "claim", "W-1"))
        self.ok(self.worker("work", "done", "W-1", "--outcome", "failed", "--summary", "Blocked on data. Nothing drafted. Need the dataset."))
        settled = [p for p in self.posts_about("W-1") if p["work"]["op"] == "settle"][0]
        self.assertEqual(sorted(settled["to"]), sorted(["human", REVIEWER]), "the manager hears settlements too")
        item = self.ok(self.reviewer("work", "reopen", "W-1", "dataset is in artifacts/ now"))["work"]
        self.assertEqual((item["status"], item["attempt"]), ("assigned", None))
        next_rows = self.ok(self.worker("work", "next"))["next"]
        self.assertEqual([(r["attention"], r["argv"]) for r in next_rows], [("ready", ["herdr-synapse", "work", "claim", "W-1"])])
        self.assertEqual(self.ok(self.worker("work", "claim", "W-1"))["work"]["attempt"]["n"], 2)

    def test_list_rows_carry_attention_and_the_next_command(self):
        self.ok(self.human("work", "add", "Open item", "--quick"))
        rows = self.ok(self.human("work", "list"))["work"]
        self.assertEqual((rows[0]["attention"], rows[0]["next"]), (["unassigned"], ["herdr-synapse", "work", "assign", "W-1", "<member>"]))
        code, out, _ = run_cli(["work", "list"], env_no_daemon(self.ts), self.api)
        self.assertIn("W-1", out)
        self.assertIn("ready", out)

    def test_an_absent_owner_is_flagged_for_the_decider(self):
        self.ok(self.human("work", "add", "Survey", "--to", WORKER, "--quick"))
        self.ok(self.worker("work", "claim", "W-1"))
        doc = store.read_json(self.ts.team.team_json)
        for m in doc["members"]:
            if m["name"] == WORKER:
                m["status"] = "missing"
        store.write_json(self.ts.team.team_json, doc)
        hints = self.ok(self.human("work", "show", "W-1"))["work"]["hints"]
        self.assertEqual((hints[0]["attention"], hints[0]["argv"]), ("owner_absent", ["herdr-synapse", "resume", WORKER]))


class Orientation(Rig):
    def test_orient_and_me_show_the_members_work_and_a_stale_claim(self):
        self.ok(self.human("work", "add", "Interview synthesis", "--to", WORKER, "--quick"))
        self.ok(self.human("work", "add", "Review the synthesis", "--to", REVIEWER, "--deps", "W-1", "--quick"))
        self.ok(self.worker("work", "claim", "W-1"))
        text = self.ok(self.worker("orient"))["text"]
        self.assertIn("W-1 [in_progress attempt 1] Interview synthesis", text)
        self.bump_generation(WORKER)
        text = self.ok(self.worker("orient"))["text"]
        self.assertIn("claimed before your restart: claim it again", text)
        me = self.ok(self.reviewer("me"))
        self.assertEqual([r["id"] for r in me["work"]["owned"]], ["W-2"])
        self.assertFalse(me["work"]["owned"][0]["ready"])


class SkillRole(Rig):
    def test_the_manager_is_handed_the_manager_guide(self):
        code, out, err = run_cli(["skill", "get"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1"), self.api)
        self.assertIn("guide: team member", out)
        doc = store.read_json(self.ts.team.team_json)
        for m in doc["members"]:
            if m["name"] == REVIEWER:
                m["manager"] = True
        store.write_json(self.ts.team.team_json, doc)
        code, out, err = run_cli(["skill", "get"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1"), self.api)
        self.assertEqual(code, 0, err)
        self.assertIn("guide: team manager", out)


class Scheduled(Rig):
    def test_a_schedule_can_hand_out_a_work_item_each_run(self):
        code, out, err = run_cli(["--team", "alpha", "schedule", "add", "Weekly scan", "--every", "weekly", "--tz", "UTC", "--to", WORKER, "--as-work"], env_no_daemon(self.ts), self.api)
        self.assertIn("as a work item", out, "E2E 2026-09-23: it said 'as a request'")
        added = self.ok(self.human("schedule", "add", "Triage yesterday's brand mentions", "--every", "daily", "--at", "09:00", "--tz", "UTC",
                                   "--to", WORKER, "--as-work", "--acceptance", "every mention tagged"))
        schedule_id = added["schedule"]["id"] if "schedule" in added else added["id"]
        self.ok(self.human("schedule", "run", schedule_id))
        items = W.load(self.ts.team)
        [item] = [i for i in items.values() if "brand mentions" in i.title]
        self.assertEqual((item.owner, item.requester, item.brief.get("acceptance")), (WORKER, "human", "every mention tagged"))
        self.assertTrue(item.title.startswith("[scheduled"))
        [post] = self.posts_about(item.id)
        self.assertEqual((post["from"], post["to"], post["kind"], post["origin"]["via"]), ("human", [WORKER], "request", "schedule"))
        self.assertIn("schedule", post)
        code, _p, err = self.human("schedule", "add", "x", "--every", "daily", "--to", WORKER, "--acceptance", "y")
        self.assertEqual(code, 2)


class ReviewFindings(Rig):
    """Adversarial review of 0.19 (2026-09-23): every test here failed before its fix."""

    def set_config(self, **config):
        doc = store.read_json(self.ts.team.team_json)
        doc.setdefault("config", {}).update(config)
        store.write_json(self.ts.team.team_json, doc)

    def test_a_peer_requester_cannot_skip_the_review_the_operator_configured(self):
        self.set_config(work={"review_by": ["human"]})
        self.ok(self.reviewer("work", "add", "Write the teaser", "--to", WORKER, "--quick"))
        item = W.load(self.ts.team)["W-1"]
        self.assertEqual(item.review_by, ["human"], "the team's reviewer applies to a peer's request")
        self.ok(self.worker("work", "claim", "W-1"))
        self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "drafted"))
        code, _p, err = self.reviewer("work", "review", "W-1", "--approve")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"), "the requester is not the reviewer")
        code, _p, err = self.reviewer("work", "close", "W-1")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"), "closing in-review work skips the review")
        code, _p, err = self.reviewer("work", "update", "W-1", "--review-by", "")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"), "nor may the requester drop the reviewer")
        self.assertEqual(self.ok(self.human("work", "review", "W-1", "--approve"))["work"]["status"], "done")

    def test_self_owned_work_cannot_be_closed_past_its_review(self):
        self.ok(self.worker("work", "add", "My own item", "--to", "me", "--review-by", REVIEWER, "--quick"))
        self.ok(self.worker("work", "claim", "W-1"))
        self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "done"))
        code, _p, err = self.worker("work", "close", "W-1")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))

    def test_blocking_after_changes_were_requested_does_not_strand_the_owner(self):
        self.ok(self.human("work", "add", "Draft", "--to", WORKER, "--review-by", REVIEWER, "--quick"))
        self.ok(self.worker("work", "claim", "W-1"))
        self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "v1"))
        self.ok(self.reviewer("work", "review", "W-1", "--changes", "tighten the intro"))
        self.ok(self.worker("work", "block", "W-1", "waiting for the brand guide"))
        self.assertEqual(self.ok(self.worker("work", "unblock", "W-1"))["work"]["status"], "changes_requested")
        self.assertEqual(self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "v2"))["work"]["status"], "in_review")
        # and straight from blocked
        self.ok(self.reviewer("work", "review", "W-1", "--changes", "one more"))
        self.ok(self.worker("work", "block", "W-1", "again"))
        self.assertEqual(self.ok(self.worker("work", "done", "W-1", "--outcome", "succeeded", "--summary", "v3"))["work"]["status"], "in_review")

    def test_an_unverified_claim_to_be_the_owner_cannot_unblock(self):
        self.ok(self.human("work", "add", "x", "--to", WORKER, "--quick"))
        self.ok(self.worker("work", "claim", "W-1"))
        self.ok(self.worker("work", "block", "W-1", "why"))
        code, _p, err = json_out(run_cli(["--json", "work", "unblock", "W-1"], env_no_daemon(self.ts, HERDR_TEAM_MEMBER=WORKER, HERDR_PANE_ID="w9:p9"), self.api))
        self.assertNotEqual(code, 0)

    def test_a_cancelled_dependency_is_flagged_for_the_decider(self):
        self.ok(self.human("work", "add", "first", "--to", WORKER, "--quick"))
        self.ok(self.human("work", "add", "second", "--to", REVIEWER, "--deps", "W-1", "--quick"))
        self.ok(self.human("work", "cancel", "W-1", "not needed"))
        rows = self.ok(self.human("work", "next"))["next"]
        self.assertEqual([(r["id"], r["attention"]) for r in rows], [("W-2", "dependency_dead")])
        self.assertEqual(rows[0]["argv"][-2:], ["--deps", ""])

    def test_a_cycle_is_refused_under_the_lock_too(self):
        items = {}
        W._apply(items, {"op": "create", "id": "W-1", "title": "a", "by": "human", "at": "t"})
        W._apply(items, {"op": "create", "id": "W-2", "title": "b", "by": "human", "at": "t", "deps": ["W-1"]})
        with self.assertRaises(Exception) as ctx:
            W.validate(items, {"op": "update", "id": "W-1", "deps": ["W-2"], "by": "human"})
        self.assertEqual(getattr(ctx.exception, "code", None), "work_dep_cycle")

    def test_hand_edited_lines_of_the_wrong_type_never_break_readers(self):
        path = W.work_jsonl(self.ts.team)
        path.write_text('{"op":"create","id":"W-1","title":"t","by":"human","at":"2026-09-23T00:00:00.000Z","brief":"oops","deps":"W-9"}\n'
                        '{"op":"claim","id":"W-1","by":"%s","gen":"two","at":"2026-09-23T00:00:01.000Z"}\n' % WORKER, encoding="utf-8")
        items = W.load(self.ts.team)
        self.assertEqual((items["W-1"].brief, items["W-1"].deps, items["W-1"].attempts[0].gen), ({}, [], 1))
        self.ok(self.worker("orient"))
        from herdr_team import mission as M
        M.gather(self.ts.layout)


class Model(unittest.TestCase):
    def test_a_torn_or_foreign_line_never_breaks_reading(self):
        with TempState() as ts:
            path = W.work_jsonl(ts.team)
            path.write_text('{"op":"create","id":"W-1","title":"t","by":"human","at":"2026-09-23T00:00:00.000Z"}\n{"op":"cre\nnot json\n{"op":"launch","id":"W-1"}\n', encoding="utf-8")
            items = W.load(ts.team)
            self.assertEqual(list(items), ["W-1"])

    def test_parse_id(self):
        self.assertEqual(W.parse_id("4"), "W-4")
        self.assertEqual(W.parse_id("w-12"), "W-12")
        with self.assertRaises(Exception):
            W.parse_id("W-0")


if __name__ == "__main__":
    unittest.main()

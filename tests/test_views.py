"""Generated team views (0.21, ``herdr_team.views``): shapes on a fixture team, and graceful degradation."""
from __future__ import annotations

import unittest
from unittest import mock

from support import FAKE_AGENTS, TempState, fake_agent
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import features, links, store, views

REVIEWER = "alpha-reviewer"  # codex, w2:p1
WORKER = "alpha-worker"      # claude, w2:p2
SECRET = "ghp_" + "a" * 36


class Rig(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api(list(FAKE_AGENTS))

    def cli(self, argv, pane=None):
        overrides = {"HERDR_PANE_ID": pane} if pane else {}
        code, payload, err = json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **overrides), self.api))
        self.assertEqual(code, 0, err)
        return payload

    def human(self, *argv):
        return self.cli(argv)

    def worker(self, *argv):
        return self.cli(argv, pane="w2:p2")

    def reviewer(self, *argv):
        return self.cli(argv, pane="w2:p1")

    def views(self, **kw):
        return views.team_views(self.ts.layout, "alpha", **kw)


class WorkGraphTests(Rig):
    def test_nodes_carry_status_owner_attempt_readiness_and_review_and_edges_run_from_the_dependency(self):
        self.human("work", "add", "Collect competitors", "--to", WORKER, "--quick")
        self.human("work", "add", "Compare their pricing", "--to", REVIEWER, "--deps", "W-1", "--review-by", "human", "--quick")
        self.worker("work", "claim", "W-1")
        graph = views.work_graph(self.ts.team)
        nodes = {n["id"]: n for n in graph["nodes"]}
        self.assertEqual(set(nodes), {"W-1", "W-2"})
        self.assertEqual(set(nodes["W-1"]), {"id", "title", "status", "owner", "attempt", "ready", "waiting_on", "review"})
        self.assertEqual((nodes["W-1"]["status"], nodes["W-1"]["owner"], nodes["W-1"]["attempt"]), ("in_progress", WORKER, 1))
        self.assertEqual((nodes["W-2"]["ready"], nodes["W-2"]["waiting_on"], nodes["W-2"]["review"]), (False, ["W-1"], True))
        self.assertEqual(nodes["W-2"]["attempt"], 0)
        self.assertEqual(graph["edges"], [{"from": "W-1", "to": "W-2"}])

    def test_no_work_is_an_empty_graph(self):
        self.assertEqual(views.work_graph(self.ts.team), {"nodes": [], "edges": []})


class FactMapTests(Rig):
    def test_facts_group_by_subject_with_support_supersession_and_disputes(self):
        self.worker("fact", "add", "Pro plan is $49/mo", "--about", "Competitor X", "--attribute", "price")
        self.worker("fact", "add", "Pro plan is $59/mo", "--about", "Competitor X", "--attribute", "price", "--supersedes", "F-1")
        self.reviewer("fact", "add", "Pro plan is $69/mo", "--about", "Competitor X", "--attribute", "price")
        self.reviewer("fact", "add", "Launch is in October", "--about", "Acme", "--attribute", "launch")
        self.worker("fact", "support", "F-4")
        self.worker("fact", "add", "The dataset has 12k rows")
        facts = views.fact_map(self.ts.team)
        self.assertEqual([s["about"] for s in facts["subjects"]], ["Acme", "Competitor X", None])
        by_id = {f["id"]: f for s in facts["subjects"] for f in s["facts"]}
        self.assertEqual(set(by_id["F-1"]), {"id", "attribute", "statement", "author", "support", "status", "superseded_by", "disputes"})
        self.assertEqual((by_id["F-1"]["status"], by_id["F-1"]["superseded_by"]), ("superseded", "F-2"))
        self.assertEqual(by_id["F-2"]["status"], "current")
        self.assertEqual(by_id["F-4"]["support"], 2, "the author and one supporter")
        self.assertEqual(by_id["F-2"]["disputes"], ["D-1"])
        self.assertEqual(by_id["F-3"]["disputes"], ["D-1"])
        [dispute] = facts["disputes"]
        self.assertEqual((dispute["id"], dispute["open"], dispute["mode"], dispute["about"], dispute["attribute"]),
                         ("D-1", True, "observe", "Competitor X", "price"))
        self.assertEqual(sorted(dispute["facts"]), ["F-2", "F-3"])
        for fact in by_id.values():
            self.assertIn(fact["status"], views.FACT_STATUSES)

    def test_no_facts_is_an_empty_map(self):
        self.assertEqual(views.fact_map(self.ts.team), {"subjects": [], "disputes": []})


class TopologyTests(Rig):
    def add_beta(self):
        beta = self.ts.session.team("beta")
        from herdr_team import paths

        paths.ensure_team_dirs(beta)
        store.write_json(beta.team_json, {"schema": 1, "team": "beta", "created_at": "2026-09-04T10:00:00Z", "socket": str(self.ts.socket_path),
                                          "state_dir": str(self.ts.state_root), "naming": "prefixed", "revision": 1, "charter": None,
                                          "members": [{"name": "beta-lead", "role": "lead", "kind": "claude", "terminal_id": "term_b",
                                                       "status": "active", "manager": True}]})

    def test_members_with_live_state_profiles_colours_and_linked_teams(self):
        doc = store.read_json(self.ts.team.team_json)
        for member in doc["members"]:
            if member["name"] == REVIEWER:
                member.update(manager=True, profile="fast")
        store.write_json(self.ts.team.team_json, doc)
        store.write_json(self.ts.session.who_json, {"v": 1, "teams": {"alpha": {"members": [
            {"name": REVIEWER, "agent_status": "working", "pane_id": "w2:p7"}, {"name": WORKER, "agent_status": "blocked", "pane_id": "w2:p2"}]}}})
        scene = features.whiteboard_dir(self.ts.team) / "scene.json"
        store.write_json(scene, {"v": 1, "authors": {WORKER: {"color": "#1971c2", "index": 0, "kind": "member"}}})
        self.add_beta()
        links.link(self.ts.session, "alpha", "beta", "human")
        topo = self.views()["topology"]
        self.assertEqual((topo["team"], topo["manager"]), ("alpha", REVIEWER))
        members = {m["name"]: m for m in topo["members"]}
        self.assertEqual(set(members), {REVIEWER, WORKER}, "the human is not drawn as a member")
        self.assertEqual(set(members[REVIEWER]), {"name", "role", "kind", "profile", "status", "agent_status", "pane_id", "color"})
        self.assertEqual((members[REVIEWER]["profile"], members[REVIEWER]["agent_status"], members[REVIEWER]["pane_id"]), ("fast", "working", "w2:p7"))
        self.assertEqual((members[WORKER]["agent_status"], members[WORKER]["color"]), ("blocked", "#1971c2"))
        self.assertIsNone(members[REVIEWER]["color"])
        self.assertEqual(topo["links"], [{"team": "beta", "state": "active", "manager": "beta-lead"}])
        self.assertFalse([m for m, _p in self.api.calls if m == "agent.list"], "who.json answered; nothing asked Herdr")

    def test_without_who_json_one_agent_list_fills_in_live_states(self):
        api = live_api([fake_agent("w2:p1", "term_r1", "codex", REVIEWER, status="done")])
        topo = self.views(api=api)["topology"]
        members = {m["name"]: m for m in topo["members"]}
        self.assertEqual(members[REVIEWER]["agent_status"], "done")
        self.assertIsNone(members[WORKER]["agent_status"])
        self.assertIsNone({m["name"]: m for m in self.views()["topology"]["members"]}[REVIEWER]["agent_status"], "no api, no live state")


class TimelineTests(Rig):
    def test_newest_records_oldest_first_redacted_and_clipped(self):
        self.worker("post", "first note")
        self.human("post", "x" * 400)
        store.BoardStore(self.ts.team).append({"from": "system", "from_kind": "system", "kind": "system", "event": "work_ready",
                                               "to": [WORKER], "text": "leaked " + SECRET})
        line = views.timeline(self.ts.team)
        self.assertEqual([r["seq"] for r in line], sorted(r["seq"] for r in line))
        self.assertEqual(set(line[0]), {"seq", "ts", "kind", "event", "from", "to", "text"})
        self.assertEqual((line[0]["from"], line[0]["text"]), (WORKER, "first note"))
        self.assertLessEqual(len(line[1]["text"]), views.TIMELINE_TEXT_CHARS)
        self.assertTrue(line[1]["text"].endswith("…"))
        self.assertEqual((line[2]["event"], line[2]["to"]), ("work_ready", [WORKER]))
        self.assertNotIn(SECRET, line[2]["text"])
        self.assertIn("[redacted:", line[2]["text"])
        self.assertEqual(len(views.timeline(self.ts.team, limit=2)), 2)

    def test_it_keeps_only_the_newest_limit(self):
        board = store.BoardStore(self.ts.team)
        for i in range(views.TIMELINE_LIMIT + 5):
            board.append({"from": "human", "from_kind": "human", "kind": "note", "to": ["all"], "text": "post {}".format(i)})
        line = views.timeline(self.ts.team)
        self.assertEqual(len(line), views.TIMELINE_LIMIT)
        self.assertEqual(line[-1]["text"], "post {}".format(views.TIMELINE_LIMIT + 4))


class TeamViewsTests(Rig):
    def test_every_part_is_present_with_the_five_lanes(self):
        payload = self.views(now=1_790_000_000.25)
        self.assertEqual(set(payload), {"team", "generated_at", "work", "facts", "topology", "timeline", "lanes", "canvas"})
        self.assertEqual(payload["team"], "alpha")
        self.assertRegex(payload["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(set(payload["lanes"]), {"needs_you", "blocked", "working", "done", "idle"})
        self.assertIsNone(payload["canvas"])

    def test_the_canvas_summary_only_while_the_teams_canvas_is_on(self):
        summary = {"version": 42, "elements": 18, "comments_open": 2, "claims_active": 1, "updated_at": "2026-09-26T19:40:02.123Z"}
        with mock.patch("herdr_team.canvas.summary", return_value=summary):
            self.assertIsNone(self.views()["canvas"])
            features.set_layer(self.ts.session, True, "human", "cli")
            self.assertEqual(self.views()["canvas"], summary)
            features.set_team(self.ts.team, enabled=False)
            self.assertIsNone(self.views()["canvas"])

    def test_unreadable_parts_degrade_to_empty(self):
        from herdr_team import work as _work

        _work.work_jsonl(self.ts.team).write_bytes(b"{not json\n")
        store.write_json(self.ts.session.who_json, ["not", "a", "dict"])
        features.set_layer(self.ts.session, True, "human", "cli")
        with mock.patch("herdr_team.facts.load", side_effect=OSError("gone")), \
                mock.patch("herdr_team.mission.gather", side_effect=ValueError("bad")), \
                mock.patch("herdr_team.canvas.summary", side_effect=OSError("unreadable scene")):
            payload = self.views()
        self.assertEqual(payload["work"], {"nodes": [], "edges": []})
        self.assertEqual(payload["facts"], {"subjects": [], "disputes": []})
        self.assertEqual(payload["lanes"], {})
        self.assertIsNone(payload["canvas"])
        self.assertEqual(len(payload["topology"]["members"]), 2)

    def test_a_missing_team_still_answers(self):
        payload = views.team_views(self.ts.layout, "ghost")
        self.assertEqual(payload["team"], "ghost")
        self.assertEqual(payload["topology"]["members"], [])
        self.assertEqual(payload["timeline"], [])


if __name__ == "__main__":
    unittest.main()

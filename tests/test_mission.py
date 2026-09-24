"""Mission control (0.19): every team's cards in five lanes, and the popup's Enter."""
from __future__ import annotations

import json
import time
import unittest

from support import FAKE_AGENTS, FakeApi, TempState
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import cmd_mission, mission as M, store

REVIEWER = "alpha-reviewer"  # w2:p1
WORKER = "alpha-worker"      # w2:p2


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

    def who(self, states):
        store.write_json(self.ts.session.who_json, {"v": 1, "teams": {"alpha": {"members": [
            {"name": name, "agent_status": status, "last_headline": "doing {}".format(name)} for name, status in states.items()]}}})

    def lane(self, report, lane):
        return [(c["kind"], c["title"]) for c in report["lanes"][lane]]


class Lanes(Rig):
    def test_each_concern_lands_in_its_lane(self):
        self.who({REVIEWER: "blocked", WORKER: "working"})
        self.cli(["post", "--to", "human", "--kind", "question", "--no-wait", "ship it?"], pane="w2:p2")
        self.cli(["work", "add", "Pick the positioning", "--to", WORKER, "--review-by", "human", "--quick"])
        self.cli(["work", "claim", "W-1"], pane="w2:p2")
        self.cli(["work", "done", "W-1", "--outcome", "succeeded", "--summary", "Option B. Tested with 5 users. None."], pane="w2:p2")
        self.cli(["work", "add", "Scan forums", "--to", REVIEWER, "--quick"])
        self.cli(["work", "claim", "W-2"], pane="w2:p1")
        self.cli(["work", "block", "W-2", "forum needs a login"], pane="w2:p1")
        self.cli(["fact", "add", "Pro is $49", "--about", "X", "--attribute", "price"], pane="w2:p2")
        self.cli(["fact", "add", "Pro is $59", "--about", "X", "--attribute", "price"], pane="w2:p1")
        report = M.gather(self.ts.layout)
        needs = [kind for kind, _ in self.lane(report, "needs_you")]
        self.assertEqual(sorted(needs), sorted(["ask", "dialog", "review", "dispute"]), self.lane(report, "needs_you"))
        self.assertEqual([k for k, _ in self.lane(report, "blocked")], ["work"])
        working = report["lanes"]["working"]
        self.assertEqual([c["who"] for c in working], [WORKER])
        dialog = next(c for c in report["lanes"]["needs_you"] if c["kind"] == "dialog")
        self.assertEqual((dialog["who"], dialog["pane_id"]), (REVIEWER, "w2:p1"))
        review = next(c for c in report["lanes"]["needs_you"] if c["kind"] == "review")
        self.assertEqual(review["argv"][-3:], ["review", "W-1", "--approve"])

    def test_done_idle_and_an_absent_owner(self):
        self.who({REVIEWER: "idle", WORKER: "done"})
        self.cli(["work", "add", "Quick one", "--to", WORKER, "--quick"])
        self.cli(["work", "claim", "W-1"], pane="w2:p2")
        self.cli(["work", "done", "W-1", "--outcome", "succeeded", "--summary", "done"], pane="w2:p2")
        report = M.gather(self.ts.layout)
        self.assertEqual(sorted(k for k, _ in self.lane(report, "done")), ["member", "work"])
        self.assertEqual([c["who"] for c in report["lanes"]["idle"]], [REVIEWER])
        self.cli(["work", "add", "Held", "--to", REVIEWER, "--quick"])
        self.cli(["work", "claim", "W-2"], pane="w2:p1")
        doc = store.read_json(self.ts.team.team_json)
        for m in doc["members"]:
            if m["name"] == REVIEWER:
                m["status"] = "missing"
        store.write_json(self.ts.team.team_json, doc)
        blocked = M.gather(self.ts.layout)["lanes"]["blocked"]
        self.assertEqual((blocked[0]["kind"], blocked[0]["argv"][-2:]), ("member", ["resume", REVIEWER]))

    def test_a_partial_result_needs_a_decision_only_without_a_manager(self):
        self.who({})
        self.cli(["work", "add", "Survey", "--to", WORKER, "--quick"])
        self.cli(["work", "claim", "W-1"], pane="w2:p2")
        self.cli(["work", "done", "W-1", "--outcome", "partial", "--summary", "half"], pane="w2:p2")
        self.assertIn("decision", [k for k, _ in self.lane(M.gather(self.ts.layout), "needs_you")], "the human requested it")

    def test_the_command_prints_every_lane(self):
        code, out, err = run_cli(["mission"], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0, err)
        for title in ("Needs you", "Blocked", "Working", "Done", "Idle"):
            self.assertIn(title, out)


class Popup(Rig):
    def test_enter_focuses_an_agent_and_shows_the_command_otherwise(self):
        api = FakeApi()
        api.set_response("pane.focus", {"type": "ok"})
        self.assertIsNone(cmd_mission.activate({"kind": "member", "pane_id": "w2:p1"}, api))
        self.assertIn(("pane.focus", {"pane_id": "w2:p1"}), api.calls)
        self.assertEqual(cmd_mission.activate({"kind": "work", "argv": ["herdr-synapse", "work", "show", "W-1"]}, api), "$ herdr-synapse work show W-1")

    def test_the_popup_refuses_outside_its_pane(self):
        code, _p, err = json_out(run_cli(["--json", "mission-pane"], env_no_daemon(self.ts), self.api))
        self.assertEqual((code, err["code"]), (1, "not_a_plugin_pane"))

    def test_the_manifest_and_keys_offer_it(self):
        from herdr_team import cmd_misc, cmd_ui, paths

        manifest = (paths.plugin_root() / "herdr-plugin.toml").read_text(encoding="utf-8")
        self.assertIn('id = "mission"', manifest)
        self.assertIn('command = ["./bin/herdr-synapse", "mission-pane"]', manifest)
        self.assertEqual(cmd_misc.KEYS["mission"], "prefix+d")
        self.assertIn("mission", cmd_ui.UI_TARGETS)


if __name__ == "__main__":
    unittest.main()

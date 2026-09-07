"""``add`` announces the newcomer: an urgent ``member_joined`` broadcast that nudges every other member."""

from __future__ import annotations

import unittest

from herdr_team import roster, store
from support import TempState
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon


class MemberJoinedTests(unittest.TestCase):
    def test_add_posts_an_urgent_member_joined_broadcast(self):
        with TempState() as ts:
            api = live_api()
            env = env_no_daemon(ts)
            code, payload, err = json_out(run_cli(["--json", "add", "alpha", "w5:p1", "--role", "tester", "--as", "tess"], env, api))
            self.assertEqual(code, 0, err)
            record = store.BoardStore(ts.team).get(payload["joined_record"])
            self.assertEqual((record["from"], record["kind"], record["event"], record["to"], record["urgent"]), ("system", "system", "member_joined", ["all"], True))
            self.assertEqual((record["member"], record["role"], record["member_kind"]), ("tess", "tester", "codex"))
            self.assertIn("tess joined team alpha as tester", record["text"])

    def test_add_warns_when_the_kind_is_not_trusted_for_delivery(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "add", "alpha", "w5:p1", "--role", "tester", "--as", "tess"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertFalse(payload["kind_trusted"])
            self.assertIn("not trusted for delivery yet", err)
            self.assertIn("herdr-synapse kinds trust codex", err)
        with TempState() as ts:
            store.write_json(ts.session.kinds_json, {"codex": {"trusted": True}})
            code, payload, err = json_out(run_cli(["--json", "add", "alpha", "w5:p1", "--role", "tester", "--as", "tess"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertTrue(payload["kind_trusted"])
            self.assertNotIn("not trusted", str(err))

    def test_daemon_nudges_every_member_but_the_newcomer(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        d, api, clock = make_daemon(ts)
        d.on_connected()
        seq = roster.append_system_record(ts.team, "member_joined", "alpha-reviewer joined team alpha as reviewer (codex, w2:p1)", to=["all"],
                                          extra={"urgent": True, "member": "alpha-reviewer", "role": "reviewer", "member_kind": "codex"})
        d.tick()
        team = d.teams["alpha"]
        self.assertEqual(sorted(team.pending), ["alpha-worker"])
        self.assertEqual((team.pending["alpha-worker"].seqs, team.pending["alpha-worker"].urgent), ([seq], True))
        # a cold start rebuilds the same pending from the board
        d2, _api2, _clock2 = make_daemon(ts)
        d2.on_connected()
        team2 = d2.teams["alpha"]
        team2.pending.clear()
        team2.watermark = seq
        d2._rebuild_pending(team2)
        self.assertEqual(sorted(team2.pending), ["alpha-worker"])
        # a non-urgent system broadcast still nudges nobody
        roster.append_system_record(ts.team, "renamed", "x renamed", to=["all"])
        d.tick()
        self.assertEqual(sorted(team.pending), ["alpha-worker"])


if __name__ == "__main__":
    unittest.main()

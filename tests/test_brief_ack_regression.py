"""Sandbox regression (2026-09-05): an ack at an unchanged cursor seq was invisible to the daemon.

The join sets a member's cursor at the board max. When nothing is posted before the
briefing lands, ``herdr-team ack`` moves the cursor from N to N, the monotone store did
not rewrite the file, its ``updated`` stayed at the join time, and the daemon concluded
"did not ack the briefing; re-briefing once" 90 s later. ``touch`` makes the ack a write.
"""
from __future__ import annotations

import time
import unittest

from herdr_team import store
from support import TempState
from test_daemon import make_daemon


class CursorTouchTests(unittest.TestCase):
    def test_touch_rewrites_updated_at_an_unchanged_seq(self):
        with TempState() as ts:
            cursors = store.Cursors(ts.team)
            first = cursors.advance("alpha-reviewer", 0, "term_r1", "cli", touch=True)
            self.assertIsNotNone(first["updated"])
            self.assertFalse(first["advanced"])
            time.sleep(0.002)
            plain = cursors.advance("alpha-reviewer", 0, "term_r1", "cli")
            self.assertEqual(plain["updated"], first["updated"])  # no touch: untouched
            touched = cursors.advance("alpha-reviewer", 0, "term_r1", "cli", touch=True)
            self.assertNotEqual(touched["updated"], first["updated"])
            self.assertEqual(touched["seq"], 0)
            self.assertFalse(touched["advanced"])
            self.assertEqual(cursors.get("alpha-reviewer")["surfaced_by"], "cli")


class BriefAckRegressionTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()

    def _land_briefing(self):
        path = self.ts.team.jobs_dir / "{}-brief.json".format(int(time.time() * 1000))
        store.write_json(path, {"v": 1, "kind": "brief", "member": "alpha-reviewer", "force": False, "requested_by": {"name": "human"}, "requested_at": "now"})
        self.clock.advance(1)
        self.d.consume_jobs(self.clock() * 1000)
        for _ in range(4):
            self.clock.advance(1)
            self.d.tick()
        team = self.d.teams["alpha"]
        pending = team.pending["alpha-reviewer"]
        self.assertEqual(pending.kind, "brief")
        self.assertIsNotNone(pending.landed_ms, self.d.logged)
        return team

    def test_ack_at_the_join_seq_is_recognised_and_never_rebriefed(self):
        team = self._land_briefing()
        brief_seq = team.rt("alpha-reviewer").brief_seq
        # empty board: the join cursor already equals the board max, so the ack moves nothing
        self.assertEqual(store.Cursors(self.ts.team).get("alpha-reviewer")["seq"], brief_seq or 0)
        store.Cursors(self.ts.team).advance("alpha-reviewer", brief_seq or 0, "term_r1", "cli", touch=True)  # what `herdr-team ack` does
        self.clock.advance(1)
        self.d.tick()
        self.assertNotIn("alpha-reviewer", team.pending, self.d.logged)
        self.assertTrue(any("acknowledged the briefing" in line for line in self.d.logged), self.d.logged)
        # and 90 s later nothing is re-briefed
        self.clock.advance(100)
        self.d.tick()
        self.assertFalse(any("re-briefing" in line for line in self.d.logged), self.d.logged)
        self.assertEqual(len([m for m, _ in self.api.calls if m == "agent.prompt"]), 1)

    def test_without_a_write_the_daemon_still_rebriefs_once(self):
        team = self._land_briefing()
        self.clock.advance(100)
        self.d.tick()
        self.assertTrue(any("re-briefing once" in line for line in self.d.logged), self.d.logged)


if __name__ == "__main__":
    unittest.main()

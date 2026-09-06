"""A removed member is a tombstone: it must not be addressed, acted on, or nudged again.

`remove` keeps the entry with `status: "left"` so old board records stay
readable. That tombstone used to shadow a live member of the same name, and
the daemon kept its pending work alive for ever; both are covered here.
"""

from __future__ import annotations

import json
import unittest

from support import FakeApi, TempState

from herdr_team import roster, store
from herdr_team.errors import HerdrTeamError
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon, post


def member(name, terminal, status="active", **extra):
    doc = {"name": name, "role": "worker", "kind": "claude", "terminal_id": terminal, "status": status}
    doc.update(extra)
    return roster.Member.from_json(doc)


def team_with(*members):
    return roster.Team(team="alpha", socket="/s", state_dir="/d", created_at="2026-09-06T00:00:00Z", members=list(members))


class FindPrefersTheLiveMemberTests(unittest.TestCase):
    """Re-adding a name after a remove is legal, so both entries exist; the live one must win."""

    def test_a_live_member_wins_over_a_tombstone_by_name(self):
        gone, live = member("alpha-worker", "term_old", status="left"), member("alpha-worker", "term_new")
        team = team_with(gone, live)  # the tombstone comes first in file order
        self.assertIs(team.find("alpha-worker"), live)
        self.assertIs(team.find("term_new"), live)
        self.assertIs(team.find("term_old"), gone)  # nothing live holds that terminal
        self.assertEqual(team.names(), ["alpha-worker"])

    def test_a_tombstone_still_resolves_when_nothing_live_holds_the_name(self):
        gone = member("alpha-worker", "term_old", status="left")
        team = team_with(gone)
        self.assertIs(team.find("alpha-worker"), gone)
        self.assertIsNone(team.find("nobody"))
        self.assertIsNone(team.find(""))

    def test_a_live_retired_name_wins_over_a_tombstones_current_name(self):
        gone = member("alpha-worker", "term_old", status="left")
        live = member("alpha-lead", "term_new", previous_names=[{"name": "alpha-worker", "retired_at": roster.now_iso()}])
        team = team_with(gone, live)
        # The live member answered to that name minutes ago; the tombstone must not shadow it.
        self.assertIs(team.find("alpha-worker"), live)


class RefuseActingOnATombstoneTests(unittest.TestCase):
    def leave_behind_a_tombstone(self, ts):
        code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "remove", "alpha", "alpha-worker"], ts.env, FakeApi()))
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["removed"], "alpha-worker")

    def test_remove_rename_and_brief_all_refuse_a_member_that_left(self):
        with TempState() as ts:
            self.leave_behind_a_tombstone(ts)
            code, _payload, err = json_out(run_cli(["--json", "--team", "alpha", "remove", "alpha", "alpha-worker"], ts.env, FakeApi()))
            self.assertEqual((code, err["code"], err["status"]), (1, "member_not_found", "left"))
            self.assertIn("already left", err["message"])
            code, _payload, err = json_out(run_cli(["--json", "--team", "alpha", "rename", "alpha-worker", "alpha-lead"], ts.env, FakeApi()))
            self.assertEqual((code, err["code"]), (1, "member_not_found"))
            code, _payload, err = json_out(run_cli(["--json", "--team", "alpha", "brief", "alpha-worker", "--set", "new goal"], ts.env, FakeApi()))
            self.assertEqual((code, err["code"]), (1, "member_not_found"))
            doc = store.read_json(ts.team.team_json)
            entry = [m for m in doc["members"] if m["name"] == "alpha-worker"][0]
            self.assertEqual(entry["status"], "left")
            self.assertIsNone(entry.get("brief"))  # the refused brief wrote nothing


class CursorFollowsARenameTests(unittest.TestCase):
    def test_migrate_cursor_copies_once_and_never_clobbers(self):
        with TempState() as ts:
            for _ in range(12):
                post(ts, "all")  # a cursor is clamped to the board max, so the board must have seqs
            cursors = store.Cursors(ts.team)
            cursors.advance("alpha-worker", 7, terminal_id="term_w1", surfaced_by="cli")
            self.assertTrue(roster.migrate_cursor(ts.team, "alpha-worker", "alpha-lead"))
            self.assertEqual(cursors.get("alpha-lead")["seq"], 7)
            # a second call must not overwrite a cursor the new name has moved on its own
            cursors.advance("alpha-lead", 9, terminal_id="term_w1", surfaced_by="cli")
            self.assertFalse(roster.migrate_cursor(ts.team, "alpha-worker", "alpha-lead"))
            self.assertEqual(cursors.get("alpha-lead")["seq"], 9)
            self.assertFalse(roster.migrate_cursor(ts.team, "nobody", "someone"))
            self.assertFalse(roster.migrate_cursor(ts.team, "alpha-worker", "alpha-worker"))

    def test_rename_carries_the_read_position_so_the_board_is_not_replayed(self):
        with TempState() as ts:
            for _ in range(12):
                post(ts, "all")
            store.Cursors(ts.team).advance("alpha-worker", 12, terminal_id="term_w1", surfaced_by="cli")
            code, payload, err = json_out(run_cli(["--json", "rename", "alpha-worker", "alpha-lead", "--team", "alpha"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual((payload["old"], payload["new"]), ("alpha-worker", "alpha-lead"))
            self.assertEqual(store.Cursors(ts.team).get("alpha-lead")["seq"], 12)
            # without the migration the new name starts at 0 and replays the whole board
            self.assertEqual(store.Cursors(ts.team).get("alpha-worker")["seq"], 12)


class DaemonDropsWorkForARemovedMemberTests(unittest.TestCase):
    def test_pending_work_is_dropped_and_never_delivered(self):
        with TempState() as ts:
            d, api, clock = make_daemon(ts)
            d.on_connected()
            seq = post(ts, "alpha-reviewer")
            d.tick()
            self.assertEqual(d.teams["alpha"].pending["alpha-reviewer"].seqs, [seq])
            # the human removes it from the picker while the nudge is still held
            roster.Roster(ts.layout, "alpha").remove_member(api, "alpha-reviewer", socket="/s")
            d._reload_roster(d.teams["alpha"])
            for _ in range(20):
                clock.advance(5)
                d.tick()
            self.assertNotIn("alpha-reviewer", d.teams["alpha"].pending)
            self.assertEqual([p for m, p in api.calls if m == "agent.prompt"], [])
            self.assertTrue(any("left the team; dropping its pending work" in line for line in d.logged), d.logged[-6:])


if __name__ == "__main__":
    unittest.main()

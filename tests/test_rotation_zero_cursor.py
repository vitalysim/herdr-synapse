"""A zero or absent cursor still means archived addressed posts are unread."""
from __future__ import annotations

import unittest
from unittest import mock

from herdr_team import hooks, store
from support import TempState
from test_cmd_board import json_out, run_cli
from test_cmd_roster import live_api

MEMBER = "alpha-reviewer"
PEER = "alpha-worker"


class ZeroCursorRotation(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api()
        self.env = self.ts.env_with(HERDR_PANE_ID="w2:p1")
        self.board = store.BoardStore(self.ts.team)
        self.old = self.board.append({"from": PEER, "kind": "note", "to": [MEMBER], "text": "archived unread"})
        for index in range(2):
            self.board.append({"from": PEER, "kind": "note", "to": [MEMBER], "text": "older unread {}".format(index)})
        self.assertIsNotNone(store.BoardStore(self.ts.team, rotate_bytes=1).rotate_if_needed())

    def test_who_counts_missing_member_cursor_even_when_other_readers_are_current(self):
        # A best-effort join cursor write can fail; missing still means seq zero.
        store.Cursors(self.ts.team).advance(PEER, self.board.max_seq(), "term_w1", "cli")
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "who"], self.ts.env, self.api))
        self.assertEqual(code, 0, err)
        row = next(m for m in out["members"] if m["name"] == MEMBER)
        self.assertEqual(row["unread"], 4)  # Three authored posts plus the rotation notice.

    def test_who_counts_missing_requesting_human_cursor(self):
        self.board.append({"from": PEER, "kind": "note", "to": ["human"], "text": "operator archive"})
        self.assertIsNotNone(store.BoardStore(self.ts.team, rotate_bytes=1).rotate_if_needed())
        cursors = store.Cursors(self.ts.team)
        for member in self.ts.members:
            cursors.advance(member["name"], self.board.max_seq(), member.get("terminal_id"), "cli")
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "who"], self.ts.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["unread_for_you"], 3)  # Both rotation notices and the operator post.

    def test_stop_zero_cursor_blocks_for_archived_authored_mail(self):
        code, text = hooks.stop_decision(self.ts.layout, "alpha", MEMBER, {}, now=1_800_000_000)
        self.assertEqual(code, 2)
        self.assertIn("seq {}".format(self.old), text)

    def test_ack_zero_cursor_does_not_skip_archived_unseen_post(self):
        code, out, err = json_out(run_cli(["--json", "ack"], self.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["cursor"], self.old - 1)
        self.assertIn(self.old, store.Cursors(self.ts.team).unshown(MEMBER))

    def test_unshown_missing_cursor_includes_archived_post(self):
        self.assertIn(self.old, store.Cursors(self.ts.team).unshown(MEMBER))

    def test_corrupt_cursor_fallback_keeps_archived_posts_unread(self):
        store.write_json(self.ts.team.cursor(MEMBER), {"seq": "broken"})
        self.assertIn(self.old, store.Cursors(self.ts.team).unshown(MEMBER))
        self.assertIn(self.old, [r["seq"] for r in hooks.unread_for(self.ts.team, MEMBER)])
        code, out, err = json_out(run_cli(["--json", "ack"], self.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["cursor"], self.old - 1)

    def test_who_all_displayed_and_requesting_readers_current_does_not_read_archive(self):
        cursors = store.Cursors(self.ts.team)
        for member in self.ts.members:
            cursors.advance(member["name"], self.board.max_seq(), member.get("terminal_id"), "cli")
        cursors.advance("human@human", self.board.max_seq(), None, "cli")
        with mock.patch.object(store.BoardStore, "read_archive_range", side_effect=AssertionError("archive read")):
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "who"], self.ts.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["unread_for_you"], 0)
        self.assertTrue(all(m["unread"] == 0 for m in out["members"]))

    def test_ack_after_reading_archived_posts_advances_normally(self):
        code, out, err = json_out(run_cli(["--json", "board", "--new"], self.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual([r["seq"] for r in out["posts"]][:3], [self.old, self.old + 1, self.old + 2])
        code, out, err = json_out(run_cli(["--json", "ack"], self.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["cursor"], self.board.max_seq())
        self.assertEqual(out["unread"], 0)

    def test_rotation_before_active_snapshot_does_not_skip_unseen_posts(self):
        with TempState() as ts:
            board = store.BoardStore(ts.team)
            board.append({"from": PEER, "kind": "note", "to": [MEMBER], "text": "already seen"})
            store.Cursors(ts.team).advance(MEMBER, 1, "term_r1", "cli")
            old = [board.append({"from": PEER, "kind": "note", "to": [MEMBER], "text": "unseen {}".format(i)}) for i in range(2)]
            original = store.BoardStore._read_active
            rotated = []

            def rotate_then_read(active_board):
                if active_board.team.root == ts.team.root and not rotated:
                    rotated.append(True)
                    self.assertIsNotNone(store.BoardStore(ts.team, rotate_bytes=1).rotate_if_needed())
                    board.append({"from": PEER, "kind": "note", "to": [MEMBER], "text": "fresh addressed"})
                return original(active_board)

            # Rotation happens before the actual snapshot, after any obsolete
            # first-seq probe. The proof still rotates in a one-read design.
            with mock.patch.object(store.BoardStore, "_read_active", new=rotate_then_read):
                code, out, err = json_out(run_cli(["--json", "board", "--new"], ts.env_with(HERDR_PANE_ID="w2:p1"), self.api))
            self.assertEqual(code, 0, err)
            self.assertEqual(rotated, [True])
            shown = [r["seq"] for r in out["posts"]]
            self.assertEqual(([s for s in old if s in shown], out["cursor"]["before"], out["cursor"]["after"]), (old, 1, board.max_seq()))

    def test_suffix_keeps_receipts_and_filter_safe_advancement(self):
        answer = self.board.append({"from": PEER, "kind": "answer", "to": [MEMBER], "text": "new answer"})
        self.board.append({"from": "system", "kind": "system", "event": "nudged", "to": [MEMBER], "text": "nudged", "reply_to": answer})
        code, out, err = json_out(run_cli(["--json", "board", "--new", "--kind", "answer", "--receipts"], self.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual([r["seq"] for r in out["posts"]], [answer])
        self.assertEqual(out["cursor"]["after"], 0)
        self.assertEqual(len(out["receipts"][str(answer)]["nudged"]), 1)
        code, out, err = json_out(run_cli(["--json", "board", "--new"], self.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual([r["seq"] for r in out["posts"]][:3], [self.old, self.old + 1, self.old + 2])
        self.assertNotIn(answer, [r["seq"] for r in out["posts"]])

    def test_invalid_records_above_positive_cursor_are_reported(self):
        store.Cursors(self.ts.team).advance(MEMBER, self.old, "term_r1", "cli")
        invalid = {"v": 1, "seq": self.board.max_seq() + 1, "ts": store.now_iso(), "from": PEER, "kind": "note", "to": "not-a-list", "text": "bad grammar"}
        with self.ts.team.board_jsonl.open("ab") as output:
            output.write(store.encode_record(invalid) + b"not json\n" + b'{"seq":')
        code, out, err = json_out(run_cli(["--json", "board", "--new"], self.env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual([r["seq"] for r in out["posts"]][:2], [self.old + 1, self.old + 2])
        # Stored grammar failures are counted as corrupt by the store parser;
        # passing a positive floor must not hide those diagnostics.
        self.assertEqual(out["skipped"]["corrupt"], 2)
        self.assertEqual(out["skipped"]["fragment"], 1)

    def test_current_hook_does_not_read_archive(self):
        store.Cursors(self.ts.team).advance(MEMBER, self.board.max_seq(), "term_r1", "cli")
        with mock.patch.object(store.BoardStore, "read_archive_range", side_effect=AssertionError("archive read")):
            self.assertEqual(hooks.unread_for(self.ts.team, MEMBER), [])


if __name__ == "__main__":
    unittest.main()

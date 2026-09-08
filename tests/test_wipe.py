"""``wipe``: empty a board without renumbering it (0.15.1). Every test here fails against 0.15.0."""
from __future__ import annotations

import os
import unittest

from support import TempState
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon, post

from herdr_team import console, store, tui_model
from herdr_team import daemon as D

MEMBER = "alpha-reviewer"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.board = store.BoardStore(self.ts.team)

    def test_clear_archives_every_post_and_keeps_counting(self):
        for i in range(5):
            post(self.ts, MEMBER, "post {}".format(i))
        result = self.board.clear("human", reason="fresh start")
        self.assertEqual((result["records"], result["first_seq"], result["last_seq"], result["note_seq"]), (5, 1, 5, 6))
        self.assertTrue(result["archived_to"].endswith("archive/board.1-5.jsonl"))
        active = self.board.read()
        self.assertEqual([r["seq"] for r in active], [6])
        note = active[0]
        self.assertEqual((note["event"], note["by"], note["reason"], note["cleared_first_seq"], note["cleared_last_seq"]), ("board_cleared", "human", "fresh start", 1, 5))
        self.assertIn("moved to archive/board.1-5.jsonl", note["text"])
        # the history is still there, and the next post keeps counting
        self.assertEqual([r["seq"] for r in self.board.read(since_seq=0, include_archive=True)][:5], [1, 2, 3, 4, 5])
        self.assertEqual(post(self.ts, MEMBER, "after"), 7)
        self.assertEqual([s["file"] for s in self.board.archive_segments()], ["board.1-5.jsonl"])

    def test_purge_deletes_the_archive_and_the_payloads(self):
        for i in range(3):
            post(self.ts, MEMBER, "post {}".format(i))
        self.board.clear("human")                       # one archive segment
        post(self.ts, MEMBER, "more")
        os.makedirs(self.ts.team.payloads_dir, exist_ok=True)
        (self.ts.team.payloads_dir / "2-body.md").write_text("long", encoding="utf-8")
        result = self.board.clear("human", purge=True)
        self.assertTrue(result["purge"])
        self.assertIsNone(result["archived_to"])
        self.assertEqual((result["purged_segments"], result["purged_payloads"]), (2, 1))
        self.assertEqual(self.board.archive_segments(), [])
        self.assertEqual(sorted(os.listdir(self.ts.team.payloads_dir)), [])
        active = self.board.read()
        self.assertEqual(len(active), 1)
        self.assertIn("deleted with the archive and payloads", active[0]["text"])
        self.assertEqual(self.board.read(since_seq=0, include_archive=True), active, "nothing older survives")

    def test_an_empty_board_is_a_no_op(self):
        result = self.board.clear("human")
        self.assertEqual((result["records"], result["note_seq"], result["archived_to"]), (0, None, None))
        self.assertEqual(self.board.read(), [])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        post(self.ts, MEMBER, "hello")

    def wipe(self, *argv, env=None):
        return json_out(run_cli(["--json", "--team", "alpha", "wipe"] + list(argv), env if env is not None else env_no_daemon(self.ts), live_api()))

    def test_it_asks_unless_told_and_is_human_only(self):
        code, _payload, err = self.wipe()
        self.assertEqual((code, err["code"]), (1, "confirmation_required"))
        self.assertEqual(len(store.BoardStore(self.ts.team).read()), 1, "nothing happened")
        code, _payload, err = self.wipe("--yes", env=env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1"))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        code, payload, err = self.wipe("--yes", "--reason", "new sprint")
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["wiped"], payload["records"], payload["note_seq"]), (True, 1, 2))
        from herdr_team.identity import read_audit

        self.assertIn("board_wiped", [e["event"] for e in read_audit(self.ts.layout, "alpha")])

    def test_purge_is_audited_as_such(self):
        code, payload, err = self.wipe("--yes", "--purge")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["purge"])
        from herdr_team.identity import read_audit

        self.assertIn("board_purged", [e["event"] for e in read_audit(self.ts.layout, "alpha")])


class DaemonTests(unittest.TestCase):
    def test_the_note_drops_what_pointed_at_the_old_board(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        d, api, clock = make_daemon(ts)
        d.on_connected()
        team = d.teams["alpha"]
        post(ts, MEMBER, "a")
        store.BoardStore(ts.team).append({"from": MEMBER, "from_kind": "codex", "from_terminal": "term_r1", "origin": {"via": "cli", "verified": True},
                                          "to": ["human"], "kind": "question", "text": "may I?"})
        d.scan_teams(force=True)
        d.tail_boards()
        self.assertIn(MEMBER, team.pending)
        self.assertTrue(team.open_asks)
        store.BoardStore(ts.team).clear("human")
        d.tail_boards()
        self.assertEqual(team.open_asks, {})
        self.assertNotIn(MEMBER, team.pending)
        self.assertEqual(D.SYSTEM_EVENT_DELIVERY["board_cleared"], {})
        # the tailer followed the rotation: the note is the next record it saw
        self.assertTrue(any("board cleared at #" in line for line in d.logged))


class ConsoleTests(unittest.TestCase):
    def test_slash_wipe_parses_and_asks_first(self):
        intent = tui_model.parse_input_line("/wipe", "alpha")
        self.assertEqual((intent.kind, intent.args["purge"], intent.args["reason"]), ("wipe", False, None))
        intent = tui_model.parse_input_line("/wipe --purge new sprint", "alpha")
        self.assertEqual((intent.args["purge"], intent.args["reason"]), (True, "new sprint"))
        self.assertIn("/wipe", tui_model.SLASH_COMMANDS)
        self.assertIn("/wipe", tui_model.SLASH_USAGE)
        from test_tui_model import model_with

        model = model_with([])
        for ch in "/wipe":
            tui_model.apply_key(model, ch)
        self.assertEqual(tui_model.apply_key(model, "ENTER").kind, "none")
        self.assertIsNotNone(model.pending_confirm)
        self.assertIn("clear the board", model.status or "")
        self.assertEqual(tui_model.apply_key(model, "n").kind, "none")
        self.assertIsNone(model.pending_confirm)
        for ch in "/wipe --purge":
            tui_model.apply_key(model, ch)
        tui_model.apply_key(model, "ENTER")
        self.assertIn("nothing is kept", model.status or "")
        confirmed = tui_model.apply_key(model, "y")
        self.assertEqual((confirmed.kind, confirmed.args["confirm"], confirmed.args["purge"]), ("wipe", True, True))

    def test_the_feed_cache_starts_over_after_a_wipe(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        for i in range(3):
            post(ts, MEMBER, "post {}".format(i))
        tail = console.BoardTail(ts.team)
        tail.poll()
        self.assertEqual([r["seq"] for r in tail.records], [1, 2, 3])
        store.BoardStore(ts.team).clear("human")
        tail.poll()
        self.assertEqual([r["seq"] for r in tail.records], [1, 2, 3, 4], "without a reset the cache keeps the archived posts")
        tail.reset()
        tail.poll()
        self.assertEqual([r["seq"] for r in tail.records], [4])
        self.assertEqual(tail.records[0]["event"], "board_cleared")


if __name__ == "__main__":
    unittest.main()

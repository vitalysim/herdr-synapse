"""Ledger: append-only intent/result/outcome records, replay, stats (plan 8.3)."""

import json
import os
import unittest

from herdr_team import ledger as L
from herdr_team import store
from support import TempState


def attempt(attempt_id, member="alpha-reviewer", kind="codex", seqs=(41, 42), attempts=1, **extra):
    return L.Attempt(
        id=attempt_id, member=member, kind=kind, seqs=list(seqs), hook_authority=False, weak_idle=True,
        focused=False, prompt_line_empty=True, gate_ms=2500.0, queue_ms=4000.0, attempts=attempts, extra=dict(extra),
    )


class LedgerWriteTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.ledger = L.Ledger(self.ts.team)

    def read_lines(self):
        raw = store.read_bytes(self.ts.team.ledger) or b""
        return [json.loads(l) for l in raw.split(b"\n") if l.strip()]

    def test_intent_result_outcome_are_three_fsynced_lines(self):
        self.ledger.record_intent(attempt("a1", pane_id="w2:p1"))
        self.ledger.record_result("a1", L.RESULT_LANDED_WORKING, {"elapsed_ms": 812.0})
        self.ledger.record_outcome("a1", True, 15000.0)
        lines = self.read_lines()
        self.assertEqual([l["phase"] for l in lines], ["intent", "result", "outcome"])
        self.assertEqual(lines[0]["member"], "alpha-reviewer")
        self.assertEqual(lines[0]["seqs"], [41, 42])
        self.assertEqual(lines[0]["pane_id"], "w2:p1")  # extra keys flattened
        self.assertEqual(lines[1]["result"], "landed_working")
        self.assertEqual(lines[1]["elapsed_ms"], 812.0)
        self.assertTrue(lines[2]["cursor_advanced"])
        self.assertEqual(lines[2]["cursor_latency_ms"], 15000.0)
        for line in lines:
            self.assertRegex(line["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(oct(os.stat(self.ts.team.ledger).st_mode & 0o777), oct(0o600))

    def test_attempts_merge_phases_and_open_intents_count_as_sent(self):
        self.ledger.record_intent(attempt("a1"))
        self.ledger.record_result("a1", L.RESULT_LANDED_WORKING)
        self.ledger.record_outcome("a1", True, 1000.0)
        self.ledger.record_intent(attempt("a2", member="alpha-worker", kind="claude"))
        merged = self.ledger.attempts()
        self.assertEqual(list(merged), ["a1", "a2"])
        self.assertEqual(merged["a1"]["result"], "landed_working")
        self.assertTrue(merged["a1"]["cursor_advanced"])
        self.assertIsNone(merged["a2"]["result"])
        opened = self.ledger.open_intents()
        self.assertEqual([o["id"] for o in opened], ["a2"])
        self.assertEqual(opened[0]["member"], "alpha-worker")

    def test_replay_skips_torn_and_garbage_lines(self):
        self.ledger.record_intent(attempt("a1"))
        store.append_line(self.ts.team.ledger, b"{not json", fsync=False)
        store.append_line(self.ts.team.ledger, b'{"phase": 7}', fsync=False)
        store.append_line(self.ts.team.ledger, b"[1,2,3]", fsync=False)
        self.ledger.record_result("a1", L.RESULT_TRANSIENT)
        # torn final fragment without a newline
        with open(self.ts.team.ledger, "ab") as fh:
            fh.write(b'{"phase":"result","id":"a1","result":"lan')
        phases = [obj["phase"] for obj in self.ledger.replay()]
        self.assertEqual(phases, ["intent", "result"])
        # the next append heals the missing newline and stays readable
        self.ledger.record_outcome("a1", False, None)
        phases = [obj["phase"] for obj in self.ledger.replay()]
        self.assertEqual(phases, ["intent", "result", "outcome"])

    def test_counts_include_wrong_target_and_every_result_key(self):
        counts = self.ledger.counts()
        for key in L.RESULTS:
            self.assertEqual(counts[key], 0)
        self.assertEqual(counts["intents"], 0)
        self.ledger.record_intent(attempt("a1"))
        self.ledger.record_result("a1", L.RESULT_LANDED_IN_TURN)
        self.ledger.record_intent(attempt("a2", attempts=2))
        self.ledger.record_result("a2", L.RESULT_LANDED_WORKING)
        self.ledger.record_outcome("a2", True, 500.0)
        self.ledger.record_intent(attempt("a3", attempts=3))
        self.ledger.record_wrong_target("alpha-reviewer", "term_owner", "wA:p6", "terminal not in roster")
        counts = self.ledger.counts()
        self.assertEqual(counts["intents"], 3)
        self.assertEqual(counts["landed_in_turn"], 1)
        self.assertEqual(counts["landed_working"], 1)
        self.assertEqual(counts["outcomes"], 1)
        self.assertEqual(counts["cursor_advanced"], 1)
        self.assertEqual(counts["open_intents"], 1)
        self.assertEqual(counts["wrong_target"], 1)
        wrong = [obj for obj in self.ledger.replay() if obj["phase"] == "wrong_target"]
        self.assertEqual(wrong[0]["pane_id"], "wA:p6")
        self.assertEqual(wrong[0]["result"], "wrong_target")

    def test_clean_rate_needs_a_full_window(self):
        for i in range(19):
            self.ledger.record_intent(attempt("c{}".format(i)))
            self.ledger.record_result("c{}".format(i), L.RESULT_LANDED_WORKING)
            self.ledger.record_outcome("c{}".format(i), True, 1000.0)
        self.assertIsNone(self.ledger.clean_rate("codex"))
        self.ledger.record_intent(attempt("c19", attempts=2))
        self.ledger.record_result("c19", L.RESULT_LANDED_WORKING)
        self.ledger.record_outcome("c19", True, 1000.0)
        self.assertAlmostEqual(self.ledger.clean_rate("codex"), 0.95)
        stats = self.ledger.stats()
        self.assertEqual(stats["kinds"]["codex"]["round_trips"], 20)
        self.assertEqual(stats["kinds"]["codex"]["clean"], 19)
        self.assertTrue(stats["kinds"]["codex"]["verified"])
        self.assertIsNone(self.ledger.clean_rate("claude"))
        self.assertEqual(stats["path"], os.fspath(self.ts.team.ledger))

    def test_is_clean_rules(self):
        self.assertTrue(L.Ledger.is_clean({"result": "landed_working", "cursor_advanced": True, "cursor_latency_ms": 1000.0, "attempts": 1}))
        self.assertFalse(L.Ledger.is_clean({"result": "landed_working", "cursor_advanced": True, "cursor_latency_ms": 400000.0, "attempts": 1}))
        self.assertFalse(L.Ledger.is_clean({"result": "landed_working", "cursor_advanced": True, "attempts": 2}))
        self.assertFalse(L.Ledger.is_clean({"result": "landed_in_turn", "cursor_advanced": True, "attempts": 1}))
        self.assertFalse(L.Ledger.is_clean({"result": "landed_working", "cursor_advanced": False, "attempts": 1}))

    def test_dry_results_do_not_count_as_round_trips(self):
        for i in range(25):
            self.ledger.record_intent(attempt("d{}".format(i)))
            self.ledger.record_result("d{}".format(i), L.RESULT_DRY)
        self.assertIsNone(self.ledger.clean_rate("codex"))
        self.assertEqual(self.ledger.counts()["dry"], 25)

    def test_empty_ledger_replays_nothing(self):
        self.assertEqual(list(self.ledger.replay()), [])
        self.assertEqual(self.ledger.open_intents(), [])
        self.assertEqual(self.ledger.stats()["kinds"], {})


if __name__ == "__main__":
    unittest.main()

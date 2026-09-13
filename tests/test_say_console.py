"""The console side of ``say``: ``!name text`` parsing, the members-only ``!`` menu, outcome tags, the status watch."""

from __future__ import annotations

import time
import unittest
from datetime import timedelta
from typing import Any, Dict, List

from herdr_team import console, store
from herdr_team import tui_model as tm
from herdr_team.tui_model import Intent
from support import FakeApi, TempState
from test_mention import MEMBERS, console as console_model, type_keys
from test_tui_model import NOW, drive, iso, model_with, record, system_record, who_doc


def direct(seq: int, text: str = "fix it", to: str = "alpha-worker", **overrides: Any) -> Dict[str, Any]:
    rec = record(seq, **{"from": "human", "from_kind": "human", "from_pane": "w7:p1", "from_terminal": "term_console", "kind": "direct", "text": text, "to": [to],
                         "origin": {"via": "console", "verified": True}, "force": False})
    rec.update(overrides)
    return rec


def typed(seq: int, for_seq: int, result: str = "typed", reason: Any = None, **extra: Any) -> Dict[str, Any]:
    fields = {"to": ["human"], "seqs": [for_seq], "reply_to": for_seq, "member": "alpha-worker", "kind_of_member": "claude", "result": result, "reason": reason,
              "detail": None, "force": False, "force_verified": None}
    fields.update(extra)
    return system_record(seq, "typed", "typed #{} into alpha-worker".format(for_seq), **fields)


# --------------------------------------------------------------------------
# parsing


class BangParsingTests(unittest.TestCase):
    def test_say_lines(self):
        cases = [
            ("!alpha-worker fix the test", {"member": "alpha-worker", "text": "fix the test", "force": False, "team": "alpha"}),
            ("!!alpha-worker stop, fix the test", {"member": "alpha-worker", "text": "stop, fix the test", "force": True, "team": "alpha"}),
            ("!!all stop and summarize", {"member": "all", "text": "stop and summarize", "force": True, "team": "alpha"}),
            ("  !alpha-worker  two  spaces ", {"member": "alpha-worker", "text": "two  spaces", "force": False, "team": "alpha"}),
            ("!alpha-worker @foo look at this", {"member": "alpha-worker", "text": "@foo look at this", "force": False, "team": "alpha"}),
            ("!alpha-worker /compact", {"member": "alpha-worker", "text": "/compact", "force": False, "team": "alpha"}),
        ]
        for line, args in cases:
            with self.subTest(line):
                intent = tm.parse_input_line(line, "alpha")
                self.assertEqual(intent.kind, "say")
                self.assertEqual(intent.args, args)

    def test_every_bang_line_is_a_say_attempt_and_never_a_post(self):
        for line in ("!", "!!", "!!! urgent", "! wow", "!?", "!Name x", "!alpha-worker", "!alpha-worker   ", "!role:qa text", "!all text",
                     "!human text", "!me text", "!bad!name text", "!alpha-worker a\nb"):
            with self.subTest(line):
                intent = tm.parse_input_line(line, "alpha")
                self.assertEqual(intent.kind, "error", line)
                self.assertNotEqual(intent.kind, "post")

    def test_errors_carry_the_hint_or_the_usage(self):
        self.assertIn(tm.BANG_HINT, tm.parse_input_line("!Name x", "alpha").args["message"])
        self.assertIn(tm.BANG_HINT, tm.parse_input_line("!!! urgent", "alpha").args["message"])
        self.assertTrue(tm.parse_input_line("!alpha-worker", "alpha").args["message"].startswith("usage: !alpha-worker <text>"))
        self.assertIn("one member, not a role", tm.parse_input_line("!role:qa x", "alpha").args["message"])
        self.assertIn("use !!all", tm.parse_input_line("!all x", "alpha").args["message"])
        self.assertIn("one line", tm.parse_bang("!a b\nc")[1])
        self.assertEqual(tm.parse_bang("hello"), (None, None))
        self.assertEqual(tm.parse_bang("!!a b c"), (tm.SaySpec("a", "b c", True), None))

    def test_text_that_starts_with_a_bang_is_posted_through_a_directive(self):
        intent = tm.parse_input_line("/all !text", "alpha")
        self.assertEqual((intent.kind, intent.args["to"], intent.args["text"]), ("post", ["all"], "!text"))
        intent = tm.parse_input_line("@alpha-worker !text", "alpha")
        self.assertEqual((intent.kind, intent.args["to"], intent.args["text"]), ("post", ["alpha-worker"], "!text"))

    def test_help_mentions_the_sign(self):
        self.assertIn("!name text", tm.HELP_TEXT)
        self.assertIn("!!name", tm.HELP_TEXT)
        self.assertIn("!!all", tm.HELP_TEXT)


class ApplySayTests(unittest.TestCase):
    def test_enter_yields_say_and_clears_the_input_without_a_confirm(self):
        model = console_model()
        drive(model, "!red-dev-tester fix it")
        intent = tm.apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "say")
        self.assertEqual(intent.args, {"member": "red-dev-tester", "text": "fix it", "force": False, "team": "red-dev"})
        self.assertEqual((model.input, model.cursor, model.pending_confirm), ("", 0, None))
        drive(model, "!!red-dev-tester go")
        intent = tm.apply_key(model, "ENTER")
        self.assertEqual((intent.kind, intent.args["force"]), ("say", True))
        self.assertIsNone(model.pending_confirm)

        drive(model, "!!all finish your current task")
        intent = tm.apply_key(model, "ENTER")
        self.assertEqual((intent.kind, intent.args["member"], intent.args["force"]), ("say", "all", True))
        self.assertIsNone(model.pending_confirm)

    def test_unknown_or_left_member_is_an_error_with_the_hint(self):
        for name in ("ghost", "red-dev-old", "red"):
            model = console_model()
            drive(model, "!{} hi".format(name))
            intent = tm.apply_key(model, "ENTER")
            self.assertEqual(intent.kind, "error", name)
            self.assertIn("no member {}".format(name), model.status)
            self.assertIn("/all !", model.status)

    def test_empty_roster_defers_to_the_cli(self):
        model = console_model(members=[])
        drive(model, "!ghost hi")
        self.assertEqual(tm.apply_key(model, "ENTER").kind, "say")


# --------------------------------------------------------------------------
# the ! menu


class BangMenuTests(unittest.TestCase):
    def test_bang_and_double_bang_open_with_their_sigil(self):
        self.assertEqual(tm.mention_context("!", 1), (0, 1, "", "!"))
        self.assertEqual(tm.mention_context("!!re", 4), (0, 4, "re", "!!"))
        self.assertEqual(tm.mention_context("  !re", 5), (2, 5, "re", "!"))
        self.assertEqual(tm.mention_context("!!", 2), (0, 2, "", "!!"))

    def test_non_say_bangs_do_not_open_the_menu(self):
        for text in ("wow!", "foo!bar", "hello !re", "@x !re", "!!!x", "!Name", "!?"):
            with self.subTest(text):
                self.assertIsNone(tm.mention_context(text, len(text)))

    def test_members_only_drops_groups_but_force_all_adds_the_broadcast(self):
        rows = tm.mention_candidates(MEMBERS, "", members_only=True)
        self.assertEqual([r["insert"] for r in rows], ["red-dev-claude-dev", "red-dev-codex-reviewer", "red-dev-tester"])
        rows = tm.mention_candidates(MEMBERS, "rev", members_only=True)
        self.assertEqual([r["insert"] for r in rows], ["red-dev-codex-reviewer", "red-dev-tester"])
        self.assertEqual(tm.mention_candidates(MEMBERS, "al", members_only=True), [])
        self.assertEqual([r["insert"] for r in tm.mention_candidates(MEMBERS, "al", members_only=True, force_all=True)], ["all"])
        self.assertTrue(any(r["insert"] == "all" for r in tm.mention_candidates(MEMBERS, "al")))

    def test_bang_opens_a_members_only_menu_and_tab_keeps_the_sigil(self):
        model = console_model()
        type_keys(model, "!")
        self.assertEqual([r["insert"] for r in tm.mention_menu(model)], ["red-dev-claude-dev", "red-dev-codex-reviewer", "red-dev-tester"])
        lines = tm.mention_lines(model, 100)
        self.assertTrue(lines[0].startswith(tm.MENTION_MARKER + " !red-dev-claude-dev"), lines[0])
        self.assertIn("working", lines[0])
        tm.apply_key(model, "TAB")
        self.assertEqual(model.input, "!red-dev-claude-dev ")
        model = console_model()
        type_keys(model, "!!co")
        tm.apply_key(model, "TAB")
        self.assertEqual(model.input, "!!red-dev-codex-reviewer ")
        type_keys(model, "stop")
        intent = tm.apply_key(model, "ENTER")
        self.assertEqual((intent.kind, intent.args["force"], intent.args["text"], intent.args["member"]), ("say", True, "stop", "red-dev-codex-reviewer"))

        model = console_model()
        type_keys(model, "!!al")
        self.assertEqual([r["insert"] for r in tm.mention_menu(model)], ["all"])
        tm.apply_key(model, "TAB")
        self.assertEqual(model.input, "!!all ")

    def test_enter_without_text_accepts_the_completion_then_errors(self):
        model = console_model()
        type_keys(model, "!red-dev-tester")
        self.assertIsNone(tm.apply_key(model, "ENTER"))
        self.assertEqual(model.input, "!red-dev-tester ")
        intent = tm.apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "error")
        self.assertIn("usage: !red-dev-tester <text>", model.status)

    def test_bang_menu_rows_are_all_member_styled(self):
        model = console_model()
        type_keys(model, "!")
        styled = tm.mention_rows_styled(model, 100)
        self.assertEqual(styled[0][1], tm.STYLE_MENU_SELECTED)
        for _line, style in styled[1:]:
            self.assertTrue(style.startswith("member:"), style)
        self.assertNotIn(tm.STYLE_MENU, [s for _, s in styled])

    def test_compose_refuses_bang_lines_and_offers_no_names_for_them(self):
        model = tm.ComposeModel(default_to="red-dev-tester", team="red-dev", roster_members=list(MEMBERS))
        for ch in "!":
            tm.compose_apply_key(model, ch)
        self.assertEqual(tm.mention_menu(model), [])
        for ch in "red-dev-tester go":
            tm.compose_apply_key(model, ch)
        intent = tm.compose_apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "error")
        self.assertIn("console-only", model.status)
        model = tm.ComposeModel(default_to="red-dev-tester", team="red-dev", roster_members=list(MEMBERS))
        for ch in "!!! wow":
            tm.compose_apply_key(model, ch)
        self.assertEqual(tm.compose_apply_key(model, "ENTER").kind, "error")
        model = tm.ComposeModel(default_to="red-dev-tester", team="red-dev", roster_members=list(MEMBERS))
        for ch in "@red-dev-tester !wow":
            tm.compose_apply_key(model, ch)
        intent = tm.compose_apply_key(model, "ENTER")
        self.assertEqual((intent.kind, intent.args["text"]), ("post", "!wow"))


# --------------------------------------------------------------------------
# outcomes in the feed


class TypedReceiptTests(unittest.TestCase):
    def test_typed_outcome_attaches_to_the_direct_entry_only(self):
        members = who_doc()["teams"]["alpha"]["members"]
        cursors = {"alpha-worker": {"seq": 20}}
        receipts = tm.derive_receipts([direct(12), typed(13, 12, "typed", "in_turn"), record(14)], cursors, members)
        self.assertEqual(receipts[12]["typed"]["result"], "typed")
        self.assertEqual(receipts[12]["typed"]["reason"], "in_turn")
        self.assertEqual((receipts[12]["nudged"], receipts[12]["read"], receipts[12]["read_by"]), ([], [], None))
        self.assertNotIn(13, receipts)
        self.assertIsNone(receipts[14]["typed"])

    def test_newest_typed_record_wins_and_text_hashes_count(self):
        outcomes = tm.typed_outcomes([typed(13, 12, "refused", "working"), typed(15, 12, "typed", None)])
        self.assertEqual(outcomes[12]["result"], "typed")
        outcomes = tm.typed_outcomes([system_record(9, "typed", "typed #7 into alpha-worker", to=["human"], result="typed", reason=None)])
        self.assertEqual(outcomes[7]["result"], "typed")


class TypedLabelTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ({"result": "typed", "reason": None}, (True, "typed", "✓typed", "+typed")),
            ({"result": "typed", "reason": "in_turn", "force_verified": True}, (True, "typed (in running turn)", "✓typed (in running turn)", "+typed (in running turn)")),
            ({"result": "typed", "reason": "in_turn", "force_verified": False, "kind": "gemini"}, (True, "typed (in running turn, unverified for gemini)", "✓typed (in running turn, unverified for gemini)", "+typed (in running turn, unverified for gemini)")),
            ({"result": "typed", "reason": "dry"}, (True, "typed (dry run)", "✓typed (dry run)", "+typed (dry run)")),
            ({"result": "refused", "reason": "working"}, (False, "not typed (working)", "✗ not typed (working)", "x not typed (working)")),
            ({"result": "refused", "reason": "muted"}, (False, "not typed (muted)", "✗ not typed (muted)", "x not typed (muted)")),
            ({"result": "refused", "reason": "dialog"}, (False, "not typed (dialog)", "✗ not typed (dialog)", "x not typed (dialog)")),
            ({"result": "refused", "reason": "draft"}, (False, "not typed (draft)", "✗ not typed (draft)", "x not typed (draft)")),
            ({"result": "refused", "reason": "skip_state_update"}, (False, "not typed (overlay open)", "✗ not typed (overlay open)", "x not typed (overlay open)")),
            ({"result": "refused", "reason": "blocked"}, (False, "blocked", "✗ blocked", "x blocked")),
            ({"result": "refused", "reason": "absent"}, (False, "not typed (absent)", "✗ not typed (absent)", "x not typed (absent)")),
            ({"result": "refused", "reason": "wrong_occupant"}, (False, "not typed (absent)", "✗ not typed (absent)", "x not typed (absent)")),
            ({"result": "refused", "reason": "state_changed"}, (False, "not typed (state changed; retry)", "✗ not typed (state changed; retry)", "x not typed (state changed; retry)")),
            ({"result": "refused", "reason": "update_required"}, (False, "not typed (update Herdr)", "✗ not typed (update Herdr)", "x not typed (update Herdr)")),
            ({"result": "refused", "reason": "capability_unavailable"}, (False, "safe ! unavailable; use @name", "✗ safe ! unavailable; use @name", "x safe ! unavailable; use @name")),
            ({"result": "refused", "reason": "in_flight"}, (False, "not typed (busy)", "✗ not typed (busy)", "x not typed (busy)")),
            ({"result": "refused", "reason": "unverified_source"}, (False, "refused (unverified source)", "✗ refused (unverified source)", "x refused (unverified source)")),
            ({"result": "refused", "reason": "weird"}, (False, "not typed (weird)", "✗ not typed (weird)", "x not typed (weird)")),
            ({"result": "not_submitted", "reason": "not_submitted"}, (False, "not submitted", "✗ not submitted", "x not submitted")),
            ({"result": "failed", "reason": "hung"}, (False, "failed (pane hung)", "✗ failed (pane hung)", "x failed (pane hung)")),
            ({"result": "failed", "reason": "unconfirmed"}, (False, "unconfirmed", "✗ unconfirmed", "x unconfirmed")),
            ({"result": "failed", "reason": "transient", "detail": "agent_not_ready: launching"}, (False, "failed (agent_not_ready: launch…)", "✗ failed (agent_not_ready: launch…)", "x failed (agent_not_ready: launch…)")),
            (None, (False, "failed (unknown)", "✗ failed (unknown)", "x failed (unknown)")),
        ]
        for outcome, (ok, label, tag, ascii_tag) in cases:
            with self.subTest(outcome):
                self.assertEqual(tm.typed_label(outcome), (ok, label))
                self.assertEqual(tm.typed_tag(outcome), tag)
                self.assertEqual(tm.typed_tag(outcome, ascii_only=True), ascii_tag)
        tag = tm.typed_tag({"result": "refused", "reason": "\x1b[31mred\x1b[0m"})
        self.assertNotIn("\x1b", tag)


class DirectFeedTests(unittest.TestCase):
    def test_direct_entry_shows_glyph_kind_text_and_outcome(self):
        feed = tm.build_feed([direct(12), typed(13, 12)], members=who_doc()["teams"]["alpha"]["members"], width=120, now=NOW)
        entry = feed[0]
        self.assertEqual((entry["kind"], entry["from"], entry["to"]), ("direct", "human", ["alpha-worker"]))
        self.assertIn("»direct", entry["line"])
        self.assertIn("fix it", entry["line"])
        self.assertTrue(entry["line"].endswith("✓typed"), entry["line"])
        self.assertEqual(entry["tail"], "✓typed")
        ascii_feed = tm.build_feed([direct(12), typed(13, 12)], ascii_only=True, width=120, now=NOW)
        self.assertIn(">>direct", ascii_feed[0]["line"])
        self.assertTrue(ascii_feed[0]["line"].endswith("+typed"))
        typed_row = feed[1]
        self.assertIn("system typed:", typed_row["line"])
        self.assertEqual(tm.entry_style(typed_row), tm.STYLE_SYSTEM)
        self.assertEqual(tm.entry_style(entry), tm.STYLE_HUMAN)
        self.assertTrue(tm.filter_matches(entry, "human"))
        self.assertFalse(tm.filter_matches(entry, "requests"))
        self.assertTrue(tm.filter_matches(typed_row, "system"))

    def test_no_outcome_yet_reads_typing_then_no_outcome(self):
        fresh = direct(12, ts=iso(NOW - timedelta(seconds=2)))
        feed = tm.build_feed([fresh], width=120, now=NOW)
        self.assertTrue(feed[0]["line"].endswith("… typing"), feed[0]["line"])
        stale = direct(12, ts=iso(NOW - timedelta(seconds=30)))
        feed = tm.build_feed([stale], width=120, now=NOW)
        self.assertTrue(feed[0]["line"].endswith("✗ no outcome (notifier?)"), feed[0]["line"])
        feed = tm.build_feed([stale], ascii_only=True, width=120, now=NOW)
        self.assertTrue(feed[0]["line"].endswith("x no outcome (notifier?)"))

    def test_refused_outcome_and_narrow_widths(self):
        feed = tm.build_feed([direct(12), typed(13, 12, "refused", "working")], width=120, now=NOW)
        self.assertTrue(feed[0]["line"].endswith("✗ not typed (working)"))
        feed = tm.build_feed([direct(12), typed(13, 12, "refused", "working")], width=40, now=NOW)
        self.assertNotIn("not typed", feed[0]["line"])  # receipt tags need 60 columns; the status line carries the outcome
        self.assertEqual(feed[0]["tail"], "")

    def test_render_keeps_exact_height_and_width_with_direct_entries(self):
        long_text = " ".join("w{}".format(i) for i in range(40))
        for width, height in ((40, 12), (60, 20), (100, 30), (25, 8)):
            model = model_with([direct(1, long_text), typed(2, 1, "refused", "dialog"), direct(3), record(4)], width=width, height=height)
            screen = tm.render_console(model, width, height)
            self.assertEqual(len(screen), height, (width, height))
            for line in screen:
                self.assertLessEqual(tm.display_width(line), width, (width, line))

    def test_direct_tag_follows_the_last_wrapped_row(self):
        long_text = " ".join("word{}".format(i) for i in range(30))
        entry = tm.feed_entry(direct(13, long_text), receipts={13: {"nudged": [], "read": [], "read_by": None, "typed": {"result": "refused", "reason": "dialog"}}}, width=60, now=NOW)
        rows = tm.entry_rows(entry, 60)
        self.assertGreater(len(rows), 2)
        self.assertIn("✗ not typed (dialog)", rows[-1])
        for row in rows:
            self.assertLessEqual(tm.display_width(row), 60)


class RosterSayTests(unittest.TestCase):
    def test_a_say_in_flight_shows_on_the_roster_row(self):
        member = {"name": "alpha-worker", "kind": "claude", "role": "worker", "agent_status": "idle", "status": "active", "pane_id": "w2:p2", "say": "confirming"}
        self.assertIn("» typing", tm.roster_line(member, width=120))
        self.assertIn(">> typing", tm.roster_line(member, width=120, ascii_only=True))
        member["say"] = None
        self.assertNotIn("typing", tm.roster_line(member, width=120))


# --------------------------------------------------------------------------
# the console runtime


class ConsoleRuntimeSayTests(unittest.TestCase):
    def run_with_cli(self, ts, fake_run_cli, intent):
        original = console.run_cli
        console.run_cli = fake_run_cli
        try:
            state = console.ConsoleState(ts.layout, "alpha", ts.env)
            model = console.build_model(ts.layout, "alpha", state, env=ts.env)
            console.execute_intent(intent, model, state, FakeApi())
            return model
        finally:
            console.run_cli = original

    def test_say_goes_through_the_cli_without_waiting_and_is_watched(self):
        with TempState() as ts:
            calls: List[List[str]] = []

            def fake_run_cli(args, env, timeout=0.0):
                calls.append(list(args))
                return 0, {"team": "alpha", "member": "alpha-worker", "seq": 12, "job": "j3", "outcome": None}, None

            model = self.run_with_cli(ts, fake_run_cli, Intent("say", {"member": "alpha-worker", "text": "fix it", "force": True, "team": "alpha"}))
            self.assertEqual(calls, [["--team", "alpha", "say", "--no-wait", "--force", "--", "alpha-worker", "fix it"]])
            self.assertEqual(model.status, "typing into alpha-worker… #12")
            self.assertEqual(model.watching_say[12][0], "alpha-worker")
            calls.clear()
            model = self.run_with_cli(ts, fake_run_cli, Intent("say", {"member": "alpha-worker", "text": "-v please", "force": False, "team": "alpha"}))
            self.assertEqual(calls, [["--team", "alpha", "say", "--no-wait", "--", "alpha-worker", "-v please"]])

    def test_say_all_watches_every_fanned_out_delivery(self):
        with TempState() as ts:
            calls: List[List[str]] = []

            def fake_run_cli(args, env, timeout=0.0):
                calls.append(list(args))
                return 0, {"team": "alpha", "member": "all", "deliveries": [
                    {"member": "alpha-reviewer", "seq": 12, "job": "j1", "outcome": None},
                    {"member": "alpha-worker", "seq": 13, "job": "j2", "outcome": None},
                ]}, None

            intent = Intent("say", {"member": "all", "text": "finish", "force": True, "team": "alpha"})
            model = self.run_with_cli(ts, fake_run_cli, intent)
            self.assertEqual(calls, [["--team", "alpha", "say", "--no-wait", "--force", "--", "all", "finish"]])
            self.assertEqual(model.status, "typing into all 2 agents… #12, #13")
            self.assertEqual(model.watching_say[12][0], "alpha-reviewer")
            self.assertEqual(model.watching_say[13][0], "alpha-worker")

    def test_refusals_map_to_status_hints(self):
        with TempState() as ts:
            for code in ("author_mismatch", "member_not_found", "echo_rejected", "secret_detected", "daemon_down", "say_unverified", "say_control_command", "kind_unverified"):
                model = self.run_with_cli(ts, lambda args, env, timeout=0.0: (1, None, {"code": code, "message": "m"}), Intent("say", {"member": "alpha-worker", "text": "x", "force": False}))
                self.assertTrue(model.status.startswith("say to alpha-worker refused:"), model.status)
                self.assertTrue(model.status.endswith("({})".format(code)), model.status)
                self.assertEqual(model.watching_say, {})
            model = self.run_with_cli(ts, lambda args, env, timeout=0.0: (1, None, {"code": "text_too_long", "message": "m"}), Intent("say", {"member": "alpha-worker", "text": "x"}))
            self.assertEqual(model.status, "say to alpha-worker failed: text_too_long: m")
            self.assertIn("!!alpha-worker text forces", console.say_failure_status({"code": "say_control_command"}, "alpha-worker"))

    def test_refresh_settles_a_say_watch_into_the_status(self):
        with TempState() as ts:
            store.write_json(ts.session.who_json, who_doc())
            board = store.BoardStore(ts.team)
            first = board.append({"from": "human", "from_kind": "human", "from_terminal": "term_console", "origin": {"via": "console", "verified": True}, "to": ["alpha-worker"], "kind": "direct", "text": "fix it"})
            state = console.ConsoleState(ts.layout, "alpha", ts.env)
            model = console.build_model(ts.layout, "alpha", state, env=ts.env)
            sent = time.monotonic()
            model.watching_say = {first: ("alpha-worker", sent)}
            console.refresh(model, ts.layout, state)
            self.assertEqual(model.watching_say, {first: ("alpha-worker", sent)})  # no outcome yet, still watching
            board.append({"from": "system", "kind": "system", "event": "typed", "text": "typed #{} into alpha-worker".format(first), "to": ["human"], "origin": {"via": "system", "verified": True},
                          "seqs": [first], "reply_to": first, "member": "alpha-worker", "result": "typed", "reason": None})
            console.refresh(model, ts.layout, state)
            self.assertEqual(model.status, "say #{} to alpha-worker: typed".format(first))
            self.assertEqual(model.watching_say, {})
            second = board.append({"from": "human", "from_kind": "human", "from_terminal": "term_console", "origin": {"via": "console", "verified": True}, "to": ["alpha-worker"], "kind": "direct", "text": "again"})
            board.append({"from": "system", "kind": "system", "event": "typed", "text": "#{} not typed into alpha-worker: working".format(second), "to": ["human"], "origin": {"via": "system", "verified": True},
                          "seqs": [second], "reply_to": second, "member": "alpha-worker", "result": "refused", "reason": "working"})
            model.watching_say = {second: ("alpha-worker", time.monotonic())}
            console.refresh(model, ts.layout, state)
            self.assertEqual(model.status, "say #{} to alpha-worker: not typed (working) (!!alpha-worker text forces)".format(second))
            self.assertTrue(any(e["kind"] == "direct" and "✗ not typed (working)" in e["line"] for e in model.feed if model.width >= 60) or model.width < 60)
            model.watching_say = {99: ("alpha-worker", 0.0)}
            self.assertIn("no outcome after 30s", tm.settle_say_watch(model, [], now_mono=100.0))
            self.assertEqual(model.watching_say, {})

    def test_watch_survives_a_rebuild(self):
        model = model_with([record(1)])
        model.watching_say = {1: ("alpha-worker", 5.0)}
        rebuilt = tm.build_console_model("alpha", who_doc(), [record(1)], previous=model)
        self.assertEqual(rebuilt.watching_say, {1: ("alpha-worker", 5.0)})


if __name__ == "__main__":
    unittest.main()

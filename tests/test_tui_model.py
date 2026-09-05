"""Console, picker, and compose models (plan 7.3, 11, 7.1e) plus the curses-free runtime helpers."""

from __future__ import annotations

import argparse
import io
import json
import os
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from support import FAKE_AGENTS, FAKE_MEMBERS, PLUGIN_ROOT, FakeApi, TempState, fake_agent

from herdr_team import compose, console, paths, picker, store, tui_model
from herdr_team.errors import HerdrTeamError
from herdr_team.tui_model import (
    ComposeModel,
    ConsoleHeader,
    ConsoleModel,
    Intent,
    PickerModel,
    PickerRow,
    apply_key,
    box,
    build_console_model,
    build_feed,
    compose_apply_key,
    derive_receipts,
    filter_matches,
    header_lines,
    merge_audit_warnings,
    parse_input_line,
    picker_apply_key,
    picker_rows_from_agent_list,
    render_console,
    roster_line,
    suggest_name,
    visible_feed,
)

FIXTURES = PLUGIN_ROOT / "tests" / "fixtures"
NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def record(seq: int, **overrides: Any) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "v": 1, "seq": seq, "ts": iso(NOW - timedelta(minutes=60 - seq)), "from": "alpha-worker", "from_label": None,
        "from_kind": "claude", "from_pane": "w2:p2", "from_terminal": "term_w1", "from_gen": 1,
        "origin": {"via": "cli", "verified": True, "pid": 1, "ppid": 1, "workspace_id": "w2", "tab_id": "w2:t1", "socket": "/s"},
        "to": ["human"], "to_role": None, "kind": "note", "text": "post {}".format(seq), "refs": [], "reply_to": None,
        "retracts": None, "supersedes": None, "urgent": False, "ttl_ms": None, "truncated": False, "event": None, "relayed_for": None,
    }
    rec.update(overrides)
    return rec


def system_record(seq: int, event: str, text: str, **overrides: Any) -> Dict[str, Any]:
    return record(seq, **{"from": "system", "from_kind": "system", "kind": "system", "event": event, "text": text, "to": ["all"], "origin": {"via": "system", "verified": True}, **overrides})


def who_doc(members: Optional[List[Dict[str, Any]]] = None, charter: Optional[Dict[str, Any]] = None, toasts: str = "terminal") -> Dict[str, Any]:
    if members is None:
        members = [
            {"name": "alpha-reviewer", "role": "reviewer", "kind": "codex", "status": "active", "agent_status": "idle", "pane_id": "w2:p1",
             "terminal_id": "term_r1", "last_headline": None, "pending_nudges": 0, "muted_until": None, "verified_kind": True, "delivery": "nudge"},
            {"name": "alpha-worker", "role": "worker", "kind": "claude", "status": "active", "agent_status": "working", "pane_id": "w2:p2",
             "terminal_id": "term_w1", "last_headline": "→ patching session.rs", "pending_nudges": 2, "muted_until": None, "verified_kind": True, "delivery": "nudge"},
        ]
    if charter is None:
        charter = {"seq": 3, "headline": "Find and fix the session-isolation bug", "refs": []}
    return {"v": 1, "daemon_beat_at": iso(NOW), "socket": "/s", "default_team": "alpha", "toasts": toasts,
            "charters": {"alpha": charter}, "teams": {"alpha": {"members": members}}}


def model_with(records: List[Dict[str, Any]], width: int = 80, height: int = 24, **kwargs: Any) -> ConsoleModel:
    return build_console_model("alpha", who_doc(), records, width=width, height=height, now=NOW, **kwargs)


def drive(model: ConsoleModel, text: str) -> None:
    for ch in text:
        apply_key(model, ch)


# --------------------------------------------------------------------------
# header (plan 7.3, exact)


class HeaderTests(unittest.TestCase):
    def test_exact_header_lines(self):
        model = model_with([record(41), record(42, to=["all"])], view_on=True)
        lines = header_lines(model.header, 80)
        self.assertEqual(lines[0], "team alpha · 2 members · view:on · nudges:on · toasts:terminal · unread(you):2")
        self.assertEqual(lines[1], "charter #3: Find and fix the session-isolation bug")

    def test_unread_counts_only_posts_to_human_or_all_past_the_cursor(self):
        recs = [record(1), record(2, to=["alpha-reviewer"]), record(3, to=["all"]), record(4, **{"from": "human", "to": ["all"]}), system_record(5, "nudged", "x")]
        cursors = {"human@human": {"v": 1, "seq": 1}}
        model = model_with(recs, cursors=cursors)
        self.assertEqual(model.header.unread, 1)  # only #3

    def test_nudges_paused_label_and_charter_none(self):
        mutes = {"*": iso(NOW + timedelta(minutes=7, seconds=30))}
        who = who_doc(charter={})
        who["charters"] = {}
        model = build_console_model("alpha", who, [], mutes=mutes, now=NOW)
        lines = header_lines(model.header, 100)
        self.assertEqual(lines[0], "team alpha · 2 members · view:off · nudges:paused (7m) · toasts:terminal · unread(you):0")
        self.assertEqual(tui_model.display_width(header_lines(model.header, 80)[0]), 80)
        self.assertEqual(lines[1], "charter: none")
        self.assertEqual(tui_model.nudges_label({"*": None}), "paused")
        self.assertEqual(tui_model.nudges_label({"*": iso(NOW - timedelta(minutes=1))}, NOW), "on")
        self.assertEqual(tui_model.nudges_label({"alpha-worker": iso(NOW + timedelta(minutes=1))}, NOW), "on")

    def test_view_off_and_toast_mode_override(self):
        model = model_with([], toasts="herdr")
        self.assertEqual(header_lines(model.header, 80)[0], "team alpha · 2 members · view:off · nudges:on · toasts:herdr · unread(you):0")

    def test_width_degradation(self):
        header = ConsoleHeader("alpha", 2, True, "on", "terminal", 3, 3, "Find and fix the session-isolation bug")
        self.assertEqual(header_lines(header, 59)[0], "team alpha · 2 members · nudges:on · unread(you):3")
        self.assertEqual(header_lines(header, 39)[0], "team alpha · 2 · unread:3")
        for width in (80, 59, 39, 20, 8):
            for line in header_lines(header, width):
                self.assertLessEqual(tui_model.display_width(line), width, (width, line))
        self.assertEqual(tui_model.degrade_level(60), 0)
        self.assertEqual(tui_model.degrade_level(59), 1)
        self.assertEqual(tui_model.degrade_level(40), 1)
        self.assertEqual(tui_model.degrade_level(39), 2)


# --------------------------------------------------------------------------
# roster lines (plan 11 who format)


class RosterLineTests(unittest.TestCase):
    def member(self, **over: Any) -> Dict[str, Any]:
        base = {"name": "lead", "kind": "claude", "pane_id": "wA:p6", "status": "active", "agent_status": "working",
                "last_headline": "headline", "pending_nudges": 2, "muted_until": iso(NOW + timedelta(minutes=5))}
        base.update(over)
        return base

    def test_plan_example_line(self):
        self.assertEqual(roster_line(self.member(), 80, now=NOW), '◐ lead  claude  wA:p6  working  "headline"  ↪2  muted')

    def test_ascii_glyphs(self):
        self.assertEqual(roster_line(self.member(), 80, ascii_only=True, now=NOW), '* lead  claude  wA:p6  working  "headline"  ^2  muted')

    def test_headline_cut_to_24_columns_by_display_width(self):
        wide = "漢字" * 20  # 40 wide chars = 80 columns
        line = roster_line(self.member(last_headline=wide, pending_nudges=0, muted_until=None), 120, now=NOW)
        quoted = line.split('"')[1]
        self.assertLessEqual(tui_model.display_width(quoted), 24)
        self.assertTrue(quoted.endswith("…"))
        self.assertEqual(tui_model.headline("abc\x1b[31mdef\x07\nsecond line"), "abcdef")

    def test_status_annotations(self):
        gone = self.member(status="missing", agent_status="unknown", last_seen_at=iso(NOW - timedelta(minutes=3)), pending_nudges=0, muted_until=None, last_headline=None)
        self.assertEqual(roster_line(gone, 80, now=NOW), "? lead  claude  wA:p6  unknown  gone 3m")
        flags = self.member(pending_nudges=0, muted_until=None, last_headline=None, briefed=False, charter_stale=True)
        self.assertEqual(roster_line(flags, 80, now=NOW), "◐ lead  claude  wA:p6  working  unbriefed  charter: stale")
        conflict = self.member(status="name_conflict", pending_nudges=0, muted_until=None, last_headline=None)
        self.assertIn("name conflict", roster_line(conflict, 80, now=NOW))

    def test_global_mute_marks_every_member(self):
        m = self.member(muted_until=None)
        self.assertNotIn("muted", roster_line(m, 80, now=NOW))
        self.assertIn("muted", roster_line(m, 80, mutes={"*": None}, now=NOW))
        self.assertIn("muted", roster_line(m, 80, mutes={"lead": iso(NOW + timedelta(minutes=1))}, now=NOW))
        self.assertNotIn("muted", roster_line(m, 80, mutes={"lead": iso(NOW - timedelta(minutes=1))}, now=NOW))

    def test_narrow_widths_drop_kind_pane_and_headline(self):
        m = self.member()
        self.assertEqual(roster_line(m, 50, now=NOW), "◐ lead  claude  wA:p6  working  ↪2  muted")
        self.assertEqual(roster_line(m, 39, now=NOW), "◐ lead  working  ↪2  muted")


# --------------------------------------------------------------------------
# receipts and feed


class ReceiptTests(unittest.TestCase):
    def members(self) -> List[Dict[str, Any]]:
        return [dict(m) for m in FAKE_MEMBERS]

    def test_nudged_from_system_records_and_read_from_cursors(self):
        recs = [
            record(41, **{"from": "human", "to": ["alpha-reviewer"], "origin": {"via": "console", "verified": True}}),
            record(42, **{"from": "human", "to": ["alpha-reviewer"], "origin": {"via": "console", "verified": True}}),
            system_record(43, "nudged", "[herdr-team nudge] 2 new board posts for alpha-reviewer (seq 41-42). Run: herdr-team board --new [n17]", to=["alpha-reviewer"]),
        ]
        cursors = {"alpha-reviewer": {"v": 1, "seq": 41, "surfaced_by": "cli"}}
        receipts = derive_receipts(recs, cursors, self.members())
        self.assertEqual(receipts[41]["nudged"], [recs[2]["ts"]])
        self.assertEqual(receipts[42]["nudged"], [recs[2]["ts"]])
        self.assertEqual(receipts[41]["read"], ["alpha-reviewer"])
        self.assertEqual(receipts[42]["read"], [])
        self.assertIsNone(receipts[41]["read_by"])
        self.assertNotIn(43, receipts)

    def test_read_by_k_of_n_for_all_posts_excludes_author_and_human(self):
        recs = [record(10, to=["all"])]  # from alpha-worker
        cursors = {"alpha-reviewer": {"seq": 10}, "alpha-worker": {"seq": 10}, "human@vitaly": {"seq": 9}}
        receipts = derive_receipts(recs, cursors, self.members())
        self.assertEqual(receipts[10]["read_by"], "1/1")
        self.assertEqual(receipts[10]["read"], ["alpha-reviewer"])
        cursors["human@vitaly"]["seq"] = 10
        receipts = derive_receipts(recs, cursors, self.members())
        self.assertEqual(receipts[10]["read"], ["alpha-reviewer", "human"])
        self.assertEqual(receipts[10]["read_by"], "1/1")

    def test_nudged_seqs_parsing(self):
        self.assertEqual(tui_model.nudged_seqs({"text": "seq 41-42"}), [41, 42])
        self.assertEqual(tui_model.nudged_seqs({"text": "1 new board post for x (seq 7)."}), [7])
        self.assertEqual(tui_model.nudged_seqs({"seqs": [3, 4], "text": "nudged #5"}), [3, 4, 5])
        self.assertEqual(tui_model.nudged_seqs({"text": "seq 1-5000"}), [])

    def test_corrupt_cursor_counts_as_zero(self):
        receipts = derive_receipts([record(3, to=["alpha-reviewer"])], {"alpha-reviewer": {"seq": "junk"}}, self.members())
        self.assertEqual(receipts[3]["read"], [])


class FeedTests(unittest.TestCase):
    def test_receipt_tags_and_struck_retraction(self):
        recs = [
            record(41, **{"from": "human", "to": ["alpha-reviewer"], "kind": "request", "text": "review this", "origin": {"via": "console", "verified": True}}),
            system_record(42, "nudged", "seq 41", to=["alpha-reviewer"]),
            record(43, to=["all"], text="fyi"),
            record(44, kind="retract", retracts=43, text=""),
        ]
        cursors = {"alpha-reviewer": {"seq": 41}}
        feed = build_feed(recs, cursors, FAKE_MEMBERS, width=120)
        by_seq = {e["seq"]: e for e in feed}
        self.assertNotIn(44, by_seq)  # retract records never render
        self.assertIn("✓nudged", by_seq[41]["line"])
        self.assertIn("✓read", by_seq[41]["line"])
        self.assertIn("→request", by_seq[41]["line"])
        self.assertTrue(by_seq[43]["struck"])
        self.assertIn("~~fyi~~ (retracted by #44)", by_seq[43]["line"])
        self.assertNotIn("read by", by_seq[43]["line"])
        self.assertTrue(by_seq[42]["line"].startswith("#42 "))
        self.assertIn("system nudged:", by_seq[42]["line"])
        ascii_feed = build_feed(recs, cursors, FAKE_MEMBERS, ascii_only=True, width=120)
        self.assertIn("+nudged", [e for e in ascii_feed if e["seq"] == 41][0]["line"])

    def test_audit_warnings_render_in_the_feed(self):
        # HP-08 (2026-09-05): `--as human` from the reviewer pane was refused and audited, but the
        # console showed nothing; plan 7.1 promises a console warning for author_mismatch.
        recs = [record(1, ts="2026-09-05T12:00:00.000Z", to=["all"]), record(2, ts="2026-09-05T12:01:00.000Z", to=["all"])]
        audit = [
            {"ts": "2026-09-05T12:00:39.626Z", "event": "author_mismatch", "author": "alpha-reviewer", "via": "cli", "pane_id": "w1:p2", "details": {"requested": "human", "resolved": "alpha-reviewer"}},
            {"ts": "2026-09-05T12:00:40.000Z", "event": "charter_set", "author": "human", "via": "outside", "pane_id": None, "details": {}},
            {"ts": "2026-09-05T12:02:00.000Z", "event": "pane_mismatch", "author": "\x1b[31mevil\x1b[0m", "via": "cli", "pane_id": "w1:p4", "details": {"claimed_pane_id": "w1:p2"}},
        ]
        feed = merge_audit_warnings(build_feed(recs, width=200), audit, width=200)
        self.assertEqual([e["seq"] for e in feed], [1, None, 2, None])
        self.assertIn("warning: alpha-reviewer tried to post as human (author_mismatch, w1:p2)", feed[1]["line"])
        self.assertTrue(feed[1]["line"].startswith("⚠ "))
        self.assertEqual((feed[1]["kind"], feed[1]["from"], feed[1]["to"]), ("warning", "alpha-reviewer", []))
        self.assertIn("claimed pane w1:p2 (pane_mismatch, w1:p4)", feed[3]["line"])
        self.assertNotIn("\x1b", feed[3]["line"])  # audit author text is sanitized before render
        ascii_feed = merge_audit_warnings(build_feed(recs, width=200), audit, ascii_only=True, width=200)
        self.assertTrue(ascii_feed[1]["line"].startswith("! "))
        # the system filter shows warnings; "human" does not
        self.assertTrue(filter_matches(feed[1], "system"))
        self.assertTrue(filter_matches(feed[1], "all"))
        self.assertFalse(filter_matches(feed[1], "human"))
        # rendering never trips over the None seq
        model = build_console_model("alpha", None, recs, width=200, height=12, audit=audit)
        screen = render_console(model, 200, 12)
        self.assertTrue(any("warning: alpha-reviewer tried to post as human" in line for line in screen), screen)

    def test_read_by_tag_for_all_posts(self):
        recs = [record(7, to=["all"])]
        feed = build_feed(recs, {"alpha-reviewer": {"seq": 7}}, FAKE_MEMBERS, width=120)
        self.assertIn("read by 1/1", feed[0]["line"])

    def test_author_labels(self):
        unverified = record(1, **{"from": "human", "origin": {"via": "cli-unverified", "verified": False}})
        console_ok = record(2, **{"from": "human", "from_label": "vitaly", "origin": {"via": "console", "verified": True}})
        relayed = record(3, relayed_for="human")
        forged = record(4, **{"from": "system", "kind": "note"})
        member_unverified = record(5, origin={"via": "cli", "verified": False})
        lines = {e["seq"]: e["line"] for e in build_feed([unverified, console_ok, relayed, forged, member_unverified], width=200)}
        self.assertIn("human (unverified)", lines[1])
        self.assertIn("human (vitaly)→human", lines[2])
        self.assertNotIn("unverified", lines[2])
        self.assertIn("alpha-worker (relaying for human)", lines[3])
        self.assertIn("system (unverified)", lines[4])
        self.assertIn("alpha-worker (unverified)", lines[5])

    def test_filters_and_scroll(self):
        recs = [record(1, to=["alpha-reviewer"]), record(2, kind="request", to=["all"]), record(3, **{"from": "human", "to": ["all"], "origin": {"via": "console", "verified": True}}), system_record(4, "toast", "x")]
        model = model_with(recs)
        self.assertEqual([e["seq"] for e in visible_feed(model, 10)], [1, 2, 3, 4])
        model.filter_index = tui_model.FILTERS.index("to me")
        self.assertEqual([e["seq"] for e in visible_feed(model, 10)], [2, 3, 4])
        model.filter_index = tui_model.FILTERS.index("requests")
        self.assertEqual([e["seq"] for e in visible_feed(model, 10)], [2])
        model.filter_index = tui_model.FILTERS.index("human")
        self.assertEqual([e["seq"] for e in visible_feed(model, 10)], [3])
        model.filter_index = tui_model.FILTERS.index("system")
        self.assertEqual([e["seq"] for e in visible_feed(model, 10)], [4])
        model.filter_index = 0
        self.assertEqual([e["seq"] for e in visible_feed(model, 2)], [3, 4])
        model.scroll = 1
        self.assertEqual([e["seq"] for e in visible_feed(model, 2)], [2, 3])
        model.scroll = 99
        self.assertEqual([e["seq"] for e in visible_feed(model, 2)], [1, 2])
        self.assertEqual(model.scroll, 2)  # clamped
        self.assertEqual(visible_feed(model, 0), [])


# --------------------------------------------------------------------------
# command parsing


class CommandParsingTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("hello there", "post", {"text": "hello there", "to": [], "kind": "note"}),
            ("@alpha-reviewer please look", "post", {"to": ["alpha-reviewer"], "text": "please look"}),
            ("@alpha-reviewer, @alpha-worker both", "post", {"to": ["alpha-reviewer", "alpha-worker"], "text": "both"}),
            ("@role:qa run the suite", "post", {"to": ["role:qa"], "text": "run the suite"}),
            ("/all stand-up in 5", "post", {"to": ["all"], "text": "stand-up in 5"}),
            ("/human note to self", "post", {"to": ["human"], "text": "note to self"}),
            ("/kind request @alpha-worker fix it", "post", {"kind": "request", "to": ["alpha-worker"], "text": "fix it"}),
            ("/reply 42 looks good", "post", {"reply_to": 42, "text": "looks good"}),
            ("/reply #42 looks good", "post", {"reply_to": 42}),
            ("/urgent @alpha-worker stop", "post", {"urgent": True, "to": ["alpha-worker"], "text": "stop"}),
            ("/ref payloads/1-diff.md see diff", "post", {"refs": ["payloads/1-diff.md"], "text": "see diff"}),
            ("/retract 42", "retract", {"seq": 42, "confirm": False}),
            ("/retract #42", "retract", {"seq": 42}),
            ("/mute alpha-worker 10m", "mute", {"member": "alpha-worker", "for": "10m"}),
            ("/mute", "mute", {"member": None, "for": None}),
            ("/mute --all 5m", "mute", {"member": None, "for": "5m"}),
            ("/pause", "mute", {"member": None}),
            ("/unmute alpha-worker", "unmute", {"member": "alpha-worker"}),
            ("/unmute", "unmute", {"member": None}),
            ("/nudge alpha-worker", "nudge", {"member": "alpha-worker", "force": False}),
            ("/nudge alpha-worker --force", "nudge", {"member": "alpha-worker", "force": True}),
            ("/focus alpha-worker", "focus", {"member": "alpha-worker"}),
            ("/peek alpha-worker", "peek", {"member": "alpha-worker"}),
            ("/who", "who", {"team": "alpha"}),
            ("/filter requests", "filter", {"name": "requests"}),
            ("/filter to me", "filter", {"name": "to me"}),
            ("/filter", "filter", {"name": None}),
            ("/as vitaly.m", "as", {"label": "vitaly.m"}),
            ("/use beta", "use", {"team": "beta"}),
            ("/charter", "charter", {"action": "show"}),
            ("/charter set Ship the fix by Friday", "charter", {"action": "set", "text": "Ship the fix by Friday", "urgent": False}),
            ("/charter set --urgent Stop everything", "charter", {"action": "set", "text": "Stop everything", "urgent": True}),
            ("/remove alpha-worker", "remove", {"member": "alpha-worker", "confirm": False}),
            ("/quit", "quit", {}),
            ("/help", "help", {}),
            ("   ", "none", {}),
        ]
        for line, kind, expect in cases:
            intent = parse_input_line(line, "alpha")
            self.assertEqual(intent.kind, kind, line)
            for key, value in expect.items():
                self.assertEqual(intent.args.get(key), value, (line, key))

    def test_errors(self):
        for line in ("/retract", "/retract x", "/kind bogus text", "/reply x text", "@bad!name text", "@alpha-worker", "/frobnicate", "/use Bad", "/as 'x'", "/charter set", "/mute nope 3d", "/nudge", "/peek a b"):
            self.assertEqual(parse_input_line(line, "alpha").kind, "error", line)

    def test_compose_default_recipient_applied_only_when_no_directive(self):
        spec, err = tui_model.parse_post_directives("hi", "alpha-worker")
        self.assertIsNone(err)
        self.assertEqual(spec.to, ["alpha-worker"])
        spec, _ = tui_model.parse_post_directives("/all hi", "alpha-worker")
        self.assertEqual(spec.to, ["all"])


# --------------------------------------------------------------------------
# key handling on the console model


class ApplyKeyTests(unittest.TestCase):
    def test_typing_enter_posts_with_default_recipient(self):
        model = model_with([])
        drive(model, "hello")
        self.assertEqual(model.input, "hello")
        intent = apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "post")
        self.assertEqual(intent.args["to"], ["all"])
        self.assertEqual(intent.args["text"], "hello")
        self.assertFalse(intent.args["spill"])
        self.assertTrue(intent.args["focused"])
        self.assertEqual(intent.args["label"], "human")
        self.assertEqual(model.input, "")
        model.default_recipient = "alpha-worker"
        drive(model, "again")
        self.assertEqual(apply_key(model, "ENTER").args["to"], ["alpha-worker"])

    def test_reply_defaults_recipient_to_the_original_author(self):
        model = model_with([record(7, **{"from": "alpha-reviewer"})])
        drive(model, "/reply 7 thanks")
        intent = apply_key(model, "ENTER")
        self.assertEqual(intent.args["to"], ["alpha-reviewer"])
        self.assertEqual(intent.args["reply_to"], 7)

    def test_unfocused_post_is_flagged(self):
        model = model_with([])
        model.focused = False
        drive(model, "x")
        intent = apply_key(model, "ENTER")
        self.assertFalse(intent.args["focused"])
        self.assertIn("unfocused", model.status)

    def test_spill_over_2000_chars(self):
        model = model_with([])
        model.input = "a" * 2001
        model.cursor = len(model.input)
        self.assertTrue(apply_key(model, "ENTER").args["spill"])

    def test_alt_enter_and_bracketed_paste_insert_newlines(self):
        model = model_with([])
        drive(model, "one")
        apply_key(model, "ALT_ENTER")
        drive(model, "two")
        self.assertEqual(model.input, "one\ntwo")
        apply_key(model, "PASTE_START")
        self.assertTrue(model.paste_mode)
        apply_key(model, "ENTER")
        self.assertIsNone(apply_key(model, "TAB"))
        drive(model, "three")
        self.assertEqual(model.input, "one\ntwo\nthree")
        self.assertEqual(model.filter_index, 0)
        apply_key(model, "PASTE_END")
        intent = apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "post")
        self.assertEqual(intent.args["text"], "one\ntwo\nthree")

    def test_line_editing_keys(self):
        model = model_with([])
        drive(model, "abcd")
        apply_key(model, "LEFT")
        apply_key(model, "BACKSPACE")
        self.assertEqual(model.input, "abd")
        apply_key(model, "HOME")
        apply_key(model, "DELETE")
        self.assertEqual(model.input, "bd")
        apply_key(model, "END")
        drive(model, "e")
        apply_key(model, "CTRL_A")
        apply_key(model, "RIGHT")
        apply_key(model, "CTRL_K")
        self.assertEqual(model.input, "b")
        apply_key(model, "CTRL_U")
        self.assertEqual((model.input, model.cursor), ("", 0))

    def test_tab_cycles_filters(self):
        model = model_with([])
        intent = apply_key(model, "TAB")
        self.assertEqual((intent.kind, intent.args["name"]), ("filter", "to me"))
        for _ in range(len(tui_model.FILTERS) - 1):
            apply_key(model, "TAB")
        self.assertEqual(model.filter_index, 0)
        drive(model, "/filter system")
        self.assertEqual(apply_key(model, "ENTER").args["name"], "system")
        self.assertEqual(model.filter_index, tui_model.FILTERS.index("system"))
        self.assertIn("[system]", tui_model.filter_line(model))

    def test_retract_and_remove_need_confirmation(self):
        model = model_with([])
        drive(model, "/retract 42")
        self.assertEqual(apply_key(model, "ENTER").kind, "none")
        self.assertEqual(model.status, "retract #42? y/n")
        self.assertIsNone(apply_key(model, "x"))
        self.assertEqual(apply_key(model, "n").kind, "none")
        self.assertIsNone(model.pending_confirm)
        drive(model, "/retract 42")
        apply_key(model, "ENTER")
        confirmed = apply_key(model, "y")
        self.assertEqual((confirmed.kind, confirmed.args["seq"], confirmed.args["confirm"]), ("retract", 42, True))
        drive(model, "/remove alpha-worker")
        apply_key(model, "ENTER")
        self.assertEqual(model.status, "remove alpha-worker? y/n")
        self.assertEqual(apply_key(model, "ENTER").kind, "remove")

    def test_ctrl_c_clears_then_quits_and_scroll_keys(self):
        model = model_with([record(i) for i in range(1, 6)])
        drive(model, "draft")
        self.assertIsNone(apply_key(model, "CTRL_C"))
        self.assertEqual(model.input, "")
        self.assertEqual(apply_key(model, "CTRL_C").kind, "quit")
        apply_key(model, "UP")
        apply_key(model, "PGUP")
        self.assertEqual(model.scroll, 11)
        apply_key(model, "PGDN")
        apply_key(model, "DOWN")
        self.assertEqual(model.scroll, 0)
        drive(model, "/quit")
        self.assertEqual(apply_key(model, "ENTER").kind, "quit")

    def test_as_help_and_error_set_status(self):
        model = model_with([])
        drive(model, "/as vitaly")
        self.assertEqual(apply_key(model, "ENTER").kind, "as")
        self.assertEqual(model.human_label, "vitaly")
        drive(model, "x")
        self.assertEqual(apply_key(model, "ENTER").args["label"], "vitaly")
        drive(model, "/help")
        apply_key(model, "ENTER")
        self.assertIn("/peek", model.status)
        drive(model, "/bogus")
        self.assertEqual(apply_key(model, "ENTER").kind, "error")
        self.assertTrue(model.status.startswith("error:"))
        apply_key(model, "ESC")
        self.assertIsNone(model.status)

    def test_peek_box_closes_on_esc(self):
        model = model_with([])
        model.peek = box(["x"], 40)
        self.assertIsNone(apply_key(model, "ESC"))
        self.assertIsNone(model.peek)


# --------------------------------------------------------------------------
# /peek box and the full render


class PeekBoxTests(unittest.TestCase):
    def assert_no_agent_markers(self, lines: List[str]) -> None:
        for line in lines:
            self.assertNotIn("❯", line)
            self.assertNotIn("›", line)
            inner = line[1:].lstrip() if line.startswith("│") else line.lstrip()
            self.assertFalse(inner.startswith("❯") or inner.startswith("›") or inner.startswith(">"), line)

    def test_claude_and_codex_screens_are_neutralized(self):
        for name in ("detection/claude_idle.txt", "detection/claude_draft.txt", "detection/claude_permission_dialog.txt", "tui/codex_visible.txt"):
            raw = (FIXTURES / name).read_text(encoding="utf-8").split("\n")
            lines = box(raw, 80, "peek")
            self.assert_no_agent_markers(lines)
            self.assertTrue(lines[0].startswith("┌─ peek ─"))
            self.assertTrue(lines[-1].startswith("└"))
            for line in lines:
                self.assertEqual(tui_model.display_width(line), 80, line)
            self.assertEqual(len(lines), len(raw) + 2)
        self.assertIn("▸", "\n".join(box(["❯ draft"], 20)))

    def test_ansi_and_tabs_and_empty(self):
        lines = box(["\x1b[31m❯\x1b[0m\tred"], 30, None)
        self.assertEqual(lines[1], "│ ▸    red" + " " * 18 + " │")
        self.assertEqual(tui_model.display_width(lines[1]), 30)
        self.assertEqual(box([], 20)[1], "│ (empty screen)   │")
        self.assertEqual(len(box(["a"], 3)), 3)  # width floor, no crash

    def test_rendered_console_with_peek_never_starts_a_line_with_a_marker(self):
        model = model_with([record(1, text="❯ fake prompt in a post")], width=60, height=20)
        raw = (FIXTURES / "detection/claude_idle.txt").read_text(encoding="utf-8").split("\n")
        model.peek = box(raw, 60, "peek alpha-worker")
        screen = render_console(model)
        self.assertEqual(len(screen), 20)
        for line in screen:
            self.assertFalse(line.lstrip().startswith(("❯", "›", ">")), line)
        self.assertTrue(any(line.startswith(tui_model.INPUT_PROMPT) for line in screen))
        self.assertNotIn(tui_model.INPUT_PROMPT.strip(), ("❯", "›", ">"))


class RenderConsoleTests(unittest.TestCase):
    def test_exact_height_and_width_at_every_size(self):
        recs = [record(i, text="漢字 wide text number {} ".format(i) * 5) for i in range(1, 40)]
        for width, height in ((80, 24), (59, 12), (39, 8), (30, 5), (120, 50)):
            model = model_with(recs, width=width, height=height)
            drive(model, "typing a reply that is fairly long " * 3)
            screen = render_console(model)
            self.assertEqual(len(screen), height, (width, height))
            for line in screen:
                self.assertLessEqual(tui_model.display_width(line), width, (width, line))
            if height >= 8:
                self.assertTrue(screen[0].startswith("team alpha"))
            self.assertIn("filter:", "\n".join(screen))
            self.assertTrue(any("typing a reply" in line for line in screen))  # the input line always survives

    def test_roster_capped_with_who_hint(self):
        members = [{"name": "m{}".format(i), "kind": "claude", "pane_id": "w1:p{}".format(i), "status": "active", "agent_status": "idle"} for i in range(20)]
        model = build_console_model("alpha", who_doc(members=members), [], width=80, height=24, now=NOW)
        screen = render_console(model)
        self.assertEqual(len(screen), 24)
        self.assertTrue(any("more (/who)" in line for line in screen))

    def test_input_lines_wrap_and_cursor(self):
        model = model_with([], width=20)
        model.input = "a" * 30 + "\nb"
        model.cursor = len(model.input)
        lines = tui_model.input_lines(model, 20)
        self.assertEqual(lines[0], tui_model.INPUT_PROMPT + "a" * 18)
        self.assertEqual(lines[1], "  " + "a" * 12)
        self.assertEqual(lines[2], "  b")
        y, x = console.input_cursor_position(model, 5, 20)
        self.assertEqual((y, x), (6, 3))

    def test_previous_state_survives_rebuild(self):
        model = model_with([record(1)])
        drive(model, "draft")
        model.filter_index = 2
        model.scroll = 1
        model.default_recipient = "alpha-worker"
        fresh = build_console_model("alpha", who_doc(), [record(1), record(2)], now=NOW, previous=model)
        self.assertEqual((fresh.input, fresh.cursor, fresh.filter_index, fresh.scroll, fresh.default_recipient), ("draft", 5, 2, 1, "alpha-worker"))
        self.assertEqual(len(fresh.feed), 2)


# --------------------------------------------------------------------------
# picker


def agent_rows() -> List[Dict[str, Any]]:
    return [
        fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working"),
        fake_agent("w1:p3", "term_a", "codex", None),
        fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer"),
        fake_agent("w1:p10", "term_b", "claude", None, launch_pending=True),
        fake_agent("w1:p2", "term_c", "gemini", "gem", status="blocked"),
        fake_agent("w3:p1", "term_shell", None, None),  # a shell pane: never listed
    ]


def picker_model(rosters: Optional[Dict[str, List[Dict[str, Any]]]] = None, focused: Optional[str] = "w1") -> PickerModel:
    rows = picker_rows_from_agent_list(agent_rows(), focused)
    tui_model.mark_claimed(rows, rosters or {})
    model = PickerModel(rows=rows, focused_workspace=focused)
    model.live_names = {"alpha-worker", "alpha-reviewer", "gem"}
    model.existing_teams = sorted(rosters or {})
    return model


def type_line(model: PickerModel, text: str) -> None:
    model.input = ""
    model.cursor_pos = 0
    for ch in text:
        picker_apply_key(model, ch)


class PickerRowTests(unittest.TestCase):
    def test_rows_sorted_and_shell_panes_omitted(self):
        rows = picker_rows_from_agent_list(agent_rows(), "w1")
        self.assertEqual([r.pane_id for r in rows], ["w1:p2", "w1:p3", "w1:p10", "w2:p1", "w2:p2"])
        self.assertTrue(rows[2].launch_pending)
        self.assertFalse(tui_model.selectable(rows[2]))
        self.assertFalse(tui_model.selectable(rows[0]))  # blocked
        self.assertTrue(tui_model.selectable(rows[1]))

    def test_claimed_rows_by_terminal_then_pane(self):
        rows = picker_rows_from_agent_list(agent_rows(), None)
        tui_model.mark_claimed(rows, {"alpha": [dict(m) for m in FAKE_MEMBERS], "beta": [{"name": "left", "status": "left", "terminal_id": "term_a"}]})
        claimed = {r.pane_id: r.claimed_by for r in rows}
        self.assertEqual(claimed["w2:p1"], "alpha")
        self.assertEqual(claimed["w2:p2"], "alpha")
        self.assertEqual(claimed["w1:p3"], "")  # left members do not claim

    def test_scope_toggle_select_all_and_space(self):
        model = picker_model()
        self.assertIsNone(model.scope_workspace)
        picker_apply_key(model, "w")
        self.assertEqual(model.scope_workspace, "w1")
        self.assertEqual([r.pane_id for r in tui_model.visible_rows(model)], ["w1:p2", "w1:p3", "w1:p10"])
        picker_apply_key(model, "a")
        self.assertEqual([r.pane_id for r in tui_model.selected_rows(model)], ["w1:p3"])  # blocked and launching skipped
        picker_apply_key(model, "a")
        self.assertEqual(tui_model.selected_rows(model), [])
        picker_apply_key(model, "DOWN")
        picker_apply_key(model, "DOWN")
        picker_apply_key(model, " ")
        self.assertIn("launching", model.error)
        picker_apply_key(model, "UP")
        picker_apply_key(model, "UP")
        picker_apply_key(model, " ")
        self.assertIn("blocked", model.error)
        picker_apply_key(model, "w")
        self.assertIsNone(model.scope_workspace)
        self.assertEqual(model.cursor, 0)
        self.assertEqual(picker_apply_key(model, "ESC").kind, "quit")
        self.assertEqual(picker_apply_key(model, "CTRL_C").kind, "quit")
        self.assertEqual(picker_apply_key(model, "r").kind, "refresh")
        lines = tui_model.picker_lines(model, 70, 24)
        self.assertTrue(any("(launching)" in line for line in lines))
        self.assertTrue(any("(blocked)" in line for line in lines))

    def test_claimed_row_refuses_selection(self):
        model = picker_model({"alpha": [dict(m) for m in FAKE_MEMBERS]}, focused=None)
        idx = [r.pane_id for r in model.rows].index("w2:p1")
        for _ in range(idx):
            picker_apply_key(model, "DOWN")
        picker_apply_key(model, " ")
        self.assertIn("already in team alpha", model.error)
        self.assertEqual(tui_model.selected_rows(model), [])


class NameSuggestionTests(unittest.TestCase):
    def test_suffixing_only_when_the_cap_allows(self):
        self.assertEqual(suggest_name("vuln-reviewer", []), "vuln-reviewer")
        self.assertEqual(suggest_name("vuln-reviewer", ["vuln-reviewer"]), "vuln-reviewer-2")
        self.assertEqual(suggest_name("vuln-reviewer", ["vuln-reviewer", "vuln-reviewer-2"]), "vuln-reviewer-3")
        base = "a" * 31
        self.assertEqual(suggest_name(base, []), base)
        self.assertIsNone(suggest_name(base, [base]))  # base-2 would be 33 chars
        base30 = "a" * 30
        self.assertEqual(suggest_name(base30, [base30]), base30 + "-2")
        self.assertIsNone(suggest_name(base30, [base30] + [base30 + "-{}".format(i) for i in range(2, 10)]))
        self.assertIsNone(suggest_name("a" * 33, []))

    def test_team_name_normalization(self):
        self.assertEqual(tui_model.normalize_team_name("  Vuln Hunt!! "), "vuln-hunt")
        self.assertEqual(tui_model.normalize_team_name("42-things"), "things")
        self.assertEqual(tui_model.normalize_team_name("a" * 30), "a" * 15)
        self.assertEqual(tui_model.normalize_team_name("___"), "")
        self.assertIsNotNone(tui_model.validate_team_name_local(""))
        self.assertIsNotNone(tui_model.validate_team_name_local("human"))
        self.assertIsNone(tui_model.validate_team_name_local("vuln-hunt"))

    def test_member_name_validation_refuses_reserved_and_kind_labels(self):
        for bad in ("human", "all", "me", "claude", "codex", "Upper", "", "a" * 33, "1abc"):
            self.assertIsNotNone(tui_model.validate_member_name_local(bad), bad)
        self.assertIsNone(tui_model.validate_member_name_local("alpha-reviewer-2"))
        self.assertIsNotNone(tui_model.validate_role_local("claude"))
        self.assertIsNotNone(tui_model.validate_role_local("a" * 15))
        self.assertIsNone(tui_model.validate_role_local("reviewer"))


class PickerWizardTests(unittest.TestCase):
    def select(self, model: PickerModel, *pane_ids: str) -> None:
        for pane in pane_ids:
            model.cursor = [r.pane_id for r in tui_model.visible_rows(model)].index(pane)
            picker_apply_key(model, " ")

    def test_full_wizard_to_create_spec(self):
        model = picker_model(focused=None)
        self.assertIsNone(picker_apply_key(model, "ENTER"))
        self.assertIn("select at least one", model.error)
        self.select(model, "w1:p3", "w2:p1")
        self.assertIsNone(picker_apply_key(model, "ENTER"))
        self.assertEqual(model.stage, "name")
        type_line(model, "Vuln Hunt")
        picker_apply_key(model, "ENTER")
        self.assertEqual((model.stage, model.team_name), ("charter", "vuln-hunt"))
        type_line(model, "Find the bug.")
        picker_apply_key(model, "ENTER")
        type_line(model, "Reviewer owns review.")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "ENTER")  # empty line finishes
        self.assertEqual(model.charter, "Find the bug.\nReviewer owns review.")
        self.assertEqual((model.stage, model.member_index, model.member_field), ("members", 0, "role"))
        self.assertEqual(model.input, "codex-dev")  # default role: kind label + "-dev" (bare labels are refused)
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.member_field, "name")
        self.assertEqual(model.input, "vuln-hunt-codex-dev")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.member_field, "brief")
        type_line(model, "x" * 301)
        picker_apply_key(model, "ENTER")
        self.assertIn("max 300", model.error)
        type_line(model, "Review every patch.")
        picker_apply_key(model, "ENTER")
        self.assertEqual((model.member_index, model.member_field), (1, "role"))
        type_line(model, "Reviewer")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.input, "vuln-hunt-reviewer")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "ENTER")  # skip brief
        self.assertEqual(model.stage, "confirm")
        lines = tui_model.picker_lines(model, 100, 24)
        joined = "\n".join(lines)
        self.assertIn("vuln-hunt-codex-dev", joined)
        self.assertIn("vuln-hunt-reviewer", joined)
        self.assertIn("Review every patch.", joined)
        intent = picker_apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "create")
        spec = intent.args
        self.assertEqual(spec["team"], "vuln-hunt")
        self.assertEqual(spec["charter"], "Find the bug.\nReviewer owns review.")
        self.assertEqual([m["name"] for m in spec["members"]], ["vuln-hunt-codex-dev", "vuln-hunt-reviewer"])
        self.assertEqual([m["role"] for m in spec["members"]], ["codex-dev", "reviewer"])
        self.assertEqual(spec["members"][0]["brief"], "Review every patch.")
        self.assertIsNone(spec["members"][1]["brief"])
        self.assertTrue(spec["members"][1]["renamed"])  # alpha-reviewer -> vuln-hunt-reviewer
        self.assertFalse(spec["members"][0]["renamed"])
        argv = picker.create_args(spec)
        self.assertEqual(argv[:4], ["create", "vuln-hunt", "--charter", "Find the bug.\nReviewer owns review."])
        self.assertIn("w1:p3:codex-dev:vuln-hunt-codex-dev", argv)
        self.assertIn("vuln-hunt-codex-dev=Review every patch.", argv)

    def test_duplicate_default_names_get_suffixed_and_pending_set_is_checked(self):
        model = picker_model(focused=None)
        self.select(model, "w1:p3", "w2:p1")  # two codex agents
        picker_apply_key(model, "ENTER")
        type_line(model, "t")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "TAB")  # skip charter
        self.assertEqual(model.charter, "")
        picker_apply_key(model, "ENTER")  # role codex-dev
        self.assertEqual(model.input, "t-codex-dev")
        picker_apply_key(model, "ENTER")  # name
        picker_apply_key(model, "ENTER")  # brief
        picker_apply_key(model, "ENTER")  # role codex-dev for member 2
        self.assertEqual(model.input, "t-codex-dev-2")
        type_line(model, "t-codex-dev")
        picker_apply_key(model, "ENTER")
        self.assertIn("taken", model.error)
        type_line(model, "alpha-worker")  # a live name elsewhere
        picker_apply_key(model, "ENTER")
        self.assertIn("taken", model.error)
        type_line(model, "codex")
        picker_apply_key(model, "ENTER")
        self.assertIn("kind", model.error)
        type_line(model, "t-codex-dev-2")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.member_field, "brief")

    def test_agent_keeps_its_own_name_when_typed(self):
        model = picker_model(focused=None)
        self.select(model, "w2:p1")  # alpha-reviewer, live
        picker_apply_key(model, "ENTER")
        type_line(model, "beta")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "TAB")
        picker_apply_key(model, "ENTER")
        type_line(model, "alpha-reviewer")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.member_field, "brief")
        picker_apply_key(model, "ENTER")
        self.assertFalse(picker_apply_key(model, "ENTER").args["members"][0]["renamed"])

    def test_validation_and_backtracking(self):
        model = picker_model({"alpha": []}, focused=None)
        self.select(model, "w1:p3")
        picker_apply_key(model, "ENTER")
        type_line(model, "!!!")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "name")
        self.assertIn("must match", model.error)
        type_line(model, "alpha")
        picker_apply_key(model, "ENTER")
        self.assertIn("already exists", model.error)
        type_line(model, "human")
        picker_apply_key(model, "ENTER")
        self.assertIn("reserved", model.error)
        picker_apply_key(model, "ESC")
        self.assertEqual(model.stage, "select")
        picker_apply_key(model, "ENTER")
        type_line(model, "beta")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "charter")
        type_line(model, "x" * 2001)
        picker_apply_key(model, "ALT_ENTER")
        self.assertEqual(model.stage, "charter")
        self.assertIn("max 2000", model.error)
        self.assertEqual(picker_apply_key(model, "CTRL_O").kind, "load_file")
        picker_apply_key(model, "ESC")
        self.assertEqual((model.stage, model.input), ("name", "beta"))
        picker_apply_key(model, "ENTER")
        model.charter_lines = []
        picker_apply_key(model, "TAB")
        self.assertEqual(model.stage, "members")
        type_line(model, "human")
        picker_apply_key(model, "ENTER")
        self.assertIn("reserved", model.error)
        type_line(model, "qa")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "ESC")
        self.assertEqual(model.member_field, "role")
        picker_apply_key(model, "ESC")
        self.assertEqual(model.stage, "charter")
        picker_apply_key(model, "TAB")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "confirm")
        picker_apply_key(model, "ESC")
        self.assertEqual((model.stage, model.member_field), ("members", "brief"))

    def test_picker_lines_fit(self):
        model = picker_model()
        for stage in ("select", "name", "charter", "members", "confirm"):
            model.stage = stage
            if stage in ("members", "confirm"):
                model.rows[1].selected = True
                model.rows[1].role = "qa"
                model.rows[1].member_name = "t-qa"
            for line in tui_model.picker_lines(model, 40, 10):
                self.assertLessEqual(tui_model.display_width(line), 40)


class PickerRuntimeTests(unittest.TestCase):
    def test_build_model_from_fake_api_and_rosters(self):
        with TempState() as ts:
            api = FakeApi()
            api.set_response("agent.list", {"type": "agent_list", "agents": agent_rows()})
            model = picker.build_model(api, {"focused_pane_id": "w2:p2"}, ts.layout)
            self.assertEqual(model.focused_workspace, "w2")
            self.assertEqual(model.scope_workspace, "w2")
            self.assertEqual(model.existing_teams, ["alpha"])
            self.assertIn("alpha-worker", model.live_names)
            self.assertEqual({r.pane_id: r.claimed_by for r in model.rows}["w2:p1"], "alpha")
            model.rows[0].selected = True
            picker.refresh_rows(model, api, ts.layout)
            self.assertEqual(len(model.rows), 5)

    def test_load_context_and_charter_file(self):
        env = {"HERDR_PLUGIN_CONTEXT_JSON": "{bad json", "HERDR_WORKSPACE_ID": "w9", "HERDR_PANE_ID": "w9:p1"}
        ctx = picker.load_context(env)
        self.assertEqual((ctx["workspace_id"], ctx["focused_pane_id"]), ("w9", "w9:p1"))
        with TempState() as ts:
            path = ts.tmp / "charter.txt"
            path.write_text("line one\nline two\n", encoding="utf-8")
            model = picker_model()
            picker.load_charter_file(model, os.fspath(path))
            self.assertEqual(model.charter_lines, ["line one", "line two"])
            picker.load_charter_file(model, os.fspath(ts.tmp / "missing.txt"))
            self.assertIn("cannot read", model.error)
            path.write_text("x" * 2100, encoding="utf-8")
            picker.load_charter_file(model, os.fspath(path))
            self.assertIn("over 2000", model.error)

    def test_refresh_status_from_who(self):
        with TempState() as ts:
            store.write_json(ts.session.who_json, who_doc())
            model = picker_model(focused=None)
            picker.refresh_status_from_who(model, ts.layout)
            self.assertEqual({r.pane_id: r.agent_status for r in model.rows}["w2:p1"], "idle")


# --------------------------------------------------------------------------
# compose


class ComposeTests(unittest.TestCase):
    def context(self) -> Dict[str, Any]:
        return json.loads((FIXTURES / "tui" / "compose_context.json").read_text(encoding="utf-8"))

    def test_default_recipient_from_fixture_context(self):
        self.assertEqual(compose.default_recipient(self.context(), FAKE_MEMBERS), "alpha-worker")
        self.assertIsNone(compose.default_recipient({"focused_pane_id": "wA:p6", "focused_pane_agent": "claude"}, FAKE_MEMBERS))
        self.assertIsNone(compose.default_recipient({}, FAKE_MEMBERS))
        left = [dict(FAKE_MEMBERS[1], status="left")]
        self.assertIsNone(compose.default_recipient(self.context(), left))

    def test_parse_compose_line(self):
        intent = compose.parse_compose_line("ship it", "alpha-worker")
        self.assertEqual((intent.text, intent.to, intent.kind), ("ship it", ["alpha-worker"], "note"))
        intent = compose.parse_compose_line("/kind request /reply 3 /urgent @role:qa run tests", None)
        self.assertEqual((intent.kind, intent.reply_to, intent.urgent, intent.to), ("request", 3, True, ["role:qa"]))
        self.assertEqual(compose.parse_compose_line("plain", None).to, ["all"])
        self.assertTrue(compose.parse_compose_line("a" * 2001, None).to_args()["spill"])
        with self.assertRaises(HerdrTeamError) as ctx:
            compose.parse_compose_line("   ", None)
        self.assertEqual(ctx.exception.code, "compose_invalid")
        with self.assertRaises(HerdrTeamError):
            compose.parse_compose_line("@Bad! x", None)

    def test_compose_model_keys(self):
        model = ComposeModel(default_to="alpha-worker", team="alpha")
        for ch in "hello":
            compose_apply_key(model, ch)
        self.assertEqual(compose_apply_key(model, "RESIZE"), None)
        intent = compose_apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "post")
        self.assertEqual(intent.args["to"], ["alpha-worker"])
        self.assertEqual(intent.args["team"], "alpha")
        self.assertFalse(intent.args["spill"])
        empty = ComposeModel(team="alpha")
        self.assertEqual(compose_apply_key(empty, "ENTER").kind, "error")
        self.assertIn("empty", empty.status)
        self.assertEqual(compose_apply_key(empty, "ESC").kind, "quit")
        self.assertEqual(compose_apply_key(empty, "CTRL_C").kind, "quit")
        lines = tui_model.compose_lines(model, 40)
        self.assertTrue(all(tui_model.display_width(line) <= 40 for line in lines))
        self.assertTrue(lines[1].startswith(tui_model.INPUT_PROMPT))

    def test_build_model_and_nonce_env(self):
        with TempState() as ts:
            model = compose.build_model(ts.layout, "alpha", self.context())
            self.assertEqual(model.default_to, "alpha-worker")
            self.assertEqual(model.roster_names, ["alpha-reviewer", "alpha-worker", "human"])
        env = compose.env_with_nonce({"HERDR_PLUGIN_CONTEXT_JSON": json.dumps(self.context())}, "deadbeef")
        doc = json.loads(env["HERDR_PLUGIN_CONTEXT_JSON"])
        self.assertEqual(doc["launch_nonce"], "deadbeef")
        self.assertEqual(doc["focused_pane_id"], "w2:p2")
        env = compose.env_with_nonce({"HERDR_PLUGIN_CONTEXT_JSON": "{broken"}, "n1")
        self.assertEqual(json.loads(env["HERDR_PLUGIN_CONTEXT_JSON"]), {"launch_nonce": "n1"})
        self.assertEqual(len(compose.new_nonce()), 16)

    def test_execute_post_uses_cli_and_reports(self):
        calls: List[List[str]] = []

        def fake_run_cli(args, env, timeout=0.0):
            calls.append(list(args))
            return 0, {"seq": 9, "to": ["alpha-worker"], "notifier": "alive"}, None

        original = console.run_cli
        console.run_cli = fake_run_cli
        try:
            intent = Intent("post", compose.parse_compose_line("/kind request hi", "alpha-worker").to_args())
            result = compose.execute_post(intent, "alpha", {})
        finally:
            console.run_cli = original
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["seq"], 9)
        self.assertEqual(calls[0], ["--team", "alpha", "post", "hi", "--to", "alpha-worker", "--kind", "request"])

    def test_execute_post_failure(self):
        original = console.run_cli
        console.run_cli = lambda args, env, timeout=0.0: (4, None, {"code": "echo_rejected", "message": "marker"})
        try:
            result = compose.execute_post(Intent("post", {"text": "[herdr-team nudge] x", "to": ["all"]}), "alpha", {})
        finally:
            console.run_cli = original
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "echo_rejected")

    def test_run_args_refuses_outside_popup(self):
        with TempState() as ts:
            import argparse

            args = argparse.Namespace(env=ts.env, team=None, session=None, socket=None, json=False, force=False)
            with self.assertRaises(HerdrTeamError) as ctx:
                compose.run_args(args)
            self.assertEqual(ctx.exception.code, "not_a_plugin_pane")


# --------------------------------------------------------------------------
# console runtime helpers (no curses)


class BoardTailTests(unittest.TestCase):
    def test_incremental_reads_skip_fragments_and_detect_rotation(self):
        with TempState() as ts:
            tail = console.BoardTail(ts.team)
            self.assertEqual(tail.poll(), 0)
            path = ts.team.board_jsonl
            third = json.dumps(record(3, text="partial")).encode()
            with open(path, "ab") as fh:
                fh.write(json.dumps(record(1)).encode() + b"\n")
                fh.write(json.dumps(record(2)).encode() + b"\n")
                fh.write(third[:-12])
            self.assertEqual(tail.poll(), 2)
            self.assertEqual([r["seq"] for r in tail.records], [1, 2])
            self.assertEqual(tail.poll(), 0)  # the fragment is re-read only once it is complete
            with open(path, "ab") as fh:
                fh.write(third[-12:] + b'\nnot json\n' + json.dumps(record(4)).encode() + b"\n")
            self.assertEqual(tail.poll(), 2)  # #3 healed, junk skipped, #4
            self.assertEqual([r["seq"] for r in tail.records], [1, 2, 3, 4])
            self.assertEqual(tail.records[2]["text"], "partial")
            self.assertEqual(tail.poll(), 0)
            # rotation: file replaced by a shorter one with new seqs
            with open(path, "wb") as fh:
                fh.write(json.dumps(record(5)).encode() + b"\n")
            self.assertEqual(tail.poll(), 1)
            self.assertEqual(tail.resets, 1)
            self.assertEqual([r["seq"] for r in tail.records], [1, 2, 3, 4, 5])
            os.unlink(path)
            self.assertEqual(tail.poll(), 0)


class ConsoleRuntimeTests(unittest.TestCase):
    def test_pick_team(self):
        with TempState() as ts:
            self.assertEqual(console.pick_team(ts.layout, None, ts.env), "alpha")
            self.assertEqual(console.pick_team(ts.layout, "alpha", ts.env), "alpha")
            with self.assertRaises(HerdrTeamError) as ctx:
                console.pick_team(ts.layout, "nope", ts.env)
            self.assertEqual(ctx.exception.code, "team_not_found")
            beta = ts.session.team("beta")
            from herdr_team import paths as _paths

            _paths.ensure_team_dirs(beta)
            store.write_json(beta.team_json, {"team": "beta", "members": []})
            with self.assertRaises(HerdrTeamError) as ctx:
                console.pick_team(ts.layout, None, ts.env)
            self.assertEqual(ctx.exception.code, "team_ambiguous")
            self.assertEqual(console.pick_team(ts.layout, None, ts.env_with(HERDR_TEAM="beta")), "beta")
            console.write_console_record(ts.layout, {"default_team": "beta"})
            self.assertEqual(console.pick_team(ts.layout, None, ts.env), "beta")

    def test_build_model_from_files(self):
        with TempState() as ts:
            store.write_json(ts.session.who_json, who_doc())
            store.write_json(ts.team.mute_json, {"*": iso(datetime.now(timezone.utc) + timedelta(minutes=9))})
            store.write_json(ts.session.view_json, {"on": True, "label": "team:alpha"})
            store.write_json(ts.team.cursor("human@vitaly"), {"v": 1, "seq": 1})
            console.write_console_record(ts.layout, {"human_label": "vitaly", "open": True})
            with open(ts.team.board_jsonl, "ab") as fh:
                for seq in (1, 2):
                    fh.write(json.dumps(record(seq)).encode() + b"\n")
            state = console.ConsoleState(ts.layout, "alpha", ts.env)
            state.width = 120
            model = console.build_model(ts.layout, "alpha", state, env=ts.env)
            first, second = header_lines(model.header, 120)
            self.assertTrue(first.startswith("team alpha · 2 members · view:on · nudges:paused (8m) · toasts:terminal · unread(you):1"), first)
            self.assertEqual(second, "charter #3: Find and fix the session-isolation bug")
            self.assertEqual(model.human_label, "vitaly")
            self.assertEqual(len(model.feed), 2)
            # HP-08: an audit.jsonl author_mismatch line becomes a console warning on the next build
            with open(ts.team.audit_jsonl, "ab") as fh:
                fh.write(json.dumps({"ts": "2026-09-05T12:00:39.626Z", "event": "author_mismatch", "author": "alpha-reviewer", "via": "cli", "pane_id": "w1:p2", "details": {"requested": "human"}}).encode() + b"\n")
                fh.write(b"not json\n")
            model = console.build_model(ts.layout, "alpha", state, env=ts.env)
            self.assertEqual(len(model.feed), 3)
            self.assertTrue(any(e["kind"] == "warning" and "alpha-reviewer tried to post as human" in e["line"] for e in model.feed), [e["line"] for e in model.feed])
            self.assertTrue(any("muted" in line for line in model.roster_lines))
            with open(ts.team.board_jsonl, "ab") as fh:
                fh.write(json.dumps(record(3)).encode() + b"\n")
            model.input = "draft"
            console.refresh(model, ts.layout, state)
            self.assertEqual(len(model.feed), 4)  # three records plus the audit warning
            self.assertEqual(model.input, "draft")

    def test_post_args(self):
        intent = Intent("post", {"text": "hi", "to": ["a", "b"], "kind": "request", "reply_to": 4, "urgent": True, "refs": ["p.md"], "spill": True, "label": "v"})
        self.assertEqual(console.post_args(intent, "alpha"), ["--team", "alpha", "post", "hi", "--to", "a,b", "--kind", "request", "--reply-to", "4", "--urgent", "--ref", "p.md", "--spill", "--name", "v"])
        self.assertEqual(console.post_args(Intent("post", {"text": "x"}), "alpha"), ["--team", "alpha", "post", "x"])

    def test_execute_peek_who_charter_and_as(self):
        with TempState() as ts:
            store.write_json(ts.session.who_json, who_doc())
            api = FakeApi()
            screen = (FIXTURES / "detection/claude_idle.txt").read_text(encoding="utf-8")
            api.set_cli(["agent", "read", "w2:p2"], 0, screen, "")
            api.set_cli_result(["pane", "get", "wC:p1"], {"type": "pane_info", "pane": {"pane_id": "wC:p1", "focused": True, "terminal_id": "term_console"}}, "cli:pane:get")
            state = console.ConsoleState(ts.layout, "alpha", ts.env_with(HERDR_PANE_ID="wC:p1"))
            model = console.build_model(ts.layout, "alpha", state, env=state.env)
            self.assertTrue(console.execute_intent(Intent("peek", {"member": "alpha-worker"}), model, state, api))
            self.assertEqual(api.runs[-1][:5], ["agent", "read", "w2:p2", "--source", "visible"])
            self.assertTrue(all("--lines" not in argv for argv in api.runs))
            for line in model.peek:
                self.assertNotIn("❯", line)
                self.assertFalse(line[1:].lstrip().startswith(">"))
            self.assertTrue(model.peek[0].startswith("┌─ peek alpha-worker"))
            console.execute_intent(Intent("peek", {"member": "ghost"}), model, state, api)
            self.assertEqual(model.status, "no member ghost")
            console.execute_intent(Intent("who", {}), model, state, api)
            self.assertIn("alpha-worker", "\n".join(model.peek))
            console.execute_intent(Intent("charter", {"action": "show"}), model, state, api)
            self.assertIn("Find and fix the bug.", "\n".join(model.peek))
            self.assertTrue(console.execute_intent(Intent("as", {"label": "vitaly"}), model, state, api))
            self.assertEqual(console.read_console_json(ts.layout)["human_label"], "vitaly")
            self.assertFalse(console.execute_intent(Intent("quit"), model, state, api))
            self.assertTrue(console.focused_now(api, "wC:p1"))
            self.assertFalse(console.focused_now(api, None))
            self.assertFalse(console.focused_now(api, "wZ:p9"))

    def test_execute_post_and_delivery_intents_go_through_the_cli(self):
        with TempState() as ts:
            calls: List[List[str]] = []

            def fake_run_cli(args, env, timeout=0.0):
                calls.append(list(args))
                head = args[2] if len(args) > 2 else args[0]
                outs = {
                    "post": {"seq": 12, "to": ["alpha-worker"], "notifier": "offline"},
                    "retract": {"seq": 13, "retracts": 12},
                    "mute": {"muted": {}}, "unmute": {"muted": {}},
                    "nudge": {"job": "j1"}, "focus": {"job": "j2"}, "remove": {"removed": "alpha-worker"},
                    "charter": {"charter": {"seq": 4}}, "use": {"default_team": "alpha"},
                }
                if head == "post" and "fail" in args[3]:
                    return 1, None, {"code": "recipient_unknown", "message": "no such member"}
                return 0, outs.get(head, {}), None

            original = console.run_cli
            console.run_cli = fake_run_cli
            try:
                api = FakeApi()
                api.set_cli_result(["pane", "get", "wC:p1"], {"type": "pane_info", "pane": {"pane_id": "wC:p1", "focused": False}}, "cli:pane:get")
                state = console.ConsoleState(ts.layout, "alpha", ts.env_with(HERDR_PANE_ID="wC:p1"))
                model = console.build_model(ts.layout, "alpha", state, env=state.env)
                console.execute_intent(Intent("post", {"text": "hi", "to": ["alpha-worker"]}), model, state, api)
                self.assertEqual(calls[-1][:4], ["--team", "alpha", "post", "hi"])
                self.assertIn("posted #12 to alpha-worker (unfocused: unverified)", model.status)
                self.assertIn("notifier offline", model.status)
                self.assertFalse(model.focused)
                console.execute_intent(Intent("post", {"text": "fail", "to": ["x"]}), model, state, api)
                self.assertIn("recipient_unknown", model.status)
                console.execute_intent(Intent("retract", {"seq": 12, "confirm": True}), model, state, api)
                self.assertEqual(calls[-1], ["--team", "alpha", "retract", "12"])
                console.execute_intent(Intent("mute", {"member": None, "for": "10m"}), model, state, api)
                self.assertEqual(calls[-1], ["--team", "alpha", "mute", "--all", "--for", "10m"])
                console.execute_intent(Intent("unmute", {"member": "alpha-worker"}), model, state, api)
                self.assertEqual(calls[-1], ["--team", "alpha", "unmute", "alpha-worker"])
                console.execute_intent(Intent("nudge", {"member": "alpha-worker", "force": True}), model, state, api)
                self.assertEqual(calls[-1], ["--team", "alpha", "nudge", "alpha-worker", "--force"])
                console.execute_intent(Intent("focus", {"member": "alpha-worker"}), model, state, api)
                self.assertEqual(calls[-1], ["--team", "alpha", "focus", "alpha-worker"])
                console.execute_intent(Intent("remove", {"member": "alpha-worker", "confirm": True}), model, state, api)
                self.assertEqual(calls[-1], ["--team", "alpha", "remove", "alpha", "alpha-worker"])
                console.execute_intent(Intent("charter", {"action": "set", "text": "New goal", "urgent": True}), model, state, api)
                self.assertEqual(calls[-1], ["--team", "alpha", "charter", "set", "New goal", "--urgent"])
                self.assertIn("#4", model.status)
                console.execute_intent(Intent("use", {"team": "nope"}), model, state, api)
                self.assertIn("no team nope", model.status)
                self.assertTrue(all(argv[0] != "post" or "wA:p6" not in argv for argv in calls))
            finally:
                console.run_cli = original
            # the console never prompts or toasts on its own
            self.assertTrue(all(argv[0] not in ("agent",) or argv[1] == "read" for argv in api.runs))

    def test_run_cli_reports_bad_paths(self):
        original = console.cli_path
        console.cli_path = lambda: Path("/nonexistent/herdr-team")
        try:
            rc, out, err = console.run_cli(["--version"], {}, timeout=1.0)
        finally:
            console.cli_path = original
        self.assertEqual(err["code"], "cli_unavailable")
        self.assertIsNone(out)

    def test_console_record_and_run_args_guards(self):
        with TempState() as ts:
            console.write_console_record(ts.layout, {"pane_id": "wC:p1", "open": True})
            doc = console.read_console_json(ts.layout)
            self.assertEqual(doc, {"pane_id": "wC:p1", "open": True})
            self.assertEqual(oct(ts.session.console_json.stat().st_mode & 0o777), "0o600")
            import argparse

            args = argparse.Namespace(env=ts.env, team=None, session=None, socket=None, json=False, target_pane=None, force=False)
            with self.assertRaises(HerdrTeamError) as ctx:
                console.run_args(args)
            self.assertEqual(ctx.exception.code, "not_a_plugin_pane")
            args = argparse.Namespace(env=ts.env, team=None, session=None, socket=None, json=False, force=False)
            with self.assertRaises(HerdrTeamError) as ctx:
                picker.run_args(args)
            self.assertEqual(ctx.exception.code, "not_a_plugin_pane")

    def test_sigterm_unwinds_through_the_finally_and_records_closed(self):
        """UI-05 (rig, 2026-09-05): ``kill -TERM <console pid>`` left open:true and a hint pane waiting for a key."""
        import signal

        with TempState() as ts:
            console.write_console_record(ts.layout, {"default_team": "alpha", "open": False})
            seen = {}

            def fake_wrapper(fn, *args):
                seen["handler"] = signal.getsignal(signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)  # delivered to this thread; the handler raises before the next line
                raise AssertionError("SIGTERM handler did not interrupt the loop")

            before = signal.getsignal(signal.SIGTERM)
            env = {k: v for k, v in ts.env.items() if k != "HERDR_PANE_ID"}
            with mock.patch("curses.wrapper", fake_wrapper):
                code = console.run(ts.layout, None, "alpha", env)
            self.assertEqual(code, 0)
            self.assertTrue(callable(seen["handler"]))
            self.assertEqual(signal.getsignal(signal.SIGTERM), before)  # restored for the rest of the process
            doc = console.read_console_json(ts.layout)
            self.assertEqual((doc["open"], doc["pid"], doc["default_team"]), (False, None, "alpha"))
            # a plain return from the loop still records closed and keeps the exit code
            with mock.patch("curses.wrapper", lambda fn, *a: 0):
                self.assertEqual(console.run(ts.layout, None, "alpha", env), 0)
            self.assertFalse(console.read_console_json(ts.layout)["open"])

    def test_helpers(self):
        with TempState() as ts:
            self.assertFalse(console.view_is_on(ts.layout, "alpha"))
            store.write_json(ts.session.view_json, {"label": "team:alpha"})
            self.assertTrue(console.view_is_on(ts.layout, "alpha"))
            self.assertEqual(console.toast_mode(ts.layout, {"toasts": "herdr"}), "herdr")
            store.write_json(ts.session.daemon_json, {"toasts": "terminal"})
            self.assertEqual(console.toast_mode(ts.layout, {}), "terminal")
            self.assertEqual(console.human_label_for(ts.layout, ts.env_with(HERDR_TEAM_HUMAN="v")), "v")
            self.assertEqual([m["name"] for m in console.roster_members(ts.layout, "alpha")], ["alpha-reviewer", "alpha-worker", "human"])
            self.assertEqual(console.read_cursors(ts.team), {})


class PaneEntrypointSocketGateTests(unittest.TestCase):
    """Plan 4.1 / PK-07 (G21): console, compose and picker are no-ops for a socket the rig has not listed.

    Right after the entrypoint check each ``run_args`` prints the hint on
    stderr and exits 0 before any socket call, before ``console.json`` is
    written and before the curses loop; ``ui *`` already skips the same way.
    """

    def _gated(self, ts: TempState, entrypoint: str, **extra: Any) -> "tuple[argparse.Namespace, FakeApi, io.StringIO]":
        allowed = paths.allowed_sockets_file(ts.config_dir)
        allowed.parent.mkdir(parents=True, exist_ok=True)
        allowed.write_text("/somewhere/else/herdr.sock\n")
        api = FakeApi()
        env = ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID=entrypoint, HERDR_PANE_ID="w1:p2")
        args = argparse.Namespace(env=env, team=None, session=None, socket=None, json=False, force=False, api=api, **extra)
        return args, api, io.StringIO()

    def test_console_skips_an_unlisted_socket_before_writing_console_json(self):
        with TempState() as ts:
            args, api, err = self._gated(ts, "console", target_pane=None)
            with mock.patch("sys.stderr", err):
                self.assertEqual(console.run_args(args), 0)
            self.assertEqual(err.getvalue(), "herdr-team console skipped: socket not allowed\n")
            self.assertEqual(api.calls, [])
            self.assertFalse(ts.session.console_json.exists())
            self.assertIsNone(paths.read_pointer(ts.config_dir))

    def test_compose_skips_an_unlisted_socket(self):
        with TempState() as ts:
            args, api, err = self._gated(ts, "compose")
            with mock.patch("sys.stderr", err):
                self.assertEqual(compose.run_args(args), 0)
            self.assertEqual(err.getvalue(), "herdr-team compose skipped: socket not allowed\n")
            self.assertEqual(api.calls, [])
            self.assertFalse(ts.session.console_json.exists())

    def test_picker_skips_an_unlisted_socket(self):
        with TempState() as ts:
            args, api, err = self._gated(ts, "picker", dry_run=True)
            with mock.patch("sys.stderr", err):
                self.assertEqual(picker.run_args(args), 0)
            self.assertEqual(err.getvalue(), "herdr-team picker skipped: socket not allowed\n")
            self.assertEqual(api.calls, [])
            self.assertFalse(ts.session.console_json.exists())

    def test_entrypoint_check_still_comes_first(self):
        # A plain shell without the entrypoint env keeps refusing with not_a_plugin_pane even when the socket is unlisted.
        with TempState() as ts:
            args, api, err = self._gated(ts, "other", target_pane=None)
            with self.assertRaises(HerdrTeamError) as ctx:
                console.run_args(args)
            self.assertEqual(ctx.exception.code, "not_a_plugin_pane")
            self.assertEqual(api.calls, [])

    def test_listed_socket_reaches_the_tty_check(self):
        # With the socket listed the gate is transparent: the next guard (no tty under the test runner) fires.
        with TempState() as ts:
            args, api, _err = self._gated(ts, "compose")
            paths.allowed_sockets_file(ts.config_dir).write_text(os.fspath(ts.socket_path) + "\n")
            with mock.patch("sys.stdout.isatty", return_value=False):
                with self.assertRaises(HerdrTeamError) as ctx:
                    compose.run_args(args)
            self.assertEqual(ctx.exception.code, "no_tty")
            self.assertEqual(api.calls, [])


class GlyphAndTimeTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(tui_model.remaining_label(iso(NOW + timedelta(seconds=45)), NOW), "45s")
        self.assertEqual(tui_model.remaining_label(iso(NOW + timedelta(hours=3)), NOW), "3h")
        self.assertIsNone(tui_model.remaining_label(None, NOW))
        self.assertIsNone(tui_model.remaining_label("garbage", NOW))
        self.assertEqual(tui_model.age_label(iso(NOW - timedelta(days=2)), NOW), "2d")
        self.assertEqual(tui_model.clock_label("junk"), "--:--")
        self.assertEqual(tui_model.status_glyph("blocked"), "●")
        self.assertEqual(tui_model.status_glyph("weird", ascii_only=True), "?")
        self.assertEqual(tui_model.kind_glyph("done"), "✓")
        self.assertEqual(tui_model.truncate_columns("漢字漢字", 5), "漢字…")
        self.assertEqual(tui_model.truncate_columns("abc", 0), "")
        self.assertEqual(tui_model.pad_columns("ab", 4), "ab  ")
        self.assertEqual(tui_model.neutralize_prompt_markers("  > quoted"), "  ▸ quoted")
        self.assertEqual(tui_model.neutralize_prompt_markers("a > b"), "a > b")


if __name__ == "__main__":
    unittest.main()

"""Gate tests: one row per gate, fixtures for the dialog heuristic, ND-06, focus, follow-up, pair budget, TTL."""

from __future__ import annotations

import unittest
from pathlib import Path

from herdr_team import gate
from herdr_team.gate import (
    ActiveClock, GateConfig, MemberSnapshot, PendingWork, classify_result, decide, dialog_line,
    evaluate, next_action, prompt_line_empty, prompt_line_text, window_from_samples,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "detection"
NOW = 1_000_000.0


def fixture(name: str) -> str:
    return (FIXTURES / (name + ".txt")).read_text(encoding="utf-8")


STRONG_IDLE = {"state": "idle", "matched_rule": {"id": "live_prompt_box", "region": "prompt_box_body", "priority": 950, "state": "idle"},
               "visible_blocker": False, "skip_state_update": False, "skipped_update_reason": None, "screen_detection_skipped": False}
WEAK_IDLE = {"state": "idle", "matched_rule": {"id": "osc_title_idle", "region": "osc_title", "priority": 250, "state": "idle"},
             "visible_blocker": False, "skip_state_update": False, "skipped_update_reason": None}


def snap(**overrides) -> MemberSnapshot:
    """A member that passes every gate at NOW unless overridden."""
    base = dict(
        name="alpha-worker", kind="claude", terminal_id="term_w1", pane_id="w2:p2", agent_kind="claude",
        agent_status="idle", state_change_seq=7, launch_pending=False, focused=False, screen_detection_skipped=False,
        delivery="nudge", verified_kind=True, stable_since_ms=NOW - 5000, idle_since_ms=NOW - 120000,
        explain=dict(STRONG_IDLE), detection_text=fixture("claude_idle"),
    )
    base.update(overrides)
    return MemberSnapshot(**base)


def pend(**overrides) -> PendingWork:
    base = dict(seqs=[41, 42], urgent=False, cursor_seq=40, authors=["alpha-reviewer", "human"])
    base.update(overrides)
    return PendingWork(**base)


def run(snapshot=None, pending=None, now=NOW, global_last=None, pair=0, config=None):
    return evaluate(snapshot or snap(), pending or pend(), now, global_last, pair, config)


class DecisionTableTests(unittest.TestCase):
    def test_baseline_delivers(self):
        d = run()
        self.assertTrue(d.deliver)
        self.assertEqual(d.action, "send")
        self.assertIsNone(d.reason)
        self.assertFalse(d.weak_idle)
        self.assertEqual(d.stable_ms_required, 2000)
        self.assertEqual(d.details["prompt_line"], "empty")

    def test_gate_rows(self):
        rows = [
            # name, snapshot overrides, pending overrides, expected reason, expected action
            ("1 nothing pending", {}, {"seqs": []}, "nothing_pending", "drop"),
            ("1 read before nudge", {}, {"cursor_seq": 42}, "read_before_nudge", "drop"),
            ("1 expired ttl", {}, {"active_ms": gate.POST_TTL_MS}, "expired", "drop"),
            ("2 self", {}, {"authors": ["alpha-worker", "alpha-worker"]}, "self", "drop"),
            ("2 human", {"kind": "human", "terminal_id": None}, {}, "no_terminal", "drop"),
            ("2 no terminal", {"terminal_id": None}, {}, "no_terminal", "drop"),
            ("2 broadcast", {}, {"broadcast": True}, "broadcast", "drop"),
            ("2 muted", {"muted_until": NOW + 1000}, {}, "muted", "hold"),
            ("3 absent", {"agent_kind": None}, {}, "absent", "hold"),
            ("3 kind mismatch", {"agent_kind": "codex"}, {}, "kind_mismatch", "hold"),
            ("3 launch pending", {"launch_pending": True}, {}, "launch_pending", "hold"),
            ("4 kind unverified", {"verified_kind": False}, {}, "kind_unverified", "hold"),
            ("5 working", {"agent_status": "working"}, {}, "not_idle", "hold"),
            ("5 blocked", {"agent_status": "blocked"}, {}, "not_idle", "hold"),
            ("5 unknown", {"agent_status": "unknown"}, {}, "not_idle", "hold"),
            ("6 no window", {"stable_since_ms": None}, {}, "unstable", "hold"),
            ("6 short window", {"stable_since_ms": NOW - 1999}, {}, "unstable", "hold"),
            ("6 fresh get differs", {"fresh_state_change_seq": 8}, {}, "unstable", "hold"),
            ("6 done hold", {"idle_since_ms": NOW - 59999}, {}, "done_hold", "hold"),
            ("7 explain blocked", {"explain": dict(STRONG_IDLE, state="blocked")}, {}, "blocked", "hold"),
            ("7 visible blocker", {"explain": dict(STRONG_IDLE, visible_blocker=True)}, {}, "visible_blocker", "hold"),
            ("7 skip state update", {"explain": dict(STRONG_IDLE, skip_state_update=True)}, {}, "skip_state_update", "hold"),
            ("7 skipped reason", {"explain": dict(STRONG_IDLE, skipped_update_reason="transcript_viewer")}, {}, "skip_state_update", "hold"),
            ("8 dialog", {"detection_text": fixture("claude_permission_dialog")}, {}, "dialog", "hold"),
            ("9 draft", {"detection_text": fixture("claude_draft")}, {}, "draft_present", "hold"),
            ("9 draft from prompt_line", {"detection_text": None, "prompt_line": "half typed"}, {}, "draft_present", "hold"),
            ("10 focused", {"focused": True}, {}, "focused", "hold"),
            ("11 pane stuck", {"pane_stuck_until_ms": NOW + 30000}, {}, "pane_stuck", "hold"),
            ("11 in flight", {"in_flight": True}, {}, "in_flight", "hold"),
            ("11 interval", {"last_nudge_ms": NOW - 5000}, {}, "interval", "hold"),
        ]
        for name, s_over, p_over, reason, action in rows:
            with self.subTest(name):
                d = run(snap(**s_over), pend(**p_over))
                self.assertFalse(d.deliver, name)
                self.assertEqual(d.reason, reason, name)
                self.assertEqual(d.action, action, name)
                self.assertEqual(d.hold, reason)

    def test_gate_order_is_fail_fast(self):
        # A muted, absent, working member reports the earliest failing gate.
        d = run(snap(muted_until=NOW + 1, agent_kind=None, agent_status="working"))
        self.assertEqual(d.reason, "muted")
        d = run(snap(agent_kind=None, agent_status="working", detection_text=fixture("claude_permission_dialog")))
        self.assertEqual(d.reason, "absent")
        self.assertEqual(d.details["gate"], 3)

    def test_global_interval_and_pair_budget(self):
        self.assertEqual(run(global_last=NOW - 1000).reason, "global_interval")
        self.assertTrue(run(global_last=NOW - 1500).deliver)
        d = run(pair=gate.PAIR_BUDGET)
        self.assertEqual(d.reason, "pair_budget")
        self.assertTrue(d.toast)
        self.assertTrue(run(pair=gate.PAIR_BUDGET - 1).deliver)

    def test_toast_holds_flagged(self):
        self.assertTrue(run(snap(agent_kind="codex")).toast)
        self.assertTrue(run(snap(verified_kind=False)).toast)
        self.assertFalse(run(snap(agent_status="working")).toast)

    def test_urgent_semantics(self):
        # urgent skips done_hold, broadcast, and the per-target interval ...
        self.assertTrue(run(snap(idle_since_ms=NOW - 1000), pend(urgent=True)).deliver)
        self.assertTrue(run(snap(), pend(urgent=True, broadcast=True)).deliver)
        self.assertTrue(run(snap(last_nudge_ms=NOW - 5000), pend(urgent=True)).deliver)
        # ... but never stable_ms, dialog, focus, the global bucket, or the pair budget
        self.assertEqual(run(snap(stable_since_ms=NOW - 1000), pend(urgent=True)).reason, "unstable")
        self.assertEqual(run(snap(detection_text=fixture("claude_plan_mode_question")), pend(urgent=True)).reason, "dialog")
        self.assertEqual(run(snap(focused=True), pend(urgent=True)).reason, "focused")
        self.assertEqual(run(snap(), pend(urgent=True), global_last=NOW - 100).reason, "global_interval")
        self.assertEqual(run(snap(), pend(urgent=True), pair=10).reason, "pair_budget")

    def test_force_skips_done_hold_and_interval_only(self):
        self.assertTrue(run(snap(idle_since_ms=NOW - 1000, last_nudge_ms=NOW - 1000), pend(force=True)).deliver)
        self.assertEqual(run(snap(), pend(force=True, broadcast=True)).reason, "broadcast")
        self.assertEqual(run(snap(stable_since_ms=NOW - 100), pend(force=True)).reason, "unstable")

    def test_done_hold_boundary(self):
        self.assertEqual(run(snap(idle_since_ms=NOW - 59999)).reason, "done_hold")
        self.assertTrue(run(snap(idle_since_ms=NOW - 60000)).deliver)
        self.assertTrue(run(snap(idle_since_ms=None)).deliver)

    def test_config_overrides(self):
        cfg = GateConfig.from_mapping({"done_hold_ms": 0, "min_interval_ms": 1000})
        self.assertTrue(run(snap(idle_since_ms=NOW - 10, last_nudge_ms=NOW - 1000), config=cfg).deliver)
        with self.assertRaises(ValueError):
            GateConfig.from_mapping({"bogus": 1})
        self.assertIs(GateConfig.from_mapping(cfg), cfg)


class StableWindowTests(unittest.TestCase):
    def test_stable_ms_by_authority(self):
        self.assertEqual(gate.stable_ms_for(snap(), False), 2000)
        self.assertEqual(gate.stable_ms_for(snap(), True), 4000)
        self.assertEqual(gate.stable_ms_for(snap(screen_detection_skipped=True), False), 750)
        # hook authority has no screen rule to be weak about
        self.assertEqual(gate.stable_ms_for(snap(screen_detection_skipped=True), True), 750)
        self.assertEqual(gate.stable_ms_for(snap(delivery="hooks"), False), 15000)
        self.assertEqual(gate.stable_ms_for(snap(delivery="hooks"), True), 15000)

    def test_weak_idle_detection(self):
        self.assertFalse(gate.is_weak_idle(STRONG_IDLE))
        self.assertTrue(gate.is_weak_idle(WEAK_IDLE))
        self.assertTrue(gate.is_weak_idle(dict(STRONG_IDLE, matched_rule=None)))
        self.assertTrue(gate.is_weak_idle(None))
        self.assertTrue(gate.is_weak_idle({"matched_rule": {"id": "osc_progress_idle", "region": "osc_progress"}}))

    def test_weak_idle_doubles_window_and_names_the_hold(self):
        d = run(snap(explain=dict(WEAK_IDLE), stable_since_ms=NOW - 2500))
        self.assertEqual(d.reason, "weak_idle")
        self.assertTrue(d.weak_idle)
        self.assertEqual(d.stable_ms_required, 4000)
        d = run(snap(explain=dict(WEAK_IDLE), stable_since_ms=NOW - 1000))
        self.assertEqual(d.reason, "unstable")
        self.assertTrue(run(snap(explain=dict(WEAK_IDLE), stable_since_ms=NOW - 4000)).deliver)

    def test_hooks_delivery_needs_15s(self):
        self.assertEqual(run(snap(delivery="hooks", stable_since_ms=NOW - 14000)).reason, "unstable")
        self.assertTrue(run(snap(delivery="hooks", stable_since_ms=NOW - 15000)).deliver)

    def test_hook_authority_needs_750ms(self):
        s = snap(screen_detection_skipped=True, explain=None, stable_since_ms=NOW - 700)
        self.assertEqual(run(s).reason, "unstable")
        s.stable_since_ms = NOW - 750
        self.assertTrue(run(s).deliver)

    def test_window_from_samples(self):
        samples = [(NOW - 9000, 6, "working"), (NOW - 6000, 7, "idle"), (NOW - 3000, 7, "idle"), (NOW - 500, 7, "done")]
        w = window_from_samples(samples, NOW)
        self.assertEqual((w.state_change_seq, w.agent_status), (7, "done"))
        self.assertEqual(w.stable_since_ms, NOW - 6000)
        self.assertEqual(w.idle_since_ms, NOW - 6000)
        self.assertEqual(w.samples, 4)
        self.assertFalse(w.reset)

    def test_window_gap_resets(self):
        samples = [(NOW - 30000, 7, "idle"), (NOW - 19000, 7, "idle"), (NOW - 2000, 7, "idle")]
        w = window_from_samples(samples, NOW)
        self.assertEqual(w.stable_since_ms, NOW - 2000, "an 17 s gap between samples resets the run")
        w = window_from_samples([(NOW - 20000, 7, "idle")], NOW)
        self.assertIsNone(w.stable_since_ms)
        self.assertTrue(w.reset)
        self.assertEqual(window_from_samples([], NOW).samples, 0)

    def test_window_idle_and_stable_diverge(self):
        # seq changed on the working->idle flip; the idle run is shorter than the sample run
        samples = [(NOW - 5000, 7, "idle"), (NOW - 4000, 8, "working"), (NOW - 3000, 9, "idle"), (NOW - 100, 9, "idle")]
        w = window_from_samples(samples, NOW)
        self.assertEqual(w.stable_since_ms, NOW - 3000)
        self.assertEqual(w.idle_since_ms, NOW - 3000)


class RegionTests(unittest.TestCase):
    def test_is_horizontal_rule_mirrors_detector(self):
        self.assertTrue(gate.is_horizontal_rule("───"))
        self.assertTrue(gate.is_horizontal_rule("  ──  "))
        self.assertTrue(gate.is_horizontal_rule("──── Section"))
        self.assertFalse(gate.is_horizontal_rule("─ x"))
        self.assertFalse(gate.is_horizontal_rule("╭────╮"))
        self.assertFalse(gate.is_horizontal_rule(""))
        self.assertFalse(gate.is_horizontal_rule("---"))

    def test_split_lines_like_rust(self):
        self.assertEqual(gate.split_lines("a\r\nb\n"), ["a", "b"])
        self.assertEqual(gate.split_lines("a\n\nb"), ["a", "", "b"])
        self.assertEqual(gate.split_lines(""), [])
        self.assertEqual(gate.split_lines(None), [])

    def test_claude_regions(self):
        text = fixture("claude_idle")
        self.assertEqual(gate.prompt_box_body(text), "❯")
        self.assertIn("? for shortcuts", gate.after_last_horizontal_rule(text))
        self.assertNotIn("Done.", gate.after_last_horizontal_rule(text))
        self.assertIsNone(gate.prompt_box_body("no rules here\n❯ x"))
        self.assertEqual(gate.after_last_horizontal_rule("a\nb"), "a\nb")

    def test_codex_regions(self):
        text = fixture("codex_idle")
        self.assertEqual(gate.after_last_prompt_marker(text).strip(), "? for shortcuts                                    92% context left")
        self.assertEqual(gate.after_last_prompt_marker("no marker"), "no marker")
        self.assertTrue(gate.is_codex_prompt_line("›"))
        self.assertTrue(gate.is_codex_prompt_line("› hi"))
        self.assertFalse(gate.is_codex_prompt_line("›hi"))
        self.assertFalse(gate.is_codex_prompt_line(" › hi"))

    def test_bottom_non_empty_lines(self):
        self.assertEqual(gate.bottom_non_empty_lines("a\n\nb\n\nc\n", 2), "b\n\nc")
        self.assertEqual(gate.bottom_non_empty_lines("\n\n", 2), "")
        self.assertEqual(gate.bottom_non_empty_lines("only", 5), "only")

    def test_detection_hash_ignores_trailing_whitespace(self):
        self.assertEqual(gate.detection_hash("a  \nb\n"), gate.detection_hash("a\nb"))
        self.assertNotEqual(gate.detection_hash("a\nb"), gate.detection_hash("a\nc"))


class DialogHeuristicTests(unittest.TestCase):
    def test_claude_dialog_fixtures(self):
        self.assertEqual(dialog_line(fixture("claude_permission_dialog"), "claude"), "Esc to cancel · Tab to amend · Ctrl+E to explain")
        self.assertEqual(dialog_line(fixture("claude_plan_mode_question"), "claude"), "Enter to select · Tab/arrow keys to navigate · Esc to cancel")
        self.assertEqual(dialog_line(fixture("claude_model_picker"), "claude"), "Select model")
        self.assertEqual(dialog_line(fixture("claude_transcript_viewer"), "claude"), "Showing detailed transcript · Ctrl+O to toggle · ↑↓ scroll")

    def test_claude_idle_and_draft_are_not_dialogs(self):
        self.assertIsNone(dialog_line(fixture("claude_idle"), "claude"))
        self.assertIsNone(dialog_line(fixture("claude_draft"), "claude"))
        self.assertIsNone(dialog_line(None, "claude"))
        self.assertIsNone(dialog_line("", "claude"))

    def test_prose_mentions_do_not_trigger(self):
        text = fixture("claude_prose_mentions")
        self.assertIn("select model", text.lower())
        self.assertIn("esc to cancel", text.lower())
        self.assertIn("action required", text.lower())
        self.assertIsNone(dialog_line(text, "claude"))
        self.assertTrue(prompt_line_empty(text, "claude"))
        self.assertTrue(run(snap(detection_text=text)).deliver)

    def test_codex_fixtures(self):
        self.assertIsNone(dialog_line(fixture("codex_idle"), "codex"))
        self.assertEqual(dialog_line(fixture("codex_approval"), "codex"), "Allow command?")
        self.assertEqual(dialog_line(fixture("codex_transcript_viewer"), "codex"), "↑/↓ to scroll   PgUp/PgDn to page   Home/End to jump   q to quit   Esc to edit prev")

    def test_codex_viewer_needs_both_markers(self):
        self.assertIsNone(dialog_line("›\n q to quit the game", "codex"))
        self.assertIsNone(dialog_line("›\n ↑/↓ to scroll the list", "codex"))
        self.assertIsNotNone(dialog_line("›\n ↑/↓ to scroll   q to quit", "codex"))

    def test_codex_scope_excludes_history_above_prompt(self):
        text = "• The tool printed 'Allow command?' earlier, all good now.\n\n›\n\n  ? for shortcuts\n"
        self.assertIsNone(dialog_line(text, "codex"))

    def test_generic_kind_uses_bottom_window(self):
        text = "\n".join(["esc to cancel mentioned long ago"] + ["line {}".format(i) for i in range(10)]) + "\n"
        self.assertIsNone(dialog_line(text, "gemini"))
        self.assertEqual(dialog_line("output\n\nAllow command? [y/n]\n", "gemini"), "Allow command? [y/n]")

    def test_dialog_hold_cap_flags_toast(self):
        s = snap(detection_text=fixture("claude_permission_dialog"), dialog_hold_since_ms=NOW - gate.DIALOG_HOLD_CAP_MS)
        d = run(s)
        self.assertEqual(d.reason, "dialog")
        self.assertTrue(d.details["dialog_hold_capped"])
        self.assertTrue(d.toast)
        self.assertEqual(d.matched_dialog_line, "Esc to cancel · Tab to amend · Ctrl+E to explain")
        s.dialog_hold_since_ms = NOW - 1000
        self.assertFalse(run(s).toast)


class PromptLineTests(unittest.TestCase):
    def test_claude_prompt_line(self):
        self.assertEqual(prompt_line_text(fixture("claude_idle"), "claude"), "")
        self.assertTrue(prompt_line_empty(fixture("claude_idle"), "claude"))
        self.assertEqual(prompt_line_text(fixture("claude_draft"), "claude"), "now also fix the flaky test in src/bff/cache_test.rs")
        self.assertFalse(prompt_line_empty(fixture("claude_draft"), "claude"))
        multi = "───\n❯ first line\n  second line\n───\n  ? for shortcuts\n"
        self.assertEqual(prompt_line_text(multi, "claude"), "first line\n  second line")
        self.assertIsNone(prompt_line_text("no box at all", "claude"))
        self.assertTrue(prompt_line_empty("no box at all", "claude"))
        self.assertTrue(prompt_line_empty(None, "claude"))
        # a box without a prompt marker is not a prompt box
        self.assertIsNone(prompt_line_text("───\nsome table row\n───\n", "claude"))

    def test_codex_prompt_line(self):
        self.assertEqual(prompt_line_text(fixture("codex_idle"), "codex"), "")
        self.assertEqual(prompt_line_text("• done\n\n› half typed\n\n  ? for shortcuts\n", "codex"), "half typed")
        self.assertFalse(prompt_line_empty("› x", "codex"))
        self.assertIsNone(prompt_line_text("nothing", "codex"))

    def test_codex_placeholder_is_not_a_draft(self):
        # Live 2026-09-05 (Codex 0.153.2): a fresh idle Codex paints "› Ask Codex to do anything" on the
        # empty prompt line; the gate held the member with draft_present forever.
        screen = "• You have 2 usage limit resets available. Run /usage to use one.\n› Ask Codex to do anything\n  gpt-5.4-mini low · /var/tmp/herdr-synapse-rig/work\n"
        self.assertEqual(prompt_line_text(screen, "codex"), "")
        self.assertTrue(prompt_line_empty(screen, "codex"))
        # a real draft that merely starts like the placeholder is still a draft
        self.assertEqual(prompt_line_text("› Ask Codex to do anything about the flaky test\n", "codex"), "Ask Codex to do anything about the flaky test")
        self.assertFalse(prompt_line_empty("› Ask Codex to do anything about the flaky test\n", "codex"))

    def test_generic_prompt_line(self):
        self.assertEqual(prompt_line_text("output\n$ ls -la\n", "gemini"), "ls -la")
        self.assertEqual(prompt_line_text("output\n> \n", "gemini"), "")
        self.assertIsNone(prompt_line_text("output\nplain last line\n", "gemini"))

    def test_codex_animated_placeholder(self):
        for line in ("› Ask Codex to do anything⡀        ⠈    ⠁ ",
                     "›⠁Ask Codex to do anything⡀     ⠈  ⠐", "› ⠈ Ask Codex to do anything ⠂"):
            with self.subTest(line=line):
                self.assertEqual(prompt_line_text(line, "codex"), "")
        for text in ("⠁⠂", "שלום ⠈", "Ask Codex to do anything about tests⠁", "Ask ⠁Codex to do anything"):
            self.assertEqual(prompt_line_text("› " + text, "codex"), text)
        self.assertIsNone(prompt_line_text("›⠁custom", "codex"))


class FocusPolicyTests(unittest.TestCase):
    def test_focused_holds_until_max_hold(self):
        d = run(snap(focused=True, focus_hold_since_ms=NOW - 299999))
        self.assertEqual(d.reason, "focused")
        self.assertIn("max hold", d.detail)

    def test_after_max_hold_needs_stable_snapshot(self):
        s = snap(focused=True, focus_hold_since_ms=NOW - 300000, detection_stable_since_ms=NOW - 2999)
        d = run(s)
        self.assertEqual(d.reason, "focused")
        self.assertEqual(d.detail, "snapshot changing")
        self.assertTrue(d.details["focus_max_hold_elapsed"])
        s.detection_stable_since_ms = NOW - 3000
        d = run(s)
        self.assertTrue(d.deliver)
        self.assertTrue(d.details["focused_after_max_hold"])

    def test_after_max_hold_still_needs_empty_prompt_line(self):
        s = snap(focused=True, focus_hold_since_ms=NOW - 600000, detection_stable_since_ms=NOW - 10000, detection_text=fixture("claude_draft"))
        self.assertEqual(run(s).reason, "draft_present")

    def test_urgent_never_bypasses_focus(self):
        s = snap(focused=True, focus_hold_since_ms=NOW - 1000)
        self.assertEqual(run(s, pend(urgent=True)).reason, "focused")

    def test_nudge_focused_policy_can_be_relaxed_by_config(self):
        cfg = GateConfig(nudge_focused="always")
        self.assertTrue(run(snap(focused=True), config=cfg).deliver)


class RateLimitTests(unittest.TestCase):
    def test_follow_up_exception(self):
        # 5 s since the last nudge, the target read (cursor moved past the nudged seqs), posts remain
        s = snap(last_nudge_ms=NOW - 5000, last_nudge_cursor_seq=38)
        d = run(s, pend(cursor_seq=40, seqs=[41, 42]))
        self.assertTrue(d.deliver)
        self.assertTrue(d.details["follow_up"])
        # only one immediate follow-up
        s.follow_up_used = True
        self.assertEqual(run(s, pend(cursor_seq=40)).reason, "interval")
        # no cursor movement, no exception
        s = snap(last_nudge_ms=NOW - 5000, last_nudge_cursor_seq=40)
        self.assertEqual(run(s, pend(cursor_seq=40)).reason, "interval")
        # unknown last cursor, no exception
        s = snap(last_nudge_ms=NOW - 5000, last_nudge_cursor_seq=None)
        self.assertEqual(run(s).reason, "interval")

    def test_interval_boundary(self):
        self.assertEqual(run(snap(last_nudge_ms=NOW - 19999)).reason, "interval")
        self.assertTrue(run(snap(last_nudge_ms=NOW - 20000)).deliver)

    def test_in_flight_beats_interval(self):
        self.assertEqual(run(snap(in_flight=True, last_nudge_ms=NOW - 100)).reason, "in_flight")

    def test_pane_stuck_expires(self):
        self.assertEqual(run(snap(pane_stuck_until_ms=NOW + 1)).reason, "pane_stuck")
        self.assertTrue(run(snap(pane_stuck_until_ms=NOW)).deliver)


class TtlClockTests(unittest.TestCase):
    def test_active_clock_pauses_while_missing(self):
        clock = ActiveClock()
        clock.mark(0.0, True)
        self.assertEqual(clock.elapsed_ms(10000.0), 10000.0)
        clock.mark(10000.0, False)  # member goes missing
        self.assertEqual(clock.elapsed_ms(500000.0), 10000.0)
        clock.mark(500000.0, True)
        self.assertEqual(clock.elapsed_ms(505000.0), 15000.0)
        self.assertFalse(clock.expired(505000.0))
        self.assertTrue(clock.expired(gate.POST_TTL_MS + 490000.0))
        # idempotent marks
        clock.mark(505000.0, True)
        self.assertEqual(clock.elapsed_ms(505000.0), 15000.0)
        restored = ActiveClock.from_json(clock.to_json())
        self.assertEqual(restored.elapsed_ms(505000.0), 15000.0)
        self.assertEqual(ActiveClock.from_json(None).elapsed_ms(1.0), 0.0)

    def test_expired_only_counts_active_time(self):
        clock = ActiveClock().mark(0.0, True).mark(1000.0, False)
        active = clock.elapsed_ms(gate.POST_TTL_MS * 2)
        self.assertEqual(active, 1000.0)
        self.assertTrue(run(snap(), pend(active_ms=active)).deliver)
        self.assertEqual(run(snap(), pend(active_ms=gate.POST_TTL_MS)).action, "drop")


class DecideTests(unittest.TestCase):
    MEMBER = {"name": "alpha-worker", "role": "worker", "kind": "claude", "terminal_id": "term_w1", "pane_id": "w2:p2", "delivery": "nudge", "verified_kind": True}
    POSTS = [
        {"seq": 41, "from": "alpha-reviewer", "to": ["alpha-worker"], "urgent": False},
        {"seq": 42, "from": "human", "to": ["all"], "urgent": False},
        {"seq": 39, "from": "human", "to": ["alpha-worker"], "urgent": False},  # already read
    ]

    def row(self, **over):
        base = {"terminal_id": "term_w1", "agent": "claude", "name": "alpha-worker", "agent_status": "idle", "state_change_seq": 7,
                "focused": False, "launch_pending": False, "screen_detection_skipped": False, "pane_id": "w2:p2"}
        base.update(over)
        return base

    @staticmethod
    def dense(start_ms, seq, status, end_ms=NOW - 200, step_ms=5000):
        """Samples every ``step_ms`` (under the 10 s gap-reset rule) from ``start_ms`` to ``end_ms``."""
        ts = start_ms
        out = []
        while ts < end_ms:
            out.append((ts, seq, status))
            ts += step_ms
        out.append((end_ms, seq, status))
        return out

    def samples(self):
        # working until 125 s ago, then idle at seq 7 with a sample every 5 s
        return [(NOW - 130000, 6, "working")] + self.dense(NOW - 125000, 7, "idle")

    def test_decide_end_to_end(self):
        d = decide(self.MEMBER, self.POSTS, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), self.samples(), None, NOW)
        self.assertEqual(d.action, "send")
        self.assertIsNone(d.reason)
        self.assertEqual(d.details["seqs"], [41, 42])
        self.assertFalse(d.details["urgent"])
        self.assertEqual(d.details["stable_ms_required"], 2000)
        self.assertIsNotNone(d.gate)
        self.assertEqual(d.to_json()["action"], "send")

    def test_decide_absent_when_row_belongs_to_another_terminal(self):
        d = decide(self.MEMBER, self.POSTS, 40, self.row(terminal_id="term_other"), STRONG_IDLE, fixture("claude_idle"), self.samples(), None, NOW)
        self.assertEqual(d.reason, "absent")
        d = decide(self.MEMBER, self.POSTS, 40, None, None, None, self.samples(), None, NOW)
        self.assertEqual(d.reason, "absent")

    def test_decide_broadcast_only_drops_unless_urgent(self):
        posts = [{"seq": 42, "from": "human", "to": ["all"], "urgent": False}]
        d = decide(self.MEMBER, posts, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), self.samples(), None, NOW)
        self.assertEqual((d.action, d.reason), ("drop", "broadcast"))
        posts[0]["urgent"] = True
        d = decide(self.MEMBER, posts, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), self.samples(), None, NOW)
        self.assertEqual(d.action, "send")

    def test_decide_self_only_drops(self):
        posts = [{"seq": 42, "from": "alpha-worker", "to": ["alpha-worker"], "urgent": False}]
        d = decide(self.MEMBER, posts, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), self.samples(), None, NOW)
        self.assertEqual((d.action, d.reason), ("drop", "self"))

    def test_decide_uses_samples_for_the_window(self):
        fresh = [(NOW - 500, 7, "idle")]
        d = decide(self.MEMBER, self.POSTS, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), fresh, None, NOW)
        self.assertEqual(d.reason, "unstable")
        # the row moved past the sampled seq: no window yet
        d = decide(self.MEMBER, self.POSTS, 40, self.row(state_change_seq=8), STRONG_IDLE, fixture("claude_idle"), self.samples(), None, NOW)
        self.assertEqual(d.reason, "unstable")
        # done_hold after a recent flip to idle
        recent = [(NOW - 30000, 6, "working")] + self.dense(NOW - 25000, 7, "idle")
        d = decide(self.MEMBER, self.POSTS, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), recent, None, NOW)
        self.assertEqual(d.reason, "done_hold")
        # a sampling gap over 10 s inside the idle run voids the window (plan 8.1)
        gappy = [(NOW - 130000, 6, "working"), (NOW - 125000, 7, "idle"), (NOW - 3000, 7, "idle"), (NOW - 200, 7, "idle")]
        d = decide(self.MEMBER, self.POSTS, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), gappy, None, NOW)
        self.assertEqual(d.reason, "done_hold", "the window restarts at the sample after the gap")
        self.assertIn("3000/60000", d.details["detail"])

    def test_decide_runtime_state_and_config(self):
        d = decide(self.MEMBER, self.POSTS, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), self.samples(), {"done_hold_ms": 0}, NOW, in_flight=True)
        self.assertEqual(d.reason, "in_flight")
        d = decide(self.MEMBER, self.POSTS, 40, self.row(name="renamed-by-hand"), STRONG_IDLE, fixture("claude_idle"), self.samples(), None, NOW)
        # Gate 3 (plan 8.2): the live name must match the roster; the daemon adopts the rename first.
        self.assertEqual((d.action, d.reason), ("hold", "name_mismatch"))
        self.assertEqual(d.details["name_mismatch"], "renamed-by-hand")
        self.assertEqual(d.details["name_mismatch"], "renamed-by-hand")
        with self.assertRaises(ValueError):
            decide(self.MEMBER, self.POSTS, 40, self.row(), STRONG_IDLE, None, self.samples(), None, NOW, bogus=1)
        d = decide(self.MEMBER, self.POSTS, 40, self.row(), STRONG_IDLE, fixture("claude_idle"), self.samples(), None, NOW, verified_kind=False)
        self.assertEqual(d.reason, "kind_unverified")

    def test_pending_from_posts(self):
        p = gate.pending_from_posts(self.POSTS, "alpha-worker", 40, force=True, active_ms=5.0)
        self.assertEqual(p.seqs, [41, 42])
        self.assertEqual(p.authors, ["alpha-reviewer", "human"])
        self.assertFalse(p.broadcast)
        self.assertTrue(p.force)
        self.assertEqual(p.active_ms, 5.0)
        p = gate.pending_from_posts([{"seq": "41", "to": ["alpha-worker"]}, {"seq": True}], "alpha-worker", 0)
        self.assertEqual(p.seqs, [])
        self.assertFalse(p.broadcast)


class ClassifyResultTests(unittest.TestCase):
    def agent(self, status="working", seq=8, terminal="term_w1", kind="claude"):
        return {"terminal_id": terminal, "agent": kind, "agent_status": status, "state_change_seq": seq, "pane_id": "w2:p2", "name": "alpha-worker"}

    def test_nd06_landed_in_turn_from_send_time_snapshot(self):
        # The fake flipped to working 200 ms after the gate (seq 7 -> 8); the
        # send-time snapshot in the response already shows it.
        s = snap(state_change_seq=7)
        response = {"id": "x", "result": {"type": "agent_prompted", "agent": self.agent("working", 8)}}
        self.assertEqual(classify_result(s, response), "landed_in_turn")
        # same seq but a non-idle status is also in-turn
        self.assertEqual(classify_result(s, {"result": {"agent": self.agent("working", 7)}}), "landed_in_turn")

    def test_nd06_landed_in_turn_with_wait_surfaces_as_timeout(self):
        s = snap(state_change_seq=7)
        response = {"error": {"code": "timeout", "message": "timed out waiting for agent status"}}
        self.assertEqual(classify_result(s, response, waited=True), "landed_in_turn")
        self.assertEqual(classify_result(s, response, waited=False), "stalled")

    def test_waited_success(self):
        s = snap(state_change_seq=7)
        self.assertEqual(classify_result(s, {"result": {"agent": self.agent("working", 8)}}, waited=True), "landed_working")
        self.assertEqual(classify_result(s, {"result": {"agent": self.agent("blocked", 8)}}, waited=True), "landed_blocked")
        self.assertEqual(classify_result(s, {"result": {"agent": self.agent("idle", 9)}}, waited=True), "stalled")

    def test_send_time_snapshot_then_post_send_agent(self):
        s = snap(state_change_seq=7)
        sent = {"result": {"agent": self.agent("idle", 7)}}
        self.assertEqual(classify_result(s, sent), "sent")
        self.assertEqual(classify_result(s, sent, post_send_agent=self.agent("working", 8)), "landed_working")
        self.assertEqual(classify_result(s, sent, post_send_agent=self.agent("blocked", 8)), "landed_blocked")
        self.assertEqual(classify_result(s, sent, post_send_agent=self.agent("idle", 9)), "landed_working")  # fast turn
        self.assertEqual(classify_result(s, sent, post_send_agent=self.agent("idle", 7)), "stalled")
        self.assertEqual(classify_result(s, sent, post_send_agent=self.agent("idle", 7), post_send_text=fixture("claude_draft")), "not_submitted")
        self.assertEqual(classify_result(s, sent, post_send_text=fixture("claude_draft")), "not_submitted")

    def test_wrong_occupant(self):
        s = snap(state_change_seq=7)
        self.assertEqual(classify_result(s, {"result": {"agent": self.agent(terminal="term_new")}}), "wrong_occupant")
        self.assertEqual(classify_result(s, {"result": {"agent": self.agent(kind="codex")}}), "wrong_occupant")
        self.assertEqual(classify_result(s, {"result": {"agent": self.agent("idle", 7)}}, post_send_agent=self.agent(terminal="term_new")), "wrong_occupant")

    def test_stalled_and_not_submitted(self):
        s = snap(state_change_seq=7)
        stalled = {"error": {"code": "agent_prompt_stalled", "message": "agent prompt produced no observed state change within 5000 ms"}}
        self.assertEqual(classify_result(s, stalled), "stalled")
        self.assertEqual(classify_result(s, stalled, post_send_text=fixture("claude_draft")), "not_submitted")
        self.assertEqual(classify_result(s, stalled, post_send_text=fixture("claude_idle")), "stalled")
        self.assertEqual(classify_result(s, stalled, post_send_agent=self.agent("working", 8)), "landed_working")

    def test_hung_and_error_codes(self):
        s = snap()
        self.assertEqual(classify_result(s, None), "hung")
        self.assertEqual(classify_result(s, {"error": {"code": "herdr_timeout", "message": "15 s"}}), "hung")
        self.assertEqual(classify_result(s, {"error": {"code": "agent_not_ready", "message": "agent x is no longer the pane foreground process"}}), "busy")
        self.assertEqual(classify_result(s, {"error": {"code": "agent_not_ready", "message": "agent x is not ready"}}), "transient")
        self.assertEqual(classify_result(s, {"error": {"code": "agent_prompt_failed", "message": "pty actor closed"}}), "transient")
        self.assertEqual(classify_result(s, {"error": {"code": "agent_prompt_failed", "message": "input queue full"}}), "pane_stuck")
        self.assertEqual(classify_result(s, {"error": {"code": "agent_blocked", "message": "blocked"}}), "refused")
        self.assertEqual(classify_result(s, {"error": {"code": "agent_not_found", "message": "gone"}}), "re_resolve")
        self.assertEqual(classify_result(s, {"error": {"code": "agent_not_running", "message": "gone"}}), "re_resolve")
        self.assertEqual(classify_result(s, {"error": {"code": "something_new", "message": "?"}}), "unknown_error")
        self.assertEqual(classify_result(s, {"result": {"type": "ok"}}), "unknown_error")

    def test_bare_agent_object_accepted(self):
        s = snap(state_change_seq=7)
        self.assertEqual(classify_result(s, self.agent("working", 8), waited=True), "landed_working")
        self.assertEqual(classify_result(s, {"agent": self.agent("working", 8)}), "landed_in_turn")


class RetryTableTests(unittest.TestCase):
    def test_first_attempt(self):
        a = next_action([], NOW)
        self.assertEqual((a.action, a.attempts), ("retry", 0))

    def test_read_ends_the_work(self):
        a = next_action([{"ts_ms": NOW - 1000, "result": "landed_working", "cursor_advanced": True}], NOW)
        self.assertEqual(a.action, "done")

    def test_renudge_schedule_then_abandon(self):
        first = NOW - 1000
        history = [{"ts_ms": first, "result": "landed_working"}]
        a = next_action(history, NOW, turn_completed_since=True)
        self.assertEqual(a.action, "wait")
        self.assertEqual(a.not_before_ms, first + 120000)
        a = next_action(history, first + 120000, turn_completed_since=False)
        self.assertEqual((a.action, a.reason), ("wait", "turn_not_completed"))
        a = next_action(history, first + 120000, turn_completed_since=True, gate_passes=False)
        self.assertEqual(a.action, "hold")
        a = next_action(history, first + 120000, turn_completed_since=True)
        self.assertEqual((a.action, a.attempts), ("renudge", 1))
        history.append({"ts_ms": first + 120000, "result": "landed_working"})
        a = next_action(history, first + 200000, turn_completed_since=True)
        self.assertEqual((a.action, a.not_before_ms), ("wait", first + 300000))
        history.append({"ts_ms": first + 300000, "result": "stalled"})
        a = next_action(history, first + 600000, turn_completed_since=True)
        self.assertEqual(a.action, "renudge")
        history.append({"ts_ms": first + 600000, "result": "landed_working"})
        a = next_action(history, first + 900000, turn_completed_since=True)
        self.assertEqual(a.action, "abandon")
        self.assertEqual(a.attempts, 4)

    def test_transient_backoff_doubles_to_cap(self):
        history = [{"ts_ms": NOW, "result": "transient"}]
        self.assertEqual(next_action(history, NOW).not_before_ms, NOW + 3000)
        history.append({"ts_ms": NOW + 3000, "result": "busy"})
        self.assertEqual(next_action(history, NOW + 3000).not_before_ms, NOW + 3000 + 6000)
        for i in range(6):
            history.append({"ts_ms": NOW + 100000 + i, "result": "transient"})
        a = next_action(history, NOW + 100005)
        self.assertEqual(a.not_before_ms, NOW + 100005 + 60000)
        self.assertEqual(a.action, "wait")

    def test_other_results(self):
        # an in-turn landing is queued by the harness: it waits for the re-nudge schedule like any landing
        self.assertEqual(next_action([{"ts_ms": NOW, "result": "landed_in_turn"}], NOW).action, "wait")
        a = next_action([{"ts_ms": NOW, "result": "hung"}], NOW)
        self.assertEqual((a.action, a.not_before_ms), ("wait", NOW + 60000))
        self.assertEqual(next_action([{"ts_ms": NOW, "result": "pane_stuck"}], NOW).not_before_ms, NOW + 60000)
        self.assertEqual(next_action([{"ts_ms": NOW, "result": "wrong_occupant"}], NOW).action, "re_resolve")
        self.assertEqual(next_action([{"ts_ms": NOW, "result": "re_resolve"}], NOW).action, "re_resolve")
        a = next_action([{"ts_ms": NOW, "result": "not_submitted"}], NOW)
        self.assertEqual((a.action, a.not_before_ms), ("hold", NOW + 60000))
        self.assertEqual(next_action([{"ts_ms": NOW, "result": "refused"}], NOW).not_before_ms, NOW + 5000)
        a = next_action([{"ts_ms": NOW, "result": "unknown_error"}], NOW)
        self.assertEqual((a.action, a.reason), ("hold", "unknown_error"))
        self.assertEqual(a.to_json()["attempts"], 1)


if __name__ == "__main__":
    unittest.main()

"""Gate 9 versus Claude Code's prompt suggestion (faint "ghost text"), found live in the rig on 2026-09-05.

Claude Code 2.1.261 paints a proposed next prompt inside the prompt box as
``❯\\xa0ESC[2m<suggestion>ESC[0m`` after a turn. The detection source is plain
text, so the suggestion read as a typed draft and the daemon held every nudge
and briefing as ``draft_present`` for as long as it stayed on screen
(rig evidence ``ND-04-prep``: ``read-visible-ansi-ghost-w1:p2.txt`` versus
``read-visible-ansi-draft-w1:p2.txt``, where a typed ``zz`` carries no
styling at all). The fix reads the visible viewport with styling when, and
only when, the plain text shows a draft, and drops the faint runs on the
prompt line.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from herdr_team import daemon as D
from herdr_team import gate, store
from herdr_team.gate import MemberSnapshot, PendingWork, evaluate, styled_prompt_line_text, visible_line_text
from support import FAKE_AGENTS, FakeApi, FakeError, TempState, fake_agent, fake_read

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "detection"
NOW = 1_000_000.0
STRONG_IDLE = {"state": "idle", "matched_rule": {"id": "live_prompt_box", "region": "prompt_box_body", "priority": 950, "state": "idle"},
               "visible_blocker": False, "skip_state_update": False, "skipped_update_reason": None, "screen_detection_skipped": False}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


GHOST_PLAIN = fixture("claude_ghost_suggestion.txt")           # agent.read --source detection (plain)
GHOST_VISIBLE = fixture("claude_ghost_suggestion_visible.ansi")  # agent.read --source visible --format ansi
DRAFT_VISIBLE = fixture("claude_draft_visible.ansi")             # the same viewport with a typed "zz"


def snap(**overrides) -> MemberSnapshot:
    base = dict(
        name="alpha-worker", kind="claude", terminal_id="term_w1", pane_id="w2:p2", agent_kind="claude",
        agent_status="idle", state_change_seq=7, launch_pending=False, focused=False, screen_detection_skipped=False,
        delivery="nudge", verified_kind=True, stable_since_ms=NOW - 5000, idle_since_ms=NOW - 120000,
        explain=dict(STRONG_IDLE), detection_text=GHOST_PLAIN,
    )
    base.update(overrides)
    return MemberSnapshot(**base)


def pend() -> PendingWork:
    return PendingWork(seqs=[41], urgent=False, cursor_seq=40, authors=["alpha-reviewer"])


class AnsiLineTests(unittest.TestCase):
    def test_faint_runs_are_dropped_only_when_asked(self):
        line = "❯\xa0\x1b[0m\x1b[2mrun `herdr-team board --new`\x1b[0m\r"
        self.assertEqual(visible_line_text(line), "❯\xa0run `herdr-team board --new`")
        self.assertEqual(visible_line_text(line, drop_faint=True), "❯\xa0")

    def test_extended_colour_selector_is_not_the_faint_attribute(self):
        # 38;2;r;g;b carries a literal "2" that must not be read as SGR 2 (faint)
        line = "\x1b[0m\x1b[38;2;255;255;255m\x1b[48;2;55;55;55mkeep me\x1b[0m \x1b[38;5;2mand me\x1b[0m\x1b[2m ghost\x1b[22m tail\x1b[0m"
        self.assertEqual(visible_line_text(line, drop_faint=True), "keep me and me tail")
        self.assertEqual(visible_line_text(line), "keep me and me ghost tail")

    def test_reset_variants_end_a_faint_run(self):
        self.assertEqual(visible_line_text("\x1b[2mgone\x1b[mback", drop_faint=True), "back")
        self.assertEqual(visible_line_text("\x1b[2mgone\x1b[0;1mbold", drop_faint=True), "bold")
        self.assertEqual(visible_line_text("\x1b[1;2mgone to the end", drop_faint=True), "")

    def test_osc_and_other_csi_sequences_vanish(self):
        self.assertEqual(visible_line_text("\x1b]0;title\x07\x1b[3Ktext\x1b[?25h"), "text")


class StyledPromptLineTests(unittest.TestCase):
    def test_plain_detection_text_still_reads_the_suggestion_as_a_draft(self):
        # the plain read cannot tell them apart: documents why the styled read exists
        self.assertEqual(gate.prompt_line_text(GHOST_PLAIN, "claude"), "run `herdr-team board --new`")

    def test_live_suggestion_is_an_empty_prompt_line(self):
        self.assertEqual(styled_prompt_line_text(GHOST_VISIBLE, "claude"), "")

    def test_live_typed_draft_is_kept(self):
        self.assertEqual(styled_prompt_line_text(DRAFT_VISIBLE, "claude"), "zz")

    def test_typed_text_beside_a_suggestion_is_the_draft(self):
        mixed = GHOST_VISIBLE.replace("❯\xa0\x1b[0m\x1b[2m", "❯\xa0half typed \x1b[0m\x1b[2m")
        self.assertEqual(styled_prompt_line_text(mixed, "claude"), "half typed")

    def test_crlf_lines_from_the_socket_are_handled(self):
        self.assertEqual(styled_prompt_line_text(GHOST_VISIBLE.replace("\n", "\r\n"), "claude"), "")
        self.assertEqual(styled_prompt_line_text(DRAFT_VISIBLE.replace("\n", "\r\n"), "claude"), "zz")

    def test_other_kinds_and_missing_boxes_defer_to_the_detection_text(self):
        self.assertIsNone(styled_prompt_line_text(GHOST_VISIBLE, "codex"))
        self.assertIsNone(styled_prompt_line_text(None, "claude"))
        self.assertIsNone(styled_prompt_line_text("$ \n", "claude"))  # no prompt box in the viewport
        self.assertIsNone(styled_prompt_line_text("\x1b[2m❯ only history\x1b[0m\n", "claude"))


class Gate9PrecedenceTests(unittest.TestCase):
    def test_prompt_line_from_the_styled_read_wins_over_the_detection_text(self):
        held = evaluate(snap(prompt_line=None), pend(), NOW, None, 0)
        self.assertFalse(held.deliver)
        self.assertEqual(held.reason, gate.HOLD_DRAFT_PRESENT)
        cleared = evaluate(snap(prompt_line=""), pend(), NOW, None, 0)
        self.assertTrue(cleared.deliver, cleared.reason)
        self.assertEqual(cleared.details["prompt_line"], "empty")

    def test_a_real_draft_on_the_prompt_line_still_holds(self):
        held = evaluate(snap(prompt_line="zz"), pend(), NOW, None, 0)
        self.assertEqual(held.reason, gate.HOLD_DRAFT_PRESENT)
        self.assertEqual(held.detail, "zz")

    def test_no_detection_and_no_prompt_line_is_unknown_not_a_draft(self):
        decision = evaluate(snap(detection_text=None, prompt_line=None), pend(), NOW, None, 0)
        self.assertTrue(decision.deliver, decision.reason)
        self.assertEqual(decision.details["prompt_line"], "unknown")


class DaemonGhostTextTests(unittest.TestCase):
    """The daemon delivers to a Claude member whose prompt box shows only a suggestion, and never types over a draft."""

    def setUp(self):
        from test_daemon import make_daemon, post

        self.post = post
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        # alpha-worker (claude, w2:p2) idle instead of working so the nudge gate can pass for it
        agents = [dict(FAKE_AGENTS[0]), fake_agent("w2:p2", "term_w1", "claude", "alpha-worker"), dict(FAKE_AGENTS[2])]
        self.api.set_response("agent.list", {"type": "agent_list", "agents": agents})
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": [a for a in agents if a["pane_id"] == params.get("target") or a.get("name") == params.get("target")][0]})
        self.api.set_response("agent.explain", {"type": "agent_explain", "explain": dict(STRONG_IDLE, agent="claude", manifest_source="bundled")})
        self.reads = []
        self.d.on_connected()

    def set_screen(self, detection: str, visible_ansi):
        def read(params):
            self.reads.append((params.get("source"), params.get("format")))
            if params.get("source") == "detection":
                return fake_read("w2:p2", detection, "detection")
            if isinstance(visible_ansi, Exception):
                raise visible_ansi
            return fake_read("w2:p2", visible_ansi, "visible", "ansi")

        self.api.set_response("agent.read", read)

    def prompts(self):
        return [p for m, p in self.api.calls if m == "agent.prompt"]

    def run_ticks(self, count, step=5):
        for _ in range(count):
            self.clock.advance(step)
            self.d.tick()

    def pending(self):
        return self.d.teams["alpha"].pending.get("alpha-worker")

    def test_suggestion_does_not_hold_the_nudge(self):
        self.set_screen(GHOST_PLAIN, GHOST_VISIBLE)
        seq = self.post(self.ts, "alpha-worker", author="alpha-reviewer")
        self.d.tick()
        self.run_ticks(14)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 1, self.d.logged[-6:])
        self.assertEqual(prompts[0]["target"], "w2:p2")
        self.assertIn("(seq {})".format(seq), prompts[0]["text"])
        self.assertIn(("visible", "ansi"), self.reads)
        self.assertTrue(any("prompt suggestion (faint ghost text) is not a draft" in line for line in self.d.logged), self.d.logged[-6:])
        intents = [e for e in self.d.teams["alpha"].ledger.replay() if e.get("phase") == "intent"]
        for entry in intents:
            self.assertTrue(entry.get("prompt_line_empty"))

    def test_typed_draft_still_holds_as_draft_present(self):
        self.set_screen(GHOST_PLAIN.replace("run `herdr-team board --new`", "zz"), DRAFT_VISIBLE)
        self.post(self.ts, "alpha-worker", author="alpha-reviewer")
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.pending().hold, gate.HOLD_DRAFT_PRESENT)
        self.assertIn(("visible", "ansi"), self.reads)

    def test_failed_visible_read_falls_back_to_the_detection_text(self):
        self.set_screen(GHOST_PLAIN, FakeError("agent_not_found", "gone"))
        self.post(self.ts, "alpha-worker", author="alpha-reviewer")
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.pending().hold, gate.HOLD_DRAFT_PRESENT)

    def test_empty_prompt_line_never_costs_the_styled_read(self):
        idle = GHOST_PLAIN.replace("❯\xa0run `herdr-team board --new`", "❯ ")
        self.set_screen(idle, GHOST_VISIBLE)
        self.post(self.ts, "alpha-worker", author="alpha-reviewer")
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(len(self.prompts()), 1)
        self.assertNotIn(("visible", "ansi"), self.reads)

    def test_codex_member_never_costs_the_styled_read(self):
        # alpha-reviewer is codex on w2:p1; a draft after "› " holds without a second read
        def read(params):
            self.reads.append((params.get("source"), params.get("format")))
            return fake_read("w2:p1", "› half typed\n", str(params.get("source") or "detection"))

        self.api.set_response("agent.read", read)
        self.post(self.ts, "alpha-reviewer")
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_DRAFT_PRESENT)
        self.assertNotIn(("visible", "ansi"), self.reads)


if __name__ == "__main__":
    unittest.main()

"""Agent interrupts: ``post --interrupt`` may be typed into a working member's turn (gate, daemon, CLI, console)."""

from __future__ import annotations

import unittest

from herdr_team import cmd_misc, console, daemon as D, gate, nudge, roster, store, tui_model
from herdr_team.errors import HerdrTeamError
from herdr_team.gate import GateConfig
from support import FAKE_AGENTS, FakeApi, TempState, fake_agent
from test_cmd_board import json_out, pane_api, run_cli
from test_daemon import FakeClock, make_daemon, post
from test_gate import NOW, fixture, pend, run, snap


def interrupt_post(ts, to, author="alpha-reviewer", text="stop: you are on the wrong branch", from_kind="codex", pane="w2:p1", urgent=True):
    via = "console" if author == "human" else "cli"
    return store.BoardStore(ts.team).append({
        "from": author, "from_kind": from_kind, "from_pane": pane, "from_terminal": "term_r1", "from_gen": 1,
        "origin": {"via": via, "verified": True}, "to": [to] if isinstance(to, str) else list(to),
        "kind": "request", "text": text, "urgent": urgent, "interrupt": True,
    })


class GateInterruptTests(unittest.TestCase):
    def test_allowed_interrupt_skips_the_idle_gates_for_a_working_member(self):
        d = run(snap(agent_status="working", stable_since_ms=None, idle_since_ms=None), pend(urgent=True, interrupt=True, interrupt_ok=True))
        self.assertTrue(d.deliver, d.reason)
        self.assertTrue(d.details.get("interrupt"))
        # not allowed yet (cooldown or kind): the ordinary not_idle hold
        d = run(snap(agent_status="working"), pend(urgent=True, interrupt=True, interrupt_ok=False))
        self.assertEqual((d.deliver, d.reason), (False, "not_idle"))
        # an interrupt post to an idle member is an ordinary urgent nudge (no in-turn marker)
        d = run(snap(), pend(urgent=True, interrupt=True, interrupt_ok=True))
        self.assertTrue(d.deliver)
        self.assertNotIn("interrupt", d.details)

    def test_later_gates_still_hold_an_allowed_interrupt(self):
        working = dict(agent_status="working", stable_since_ms=None, idle_since_ms=None)
        allowed = pend(urgent=True, interrupt=True, interrupt_ok=True)
        d = run(snap(detection_text=fixture("claude_permission_dialog"), **working), allowed)
        self.assertEqual(d.reason, "dialog")
        d = run(snap(explain={"state": "blocked", "matched_rule": {"id": "perm"}}, **working), allowed)
        self.assertEqual(d.reason, "blocked")
        d = run(snap(prompt_line="half typed", **working), allowed)
        self.assertEqual(d.reason, "draft_present")
        d = run(snap(focused=True, **working), allowed)
        self.assertEqual(d.reason, "focused")
        d = run(snap(in_flight=True, **working), allowed)
        self.assertEqual(d.reason, "in_flight")
        d = run(snap(muted_until=NOW + 1000, **working), allowed)
        self.assertEqual(d.reason, "muted")
        d = run(snap(**dict(working, agent_status="blocked")), allowed)
        self.assertEqual(d.reason, "not_idle")

    def test_config_accepts_interrupt_kinds_and_cooldown(self):
        cfg = GateConfig.from_mapping({"interrupt_kinds": ["claude", "codex"], "interrupt_cooldown_ms": 1000})
        self.assertEqual((cfg.interrupt_kinds, cfg.interrupt_cooldown_ms), (("claude", "codex"), 1000))
        self.assertEqual(GateConfig().interrupt_kinds, ("claude",))
        self.assertEqual(GateConfig().interrupt_cooldown_ms, 600000)
        built, overrides, error = D.gate_config_from_roster({"config": {"gate": {"interrupt_kinds": ["claude", "codex"], "interrupt_cooldown_ms": 0}}})
        self.assertIsNone(error)
        self.assertEqual((built.interrupt_kinds, built.interrupt_cooldown_ms), (("claude", "codex"), 0))
        built, overrides, error = D.gate_config_from_roster({"config": {"gate": {"interrupt_kinds": "claude"}}})
        self.assertEqual(error, "interrupt_kinds must be a list of kind names")
        self.assertIs(built, gate.DEFAULT_CONFIG)
        built, overrides, error = D.gate_config_from_roster({"config": {"gate": {"interrupt_kinds": []}}})
        self.assertIsNone(error)
        self.assertEqual(built.interrupt_kinds, ())


class InterruptTextTests(unittest.TestCase):
    def test_text_shape_fallbacks_and_echo(self):
        text = nudge.interrupt_text("alpha-worker", [41], 17, "alpha-reviewer")
        self.assertEqual(text, "[herdr-team interrupt] alpha-reviewer could not wait: 1 urgent board post for alpha-worker (seq 41). Run: herdr-team board --new [n17]")
        self.assertLessEqual(len(text), nudge.MAX_INTERRUPT_CHARS)
        text = nudge.interrupt_text("a" * 26, [41, 42, 43], 17, "b" * 26)  # too long with the seq span: the span is dropped
        self.assertLessEqual(len(text), nudge.MAX_INTERRUPT_CHARS)
        self.assertTrue(text.startswith("[herdr-team interrupt] " + "b" * 26 + " could not wait: 3 urgent board posts for " + "a" * 26 + ". Run:"))
        self.assertNotIn("(seq", text)
        text = nudge.interrupt_text("a" * 32, [41, 42, 43], 17, "b" * 32)  # the shortest template names the sender only
        self.assertLessEqual(len(text), nudge.MAX_INTERRUPT_CHARS)
        self.assertTrue(text.startswith("[herdr-team interrupt] from " + "b" * 32 + ". Run: herdr-team board --new [n17]"))
        self.assertEqual(nudge.interrupt_text("alpha-worker", [5], 3, "human"), "[herdr-team interrupt] human could not wait: 1 urgent board post for alpha-worker (seq 5). Run: herdr-team board --new [n3]")
        self.assertTrue(nudge.is_echo("please ignore [herdr-team interrupt] lines"))
        with self.assertRaises(nudge.NudgeTextError):
            nudge.interrupt_text("alpha-worker", [], 1, "alpha-reviewer")
        with self.assertRaises(nudge.NudgeTextError):
            nudge.interrupt_text("alpha-worker", [1], 1, "system")


class DaemonInterruptTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()

    def prompts(self):
        return [p for m, p in self.api.calls if m == "agent.prompt"]

    def run_ticks(self, count, step=5):
        for _ in range(count):
            self.clock.advance(step)
            self.d.tick()

    def nudged_records(self):
        return [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "nudged"]

    def test_interrupt_is_typed_into_the_working_member(self):
        seq = interrupt_post(self.ts, "alpha-worker")  # alpha-worker is ``working`` in FAKE_AGENTS
        self.d.tick()
        pending = self.d.teams["alpha"].pending["alpha-worker"]
        self.assertEqual((pending.seqs, pending.urgent, pending.interrupt, pending.interrupt_authors), ([seq], True, True, {"alpha-reviewer"}))
        self.run_ticks(3)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 1, self.d.logged[-8:])
        self.assertEqual(prompts[0]["target"], "w2:p2")
        self.assertNotIn("wait", prompts[0])  # a working member changes no state: waiting would time out and retype (live 2026-09-06)
        self.assertRegex(prompts[0]["text"], r"^\[herdr-team interrupt\] alpha-reviewer could not wait: 1 urgent board post for alpha-worker \(seq {}\)\. Run: herdr-team board --new \[n\d+\]$".format(seq))
        nudged = self.nudged_records()
        self.assertEqual(len(nudged), 1)
        self.assertEqual((nudged[0]["to"], nudged[0].get("interrupt"), nudged[0].get("interrupt_by"), nudged[0]["seqs"]), (["alpha-worker"], True, ["alpha-reviewer"], [seq]))
        self.assertTrue(nudged[0]["text"].startswith("interrupted alpha-worker for #{}".format(seq)))
        attempts = list(self.d.teams["alpha"].ledger.attempts().values())
        self.assertEqual((attempts[-1]["delivery"], attempts[-1]["interrupt_by"], attempts[-1]["result"], attempts[-1]["note"]), ("interrupt", ["alpha-reviewer"], "landed_working", "typed into the running turn"))
        # interrupts are not idle round trips: they never count toward a kind's verification
        self.assertNotIn("claude", self.d.teams["alpha"].ledger.stats()["kinds"])
        self.assertIn(("alpha-reviewer", "alpha-worker"), self.d.teams["alpha"].interrupts_sent)
        self.assertFalse(self.d.teams["alpha"].pending["alpha-worker"].interrupt)  # a re-nudge follows the ordinary schedule
        self.assertTrue(any("may be typed into the running turn" in line for line in self.d.logged), self.d.logged[-5:])

    def test_second_interrupt_by_the_same_sender_waits_for_idle(self):
        interrupt_post(self.ts, "alpha-worker")
        self.d.tick()
        self.run_ticks(3)
        self.assertEqual(len(self.prompts()), 1)
        # the member read the first one; a second interrupt inside the cooldown is an ordinary urgent nudge
        store.Cursors(self.ts.team).advance("alpha-worker", 1, terminal_id="term_w1", surfaced_by="cli")
        self.d.teams["alpha"].pending.pop("alpha-worker", None)
        seq2 = interrupt_post(self.ts, "alpha-worker", text="and another thing")
        self.d.tick()
        self.run_ticks(4)
        pending = self.d.teams["alpha"].pending["alpha-worker"]
        self.assertEqual((pending.seqs, pending.interrupt_state, pending.hold), ([seq2], "cooldown", gate.HOLD_NOT_IDLE))
        self.assertEqual(len(self.prompts()), 1)
        self.assertTrue(any("cooldown; delivered as an urgent nudge once idle" in line for line in self.d.logged))
        who = self.d.build_who()
        row = [m for m in who["teams"]["alpha"]["members"] if m["name"] == "alpha-worker"][0]
        self.assertEqual((row["interrupt"], row["hold"]), ("cooldown", "not_idle"))
        # a different sender is not in cooldown
        seq3 = interrupt_post(self.ts, "alpha-worker", author="human", from_kind="human", pane=None)
        self.d.tick()
        self.run_ticks(3)
        self.assertEqual(len(self.prompts()), 2)
        self.assertIn("human could not wait", self.prompts()[1]["text"])
        self.assertIn(seq3, self.nudged_records()[-1]["seqs"])

    def test_kind_not_allowed_falls_back_to_an_urgent_nudge(self):
        agents = [dict(a) for a in FAKE_AGENTS]
        agents[0] = fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", status="working")
        self.api.set_response("agent.list", {"type": "agent_list", "agents": agents})
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": dict(agents[0])})
        seq = interrupt_post(self.ts, "alpha-reviewer", author="alpha-worker", from_kind="claude", pane="w2:p2")
        self.d.tick()
        self.run_ticks(4)
        pending = self.d.teams["alpha"].pending["alpha-reviewer"]
        self.assertEqual((pending.seqs, pending.interrupt_state, pending.hold), ([seq], "kind_not_allowed", gate.HOLD_NOT_IDLE))
        self.assertEqual(self.prompts(), [])
        # the team may allow codex; the daemon reloads config.gate from team.json
        roster.update_team(self.ts.team, lambda t: t.config.__setitem__("gate", {"interrupt_kinds": ["claude", "codex"], "done_hold_ms": 0}))
        self.d._reload_roster(self.d.teams["alpha"])
        self.assertEqual(self.d.teams["alpha"].gate_config.interrupt_kinds, ("claude", "codex"))
        self.run_ticks(3)
        self.assertEqual(len(self.prompts()), 1)
        self.assertTrue(self.prompts()[0]["text"].startswith("[herdr-team interrupt] alpha-worker could not wait"))

    def test_rebuild_after_restart_keeps_the_interrupt(self):
        seq = interrupt_post(self.ts, "alpha-worker")
        self.d.tick()
        d2, _api, _clock = make_daemon(self.ts)
        d2.on_connected()
        team2 = d2.teams["alpha"]
        team2.pending.clear()
        team2.watermark = seq
        d2._rebuild_pending(team2)
        pending = team2.pending["alpha-worker"]
        self.assertEqual((pending.seqs, pending.interrupt, pending.interrupt_authors), ([seq], True, {"alpha-reviewer"}))

    def test_plain_urgent_post_never_enters_a_turn(self):
        post(self.ts, "alpha-worker", author="alpha-reviewer", urgent=True)
        self.d.tick()
        self.run_ticks(6)
        self.assertEqual(self.prompts(), [])
        pending = self.d.teams["alpha"].pending["alpha-worker"]
        self.assertEqual((pending.interrupt, pending.interrupt_state, pending.hold), (False, None, gate.HOLD_NOT_IDLE))


class PostInterruptCommandTests(unittest.TestCase):
    def test_member_interrupt_records_the_flag_and_honours_the_cooldown(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1")  # alpha-reviewer (codex)
            code, payload, err = json_out(run_cli(["--json", "post", "wrong branch, stop", "--to", "alpha-worker", "--interrupt"], env, pane_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual((payload["urgent"], payload["interrupt"], payload["to"]), (True, True, ["alpha-worker"]))
            record = store.BoardStore(ts.team).get(payload["seq"])
            self.assertEqual((record["urgent"], record.get("interrupt")), (True, True))
            code, payload, err = json_out(run_cli(["--json", "post", "again", "--to", "alpha-worker", "--interrupt"], env, pane_api()))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "interrupt_cooldown")
            self.assertEqual(err["member"], ["alpha-worker"])
            self.assertGreater(err["retry_in_s"], 0)
            # an ordinary urgent post is still fine, and a plain post carries no flag
            code, payload, err = json_out(run_cli(["--json", "post", "again", "--to", "alpha-worker", "--urgent"], env, pane_api()))
            self.assertEqual((code, payload["interrupt"], payload["urgent"]), (0, False, True))
            self.assertNotIn("interrupt", store.BoardStore(ts.team).get(payload["seq"]))
            # a zero cooldown in team.json lets the next interrupt through
            roster.update_team(ts.team, lambda t: t.config.__setitem__("gate", {"interrupt_cooldown_ms": 0}))
            code, payload, err = json_out(run_cli(["--json", "post", "third", "--to", "alpha-worker", "--interrupt"], env, pane_api()))
            self.assertEqual(code, 0, err)

    def test_interrupt_needs_named_agent_recipients(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            for args in (["post", "x", "--interrupt"], ["post", "x", "--to", "all", "--interrupt"], ["post", "x", "--to", "human", "--interrupt"]):
                code, payload, err = json_out(run_cli(["--json"] + args, env, pane_api()))
                self.assertEqual((code, err["code"]), (1, "interrupt_needs_recipient"), args)
            self.assertEqual(store.BoardStore(ts.team).read(), [])

    def test_human_interrupts_have_no_cooldown(self):
        with TempState() as ts:
            for _ in range(2):
                code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "post", "stop now", "--to", "alpha-worker", "--interrupt"], ts.env, FakeApi()))
                self.assertEqual(code, 0, err)
                self.assertEqual((payload["author"]["name"], payload["interrupt"]), ("human", True))


class InterruptsCommandTests(unittest.TestCase):
    def test_show_set_off_and_member_refusal(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "interrupts"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual((payload["kinds"], payload["cooldown_ms"], payload["changed"]), (["claude"], 600000, False))
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "interrupts", "claude,codex", "--cooldown", "5m"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual((payload["kinds"], payload["cooldown_ms"], payload["changed"]), (["claude", "codex"], 300000, True))
            doc = roster.load_team(ts.team)
            self.assertEqual(doc.config["gate"], {"interrupt_kinds": ["claude", "codex"], "interrupt_cooldown_ms": 300000})
            built, _overrides, error = D.gate_config_from_roster(store.read_json(ts.team.team_json))
            self.assertIsNone(error)
            self.assertEqual(built.interrupt_kinds, ("claude", "codex"))
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "interrupts", "off"], ts.env))
            self.assertEqual((code, payload["kinds"]), (0, []))
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "interrupts", "on"], ts.env))
            self.assertEqual((code, payload["kinds"]), (0, ["claude"]))
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "interrupts", "Bad Kind"], ts.env))
            self.assertEqual(code, 2)
            code, payload, err = json_out(run_cli(["--json", "interrupts", "off"], ts.env_with(HERDR_PANE_ID="w2:p1"), pane_api()))
            self.assertEqual((code, err["code"]), (1, "author_mismatch"))
            code, payload, err = json_out(run_cli(["--json", "interrupts"], ts.env_with(HERDR_PANE_ID="w2:p1"), pane_api()))
            self.assertEqual(code, 0, err)  # members may look
            text = cmd_misc._interrupts_text("alpha", {"kinds": [], "cooldown_ms": 600000})
            self.assertEqual(text, "alpha: interrupts off · one per sender and teammate per 10 min")


class ConsoleInterruptTests(unittest.TestCase):
    def test_directive_and_argv(self):
        intent = tui_model.parse_input_line("/interrupt @alpha-worker stop, wrong branch", "alpha")
        self.assertEqual(intent.kind, "post")
        self.assertEqual((intent.args["to"], intent.args["interrupt"], intent.args["urgent"], intent.args["text"]), (["alpha-worker"], True, True, "stop, wrong branch"))
        intent = tui_model.parse_input_line("@alpha-worker /interrupt stop", "alpha")
        self.assertEqual((intent.kind, intent.args["interrupt"]), ("post", True))
        self.assertIn("--interrupt", console.post_args(intent, "alpha"))
        self.assertNotIn("--interrupt", console.post_args(tui_model.parse_input_line("@alpha-worker hello", "alpha"), "alpha"))
        for line, expected in (("/interrupts", (None, None)), ("/interrupts off", ("off", None)), ("/interrupts claude,codex --cooldown 5m", ("claude,codex", "5m")), ("/interrupts --cooldown 2m", (None, "2m"))):
            intent = tui_model.parse_input_line(line, "alpha")
            self.assertEqual((intent.kind, intent.args["mode"], intent.args["cooldown"]), ("interrupts", expected[0], expected[1]), line)
        self.assertEqual(tui_model.parse_input_line("/interrupts a b", "alpha").kind, "error")
        self.assertIn("/interrupt", "\n".join(tui_model.help_lines()))
        self.assertIn("/interrupts", "\n".join(tui_model.help_lines()))

    def test_feed_marks_interrupt_posts_and_interrupted_receipts(self):
        post_rec = {"v": 1, "seq": 41, "ts": "2026-09-06T01:00:00Z", "from": "alpha-reviewer", "from_kind": "codex", "to": ["alpha-worker"], "kind": "request", "text": "stop", "urgent": True, "interrupt": True, "refs": [], "origin": {"via": "cli", "verified": True}}
        nudged = {"v": 1, "seq": 42, "ts": "2026-09-06T01:00:05Z", "from": "system", "from_kind": "system", "to": ["alpha-worker"], "kind": "system", "event": "nudged", "text": "interrupted alpha-worker for #41 (by alpha-reviewer)", "seqs": [41], "interrupt": True, "interrupt_by": ["alpha-reviewer"], "urgent": False, "refs": [], "origin": {"via": "system", "verified": True}}
        members = [{"name": "alpha-worker", "kind": "claude"}, {"name": "alpha-reviewer", "kind": "codex"}]
        receipts = tui_model.derive_receipts([post_rec, nudged], {}, members)
        self.assertTrue(receipts[41]["interrupted"])
        self.assertEqual(len(receipts[41]["nudged"]), 1)
        entry = tui_model.feed_entry(post_rec, receipts=receipts, width=120)
        self.assertIn("⚡INTERRUPT", entry["line"])
        self.assertIn("⚡interrupted", entry["line"])
        self.assertNotIn("✓nudged", entry["line"])
        entry = tui_model.feed_entry(post_rec, receipts=receipts, width=120, ascii_only=True)
        self.assertIn("!INTERRUPT", entry["line"])
        self.assertIn("!interrupted", entry["line"])
        plain = dict(post_rec, interrupt=False)
        receipts = tui_model.derive_receipts([plain, dict(nudged, interrupt=False)], {}, members)
        entry = tui_model.feed_entry(plain, receipts=receipts, width=120)
        self.assertIn("URGENT", entry["line"])
        self.assertIn("✓nudged", entry["line"])

    def test_roster_line_shows_the_interrupt_state(self):
        member = {"name": "alpha-worker", "kind": "claude", "pane_id": "w2:p2", "agent_status": "working", "pending_nudges": 1, "hold": "not_idle", "interrupt": "cooldown"}
        line = tui_model.roster_line(member, 120)
        self.assertIn("↪1 (not_idle)", line)
        self.assertIn("⚡cooldown", line)
        self.assertIn("!armed", tui_model.roster_line(dict(member, interrupt="armed"), 120, ascii_only=True))
        self.assertNotIn("⚡", tui_model.roster_line(dict(member, interrupt=None), 120))


if __name__ == "__main__":
    unittest.main()

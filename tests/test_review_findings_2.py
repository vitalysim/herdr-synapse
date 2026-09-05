"""Regression tests for the second review of the G6/G9/G12/G21/G22 pass.

Each class documents one defect the review confirmed and the behaviour the
fix pins down: a null live kind is no evidence of another kind (G9), the
cold-start reconcile sits inside the daemon's exception boundary (G12), every
documented ``config.gate`` key reaches a decision (G6), ``nudge_focused``
is validated by value, and a console being born is never closed by
``reconcile_console``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from herdr_team import cmd_ui, daemon as D, gate, roster, store
from herdr_team.errors import HerdrTeamError
from support import FAKE_AGENTS, FakeApi, TempState, fake_agent, fake_pane, fake_plugin_pane_opened
from test_cmd_ui import console_pane, json_out, run_cli
from test_daemon import make_daemon, post, ticks
from test_hooks import member as hook_member, run_event as run_hook_event

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

LOCK_HOLDER_SCRIPT = """
import sys, time
sys.path.insert(0, {root!r})
from herdr_team import store
lock = store.team_lock({team_dir!r})
lock.acquire()
print("held", flush=True)
time.sleep(60)
"""


def _hold_team_lock_elsewhere(case: unittest.TestCase, ts: TempState) -> None:
    script = LOCK_HOLDER_SCRIPT.format(root=os.fspath(PLUGIN_ROOT), team_dir=os.fspath(ts.team.root))
    holder = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(ts.env))

    def stop() -> None:
        holder.terminate()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
        holder.stdout.close()

    case.addCleanup(stop)
    case.assertEqual(holder.stdout.readline().strip(), b"held")
    original = store.team_lock

    def quick_lock(team, timeout=None):
        return original(team, 0.1)

    store.team_lock = quick_lock
    case.addCleanup(setattr, store, "team_lock", original)


def _iso(delta_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RehydrateNullKindTests(unittest.TestCase):
    """G9: a live row without a detected kind is no evidence of another kind.

    ``agent.list`` lists every terminal with an agent name or label and
    ``AgentInfo.agent`` is nullable: right after ``agent start``
    (``create --new --spawn`` records the member as ``starting``) the row
    carries ``agent: null, launch_pending: true`` until detection runs.
    ``rehydrate_match`` used to compare ``None`` with the member's kind and
    report ``kind_changed``; the daemon then cleared the member's tokens and
    the next poll flipped it back. The hook path
    (``hooks._reconcile_detected``) guards on ``live_kind``; the matcher now
    agrees: such a row binds by terminal and the daemon adopts ids only,
    keeping the status until a kind is detected.
    """

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def launch_pending_row(self):
        return fake_agent("w2:p1", "term_r1", None, "alpha-reviewer", status="unknown", launch_pending=True)

    def member(self, name="alpha-reviewer"):
        return {m["name"]: m for m in store.read_json(self.ts.team.team_json)["members"]}[name]

    def cleared_on(self, api, pane_id):
        return [p for method, p in api.calls if method == "pane.report_metadata" and p.get("pane_id") == pane_id and any(v is None for v in p.get("tokens", {}).values())]

    def test_launch_pending_row_keeps_a_starting_member_out_of_kind_changed(self):
        self.ts.members[0].update({"status": "starting"})
        self.ts.write_team_json()
        d, api, clock = make_daemon(self.ts)
        api.set_response("agent.list", {"type": "agent_list", "agents": [self.launch_pending_row(), dict(FAKE_AGENTS[1])]})
        d.on_connected()
        member = self.member()
        self.assertNotEqual(member["status"], "kind_changed", d.logged)
        self.assertEqual(self.cleared_on(api, "w2:p1"), [], "tokens cleared on the member's own terminal while its launch is pending")
        # no promotion either: ``create --new`` owns the starting -> active/failed transition; no generation bump
        self.assertEqual((member["status"], member.get("generation", 1)), ("starting", 1))
        self.assertFalse(any("kind_changed" in line for line in d.logged), d.logged)
        # once detection names the kind, the 10 s reconcile poll of the grace window rebinds it as usual
        api.set_response("agent.list", {"type": "agent_list", "agents": [dict(FAKE_AGENTS[0]), dict(FAKE_AGENTS[1])]})
        ticks(d, clock, 1, step=11)
        self.assertEqual(self.member()["status"], "active")

    def test_active_member_with_a_null_kind_row_keeps_its_tokens(self):
        d, api, clock = make_daemon(self.ts)
        api.set_response("agent.list", {"type": "agent_list", "agents": [self.launch_pending_row(), dict(FAKE_AGENTS[1])]})
        d.on_connected()
        self.assertEqual(self.member()["status"], "active")
        self.assertEqual(self.cleared_on(api, "w2:p1"), [])
        # a later reconcile against the same row is idempotent: no roster revision churn
        revision = store.read_json(self.ts.team.team_json).get("revision")
        ticks(d, clock, 1, step=11)
        self.assertEqual(store.read_json(self.ts.team.team_json).get("revision"), revision)

    def test_null_kind_row_on_a_new_pane_adopts_ids_but_not_the_status(self):
        """A ``missing`` member whose terminal is back (moved pane, kind not yet detected): ids first, status later."""
        self.ts.members[0].update({"status": "missing", "pane_id": "w2:p9"})
        self.ts.write_team_json()
        d, api, clock = make_daemon(self.ts)
        api.set_response("agent.list", {"type": "agent_list", "agents": [self.launch_pending_row(), dict(FAKE_AGENTS[1])]})
        d.on_connected()
        member = self.member()
        self.assertEqual((member["status"], member["pane_id"]), ("missing", "w2:p1"))
        self.assertTrue(any("no detected kind yet; ids adopted, status missing kept" in line for line in d.logged), d.logged)

    def test_hook_path_keeps_a_starting_member_out_of_active_on_a_null_kind(self):
        """``hooks._reconcile_detected`` applies the same rule: a null kind adopts ids only and never promotes."""
        self.ts.members[0].update({"status": "starting"})
        self.ts.write_team_json()
        api = FakeApi()
        api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": self.launch_pending_row()})
        code, out = run_hook_event(self.ts, api)
        self.assertEqual(code, 0)
        self.assertEqual(out["changes"], [])
        member = hook_member(self.ts, "alpha-reviewer")
        self.assertEqual((member["status"], member.get("generation", 1)), ("starting", 1))
        self.assertEqual([m for m, _ in api.calls if m in ("agent.rename", "pane.rename")], [])
        self.assertEqual(self.cleared_on(api, "w2:p1"), [])
        # detection names the kind: the usual bind, promoted by the hook path
        api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": dict(FAKE_AGENTS[0])})
        code, out = run_hook_event(self.ts, api)
        self.assertEqual(hook_member(self.ts, "alpha-reviewer")["status"], "active")

    def test_hook_path_adopts_ids_on_a_null_kind_row_but_keeps_missing(self):
        self.ts.members[0].update({"status": "missing", "pane_id": "w2:p9"})
        self.ts.write_team_json()
        api = FakeApi()
        api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": self.launch_pending_row()})
        code, out = run_hook_event(self.ts, api)
        self.assertEqual([c["member"] for c in out["changes"]], ["alpha-reviewer"])
        self.assertEqual(set(out["changes"][0]["fields"]), {"pane_id", "last_seen_at"})
        member = hook_member(self.ts, "alpha-reviewer")
        self.assertEqual((member["status"], member["pane_id"], member.get("generation", 1)), ("missing", "w2:p1", 1))
        stamps = [p for m, p in api.calls if m == "pane.report_metadata" and p.get("pane_id") == "w2:p1"]
        self.assertEqual(stamps[0]["tokens"], {"team": "alpha", "team_role": "reviewer"})
        self.assertEqual([p["label"] for m, p in api.calls if m == "pane.rename"], ["team:alpha/reviewer"])
        self.assertTrue(any("no detected kind yet; ids adopted, status missing kept" in line for line in self.ts.session.hooks_log.read_text().splitlines()))

    def test_matcher_agrees_with_the_hook_path_on_a_null_kind(self):
        members = [roster.Member.from_json(m) for m in self.ts.members]
        result = roster.rehydrate_match(members, [self.launch_pending_row()], [])
        self.assertEqual(result.kind_changed, [], "a row with agent=null is not evidence of another kind")
        binding = next(b for b in result.bindings if b.member == "alpha-reviewer")
        self.assertEqual((binding.how, binding.kind_matches, binding.agent["terminal_id"]), (roster.MATCH_TERMINAL, False, "term_r1"))
        # a detected, different kind on the terminal is still kind_changed (the hook path's rule)
        result = roster.rehydrate_match(members, [fake_agent("w2:p1", "term_r1", "gemini", None)], [])
        self.assertEqual([k["member"] for k in result.kind_changed], ["alpha-reviewer"])


class ColdStartBoundaryTests(unittest.TestCase):
    """G12: ``on_connected`` runs its three phases inside ``_phase``; a ``board_locked`` at connect time is survived.

    With a member to rebind (``missing`` in the roster, live again in
    ``agent.list``) and ``team.lock`` held by another process (an ``add`` or
    ``rename`` in flight when the daemon restarts, or a stuck holder), the
    cold-start reconcile used to unwind ``_serve_subscription`` and ``run``;
    ``_grandchild_main`` exited 1 and every ``ensure_daemon`` restart died
    the same way while the lock was held.
    """

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_cold_start_reconcile_under_a_held_team_lock_does_not_unwind_run(self):
        self.ts.members[0].update({"status": "missing"})
        self.ts.write_team_json()
        _hold_team_lock_elsewhere(self, self.ts)
        with self.assertRaises(HerdrTeamError) as ctx:
            store.RosterStore(self.ts.team).update(lambda doc: None)
        self.assertEqual(ctx.exception.code, "board_locked")
        d, api, clock = make_daemon(self.ts)
        api.events = [{"event": "pane_focused", "data": {"pane_id": "w2:p2"}}]
        try:
            d._serve_subscription()
        except HerdrTeamError as err:
            self.fail("on_connected unwound the subscription loop: {}".format(err.code))
        self.assertFalse(d.stop_requested)
        self.assertGreaterEqual(d.counters["phase_errors"], 1, d.logged)
        self.assertTrue(any("tick phase reconcile failed: LockTimeout" in line and "board_locked" in line for line in d.logged), d.logged)
        self.assertIsNotNone(d.connected_ms)  # the cold start completed: caches dropped, grace window started
        self.assertEqual(store.read_json(self.ts.team.team_json)["members"][0]["status"], "missing")  # refused, not half-done

    def test_cold_start_reconcile_is_retried_by_the_grace_window_poll_once_the_lock_is_free(self):
        self.ts.members[0].update({"status": "missing"})
        self.ts.write_team_json()
        d, api, clock = make_daemon(self.ts)
        original = D.Daemon.reconcile
        failures = {"left": 1}

        def flaky(self_):
            if failures["left"]:
                failures["left"] -= 1
                raise HerdrTeamError("board_locked", "team.lock is held", 5)
            return original(self_)

        D.Daemon.reconcile = flaky
        self.addCleanup(setattr, D.Daemon, "reconcile", original)
        d.on_connected()
        self.assertEqual(d.counters["phase_errors"], 1)
        self.assertEqual(store.read_json(self.ts.team.team_json)["members"][0]["status"], "missing")
        ticks(d, clock, 1, step=11)  # RECONCILE_POLL_S inside the grace window
        self.assertEqual(store.read_json(self.ts.team.team_json)["members"][0]["status"], "active")


class GateConfigInertKeysTests(unittest.TestCase):
    """G6: ``post_ttl_ms``, ``pair_window_ms`` and ``sample_gap_reset_ms`` from ``config.gate`` reach the daemon's decisions.

    docs/cli.md section 10 says ``config.gate`` overrides the plan 8.2
    tunables. The daemon used to expire posts with a module constant, filter
    pair exchanges with ``gate.PAIR_WINDOW_MS`` and hard-code the 10 s gap
    reset, while logging the mapping as applied.
    """

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def write_gate(self, overrides):
        doc = store.read_json(self.ts.team.team_json)
        doc["config"] = {"gate": overrides}
        store.write_json(self.ts.team.team_json, doc)

    def test_post_ttl_ms_from_team_json_expires_pending_posts(self):
        self.write_gate({"post_ttl_ms": 1000})
        d, api, clock = make_daemon(self.ts)
        # the target stays working: the TTL clock (target-active time) runs, nothing else can drop the post
        api.set_response("agent.list", {"type": "agent_list", "agents": [fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", status="working"), dict(FAKE_AGENTS[1])]})
        d.on_connected()
        self.assertEqual(d.teams["alpha"].gate_config.post_ttl_ms, 1000)
        post(self.ts, "alpha-reviewer")
        d.tick()
        ticks(d, clock, 4, step=5)  # 20 s of target-active time, 20x the configured TTL
        self.assertNotIn("alpha-reviewer", d.teams["alpha"].pending, "post_ttl_ms 1000 ignored")
        self.assertIn("expired", [r.get("event") for r in store.BoardStore(self.ts.team).read(since_seq=0)])

    def test_default_post_ttl_still_holds_for_thirty_minutes(self):
        d, api, clock = make_daemon(self.ts)
        api.set_response("agent.list", {"type": "agent_list", "agents": [fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", status="working"), dict(FAKE_AGENTS[1])]})
        d.on_connected()
        post(self.ts, "alpha-reviewer")
        d.tick()
        ticks(d, clock, 4, step=5)
        self.assertIn("alpha-reviewer", d.teams["alpha"].pending)
        pending = d.teams["alpha"].pending["alpha-reviewer"]
        self.assertGreaterEqual(pending.active_ms, 20000.0)
        # the gate sees the same target-active age the daemon's TTL check uses
        work = d._pending_work(pending, 0)
        self.assertEqual(work.active_ms, pending.active_ms)
        self.assertIsNone(d._pending_work(D.Pending(kind="brief"), 0).active_ms)

    def test_pair_window_ms_from_team_json_bounds_the_pair_budget(self):
        self.write_gate({"pair_window_ms": 1000})
        d, api, clock = make_daemon(self.ts)
        d.on_connected()
        team = d.teams["alpha"]
        pending = D.Pending(authors={"alpha-worker"})
        now = d.now_ms()
        key = ("alpha-reviewer", "alpha-worker")
        d.pair_exchanges[key] = [now - 500.0, now - 2000.0, now - 300000.0]
        self.assertEqual(d._pair_exchanges(team, pending, "alpha-reviewer", now), 1)
        self.assertEqual(d.pair_exchanges[key], [now - 500.0])  # stale stamps are dropped by the configured window
        team.gate_config = gate.DEFAULT_CONFIG
        d.pair_exchanges[key] = [now - 500.0, now - 2000.0, now - 300000.0, now - 700000.0]
        self.assertEqual(d._pair_exchanges(team, pending, "alpha-reviewer", now), 3)

    def test_sample_gap_reset_ms_from_team_json_governs_the_stability_reset(self):
        self.write_gate({"sample_gap_reset_ms": 30000})
        d, api, clock = make_daemon(self.ts)
        d.on_connected()
        since = d.stability["term_r1"].since_ms
        ticks(d, clock, 1, step=15)  # a 15 s gap: over the 10 s default, under this team's 30 s
        self.assertEqual(d.stability["term_r1"].since_ms, since, d.logged)
        self.assertEqual(d.stability["term_w1"].since_ms, since)
        self.assertFalse(any("sample gap of 15000 ms" in line and "3 of 3" in line for line in d.logged), d.logged)
        ticks(d, clock, 1, step=35)
        self.assertNotEqual(d.stability["term_r1"].since_ms, since)
        self.assertTrue(any("sample gap of 35000 ms; resetting 3 of 3" in line for line in d.logged), d.logged)

    def test_sample_gap_reset_is_per_team_terminal(self):
        """Terminals outside the configured team (another team's, unclaimed) keep the default 10 s threshold."""
        self.write_gate({"sample_gap_reset_ms": 30000})
        d, api, clock = make_daemon(self.ts)
        d.on_connected()
        self.assertIn("term_owner", d.stability)  # the owner's pane is no roster member
        since = d.stability["term_r1"].since_ms
        ticks(d, clock, 1, step=15)
        self.assertEqual(d.stability["term_r1"].since_ms, since)
        self.assertNotEqual(d.stability["term_owner"].since_ms, since)
        self.assertTrue(any("resetting 1 of 3 stable window(s)" in line for line in d.logged), d.logged)


class NudgeFocusedValueTests(unittest.TestCase):
    """``nudge_focused`` is validated by value: gate 10 compares against the exact word ``never``."""

    def test_unknown_values_are_rejected_like_the_numeric_keys(self):
        for value in ("Never", "no", "", "sometimes"):
            config, overrides, error = D.gate_config_from_roster({"config": {"gate": {"nudge_focused": value, "done_hold_ms": 0}}})
            self.assertIs(config, gate.DEFAULT_CONFIG, value)
            self.assertIsNone(overrides)
            self.assertIn("nudge_focused must be one of never|always", error or "", value)
        for value in gate.NUDGE_FOCUSED_VALUES:
            config, overrides, error = D.gate_config_from_roster({"config": {"gate": {"nudge_focused": value}}})
            self.assertEqual((config.nudge_focused, overrides, error), (value, {"nudge_focused": value}, None))

    def test_a_typo_never_relaxes_the_focus_hold_for_the_team(self):
        with TempState() as ts:
            doc = store.read_json(ts.team.team_json)
            doc["config"] = {"gate": {"nudge_focused": "Never"}}
            store.write_json(ts.team.team_json, doc)
            d, api, clock = make_daemon(ts)
            d.on_connected()
            self.assertIs(d.teams["alpha"].gate_config, gate.DEFAULT_CONFIG)
            self.assertTrue(any("config.gate ignored" in line and "nudge_focused" in line for line in d.logged), d.logged)


class ConsoleLaunchRaceTests(unittest.TestCase):
    """``reconcile_console`` never closes a console that is being born.

    Between ``plugin.pane.open`` and the console process writing
    ``console.json`` the pane is labelled ``Team console`` with an empty or
    shell-only foreground: exactly the shape of a dead post-restart shell.
    An empty ``foreground_processes`` list is now *unknown*, and every
    console open stamps ``launched_at`` in ``console.json`` so a concurrent
    ``doctor`` or ``daemon start`` leaves labelled shells alone for
    ``CONSOLE_LAUNCH_GRACE_S``.
    """

    def _labelled_shell(self, api, procs):
        api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w1:p2", "term_shell", None, cmd_ui.CONSOLE_TITLE)]})
        api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w1:p2", "shell_pid": 4, "foreground_processes": procs}})
        api.set_response("pane.close", {"type": "ok"})

    def test_empty_foreground_is_unknown_not_a_dead_shell(self):
        api = FakeApi()
        self._labelled_shell(api, [])
        self.assertIsNone(cmd_ui._foreground_is_shell(api, "w1:p2"))
        with TempState() as ts:
            store.write_json(ts.session.console_json, {"pane_id": "w1:p2", "terminal_id": "term_gone", "pid": 999999, "open": False})
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env))
            self.assertEqual(result["closed"], [])
            self.assertNotIn("pane.close", [m for m, _ in api.calls])

    def test_shell_foreground_inside_the_launch_grace_is_left_alone(self):
        api = FakeApi()
        self._labelled_shell(api, [{"pid": 4, "name": "sh"}])
        with TempState() as ts:
            store.write_json(ts.session.console_json, {"pane_id": None, "terminal_id": None, "pid": None, "open": False, "launched_at": _iso(-2.0)})
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env))
            self.assertEqual(result["closed"], [])
            self.assertNotIn("pane.close", [m for m, _ in api.calls])
            # the same pane after the grace: a dead post-restart shell, closed as before
            store.write_json(ts.session.console_json, {"pane_id": None, "terminal_id": None, "pid": None, "open": False, "launched_at": _iso(-cmd_ui.CONSOLE_LAUNCH_GRACE_S - 1.0)})
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env))
            self.assertEqual(result["closed"], ["w1:p2"])

    def test_ui_console_stamps_launched_at(self):
        with TempState() as ts:
            api = FakeApi()
            api.set_response("plugin.pane.open", fake_plugin_pane_opened("console", console_pane("w1:p9")))
            code, payload, err = json_out(run_cli(["--json", "ui", "console"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"ui": "console", "opened": True, "placement": "split", "pane_id": "w1:p9", "fallback": None, "retried": False})
            age = cmd_ui._iso_age_s(store.read_json(ts.session.console_json).get("launched_at"))
            self.assertIsNotNone(age)
            self.assertLess(abs(age), 5.0)
            self.assertTrue(cmd_ui._launch_in_progress(store.read_json(ts.session.console_json)))

    def test_reopen_after_a_dead_shell_stamps_launched_at(self):
        with TempState() as ts:
            api = FakeApi()
            store.write_json(ts.session.console_json, {"pane_id": "w1:p2", "terminal_id": "term_gone", "pid": 999999, "open": True, "default_team": "alpha"})
            self._labelled_shell(api, [{"pid": 4, "name": "zsh"}])
            api.set_response("plugin.pane.open", fake_plugin_pane_opened("console", console_pane("w1:p9")))
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env))
            self.assertEqual((result["closed"], result["reopened"]), (["w1:p2"], "w1:p9"))
            console = store.read_json(ts.session.console_json)
            self.assertFalse(console["open"])
            self.assertTrue(cmd_ui._launch_in_progress(console))
            # a second reconcile right after the reopen (daemon start following doctor) closes nothing
            api.calls.clear()
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env))
            self.assertEqual(result["closed"], [])
            self.assertNotIn("pane.close", [m for m, _ in api.calls])

    def test_iso_age_parses_plugin_stamps_and_rejects_junk(self):
        self.assertIsNone(cmd_ui._iso_age_s(None))
        self.assertIsNone(cmd_ui._iso_age_s("yesterday"))
        self.assertIsNone(cmd_ui._iso_age_s(12))
        self.assertAlmostEqual(cmd_ui._iso_age_s("1970-01-01T00:00:10.500Z", now=20.0), 9.5, places=3)
        self.assertAlmostEqual(cmd_ui._iso_age_s("1970-01-01T00:00:10Z", now=20.0), 10.0, places=3)


if __name__ == "__main__":
    unittest.main()

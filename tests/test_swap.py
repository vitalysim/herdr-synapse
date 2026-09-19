"""Create a fresh replacement without losing the logical member or racing delivery."""
import copy
import unittest
from unittest import mock

from herdr_team import cmd_hooks, cmd_restore, roster, store, swap, tui_model
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from support import TempState, fake_agent
from test_cmd_roster import live_api, run_cli, json_out, env_no_daemon


class SwapTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.name = "alpha-worker"
        self.ts.members[1].update(cwd=str(self.ts.home), manager=True, model="opus", effort="high",
                                  brief="Review changes", instructions_seq=3, instructions_seq_acked=3,
                                  session={"source": "herdr:claude", "kind": "id", "value": "old-session"})
        self.ts.write_team_json()
        self.source = fake_agent("w2:p2", "term_w1", "claude", self.name, status="blocked", cwd=str(self.ts.home))
        self.api = live_api([self.source, fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer")])
        self.tabs = []
        self.api.set_response("pane.list", lambda p: {"panes": list(self.api.rows)})
        self.api.set_response("tab.list", lambda p: {"tabs": self.tabs})
        self.api.set_response("layout.apply", self.layout)
        self.api.set_response("pane.close", self.close)
        self.api.set_response("pane.get", self.get_pane)
        self.book = roster.Roster(self.ts.layout, "alpha")
        self.start = mock.patch("herdr_team.cmd_roster._start_agent", side_effect=self.launch).start()
        mock.patch("herdr_team.swap.shutil.which", return_value="/bin/true").start()
        mock.patch("herdr_team.roster._kind_verified", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def layout(self, params):
        self.tabs.append({"tab_id": "w2:t9", "label": params["tab_label"]})
        self.api.rows.append(fake_agent("w2:p9", "term_new", None, None, tab_id="w2:t9", cwd=str(self.ts.home)))
        return {"layout": {"root": {"type": "pane", "pane_id": "w2:p9"}}}

    def close(self, params):
        self.api.rows[:] = [r for r in self.api.rows if r["pane_id"] != params["pane_id"]]
        return {"type": "ok"}

    def get_pane(self, params):
        pane = next((r for r in self.api.rows if r["pane_id"] == params["pane_id"]), None)
        if pane is None:
            raise HerdrTeamError("pane_not_found", "gone", 1)
        return {"pane": pane}

    def launch(self, api, name, kind, pane_id, args=()):
        self.assertFalse(any(r["terminal_id"] == "term_w1" for r in api.rows))
        row = next(r for r in api.rows if r["pane_id"] == pane_id)
        row.update(name=name, agent=kind, agent_status="idle")
        return {"terminal_id": row["terminal_id"]}

    def run_swap(self, *extra):
        return json_out(run_cli(["--json", "--team", "alpha", "swap", self.name, *extra], env_no_daemon(self.ts), self.api))

    def test_native_permissions_survive_failed_swap_retry_and_takeover(self):
        self.book.update(lambda team: setattr(team.find(self.name), "permissions", "native"))
        self.start.side_effect = HerdrTeamError("agent_start_failed", "try again", 1)
        code, out, err = self.run_swap("--to", "codex")
        self.assertNotEqual(code, 0)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", self.start.call_args.kwargs["args"])
        self.assertEqual(self.book.load().find(self.name).permissions, "native")
        self.start.side_effect = self.launch
        code, out, err = self.run_swap("--retry")
        self.assertEqual(code, 0, (out, err))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", self.start.call_args.kwargs["args"])
        self.assertEqual(self.book.load().find(self.name).permissions, "native")
        spec = swap.plan(self.book.load(), self.name, "claude", None, env_no_daemon(self.ts))
        self.assertNotIn("--dangerously-skip-permissions", spec["argv"])

    def test_creates_new_agent_for_blocked_source_and_keeps_configuration(self):
        store.write_json(self.ts.team.cursors_dir / (self.name + ".json"), {"seq": 7})
        cursor = (self.ts.team.cursors_dir / (self.name + ".json")).read_bytes()
        before = self.book.load().find(self.name)
        code, out, err = self.run_swap("--to", "codex")
        self.assertEqual(code, 0, (out, err))
        member = self.book.load().find(self.name)
        self.assertEqual((member.kind, member.name, member.role, member.brief, member.manager, member.cwd),
                         ("codex", before.name, before.role, before.brief, True, before.cwd))
        self.assertIsNone(member.model)
        self.assertIsNone(member.effort)
        self.assertIsNone(member.session)
        self.assertEqual(member.generation, before.generation + 1)
        self.assertIsNone(member.instructions_seq_acked)
        self.assertEqual(member.instructions_seq, 3)
        self.assertEqual(member.agent_history[-1]["session"]["value"], "old-session")
        self.assertEqual((self.ts.team.cursors_dir / (self.name + ".json")).read_bytes(), cursor)
        self.assertEqual(len(self.book.load().agents()), 2)
        self.assertTrue(any(r["name"] == "alpha-reviewer" for r in self.api.rows))
        self.assertFalse(any(m in ("pane.send_text", "pane.send_keys", "agent.prompt") for m, p in self.api.calls))
        self.assertNotIn("resume", self.start.call_args.kwargs["args"])
        self.assertEqual(out["swap"]["phase"], "complete")

    def test_dry_run_does_not_mutate_or_launch(self):
        before = self.ts.team.team_json.read_bytes()
        code, out, err = self.run_swap("--to", "codex", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ts.team.team_json.read_bytes(), before)
        self.start.assert_not_called()
        self.assertFalse(self.tabs)
        self.assertTrue(out["fresh"])

    def test_pi_replacement_uses_native_fresh_launch_and_name(self):
        from herdr_team import session_names
        code, out, err = self.run_swap("--to", "pi", "--model", "provider/model@high")
        self.assertEqual(code, 0, (out, err))
        args = self.start.call_args.kwargs["args"]
        self.assertIn("provider/model", args)
        self.assertEqual(args[-2:], ["--name", self.name])
        self.assertNotIn("--resume", args)
        self.assertNotIn("--continue", args)
        self.assertNotIn("old-session", args)
        member = self.book.load().find(self.name)
        naming = session_names.load(self.ts.team)[self.name]
        self.assertEqual((member.kind, naming["kind"], naming["status"]), ("pi", "pi", "launch-option"))
        self.assertEqual(naming["generation"], member.generation)

    def test_mirrored_source_terminal_is_not_closed(self):
        self.api.rows.append(fake_agent("w2:p8", "term_w1", "claude", self.name))
        code, out, err = self.run_swap("--to", "codex")
        self.assertNotEqual(code, 0)
        self.assertEqual(err["code"], "swap_shared_terminal")
        self.assertFalse(any(m == "pane.close" for m, p in self.api.calls))

    def test_missing_binary_and_untrusted_kind_leave_source_untouched(self):
        for target, value in (("herdr_team.swap.shutil.which", None), ("herdr_team.roster._kind_verified", False)):
            with self.subTest(target=target), mock.patch(target, return_value=value):
                code, out, err = self.run_swap("--to", "codex")
                self.assertNotEqual(code, 0)
                self.assertIsNone(self.book.load().find(self.name).swap)
                self.assertFalse(self.tabs)

    def test_recycled_source_pane_is_never_closed(self):
        self.api.rows[0]["terminal_id"] = "unrelated"
        code, out, err = self.run_swap("--to", "codex")
        self.assertNotEqual(code, 0)
        self.assertEqual(err["code"], "member_changed")
        self.assertFalse(any(m == "pane.close" for m, p in self.api.calls))

    def test_failed_start_is_durable_and_retry_reuses_the_reserved_pane(self):
        self.start.side_effect = HerdrTeamError("agent_start_failed", "login needed", EXIT_REFUSED)
        code, out, err = self.run_swap("--to", "codex")
        self.assertEqual(code, 1, err)
        self.assertEqual(out["swap"]["phase"], "failed")
        self.assertEqual(out["swap"]["resume_phase"], "starting")
        self.start.side_effect = self.launch
        code, out, err = self.run_swap("--retry")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(len(self.tabs), 1)
        self.assertEqual(self.book.load().find(self.name).generation, 2)

    def test_retry_adopts_only_its_own_started_instance(self):
        def timed_out(*args, **kwargs):
            self.launch(*args, **kwargs)
            raise HerdrTeamError("herdr_timeout", "lost response", 3)
        self.start.side_effect = timed_out
        self.run_swap("--to", "codex")
        code, out, err = self.run_swap("--retry")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(len(self.tabs), 1)

    def test_layout_timeout_recovers_by_operation_label(self):
        def timeout(params):
            self.layout(params)
            raise HerdrTeamError("herdr_timeout", "lost layout response", 3)
        self.api.set_response("layout.apply", timeout)
        code, out, err = self.run_swap("--to", "codex")
        self.assertNotEqual(code, 0)
        code, out, err = self.run_swap("--retry")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(len(self.tabs), 1)

    def test_unfinished_swap_blocks_identity_mutation_and_restore(self):
        self.start.side_effect = HerdrTeamError("agent_start_failed", "failed", 1)
        self.run_swap("--to", "codex")
        with self.assertRaises(HerdrTeamError) as ctx:
            self.book.set_status(self.name, "left")
        self.assertEqual(ctx.exception.code, "swap_busy")
        items = cmd_restore.plan_restore(self.book.load(), [], [], env_no_daemon(self.ts))
        self.assertEqual(next(i for i in items if i["name"] == self.name)["status"], "skipped")

    def test_stale_controls_cancel_but_mail_and_own_initial_setting_survive(self):
        member = {"swap": {"id": "operation", "phase": "complete", "cutoff_seq": 12}}
        self.assertTrue(swap.stale_job(member, {"kind": "control", "seq": 11}))
        self.assertTrue(swap.stale_job(member, {"kind": "say", "seq": 12}))
        self.assertFalse(swap.stale_job(member, {"kind": "control", "seq": 13}))
        self.assertFalse(swap.stale_job(member, {"kind": "nudge", "seq": 1}))
        member["generation"] = 2
        self.assertTrue(swap.stale_job(member, {"kind": "control", "seq": 13, "target_generation": 1}))
        self.assertFalse(swap.stale_job(member, {"kind": "control", "seq": 13, "target_generation": 2}))
        member["swap"]["phase"] = "briefing"
        self.assertFalse(swap.stale_job(member, {"kind": "control", "seq": 13, "swap_id": "operation"}))

    def test_handoff_in_orientation_and_private_notes_stay_private(self):
        self.ts.team.instructions_dir.mkdir(exist_ok=True)
        self.ts.team.instructions(self.name).write_text("## Mission\nReview the patch\n\n## Notes\nSECRET-OPERATOR-NOTE\n")
        self.run_swap("--to", "codex")
        context = cmd_hooks.brief_context(self.ts.team, "alpha", self.book.load().find(self.name).to_json())
        self.assertIn("freshly created replacement", context)
        self.assertNotIn("SECRET-OPERATOR-NOTE", context)

    def test_cancel_after_source_stopped_preserves_its_exact_resume_reference(self):
        self.start.side_effect = HerdrTeamError("agent_start_failed", "failed", 1)
        self.run_swap("--to", "codex")
        code, out, err = self.run_swap("--cancel")
        self.assertEqual(code, 0, (out, err))
        member = self.book.load().find(self.name)
        self.assertEqual((member.kind, member.status, member.session["value"]), ("claude", "missing", "old-session"))
        self.assertEqual(member.swap["phase"], "cancelled")
        self.assertFalse(any(r["terminal_id"] == "term_new" for r in self.api.rows))

    def test_cancel_layout_failure_resumes_delivery_to_source(self):
        self.api.set_response("layout.apply", lambda p: (_ for _ in ()).throw(HerdrTeamError("failed", "layout failed", 1)))
        self.run_swap("--to", "codex")
        code, out, err = self.run_swap("--cancel")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(self.book.load().find(self.name).status, "active")
        self.assertTrue(any(r["terminal_id"] == "term_w1" for r in self.api.rows))

    def test_cancel_releases_changed_source_for_rebinding_without_closing_it(self):
        self.api.set_response("layout.apply", lambda p: (_ for _ in ()).throw(HerdrTeamError("failed", "layout failed", 1)))
        self.run_swap("--to", "codex")
        self.api.rows[0]["pane_id"] = "w2:p8"
        code, out, err = self.run_swap("--cancel")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(self.book.load().find(self.name).status, "missing")
        self.assertFalse(swap.active(self.book.load().find(self.name)))
        self.assertFalse(any(m == "pane.close" for m, p in self.api.calls))

    def test_switching_back_selects_saved_native_model_without_resuming(self):
        self.run_swap("--to", "codex", "--model", "other-model@medium")
        spec = swap.plan(self.book.load(), self.name, "claude", None, env_no_daemon(self.ts))
        self.assertEqual((spec["model"], spec["effort"]), ("opus", "high"))
        self.assertNotIn("--resume", spec["argv"])

    def test_busy_operation_refuses_second_swap_and_rename_before_api_changes(self):
        self.start.side_effect = HerdrTeamError("agent_start_failed", "failed", 1)
        self.run_swap("--to", "codex")
        count = len(self.api.calls)
        code, out, err = self.run_swap("--to", "opencode")
        self.assertEqual(err["code"], "swap_busy")
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "rename", self.name, "new-worker"], env_no_daemon(self.ts), self.api))
        self.assertEqual(err["code"], "swap_busy")
        self.assertFalse(any(m in ("pane.close", "agent.rename", "layout.apply") for m, p in self.api.calls[count:]))

    def test_destination_replaced_by_unrelated_agent_is_not_adopted_on_retry(self):
        self.start.side_effect = HerdrTeamError("agent_start_failed", "failed", 1)
        self.run_swap("--to", "codex")
        target = next(r for r in self.api.rows if r["terminal_id"] == "term_new")
        target.update(agent="codex", name="unrelated")
        code, out, err = self.run_swap("--retry")
        self.assertEqual(code, 1)
        self.assertEqual(self.book.load().find(self.name).kind, "claude")
        self.assertEqual(target["name"], "unrelated")

    def test_source_moved_to_other_pane_is_not_duplicated(self):
        self.api.rows[0]["pane_id"] = "w2:p3"
        code, out, err = self.run_swap("--to", "codex")
        self.assertNotEqual(code, 0)
        self.assertEqual(err["code"], "member_changed")
        self.assertFalse(self.tabs)

    def test_daemon_restart_checks_swap_before_its_next_cached_roster_refresh(self):
        from test_daemon import make_daemon
        daemon, api, clock = make_daemon(self.ts)
        daemon.scan_teams(force=True)
        team = daemon.teams["alpha"]
        rt = team.rt(self.name)
        rt.restart = {"phase": "exiting", "pane_id": "w2:p2", "kind": "claude", "argv": ["claude", "--resume", "old-session"]}
        self.start.side_effect = HerdrTeamError("agent_start_failed", "failed", 1)
        self.run_swap("--to", "codex")
        with mock.patch("herdr_team.launch.start_agent_async") as restart:
            daemon._advance_restart(team, self.name, rt, daemon.now_ms())
            restart.assert_not_called()
        self.assertNotIn(self.name, team.runtime)

    def test_daemon_drops_old_control_and_waits_for_takeover(self):
        from test_daemon import make_daemon
        from herdr_team.daemon import Pending
        daemon, api, clock = make_daemon(self.ts)
        daemon.scan_teams(force=True)
        team = daemon.teams["alpha"]
        team.pending[self.name] = Pending(kind="control", first_ms=0, lines=["/clear"])
        team.rt(self.name).restart = {"phase": "exiting"}
        self.start.side_effect = HerdrTeamError("agent_start_failed", "failed", 1)
        self.run_swap("--to", "codex")
        daemon._reload_roster(team)
        self.assertNotIn(self.name, team.pending)
        self.assertNotIn(self.name, team.runtime)
        members, _ = daemon._rehydration_members(team)
        self.assertNotIn(self.name, [m.name for m in members])
        self.assertFalse(daemon._assert_roster_terminal(team, team.member(self.name), self.source))


class SwapUiTests(unittest.TestCase):
    def test_picker_selects_kind_and_requires_confirmation_before_creating(self):
        from test_picker_manage import managed, picker_model, ALPHA
        from herdr_team.swap_ui import command
        model = managed(picker_model(ALPHA, focused=None))
        self.assertIsNone(tui_model.picker_apply_key(model, "0"))
        self.assertEqual(model.stage, "swap_kind")
        tui_model.picker_apply_key(model, "DOWN")
        tui_model.picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "swap_model")
        tui_model.picker_apply_key(model, "ENTER")
        self.assertIsNotNone(model.pending_action)
        self.assertIsNone(tui_model.picker_apply_key(model, "ENTER"))
        intent = tui_model.picker_apply_key(model, "y")
        self.assertEqual(command(intent.args), ["--team", "alpha", "swap", "alpha-reviewer", "--to", "codex"])

    def test_picker_retry_is_available_from_saved_operation(self):
        from test_picker_manage import managed, picker_model, ALPHA
        teams = copy.deepcopy(ALPHA)
        teams["alpha"][0]["swap"] = {"id": "operation", "phase": "failed"}
        model = managed(picker_model(teams, focused=None))
        intent = tui_model.picker_apply_key(model, "0")
        self.assertTrue(intent.args["retry"])

    def test_console_command_and_progress_are_registered(self):
        self.assertIn("/swap", tui_model.SLASH_COMMANDS)
        self.assertIn("/swap", tui_model.SLASH_USAGE)
        from herdr_team.swap_ui import result_text
        text = result_text({"member": "reviewer", "swap": {"phase": "failed", "error": "login needed"}, "exit_code": 1})
        self.assertIn("/swap reviewer --retry", text)


if __name__ == "__main__":
    unittest.main()

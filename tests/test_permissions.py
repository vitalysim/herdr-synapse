"""YOLO compatibility, explicit native opt-out, and lifecycle policy freshness."""
import unittest
from unittest import mock

from herdr_team import cmd_restore, cmd_roster, console, models, permissions, roster, store, swap, tui_model
from herdr_team.errors import HerdrTeamError, UsageError
from support import TempState, fake_pane
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
import test_models as rigs


class PermissionTests(unittest.TestCase):
    def test_resolution_and_member_round_trip(self):
        old = dict(name="worker", role="worker", kind="codex", terminal_id=None)
        member = roster.Member.from_json(old)
        self.assertEqual(permissions.effective({}, member), "yolo")
        self.assertNotIn("permissions", member.to_json())
        self.assertEqual(permissions.effective({"permissions": "native"}, member), "native")
        member.permissions = "yolo"
        restored = roster.Member.from_json(member.to_json())
        self.assertEqual(permissions.effective({"permissions": "native"}, restored), "yolo")
        for bad in ("", "invalid", False, {}, []):
            with self.assertRaises(UsageError):
                permissions.effective({"permissions": bad}, old)

    def test_every_launch_builder_obeys_mode_for_every_supported_kind(self):
        for kind in models.KINDS:
            session = dict(source="herdr:" + kind, kind="path" if kind == "pi" else "id",
                           value="/tmp/session.jsonl" if kind == "pi" else "exact-session")
            for mode in permissions.MODES:
                with self.subTest(kind=kind, mode=mode):
                    old = [kind] + list(permissions.YOLO_ARGS[kind]) + ["--dangerously-bypass-hook-trust"]
                    variants = [models.launch_args(kind, None, None, mode),
                                models.fresh_argv(kind, None, None, old, mode),
                                models.resume_argv(kind, session, None, None, mode),
                                models.restart_argv(kind, session, None, None, old, mode)]
                    for argv in variants:
                        self.assertEqual(permissions.YOLO_ARGS[kind][0] in argv, mode == "yolo")
                        if mode == "native":
                            self.assertNotIn("--dangerously-bypass-hook-trust", argv)
        self.assertEqual(models.launch_args("some-future-agent", None, None), [])
        self.assertIn("no known YOLO switch", permissions.view({}, {"kind": "some-future-agent"})["effect"])
        self.assertIn("no built-in tool approval", permissions.view({}, {"kind": "pi"})["effect"])

    def test_every_other_kind_with_a_switch_gets_it_by_default_on_start_resume_and_restore(self):
        """Beyond the four core harnesses, every kind the CLI can spawn with a known switch starts in YOLO."""
        self.assertEqual(set(permissions.YOLO_EVIDENCE), set(permissions.YOLO_ARGS))
        self.assertTrue(set(permissions.YOLO_ARGS) <= set(roster.KIND_LABELS), set(permissions.YOLO_ARGS) - set(roster.KIND_LABELS))
        self.assertEqual({k for k, v in permissions.YOLO_EVIDENCE.items() if v == "live"}, set(models.KINDS))
        self.assertTrue(set(permissions.YOLO_EVIDENCE.values()) <= {"live", "help", "docs"})
        for kind in sorted(set(permissions.YOLO_ARGS) - set(models.KINDS)):
            flags = list(permissions.YOLO_ARGS[kind])
            with self.subTest(kind=kind):
                self.assertEqual(models.launch_args(kind, None, None), flags)
                self.assertEqual(models.launch_args(kind, None, None, "native"), [])
                self.assertEqual(models.fresh_argv(kind, None, None), [kind] + flags)
                view = permissions.view({}, {"kind": kind})
                self.assertEqual((view["mode"], view["flags"], view["evidence"]), ("yolo", flags, permissions.YOLO_EVIDENCE[kind]))
                self.assertIn("not yet live-verified", view["effect"])
                self.assertEqual(permissions.view({"permissions": "native"}, {"kind": kind})["flags"], [])
                for source, (agent, ref_kinds, template) in roster.RESUME_COMMANDS.items():
                    if agent != kind:
                        continue
                    session = dict(source=source, kind=ref_kinds[0], value="/tmp/s.jsonl" if ref_kinds[0] == "path" else "exact-session")
                    argv = models.resume_argv(kind, session, None, None)
                    self.assertEqual(argv[:len(template)], [p.replace("{id}", session["value"]) for p in template])
                    self.assertEqual(argv[len(template):], flags)
                    self.assertEqual(models.resume_argv(kind, session, None, None, "native")[len(template):], [])
        self.assertEqual(permissions.view({}, {"kind": "claude"})["effect"], "permission bypass")

    def test_command_defaults_override_inherit_and_audit(self):
        with TempState() as ts:
            def command(*args):
                return json_out(run_cli(["--json", "--team", "alpha", "permissions", *args], env_no_daemon(ts), live_api()))
            code, out, err = command()
            self.assertEqual((code, out["default"]), (0, "yolo"), err)
            self.assertTrue(all(r["running_mode"] == "unknown" for r in out["members"]))
            code, out, err = command("--default", "native")
            self.assertEqual(code, 0, err)
            self.assertTrue(all(r["mode"] == "native" and r["source"] == "team" for r in out["members"]))
            code, out, err = command(rigs.MEMBER, "yolo")
            self.assertEqual((code, out["members"][0]["mode"], out["members"][0]["source"]), (0, "yolo", "member"), err)
            code, out, err = command(rigs.MEMBER, "inherit")
            self.assertEqual((code, out["members"][0]["mode"]), (0, "native"), err)
            self.assertIsNone(roster.load_team(ts.team).find(rigs.MEMBER).permissions)
            self.assertEqual(len([r for r in store.BoardStore(ts.team).read() if r.get("event") == "permissions_changed"]), 3)
            code, out, err = json_out(run_cli(["--json", "who", "alpha"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertTrue(all(r["permissions"]["mode"] == "native" for r in out["members"] if r["kind"] != "human"))

    def test_agents_including_manager_cannot_change_permissions(self):
        with TempState() as ts:
            rigs.set_member(ts, rigs.MEMBER, manager=True)
            before = ts.team.team_json.read_bytes()
            for args in ([rigs.MEMBER, "yolo"], ["--default", "native"]):
                code, out, err = json_out(run_cli(["--json", "--team", "alpha", "permissions", *args],
                    env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), live_api()))
                self.assertNotEqual(code, 0)
                self.assertEqual(err["code"], "author_mismatch")
            self.assertEqual(ts.team.team_json.read_bytes(), before)

    def test_model_command_keeps_native_policy_in_restart_job(self):
        from test_cmd_board import write_live_daemon
        with TempState() as ts:
            rigs.set_config(ts, permissions="native")
            rigs.set_member(ts, rigs.MEMBER, session=rigs.sess("exact"))
            write_live_daemon(ts)
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "model", rigs.MEMBER,
                "gpt-5.6-luna@high", "--apply", "restart"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(out["control"]["permissions"], "native")
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", out["control"]["argv"])

    def test_policy_cannot_change_while_swap_is_pending(self):
        with TempState() as ts:
            rigs.set_member(ts, rigs.MEMBER, swap={"id": "pending", "phase": "prepared"})
            before = ts.team.team_json.read_bytes()
            for args in ([rigs.MEMBER, "native"], ["--default", "native"]):
                code, out, err = json_out(run_cli(["--json", "--team", "alpha", "permissions", *args], env_no_daemon(ts), live_api()))
                self.assertNotEqual(code, 0)
                self.assertEqual(err["code"], "swap_busy")
            self.assertEqual(ts.team.team_json.read_bytes(), before)

    def test_resume_restore_and_cross_kind_swap_inherit_native(self):
        with TempState() as ts, mock.patch("shutil.which", return_value="/bin/true"):
            rigs.set_config(ts, permissions="native")
            rigs.set_member(ts, rigs.MEMBER, session=rigs.sess("exact"), cwd=str(ts.home))
            rigs.set_member(ts, rigs.PEER, cwd=str(ts.home))
            team = roster.load_team(ts.team)
            resumed = cmd_roster.resume_plan("alpha", team.find(rigs.MEMBER), team.config)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", resumed["argv"])
            planned = cmd_restore.plan_restore(team, [], [], env_no_daemon(ts))
            self.assertEqual([r["permissions"]["mode"] for r in planned], ["native", "native"])
            self.assertNotIn("--dangerously-skip-permissions", planned[1]["argv"])
            spec = swap.plan(team, rigs.MEMBER, "claude", None, env_no_daemon(ts))
            self.assertEqual(spec["permissions"]["mode"], "native")
            self.assertNotIn("--dangerously-skip-permissions", spec["argv"])

    def test_new_team_and_individual_spawn_overrides(self):
        rig = rigs.SpawnTests()
        with TempState(write_team=False) as ts:
            api = rig.api()
            api.set_cli_result(["agent", "start"], rig.STARTED_CODEX, request_id="cli:agent:start")
            with mock.patch("herdr_team.cmd_roster._wait_for_agent", return_value=None):
                code, out, err = json_out(run_cli(["--json", "create", "delta", "--new", "--workspace", "w9",
                    "--spawn", "reviewer:codex", "--spawn", "worker:claude", "--brief", "reviewer=Review patches",
                    "--brief", "worker=Write patches", "--permissions", "native", "--member-permissions", "worker=yolo"], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            starts = [argv for argv in api.runs if argv[:2] == ["agent", "start"]]
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", starts[0])
            self.assertIn("--dangerously-skip-permissions", starts[1])
            team = roster.load_team(ts.session.team("delta"))
            self.assertEqual(team.config["permissions"], "native")
            self.assertEqual(team.find("delta-worker").permissions, "yolo")

    def test_console_parses_permission_command(self):
        for text, args in (("/permissions", []), ("/permissions --default native", ["--default", "native"]),
                           ("/permissions worker inherit", ["worker", "inherit"])):
            intent = tui_model.parse_input_line(text, "alpha")
            self.assertEqual((intent.kind, intent.args["args"]), ("permissions", args))

    def test_console_executes_command_and_displays_saved_policy(self):
        with TempState() as ts:
            state = console.ConsoleState(ts.layout, "alpha", env_no_daemon(ts))
            state.width = 120
            model = console.build_model(ts.layout, "alpha", state, env=state.env)
            def cli(args, env):
                return json_out(run_cli(["--json", *args], env, live_api()))
            with mock.patch("herdr_team.console.run_cli", side_effect=cli):
                console.execute_intent(tui_model.parse_input_line("/permissions --default native", "alpha"), model, state, live_api())
            self.assertEqual(roster.load_team(ts.team).config["permissions"], "native")
            self.assertIn("next launch", "\n".join(model.peek))
            self.assertIn("native", "\n".join(model.peek))

    def test_attaching_live_members_records_policy_without_relaunching(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            code, out, err = json_out(run_cli(["--json", "create", "delta", "--member", "w5:p1:reviewer",
                "--brief", "reviewer=Review patches", "--member-permissions", "reviewer=native"], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            self.assertEqual(roster.load_team(ts.session.team("delta")).find("delta-reviewer").permissions, "native")
            self.assertEqual(out["members"][0]["launch_permissions"]["mode"], "native")
            self.assertFalse(any(argv[:2] == ["agent", "start"] for argv in api.runs))

    def test_old_daemon_must_be_replaced_before_saving_policy(self):
        with TempState() as ts, mock.patch("herdr_team.daemon.read_daemon_info", return_value=mock.Mock(version="0.17.0")), \
                mock.patch("herdr_team.daemon.info_alive", return_value=True), \
                mock.patch("herdr_team.daemon.detach_and_run") as replace, \
                mock.patch("herdr_team.daemon.ensure_daemon", return_value=True):
            before = ts.team.team_json.read_bytes()
            env = env_no_daemon(ts)
            env.pop("HERDR_TEAM_NO_DAEMON", None)
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "permissions", "--default", "native"], env, live_api()))
            self.assertNotEqual(code, 0)
            self.assertEqual(err["code"], "daemon_outdated")
            replace.assert_called_once()
            self.assertEqual(ts.team.team_json.read_bytes(), before)


class RestartPermissionsTests(rigs.ControlRig):
    def setUp(self):
        super().setUp()
        rigs.set_member(self.ts, rigs.MEMBER, session=rigs.sess("exact"))
        self.d.scan_teams(force=True)

    def request(self, mode):
        self.control(rigs.MEMBER, {"action": "restart", "exit": "/quit", "kind": "codex",
            "argv": models.restart_argv("codex", rigs.sess("exact"), None, None, permissions=mode)})

    def test_native_restart_is_validated_and_launched_without_bypass(self):
        rigs.set_config(self.ts, permissions="native")
        self.request("native")
        self.settle()
        rt = self.team.rt(rigs.MEMBER)
        self.assertEqual(rt.restart["permissions"], "native")
        self.api.set_response("pane.get", lambda p: {"pane": fake_pane(p["pane_id"], "term_r1", None)})
        self.d.advance_restarts(self.clock() * 1000)
        starts = [argv for argv in self.api.runs if argv[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", starts[0])

    def test_queued_yolo_restart_rejected_using_current_not_cached_policy(self):
        rigs.set_config(self.ts, permissions="native")
        self.request("yolo")
        self.settle()
        self.assertEqual(self.sent("pane.send_text"), [])
        self.assertIsNone(self.team.rt(rigs.MEMBER).restart)

    def test_policy_changed_after_exit_cancels_stale_launch(self):
        self.request("yolo")
        self.settle()
        self.assertEqual(self.team.rt(rigs.MEMBER).restart["phase"], "exiting")
        rigs.set_config(self.ts, permissions="native")
        self.api.set_response("pane.get", lambda p: {"pane": fake_pane(p["pane_id"], "term_r1", None)})
        self.d.advance_restarts(self.clock() * 1000)
        self.assertFalse(any(argv[:2] == ["agent", "start"] for argv in self.api.runs))
        self.assertIsNone(self.team.rt(rigs.MEMBER).restart)
        self.assertIn("permissions changed", self.records("restart_failed")[-1]["text"])

    def test_policy_changed_while_waiting_for_idle_leaves_running_agent_alone(self):
        self.request("yolo")
        rigs.set_config(self.ts, permissions="native")
        self.settle()
        self.assertEqual(self.sent("pane.send_text"), [])
        self.assertIsNone(self.team.rt(rigs.MEMBER).restart)

    def test_opencode_clear_uses_native_policy_even_from_yolo_process(self):
        rigs.set_member(self.ts, rigs.MEMBER, kind="opencode", permissions="native")
        self.d.scan_teams(force=True)
        self.api.set_response("pane.process_info", {"process_info": {"foreground_processes": [
            {"name": "opencode", "argv": ["opencode", "--auto", "--pure"]}]}})
        self.control(rigs.MEMBER, {"action": "clear"})
        pending = self.team.pending[rigs.MEMBER]
        self.assertEqual(pending.control["permissions"], "native")
        self.assertEqual(pending.control["argv"], ["opencode", "--pure"])

"""Team recovery preserves saved identity/history and never duplicates reserved panes."""
import copy
import unittest
from unittest import mock

from herdr_team import cmd_restore, cmd_roster, paths, picker, roster, store, tui_model
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.restore_ui import RestoreProcess
from support import TempState, fake_agent
from test_cmd_roster import live_api, run_cli, json_out, env_no_daemon


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        for member in self.ts.members:
            member["cwd"] = str(self.ts.home)
        self.ts.members[0].update(manager=True, brief="Review carefully", model="configured-model", effort="high", generation=7)
        self.ts.write_team_json()
        self.api = live_api([])
        self.api.set_response("pane.list", lambda p: {"panes": list(self.api.rows)})
        self.api.set_response("layout.apply", self.layout)
        self.api.set_response("pane.focus", {"type": "ok"})
        self.patch = mock.patch("herdr_team.cmd_restore.shutil.which", return_value="/bin/true")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.start = mock.patch("herdr_team.cmd_roster._start_agent", side_effect=self.launch).start()
        self.addCleanup(mock.patch.stopall)

    def layout(self, params):
        def build(node):
            if node["type"] != "pane":
                return dict(node, first=build(node["first"]), second=build(node["second"]))
            n = len(self.api.rows) + 30
            pane_id = "w9:p{}".format(n)
            self.api.rows.append(fake_agent(pane_id, "term_new{}".format(n), None, None, cwd=str(self.ts.home)))
            return {"type": "pane", "pane_id": pane_id}
        return {"layout": {"root": build(params["root"])}}

    def launch(self, api, name, kind, pane_id, args=()):
        for row in api.rows:
            if row["pane_id"] == pane_id:
                row.update(agent=kind, name=name)
                member = roster.load_team(self.ts.team).find(name)
                row["agent_session"] = member.session
                return {"terminal_id": row["terminal_id"]}
        self.fail("launch has no allocated pane")

    def run_restore(self, *extra):
        return json_out(run_cli(["--json", "restore", "alpha", "--workspace", "w9", *extra], env_no_daemon(self.ts), self.api))

    def test_restores_metadata_and_preserves_unread_cursor_and_retries_without_duplicates(self):
        cursor = self.ts.team.root / "cursors" / "alpha-reviewer.json"
        store.write_json(cursor, {"seq": 3, "terminal_id": "term_r1"})
        before = cursor.read_bytes()
        code, out, err = self.run_restore()
        self.assertEqual(code, 0, err)
        self.assertEqual(out["counts"], {"fresh": 2, "resumed": 0, "skipped": 0, "failed": 0})
        member = roster.load_team(self.ts.team).find("alpha-reviewer")
        self.assertEqual((member.generation, member.manager, member.role, member.brief, member.model, member.effort),
                         (8, True, "reviewer", "Review carefully", "configured-model", "high"))
        self.assertEqual(cursor.read_bytes(), before)
        self.assertEqual(self.start.call_args_list[0].kwargs["args"][:2], ["-m", "configured-model"])
        count = len(self.api.rows)
        code, out, err = self.run_restore()
        self.assertEqual((code, out["counts"]["skipped"], len(self.api.rows)), (0, 2, count), err)
        self.assertEqual(self.start.call_count, 2)

    def test_exact_recorded_session_and_kind_flags(self):
        self.ts.members[0]["session"] = {"source": "herdr:codex", "kind": "id", "value": "session-123", "agent": "codex"}
        self.ts.write_team_json()
        code, out, err = self.run_restore()
        self.assertEqual(code, 0, err)
        self.assertEqual(out["counts"]["resumed"], 1)
        self.assertEqual(self.start.call_args_list[0].kwargs["args"][:2], ["resume", "session-123"])

    def test_dry_run_makes_no_layout_or_roster_changes(self):
        before = self.ts.team.team_json.read_bytes()
        code, out, err = self.run_restore("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.ts.team.team_json.read_bytes(), before)
        self.assertFalse((self.ts.team.root / "restore.lock").exists())
        self.assertFalse(any(method == "layout.apply" for method, _ in self.api.calls))

    def test_recycled_pane_id_is_not_an_identity_match(self):
        self.api.rows.append(fake_agent("w2:p1", "term_unrelated", "claude", "unrelated"))
        code, out, err = self.run_restore()
        self.assertEqual((code, out["counts"]["fresh"]), (0, 2), err)

    def test_launch_pending_or_failed_shell_reservation_prevents_duplicates(self):
        self.api.rows.append(fake_agent("w2:p1", "term_r1", None, None, launch_pending=True))
        code, out, err = self.run_restore()
        self.assertEqual((code, out["counts"]["skipped"], out["counts"]["fresh"]), (0, 1, 1), err)

    def test_removed_members_excluded_and_missing_cwd_is_reported(self):
        self.ts.members[0]["status"] = "left"
        self.ts.members[1]["cwd"] = str(self.ts.tmp / "gone")
        self.ts.write_team_json()
        code, out, _err = self.run_restore()
        self.assertEqual((code, len(out["members"]), out["counts"]["failed"]), (1, 1, 1))
        self.assertEqual(self.api.rows, [])

    def test_busy_restore_refuses_before_allocating(self):
        with store.FileLock(self.ts.team.root / "restore.lock"):
            code, out, err = self.run_restore()
        self.assertNotEqual(code, 0)
        self.assertEqual(err["code"], "restore_busy")
        self.assertEqual(self.api.rows, [])

    def test_failure_keeps_pane_and_other_agents_restore(self):
        def start(api, name, kind, pane_id, args=()):
            if kind == "codex":
                raise HerdrTeamError("agent_start_failed", "login needed", EXIT_REFUSED)
            return self.launch(api, name, kind, pane_id, args)
        self.start.side_effect = start
        code, out, _err = self.run_restore()
        self.assertEqual((code, out["counts"]["failed"], out["counts"]["fresh"]), (1, 1, 1))
        self.assertEqual(len(self.api.rows), 2)
        code, out, _err = self.run_restore()
        self.assertEqual((code, out["counts"]["skipped"], len(self.api.rows)), (0, 2, 2))

    def test_concurrent_removal_is_not_undone(self):
        def start(api, name, kind, pane_id, args=()):
            result = self.launch(api, name, kind, pane_id, args)
            book = roster.Roster(paths.resolve_layout(self.ts.env), "alpha")
            book.set_status(name, "left")
            return result
        self.start.side_effect = start
        code, out, _err = self.run_restore()
        self.assertEqual((code, out["counts"]["failed"]), (1, 2))
        self.assertTrue(all(m.status == "left" for m in roster.load_team(self.ts.team).members if not m.is_human))

    def test_conflicting_name_and_unsupported_session_do_not_start(self):
        self.api.rows.append(fake_agent("w3:p9", "term_other", "codex", "alpha-reviewer"))
        self.ts.members[1]["session"] = {"source": "unknown", "value": "old"}
        self.ts.write_team_json()
        code, out, _err = self.run_restore()
        self.assertEqual((code, out["counts"]["failed"]), (1, 2))
        self.start.assert_not_called()

    def test_team_over_layout_limit_uses_numbered_tabs(self):
        members = []
        for n in range(25):
            member = copy.deepcopy(self.ts.members[1])
            member.update(name="worker-{}".format(n), terminal_id="old-{}".format(n))
            members.append(member)
        self.ts.members = members
        self.ts.write_team_json()
        code, out, err = self.run_restore()
        self.assertEqual((code, out["counts"]["fresh"]), (0, 25), err)
        labels = [p["tab_label"] for method, p in self.api.calls if method == "layout.apply"]
        self.assertEqual(labels, ["team:alpha", "team:alpha:2"])

    def test_notifier_defers_identity_writes_until_restore_releases_lock(self):
        from test_daemon import make_daemon
        daemon, _api, _clock = make_daemon(self.ts)
        daemon.scan_teams(force=True)
        team = daemon.teams["alpha"]
        before = self.ts.team.team_json.read_bytes()
        with store.FileLock(self.ts.team.root / "restore.lock"):
            daemon._apply_changes(team, [("alpha-reviewer", {"status": "missing"})])
        self.assertEqual(self.ts.team.team_json.read_bytes(), before)
        self.assertTrue(daemon.reconcile_due)
        daemon._apply_changes(team, [("alpha-reviewer", {"status": "missing"})])
        self.assertEqual(roster.load_team(self.ts.team).find("alpha-reviewer").status, "missing")

    def test_detection_hook_during_start_cannot_double_increment_generation(self):
        from herdr_team import hooks
        def start(api, name, kind, pane_id, args=()):
            result = self.launch(api, name, kind, pane_id, args)
            doc = store.read_json(self.ts.team.team_json)
            row = next(a for a in api.rows if a["pane_id"] == pane_id)
            hooks._reconcile_detected(self.ts.layout, api, {"alpha": doc}, pane_id, row, None, {"team": None, "changes": []}, lambda text: None)
            return result
        self.start.side_effect = start
        code, out, err = self.run_restore()
        self.assertEqual(code, 0, err)
        self.assertEqual(roster.load_team(self.ts.team).find("alpha-reviewer").generation, 8)

    def test_wrong_resumed_session_is_not_adopted(self):
        saved = {"source": "herdr:codex", "kind": "id", "value": "original", "agent": "codex"}
        self.ts.members[0]["session"] = saved
        self.ts.write_team_json()
        def start(api, name, kind, pane_id, args=()):
            result = self.launch(api, name, kind, pane_id, args)
            if kind == "codex":
                row = next(a for a in api.rows if a["pane_id"] == pane_id)
                row["agent_session"] = dict(saved, value="different")
            return result
        self.start.side_effect = start
        code, out, err = self.run_restore()
        self.assertEqual((code, out["counts"]["failed"], out["counts"]["fresh"]), (1, 1, 1), err)
        self.assertEqual(roster.load_team(self.ts.team).find("alpha-reviewer").session, saved)

    def test_notifier_stale_snapshot_cannot_overwrite_completed_restore(self):
        from test_daemon import make_daemon
        daemon, _api, _clock = make_daemon(self.ts)
        daemon.scan_teams(force=True)
        team = daemon.teams["alpha"]
        code, out, err = self.run_restore()
        self.assertEqual(code, 0, err)
        before = roster.load_team(self.ts.team).find("alpha-reviewer").to_json()
        daemon._apply_changes(team, [("alpha-reviewer", {"terminal_id": "term_r1", "generation": 8, "status": "missing"})])
        self.assertEqual(roster.load_team(self.ts.team).find("alpha-reviewer").to_json(), before)

    def test_opencode_effort_is_queued_before_briefing(self):
        self.ts.members[0].update(kind="opencode", model="provider/model", effort="high")
        self.ts.write_team_json()
        code, out, err = self.run_restore()
        self.assertEqual(code, 0, err)
        records = store.BoardStore(self.ts.team).read()
        controls = [r["control"] for r in records if r.get("control")]
        self.assertEqual(len(controls), 1)
        self.assertEqual((controls[0]["effort"], controls[0]["brief_after"]), ("high", True))

    def test_missing_executable_does_not_allocate(self):
        with mock.patch("herdr_team.cmd_restore.shutil.which", return_value=None):
            code, out, _err = self.run_restore()
        self.assertEqual((code, out["counts"]["failed"]), (1, 2))
        self.assertEqual(self.api.rows, [])

    def test_optional_naming_failure_does_not_fail_started_members(self):
        with mock.patch("herdr_team.session_names.arm", side_effect=HerdrTeamError("lock_timeout", "busy", 1)):
            code, out, err = self.run_restore()
        self.assertEqual((code, out["counts"]["fresh"]), (0, 2), err)
        self.assertTrue(all(m.status == "active" for m in roster.load_team(self.ts.team).members if not m.is_human))
        self.assertEqual(len(list(self.ts.team.jobs_dir.glob("*.json"))), 2)

    def test_close_every_agent_then_restore_again_uses_new_identity(self):
        code, out, err = self.run_restore()
        self.assertEqual(code, 0, err)
        original = roster.load_team(self.ts.team).find("alpha-reviewer")
        self.api.rows.clear()
        # Real terminal identities cannot be recycled even if pane IDs are.
        apply = self.layout
        def layout(params):
            result = apply(params)
            for row in self.api.rows:
                row["terminal_id"] += "-again"
            return result
        self.api.set_response("layout.apply", layout)
        code, out, err = self.run_restore()
        self.assertEqual((code, out["counts"]["fresh"]), (0, 2), err)
        restored = roster.load_team(self.ts.team).find("alpha-reviewer")
        self.assertNotEqual(original.terminal_id, restored.terminal_id)
        self.assertEqual(restored.generation, original.generation + 1)


class RestorePickerTests(unittest.TestCase):
    def test_team_shortcut_and_narrow_help(self):
        model = tui_model.PickerModel(rows=[], focused_workspace="w9")
        model.rosters = {"alpha": []}
        intent = tui_model.picker_apply_key(model, "s")
        self.assertEqual((intent.kind, intent.args), ("team_restore", {"team": "alpha", "workspace": "w9"}))
        lines = tui_model.picker_lines(model, 60, 30)
        self.assertTrue(any("s restore team" in line for line in lines))
        self.assertTrue(all(tui_model.display_width(line) <= 60 for line in lines))

    def test_partial_result_keeps_picker_open_and_checks_terminal_before_focus(self):
        model = tui_model.PickerModel(rows=[])
        api = live_api([])
        result = {"exit_code": 1, "counts": {"failed": 1}, "members": [{"name": "alpha-worker", "status": "failed", "reason": "login needed", "pane_id": "w9:p8", "terminal_id": "original"}]}
        with mock.patch.object(picker, "refresh_rows"):
            self.assertFalse(picker.finish_restore(model, api, None, result))
        self.assertEqual(model.stage, "restore_results")
        self.assertTrue(any("login needed" in line for line in model.restore_results))
        api.rows.append(fake_agent("w9:p8", "replacement", "claude", "other"))
        self.assertFalse(picker.focus_restored_pane(model, api))
        self.assertFalse(any(method == "pane.focus" for method, _ in api.calls))

    def test_all_failure_details_can_be_scrolled_at_narrow_width(self):
        model = tui_model.PickerModel(rows=[], stage="restore_results", restore_team="alpha")
        model.restore_results = ["member-{}: failed — {} end-{}".format(i, "long explanation " * 8, i) for i in range(8)]
        first = tui_model.picker_lines(model, 45, 12)
        self.assertTrue(any("g go to tab" in line for line in first))
        tui_model.picker_apply_key(model, "END")
        last = tui_model.picker_lines(model, 45, 12)
        self.assertIn("end-7", "\n".join(last))
        self.assertTrue(all(tui_model.display_width(line) <= 45 for line in first + last))
        tui_model.picker_apply_key(model, "ESC")
        self.assertEqual(model.stage, "select")

    def test_process_progress_and_result_without_short_timeout(self):
        class Process:
            returncode = None
            def poll(self):
                return self.returncode
        proc = Process()
        with mock.patch("herdr_team.restore_ui.subprocess.Popen", return_value=proc) as spawn:
            worker = RestoreProcess("alpha", "w9", {})
        try:
            self.assertIsNone(worker.result())
            worker.errors.write(b"starting\n1 fresh\n")
            worker.errors.flush()
            self.assertEqual(worker.progress(), "1 fresh")
            worker.output.write(b'{"counts":{"fresh":1},"members":[]}')
            worker.output.flush()
            proc.returncode = 0
            self.assertEqual(worker.result()["counts"]["fresh"], 1)
            self.assertIn("--workspace", spawn.call_args.args[0])
        finally:
            worker.close()

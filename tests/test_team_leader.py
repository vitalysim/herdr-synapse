"""Creation leadership is explicit without migrating existing teams or operator authority."""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from herdr_team import cmd_hooks, cmd_roster, instructions_doc, operator, picker, roster, store, templates, tui_model
from support import FakeError, fake_agent
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_templates import spawning_api
from test_tui_model import picker_model
from support import TempState


class LeaderCreation(unittest.TestCase):
    def argv(self, *extra):
        return ["--json", "create", "beta", "--member", "w5:p1:reviewer", "--brief", "reviewer=Report evidence and blockers."] + list(extra)

    def test_fresh_creation_requires_an_explicit_leader_even_with_reuse(self):
        for extra in ([], ["--reuse"]):
            with TempState(write_team=False) as ts:
                api = live_api()
                code, _, err = json_out(run_cli(self.argv(*extra), env_no_daemon(ts), api))
                self.assertEqual((code, err["code"]), (1, "leader_required"))
                self.assertFalse(ts.session.team("beta").root.exists())
                self.assertFalse(any(m in ("agent.rename", "layout.apply", "pane.rename") for m, _ in api.calls))

    def test_invalid_leader_is_refused_before_creation(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            code, _, err = json_out(run_cli(self.argv("--manager", "missing"), env_no_daemon(ts), api))
            self.assertEqual((code, err["code"]), (1, "member_not_found"))
            self.assertFalse(ts.session.team("beta").root.exists())
            self.assertFalse(any(m == "agent.rename" for m, _ in api.calls))

    def test_alias_and_unique_role_resolve_to_the_saved_member(self):
        for flag, selector in (("--leader", "role:reviewer"), ("--manager", "beta-reviewer")):
            with TempState(write_team=False) as ts:
                code, out, err = json_out(run_cli(self.argv(flag, selector), env_no_daemon(ts), live_api()))
                self.assertEqual(code, 0, err)
                self.assertEqual(out["manager"], "beta-reviewer")
                self.assertTrue(out["members"][0]["manager"])
                team = roster.load_team(ts.session.team("beta"))
                self.assertEqual(team.manager().name, "beta-reviewer")
                self.assertFalse(team.manager().to_json().get("operator"))
                self.assertIn("Report evidence and blockers.", ts.session.team("beta").instructions("beta-reviewer").read_text())

    def test_ordinary_agent_cannot_gain_manager_permissions_by_creating(self):
        with TempState() as ts:
            for extra, expected in (([], "leader_required"), (["--leader", "role:reviewer"], "author_mismatch")):
                code, _, err = json_out(run_cli(self.argv(*extra), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), live_api()))
                self.assertEqual((code, err["code"]), (1, expected))
                self.assertFalse(ts.session.team("beta").root.exists())

    def test_existing_leaderless_reuse_does_not_elect_one(self):
        with TempState() as ts:
            before = roster.load_team(ts.team)
            code, out, err = json_out(run_cli(["--json", "create", "alpha", "--reuse", "--member", "w5:p1:tester:tess", "--brief", "tess=Test."], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertIsNone(out["manager"])
            after = roster.load_team(ts.team)
            self.assertIsNone(after.manager())
            self.assertEqual(after.find("alpha-reviewer").instructions_seq, before.find("alpha-reviewer").instructions_seq)

    def test_existing_manager_is_reported_on_reuse_without_reselection(self):
        with TempState() as ts:
            roster.update_team(ts.team, lambda t: setattr(t.find("alpha-reviewer"), "manager", True))
            code, out, err = json_out(run_cli(["--json", "create", "alpha", "--reuse", "--member", "w5:p1:tester:tess", "--brief", "tess=Test."], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(out["manager"], "alpha-reviewer")
            self.assertFalse(out["members"][0]["manager"])

    def test_template_leader_must_be_in_the_actual_planned_batch(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            code, _, err = json_out(run_cli(["--json", "create", "beta", "--template", "vuln-hunt", "--member", "w5:p1:hunter"], env_no_daemon(ts), api))
            self.assertEqual((code, err["code"]), (1, "member_not_found"))
            self.assertFalse(ts.session.team("beta").root.exists())

    def test_selected_spawn_is_designated_before_agent_start(self):
        with TempState(write_team=False) as ts:
            api = spawning_api(["codex", "claude"])
            original = api.run
            observed = []
            def run(argv, *args, **kwargs):
                if argv[:2] == ["agent", "start"]:
                    team = roster.load_team(ts.session.team("beta"))
                    observed.append(team.manager().name if team.manager() else None)
                return original(argv, *args, **kwargs)
            api.run = run
            code, out, err = json_out(run_cli(["--json", "create", "beta", "--new", "--spawn", "reviewer:codex", "--spawn", "lead:claude", "--brief", "reviewer=Review.", "--brief", "lead=Report only.", "--manager", "role:lead"], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            self.assertEqual(observed, ["beta-lead", "beta-lead"])
            self.assertEqual(out["manager"], "beta-lead")

    def test_every_creation_briefing_sees_the_leader_and_canonical_mission(self):
        with TempState(write_team=False) as ts:
            observed = []
            original = roster.write_briefing_job
            def queue(paths, name, **kwargs):
                team = roster.load_team(paths)
                observed.append((name, team.manager().name, paths.instructions(team.manager().name).read_text()))
                return original(paths, name, **kwargs)
            with mock.patch.object(roster, "write_briefing_job", side_effect=queue):
                code, _, err = json_out(run_cli(self.argv("--member", "wA:p6:lead:bob", "--brief", "bob=Report only.", "--leader", "bob"), env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(len(observed), 2)
            self.assertTrue(all(leader == "bob" and "Report only." in text for _, leader, text in observed))

    def test_exact_name_wins_and_role_ambiguity_refuses_before_writes(self):
        for selector, expected in (("role:reviewer", 1), ("beta-reviewer", 0)):
            with TempState(write_team=False) as ts:
                code, _, err = json_out(run_cli(self.argv("--member", "wA:p6:reviewer:bob", "--brief", "bob=Review.", "--leader", selector), env_no_daemon(ts), live_api()))
                self.assertEqual(code, expected, err)
                if expected:
                    self.assertEqual(err["code"], "leader_ambiguous")
                    self.assertFalse(ts.session.team("beta").root.exists())

    def test_conflicting_aliases_are_a_usage_error(self):
        with TempState(write_team=False) as ts:
            code, _, err = json_out(run_cli(self.argv("--leader", "beta-reviewer", "--manager", "beta-reviewer"), env_no_daemon(ts), live_api()))
            self.assertEqual((code, err["code"]), (2, "usage"))
            self.assertFalse(ts.session.team("beta").root.exists())

    def test_template_brief_override_changes_only_its_mission(self):
        with TempState(write_team=False) as ts:
            code, _, err = json_out(run_cli(["--json", "create", "beta", "--template", "vuln-hunt", "--member", "w5:p1:lead", "--brief", "beta-lead=Report evidence, do not assign work."], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            paths = ts.session.team("beta")
            member = roster.load_team(paths).manager()
            sections = instructions_doc.parse(paths.instructions(member.name).read_text())
            self.assertEqual(instructions_doc.section(sections, "Mission"), ["Report evidence, do not assign work."])
            self.assertTrue(instructions_doc.section(sections, "Scope"))
            original = next(r.document for r in templates.load("vuln-hunt", ts.config_dir).roles if r.name == "lead")
            original_sections = instructions_doc.parse(original)
            for title, body in original_sections:
                if title != "Mission":
                    self.assertEqual(instructions_doc.section(sections, title), body, title)
            context = cmd_hooks.brief_context(paths, "beta", member.to_json())
            self.assertIn("Report evidence, do not assign work.", context)
            self.assertNotIn("split and sequence", context)

    def test_exact_spawn_name_preserves_a_future_live_members_name(self):
        with TempState(write_team=False) as ts:
            api = spawning_api(["claude"])
            code, out, err = json_out(run_cli(["--json", "create", "beta", "--new", "--spawn", "dev:claude", "--spawn-name", "dev=beta-dev-2", "--brief", "dev=Report.", "--leader", "role:dev"], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            self.assertEqual(out["manager"], "beta-dev-2")
            code, out, err = json_out(run_cli(["--json", "create", "beta", "--reuse", "--member", "w5:p1:worker:beta-dev", "--brief", "beta-dev=Build."], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            self.assertEqual(out["manager"], "beta-dev-2")
            self.assertFalse(out["members"][0]["manager"])

    def test_failed_selected_spawn_keeps_its_designation_without_fallback(self):
        with TempState(write_team=False) as ts:
            api = spawning_api(["codex", "claude"])
            def settle(api, pane_id, kind, timeout):
                if kind == "claude":
                    return None
                return roster.resolve_target(api, pane_id)
            with mock.patch.object(cmd_roster, "_wait_for_agent", side_effect=settle):
                code, out, err = run_cli(["--json", "create", "beta", "--new", "--spawn", "worker:codex", "--spawn", "lead:claude", "--brief", "worker=Work.", "--brief", "lead=Report.", "--leader", "role:lead"], env_no_daemon(ts), api)
            self.assertEqual(code, 0, err)
            out = json.loads(out)
            self.assertEqual((out["manager"], out["failed"]), ("beta-lead", ["beta-lead"]))
            self.assertIn("no replacement is elected", err)
            self.assertEqual(roster.load_team(ts.session.team("beta")).manager().status, "failed")

    def test_existence_race_cannot_turn_reuse_into_leaderless_creation(self):
        with TempState() as ts:
            with mock.patch.object(store, "read_json", return_value=None):
                with self.assertRaises(Exception) as caught:
                    roster.create_team(ts.layout, "alpha", reuse=True, expected_exists=True)
            self.assertEqual(caught.exception.code, "team_changed")
            self.assertIsNotNone(store.read_json(ts.team.team_json))

    def test_all_fresh_authority_refusals_leave_no_target_directory(self):
        for extra in (["--charter", "Work."], ["--rules", "No writes."], ["--template", "vuln-hunt"], ["--canvas"], ["--permissions", "native"]):
            with TempState() as ts:
                code, _, err = json_out(run_cli(self.argv(*extra), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), live_api()))
                self.assertEqual((code, err["code"]), (1, "author_mismatch"))
                self.assertFalse(ts.session.team("beta").root.exists())

    def test_delegated_invalid_selection_does_not_materialize_audit_directory(self):
        with TempState() as ts:
            operator.grant(ts.session, "alpha", "alpha-reviewer", ttl_s=0)
            code, _, err = json_out(run_cli(self.argv("--instructions", "reviewer=Report.", "--leader", "missing"), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), live_api()))
            self.assertEqual((code, err["code"]), (1, "member_not_found"))
            self.assertFalse(ts.session.team("beta").root.exists())

    def test_template_reuse_does_not_reselect_an_existing_manager(self):
        with TempState() as ts:
            roster.update_team(ts.team, lambda t: setattr(t.find("alpha-reviewer"), "manager", True))
            code, out, err = json_out(run_cli(["--json", "create", "alpha", "--reuse", "--template", "vuln-hunt", "--member", "w5:p1:hunter"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(out["manager"], "alpha-reviewer")

    def test_recycled_picker_pane_is_refused_by_cli_before_creation(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            spec = {"team": "beta", "members": [{"target": "w5:p1", "terminal_id": "replaced-terminal", "role": "reviewer", "name": "beta-reviewer", "kind": "codex", "brief": "Report.", "leader": True}]}
            code, _, err = json_out(run_cli(["--json"] + picker.create_args(spec), env_no_daemon(ts), api))
            self.assertEqual((code, err["code"]), (1, "pane_mismatch"))
            self.assertFalse(ts.session.team("beta").root.exists())
            self.assertFalse(any(m == "agent.rename" for m, _ in api.calls))

    def test_fresh_partial_live_failure_reports_an_unappointed_selected_leader(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            original = api.responses["agent.rename"]
            def rename(params):
                if params["target"] == "wA:p6":
                    raise FakeError("agent_name_taken", "leader rename failed")
                return original(params)
            api.set_response("agent.rename", rename)
            code, _, err = json_out(run_cli(self.argv("--member", "wA:p6:lead:bob", "--brief", "bob=Report.", "--leader", "bob"), env_no_daemon(ts), api))
            self.assertEqual(code, 1, err)
            self.assertEqual(err["team_created_with"], ["beta-reviewer"])
            self.assertEqual((err["selected_leader"], err["manager"], err["leader_status"]), ("bob", None, "unappointed"))
            self.assertIn("No replacement was elected", err["recovery"])

    def test_reusing_a_left_name_can_designate_the_new_member(self):
        with TempState() as ts:
            def tombstone(t):
                old = t.find("alpha-worker")
                old.name, old.status = "tess", "left"
            roster.update_team(ts.team, tombstone)
            code, out, err = json_out(run_cli(["--json", "create", "alpha", "--reuse", "--member", "w5:p1:lead:tess", "--brief", "tess=Report.", "--leader", "tess"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(out["manager"], "tess")
            self.assertEqual(roster.load_team(ts.team).manager().status, "active")

    def test_a_retired_alias_does_not_appoint_the_wrong_member_before_failure(self):
        with TempState() as ts:
            def prepare(t):
                t.find("alpha-reviewer").previous_names = [{"name": "tess", "retired_at": roster.now_iso()}]
                t.find("alpha-worker").manager = True
            roster.update_team(ts.team, prepare)
            api = live_api()
            def fail(params):
                raise FakeError("agent_name_taken", "new leader could not join")
            api.set_response("agent.rename", fail)
            code, _, err = json_out(run_cli(["--json", "create", "alpha", "--reuse", "--member", "w5:p1:lead:tess", "--brief", "tess=Report.", "--leader", "tess"], env_no_daemon(ts), api))
            self.assertEqual(code, 1, err)
            self.assertEqual(roster.load_team(ts.team).manager().name, "alpha-worker")


class LeaderPicker(unittest.TestCase):
    def test_folder_stage_requires_leader_choice_before_member_editing(self):
        model = picker_model(focused=None)
        row = model.rows[0]
        row.selected = True
        model.team_name = "beta"
        model.stage = "project"
        tui_model.picker_apply_key(model, "TAB")
        self.assertEqual(model.stage, "leader")
        self.assertIn("leader", "\n".join(tui_model.picker_lines(model, 100, 20)))
        tui_model.picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "members")
        self.assertTrue(row.brief)
        row.role, row.member_name = "renamed-role", "beta-renamed"
        self.assertTrue(tui_model.create_spec(model)["members"][0]["leader"])

    def spec(self, spawned_leader):
        return {"team": "mix", "mode": "create", "charter": "Ship.", "members": [
            {"target": "w1:p1", "terminal_id": "term-live", "name": "mix-dev", "role": "worker", "kind": "codex", "brief": "Build.", "spawn": False, "leader": not spawned_leader},
            {"target": "new-1", "name": "mix-dev-2", "role": "dev", "kind": "claude", "brief": "Report.", "spawn": True, "leader": spawned_leader},
        ]}

    def test_mixed_creation_starts_the_leader_batch_first_with_exact_names(self):
        for spawned_leader in (False, True):
            calls = []
            def invoke(argv, env):
                calls.append(argv)
                return 0, {"members": [{"name": "actual-created"}], "manager": "chosen"}, None
            with mock.patch("herdr_team.console.run_cli", invoke), mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(picker.execute_create(self.spec(spawned_leader), {}), 0)
            self.assertEqual("--new" in calls[0], spawned_leader)
            self.assertIn("--leader", calls[0])
            self.assertIn("--reuse", calls[1])
            self.assertNotIn("--leader", calls[1])
            spawned = calls[0] if spawned_leader else calls[1]
            self.assertIn("dev=mix-dev-2", spawned)

    def test_second_phase_failure_reports_actual_created_names(self):
        results = [(0, {"members": [{"name": "actual-leader"}], "manager": "actual-leader"}, None), (1, None, {"code": "failed"})]
        with mock.patch("herdr_team.console.run_cli", side_effect=results), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(picker.execute_create(self.spec(True), {}), 1)
        payload = json.loads(err.getvalue())
        self.assertEqual(payload["team_created_with"], ["actual-leader"])
        self.assertEqual(payload["manager"], "actual-leader")

    def test_second_phase_partial_join_reports_every_retained_member(self):
        with TempState(write_team=False) as ts:
            api = spawning_api(["claude"])
            original = api.responses["agent.rename"]
            def rename(params):
                if params["target"] == "wA:p6":
                    raise FakeError("agent_name_taken", "last rename failed")
                return original(params)
            api.set_response("agent.rename", rename)
            spec = self.spec(True)
            spec["members"][0].update(target="w5:p1", terminal_id="term_51")
            spec["members"].append({"target": "wA:p6", "terminal_id": "term_owner", "name": "mix-third", "role": "third", "kind": "claude", "brief": "Test.", "spawn": False, "leader": False})
            def invoke(argv, env):
                return json_out(run_cli(["--json"] + argv, env_no_daemon(ts), api))
            with mock.patch("herdr_team.console.run_cli", invoke), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(picker.execute_create(spec, {}), 1)
            payload = json.loads(err.getvalue())
            actual = roster.load_team(ts.session.team("mix"))
            self.assertEqual(payload["team_created_with"], [m.name for m in actual.members if m.kind != "human" and m.status != "left"])
            self.assertEqual(payload["team_created_with"], ["mix-dev-2", "mix-dev"])
            self.assertEqual(payload["manager"], actual.manager().name)

    def test_solo_creation_refuses_replacement_during_an_earlier_step(self):
        from herdr_team import picker_whiteboard

        with TempState(write_team=False) as ts:
            api = live_api([fake_agent("w5:p1", "term-selected", "codex", None)])
            model = picker.build_model(api, {}, ts.layout)
            model.watch_layer = False
            model.wb_target = {"pane_id": "w5:p1", "terminal_id": "term-selected", "agent_kind": "codex", "label": "codex"}
            model.solo_team, model.solo_mission = "beta", "Report only."
            intent = picker_whiteboard.solo_intent(model)
            calls = []
            def invoke(argv, env, **kwargs):
                calls.append(argv)
                if argv == ["whiteboard", "enable"]:
                    api.rows[0]["terminal_id"] = "term-replacement"
                    return 0, {}, None
                return json_out(run_cli(["--json"] + argv, env_no_daemon(ts), api))
            with mock.patch("herdr_team.console.run_cli", invoke), mock.patch.object(picker, "refresh_rows"):
                self.assertTrue(picker.execute_whiteboard(intent, model, api, ts.layout, {}))
            self.assertEqual(len(calls), 2)
            self.assertIsNotNone(model.error)
            self.assertFalse(ts.session.team("beta").root.exists())
            self.assertFalse(any(method == "agent.rename" for method, _ in api.calls))

    def test_refresh_and_edit_keep_terminal_identity_but_removal_requires_reselection(self):
        model = picker_model(focused=None)
        row = model.rows[0]
        row.selected, row.brief = True, "Custom reporting only."
        model.team_name, model.stage = "beta", "project"
        tui_model.picker_apply_key(model, "TAB")
        tui_model.picker_apply_key(model, "ENTER")
        self.assertEqual(row.brief, "Custom reporting only.")
        row.pane_id, row.member_name, row.role = "w9:p7", "new-name", "changed"
        self.assertTrue(tui_model.create_spec(model)["members"][0]["leader"])
        row.terminal_id = "replacement-terminal"
        model.stage = "confirm"
        self.assertIsNone(tui_model.picker_apply_key(model, "ENTER"))
        self.assertEqual(model.stage, "leader")

    def test_real_refresh_preserves_settings_only_for_the_same_terminal(self):
        api = live_api([fake_agent("w5:p1", "term-original", "codex", None)])
        model = picker.build_model(api, {})
        row = model.rows[0]
        row.selected, row.role, row.member_name, row.brief, row.setting = True, "lead", "beta-lead", "Report only.", "@high"
        model.leader_key = tui_model.leader_row_key(row)
        api.rows[0]["pane_id"] = "w7:p3"
        picker.refresh_rows(model, api, None)
        self.assertEqual((model.rows[0].setting, model.rows[0].brief), ("@high", "Report only."))
        self.assertTrue(model.rows[0].selected)
        api.rows[0]["terminal_id"] = "term-replacement"
        picker.refresh_rows(model, api, None)
        self.assertFalse(model.rows[0].selected)
        self.assertEqual((model.rows[0].setting, model.rows[0].brief), ("", ""))

    def test_reselection_keeps_a_visible_highlight_in_a_small_window(self):
        model = picker_model(focused=None)
        for row in model.rows[:2]:
            row.selected = True
        model.stage, model.leader_key, model.leader_index = "confirm", "removed", 99
        tui_model.picker_apply_key(model, "ENTER")
        self.assertLess(model.leader_index, len(tui_model.selected_rows(model)))
        lines = tui_model.picker_lines(model, 35, 8)
        self.assertTrue(any(line.startswith(">") for line in lines), lines)
        self.assertEqual(model.stage, "leader")

    def test_unknown_live_identity_is_refused_before_any_phase(self):
        spec = self.spec(True)
        spec["members"][0].pop("terminal_id")
        with mock.patch("herdr_team.console.run_cli") as run, mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(picker.execute_create(spec, {}), 1)
        run.assert_not_called()
        self.assertEqual(json.loads(err.getvalue())["code"], "pane_mismatch")

    def test_add_mode_does_not_advertise_a_stale_creation_leader(self):
        model = picker_model(focused=None)
        row = model.rows[0]
        row.selected = True
        model.leader_key = tui_model.leader_row_key(row)
        model.stage, model.mode, model.member_field = "members", "add", "brief"
        lines = "\n".join(tui_model.picker_lines(model, 100, 20))
        self.assertNotIn("Leader responsibilities", lines)
        self.assertFalse(tui_model.create_spec(model)["members"][0]["leader"])

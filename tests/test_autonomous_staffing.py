"""Isolated characterization of additive staffing; no real agents or inference."""
from __future__ import annotations

import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from support import TempState, fake_agent
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli, write_live_daemon
from test_templates import spawning_api
from test_models import ControlRig
from support import fake_pane
from herdr_team import charter, cli, cmd_hooks, cmd_staff, identity, model_checks, model_inventory as I, roster, staffing as S, store, work
from herdr_team.errors import HerdrTeamError


def inventory(kind="codex", provider="openai", model="model-a", context="ctx"):
    return {"kind": kind, "profile": None, "installed": True, "version": "0.162.1", "executable": "/fake/" + kind,
            "context": context, "route_context": "route-ctx", "account_context": "opaque-account", "source": "fixture",
            "models": [{"id": model, "provider": provider, "efforts": ["low", "high"]}],
            "providers": [{"id": provider, "authentication": "credentials_reported"}], "quota": "unknown", "inference": "unverified"}


def enroll(ts, maximum=4):
    def change(team):
        team.find("alpha-reviewer").manager = True
        team.config["staffing"] = {"version": 1, "enabled": True, "paused": False, "max_workers": maximum,
                                    "approved": {"codex:openai": {"context": "ctx", "account_context": "opaque-account"}},
                                    "cwd": str(ts.home), "task": "Task", "acceptance": "Evidence", "attempts": {}}
    return roster.update_team(ts.team, change)


def manager():
    return identity.Author("alpha-reviewer", "codex", "cli", True, terminal_id="term_r1", team="alpha", generation=1)


def human():
    return identity.Author("human", "human", "outside", False)


CHOSEN = {"kind": "codex", "model": "model-a", "effort": None, "profile": None, "route": "codex:openai", "context": "ctx", "route_context": "route-ctx"}


class Reservations(unittest.TestCase):
    def reserve(self, ts, request="r1", name="alpha-dev", author=None):
        return S.reserve(ts.team, author or manager(), request, name, "dev", CHOSEN, "Implement", "Tests", None)

    def test_four_atomic_slots_exclude_manager_and_manual_members(self):
        with TempState() as ts:
            enroll(ts)
            for n in range(4):
                self.reserve(ts, "r" + str(n), "alpha-dev" + str(n))
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 4)
            with self.assertRaisesRegex(HerdrTeamError, "capacity is full"):
                self.reserve(ts, "r5", "alpha-dev5")

    def test_concurrent_reservations_cannot_oversubscribe(self):
        with TempState() as ts:
            enroll(ts)
            successes, failures = [], []
            barrier = threading.Barrier(8)
            def reserve(n):
                barrier.wait()
                try:
                    successes.append(self.reserve(ts, "r" + str(n), "alpha-dev" + str(n)))
                except HerdrTeamError as err:
                    failures.append(err.code)
            threads = [threading.Thread(target=reserve, args=(n,)) for n in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual((len(successes), failures.count("staffing_capacity")), (4, 4))

    def test_duplicate_id_is_idempotent_but_payload_change_is_not(self):
        with TempState() as ts:
            enroll(ts)
            self.reserve(ts)
            self.assertTrue(self.reserve(ts)["reused"])
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)
            with self.assertRaises(HerdrTeamError) as caught:
                self.reserve(ts, name="alpha-other")
            self.assertEqual(caught.exception.code, "staffing_request_conflict")

    def test_ambiguous_start_and_retiring_worker_retain_capacity(self):
        with TempState() as ts:
            enroll(ts)
            self.reserve(ts)
            for phase in ("laying_out", "starting", "ambiguous", "active", "retiring"):
                S.phase(ts.team, "r1", phase)
                self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)
            S.phase(ts.team, "r1", "retired")
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 0)

    def test_worker_and_stale_manager_do_not_gain_operator_authority(self):
        with TempState() as ts:
            team = enroll(ts)
            for field, value in (("name", "alpha-worker"), ("terminal_id", "other"), ("generation", 2), ("team", "other"), ("verified", False)):
                caller = manager()
                setattr(caller, field, value)
                with self.assertRaises(HerdrTeamError):
                    S.require_manager(team, caller)
            caller = manager()
            caller.operator = True
            with self.assertRaises(HerdrTeamError):
                S.operator(caller)
            self.assertFalse(manager().operator)

    def test_pause_and_existing_name_refuse_before_start(self):
        with TempState() as ts:
            enroll(ts)
            with self.assertRaises(HerdrTeamError) as caught:
                self.reserve(ts, name="alpha-worker")
            self.assertEqual(caught.exception.code, "name_taken")
            roster.update_team(ts.team, lambda t: t.config["staffing"].update(paused=True))
            with self.assertRaises(HerdrTeamError) as caught:
                self.reserve(ts)
            self.assertEqual(caught.exception.code, "staffing_paused")

    def test_manual_roster_serialization_is_unchanged_and_new_field_survives(self):
        with TempState() as ts:
            member = roster.load_team(ts.team).find("alpha-worker")
            self.assertNotIn("staffing", member.to_json())
            member.staffing = {"request_id": "r1"}
            self.assertEqual(roster.Member.from_json(member.to_json()).staffing, member.staffing)

    def test_manual_member_launch_is_not_limited_by_pool_or_pause(self):
        with TempState() as ts:
            team = enroll(ts)
            team.config["staffing"].update(paused=True, approved={})
            S.launch_guard(team, team.find("alpha-worker"), starting=True)
            S.control_guard(team.to_json(), team.find("alpha-worker").to_json(), {}, {}, starting=True)

    def test_owned_launch_and_queued_model_controls_are_fenced(self):
        with TempState() as ts:
            enroll(ts)
            self.reserve(ts)
            def owned(t):
                t.find("alpha-worker").staffing = {"request_id": "r1"}
            team = roster.update_team(ts.team, owned)
            member = team.find("alpha-worker").to_json()
            good = {"from": manager().name, "from_terminal": "term_r1", "from_gen": 1}
            S.control_guard(team.to_json(), member, good, CHOSEN)
            for record in (dict(good, from_gen=2), dict(good, from_terminal="reused"), dict(good, **{"from": "alpha-worker"})):
                with self.assertRaises(HerdrTeamError):
                    S.control_guard(team.to_json(), member, record, CHOSEN)
            with self.assertRaises(HerdrTeamError):
                S.control_guard(team.to_json(), member, good, dict(CHOSEN, model="other"))
            # Trusted-human origin is validated by the existing notifier gate, not removed here.
            S.control_guard(team.to_json(), member, {"from": "human"}, {"model": "anything"}, starting=True)

    def test_pool_change_between_planning_and_reservation_refuses(self):
        with TempState() as ts:
            enroll(ts)
            roster.update_team(ts.team, lambda t: t.config["staffing"].update(approved={}))
            with self.assertRaises(HerdrTeamError) as caught:
                self.reserve(ts)
            self.assertEqual(caught.exception.code, "provider_context_changed")
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 0)

    def test_launch_submission_rechecks_native_context_without_discovery(self):
        with TempState() as ts:
            enroll(ts)
            self.reserve(ts)
            team = roster.update_team(ts.team, lambda t: setattr(t.find("alpha-worker"), "staffing", {"request_id": "r1"}))
            with mock.patch.object(I, "fingerprint", return_value="ctx"), mock.patch.object(I, "discover") as discovery:
                S.launch_context_guard(team, team.find("alpha-worker"), ts.env)
                discovery.assert_not_called()
            with mock.patch.object(I, "fingerprint", return_value="changed"):
                with self.assertRaises(HerdrTeamError) as caught:
                    S.launch_context_guard(team, team.find("alpha-worker"), ts.env)
                self.assertEqual(caught.exception.code, "provider_context_changed")

    def test_catalog_refresh_does_not_turn_same_request_into_another_launch(self):
        with TempState() as ts:
            enroll(ts)
            self.reserve(ts)
            refreshed = dict(CHOSEN, route_context="new-catalog", native_version="0.162.1")
            repeated = S.reserve(ts.team, manager(), "r1", "alpha-dev", "dev", refreshed, "Implement", "Tests", None)
            self.assertTrue(repeated["reused"])
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)

    def test_queued_control_from_missing_manager_no_longer_has_staffing_authority(self):
        with TempState() as ts:
            enroll(ts)
            self.reserve(ts)
            def change(team):
                team.find("alpha-worker").staffing = {"request_id": "r1"}
                team.manager().status = "missing"
            team = roster.update_team(ts.team, change)
            record = {"from": manager().name, "from_terminal": "term_r1", "from_gen": 1}
            with self.assertRaises(HerdrTeamError) as caught:
                S.control_guard(team.to_json(), team.find("alpha-worker").to_json(), record, CHOSEN)
            self.assertEqual(caught.exception.code, "staffing_authority")


class Fallbacks(unittest.TestCase):
    def test_one_fallback_per_failed_attempt_retains_reason_and_quota_refuses(self):
        with TempState() as ts:
            enroll(ts)
            S.reserve(ts.team, manager(), "prior", "alpha-dev", "dev", CHOSEN, "Mission", "Tests", "W-1")
            team = roster.update_team(ts.team, lambda t: setattr(t.find("alpha-worker"), "staffing", {"request_id": "prior"}))
            failed = SimpleNamespace(owner="alpha-worker", outcome="failed", gen=1, n=1, summary="Model unavailable")
            item = SimpleNamespace(id="W-1", attempts=[failed])
            args = SimpleNamespace(fallback_for="alpha-worker", reason="Choose another approved model", work_id="W-1", request_id="retry")
            with mock.patch.object(work, "load", return_value={"W-1": item}):
                fallback = cmd_staff._fallback(ts.team, team, args)
                self.assertEqual(fallback, {"attempt_key": "W-1:1", "previous": "alpha-worker", "reason": args.reason})
                S.reserve(ts.team, manager(), "retry", "alpha-retry", "retry", CHOSEN, "Retry", "Tests", "W-1")
                S.phase(ts.team, "retry", "ambiguous", fallback=fallback)
                team = roster.load_team(ts.team)
                self.assertEqual(cmd_staff._fallback(ts.team, team, args), fallback)
                args.request_id = "another-retry"
                with self.assertRaises(HerdrTeamError) as caught:
                    cmd_staff._fallback(ts.team, team, args)
                self.assertEqual(caught.exception.code, "fallback_exhausted")
                # Changing the request's wording must not conceal quota in the original failure.
                team.config["staffing"]["attempts"].pop("retry")
                failed.summary = "429 rate_limit exceeded"
                with self.assertRaises(HerdrTeamError) as caught:
                    cmd_staff._fallback(ts.team, team, args)
                self.assertEqual(caught.exception.code, "fallback_quota")


class NotifierFences(ControlRig):
    def owned_worker(self, phase="active"):
        enroll(self.ts)
        S.reserve(self.ts.team, manager(), "owned", "alpha-dev", "dev", CHOSEN, "Mission", "Tests", None)
        def change(team):
            team.find("alpha-worker").staffing = {"request_id": "owned"}
            team.config["staffing"]["attempts"]["owned"].update(phase=phase, name="alpha-worker", terminal_id="term_w1", generation=1)
        roster.update_team(self.ts.team, change)
        self.d.scan_teams(force=True)

    def test_retire_job_rechecks_generation_before_typing_and_does_not_release_capacity(self):
        self.owned_worker("retiring")
        seq = self.control("alpha-worker", {"action": "retire", "request_id": "owned", "terminal_id": "term_w1", "generation": 1, "exit": "/exit"})
        self.assertIn("alpha-worker", self.team.pending)
        pending = self.team.pending["alpha-worker"]
        roster.update_team(self.ts.team, lambda t: setattr(t.find("alpha-worker"), "generation", 2))
        self.d._send_control(self.team, self.team.member("alpha-worker"), pending, None, self.clock() * 1000)
        self.assertEqual(self.sent("pane.send_text"), [])
        self.assertEqual(S.capacity(roster.load_team(self.ts.team)), 1)
        self.assertTrue(any("retire refused" in r.get("text", "") for r in self.records("typed")))

    def test_delayed_restart_refuses_native_context_changed_after_exit(self):
        self.owned_worker()
        row = self.team.member("alpha-worker")
        record = {"from": manager().name, "from_kind": "codex", "from_terminal": "term_r1", "from_gen": 1,
                  "origin": {"via": "cli", "verified": True}, "kind": "direct", "to": [row["name"]]}
        seq = store.BoardStore(self.ts.team).append(record)
        rt = self.team.rt(row["name"])
        rt.restart = dict(CHOSEN, phase="exiting", since_ms=self.clock() * 1000, pane_id=row["pane_id"],
                          argv=["codex"], requested_by=manager().name, source_seq=seq, permissions="yolo")
        self.api.set_response("pane.get", {"pane": fake_pane(row["pane_id"], row["terminal_id"], None)})
        with mock.patch.object(I, "fingerprint", return_value="changed"), mock.patch.object(I, "discover") as discovery:
            self.d.advance_restarts(self.clock() * 1000)
            discovery.assert_not_called()
        self.assertEqual([a for a in self.api.runs if a[:2] == ["agent", "start"]], [])
        self.assertTrue(any("configuration or executable changed" in r.get("text", "") for r in self.records("restart_failed")))


class Selection(unittest.TestCase):
    def test_native_tool_context_selects_approved_model_but_real_credentials_refuse(self):
        with TempState() as ts, mock.patch.object(I.H, "which", return_value="/fake/claude"), mock.patch.object(I.H, "version", return_value="2.1.299"), mock.patch.object(I, "_claude", return_value={
                "providers": [{"id": "firstParty", "authentication": "credentials_reported"}],
                "models": [{"id": "haiku", "provider": "firstParty"}], "account_context": "same-account", "native_context": "same-native"}):
            I._cache.clear()
            approved = S.approve(["claude:firstParty"], str(ts.home), ts.env)
            team = enroll(ts)
            team.config["staffing"]["approved"] = approved
            native = dict(ts.env, CLAUDE_CODE_MESSAGING_TOKEN="native-process-messaging")
            chosen = S.selection(team, "claude", "haiku", None, None, str(ts.home), native)
            self.assertEqual(chosen["context"], approved["claude:firstParty"]["context"])
            with self.assertRaises(HerdrTeamError) as caught:
                S.selection(team, "claude", "haiku", None, None, str(ts.home), dict(native, CLAUDE_CODE_OAUTH_TOKEN="changed-login"))
            self.assertEqual(caught.exception.code, "provider_context_changed")

    def test_provider_pool_accepts_configured_unverified_candidates(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()):
            team = enroll(ts)
            choice = S.selection(team, "codex", "custom-model", None, None, str(ts.home), ts.env)
            self.assertEqual((choice["route"], choice["inference"]), ("codex:openai", "unverified"))

    def test_context_change_hidden_model_unknown_effort_and_wrong_route_refuse(self):
        for inv, effort, expected in ((inventory(context="changed"), None, "provider_context_changed"),
                                     (inventory(provider="foreign"), None, "provider_not_approved"),
                                     (dict(inventory(), models=[{"id": "model-a", "provider": "openai", "hidden": True}]), None, "model_hidden"),
                                     (dict(inventory(), models=[{"id": "model-a", "provider": "openai"}]), "high", "effort_unknown"),
                                     (inventory(), "medium", "effort_unsupported")):
            with TempState() as ts, mock.patch.object(I, "discover", return_value=inv):
                with self.assertRaises(HerdrTeamError) as caught:
                    S.selection(enroll(ts), "codex", "model-a", effort, None, str(ts.home), ts.env)
                self.assertEqual(caught.exception.code, expected)

    def test_other_harnesses_stay_manual(self):
        with self.assertRaises(HerdrTeamError):
            S.routes(["pi:anthropic"])

    def test_invalid_limits_and_route_syntax(self):
        for value in (0, -1, True, 65, "4"):
            with self.assertRaises(HerdrTeamError):
                S.max_workers(value)
        for routes in ([], ["codex"], ["codex:--provider"], ["codex:bad\nvalue"]):
            with self.assertRaises(HerdrTeamError):
                S.routes(routes)


class CommandIntegration(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(I, "fingerprint", return_value="ctx")
        patch.start()
        self.addCleanup(patch.stop)

    def argv(self):
        return ["--json", "--team", "alpha", "staff", "add", "dev", "codex", "--model", "model-a",
                "--mission", "Implement and verify", "--acceptance", "Tests pass", "--request-id", "req-1"]

    def test_actual_cli_spawns_once_with_mission_before_launch_and_inherited_permissions(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()):
            enroll(ts)
            api = spawning_api(["codex"])
            original = api.run
            observed = []
            def start(argv, *a, **kw):
                if argv[:2] == ["agent", "start"]:
                    member = roster.load_team(ts.team).find("alpha-dev")
                    observed.append((member.staffing, ts.team.instructions(member.name).read_text(), argv))
                return original(argv, *a, **kw)
            api.run = start
            result = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual(result[0], 0, result[2])
            self.assertEqual(len(observed), 1)
            self.assertIn("Acceptance: Tests pass", observed[0][1])
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", observed[0][2])
            repeated = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual(repeated[0], 0, repeated[2])
            self.assertTrue(repeated[1]["reused"])
            self.assertEqual(len(observed), 1)

    def test_staffing_mission_is_not_laundered_into_operator_instructions(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()):
            enroll(ts)
            result = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), spawning_api(["codex"])))
            self.assertEqual(result[0], 0, result[2])
            member = roster.load_team(ts.team).find("alpha-dev")
            text = cmd_hooks.brief_context(ts.team, "alpha", member.to_json())
            self.assertIn("manager-assigned work; subordinate to operator Rules", text)
            self.assertNotIn("your brief (operator authority)", text)
            self.assertNotIn("your instructions (operator authority)", text)
            self.assertEqual(len(work.load(ts.team)), 1)
            item = next(iter(work.load(ts.team).values()))
            self.assertEqual((item.owner, item.brief["acceptance"]), (member.name, "Tests pass"))

    def test_native_permission_policy_is_inherited_without_yolo(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()):
            enroll(ts)
            roster.update_team(ts.team, lambda t: t.config.update(permissions="native"))
            api = spawning_api(["codex"])
            original, launched = api.run, []
            def start(argv, *a, **kw):
                launched.append(argv)
                return original(argv, *a, **kw)
            api.run = start
            result = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual(result[0], 0, result[2])
            self.assertFalse(any("--dangerously-bypass-approvals-and-sandbox" in argv for argv in launched))

    def test_foreign_private_mission_is_not_overwritten_or_adopted(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()):
            enroll(ts)
            target = ts.team.instructions("alpha-dev")
            target.write_text("foreign document")
            api = spawning_api(["codex"])
            result = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual((result[0], result[2]["code"]), (1, "worker_instructions_exist"))
            self.assertEqual(target.read_text(), "foreign document")
            self.assertFalse(any(method == "layout.apply" for method, _ in api.calls))

    def test_mission_arriving_during_layout_is_not_overwritten_and_agent_never_starts(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()):
            enroll(ts)
            api = spawning_api(["codex"])
            original = api.request
            target = ts.team.instructions("alpha-dev")
            def request(method, *args, **kwargs):
                result = original(method, *args, **kwargs)
                if method == "layout.apply":
                    target.write_text("arrived while laying out")
                return result
            api.request = request
            result = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual((result[0], result[2]["code"]), (1, "worker_instructions_exist"))
            self.assertEqual(target.read_text(), "arrived while laying out")
            self.assertFalse(any(argv[:2] == ["agent", "start"] for argv in api.runs))
            self.assertEqual(S.attempts(roster.load_team(ts.team))["req-1"]["phase"], "ambiguous")

    def test_unsupported_atomic_mission_publication_fails_closed(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()):
            enroll(ts)
            api = spawning_api(["codex"])
            with mock.patch("herdr_team.charter.os.link", side_effect=OSError("unsupported")):
                result = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual((result[0], result[2]["code"]), (1, "worker_instructions_publish_failed"))
            self.assertFalse(ts.team.instructions("alpha-dev").exists())
            self.assertFalse(any(argv[:2] == ["agent", "start"] for argv in api.runs))

    def test_operator_only_model_checks_require_explicit_consent(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()), mock.patch.object(model_checks, "check", return_value={"kind": "codex", "requested_model": "model-a", "status": "verification_unsupported"}) as check:
            argv = ["--json", "--team", "alpha", "model-check", "codex", "--model", "model-a", "--cwd", str(ts.home)]
            result = json_out(run_cli(argv, env_no_daemon(ts), live_api()))
            self.assertEqual(result[2]["code"], "confirmation_required")
            result = json_out(run_cli(argv + ["--yes"], env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), live_api()))
            self.assertEqual(result[2]["code"], "author_mismatch")
            check.assert_not_called()
            result = json_out(run_cli(argv + ["--yes"], env_no_daemon(ts), live_api()))
            self.assertEqual(result[0], 0, result[2])
            check.assert_called_once()

    def test_autonomous_creation_requires_capability_and_saves_policy_before_briefing(self):
        with TempState(write_team=False) as ts, mock.patch.object(I, "discover", return_value=inventory()):
            argv = ["--json", "create", "beta", "--new", "--spawn", "lead:codex", "--leader", "role:lead", "--brief", "lead=Research and staff",
                    "--charter", "Task", "--project", str(ts.home), "--autonomous", "--allow-provider", "codex:openai", "--acceptance", "Tests pass"]
            api = spawning_api(["codex"])
            result = json_out(run_cli(argv, env_no_daemon(ts), api))
            self.assertEqual((result[0], result[2]["code"]), (1, "staffing_capability_unavailable"))
            self.assertFalse(ts.session.team("beta").root.exists())
            write_live_daemon(ts)
            info = store.read_json(ts.session.daemon_json)
            info["capabilities"] = {S.CAPABILITY: True}
            store.write_json(ts.session.daemon_json, info)
            result = json_out(run_cli(argv, env_no_daemon(ts), api))
            self.assertEqual(result[0], 0, result[2])
            team = roster.load_team(ts.session.team("beta"))
            self.assertEqual(team.config["staffing"]["max_workers"], 4)
            self.assertEqual(team.manager().name, "beta-lead")
            self.assertTrue(result[1]["briefing_jobs"])

    def test_layout_failure_is_durable_and_never_repeated(self):
        with TempState() as ts, mock.patch.object(I, "discover", return_value=inventory()):
            enroll(ts)
            api = live_api()
            api.set_response("layout.apply", {"layout": {"root": None}})
            result = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual(result[0], 1)
            self.assertEqual(S.attempts(roster.load_team(ts.team))["req-1"]["phase"], "ambiguous")
            repeat = json_out(run_cli(self.argv(), env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertTrue(repeat[1]["reused"])
            self.assertEqual(sum(method == "layout.apply" for method, params in api.calls), 1)

    def test_creation_recursion_is_disabled_only_for_enrolled_agents(self):
        with TempState() as ts:
            args = ["--json", "create", "beta", "--leader", "role:reviewer", "--member", "w5:p1:reviewer", "--brief", "reviewer=Review"]
            enroll(ts)
            code, _, err = json_out(run_cli(args, env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), live_api()))
            self.assertEqual((code, err["code"]), (1, "staffing_scope"))
            code, _, err = json_out(run_cli(args, env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)

    def test_operator_pause_cannot_be_undone_by_manager(self):
        with TempState() as ts:
            enroll(ts)
            for verb, env, expected in (("pause", env_no_daemon(ts), 0),
                                         ("resume", env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), 1),
                                         ("resume", env_no_daemon(ts), 0)):
                result = json_out(run_cli(["--json", "--team", "alpha", "staff", verb], env, live_api()))
                self.assertEqual(result[0], expected, result[2])

    def test_pause_is_serialized_with_worker_launch_submission(self):
        with TempState() as ts:
            enroll(ts)
            with store.FileLock(ts.team.root / "restore.lock"):
                result = json_out(run_cli(["--json", "--team", "alpha", "staff", "pause"], env_no_daemon(ts), live_api()))
            self.assertEqual((result[0], result[2]["code"]), (5, "staffing_busy"))
            self.assertFalse(roster.load_team(ts.team).config["staffing"]["paused"])

    def test_manual_teams_are_not_enrolled_by_status_or_upgrade(self):
        with TempState() as ts:
            before = ts.team.team_json.read_bytes()
            result = json_out(run_cli(["--json", "--team", "alpha", "staff", "status"], env_no_daemon(ts), live_api()))
            self.assertFalse(result[1]["enabled"])
            self.assertEqual(ts.team.team_json.read_bytes(), before)

    def test_retirement_releases_only_exact_owned_stopped_terminal(self):
        with TempState() as ts:
            enroll(ts)
            # Reserve a distinct name, then attach the test member without adopting any real pane.
            S.reserve(ts.team, manager(), "r1", "alpha-dev", "dev", CHOSEN, "Mission", "Tests", None)
            def owned(team):
                team.find("alpha-worker").staffing = {"request_id": "r1"}
                team.config["staffing"]["attempts"]["r1"].update(name="alpha-worker", phase="retiring", pane_id="w2:p2", terminal_id="term_w1", generation=1)
            roster.update_team(ts.team, owned)
            api = live_api()
            api.set_response("pane.get", {"pane": {"terminal_id": "foreign", "agent": None}})
            cmd_staff.reconcile(ts.team, api)
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)
            api.set_response("pane.get", {"pane": {"terminal_id": "term_w1", "agent": "claude"}})
            cmd_staff.reconcile(ts.team, api)
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)
            api.set_response("pane.get", {"pane": {"pane_id": "w2:p2", "terminal_id": "term_w1", "agent": None}})
            api.set_response("pane.process_info", {"process_info": {"pane_id": "w2:p2", "shell_pid": 100, "foreground_process_group_id": 100, "foreground_processes": [{"pid": 100}]}})
            with mock.patch.object(identity, "ps_table", return_value={100: 1}):
                cmd_staff.reconcile(ts.team, api)
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 0)
            self.assertTrue(roster.load_team(ts.team).find("alpha-worker"))


class RetirementEvidence(unittest.TestCase):
    def retiring(self, ts):
        enroll(ts, maximum=1)
        S.reserve(ts.team, manager(), "r1", "alpha-dev", "dev", CHOSEN, "Mission", "Tests", None)
        def attach(team):
            member = team.find("alpha-worker")
            member.staffing = {"request_id": "r1"}
            team.config["staffing"]["attempts"]["r1"].update(name=member.name, phase="retiring", pane_id=member.pane_id, terminal_id=member.terminal_id, generation=member.generation)
        roster.update_team(ts.team, attach)
        api = live_api()
        api.set_response("pane.get", {"pane": fake_pane("w2:p2", "term_w1", "claude", status="unknown")})
        api.set_response("agent.get", {"agent": fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="unknown")})
        api.set_response("pane.process_info", {"process_info": {"pane_id": "w2:p2", "shell_pid": 100, "foreground_process_group_id": 100, "foreground_processes": [{"pid": 100}]}})
        return api

    def test_retained_kind_with_unknown_status_and_only_shell_releases_one_slot(self):
        with TempState() as ts, mock.patch.object(identity, "ps_table", return_value={100: 1}):
            api = self.retiring(ts)
            cmd_staff.reconcile(ts.team, api)
            team = roster.load_team(ts.team)
            self.assertEqual(S.capacity(team), 0)
            self.assertEqual(team.find("alpha-worker").status, "unbound")
            self.assertEqual(S.attempts(team)["r1"]["history"][-1]["phase"], "retired")
            S.reserve(ts.team, manager(), "r2", "alpha-next", "next", CHOSEN, "Next", "Tests", None)
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)

    def test_older_agent_info_without_optional_launch_flag_still_needs_stop_proof(self):
        with TempState() as ts, mock.patch.object(identity, "ps_table", return_value={100: 1}):
            api = self.retiring(ts)
            api.responses["agent.get"]["agent"].pop("launch_pending")
            cmd_staff.reconcile(ts.team, api)
            self.assertEqual(S.capacity(roster.load_team(ts.team)), 0)

    def test_stopped_removed_worker_releases_slot_without_reactivating_its_claim(self):
        with TempState() as ts, mock.patch.object(identity, "ps_table", return_value={100: 1}):
            api = self.retiring(ts)
            roster.update_team(ts.team, lambda team: setattr(team.find("alpha-worker"), "status", "left"))
            api.responses["agent.get"]["agent"]["name"] = None
            cmd_staff.reconcile(ts.team, api)
            team = roster.load_team(ts.team)
            self.assertEqual(S.capacity(team), 0)
            self.assertEqual(team.find("alpha-worker").status, "left")

    def test_empty_or_ambiguous_process_evidence_never_releases_capacity(self):
        base = {"pane_id": "w2:p2", "shell_pid": 100, "foreground_process_group_id": 100, "foreground_processes": [{"pid": 100}]}
        cases = [None, {}, dict(base, foreground_processes=[]), dict(base, shell_pid=True),
                 dict(base, shell_pid=0), dict(base, foreground_process_group_id=None),
                 dict(base, foreground_process_group_id=101), dict(base, pane_id="foreign"),
                 dict(base, foreground_processes=[{}]), dict(base, foreground_processes=[None]),
                 dict(base, foreground_processes=[{"pid": 100.0}]),
                 dict(base, foreground_processes=[{"pid": 101}]), dict(base, foreground_processes=[{"pid": 100}, {"pid": 101}])]
        for info in cases:
            with self.subTest(info=info), TempState() as ts, mock.patch.object(identity, "ps_table", return_value={100: 1}):
                api = self.retiring(ts)
                # Even a cleared display hint cannot substitute for positive stop evidence.
                api.set_response("pane.get", {"pane": fake_pane("w2:p2", "term_w1", None)})
                api.set_response("pane.process_info", {"process_info": info})
                cmd_staff.reconcile(ts.team, api)
                self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)

    def test_background_child_or_unavailable_local_shell_retains_capacity(self):
        for table in (None, {101: 1}, {100: 1, 101: 100}, {100: 1, 101: 100, 102: 101}):
            with self.subTest(table=table), TempState() as ts, mock.patch.object(identity, "ps_table", return_value=table):
                cmd_staff.reconcile(ts.team, self.retiring(ts))
                self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)

    def test_live_or_foreign_agent_hints_cannot_be_retired_as_a_stale_kind(self):
        for changes in ({"agent_status": "idle"}, {"agent_status": "working"}, {"agent_status": "blocked"}, {"agent": "codex"}):
            with self.subTest(changes=changes), TempState() as ts, mock.patch.object(identity, "ps_table", return_value={100: 1}):
                api = self.retiring(ts)
                api.set_response("pane.get", {"pane": fake_pane("w2:p2", "term_w1", "claude", status="unknown", **changes)})
                cmd_staff.reconcile(ts.team, api)
                self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)
        for changes in ({"launch_pending": True}, {"name": "foreign"}, {"terminal_id": "foreign"}, {"agent_status": "working"}):
            with self.subTest(changes=changes), TempState() as ts, mock.patch.object(identity, "ps_table", return_value={100: 1}):
                api = self.retiring(ts)
                row = fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="unknown")
                row.update(changes)
                api.set_response("agent.get", {"agent": row})
                cmd_staff.reconcile(ts.team, api)
                self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)

    def test_identity_change_during_stop_probe_cannot_free_a_rebound_worker(self):
        with TempState() as ts, mock.patch.object(identity, "ps_table", return_value={100: 1}):
            api = self.retiring(ts)
            api.set_response("pane.get", {"pane": fake_pane("w2:p2", "term_w1", None)})
            info = copy.deepcopy(api.responses["pane.process_info"])
            def rebind(_params):
                roster.update_team(ts.team, lambda team: setattr(team.find("alpha-worker"), "generation", 2))
                return info
            api.set_response("pane.process_info", rebind)
            cmd_staff.reconcile(ts.team, api)
            team = roster.load_team(ts.team)
            self.assertEqual(S.capacity(team), 1)
            self.assertEqual(team.find("alpha-worker").status, "active")

    def test_reservation_or_member_change_during_probe_preserves_capacity(self):
        for target, key, value in (("member", "terminal_id", "foreign"), ("member", "kind", "codex"),
                                   ("member", "pane_id", "foreign"), ("member", "status", "left"),
                                   ("attempt", "generation", 2), ("attempt", "phase", "active"), ("attempt", "pane_id", "foreign")):
            with self.subTest(target=target, key=key), TempState() as ts, mock.patch.object(identity, "ps_table", return_value={100: 1}):
                api = self.retiring(ts)
                info = copy.deepcopy(api.responses["pane.process_info"])
                def changed(_params):
                    def change(team):
                        if target == "member":
                            setattr(team.find("alpha-worker"), key, value)
                        else:
                            S.attempts(team)["r1"][key] = value
                    roster.update_team(ts.team, change)
                    return info
                api.set_response("pane.process_info", changed)
                cmd_staff.reconcile(ts.team, api)
                self.assertEqual(S.capacity(roster.load_team(ts.team)), 1)


if __name__ == "__main__":
    unittest.main()

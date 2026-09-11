"""Model and reasoning effort per member (0.14.0). Every test here fails against 0.13.0.

The harness facts these encode were read from the installed binaries
(``herdr_team/models.py`` docstring, refreshed 2026-09-11); the live round trips are pinned below
and covered by the disposable live compatibility run.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from support import FAKE_AGENTS, FakeApi, TempState, fake_agent, fake_pane, fake_process_info
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_context import STRONG_IDLE, ticks
from test_daemon import make_daemon

from herdr_team import cmd_hooks, cmd_roster, context, models, picker, render, roster, store, tui_model
from herdr_team import daemon as D
from herdr_team.errors import HerdrTeamError, UsageError

MEMBER = "alpha-reviewer"   # codex, w2:p1, term_r1
PEER = "alpha-worker"       # claude, w2:p2, term_w1


def sess(value, source="herdr:codex", agent="codex"):
    return {"source": source, "agent": agent, "kind": "id", "value": value}


def set_member(ts, name, **fields):
    doc = store.read_json(ts.team.team_json)
    for m in doc["members"]:
        if m["name"] == name:
            m.update(fields)
    store.write_json(ts.team.team_json, doc)


def set_config(ts, **config):
    doc = store.read_json(ts.team.team_json)
    doc.setdefault("config", {}).update(config)
    store.write_json(ts.team.team_json, doc)


# --------------------------------------------------------------------------
# the table


class SettingTests(unittest.TestCase):
    def test_parse_and_label_round_trip(self):
        for text, parsed in (("opus@medium", ("opus", "medium")), ("opus", ("opus", None)), ("@high", (None, "high")),
                             ("opencode/claude-opus-4-8@max", ("opencode/claude-opus-4-8", "max")), ("gpt-5.6-luna@xhigh", ("gpt-5.6-luna", "xhigh"))):
            self.assertEqual(models.parse_setting(text), parsed, text)
            self.assertEqual(models.label(*parsed), text)
        self.assertIsNone(models.label(None, None))

    def test_nonsense_is_a_usage_error(self):
        for bad in ("", "@", "a@b@c", "opus medium", "x;rm -rf /"):
            with self.assertRaises(UsageError, msg=bad):
                models.parse_setting(bad)

    def test_launch_args_are_the_harness_own_flags(self):
        self.assertEqual(set(models.UNRESTRICTED_ARGS), set(models.KINDS))
        self.assertEqual(models.launch_args("claude", None, None), ["--dangerously-skip-permissions"])
        self.assertEqual(models.launch_args("codex", None, None), ["--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(models.launch_args("opencode", None, None), ["--auto"])
        self.assertEqual(models.launch_args("claude", "opus", "medium"),
                         ["--model", "opus", "--effort", "medium", "--dangerously-skip-permissions"])
        self.assertEqual(models.launch_args("claude", None, "max"), ["--effort", "max", "--dangerously-skip-permissions"])
        # Codex's -c value is TOML: the quotes are part of the argument, not of a shell
        self.assertEqual(models.launch_args("codex", "gpt-5.6-luna", "high"),
                         ["-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"', "--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(models.launch_args("opencode", "opencode/claude-opus-4-8", "max"),
                         ["-m", "opencode/claude-opus-4-8", "--auto"])
        self.assertEqual(models.launch_args("gemini", None, None), [])

    def test_an_unsupported_kind_and_an_unknown_effort_are_refused_by_name(self):
        with self.assertRaises(HerdrTeamError) as caught:
            models.launch_args("gemini", "pro", None)
        self.assertEqual((caught.exception.code, caught.exception.details["supported"]), ("model_unsupported", ["claude", "codex", "opencode"]))
        with self.assertRaises(HerdrTeamError) as caught:
            models.validate("codex", None, "max")
        self.assertEqual((caught.exception.code, caught.exception.details["efforts"]), ("effort_unknown", ["minimal", "low", "medium", "high", "xhigh"]))
        with self.assertRaises(HerdrTeamError) as caught:
            models.validate("claude", None, "minimal")
        self.assertEqual(caught.exception.code, "effort_unknown")
        models.validate("opencode", None, "whatever-the-provider-calls-it")  # provider-specific: passed through
        models.validate("gemini", None, None)  # nothing set: nothing to refuse

    def test_resume_argv_appends_the_flags_to_the_recorded_resume(self):
        self.assertEqual(models.resume_argv("codex", sess("0199"), "gpt-5.6-luna", "high"),
                         ["codex", "resume", "0199", "-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"',
                          "--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(models.resume_argv("claude", sess("abc", "herdr:claude", "claude"), "opus", "medium"),
                         ["claude", "--resume", "abc", "--model", "opus", "--effort", "medium", "--dangerously-skip-permissions"])
        self.assertEqual(models.resume_argv("opencode", sess("s1", "herdr:opencode", "opencode"), "opencode/big-pickle", None),
                         ["opencode", "--session", "s1", "-m", "opencode/big-pickle", "--auto"])
        self.assertEqual(models.resume_argv("opencode", sess("s1", "herdr:opencode", "opencode"), "opencode/big-pickle", "high"),
                         ["opencode", "--session", "s1", "-m", "opencode/big-pickle", "--auto"])

    def test_restart_argv_preserves_only_allowlisted_runtime_policy(self):
        current = [
            "/opt/bin/codex", "resume", "0199", "-m", "old-model",
            "-c", 'model_reasoning_effort="low"', "-a", "never",
            "-s=danger-full-access", "--search", "--image", "/private/input.png",
        ]
        self.assertEqual(models.foreground_argv("codex", [{"name": "codex", "argv": current}]), current)
        self.assertEqual(
            models.preserved_launch_args("codex", current),
            ["--search"],
        )
        self.assertEqual(
            models.restart_argv("codex", sess("0199"), "gpt-5.6-luna", "high", current),
            ["codex", "resume", "0199", "-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"',
             "--dangerously-bypass-approvals-and-sandbox", "-c", "check_for_update_on_startup=false", "--search"],
        )
        self.assertIsNone(models.foreground_argv("codex", [{"name": "zsh", "argv": ["-zsh"]}]))
        self.assertEqual(
            models.fresh_argv("opencode", "opencode/glm-5.3-flash", "high",
                              ["opencode", "--session", "old", "-m", "old/model", "--pure", "--auto"]),
            ["opencode", "-m", "opencode/glm-5.3-flash", "--auto", "--pure"],
        )

    def test_native_live_commands_are_exact_per_harness(self):
        self.assertEqual(models.live_keystrokes("claude", "opus", "medium"), ["/model opus", "/effort medium"])
        self.assertEqual(models.live_keystrokes("claude", None, "high"), ["/effort high"])
        self.assertIsNone(models.live_keystrokes("codex", "gpt-5.6-luna", "high"))
        self.assertEqual(models.live_keystrokes("opencode", None, "max"), ["/variants", "max"])
        self.assertIsNone(models.live_keystrokes("opencode", "x/y", "max"), "OpenCode's model picker has no exact argument form")
        self.assertEqual(models.post_start_keystrokes("opencode", "max"), ["/variants", "max"])
        self.assertEqual(models.post_start_keystrokes("codex", "high"), [])
        self.assertEqual((models.exit_keystroke("claude"), models.exit_keystroke("codex"), models.exit_keystroke("opencode")), ("/exit", "/quit", "/exit"))

    def test_observed_matches_understands_aliases_and_provider_prefixes(self):
        self.assertTrue(models.observed_matches("claude", "opus", "claude-opus-5"))
        self.assertTrue(models.observed_matches("claude", "claude-opus-5", "claude-opus-5"))
        self.assertFalse(models.observed_matches("claude", "opus", "claude-sonnet-5"))
        self.assertTrue(models.observed_matches("opencode", "opencode/claude-opus-4-8", "claude-opus-4-8"))
        self.assertFalse(models.observed_matches("codex", "gpt-5.6-luna", None))

    def test_effective_setting_resolves_in_three_layers(self):
        config = {"models": {"codex": {"model": "gpt-5.6-sol", "effort": "medium"}}}
        self.assertEqual(models.effective_setting(config, {"kind": "codex"}), ("gpt-5.6-sol", "medium"))
        self.assertEqual(models.effective_setting(config, {"kind": "codex", "effort": "xhigh"}), ("gpt-5.6-sol", "xhigh"))
        self.assertEqual(models.effective_setting(config, {"kind": "codex", "model": "gpt-5.6-luna"}), ("gpt-5.6-luna", "medium"))
        self.assertEqual(models.effective_setting(config, {"kind": "claude"}), (None, None))
        self.assertEqual(models.source_of(config, {"kind": "codex", "effort": "xhigh"}), ("default", "member"))
        self.assertEqual(models.source_of({}, {"kind": "claude"}), ("harness", "harness"))


class RosterTests(unittest.TestCase):
    def test_the_fields_round_trip_and_an_old_roster_loads_as_none(self):
        member = roster.Member(name="x", role="r", kind="codex", terminal_id="t", model="gpt-5.6-luna", effort="high")
        again = roster.Member.from_json(member.to_json())
        self.assertEqual((again.model, again.effort), ("gpt-5.6-luna", "high"))
        bare = roster.Member.from_json({"name": "y", "role": "r", "kind": "claude", "terminal_id": "t"})
        self.assertEqual((bare.model, bare.effort), (None, None))
        self.assertNotIn("model", roster.Member(name="z", role="r", kind="claude", terminal_id="t").to_json(), "unset halves are not written")


# --------------------------------------------------------------------------
# create / add / resume carry it


class SpawnTests(unittest.TestCase):
    STARTED_CODEX = {"type": "agent_started", "agent": fake_agent("w9:p1", "term_new", "codex", "delta-reviewer", launch_pending=False), "argv": ["codex"]}
    STARTED_CLAUDE = {"type": "agent_started", "agent": fake_agent("w9:p2", "term_new2", "claude", "delta-worker", launch_pending=False), "argv": ["claude"]}
    STARTED_OPENCODE = {"type": "agent_started", "agent": fake_agent("w9:p1", "term_oc", "opencode", "delta-builder", launch_pending=False), "argv": ["opencode"]}

    def api(self):
        api = live_api()
        api.set_response("layout.apply", lambda params: {"type": "layout_apply", "layout": {"workspace_id": "w9", "tab_id": "w9:t1", "zoomed": False, "focused_pane_id": "w9:p1",
                                                          "root": {"type": "split", "direction": "right", "ratio": 0.5, "first": {"type": "pane", "pane_id": "w9:p1"}, "second": {"type": "pane", "pane_id": "w9:p2"}}}})
        return api

    def no_waiting(self):
        """The settle poll is time-mocked as in ``test_cmd_roster``; the members may end ``failed``, which is not what these tests are about."""
        clock = [0.0]

        def monotonic():
            clock[0] += 30.0
            return clock[0]

        self.addCleanup(setattr, cmd_roster, "_sleep", cmd_roster._sleep)
        self.addCleanup(setattr, cmd_roster, "_monotonic", cmd_roster._monotonic)
        cmd_roster._sleep = lambda _s: None
        cmd_roster._monotonic = monotonic

    def test_agent_start_carries_the_flags_after_a_double_dash(self):
        self.assertEqual(cmd_roster.agent_start_argv("n", "codex", "w9:p1", args=["-m", "gpt-5.6-luna"]),
                         ["agent", "start", "n", "--kind", "codex", "--pane", "w9:p1", "--timeout", "60000", "--", "-m", "gpt-5.6-luna"])
        self.assertEqual(cmd_roster.agent_start_argv("n", "codex", "w9:p1"), ["agent", "start", "n", "--kind", "codex", "--pane", "w9:p1", "--timeout", "60000"])

    def test_create_new_starts_each_agent_with_its_setting_and_records_the_defaults(self):
        with TempState(write_team=False) as ts:
            self.no_waiting()
            api = self.api()
            api.set_cli_result(["agent", "start"], self.STARTED_CODEX, request_id="cli:agent:start")
            code, payload, err = json_out(run_cli([
                "--json", "create", "delta", "--new", "--workspace", "w9",
                "--spawn", "reviewer:codex", "--spawn", "worker:claude",
                "--brief", "reviewer=Review every patch.", "--brief", "worker=Implement the patch.",
                "--model", "reviewer=gpt-5.6-luna@high", "--model", "claude=opus@medium",
            ], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            starts = [argv for argv in api.runs if argv[:2] == ["agent", "start"]]
            self.assertEqual(len(starts), 2, starts)
            by_name = {argv[2]: argv for argv in starts}
            self.assertEqual(by_name["delta-reviewer"][by_name["delta-reviewer"].index("--") + 1:],
                             ["-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"', "--dangerously-bypass-approvals-and-sandbox"])
            self.assertEqual(by_name["delta-worker"][by_name["delta-worker"].index("--") + 1:],
                             ["--model", "opus", "--effort", "medium", "--dangerously-skip-permissions"])
            doc = store.read_json(ts.session.team("delta").team_json)
            self.assertEqual(doc["config"]["models"], {"claude": {"model": "opus", "effort": "medium"}})
            rows = {m["name"]: m for m in doc["members"]}
            self.assertEqual((rows["delta-reviewer"].get("model"), rows["delta-reviewer"].get("effort")), ("gpt-5.6-luna", "high"))
            self.assertNotIn("model", rows["delta-worker"], "a kind default is not copied onto the member; it stays a default")

    def test_a_kind_with_no_verified_flags_is_refused_before_anything_is_laid_out(self):
        with TempState(write_team=False) as ts:
            self.no_waiting()
            api = self.api()
            code, _payload, err = json_out(run_cli(["--json", "create", "delta", "--new", "--workspace", "w9", "--spawn", "qa:gemini", "--model", "qa=pro@high"], env_no_daemon(ts), api))
            self.assertEqual((code, err["code"]), (1, "model_unsupported"))
            self.assertEqual([m for m, _p in api.calls if m == "layout.apply"], [])
            self.assertFalse(ts.session.team("delta").team_json.exists())
            code, _payload, err = json_out(run_cli(["--json", "create", "delta", "--new", "--workspace", "w9", "--spawn", "qa:codex", "--model", "nobody=@high"], env_no_daemon(ts), api))
            self.assertEqual((code, err["code"]), (2, "usage"))

    def test_opencode_spawn_selects_the_variant_before_it_queues_the_briefing(self):
        with TempState(write_team=False) as ts:
            agent = fake_agent("w9:p1", "term_oc", "opencode", "delta-builder", launch_pending=False)
            api = live_api([agent])
            api.set_response("layout.apply", {"type": "layout_apply", "layout": {"workspace_id": "w9", "tab_id": "w9:t1",
                                                                                     "zoomed": False, "focused_pane_id": "w9:p1",
                                                                                     "root": {"type": "pane", "pane_id": "w9:p1"}}})
            api.set_cli_result(["agent", "start"], self.STARTED_OPENCODE, request_id="cli:agent:start")
            code, payload, err = json_out(run_cli([
                "--json", "create", "delta", "--new", "--workspace", "w9",
                "--spawn", "builder:opencode", "--brief", "builder=Build the feature.", "--model", "builder=opencode/glm-5.3-flash@high",
            ], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            start = next(argv for argv in api.runs if argv[:2] == ["agent", "start"])
            self.assertEqual(start[start.index("--") + 1:], ["-m", "opencode/glm-5.3-flash", "--auto"])
            jobs = [store.read_json(path) for path in ts.session.team("delta").jobs_dir.glob("*.json")]
            self.assertEqual(len(jobs), 1)
            self.assertEqual((jobs[0]["kind"], jobs[0]["action"]), ("control", "model"))
            control = next(record["control"] for record in store.BoardStore(ts.session.team("delta")).read()
                           if isinstance(record.get("control"), dict))
            self.assertEqual(control["keystrokes"], ["/variants", "high"])
            self.assertTrue(control["brief_after"])

    def test_add_records_a_setting_for_a_live_agent(self):
        with TempState() as ts:
            api = live_api([fake_agent("w5:p1", "term_5", "claude", None)])
            code, payload, err = json_out(run_cli(["--json", "add", "alpha", "w5:p1", "--role", "qa", "--brief", "Test the patch.", "--model", "opus@high"], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            doc = store.read_json(ts.team.team_json)
            row = next(m for m in doc["members"] if m["role"] == "qa")
            self.assertEqual((row.get("model"), row.get("effort")), ("opus", "high"))

    def test_resume_reopens_the_session_with_the_effective_flags(self):
        with TempState() as ts:
            set_member(ts, MEMBER, session=sess("0199-reviewer"), effort="xhigh")
            set_config(ts, models={"codex": {"model": "gpt-5.6-sol", "effort": "medium"}})
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "resume", MEMBER, "--print"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["argv"], ["codex", "resume", "0199-reviewer", "-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="xhigh"',
                                                "--dangerously-bypass-approvals-and-sandbox"])
            self.assertEqual(payload["setting"], "gpt-5.6-sol@xhigh")


# --------------------------------------------------------------------------
# the model command


class ModelCommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        # a codex member with a session, so --apply restart can plan a resume
        set_member(self.ts, MEMBER, session=sess("0199-reviewer"))

    def model(self, argv, env=None, api=None):
        return json_out(run_cli(["--json", "--team", "alpha", "model"] + list(argv), env if env is not None else env_no_daemon(self.ts), api or live_api()))

    def test_the_operator_sets_anyone_and_it_is_announced_to_the_member_and_the_team(self):
        code, payload, err = self.model([MEMBER, "gpt-5.6-luna@high"])
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["setting"], payload["apply"], payload["by"]), ("gpt-5.6-luna@high", "next", "operator"))
        row = next(m for m in store.read_json(self.ts.team.team_json)["members"] if m["name"] == MEMBER)
        self.assertEqual((row["model"], row["effort"]), ("gpt-5.6-luna", "high"))
        changed = [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "model_changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["to"], [MEMBER, "all"])
        self.assertIn("gpt-5.6-luna@high", changed[0]["text"])

    def test_reading_shows_configured_and_observed(self):
        set_config(self.ts, models={"codex": {"model": "gpt-5.6-sol", "effort": "medium"}})
        store.write_json(self.ts.session.who_json, {"teams": {"alpha": {"members": [{"name": MEMBER, "context": {"model": "gpt-5.6-sol", "used": 1, "window": 2, "percent": 50.0}}]}}})
        code, payload, err = self.model([])
        self.assertEqual(code, 0, err)
        row = next(r for r in payload["members"] if r["name"] == MEMBER)
        self.assertEqual((row["setting"], row["model_source"], row["effort_source"], row["observed"], row["observed_matches"]), ("gpt-5.6-sol@medium", "default", "default", "gpt-5.6-sol", True))
        code, payload, err = self.model([MEMBER])
        self.assertEqual((code, payload["setting"]), (0, "gpt-5.6-sol@medium"))

    def test_the_manager_may_set_a_peer_and_a_plain_member_may_not(self):
        set_member(self.ts, PEER, manager=True)
        env = env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2")  # the manager's pane
        code, payload, err = self.model([MEMBER, "@xhigh"], env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["by"], "manager")
        set_member(self.ts, PEER, manager=False)
        code, _payload, err = self.model([MEMBER, "@low"], env=env)
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertIn("manager", err["message"])

    def test_a_member_sets_itself_with_self_and_nobody_else(self):
        env = env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1")
        code, payload, err = self.model(["--self", "@xhigh"], env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["member"], payload["by"], payload["effort"]), (MEMBER, "self", "xhigh"))
        code, _payload, err = self.model([PEER, "--self", "@low"], env=env)
        self.assertEqual((code, err["code"]), (2, "usage"))

    def test_unsupported_live_change_and_restart_without_a_session_are_refused(self):
        code, _payload, err = self.model([MEMBER, "gpt-5.6-luna", "--apply", "live"])
        self.assertEqual((code, err["code"]), (1, "model_apply_unsupported"))
        self.assertIn("--apply restart", err["message"])
        set_member(self.ts, MEMBER, session=None)
        code, _payload, err = self.model([MEMBER, "gpt-5.6-luna", "--apply", "restart"])
        self.assertEqual((code, err["code"]), (1, "session_unknown"))

    def test_restart_writes_a_control_record_the_daemon_can_validate(self):
        from test_cmd_board import write_live_daemon

        write_live_daemon(self.ts)
        code, payload, err = self.model([MEMBER, "gpt-5.6-luna@high", "--apply", "restart"])
        self.assertEqual(code, 0, err)
        control = payload["control"]
        self.assertEqual((control["action"], control["exit"]), ("restart", "/quit"))
        self.assertEqual(control["argv"], ["codex", "resume", "0199-reviewer", "-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"',
                                                   "--dangerously-bypass-approvals-and-sandbox", "-c", "check_for_update_on_startup=false"])
        self.assertEqual(control["after"], [])
        record = next(r for r in store.BoardStore(self.ts.team).read() if r.get("seq") == payload["record_seq"])
        self.assertEqual((record["kind"], record["to"], record["control"]["action"]), ("direct", [MEMBER], "restart"))
        self.assertEqual(len(list(self.ts.team.jobs_dir.glob("*.json"))), 1)

    def test_restart_replaces_live_codex_permission_policy_with_the_unrestricted_default(self):
        from test_cmd_board import write_live_daemon

        write_live_daemon(self.ts)
        api = live_api()
        api.set_response("pane.process_info", fake_process_info("w2:p1", processes=[{
            "pid": 77, "name": "codex",
            "argv": ["/opt/bin/codex", "resume", "0199-reviewer", "-m", "old", "-c", 'model_reasoning_effort="low"',
                     "-a", "never", "-s", "danger-full-access"],
        }]))
        code, payload, err = self.model([MEMBER, "gpt-5.6-luna@high", "--apply", "restart"], api=api)
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["control"]["preserved"], [])
        self.assertEqual(
            payload["control"]["argv"],
            ["codex", "resume", "0199-reviewer", "-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"',
             "--dangerously-bypass-approvals-and-sandbox", "-c", "check_for_update_on_startup=false"],
        )

    def test_opencode_restart_uses_its_tui_for_the_variant_not_the_run_only_flag(self):
        from test_cmd_board import write_live_daemon

        set_member(self.ts, MEMBER, kind="opencode", session=sess("oc-1", "herdr:opencode", "opencode"))
        write_live_daemon(self.ts)
        code, payload, err = self.model([MEMBER, "opencode/glm-5.3-flash@high", "--apply", "restart"])
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["control"]["argv"], ["opencode", "--session", "oc-1", "-m", "opencode/glm-5.3-flash", "--auto"])
        self.assertEqual(payload["control"]["after"], ["/variants", "high"])

    def test_live_on_claude_types_model_then_effort(self):
        from test_cmd_board import write_live_daemon

        write_live_daemon(self.ts)
        code, payload, err = self.model([PEER, "opus@medium"])
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["apply"], payload["control"]["keystrokes"]), ("live", ["/model opus", "/effort medium"]))

    def test_opencode_effort_only_defaults_to_its_exact_live_variant_picker(self):
        from test_cmd_board import write_live_daemon

        set_member(self.ts, MEMBER, kind="opencode")
        write_live_daemon(self.ts)
        code, payload, err = self.model([MEMBER, "@high"])
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["apply"], payload["control"]["keystrokes"]), ("live", ["/variants", "high"]))

    def test_models_sets_and_clears_the_team_default(self):
        code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "models", "set", "claude", "opus@medium"], env_no_daemon(self.ts), live_api()))
        self.assertEqual(code, 0, err)
        self.assertEqual(store.read_json(self.ts.team.team_json)["config"]["models"], {"claude": {"model": "opus", "effort": "medium"}})
        code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "models"], env_no_daemon(self.ts), live_api()))
        self.assertEqual(payload["models"], {"claude": {"model": "opus", "effort": "medium"}})
        code, _payload, err = json_out(run_cli(["--json", "--team", "alpha", "models", "set", "gemini", "pro"], env_no_daemon(self.ts), live_api()))
        self.assertEqual((code, err["code"]), (1, "model_unsupported"))
        code, _payload, err = json_out(run_cli(["--json", "--team", "alpha", "models", "clear", "claude"], env_no_daemon(self.ts), live_api()))
        self.assertEqual(code, 0, err)
        self.assertEqual(store.read_json(self.ts.team.team_json)["config"]["models"], {})
        # a member may not set defaults
        code, _payload, err = json_out(run_cli(["--json", "--team", "alpha", "models", "set", "claude", "opus"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1"), live_api()))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))


# --------------------------------------------------------------------------
# the daemon


class ControlRig(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.api.set_response("agent.explain", STRONG_IDLE)
        self.d.control_gap_sleep = lambda _s: None
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def control(self, member, control, text="x"):
        record = {"from": "human", "from_kind": "human", "from_terminal": "term_console", "origin": {"via": "console", "verified": True},
                  "to": [member], "kind": "direct", "text": text, "control": control}
        seq = store.BoardStore(self.ts.team).append(record)
        store.write_json(self.ts.team.jobs_dir / "{}-control.json".format(int(time.time() * 1000000)),
                         {"v": 1, "kind": "control", "member": member, "action": control["action"], "seq": seq, "force": False,
                          "requested_by": {"name": "human", "via": "console", "verified": True}, "requested_at": store.now_iso()})
        self.clock.advance(1)
        self.d.consume_jobs(self.clock() * 1000)
        return seq

    def settle(self, seconds=25):
        ticks(self.d, self.clock, max(1, int(seconds // 5)), step=5)

    def sent(self, method):
        return [p for m, p in self.api.calls if m == method]

    def records(self, event):
        return [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == event]


class LiveModelJobTests(ControlRig):
    """The typing mechanics, on the codex member: its pane is not the focused one (gate 10 never types into a focused pane).
    Which lines a kind gets is ``models.live_keystrokes``, pinned in ``SettingTests``."""

    def test_it_types_each_line_with_its_own_enter_never_as_a_prompt(self):
        self.control(MEMBER, {"action": "model", "kind": "codex", "model": "gpt-5.6-luna", "effort": "high", "keystrokes": ["/model gpt-5.6-luna", "/effort high"]})
        self.assertEqual(self.team.pending[MEMBER].kind, "control")
        self.settle()
        self.assertEqual([p["text"] for p in self.sent("pane.send_text") if p["pane_id"] == "w2:p1"], ["/model gpt-5.6-luna", "/effort high"])
        self.assertEqual(len([p for p in self.sent("pane.send_keys") if p["pane_id"] == "w2:p1"]), 2)
        self.assertEqual(self.sent("agent.prompt"), [])
        self.assertEqual(self.team.rt(MEMBER).control_pending["action"], "model")

    def test_each_control_line_settles_before_enter(self):
        pauses = []
        self.d.control_gap_sleep = pauses.append
        result, _details = self.d._type_keystroke("w2:p1", "/quit")
        self.assertEqual(result, D.RESULT_LANDED_WORKING)
        self.assertEqual(pauses, [D.CONTROL_SUBMIT_GAP_S])
        calls = [(method, params) for method, params in self.api.calls if method in ("pane.send_text", "pane.send_keys")]
        self.assertEqual(calls[-2:], [
            ("pane.send_text", {"pane_id": "w2:p1", "text": "/quit"}),
            ("pane.send_keys", {"pane_id": "w2:p1", "keys": ["enter"]}),
        ])

    def observe(self, model):
        reading = context.Reading(used=5_000, window=200_000, source="/t.jsonl", model=model)
        with mock.patch.object(D._context, "read_member", return_value=reading), mock.patch.object(self.d, "_file_mtime", return_value=1.0):
            self.clock.advance(20)
            self.d.poll_context(self.team, self.clock() * 1000)

    def test_it_closes_when_the_transcript_reports_the_model(self):
        self.control(MEMBER, {"action": "model", "kind": "codex", "model": "gpt-5.6-luna", "effort": None, "keystrokes": ["/model gpt-5.6-luna"]})
        self.settle()
        rt = self.team.rt(MEMBER)
        self.assertIsNotNone(rt.control_pending)
        self.observe("gpt-5.6-luna")
        self.assertIsNone(rt.control_pending)
        self.assertNotIn(MEMBER, self.team.pending)
        applied = self.records("model_applied")
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["to"], [MEMBER, "all"])
        self.assertIn("gpt-5.6-luna", applied[0]["text"])

    def test_a_different_model_in_the_transcript_leaves_it_open(self):
        self.control(MEMBER, {"action": "model", "kind": "codex", "model": "gpt-5.6-luna", "effort": None, "keystrokes": ["/model gpt-5.6-luna"]})
        self.settle()
        self.observe("gpt-5.6-sol")
        self.assertIsNotNone(self.team.rt(MEMBER).control_pending)
        self.assertEqual(self.records("model_applied"), [])

    def test_an_effort_only_change_closes_on_typing(self):
        self.control(MEMBER, {"action": "model", "kind": "codex", "model": None, "effort": "high", "keystrokes": ["/effort high"]})
        self.settle()
        self.assertIsNone(self.team.rt(MEMBER).control_pending)
        self.assertEqual(len(self.records("model_applied")), 1)
        self.assertIn("not observable", self.records("model_applied")[0]["text"])

    def test_opencode_selects_an_exact_variant_and_then_queues_its_deferred_brief(self):
        set_member(self.ts, MEMBER, kind="opencode", effort="high", verified_kind=True)
        self.team.member(MEMBER)["kind"] = "opencode"
        self.team.member(MEMBER)["verified_kind"] = True
        live = fake_agent("w2:p1", "term_r1", "opencode", MEMBER)
        self.api.set_response("agent.get", {"type": "agent_info", "agent": live})
        self.api.set_response("agent.list", {"type": "agent_list", "agents": [live, dict(FAKE_AGENTS[1])]})
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w2:p1", "term_r1", "opencode"),
                                                                            fake_pane("w2:p2", "term_w1", "claude")]})
        self.d.agents["term_r1"] = live
        self.d.agents_by_pane["w2:p1"] = live
        self.api.set_response("agent.explain", {"type": "agent_explain", "explain": {
            "state": "idle", "matched_rule": {"id": "opencode.idle", "region": "after_last_prompt_marker"},
        }})
        self.control(MEMBER, {"action": "model", "kind": "opencode", "model": None, "effort": "high",
                              "setting": "opencode/glm-5.3-flash@high", "keystrokes": ["/variants", "high"],
                              "brief_after": True})
        self.settle()
        self.assertEqual([p["text"] for p in self.sent("pane.send_text") if p["pane_id"] == "w2:p1"], ["/variants", "high"], self.d.logged)
        self.assertEqual(self.team.pending[MEMBER].kind, "brief")
        applied = self.records("model_applied")[-1]
        self.assertIn("variant selected in OpenCode", applied["text"])

    def test_a_forged_opencode_picker_sequence_is_refused(self):
        set_member(self.ts, MEMBER, kind="opencode")
        self.team.member(MEMBER)["kind"] = "opencode"
        self.control(MEMBER, {"action": "model", "kind": "opencode", "model": None, "effort": "high",
                              "keystrokes": ["/variants", "not-high"]})
        self.assertNotIn(MEMBER, self.team.pending)
        self.assertIn("nothing to type", self.records("typed")[-1]["text"])


class RestartJobTests(ControlRig):
    ARGV = ["codex", "resume", "0199-reviewer", "-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"',
            "--dangerously-bypass-approvals-and-sandbox", "-c", "check_for_update_on_startup=false"]
    STARTED = {"type": "agent_started", "agent": fake_agent("w2:p1", "term_r1", "codex", MEMBER, launch_pending=False), "argv": ["codex"]}

    def setUp(self):
        super().setUp()
        set_member(self.ts, MEMBER, session=sess("0199-reviewer"))
        self.d.scan_teams(force=True)

    def request(self):
        return self.control(MEMBER, {"action": "restart", "kind": "codex", "model": "gpt-5.6-luna", "effort": "high", "exit": "/quit", "argv": self.ARGV})

    def pane_is_empty(self):
        self.api.set_response("pane.get", lambda p: {"pane": fake_pane(p["pane_id"], "term_r1", None)})

    def test_exit_then_start_with_the_resume_argv_then_the_session_report_closes_it(self):
        self.request()
        self.settle()
        self.assertEqual([p["text"] for p in self.sent("pane.send_text") if p["pane_id"] == "w2:p1"], ["/quit"])
        rt = self.team.rt(MEMBER)
        self.assertEqual(rt.restart["phase"], "exiting")
        # still up: nothing started yet
        self.d.advance_restarts(self.clock() * 1000)
        self.assertEqual([a for a in self.api.runs if a[:2] == ["agent", "start"]], [])
        # the pane emptied: the harness is started again with the resume argv after --
        self.pane_is_empty()
        self.api.set_cli_result(["agent", "start"], self.STARTED, request_id="cli:agent:start")
        self.api.set_response("agent.get", {
            "type": "agent_info",
            "agent": fake_agent("w2:p1", "term_r1", "codex", MEMBER, launch_pending=True),
        })
        self.clock.advance(2)
        self.d.advance_restarts(self.clock() * 1000)
        starts = [a for a in self.api.runs if a[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--") + 1:], self.ARGV[1:])
        self.assertIn(rt.restart["phase"], ("starting", "started"))
        # while it is on its way back it is never "missing"
        changes = []
        self.d._missing_update(self.team, MEMBER, self.team.member(MEMBER), False, changes)
        self.assertEqual(changes, [])
        # the same session id comes back with a new phase: a restart, not a compaction
        self.d._apply_changes(self.team, [(MEMBER, {"session": sess("0199-reviewer"), "briefed_at": None})])
        self.assertIsNone(rt.restart)
        self.assertIsNone(rt.control_pending)
        self.assertEqual(self.records("context_compacted"), [])
        applied = self.records("model_applied")
        self.assertEqual(len(applied), 1)
        self.assertTrue(applied[0].get("restarted"))
        self.assertIn("gpt-5.6-luna@high", applied[0]["text"])
        briefs = [store.read_json(p) for p in self.ts.team.jobs_dir.glob("*.json") if store.read_json(p).get("kind") == "brief"]
        self.assertEqual([j["member"] for j in briefs], [MEMBER])

    def test_an_agent_that_never_exits_fails_the_restart_and_is_left_alone(self):
        self.request()
        self.settle()
        self.clock.advance(D.RESTART_EXIT_S + 1)
        self.d.advance_restarts(self.clock() * 1000)
        rt = self.team.rt(MEMBER)
        self.assertIsNone(rt.restart)
        self.assertNotIn(MEMBER, self.team.pending)
        failed = self.records("restart_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("herdr-synapse resume {}".format(MEMBER), failed[0]["text"])
        self.assertEqual(self.team.member(MEMBER)["status"], "active")

    def test_a_failed_start_says_so(self):
        self.request()
        self.settle()
        self.pane_is_empty()
        self.api.set_cli_error(["agent", "start"], "agent_start_failed", "no such binary", request_id="cli:agent:start")
        self.clock.advance(2)
        self.d.advance_restarts(self.clock() * 1000)
        failed = self.records("restart_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("agent start failed", failed[0]["text"])
        self.assertIsNone(self.team.rt(MEMBER).restart)

    def test_a_ready_agent_with_the_same_session_completes_without_a_roster_delta(self):
        self.request()
        self.settle()
        self.pane_is_empty()
        self.api.set_cli_result(["agent", "start"], self.STARTED, request_id="cli:agent:start")
        returned = fake_agent(
            "w2:p1", "term_r1", "codex", MEMBER, launch_pending=False,
            interactive_ready=True, agent_session=sess("0199-reviewer"),
        )
        self.api.set_response("agent.get", {"type": "agent_info", "agent": returned})
        self.clock.advance(2)
        self.d.advance_restarts(self.clock() * 1000)
        rt = self.team.rt(MEMBER)
        self.assertIsNone(rt.restart)
        self.assertIsNone(rt.control_pending)
        self.assertNotIn(MEMBER, self.team.pending)
        self.assertEqual(len(self.records("model_applied")), 1)
        self.assertEqual(self.records("restart_failed"), [])

    def test_a_ready_agent_can_complete_before_the_integration_reports_its_session(self):
        self.request()
        self.settle()
        self.pane_is_empty()
        self.api.set_cli_result(["agent", "start"], self.STARTED, request_id="cli:agent:start")
        returned = fake_agent(
            "w2:p1", "term_r1", "codex", MEMBER, launch_pending=False,
            interactive_ready=True, agent_session=None,
        )
        self.api.set_response("agent.get", {"type": "agent_info", "agent": returned})
        self.clock.advance(2)
        self.d.advance_restarts(self.clock() * 1000)
        self.assertIsNone(self.team.rt(MEMBER).restart)
        self.assertEqual(len(self.records("model_applied")), 1)
        self.assertEqual(self.records("restart_failed"), [])

    def test_a_different_returned_session_does_not_complete_the_restart(self):
        self.request()
        self.settle()
        self.pane_is_empty()
        self.api.set_cli_result(["agent", "start"], self.STARTED, request_id="cli:agent:start")
        returned = fake_agent(
            "w2:p1", "term_r1", "codex", MEMBER, launch_pending=False,
            interactive_ready=True, agent_session=sess("some-other-session"),
        )
        self.api.set_response("agent.get", {"type": "agent_info", "agent": returned})
        self.clock.advance(2)
        self.d.advance_restarts(self.clock() * 1000)
        self.assertEqual(self.team.rt(MEMBER).restart["phase"], "started")
        self.assertEqual(self.records("model_applied"), [])

    def test_a_forged_restart_argv_is_refused_before_typing(self):
        self.control(MEMBER, {
            "action": "restart", "kind": "codex", "model": "gpt-5.6-luna", "effort": "high",
            "exit": "/quit", "argv": ["codex", "resume", "somebody-elses-session"],
        })
        self.assertNotIn(MEMBER, self.team.pending)
        self.assertIn("does not match its recorded session", self.records("typed")[-1]["text"])

    def test_a_restart_cannot_smuggle_an_unapproved_launch_flag(self):
        self.control(MEMBER, {
            "action": "restart", "kind": "codex", "model": "gpt-5.6-luna", "effort": "high",
            "exit": "/quit", "preserved": ["-c", 'mcp_servers.bad.command="steal"'],
            "argv": self.ARGV + ["-c", 'mcp_servers.bad.command="steal"'],
        })
        self.assertNotIn(MEMBER, self.team.pending)
        self.assertIn("does not match its recorded session", self.records("typed")[-1]["text"])

    def test_opencode_restart_defers_success_until_its_variant_is_selected(self):
        rt = self.team.rt(MEMBER)
        self.team.pending[MEMBER] = D.Pending(first_ms=self.clock() * 1000, kind="control")
        rt.control_pending = {"action": "restart", "requested_by": "human"}
        rt.restart = {"kind": "opencode", "model": "opencode/glm-5.3-flash", "effort": "high",
                      "after": ["/variants", "high"], "requested_by": "human"}
        self.d._note_restart_done(self.team, MEMBER, {"briefed_at": None}, self.clock() * 1000)
        followup = self.team.pending[MEMBER]
        self.assertEqual((followup.kind, followup.lines), ("control", ["/variants", "high"]))
        self.assertEqual(followup.control["setting"], "opencode/glm-5.3-flash@high")
        self.assertTrue(followup.control["restarted"])
        self.assertTrue(followup.control["brief_after"])
        self.assertEqual(self.records("model_applied"), [])


class DeliveryTests(ControlRig):
    def test_model_changed_nudges_the_member_it_names_and_nobody_else(self):
        rec = {"seq": 777, "from": "system", "from_kind": "system", "kind": "system", "event": "model_changed",
               "to": [MEMBER, "all"], "text": "x", "origin": {"via": "system", "verified": True}, "urgent": False}
        self.d._ingest_record(self.team, rec)
        self.assertEqual(self.team.pending[MEMBER].seqs, [777])
        self.assertNotIn(PEER, self.team.pending)


# --------------------------------------------------------------------------
# surfaces


class SurfaceTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_who_shows_the_setting_and_a_mismatching_observation(self):
        member = {"name": MEMBER, "role": "reviewer", "kind": "codex", "pane_id": "w2:p1", "status": "active", "agent_status": "idle",
                  "setting": "gpt-5.6-luna@high", "model_effective": "gpt-5.6-luna", "context": {"model": "gpt-5.6-sol", "percent": 10.0}}
        text = render.render_who({"teams": {"alpha": {"members": [member]}}, "charters": {}}, "alpha", ascii_only=True, brief=False, role=None)
        self.assertIn("model gpt-5.6-luna@high", text)
        self.assertIn("runs gpt-5.6-sol", text)
        member["context"]["model"] = "gpt-5.6-luna"
        text = render.render_who({"teams": {"alpha": {"members": [member]}}, "charters": {}}, "alpha", ascii_only=True, brief=False, role=None)
        self.assertNotIn("runs ", text)

    def test_me_and_the_briefing_name_the_members_own_setting(self):
        set_member(self.ts, MEMBER, model="gpt-5.6-luna", effort="high")
        code, payload, err = json_out(run_cli(["--json", "me"], self.ts.env_with(HERDR_PANE_ID="w2:p1"), live_api()))
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["setting"], payload["model_source"]), ("gpt-5.6-luna@high", "member"))
        doc = store.read_json(self.ts.team.team_json)
        member = next(m for m in doc["members"] if m["name"] == MEMBER)
        blob = cmd_hooks.brief_context(self.ts.team, "alpha", dict(member))
        self.assertIn("your model and effort: gpt-5.6-luna@high", blob)
        self.assertIn("model --self", blob)

    def test_console_and_picker_build_the_same_command(self):
        intent = tui_model.parse_input_line("/model alpha-reviewer gpt-5.6-luna@high --restart", "alpha")
        self.assertEqual(intent.kind, "model_set")
        self.assertEqual((intent.args["member"], intent.args["setting"], intent.args["restart"]), ("alpha-reviewer", "gpt-5.6-luna@high", True))
        self.assertEqual(tui_model.parse_input_line("/model alpha-reviewer", "alpha").kind, "error")
        pick = tui_model.Intent("member_model", {"team": "alpha", "member": "alpha-reviewer", "setting": "@high"})
        self.assertEqual(picker.action_args(pick), ["--team", "alpha", "model", "alpha-reviewer", "@high"])
        self.assertIn("model", [k for k, _l in tui_model.ACTION_OPTIONS])
        self.assertIn("/model", tui_model.SLASH_COMMANDS)
        self.assertIn("/model", tui_model.SLASH_USAGE)


class PickerCreateTests(unittest.TestCase):
    """The prefix+t team menu asks for the model and effort per agent, fourth after role, name, brief."""

    def walk_to_members(self, model):
        from test_tui_model import type_line

        codex = next(r for r in model.rows if r.kind == "codex" and not r.claimed_by)
        codex.selected = True
        claude = next(r for r in model.rows if r.kind == "claude" and not r.claimed_by)
        claude.selected = True
        tui_model.picker_apply_key(model, "ENTER")          # two agents selected: on to the team name
        self.assertEqual(model.stage, "name")
        type_line(model, "hunt")
        tui_model.picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "charter")
        type_line(model, "Find the bug.")
        tui_model.picker_apply_key(model, "ENTER")
        tui_model.picker_apply_key(model, "ENTER")          # an empty line finishes the charter
        self.assertEqual(model.stage, "rules")
        tui_model.picker_apply_key(model, "TAB")            # no team rules
        self.assertEqual(model.stage, "project")
        tui_model.picker_apply_key(model, "TAB")            # no folder
        self.assertEqual(model.stage, "members")

    def test_the_fourth_prompt_records_a_setting_and_enter_keeps_the_default(self):
        from test_tui_model import picker_model, type_line

        model = picker_model([], focused=None)
        self.walk_to_members(model)
        tui_model.picker_apply_key(model, "ENTER")          # role
        tui_model.picker_apply_key(model, "ENTER")          # name
        type_line(model, "Review this work.")
        tui_model.picker_apply_key(model, "ENTER")          # Mission / brief
        self.assertEqual(model.member_field, "model")
        self.assertIn("model@effort", "\n".join(tui_model.picker_lines(model, 100, 24)))
        type_line(model, "@high")
        tui_model.picker_apply_key(model, "ENTER")
        # the first row keeps @high; every further row keeps the default
        while model.stage == "members":
            if model.member_field == "brief":
                type_line(model, "Own this workstream.")
            tui_model.picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "confirm")
        intent = tui_model.picker_apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "create")
        first = intent.args["members"][0]
        self.assertEqual(first["setting"], "@high")
        self.assertTrue(all(m["setting"] is None for m in intent.args["members"][1:]))
        argv = picker.create_args(intent.args)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "{}=@high".format(first["name"]))
        self.assertEqual(argv.count("--model"), 1)
        self.assertEqual(picker.add_args(dict(intent.args, mode="add"), first)[-2:], ["--model", "@high"])

    def test_an_unknown_effort_for_the_rows_kind_is_refused_at_the_prompt(self):
        from test_tui_model import picker_model, type_line

        model = picker_model([], focused=None)
        self.walk_to_members(model)
        tui_model.picker_apply_key(model, "ENTER")
        tui_model.picker_apply_key(model, "ENTER")
        type_line(model, "Review this work.")
        tui_model.picker_apply_key(model, "ENTER")
        row = tui_model.selected_rows(model)[0]
        bad = "@max" if row.kind == "codex" else "@minimal"
        type_line(model, bad)
        tui_model.picker_apply_key(model, "ENTER")
        self.assertEqual(model.member_field, "model")
        self.assertIn("does not know the effort", model.error or "")


class CreateLiveMemberSettingTests(unittest.TestCase):
    """``create --member … --model name=…`` records the setting on a live agent (the picker's path)."""

    def test_it_is_recorded_and_a_wrong_name_is_a_usage_error(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            code, payload, err = json_out(run_cli(["--json", "create", "beta", "--member", "w5:p1:reviewer", "--brief", "reviewer=Review every patch.", "--model", "reviewer=@high"], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            row = next(m for m in store.read_json(ts.session.team("beta").team_json)["members"] if m["role"] == "reviewer")
            self.assertEqual((row.get("model"), row.get("effort")), (None, "high"))
            code, _payload, err = json_out(run_cli(["--json", "create", "gamma", "--member", "w5:p2:worker", "--model", "nobody=@high"], env_no_daemon(ts), api))
            self.assertEqual((code, err["code"]), (2, "usage"))
            self.assertFalse(ts.session.team("gamma").team_json.exists())


if __name__ == "__main__":
    unittest.main()

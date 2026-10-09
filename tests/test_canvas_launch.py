"""Launch-time canvas MCP injection (0.21, ``.local/prd/canvas-contracts.md`` section 12).

Every path Synapse starts a member by (create --spawn, resume, restore,
swap) must give the same ``--mcp-config`` / ``-c mcp_servers.synapse_canvas.*``
flags for a kind while the team's canvas is on and none while it is off; a
controlled restart must carry exactly those flags through
``models.preserved_launch_args``, so the notifier's argv re-check still
matches, and nothing a user passed with the same flags.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import TempState, fake_agent, whiteboard_on
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
import test_models as rigs

from herdr_team import cmd_models, cmd_restore, cmd_roster, features, models, roster, store, swap

MCP_KINDS = ("claude", "codex")
#: Spawned by the four-path tests but given no MCP flag: Pi has no --mcp-config and exits on an unknown flag.
CLI_ONLY_KINDS = ("pi",)


def spec_of(ts, team="alpha"):
    return features.mcp_spec(ts.layout, ts.session.team(team))


def contains(argv, part):
    """Whether ``part`` occurs in ``argv`` as a contiguous run, exactly once."""
    n = len(part)
    hits = [i for i in range(len(argv) - n + 1) if argv[i:i + n] == part]
    return len(hits) == 1


def mcp_tokens(kind, argv):
    """The tokens of ``argv`` that are Synapse canvas flags (the same test ``preserved_launch_args`` applies)."""
    out = []
    flags = features.PRESERVED_MCP_FLAGS.get(kind, ())
    for i, token in enumerate(argv):
        if token in flags and i + 1 < len(argv) and features.is_preserved_mcp_value(kind, token, argv[i + 1]):
            out += [token, argv[i + 1]]
    return out


def fill_layout(params):
    """``layout_apply``: the request tree echoed with ``w9:p<n>`` on every leaf."""
    counter = [0]

    def fill(node):
        if node.get("type") == "pane":
            counter[0] += 1
            return dict(node, pane_id="w9:p{}".format(counter[0]))
        return dict(node, first=fill(node["first"]), second=fill(node["second"]))

    return {"type": "layout_apply", "layout": {"workspace_id": "w9", "tab_id": "w9:t1", "zoomed": False, "focused_pane_id": "w9:p1",
                                               "root": fill(params["root"])}}


class LaunchArgsTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        self.spec = spec_of(self.ts)

    def test_the_spec_names_this_checkout_the_socket_and_the_team_dir(self):
        self.assertEqual(self.spec["server"], "synapse_canvas")
        self.assertTrue(self.spec["command"].endswith(os.path.join("bin", "herdr-synapse")))
        self.assertEqual(self.spec["args"][-2:], ["canvas", "mcp"])
        self.assertEqual(self.spec["args"][self.spec["args"].index("--team") + 1], os.path.abspath(os.fspath(self.ts.team.root)))
        doc = store.read_json(features.mcp_config_path(self.ts.team))
        self.assertEqual(doc["mcpServers"]["synapse_canvas"]["args"], self.spec["args"])

    def test_the_flags_come_last_per_kind_and_only_for_kinds_with_a_launch_flag(self):
        claude = models.launch_args("claude", "opus", "high", mcp=self.spec)
        self.assertEqual(claude, ["--model", "opus", "--effort", "high", "--dangerously-skip-permissions", "--mcp-config", self.spec["config_file"]])
        pi = models.launch_args("pi", None, None, mcp=self.spec)
        self.assertEqual(pi, models.launch_args("pi", None, None), "Pi exits on an unknown flag, so it gets none")
        codex = models.launch_args("codex", "gpt-5.6-luna", "high", mcp=self.spec)
        self.assertEqual(codex[-4:], features.mcp_launch_args("codex", self.spec))
        self.assertEqual(codex[-4], "-c")
        self.assertTrue(codex[-3].startswith("mcp_servers.synapse_canvas.command="))
        self.assertEqual(json.loads(codex[-1].split("=", 1)[1]), self.spec["args"])
        for kind in ("opencode", "cursor", "gemini"):
            with self.subTest(kind=kind):
                self.assertEqual(models.launch_args(kind, None, None, mcp=self.spec), models.launch_args(kind, None, None))

    def test_no_spec_means_no_flags_on_any_builder(self):
        session = rigs.sess("abc", "herdr:claude", "claude")
        for argv in (models.launch_args("claude", None, None), models.resume_argv("claude", session, None, None),
                     models.fresh_argv("claude", None, None), models.restart_argv("claude", session, None, None, ["claude"])):
            self.assertNotIn("--mcp-config", argv)

    def test_fresh_argv_never_doubles_the_flags_it_also_finds_live(self):
        live = ["claude", "--mcp-config", self.spec["config_file"], "--settings", "/x/s.json"]
        argv = models.fresh_argv("claude", None, None, live, mcp=self.spec)
        self.assertTrue(contains(argv, ["--mcp-config", self.spec["config_file"]]), argv)
        self.assertIn("--settings", argv)


class FourPathsTests(unittest.TestCase):
    """create --spawn, resume, restore and swap give one kind the same canvas flags."""

    def setUp(self):
        self.ts = TempState(write_team=False)
        self.addCleanup(self.ts.cleanup)
        clock = [0.0]

        def monotonic():
            clock[0] += 30.0
            return clock[0]

        self.addCleanup(setattr, cmd_roster, "_sleep", cmd_roster._sleep)
        self.addCleanup(setattr, cmd_roster, "_monotonic", cmd_roster._monotonic)
        cmd_roster._sleep = lambda _s: None
        cmd_roster._monotonic = monotonic
        mock.patch("shutil.which", return_value="/bin/true").start()
        self.addCleanup(mock.patch.stopall)

    def spawn(self, canvas=True):
        api = live_api()
        api.set_response("layout.apply", fill_layout)
        api.set_cli_result(["agent", "start"], {"type": "agent_started", "agent": fake_agent("w9:p1", "term_new", "codex", "delta-reviewer", launch_pending=False),
                                                "argv": ["codex"]}, request_id="cli:agent:start")
        code, _payload, err = json_out(run_cli([
            "--json", "create", "delta", "--leader", "role:reviewer", "--new", "--workspace", "w9"] + (["--canvas"] if canvas else []) + [

            "--spawn", "reviewer:codex", "--spawn", "worker:claude", "--spawn", "scout:pi",
            "--brief", "reviewer=Review.", "--brief", "worker=Build.", "--brief", "scout=Scout.",
        ], env_no_daemon(self.ts), api))
        self.assertEqual(code, 0, err)
        starts = [argv for argv in api.runs if argv[:2] == ["agent", "start"]]
        return {argv[argv.index("--kind") + 1]: argv[argv.index("--") + 1:] for argv in starts}

    def delta(self):
        """The spawned team, with a recorded conversation and a real directory per member (resume, restore and swap need both)."""
        paths = self.ts.session.team("delta")
        doc = store.read_json(paths.team_json)
        sessions = {"codex": rigs.sess("c-1"), "claude": rigs.sess("k-1", "herdr:claude", "claude"),
                    "pi": {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/tmp/pi-session.jsonl"}}
        for member in doc["members"]:
            if member.get("kind") in sessions:
                member.update(session=sessions[member["kind"]], cwd=str(self.ts.home), status="missing")
        store.write_json(paths.team_json, doc)
        return paths, roster.load_team(paths)

    def test_a_team_created_without_canvas_starts_without_the_tools(self):
        # A canvas is per team (2026-09-27): the layer alone gives a new team none, --canvas does.
        features.set_layer(self.ts.session, True, "human", "cli")
        argv = self.spawn(canvas=False)
        self.assertEqual([mcp_tokens(kind, args) for kind, args in sorted(argv.items())], [[], [], []])
        paths = self.ts.session.team("delta")
        self.assertFalse(features.team_switch(self.ts.session, paths).on)

    def test_every_path_gives_the_same_flags_per_kind_while_the_canvas_is_on(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        spawned = self.spawn()
        paths, team = self.delta()
        spec = features.mcp_spec(self.ts.layout, paths)
        self.assertIsNotNone(spec)
        by_kind = {m.kind: m for m in team.agents()}
        resumed = {kind: cmd_roster.resume_plan("delta", by_kind[kind], team.config, mcp=spec)["argv"] for kind in MCP_KINDS}
        restored = {i["kind"]: i["argv"] for i in cmd_restore.plan_restore(team, [], [], env_no_daemon(self.ts), mcp=spec) if i.get("argv")}
        swapped = {kind: swap.plan(team, by_kind["claude"].name, kind, None, env_no_daemon(self.ts))["argv"] for kind in MCP_KINDS}
        for kind in MCP_KINDS:
            expected = features.mcp_launch_args(kind, spec)
            with self.subTest(kind=kind):
                self.assertTrue(expected)
                for path, argv in (("spawn", spawned[kind]), ("resume", resumed[kind]), ("restore", restored[kind]), ("swap", swapped[kind])):
                    self.assertTrue(contains(argv, expected), (path, argv))
                    self.assertEqual(mcp_tokens(kind, argv), expected, path)
        pi_resumed = cmd_roster.resume_plan("delta", by_kind["pi"], team.config, mcp=spec)["argv"]
        pi_swapped = swap.plan(team, by_kind["claude"].name, "pi", None, env_no_daemon(self.ts))["argv"]
        for path, argv in (("spawn", spawned["pi"]), ("resume", pi_resumed), ("restore", restored["pi"]), ("swap", pi_swapped)):
            self.assertNotIn("--mcp-config", argv, (path, argv))
        # Claude's --mcp-config is variadic; the native --name that follows on a spawn ends it.
        self.assertEqual(spawned["claude"][-4:], ["--mcp-config", spec["config_file"], "--name", "delta-worker"])

    def test_the_cli_resume_and_restore_commands_pass_the_spec(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.spawn()
        paths, _team = self.delta()
        spec = features.mcp_spec(self.ts.layout, paths)
        code, out, err = json_out(run_cli(["--json", "--team", "delta", "resume", "delta-worker", "--print"], env_no_daemon(self.ts), live_api()))
        self.assertEqual(code, 0, err)
        self.assertTrue(contains(out["argv"], features.mcp_launch_args("claude", spec)), out["argv"])
        api = live_api([])
        api.set_response("pane.list", {"type": "pane_list", "panes": []})
        code, out, err = json_out(run_cli(["--json", "--team", "delta", "restore", "delta", "--dry-run"], env_no_daemon(self.ts), api))
        self.assertEqual(code, 0, err)
        planned = {m["kind"]: m["argv"] for m in out["members"] if m.get("argv")}
        for kind in MCP_KINDS:
            self.assertTrue(contains(planned[kind], features.mcp_launch_args(kind, spec)), (kind, planned[kind]))

    def test_no_flags_while_the_layer_is_off_or_the_team_is_off(self):
        spawned = self.spawn()
        for kind, argv in spawned.items():
            self.assertEqual(mcp_tokens(kind, argv), [], kind)
            self.assertNotIn("--mcp-config", argv)
        paths, team = self.delta()
        self.assertIsNone(features.mcp_spec(self.ts.layout, paths))
        self.assertFalse(features.mcp_config_path(paths).exists(), "nothing is written while off")
        features.set_layer(self.ts.session, True, "human", "cli")
        features.set_team(paths, enabled=False)
        self.assertIsNone(features.mcp_spec(self.ts.layout, paths))
        code, out, err = json_out(run_cli(["--json", "--team", "delta", "resume", "delta-worker", "--print"], env_no_daemon(self.ts), live_api()))
        self.assertEqual(code, 0, err)
        self.assertNotIn("--mcp-config", out["argv"])
        by_kind = {m.kind: m for m in team.agents()}
        self.assertEqual(mcp_tokens("codex", swap.plan(team, by_kind["claude"].name, "codex", None, env_no_daemon(self.ts))["argv"]), [])

    def test_live_visuals_off_keeps_the_tools(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.spawn()
        paths, _team = self.delta()
        features.set_team(paths, viz=False)
        self.assertIsNotNone(features.mcp_spec(self.ts.layout, paths), "viz off is a canvas refusal, not a missing server")


class SwapLaunchTests(unittest.TestCase):
    """The whole swap command, prepare and execute included, starts the replacement with the flags."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.name = "alpha-worker"
        self.ts.members[1].update(cwd=str(self.ts.home), session={"source": "herdr:claude", "kind": "id", "value": "old-session"})
        self.ts.write_team_json()
        self.api = live_api([fake_agent("w2:p2", "term_w1", "claude", self.name, status="blocked", cwd=str(self.ts.home)),
                             fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer")])
        self.tabs = []
        self.api.set_response("pane.list", lambda p: {"panes": list(self.api.rows)})
        self.api.set_response("tab.list", lambda p: {"tabs": self.tabs})
        self.api.set_response("layout.apply", self.layout)
        self.api.set_response("pane.close", self.close)
        self.api.set_response("pane.get", self.get_pane)
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
            from herdr_team.errors import HerdrTeamError

            raise HerdrTeamError("pane_not_found", "gone", 1)
        return {"pane": pane}

    def launch(self, api, name, kind, pane_id, args=()):
        row = next(r for r in api.rows if r["pane_id"] == pane_id)
        row.update(name=name, agent=kind, agent_status="idle")
        return {"terminal_id": row["terminal_id"]}

    def test_a_swap_to_codex_starts_it_with_the_overrides(self):
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "swap", self.name, "--to", "codex"], env_no_daemon(self.ts), self.api))
        self.assertEqual(code, 0, (out, err))
        args = list(self.start.call_args.kwargs["args"])
        self.assertTrue(contains(args, features.mcp_launch_args("codex", spec_of(self.ts))), args)

    def test_a_dry_run_shows_the_flags_and_a_swap_while_off_has_none(self):
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "swap", self.name, "--to", "claude", "--dry-run"], env_no_daemon(self.ts), self.api))
        self.assertEqual(code, 0, err)
        self.assertTrue(contains(out["argv"], features.mcp_launch_args("claude", spec_of(self.ts))), out["argv"])
        features.set_layer(self.ts.session, False, "human", "cli")
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "swap", self.name, "--to", "claude"], env_no_daemon(self.ts), self.api))
        self.assertEqual(code, 0, (out, err))
        self.assertNotIn("--mcp-config", list(self.start.call_args.kwargs["args"]))


class RestartCarriesTheFlagsTests(unittest.TestCase):
    """A controlled restart keeps exactly the injected flags, and nothing a user passed with the same flag names."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        self.spec = spec_of(self.ts)
        self.session = {"claude": rigs.sess("k-1", "herdr:claude", "claude"), "codex": rigs.sess("c-1"),
                        "pi": {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/tmp/pi-session.jsonl"}}

    def live(self, kind, extra=()):
        """What a Synapse-started member's process argv looks like: its resume argv, the canvas flags, then anything else."""
        argv = models.resume_argv(kind, self.session[kind], None, None, mcp=self.spec)
        if kind in ("claude", "pi"):
            argv = argv + ["--name", "alpha-x"]
        return argv + list(extra)

    def test_preserved_is_exactly_the_injected_flags_and_the_restart_ends_with_them(self):
        for kind in MCP_KINDS:
            with self.subTest(kind=kind):
                injected = features.mcp_launch_args(kind, self.spec)
                preserved = models.preserved_launch_args(kind, self.live(kind))
                self.assertEqual(preserved, injected)
                restart = models.restart_argv(kind, self.session[kind], None, None, self.live(kind))
                self.assertEqual(restart[-len(injected):], injected)
                self.assertEqual(mcp_tokens(kind, restart), injected)
                # the notifier rebuilds the expected control from the recorded preserved flags: same function, same answer
                self.assertEqual(models.preserved_launch_args(kind, [kind] + preserved), preserved)
                self.assertEqual(models.restart_argv(kind, self.session[kind], None, None, [kind] + preserved), restart)

    def test_the_restart_control_block_records_them(self):
        control = cmd_models.restart_control("codex", self.session["codex"], "gpt-5.6-luna", "high", None, self.live("codex"), "yolo")
        self.assertEqual(control["preserved"], features.mcp_launch_args("codex", self.spec))
        self.assertTrue(contains(control["argv"], features.mcp_launch_args("codex", self.spec)))

    def test_the_equals_spelling_is_read_too(self):
        live = ["claude", "--resume", "k-1", "--mcp-config=" + self.spec["config_file"], "--name", "x"]
        self.assertEqual(models.preserved_launch_args("claude", live), ["--mcp-config=" + self.spec["config_file"]])

    def test_a_users_own_mcp_config_or_override_is_not_preserved(self):
        inline = json.dumps({"mcpServers": {"x": {"command": "evil", "env": {"TOKEN": "sk-secret"}}}})
        claude = ["claude", "--resume", "k-1", "--mcp-config", inline, "--mcp-config", "/home/u/servers.json", "--settings", "/x/s.json"]
        self.assertEqual(models.preserved_launch_args("claude", claude), ["--settings", "/x/s.json"])
        self.assertEqual(models.preserved_launch_args("pi", ["pi", "--mcp-config", "relative/whiteboard/mcp.json", "--offline"]), ["--offline"])
        codex = ["codex", "resume", "c-1", "-c", 'model_reasoning_effort="high"', "-c", 'mcp_servers.other.command="x"',
                 "-c", "mcp_servers.synapse_canvas.env.TOKEN=\"t\"", "-c", "check_for_update_on_startup=false", "--search"]
        self.assertEqual(models.preserved_launch_args("codex", codex), ["--search"])
        self.assertEqual(models.preserved_launch_args("opencode", ["opencode", "--mcp-config", self.spec["config_file"], "--pure"]), ["--pure"])
        # a flag left without its value never swallows the next flag
        self.assertEqual(models.preserved_launch_args("claude", ["claude", "--mcp-config", "--settings", "/x/s.json"]), ["--settings", "/x/s.json"])


class NotifierRecheckTests(rigs.ControlRig):
    """The daemon rebuilds a restart's argv from its recorded fields; the carried canvas flags must pass that check."""

    def setUp(self):
        super().setUp()
        rigs.set_member(self.ts, rigs.MEMBER, session=rigs.sess("0199-reviewer"))
        self.d.scan_teams(force=True)
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        self.spec = spec_of(self.ts)

    def restart(self, preserved, argv):
        return self.control(rigs.MEMBER, {"action": "restart", "kind": "codex", "model": "gpt-5.6-luna", "effort": "high", "exit": "/quit",
                                          "argv": argv, "preserved": preserved, "permissions": "yolo"})

    def test_a_restart_carrying_the_canvas_flags_is_accepted(self):
        live = models.resume_argv("codex", rigs.sess("0199-reviewer"), None, None, mcp=self.spec)
        control = cmd_models.restart_control("codex", rigs.sess("0199-reviewer"), "gpt-5.6-luna", "high", None, live, "yolo")
        self.assertEqual(control["preserved"], features.mcp_launch_args("codex", self.spec))
        self.restart(control["preserved"], control["argv"])
        self.assertIn(rigs.MEMBER, self.team.pending, [r.get("text") for r in self.records("typed")])
        self.assertEqual(self.team.pending[rigs.MEMBER].control["argv"], control["argv"])

    def test_one_foreign_override_beside_them_is_refused(self):
        injected = features.mcp_launch_args("codex", self.spec)
        forged = injected + ["-c", 'mcp_servers.synapse_canvas.env.X="y"']
        argv = models.restart_argv("codex", rigs.sess("0199-reviewer"), "gpt-5.6-luna", "high", ["codex"]) + forged
        self.restart(forged, argv)
        self.assertNotIn(rigs.MEMBER, self.team.pending)
        self.assertIn("does not match its recorded session", self.records("typed")[-1]["text"])


if __name__ == "__main__":
    unittest.main()

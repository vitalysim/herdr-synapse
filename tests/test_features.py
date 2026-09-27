"""The visual layer's switches and the launch-time MCP contract (``herdr_team.features``, 0.21)."""
from __future__ import annotations

import json
import os
import stat
import unittest

from herdr_team import features as F
from herdr_team import store
from herdr_team.errors import HerdrTeamError
from herdr_team.identity import Author

from support import TempState, whiteboard_on


def operator() -> Author:
    return Author("human", "human", "console", True)


def unverified_human() -> Author:
    return Author("human", "human", "cli-unverified", False, reason="not the foreground job")


def member(delegate: bool = False) -> Author:
    return Author("alpha-worker", "claude", "cli", True, team="alpha", operator=delegate)


class LayerSwitchTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.session = self.ts.session

    def test_the_layer_is_off_by_default(self):
        self.assertFalse(F.layer_enabled(self.session))
        self.assertFalse(F.features_path(self.session).exists())
        self.assertEqual(F.load(self.session)["whiteboard"], {"enabled": False, "by": None, "via": None, "at": None})

    def test_enable_then_disable_records_who_and_when(self):
        result = F.set_layer(self.session, True, "human", "console")
        self.assertTrue(result["changed"])
        self.assertFalse(result["previous"])
        self.assertTrue(F.layer_enabled(self.session))
        doc = store.read_json(F.features_path(self.session))
        self.assertEqual(doc["v"], 1)
        self.assertEqual(doc["whiteboard"]["by"], "human")
        self.assertEqual(doc["whiteboard"]["via"], "console")
        self.assertTrue(doc["whiteboard"]["at"])
        self.assertEqual(stat.S_IMODE(os.stat(F.features_path(self.session)).st_mode), 0o600)
        again = F.set_layer(self.session, True, "human", "console")
        self.assertFalse(again["changed"], "a no-op flip writes nothing")
        off = F.set_layer(self.session, False, "human", "cli")
        self.assertTrue(off["changed"])
        self.assertFalse(F.layer_enabled(self.session))

    def test_only_a_literal_true_enables_the_layer(self):
        store.write_json(F.features_path(self.session), {"v": 1, "whiteboard": {"enabled": "yes"}, "other": 1})
        self.assertFalse(F.layer_enabled(self.session))
        F.set_layer(self.session, True, "human", "cli")
        self.assertEqual(store.read_json(F.features_path(self.session))["other"], 1, "unknown keys survive a write")

    def test_require_layer(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            F.require_layer(self.session)
        self.assertEqual(ctx.exception.code, "whiteboard_off")
        self.assertEqual(ctx.exception.details["scope"], "session")
        self.assertIn("whiteboard enable", ctx.exception.message)
        F.set_layer(self.session, True, "human", "cli")
        F.require_layer(self.session)


class TeamSwitchTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.session = self.ts.session
        self.team = self.ts.team

    def test_a_teams_canvas_is_off_until_turned_on_for_that_team(self):
        # The owner's call on 2026-09-27: the whiteboard is per team, not every team at once.
        switch = F.team_switch(self.session, self.team)
        self.assertEqual((switch.layer, switch.enabled, switch.viz_enabled), (False, False, True))
        F.set_layer(self.session, True, "human", "cli")
        switch = F.team_switch(self.session, self.team)
        self.assertFalse(switch.on, "the layer alone turns no team's canvas on")
        self.assertFalse(switch.viz)
        F.set_team(self.team, enabled=True)
        switch = F.team_switch(self.session, self.team)
        self.assertTrue(switch.on)
        self.assertTrue(switch.viz, "live visuals are on inside a team whose canvas is on")
        self.assertEqual(switch.to_json(), {"team": "alpha", "layer": True, "enabled": True, "viz_enabled": True, "on": True, "viz": True})
        doc = {"config": {"whiteboard": {"enabled": "yes"}}}
        self.assertFalse(F.team_settings(doc)["enabled"], "only a literal true turns a canvas on")

    def test_set_team_writes_team_json_config_and_bumps_the_revision(self):
        before_rev = store.RosterStore(self.team).current_revision()
        result = F.set_team(self.team, enabled=True, by="human", via="console")
        self.assertEqual(result["changed"], ["enabled"])
        self.assertFalse(result["before"]["enabled"])
        self.assertTrue(result["after"]["enabled"])
        doc = store.RosterStore(self.team).load()
        self.assertEqual(doc["config"]["whiteboard"]["enabled"], True)
        self.assertEqual(doc["config"]["whiteboard"]["by"], "human")
        self.assertEqual(doc["revision"], before_rev + 1)
        self.assertEqual(doc["members"][0]["name"], "alpha-reviewer", "the roster is untouched")

    def test_set_team_is_a_no_op_when_nothing_changes(self):
        rev = store.RosterStore(self.team).current_revision()
        result = F.set_team(self.team, enabled=False, viz=True)
        self.assertEqual(result["changed"], [])
        self.assertEqual(store.RosterStore(self.team).current_revision(), rev)

    def test_viz_opt_out_is_independent_of_the_canvas(self):
        F.set_layer(self.session, True, "human", "cli")
        F.set_team(self.team, enabled=True)
        F.set_team(self.team, viz=False)
        switch = F.team_switch(self.session, self.team)
        self.assertTrue(switch.on)
        self.assertFalse(switch.viz)
        F.set_team(self.team, enabled=False)
        switch = F.team_switch(self.session, self.team)
        self.assertFalse(switch.on)
        self.assertFalse(switch.viz)
        self.assertEqual(F.team_settings(store.RosterStore(self.team).load())["viz"], False, "the viz choice is kept while the team is off")

    def test_require_on_names_the_scope(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            F.require_on(self.session, self.team)
        self.assertEqual((ctx.exception.code, ctx.exception.details["scope"]), ("whiteboard_off", "session"))
        F.set_layer(self.session, True, "human", "cli")
        F.set_team(self.team, enabled=False)
        with self.assertRaises(HerdrTeamError) as ctx:
            F.require_on(self.session, self.team)
        self.assertEqual((ctx.exception.code, ctx.exception.details["scope"]), ("whiteboard_off", "team"))
        self.assertIn("whiteboard team on", ctx.exception.message)
        F.set_team(self.team, enabled=True)
        self.assertTrue(F.require_on(self.session, self.team).on)

    def test_require_viz(self):
        F.set_layer(self.session, True, "human", "cli")
        F.set_team(self.team, enabled=True)
        self.assertTrue(F.require_viz(self.session, self.team).viz)
        F.set_team(self.team, viz=False)
        with self.assertRaises(HerdrTeamError) as ctx:
            F.require_viz(self.session, self.team)
        self.assertEqual(ctx.exception.code, "viz_off")
        F.set_team(self.team, enabled=False)
        with self.assertRaises(HerdrTeamError) as ctx:
            F.require_viz(self.session, self.team)
        self.assertEqual(ctx.exception.code, "whiteboard_off", "a team that is off says so before viz")

    def test_a_missing_team_is_team_not_found(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            F.team_switch(self.session, self.session.team("ghost"))
        self.assertEqual(ctx.exception.code, "team_not_found")

    def test_status_covers_every_team(self):
        other = self.session.team("beta")
        from herdr_team.paths import ensure_team_dirs

        ensure_team_dirs(other)
        store.write_json(other.team_json, {"schema": 1, "team": "beta", "revision": 1, "members": [], "config": {"whiteboard": {"enabled": False}}})
        self.assertEqual(F.status(self.session)["teams"]["alpha"]["on"], False)
        F.set_layer(self.session, True, "human", "cli")
        doc = F.status(self.session)
        self.assertTrue(doc["session"]["enabled"])
        self.assertEqual(doc["teams"]["alpha"], {"enabled": False, "viz": True, "on": False, "viz_on": False, "by": None, "at": None})
        F.set_team(self.team, enabled=True, by="human", via="cli")
        alpha = F.status(self.session)["teams"]["alpha"]
        self.assertEqual((alpha["enabled"], alpha["on"], alpha["viz_on"], alpha["by"]), (True, True, True, "human"))
        self.assertEqual((doc["teams"]["beta"]["on"], doc["teams"]["beta"]["viz_on"]), (False, False))
        self.assertEqual(list(F.status(self.session, ["beta", "ghost"])["teams"]), ["beta"])

    def test_me_line(self):
        off = F.TeamSwitch("alpha", False, True, True)
        self.assertEqual(F.me_line(off), "whiteboard: off")
        self.assertEqual(F.me_line(F.TeamSwitch("alpha", True, False, True)), "whiteboard: off for this team")
        line = F.me_line(F.TeamSwitch("alpha", True, True, False), version=42)
        self.assertTrue(line.startswith("whiteboard: on · live visuals off · canvas v42"), line)
        self.assertIn("skill get --reference canvas", line)


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_the_layer_is_for_the_operator_in_person(self):
        self.assertEqual(F.check_authority(operator(), F.LEVEL_LAYER), "operator")
        for author in (member(), member(delegate=True), unverified_human()):
            with self.assertRaises(HerdrTeamError) as ctx:
                F.check_authority(author, F.LEVEL_LAYER)
            self.assertEqual(ctx.exception.code, "author_mismatch")

    def test_team_switches_accept_a_delegate(self):
        self.assertEqual(F.check_authority(operator(), F.LEVEL_TEAM), "operator")
        self.assertEqual(F.check_authority(member(delegate=True), F.LEVEL_TEAM), "delegate")
        with self.assertRaises(HerdrTeamError) as ctx:
            F.check_authority(member(), F.LEVEL_TEAM)
        self.assertIn("post a request", ctx.exception.message)
        with self.assertRaises(HerdrTeamError) as ctx:
            F.check_authority(unverified_human(), F.LEVEL_TEAM)
        self.assertIn("could not be verified", ctx.exception.message)

    def test_refusals_are_audited_when_a_layout_is_given(self):
        from herdr_team.identity import read_audit

        with self.assertRaises(HerdrTeamError):
            F.check_authority(member(), F.LEVEL_TEAM, self.ts.layout, "alpha")
        events = read_audit(self.ts.layout, "alpha")
        self.assertEqual(events[-1]["event"], "author_mismatch")


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_state_notice_texts(self):
        off = F.TeamSwitch("alpha", False, True, True)
        on = F.TeamSwitch("alpha", True, True, True)
        on_no_viz = F.TeamSwitch("alpha", True, True, False)
        self.assertIn("the whiteboard is on for team alpha", F.state_notice(off, on))
        self.assertIn("Live visuals (viz) are on.", F.state_notice(off, on))
        self.assertIn("Live visuals (viz) are off.", F.state_notice(off, on_no_viz))
        self.assertIn("the whiteboard is off for team alpha", F.state_notice(on, off))
        self.assertEqual(F.state_notice(on, on_no_viz), "live visuals (viz) are now off for team alpha")
        self.assertIsNone(F.state_notice(off, F.TeamSwitch("alpha", False, True, False)), "agents see nothing while the layer is off")
        self.assertIsNone(F.state_notice(on, on))

    def test_announce_posts_one_whiteboard_state_record(self):
        before = F.TeamSwitch("alpha", False, True, True)
        after = F.TeamSwitch("alpha", True, True, True)
        seq = F.announce(self.ts.layout, self.ts.team, before, after, "human")
        self.assertIsInstance(seq, int)
        record = store.BoardStore(self.ts.team).get(seq)
        self.assertEqual((record["from"], record["kind"], record["event"], record["to"]), ("system", "system", "whiteboard_state", ["all"]))
        self.assertEqual(record["whiteboard"]["after"]["on"], True)
        self.assertIsNone(F.announce(self.ts.layout, self.ts.team, after, after, "human"))

    def test_announce_layer_tells_only_teams_whose_state_changed(self):
        from herdr_team.paths import ensure_team_dirs

        other = self.ts.session.team("beta")
        ensure_team_dirs(other)
        store.write_json(other.team_json, {"schema": 1, "team": "beta", "revision": 1, "members": [], "config": {"whiteboard": {"enabled": False}}})
        self.assertEqual(F.announce_layer(self.ts.layout, False, True, "human"), {}, "no team's canvas was turned on, so none changed")
        F.set_team(self.ts.team, enabled=True)
        posted = F.announce_layer(self.ts.layout, False, True, "human")
        self.assertEqual(list(posted), ["alpha"], "only the team whose canvas was turned on is told")
        self.assertEqual(F.announce_layer(self.ts.layout, True, True, "human"), {})


class McpLaunchTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout

    def enable(self):
        whiteboard_on(self.ts.session, self.ts.team)

    def test_no_spec_while_the_layer_or_the_team_is_off(self):
        self.assertIsNone(F.mcp_spec(self.layout, self.ts.team))
        self.assertFalse(F.mcp_config_path(self.ts.team).exists())
        self.enable()
        F.set_team(self.ts.team, enabled=False)
        self.assertIsNone(F.mcp_spec(self.layout, self.ts.team))
        self.assertIsNone(F.mcp_spec(self.layout, self.ts.session.team("ghost")), "never raises")

    def test_the_spec_pins_the_socket_and_the_team_dir_and_writes_the_config(self):
        self.enable()
        spec = F.mcp_spec(self.layout, self.ts.team)
        self.assertEqual(spec["server"], "synapse_canvas")
        self.assertTrue(spec["command"].endswith("/bin/herdr-synapse"))
        self.assertTrue(os.path.isabs(spec["command"]))
        self.assertEqual(spec["args"], ["--socket", os.path.abspath(os.fspath(self.layout.socket)), "--team",
                                        os.path.abspath(os.fspath(self.ts.team.root)), "canvas", "mcp"])
        path = F.mcp_config_path(self.ts.team)
        self.assertEqual(spec["config_file"], os.path.abspath(os.fspath(path)))
        self.assertEqual(store.read_json(path), {"mcpServers": {"synapse_canvas": {"type": "stdio", "command": spec["command"], "args": spec["args"]}}})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        self.assertEqual(F.mcp_spec(self.layout, self.ts.team), spec, "every launch path gets the same spec")

    def test_write_false_leaves_the_file_alone(self):
        self.enable()
        self.assertIsNotNone(F.mcp_spec(self.layout, self.ts.team, write=False))
        self.assertFalse(F.mcp_config_path(self.ts.team).exists())

    def test_launch_args_per_kind(self):
        self.enable()
        spec = F.mcp_spec(self.layout, self.ts.team)
        self.assertEqual(F.mcp_launch_args("claude", spec), ["--mcp-config", spec["config_file"]])
        self.assertEqual(F.mcp_launch_args("pi", spec), [], "Pi has no --mcp-config and exits on an unknown flag")
        codex = F.mcp_launch_args("codex", spec)
        self.assertEqual(codex[0::2], ["-c", "-c"])
        key, value = codex[1].split("=", 1)
        self.assertEqual(key, "mcp_servers.synapse_canvas.command")
        self.assertEqual(json.loads(value), spec["command"])
        key, value = codex[3].split("=", 1)
        self.assertEqual(key, "mcp_servers.synapse_canvas.args")
        self.assertEqual(json.loads(value), spec["args"])
        for kind in ("opencode", "gemini", "cursor", None):
            self.assertEqual(F.mcp_launch_args(kind, spec), [])
        self.assertEqual(F.mcp_launch_args("claude", None), [])

    def test_only_synapse_injections_are_preserved(self):
        self.enable()
        spec = F.mcp_spec(self.layout, self.ts.team)
        for kind in ("claude", "pi", "codex"):
            args = F.mcp_launch_args(kind, spec)
            for flag, value in zip(args[0::2], args[1::2]):
                self.assertTrue(F.is_preserved_mcp_value(kind, flag, value), (kind, flag, value))
        refused = [
            ("claude", "--mcp-config", '{"mcpServers":{"x":{"command":"y","env":{"TOKEN":"secret"}}}}'),
            ("claude", "--mcp-config", "/home/me/.claude/servers.json"),
            ("claude", "--mcp-config", "whiteboard/mcp.json"),
            ("claude", "--settings", spec["config_file"]),
            ("codex", "-c", 'model_reasoning_effort="high"'),
            ("codex", "-c", 'mcp_servers.other.command="x"'),
            ("codex", "-c", 'mcp_servers.synapse_canvas.env={"A":"b"}'),
            ("codex", "--mcp-config", spec["config_file"]),
            ("opencode", "--mcp-config", spec["config_file"]),
            ("pi", "--mcp-config", spec["config_file"]),
            ("claude", "--mcp-config", spec["config_file"] + "\n"),
        ]
        for kind, flag, value in refused:
            self.assertFalse(F.is_preserved_mcp_value(kind, flag, value), (kind, flag, value))


if __name__ == "__main__":
    unittest.main()

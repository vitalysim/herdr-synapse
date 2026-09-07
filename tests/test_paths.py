import hashlib
import os
import stat
import unittest
from pathlib import Path

from herdr_team import paths
from herdr_team.errors import HerdrTeamError
from support import TempState


class SocketResolutionTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.base_env = {"HOME": os.fspath(self.ts.home), "XDG_CONFIG_HOME": os.fspath(self.ts.config_home)}

    def test_default_socket_from_config_dir(self):
        res = paths.resolve_socket(self.base_env)
        self.assertEqual(res.source, paths.SOCKET_SOURCE_DEFAULT)
        self.assertEqual(res.raw, self.ts.config_dir / "herdr.sock")
        self.assertFalse(res.explicit)
        self.assertIsNone(res.session_name)

    def test_home_fallback_without_xdg(self):
        env = {"HOME": os.fspath(self.ts.home)}
        res = paths.resolve_socket(env)
        self.assertEqual(res.raw, self.ts.home / ".config" / "herdr" / "herdr.sock")

    def test_app_dir_override(self):
        env = dict(self.base_env, HERDR_TEAM_APP_DIR="herdr-dev")
        self.assertEqual(paths.config_dir(env), self.ts.config_home / "herdr-dev")

    def test_home_unset_is_an_error(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            paths.resolve_socket({})
        self.assertEqual(ctx.exception.code, "home_unset")

    def test_env_socket_path_wins_over_env_session(self):
        env = dict(self.base_env, HERDR_SOCKET_PATH="/tmp/x/herdr.sock", HERDR_SESSION="foo")
        res = paths.resolve_socket(env)
        self.assertEqual(res.source, paths.SOCKET_SOURCE_ENV_SOCKET)
        self.assertEqual(res.raw, Path("/tmp/x/herdr.sock"))

    def test_env_session(self):
        env = dict(self.base_env, HERDR_SESSION="rig1")
        res = paths.resolve_socket(env)
        self.assertEqual(res.source, paths.SOCKET_SOURCE_ENV_SESSION)
        self.assertEqual(res.raw, self.ts.config_dir / "sessions" / "rig1" / "herdr.sock")
        self.assertEqual(res.session_name, "rig1")

    def test_session_arg_beats_env_socket(self):
        env = dict(self.base_env, HERDR_SOCKET_PATH="/tmp/x/herdr.sock")
        res = paths.resolve_socket(env, session="rig2")
        self.assertEqual(res.source, paths.SOCKET_SOURCE_SESSION_ARG)
        self.assertTrue(res.explicit)
        self.assertEqual(res.raw, self.ts.config_dir / "sessions" / "rig2" / "herdr.sock")

    def test_session_arg_default_normalizes(self):
        res = paths.resolve_socket(self.base_env, session="default")
        self.assertIsNone(res.session_name)
        self.assertEqual(res.raw, self.ts.config_dir / "herdr.sock")

    def test_socket_arg_beats_everything(self):
        env = dict(self.base_env, HERDR_SOCKET_PATH="/tmp/x/herdr.sock", HERDR_SESSION="foo")
        res = paths.resolve_socket(env, session="rig", socket="/tmp/y/custom.sock")
        self.assertEqual(res.source, paths.SOCKET_SOURCE_ARG)
        self.assertEqual(res.raw, Path("/tmp/y/custom.sock"))

    def test_invalid_session_name(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            paths.resolve_socket(self.base_env, session="bad name")
        self.assertEqual(ctx.exception.code, "session_name_invalid")

    def test_canonicalizes_symlinked_socket_dir(self):
        real_dir = self.ts.tmp / "real"
        real_dir.mkdir()
        link = self.ts.tmp / "link"
        os.symlink(real_dir, link)
        res = paths.resolve_socket(dict(self.base_env, HERDR_SOCKET_PATH=os.fspath(link / "herdr.sock")))
        self.assertEqual(res.path, Path(os.path.realpath(real_dir)) / "herdr.sock")


class SlugTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(paths.session_slug("/tmp/cfg/herdr/herdr.sock"), "default")

    def test_named(self):
        self.assertEqual(paths.session_slug("/tmp/cfg/herdr/sessions/rig-1/herdr.sock"), "rig-1")

    def test_hash_for_other_names(self):
        p = "/tmp/custom/api.sock"
        expected = "sock-" + hashlib.sha1(os.path.realpath(p).encode()).hexdigest()[:8]
        self.assertEqual(paths.session_slug(p), expected)

    def test_config_dir_for_socket(self):
        # /nonexistent avoids macOS realpath mapping /tmp to /private/tmp
        self.assertEqual(paths.config_dir_for_socket("/nonexistent/cfg/herdr/herdr.sock"), Path("/nonexistent/cfg/herdr"))
        self.assertEqual(paths.config_dir_for_socket("/nonexistent/cfg/herdr-dev/sessions/x/herdr.sock"), Path("/nonexistent/cfg/herdr-dev"))
        self.assertEqual(paths.config_dir_for_socket("/nonexistent/custom/api.sock"), Path("/nonexistent/custom"))


class StateRootTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.config = self.ts.config_dir
        self.env = {"HOME": os.fspath(self.ts.home), "XDG_STATE_HOME": os.fspath(self.ts.state_home)}

    def test_xdg_derivation_uses_config_dir_basename(self):
        res = paths.resolve_state_root(self.env, self.config)
        self.assertEqual(res.source, paths.STATE_SOURCE_XDG)
        self.assertEqual(res.path, self.ts.state_home / "herdr" / "plugins" / "herdr-team")
        res_dev = paths.resolve_state_root(self.env, self.ts.config_home / "herdr-dev")
        self.assertEqual(res_dev.path, self.ts.state_home / "herdr-dev" / "plugins" / "herdr-team")

    def test_xdg_home_fallback(self):
        res = paths.resolve_state_root({"HOME": os.fspath(self.ts.home)}, self.config)
        self.assertEqual(res.path, self.ts.home / ".local" / "state" / "herdr" / "plugins" / "herdr-team")

    def test_pointer_file_beats_xdg(self):
        target = self.ts.tmp / "pointed"
        paths.write_pointer(self.config, target)
        pointer = paths.pointer_file(self.config)
        self.assertTrue(pointer.is_file())
        self.assertEqual(stat.S_IMODE(pointer.stat().st_mode), 0o600)
        self.assertEqual(paths.read_pointer(self.config), target)
        res = paths.resolve_state_root(self.env, self.config)
        self.assertEqual((res.source, res.path), (paths.STATE_SOURCE_POINTER, target))

    def test_empty_pointer_is_ignored(self):
        pointer = paths.pointer_file(self.config)
        pointer.parent.mkdir(parents=True)
        pointer.write_text("\n")
        self.assertIsNone(paths.read_pointer(self.config))
        self.assertEqual(paths.resolve_state_root(self.env, self.config).source, paths.STATE_SOURCE_XDG)

    def test_symlinked_pointer_refused(self):
        pointer = paths.pointer_file(self.config)
        pointer.parent.mkdir(parents=True)
        real = self.ts.tmp / "real-pointer"
        real.write_text("/x\n")
        os.symlink(real, pointer)
        with self.assertRaises(HerdrTeamError) as ctx:
            paths.read_pointer(self.config)
        self.assertEqual(ctx.exception.code, "path_symlink")

    def test_plugin_state_dir_beats_pointer(self):
        paths.write_pointer(self.config, self.ts.tmp / "pointed")
        env = dict(self.env, HERDR_PLUGIN_STATE_DIR="/plug/state")
        res = paths.resolve_state_root(env, self.config)
        self.assertEqual((res.source, res.path), (paths.STATE_SOURCE_PLUGIN, Path("/plug/state")))

    def test_team_dir_env_beats_plugin_state_dir(self):
        env = dict(self.env, HERDR_PLUGIN_STATE_DIR="/plug/state", HERDR_TEAM_DIR="/root/sessions/default/teams/alpha")
        res = paths.resolve_state_root(env, self.config)
        self.assertEqual(res.source, paths.STATE_SOURCE_TEAM_DIR)
        self.assertEqual(res.path, Path("/root"))
        self.assertEqual(res.team_dir, Path("/root/sessions/default/teams/alpha"))

    def test_team_dir_env_invalid_shape(self):
        env = dict(self.env, HERDR_TEAM_DIR="/root/alpha")
        with self.assertRaises(HerdrTeamError) as ctx:
            paths.resolve_state_root(env, self.config)
        self.assertEqual(ctx.exception.code, "team_dir_invalid")

    def test_override_beats_team_dir_env(self):
        env = dict(self.env, HERDR_TEAM_DIR="/root/sessions/default/teams/alpha", HERDR_TEAM_STATE_DIR="/override")
        res = paths.resolve_state_root(env, self.config)
        self.assertEqual((res.source, res.path), (paths.STATE_SOURCE_OVERRIDE, Path("/override")))

    def test_team_path_arg_beats_override(self):
        team_dir = self.ts.team.root
        env = dict(self.env, HERDR_TEAM_STATE_DIR="/override")
        res = paths.resolve_state_root(env, self.config, team_path=os.fspath(team_dir))
        self.assertEqual(res.source, paths.STATE_SOURCE_TEAM_ARG)
        self.assertEqual(res.path, Path(os.path.realpath(self.ts.state_root)))

    def test_team_name_arg_is_not_a_path(self):
        env = dict(self.env, HERDR_TEAM_STATE_DIR="/override")
        res = paths.resolve_state_root(env, self.config, team_path="alpha")
        self.assertEqual(res.source, paths.STATE_SOURCE_OVERRIDE)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_resolve_layout_from_temp_state(self):
        layout = paths.resolve_layout(self.ts.env)
        self.assertEqual(layout.slug, "default")
        self.assertEqual(layout.session.root, self.ts.state_root / "sessions" / "default")
        self.assertEqual(layout.config_dir, Path(os.path.realpath(self.ts.config_dir)))
        self.assertEqual(layout.team("alpha").team_json, self.ts.team.team_json)
        j = layout.to_json()
        self.assertEqual(j["state_root_source"], paths.STATE_SOURCE_OVERRIDE)
        self.assertEqual(j["slug"], "default")

    def test_named_session_layout(self):
        ts = TempState(slug="rig-7")
        self.addCleanup(ts.cleanup)
        layout = paths.resolve_layout(ts.env)
        self.assertEqual(layout.slug, "rig-7")
        self.assertEqual(layout.session.root.name, "rig-7")

    def test_team_path_sets_slug_from_path(self):
        env = {"HOME": os.fspath(self.ts.home)}  # no socket, outside Herdr
        layout = paths.resolve_layout(env, team=os.fspath(self.ts.team.root))
        self.assertEqual(layout.slug, "default")
        self.assertEqual(layout.state_root.source, paths.STATE_SOURCE_TEAM_ARG)
        self.assertEqual(layout.team("alpha").root, Path(os.path.realpath(self.ts.team.root)))

    def test_team_name_from_arg(self):
        self.assertEqual(paths.team_name_from_arg("alpha", {}), "alpha")
        self.assertEqual(paths.team_name_from_arg("/x/sessions/s/teams/beta", {}), "beta")
        self.assertEqual(paths.team_name_from_arg(None, {"HERDR_TEAM_DIR": "/x/sessions/s/teams/gamma"}), "gamma")
        self.assertEqual(paths.team_name_from_arg(None, {"HERDR_TEAM": "delta"}), "delta")
        self.assertIsNone(paths.team_name_from_arg(None, {}))
        with self.assertRaises(HerdrTeamError):
            paths.team_name_from_arg("Bad", {})

    def test_team_paths_layout(self):
        team = self.ts.session.team("alpha")
        root = self.ts.session.root / "teams" / "alpha"
        self.assertEqual(team.team_lock, root / "team.lock")
        self.assertEqual(team.board_jsonl, root / "board.jsonl")
        self.assertEqual(team.board_seq, root / "board.seq")
        self.assertEqual(team.archive_index, root / "archive" / "index.json")
        self.assertEqual(team.archive_segment(1, 50), root / "archive" / "board.1-50.jsonl")
        self.assertEqual(team.cursor("alpha-worker"), root / "cursors" / "alpha-worker.json")
        self.assertEqual(team.human_cursor("vitaly"), root / "cursors" / "human@vitaly.json")
        self.assertEqual(team.briefing("alpha-worker"), root / "briefings" / "alpha-worker.txt")
        self.assertEqual(team.ledger, root / "notifier" / "ledger.jsonl")
        self.assertEqual(team.notifier_state, root / "notifier" / "state.json")
        self.assertEqual(team.jobs_dir, root / "notifier" / "jobs")
        self.assertEqual(team.mute_json, root / "mute.json")
        self.assertEqual(team.audit_jsonl, root / "audit.jsonl")
        self.assertEqual(team.payloads_dir, root / "payloads")

    def test_session_paths_layout(self):
        s = self.ts.session
        self.assertEqual(s.daemon_lock, s.root / "daemon.lock")
        self.assertEqual(s.daemon_json, s.root / "daemon.json")
        self.assertEqual(s.who_json, s.root / "who.json")
        self.assertEqual(s.console_json, s.root / "console.json")
        self.assertEqual(s.kinds_json, s.root / "kinds.json")
        self.assertEqual(s.view_json, s.root / "view.json")
        self.assertEqual(s.hooks_log, s.root / "hooks.log")
        self.assertEqual(s.pane_record("term_abc"), s.root / "panes" / "term_abc.json")
        self.assertEqual(s.team_archive("alpha", "20260904T1200"), s.root / "_archive" / "alpha-20260904T1200")
        self.assertEqual(s.list_teams(), ["alpha"])

    def test_invalid_names_refused(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            self.ts.session.team("Alpha")
        self.assertEqual(ctx.exception.code, "team_name_invalid")
        with self.assertRaises(HerdrTeamError):
            self.ts.session.team("a" * 33)
        with self.assertRaises(HerdrTeamError):
            self.ts.session.pane_record("../x")
        with self.assertRaises(HerdrTeamError):
            self.ts.team.cursor("../etc")
        with self.assertRaises(HerdrTeamError):
            self.ts.team.human_cursor("a/b")

    def test_dir_modes_and_symlink_refusal(self):
        for d in self.ts.session.directories() + self.ts.team.directories():
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)
        victim = self.ts.tmp / "victim"
        victim.mkdir()
        link = self.ts.session.root / "teams" / "evil"
        os.symlink(victim, link)
        with self.assertRaises(HerdrTeamError) as ctx:
            paths.ensure_dir(link)
        self.assertEqual(ctx.exception.code, "path_symlink")
        with self.assertRaises(HerdrTeamError) as ctx:
            paths.ensure_dir(link / "sub")
        self.assertEqual(ctx.exception.code, "path_symlink")
        plain = self.ts.tmp / "plain"
        plain.write_text("x")
        with self.assertRaises(HerdrTeamError) as ctx:
            paths.ensure_dir(plain)
        self.assertEqual(ctx.exception.code, "path_not_directory")

    def test_ensure_dir_creates_nested_with_mode(self):
        target = self.ts.tmp / "a" / "b" / "c"
        paths.ensure_dir(target)
        for d in (target, target.parent, target.parent.parent):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)


class AllowedSocketsTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_absent_allows(self):
        self.assertTrue(paths.socket_allowed(self.ts.config_dir, self.ts.socket_path))

    def test_present_requires_listing(self):
        f = paths.allowed_sockets_file(self.ts.config_dir)
        f.parent.mkdir(parents=True)
        f.write_text("# rig\n/tmp/other.sock\n")
        self.assertFalse(paths.socket_allowed(self.ts.config_dir, self.ts.socket_path))
        f.write_text("{}\n".format(self.ts.socket_path))
        self.assertTrue(paths.socket_allowed(self.ts.config_dir, self.ts.socket_path))


if __name__ == "__main__":
    unittest.main()

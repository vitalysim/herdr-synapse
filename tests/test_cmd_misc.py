"""doctor, setup, keys, install-cli, gc, prune, view, teardown, and delivery commands (docs/cli.md sections 8 and 9)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from herdr_team import cli, paths, store
from herdr_team import cmd_misc
from support import FakeApi, FakeError, TempState, fake_agent, fake_read


def run_cli(argv, env, api=None):
    out, err = io.StringIO(), io.StringIO()
    fake = api if api is not None else FakeApi()
    with mock.patch("herdr_team.api.HerdrApi", lambda socket_path, env=None, **kw: fake):
        code = cli.main(list(argv), env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def json_out(result):
    code, out, err = result
    return code, (json.loads(out) if out.strip() else None), (json.loads(err.splitlines()[0]) if err.strip() and err.lstrip().startswith("{") else err)


def write_live_daemon(ts):
    from herdr_team.cmd_board import now_iso

    start = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, check=False).stdout.decode().strip()
    store.write_json(ts.session.daemon_json, {"pid": os.getpid(), "start_time": start, "beat_at": now_iso(), "socket": os.fspath(ts.socket_path), "socket_inode": 1, "version": "0.1.0", "herdr_version": "0.8.2", "protocol": 20})


PLAN_SIDEBAR_SNIPPET = """[ui.sidebar.agents]
rows = [
  ["state_icon", "agent"],
  [{ token = "$team_c1", fg = "#fb4934" }, { token = "$team_c2", fg = "#b8bb26" }, { token = "$team_c3", fg = "#83a598" }, { token = "$team_c4", fg = "#d3869b" }, { token = "$team_c5", fg = "#fabd2f" }, { token = "$team_c6", fg = "#8ec07c" }, { token = "$team_role", dim = true }, { token = "$team_task", fg = "#89b4fa" }, { token = "$team_context", fg = "#7f849c" }, { token = "$team_context_warn", fg = "#fabd2f" }, { token = "$team_context_crit", fg = "#fb4934" }],
  ["workspace", "tab"],
]
[ui.sidebar.agents.rows_by_agent]
claude = [
  ["state_icon", "agent"],
  [{ token = "$team_c1", fg = "#fb4934" }, { token = "$team_c2", fg = "#b8bb26" }, { token = "$team_c3", fg = "#83a598" }, { token = "$team_c4", fg = "#d3869b" }, { token = "$team_c5", fg = "#fabd2f" }, { token = "$team_c6", fg = "#8ec07c" }, { token = "$team_role", dim = true }, { token = "$team_task", fg = "#89b4fa" }, { token = "$team_context", fg = "#7f849c" }, { token = "$team_context_warn", fg = "#fabd2f" }, { token = "$team_context_crit", fg = "#fb4934" }],
  ["terminal_title_stripped"],
  ["workspace", "tab"],
]
"""

DEFAULT_CONFIG_EXCERPT = """[keys]
# prefix = "ctrl+b"
# new_tab = "prefix+c"
# previous_tab = "prefix+p"
# workspace_picker = "prefix+w"
# split_vertical = "prefix+v"
# [[keys.command]]
# key = "prefix+alt+g"
# type = "popup"

[ui]
# sidebar_width = 26
"""


class DoctorTests(unittest.TestCase):
    def test_doctor_without_herdr(self):
        with TempState() as ts:
            api = FakeApi()
            api.unreachable = True
            (ts.config_dir / "config.toml").write_text("[ui.toast]\ndelivery = \"terminal\"\n")
            code, payload, err = json_out(run_cli(["--json", "doctor"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["version"], cmd_misc.VERSION)
            self.assertFalse(payload["herdr"]["reachable"])
            self.assertEqual(payload["herdr"]["bin"], "herdr")
            self.assertEqual(payload["socket"]["path"], os.path.realpath(ts.socket_path))
            self.assertEqual(payload["socket"]["source"], "env:HERDR_SOCKET_PATH")
            self.assertTrue(payload["socket"]["allowed"])
            self.assertEqual(payload["slug"], "default")
            self.assertEqual(payload["state_root"]["source"], "env:HERDR_TEAM_STATE_DIR")
            sources = [c["source"] for c in payload["state_root"]["candidates"]]
            self.assertEqual(sources, ["team-arg", "env:HERDR_TEAM_STATE_DIR", "env:HERDR_TEAM_DIR", "env:HERDR_PLUGIN_STATE_DIR", "pointer-file", "xdg"])
            self.assertEqual(payload["state_root"]["candidates"][1]["path"], os.fspath(ts.state_root))
            self.assertIsNone(payload["pointer"])
            self.assertEqual(payload["plugin"]["installed"], None)
            self.assertIn("HERDR_TEAM_ALLOW_HERDR", payload["plugin"]["skipped"])
            self.assertEqual(payload["toast_delivery"], "terminal")
            self.assertFalse(payload["daemon"]["alive"])
            self.assertEqual(payload["teams"], [{"team": "alpha", "members": 2, "missing": 0, "missions": 0, "missing_missions": ["alpha-reviewer", "alpha-worker"]}])
            self.assertEqual(payload["console"], {"open": False, "pane_id": None, "lifecycle": None})
            self.assertEqual(payload["toast_probe"], {"probed": False, "shown": False, "reason": None})
            self.assertTrue(any("unreachable" in w for w in payload["warnings"]))
            self.assertEqual(payload["errors"], [])
            self.assertFalse(api.runs)
            code, out, _ = run_cli(["doctor"], ts.env, api)
            self.assertIn("state root:", out)
            self.assertIn("pointer-file", out)
            self.assertIn("toast delivery: terminal", out)

    def test_doctor_reachable_with_pointer_and_bin_path(self):
        with TempState() as ts:
            paths.write_pointer(ts.config_dir, ts.state_root)
            write_live_daemon(ts)
            env = ts.env_with(HERDR_BIN_PATH="/opt/fake/herdr")
            code, payload, _ = json_out(run_cli(["--json", "doctor"], env, FakeApi()))
            self.assertTrue(payload["herdr"]["reachable"])
            self.assertEqual(payload["herdr"]["version"], "0.8.2")
            self.assertEqual(payload["herdr"]["protocol"], 20)
            self.assertEqual(payload["herdr"]["bin"], "/opt/fake/herdr")
            self.assertTrue(payload["pointer"].endswith("plugins/config/herdr-synapse/state-dir"))
            self.assertTrue(payload["daemon"]["alive"])
            self.assertEqual(payload["daemon"]["pid"], os.getpid())

    def test_doctor_plugin_list_only_when_allowed(self):
        with TempState() as ts:
            api = FakeApi()
            # The real CLI prints the socket envelope; run_json strips it.
            api.set_cli_result(["plugin", "list", "--json"], {"type": "plugin_list", "plugins": [{"plugin_id": "herdr-synapse", "enabled": True, "plugin_root": "/x/herdr-synapse", "warnings": ["manifest moved"]}]}, "cli:plugin")
            code, payload, _ = json_out(run_cli(["--json", "doctor"], ts.env, api))
            self.assertFalse(api.runs)
            code, payload, _ = json_out(run_cli(["--json", "doctor"], ts.env_with(HERDR_TEAM_ALLOW_HERDR="1"), api))
            self.assertEqual(api.runs, [["plugin", "list", "--json"]])
            self.assertEqual(payload["plugin"], {"installed": True, "enabled": True, "path": "/x/herdr-synapse", "warnings": ["manifest moved"], "source": "plugin list --json"})
            self.assertIn("plugin: manifest moved", payload["warnings"])


class SetupAndKeys(unittest.TestCase):
    def test_setup_print_config_matches_plan(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "setup", "--print-config"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertTrue(payload["required"].startswith(PLAN_SIDEBAR_SNIPPET))
            self.assertIn('{ token = "$team_c1", fg = "#fb4934" }', payload["required"])
            self.assertIn("[[keys.command]]\nkey = \"prefix+t\"\ntype = \"plugin_action\"\ncommand = \"herdr-synapse.team-up\"", payload["required"])
            self.assertIn('status_indicators = "symbols"', payload["optional"])
            self.assertIn("sidebar_width", payload["optional"])
            self.assertTrue(payload["notes"])
            code, out, _ = run_cli(["setup", "--print-config"], ts.env)
            self.assertIn("# --- required", out)
            self.assertIn(PLAN_SIDEBAR_SNIPPET, out)
            self.assertIn("# --- optional", out)
            code, _, err = json_out(run_cli(["--json", "setup"], ts.env))
            self.assertEqual(code, 2)
            self.assertFalse((ts.config_dir / "config.toml").exists())

    def test_keys_print(self):
        with TempState() as ts:
            code, payload, _ = json_out(run_cli(["--json", "keys", "print"], ts.env))
            self.assertEqual(payload["keys"], {"team-up": "prefix+t", "compose": "prefix+m", "console": "prefix+u", "toggle-view": "prefix+y", "usage": "prefix+i", "knowledge": "prefix+f"})
            snippet = payload["snippet"]
            self.assertEqual(snippet.count("[[keys.command]]"), 6)
            self.assertEqual(snippet.count('type = "plugin_action"'), 6)
            for action in ("herdr-synapse.team-up", "herdr-synapse.compose", "herdr-synapse.console", "herdr-synapse.toggle-view", "herdr-synapse.usage"):
                self.assertIn('command = "{}"'.format(action), snippet)
            code, out, _ = run_cli(["keys", "print"], ts.env)
            self.assertEqual(out.strip(), snippet.strip())

    def test_keys_check_against_defaults_and_user_config(self):
        with TempState() as ts:
            api = FakeApi()
            api.set_cli(["--default-config"], 0, DEFAULT_CONFIG_EXCERPT, "")
            api.set_cli(["config", "check"], 0, "config ok\n", "")
            code, payload, err = json_out(run_cli(["--json", "keys", "check"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["collisions"], [])
            self.assertIn("config ok", payload["output"])
            (ts.config_dir / "config.toml").write_text('[keys]\nnew_tab = "prefix+t"\n\n[[keys.command]]\nkey = "prefix+u"\ntype = "popup"\ncommand = "lazygit"\n')
            code, payload, _ = json_out(run_cli(["--json", "keys", "check"], ts.env, api))
            self.assertFalse(payload["ok"])
            self.assertEqual([(c["key"], c["bound_to"]) for c in payload["collisions"]], [("prefix+t", "new_tab"), ("prefix+u", "keys.command")])
            api.set_cli(["config", "check"], 1, "", "invalid key\n")
            code, payload, _ = json_out(run_cli(["--json", "keys", "check"], ts.env, api))
            self.assertFalse(payload["ok"])

    def test_collision_helper_uses_defaults(self):
        defaults = '[keys]\n# split_vertical = "prefix+v"\n# new_tab = "prefix+c"\n'
        self.assertEqual(cmd_misc.key_collisions(defaults, None), [])
        self.assertEqual(cmd_misc.key_collisions('[keys]\n# new_tab = "prefix+t"\n', None)[0]["bound_to"], "new_tab")
        self.assertEqual(cmd_misc.key_collisions(None, '[keys]\nzoom = "prefix+y"\n')[0]["action"], "toggle-view")


class InstallCli(unittest.TestCase):
    def test_dry_run_then_yes(self):
        with TempState() as ts:
            code, payload, _ = json_out(run_cli(["--json", "install-cli"], ts.env))
            self.assertEqual(code, 0)
            self.assertTrue(payload["dry_run"])
            self.assertFalse(payload["created"])
            self.assertEqual(payload["path"], os.fspath(ts.home / ".local" / "bin" / "herdr-synapse"))
            self.assertTrue(payload["target"].endswith("bin/herdr-synapse"))
            self.assertFalse((ts.home / ".local" / "bin" / "herdr-synapse").exists())
            code, payload, _ = json_out(run_cli(["--json", "install-cli", "--yes"], ts.env))
            self.assertTrue(payload["created"])
            link = ts.home / ".local" / "bin" / "herdr-synapse"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.path.realpath(link), os.path.realpath(payload["target"]))
            code, payload, _ = json_out(run_cli(["--json", "install-cli", "--yes"], ts.env))
            self.assertFalse(payload["created"])
            self.assertFalse(payload["replaced"])
            os.unlink(link)
            os.symlink("/bin/sh", link)
            code, payload, _ = json_out(run_cli(["--json", "install-cli", "--yes"], ts.env))
            self.assertTrue(payload["replaced"])
            self.assertEqual(payload["existing"], "/bin/sh")

    def test_refuses_regular_file(self):
        with TempState() as ts:
            target = ts.home / "bin"
            target.mkdir()
            (target / "herdr-synapse").write_text("#!/bin/sh\n")
            code, _, err = json_out(run_cli(["--json", "install-cli", "--yes", "--dir", os.fspath(target)], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "path_exists")


class GcAndPrune(unittest.TestCase):
    def test_gc_removes_only_old_dead_sessions(self):
        with TempState() as ts:
            old = paths.session_paths(ts.state_root, "old-sess")
            paths.ensure_session_dirs(old)
            store.write_json(old.daemon_json, {"pid": 1, "socket": os.fspath(ts.tmp / "gone.sock")})
            stale = time.time() - 8 * 86400
            os.utime(old.root, (stale, stale))
            recent = paths.session_paths(ts.state_root, "fresh")
            paths.ensure_session_dirs(recent)
            store.write_json(recent.daemon_json, {"pid": 1, "socket": os.fspath(ts.tmp / "gone.sock")})
            code, payload, err = json_out(run_cli(["--json", "gc"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["removed"], [os.fspath(old.root)])
            self.assertIn(os.fspath(recent.root), payload["kept"])
            self.assertIn(os.fspath(ts.session.root), payload["kept"])
            self.assertFalse(old.root.exists())

    def test_gc_keeps_locked_tree(self):
        with TempState() as ts:
            old = paths.session_paths(ts.state_root, "held")
            paths.ensure_session_dirs(old)
            stale = time.time() - 8 * 86400
            os.utime(old.root, (stale, stale))
            lock = store.daemon_lock(old).acquire()
            try:
                code, payload, _ = json_out(run_cli(["--json", "gc"], ts.env))
                self.assertIn(os.fspath(old.root), payload["kept"])
            finally:
                lock.release()

    def test_prune_archives_old_segments(self):
        with TempState() as ts:
            paths.ensure_dir(ts.team.archive_dir)
            old_seg = ts.team.archive_segment(1, 50)
            old_seg.write_bytes(b'{"v":1,"seq":1,"from":"human","to":["all"],"kind":"note","text":"x"}\n')
            stale = time.time() - 40 * 86400
            os.utime(old_seg, (stale, stale))
            new_seg = ts.team.archive_segment(51, 60)
            new_seg.write_bytes(b"")
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "prune", "--keep-days", "30"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["team"], "alpha")
            self.assertEqual(len(payload["archived"]), 1)
            self.assertTrue(payload["archived"][0].endswith("board.1-50.jsonl"))
            self.assertGreater(payload["bytes_freed"], 0)
            self.assertFalse(old_seg.exists())
            self.assertTrue(new_seg.exists())
            self.assertTrue(payload["archived"][0].startswith(os.fspath(ts.session.archive_dir)))


class ViewAndTeardown(unittest.TestCase):
    def test_view_request_shape(self):
        request = cmd_misc.view_request(["alpha"])
        self.assertEqual(request["source"], "plugin:herdr-synapse")
        self.assertEqual(request["label"], "team:alpha")
        self.assertEqual(request["filter"], {"op": "any", "filters": [{"op": "eq", "field": {"token": "team"}, "value": "alpha"}, {"op": "in", "field": "status", "values": ["blocked"]}]})
        self.assertEqual(request["sort"][0], {"field": "attention", "order": "desc"})
        self.assertEqual(request["sort"][1], {"field": {"token": "team_role"}, "order": "asc"})
        self.assertLessEqual(len(cmd_misc.view_label(["a-long-team-name", "another"])), 27)
        self.assertLessEqual(len(cmd_misc.view_label(["a-very-long-team-nm"])), 20)

    def test_view_on_off_toggle(self):
        with TempState() as ts:
            api = FakeApi()
            state = {"source": None}

            def clear(params):
                if state["source"] is None:
                    raise FakeError("agent_view_not_set", "no agent view is set")
                if params.get("source") not in (None, state["source"]):
                    raise FakeError("agent_view_source_mismatch", "view belongs to source {}".format(state["source"]))
                state["source"] = None
                return {"type": "ok"}

            def set_view(params):
                state["source"] = params["source"]
                return {"type": "ok"}

            api.set_response("agent.view.clear", clear)
            api.set_response("agent.view.set", set_view)
            code, payload, err = json_out(run_cli(["--json", "view", "on"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"view": "on", "source": "plugin:herdr-synapse", "label": "team:alpha", "owner": "none", "previous": "off"})
            self.assertEqual(state["source"], "plugin:herdr-synapse")
            self.assertEqual(store.read_json(ts.session.view_json)["view"], "on")
            code, payload, _ = json_out(run_cli(["--json", "view", "toggle"], ts.env, api))
            self.assertEqual(payload["view"], "off")
            self.assertEqual(payload["owner"], "own")
            self.assertIsNone(state["source"])
            self.assertFalse(ts.session.view_json.exists())
            state["source"] = "plugin:other"
            code, _, err = json_out(run_cli(["--json", "view", "on"], ts.env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "view_foreign")
            code, payload, _ = json_out(run_cli(["--json", "view", "on", "--force"], ts.env, api))
            self.assertEqual(code, 0)
            self.assertEqual(payload["owner"], "foreign")
            api.set_error("agent.view.clear", "plugin_disabled", "plugin herdr-synapse is disabled")
            code, _, err = json_out(run_cli(["--json", "view", "on"], ts.env, api))
            self.assertEqual(err["code"], "plugin_disabled")

    def test_teardown_clears_tokens_labels_view_console(self):
        with TempState() as ts:
            api = FakeApi()
            api.set_response("pane.rename", {"type": "ok"})
            api.set_response("agent.view.clear", {"type": "ok"})
            store.write_json(ts.session.view_json, {"view": "on"})
            store.write_json(ts.session.console_json, {"pane_id": "w1:p1", "pid": 2 ** 22 + 4242, "open": True})
            code, payload, err = json_out(run_cli(["--json", "teardown"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"tokens_cleared": 2, "labels_cleared": 2, "view_cleared": True, "daemon_stopped": False})
            clears = [p for m, p in api.calls if m == "pane.report_metadata"]
            self.assertTrue(all(all(v is None for v in p["tokens"].values()) for p in clears))
            self.assertFalse(ts.session.view_json.exists())
            self.assertFalse(ts.session.console_json.exists())


class Delivery(unittest.TestCase):
    def test_nudge_and_focus_need_daemon(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "nudge", "alpha-worker"], ts.env))
            self.assertEqual(code, 5)
            self.assertEqual(err["code"], "daemon_down")
            self.assertEqual(os.listdir(ts.team.jobs_dir), [])
            write_live_daemon(ts)
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "nudge", "alpha-worker", "--force"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["team"], "alpha")
            self.assertEqual(payload["member"], "alpha-worker")
            jobs = sorted(os.listdir(ts.team.jobs_dir))
            self.assertEqual(len(jobs), 1)
            job = store.read_json(ts.team.jobs_dir / jobs[0])
            self.assertEqual(job["v"], 1)
            self.assertEqual(job["kind"], "nudge")
            self.assertTrue(job["force"])
            self.assertEqual(job["requested_by"]["name"], "human")
            self.assertIn("requested_at", job)
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "focus", "alpha-reviewer"], ts.env))
            self.assertEqual(code, 0)
            jobs = sorted(os.listdir(ts.team.jobs_dir))
            self.assertEqual(len(jobs), 2)
            focus = [store.read_json(ts.team.jobs_dir / j) for j in jobs if store.read_json(ts.team.jobs_dir / j)["kind"] == "focus"][0]
            self.assertEqual(focus["pane_id"], "w2:p1")
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "nudge", "nobody"], ts.env))
            self.assertEqual(err["code"], "member_not_found")

    def test_mute_unmute_pause(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "mute", "alpha-worker", "--for", "10m"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["team"], "alpha")
            self.assertIsNone(payload["muted"]["*"])
            self.assertTrue(payload["muted"]["alpha-worker"].endswith("Z"))
            doc = store.read_json(ts.team.mute_json)
            self.assertIn("alpha-worker", doc)
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "pause"], ts.env))
            self.assertIsNone(payload["muted"]["*"])
            self.assertIn("*", store.read_json(ts.team.mute_json))
            code, who, _ = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, FakeApi()))
            self.assertEqual(who["nudges"], "paused")
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "unmute", "alpha-worker"], ts.env))
            self.assertNotIn("alpha-worker", payload["muted"])
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "unmute", "--all"], ts.env))
            self.assertEqual(payload["muted"], {"*": None})
            self.assertEqual(store.read_json(ts.team.mute_json), {})
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "mute"], ts.env))
            self.assertEqual(code, 2)
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "mute", "--all", "--for", "soon"], ts.env))
            self.assertEqual(code, 2)
            self.assertEqual(cmd_misc.parse_duration_s("90s"), 90)
            self.assertEqual(cmd_misc.parse_duration_s("2h"), 7200)
            self.assertEqual(cmd_misc.parse_duration_s("5"), 300)

    def test_read_visible_source_only(self):
        with TempState() as ts:
            api = FakeApi()
            api.set_response("agent.read", lambda params: fake_read("w2:p1", "line one\nline two", params["source"]))
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "read", "alpha-reviewer"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"team": "alpha", "member": "alpha-reviewer", "pane_id": "w2:p1", "lines": ["line one", "line two"]})
            self.assertEqual([p for m, p in api.calls if m == "agent.read"], [{"target": "w2:p1", "source": "visible"}])
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "read", "alpha-worker", "--lines", "40"], ts.env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "lines_refused")
            api.unreachable = True
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "read", "alpha-reviewer"], ts.env, api))
            self.assertEqual(code, 3)


class Registry(unittest.TestCase):
    def test_all_misc_commands_registered(self):
        names = {c.name for c in cli.load_commands()}
        for name in ("doctor", "setup", "keys", "install-cli", "gc", "prune", "view", "teardown", "nudge", "mute", "unmute", "pause", "focus", "read"):
            self.assertIn(name, names)

    def test_help_for_every_command(self):
        import contextlib

        for command in cli.load_commands():
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main([command.name, "--help"], env={}, stdout=out, stderr=err)
            self.assertEqual(code, 0, command.name)
            self.assertIn("usage:", out.getvalue())


class InboxHumanTests(unittest.TestCase):
    """``inbox --human`` (docs/cli.md section 7): posts to human, the attention mirror, and the human rendering."""

    def _attention(self, ts, seq, reason="shown", shown=True, kind="post"):
        from herdr_team.cmd_board import now_iso

        entry = {"ts": now_iso(), "team": "alpha", "seqs": [seq], "title": "#{} question from alpha-worker".format(seq), "body": "x", "reason": reason, "shown": shown, "kind": kind}
        store.append_line(ts.team.human_attention, json.dumps(entry).encode("utf-8"), fsync=False)
        return entry

    def _posts(self, ts):
        board = store.BoardStore(ts.team)
        seqs = []
        for text, to in (("first", ["human"]), ("not for human", ["alpha-reviewer"]), ("second", ["human", "alpha-reviewer"]), ("third", ["human"])):
            seqs.append(board.append({"from": "alpha-worker", "from_kind": "claude", "to": to, "kind": "question", "text": text}))
        board.append({"from": "human", "to": ["human"], "kind": "note", "text": "self-addressed, never shown"})
        return seqs

    def test_json_shape_and_filters(self):
        with TempState() as ts:
            first, _other, second, third = self._posts(ts)
            self._attention(ts, first)
            store.Cursors(ts.team).advance("human@human", first, "term_console", "cli")
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "inbox", "--human"], ts.env, FakeApi()))
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(payload), ["attention", "cursor", "posts", "reader", "team", "unread"])
            self.assertEqual(payload["team"], "alpha")
            self.assertEqual(payload["reader"], "human@human")
            self.assertEqual(payload["cursor"], first)
            self.assertEqual([p["seq"] for p in payload["posts"]], [first, second, third], "to human, not from human, in seq order")
            self.assertEqual(payload["unread"], 2, "two posts above the reader cursor")
            self.assertEqual(sorted(payload["attention"][0]), ["body", "kind", "reason", "seqs", "shown", "team", "title", "ts"])
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "inbox", "--human", "--since", str(second)], ts.env, FakeApi()))
            self.assertEqual(code, 0, err)
            self.assertEqual([p["seq"] for p in payload["posts"]], [third], "--since keeps only seqs above it")
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "inbox", "--human", "--last", "1"], ts.env, FakeApi()))
            self.assertEqual([p["seq"] for p in payload["posts"]], [third], "--last keeps the newest")
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "inbox", "--human", "--last", "0"], ts.env, FakeApi()))
            self.assertEqual((payload["posts"], payload["attention"]), ([], []), "--last 0 prints neither")
            self.assertEqual(int(store.Cursors(ts.team).get("human@human").get("seq", 0)), first, "inbox never advances a cursor")

    def test_human_rendering_board_then_attention_lines(self):
        with TempState() as ts:
            first, _other, second, third = self._posts(ts)
            shown = self._attention(ts, first)
            busy = self._attention(ts, third, reason="busy", shown=False, kind="urgent")
            store.append_line(ts.team.human_attention, b"{not json", fsync=False)
            code, out, err = run_cli(["--team", "alpha", "inbox", "--human"], ts.env, FakeApi())
            self.assertEqual(code, 0, err)
            self.assertIn("first", out)
            self.assertIn("third", out)
            self.assertNotIn("not for human", out)
            self.assertNotIn("self-addressed", out)
            attention_at = out.index("-- attention (2):")
            self.assertGreater(attention_at, out.index("third"), "board render first, then the attention block")
            tail = out[attention_at:].splitlines()
            self.assertEqual(tail[1], "  {} {} [{}] {}".format(shown["ts"], "post", "shown", shown["title"]))
            self.assertEqual(tail[2], "  {} {} [{}] {}".format(busy["ts"], "urgent", "busy", busy["title"]))

    def test_no_posts_and_no_attention(self):
        with TempState() as ts:
            code, out, err = run_cli(["--team", "alpha", "inbox", "--human"], ts.env, FakeApi())
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), "no posts to human")
            self.assertNotIn("-- attention", out)
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "inbox", "--human"], ts.env, FakeApi()))
            self.assertEqual((payload["posts"], payload["attention"], payload["unread"], payload["cursor"]), ([], [], 0, 0))


class NotifierStatsTests(unittest.TestCase):
    """``notifier stats`` (docs/cli.md section 9): ledger stats per team and kind, no daemon or socket needed."""

    def _fill(self, ts, kind="codex", n=1, member="alpha-reviewer"):
        from herdr_team import ledger as L

        ledger = L.Ledger(ts.team)
        for i in range(n):
            attempt = L.Attempt(
                id="{}-{}".format(kind, i), member=member, kind=kind, seqs=[i + 1], hook_authority=False, weak_idle=True,
                focused=False, prompt_line_empty=True, gate_ms=2500.0, queue_ms=4000.0, attempts=1, extra={},
            )
            ledger.record_intent(attempt)
            ledger.record_result(attempt.id, L.RESULT_LANDED_WORKING)
            ledger.record_outcome(attempt.id, True, 1000.0)
        return ledger

    def test_payload_keys_match_docs(self):
        with TempState() as ts:
            self._fill(ts, "codex", 2)
            self._fill(ts, "claude", 1)
            code, payload, err = json_out(run_cli(["--json", "notifier", "stats"], ts.env, FakeApi()))
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(payload), ["session_dir", "teams", "wrong_target"])
            self.assertEqual(payload["session_dir"], os.fspath(ts.session.root))
            self.assertEqual(payload["wrong_target"], 0)
            stats = payload["teams"]["alpha"]
            self.assertEqual(sorted(stats), ["counts", "kinds", "path"])
            self.assertEqual(stats["path"], os.fspath(ts.team.ledger))
            counts = stats["counts"]
            for key in ("intents", "open_intents", "outcomes", "cursor_advanced", "landed_working", "landed_in_turn", "not_submitted", "wrong_occupant", "wrong_target", "hung", "refused", "transient", "dry"):
                self.assertIn(key, counts, key)
            self.assertEqual((counts["intents"], counts["open_intents"], counts["landed_working"], counts["outcomes"], counts["cursor_advanced"]), (3, 0, 3, 3, 3))
            self.assertEqual(sorted(stats["kinds"]), ["claude", "codex"])
            bucket = stats["kinds"]["codex"]
            self.assertEqual(sorted(bucket), ["clean", "clean_rate", "round_trips", "verified"])
            self.assertEqual((bucket["round_trips"], bucket["clean"], bucket["clean_rate"], bucket["verified"]), (2, 2, None, False), "clean_rate is null below the 20 round-trip window")

    def test_kind_filter_and_team_selection(self):
        with TempState() as ts:
            self._fill(ts, "codex", 2)
            self._fill(ts, "claude", 1)
            code, payload, err = json_out(run_cli(["--json", "notifier", "stats", "--team", "alpha", "--kind", "claude"], ts.env, FakeApi()))
            self.assertEqual(code, 0, err)
            self.assertEqual(list(payload["teams"]), ["alpha"])
            self.assertEqual(list(payload["teams"]["alpha"]["kinds"]), ["claude"], "--kind keeps one bucket")
            self.assertEqual(payload["teams"]["alpha"]["counts"]["intents"], 3, "--kind does not touch the team counts")
            code, payload, err = json_out(run_cli(["--json", "notifier", "stats", "--team", "nope"], ts.env, FakeApi()))
            self.assertEqual(code, 1, err)
            self.assertEqual(err["code"], "team_not_found")
            self.assertEqual(err["teams"], ["alpha"])

    def test_human_rendering(self):
        with TempState() as ts:
            self._fill(ts, "codex", 2)
            code, out, err = run_cli(["notifier", "stats"], ts.env, FakeApi())
            self.assertEqual(code, 0, err)
            lines = out.splitlines()
            self.assertEqual(lines[0], "team alpha: intents 2 open 0 landed_working 2 landed_in_turn 0 transient 0 wrong_target 0")
            self.assertEqual(lines[1], "  codex: 2 round trips, clean rate n/a")


class DocsContractTests(unittest.TestCase):
    """docs/cli.md documents every command the board, misc, and daemon modules register, with the JSON keys they emit."""

    def _doc(self):
        from support import PLUGIN_ROOT

        return (PLUGIN_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")

    def test_inbox_human_is_documented(self):
        doc = self._doc()
        heading = doc.index("### `inbox --human [--last N] [--since <seq>]`")
        self.assertLess(doc.index("## 7. Board"), heading)
        self.assertLess(heading, doc.index("## 8. Delivery"))
        section = doc[heading:doc.index("## 8. Delivery")]
        for key in ('"team"', '"reader"', '"cursor"', '"unread"', '"posts"', '"attention"', '"seqs"', '"title"', '"body"', '"reason"', '"shown"', '"kind"'):
            self.assertIn(key, section, key)
        self.assertIn("-- attention (n):", section)
        self.assertIn("no posts to human", section)

    def test_notifier_stats_is_documented(self):
        doc = self._doc()
        heading = doc.index("### `notifier stats [--team NAME] [--kind KIND]`")
        self.assertLess(doc.index("## 9. Plugin and setup"), heading)
        self.assertLess(heading, doc.index("## 10. Shared record grammar"))
        section = doc[heading:doc.index("### `doctor`")]
        for key in ('"session_dir"', '"teams"', '"wrong_target"', '"counts"', '"kinds"', '"path"', '"round_trips"', '"clean"', '"clean_rate"', '"verified"', '"intents"', '"open_intents"', '"outcomes"', '"cursor_advanced"'):
            self.assertIn(key, section, key)
        from herdr_team import ledger as L

        for result in L.RESULTS:
            self.assertIn(result, section, result)
        self.assertIn("team_not_found", section)

    def test_every_registered_command_has_a_heading(self):
        from herdr_team import cmd_board, cmd_daemon

        doc = self._doc()
        for command in list(cmd_board.COMMANDS) + list(cmd_misc.COMMANDS) + list(cmd_daemon.COMMANDS):
            # Section 8 documents the delivery commands as table rows; everything else has a heading.
            documented = "### `{}".format(command.name) in doc or "| `{} ".format(command.name) in doc or "| `{}`".format(command.name) in doc
            self.assertTrue(documented, "docs/cli.md has no section or table row for {!r}".format(command.name))


if __name__ == "__main__":
    unittest.main()

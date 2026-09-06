"""Packaging checks: manifest grammar, launcher, hook gate, console wrapper, stubs."""

import json
import os
import re
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

from herdr_team import VERSION
from support import PLUGIN_ROOT, TempState, write_fake_herdr

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - 3.9/3.10
    tomllib = None

MANIFEST = PLUGIN_ROOT / "herdr-plugin.toml"
LAUNCHER = PLUGIN_ROOT / "bin" / "herdr-team"
HOOK = PLUGIN_ROOT / "bin" / "hook"
CONSOLE_SH = PLUGIN_ROOT / "console.sh"


def clean_env(extra=None):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "HERDR_TEAM_PYTHON": sys.executable,
    }
    if extra:
        env.update(extra)
    return env


def run(cmd, env=None, timeout=20):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env or clean_env(), cwd=os.fspath(PLUGIN_ROOT))


class LauncherTests(unittest.TestCase):
    def test_launcher_works_through_a_symlink(self):
        """``install-cli`` symlinks ~/.local/bin/herdr-team to bin/herdr-team; the launcher must follow the link."""
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "bin" / "herdr-team"
            link.parent.mkdir()
            link.symlink_to(PLUGIN_ROOT / "bin" / "herdr-team")
            nested = Path(tmp) / "nested"
            nested.symlink_to(link)  # a link to a link
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "HERDR_TEAM_PYTHON": sys.executable}
            for path in (link, nested):
                proc = subprocess.run([os.fspath(path), "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                self.assertEqual(proc.stdout.decode().strip(), "herdr-team {}".format(VERSION))


class ManifestTests(unittest.TestCase):
    def test_manifest_is_regular_file_with_expected_entries(self):
        self.assertTrue(MANIFEST.is_file() and not MANIFEST.is_symlink())
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertIn('id = "herdr-team"', text)
        self.assertIn('min_herdr_version = "0.8.2"', text)
        self.assertIn('version = "{}"'.format(VERSION), text)
        ons = re.findall(r'^on = "([^"]+)"', text, re.M)
        self.assertEqual(sorted(ons), ["pane.agent_detected", "pane.closed", "pane.exited"])
        self.assertEqual(len(ons), len(set(ons)), "duplicate on")
        action_ids = re.findall(r'^\[\[actions\]\]\nid = "([^"]+)"', text, re.M)
        self.assertEqual(action_ids, ["team-up", "compose", "console", "who", "usage", "knowledge", "toggle-view", "daemon-start"])
        pane_ids = re.findall(r'^\[\[panes\]\]\nid = "([^"]+)"', text, re.M)
        self.assertEqual(pane_ids, ["console", "compose", "picker", "usage", "knowledge"])
        self.assertEqual(text.count("[[startup]]"), 1)
        self.assertNotIn("[[build]]", text)
        for ident in action_ids + pane_ids:
            self.assertRegex(ident, r"^[a-z][a-z0-9-]*$")

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_manifest_parses_as_toml(self):
        doc = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(doc["id"], "herdr-team")
        self.assertEqual(doc["platforms"], ["macos", "linux"])
        self.assertEqual(doc["startup"], [{"command": ["./bin/herdr-team", "daemon", "start"]}])
        self.assertEqual([a["contexts"] for a in doc["actions"]], [["global"], ["pane"], ["global"], ["global"], ["global"], ["global"], ["global"], ["global"]])
        self.assertEqual(doc["panes"][0]["command"], ["sh", "console.sh"])
        self.assertEqual(doc["panes"][1]["width"], "80%")
        self.assertEqual(doc["panes"][1]["height"], 12)
        self.assertEqual(doc["panes"][2]["height"], 28)
        self.assertEqual((doc["panes"][3]["id"], doc["panes"][3]["command"]), ("usage", ["./bin/herdr-team", "usage-pane"]))
        for pane in doc["panes"][1:]:
            self.assertEqual(pane["placement"], "popup")
        self.assertEqual(doc["panes"][0]["placement"], "split")

    def test_manifest_commands_exist_and_are_executable(self):
        text = MANIFEST.read_text(encoding="utf-8")
        for rel in set(re.findall(r'"(\./bin/[a-z-]+)"', text)):
            path = PLUGIN_ROOT / rel
            self.assertTrue(path.is_file(), rel)
            self.assertTrue(os.access(path, os.X_OK), rel)
        self.assertTrue(CONSOLE_SH.is_file())


class LauncherTests(unittest.TestCase):
    def test_shebang_and_exec_bit(self):
        for script in (LAUNCHER, HOOK, CONSOLE_SH):
            self.assertTrue(script.read_text().startswith("#!/bin/sh\n"), script)
            self.assertTrue(stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR, script)

    def test_version_through_launcher(self):
        proc = run([os.fspath(LAUNCHER), "--version"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.decode().strip(), "herdr-team {}".format(VERSION))

    def test_json_error_through_launcher(self):
        proc = run([os.fspath(LAUNCHER), "nope", "--json"])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr.decode())["code"], "usage")

    def test_refuses_old_python(self):
        with TempState() as ts:
            fake_py = ts.tmp / "python3"
            fake_py.write_text("#!/bin/sh\necho 'Python 3.8.19'\n")
            os.chmod(fake_py, 0o700)
            proc = run([os.fspath(LAUNCHER), "--version"], env=clean_env({"HERDR_TEAM_PYTHON": os.fspath(fake_py)}))
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stderr.decode())["code"], "python_too_old")

    def test_falls_back_to_path_python(self):
        env = clean_env()
        del env["HERDR_TEAM_PYTHON"]
        proc = run([os.fspath(LAUNCHER), "--version"], env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_python_message(self):
        env = clean_env({"PATH": "/nonexistent", "HERDR_TEAM_PYTHON": "/nonexistent/python3"})
        proc = run([os.fspath(LAUNCHER), "--version"], env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("python", proc.stderr.decode().lower())

    def test_pythonpath_is_plugin_root(self):
        proc = run([sys.executable, "-c", "import herdr_team, os; print(os.path.dirname(os.path.dirname(herdr_team.__file__)))"], env=clean_env({"PYTHONPATH": os.fspath(PLUGIN_ROOT)}))
        self.assertEqual(Path(proc.stdout.decode().strip()), PLUGIN_ROOT)


class HookGateTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.fake_herdr = write_fake_herdr(self.ts.tmp)

    def hook_env(self):
        return clean_env({
            "HERDR_TEAM_STATE_DIR": os.fspath(self.ts.state_root),
            "HERDR_SOCKET_PATH": os.fspath(self.ts.socket_path),
            "HERDR_PLUGIN_EVENT": "pane.agent_detected",
            "HERDR_PLUGIN_EVENT_JSON": json.dumps({"type": "pane_agent_detected", "pane_id": "w2:p1"}),
            "HERDR_PLUGIN_ID": "herdr-team",
            "HERDR_BIN_PATH": os.fspath(self.fake_herdr),
        })

    def start_sleeper(self):
        proc = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(proc.pid)], stdout=subprocess.PIPE).stdout.decode()
        self.assertTrue(start.strip())
        # Return it untrimmed on purpose: both the daemon and the sh gate must trim.
        return proc, start.rstrip("\n")

    def write_daemon_json(self, pid, start_time):
        self.ts.session.daemon_json.write_text(json.dumps({"pid": pid, "start_time": start_time, "beat_at": "now", "version": VERSION}) + "\n")

    def test_live_daemon_short_circuits_fast(self):
        proc, start = self.start_sleeper()
        self.write_daemon_json(proc.pid, start)
        # warm-up run, then measure
        run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        t0 = time.monotonic()
        result = run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        elapsed = time.monotonic() - t0
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self.assertLess(elapsed, 0.2, "gate took {:.3f}s".format(elapsed))

    def assert_reconciler_ran(self, result):
        # The slow path runs `herdr-team hook-event`, which exits 0 in every non-bug case and prints one JSON line.
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(payload["event"], "agent_detected")
        self.assertEqual(payload["pane_id"], "w2:p1")
        self.assertIn("skipped", payload)

    def test_dead_pid_falls_through_to_python(self):
        self.write_daemon_json(2 ** 22 - 1, "Thu Jan  1 00:00:00 2026")
        result = run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        self.assert_reconciler_ran(result)

    def test_start_time_mismatch_falls_through(self):
        proc, _start = self.start_sleeper()
        self.write_daemon_json(proc.pid, "Thu Jan  1 00:00:00 2026")
        result = run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        self.assert_reconciler_ran(result)

    def test_named_session_slug_in_gate(self):
        ts = TempState(slug="rig-3")
        self.addCleanup(ts.cleanup)
        proc, start = self.start_sleeper()
        ts.session.daemon_json.write_text(json.dumps({"pid": proc.pid, "start_time": start}) + "\n")
        env = self.hook_env()
        env["HERDR_TEAM_STATE_DIR"] = os.fspath(ts.state_root)
        env["HERDR_SOCKET_PATH"] = os.fspath(ts.socket_path)
        result = run([os.fspath(HOOK), "pane_closed"], env=env)
        self.assertEqual((result.returncode, result.stderr), (0, b""))

    def test_no_state_dir_falls_through(self):
        env = self.hook_env()
        del env["HERDR_TEAM_STATE_DIR"]
        result = run([os.fspath(HOOK), "agent_detected"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'"event"', result.stdout)


class ConsoleWrapperTests(unittest.TestCase):
    def test_wrapper_prints_hint_on_failure(self):
        # From a plain shell (no HERDR_PLUGIN_ENTRYPOINT_ID) the console refuses with not_a_plugin_pane (exit 1)
        # and the wrapper reports it.
        proc = subprocess.run(["sh", os.fspath(CONSOLE_SH)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=clean_env(), cwd=os.fspath(PLUGIN_ROOT))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(b"not_a_plugin_pane", proc.stderr)
        out = proc.stdout.decode()
        self.assertIn("exited with status 1", out)
        self.assertIn("herdr-team ui console", out)


class StubModuleTests(unittest.TestCase):
    def test_every_convention_module_exists(self):
        expected = [
            "paths", "api", "errors", "store", "sanitize", "render", "identity", "roster", "charter", "cli",
            "cmd_board", "cmd_roster", "cmd_misc", "cmd_hooks", "cmd_skill", "cmd_daemon", "daemon", "gate",
            "nudge", "ledger", "hooks", "claude_settings", "console", "picker", "compose", "tui_model",
        ]
        for name in expected:
            self.assertTrue((PLUGIN_ROOT / "herdr_team" / (name + ".py")).is_file(), name)

    def test_no_fcntl_outside_store(self):
        for path in (PLUGIN_ROOT / "herdr_team").glob("*.py"):
            if path.name == "store.py":
                continue
            self.assertNotIn("import fcntl", path.read_text(encoding="utf-8"), path.name)

    def test_no_match_statement_or_runtime_union(self):
        for path in (PLUGIN_ROOT / "herdr_team").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"^\s*match .*:\s*$", path.name)
            if "from __future__ import annotations" not in text and path.name != "__init__.py":
                self.fail("{} lacks the annotations future import".format(path.name))


if __name__ == "__main__":
    unittest.main()

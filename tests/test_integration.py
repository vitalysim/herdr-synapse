"""End-to-end, in-process: Workflow 1 across every module with ``TempState`` and ``FakeApi``.

One scenario, one temp state dir, no real Herdr and no daemon process:

1. ``create`` a team from two fake live agents (rename, label, tokens, pane records, briefing jobs);
2. the human sets the charter (``charter_updated`` system record);
3. a member posts from its pane (roster match by ``terminal_id``, ``role:`` expansion);
4. the human posts from a verified shell pane (process group + ancestry);
5. ``board --new`` from the worker pane advances its cursor to the last printed seq;
6. ``retract`` hides the post from the default board view;
7. ``who`` renders from a generated ``who.json`` while a "live" daemon.json points at this process;
8. the gate evaluates a detection fixture and ``nudge`` builds the exact nudge text;
9. the Claude hook shim, run through ``sh``, prints the board context for the worker.

Plus the packaging invariants the integrator verifies: manifest grammar, the
``[[events]]`` set versus the daemon subscriptions, no ``set -e`` in the shim,
``fcntl`` only in ``store.py``, no bare ``herdr`` outside ``api.py``, and no
forbidden strings in non-test code.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from herdr_team import claude_settings, cli, daemon, gate, hooks, identity, nudge, roster, store
from herdr_team.cmd_board import now_iso
from support import PLUGIN_ROOT, FakeApi, FakeError, FakeHerdrServer, TempState, fake_agent

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "detection"
TEAM = "beta"
REVIEWER_PANE, REVIEWER_TERM = "w5:p1", "term_51"
WORKER_PANE, WORKER_TERM = "w5:p2", "term_52"
SHELL_PANE, SHELL_TERM = "w3:p1", "term_sh"


def live_api():
    """agent.get/list, pane.get, agent.rename, pane.rename, pane.report_metadata over two fresh agents and a shell pane."""
    rows = [
        fake_agent(REVIEWER_PANE, REVIEWER_TERM, "codex", None),
        fake_agent(WORKER_PANE, WORKER_TERM, "claude", None),
    ]
    panes = {SHELL_PANE: {"terminal_id": SHELL_TERM, "agent": None, "workspace_id": "w3", "tab_id": "w3:t1"}}
    api = FakeApi()

    def agent_get(params):
        target = params.get("target")
        for row in rows:
            if target in (row["pane_id"], row.get("name")):
                return {"type": "agent_info", "agent": dict(row)}
        raise FakeError("agent_not_found", "agent target {} not found".format(target))

    def pane_get(params):
        pane_id = params.get("pane_id")
        for row in rows:
            if row["pane_id"] == pane_id:
                return {"pane": {"pane_id": pane_id, "terminal_id": row["terminal_id"], "agent": row["agent"], "workspace_id": row["workspace_id"], "tab_id": row["tab_id"], "focused": False}}
        if pane_id in panes:
            p = panes[pane_id]
            return {"pane": {"pane_id": pane_id, "terminal_id": p["terminal_id"], "agent": None, "workspace_id": p["workspace_id"], "tab_id": p["tab_id"], "focused": False}}
        raise FakeError("pane_not_found", "pane {} not found".format(pane_id))

    def rename(params):
        for row in rows:
            if row["pane_id"] == params["target"]:
                row["name"] = params.get("name")
                return {"type": "ok"}
        raise FakeError("agent_not_found", "no agent in {}".format(params["target"]))

    api.set_response("agent.list", lambda params: {"type": "agent_list", "agents": [dict(r) for r in rows]})
    api.set_response("agent.get", agent_get)
    api.set_response("pane.get", pane_get)
    api.set_response("agent.rename", rename)
    api.set_response("pane.rename", {"type": "ok"})
    api.rows = rows
    return api


def run_cli(argv, env, api):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("herdr_team.api.HerdrApi", lambda socket_path, env=None, **kw: api):
        code = cli.main(list(argv), env=env, stdout=out, stderr=err)
    payload = json.loads(out.getvalue()) if "--json" in argv and out.getvalue().strip() else out.getvalue()
    return code, payload, err.getvalue()


def write_live_daemon(ts):
    start = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, check=False).stdout.decode().strip()
    store.write_json(ts.session.daemon_json, {"pid": os.getpid(), "start_time": start, "beat_at": now_iso(), "socket": os.fspath(ts.socket_path), "socket_inode": 1, "version": "0.1.0", "herdr_version": "0.8.2", "protocol": 20})


class Workflow1Tests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState(write_team=False)
        self.addCleanup(self.ts.cleanup)
        self.api = live_api()
        self.base_env = self.ts.env_with(HERDR_TEAM_NO_DAEMON="1")

    def env(self, **overrides):
        env = dict(self.base_env)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def cli(self, argv, **overrides):
        code, payload, err = run_cli(argv, self.env(**overrides), self.api)
        return code, payload, err

    def test_workflow_one(self):
        ts, api = self.ts, self.api
        team_paths = ts.session.team(TEAM)

        # 1. create the team from two live agents ---------------------------------------
        code, created, err = self.cli(["--json", "create", TEAM, "--member", "{}:reviewer".format(REVIEWER_PANE), "--member", "{}:worker".format(WORKER_PANE), "--charter", "Fix the session bug."])
        self.assertEqual(code, 0, err)
        self.assertEqual([m["name"] for m in created["members"]], ["beta-reviewer", "beta-worker"])
        self.assertEqual({r["name"] for r in api.rows}, {"beta-reviewer", "beta-worker"}, "agent.rename applied the derived names")
        self.assertEqual([p["label"] for m, p in api.calls if m == "pane.rename"], ["team:beta/reviewer", "team:beta/worker"])
        tokens = [p for m, p in api.calls if m == "pane.report_metadata"]
        self.assertEqual(tokens[0]["tokens"], {"team": TEAM, "team_role": "reviewer"})
        self.assertTrue(ts.session.pane_record(REVIEWER_TERM).is_file())
        self.assertTrue(ts.session.pane_record(WORKER_TERM).is_file())
        self.assertEqual(len(os.listdir(team_paths.jobs_dir)), 2, "one briefing job per member")
        doc = store.RosterStore(team_paths).load()
        self.assertEqual(doc["charter"]["seq"], 1)
        self.assertEqual(roster.load_team(team_paths).find("beta-worker").terminal_id, WORKER_TERM)
        self.assertFalse(any(m in ("agent.prompt", "notification.show") for m, _ in api.calls), "only the daemon prompts or toasts")

        # 2. the human sets the charter (outside Herdr, --team) -----------------------------
        code, charter, err = self.cli(["--json", "--team", TEAM, "charter", "set", "Fix the session-isolation bug; reviewer reviews, worker patches."])
        self.assertEqual(code, 0, err)
        self.assertEqual(charter["charter"]["seq"], 2)
        records = store.BoardStore(team_paths).read()
        self.assertEqual([r["event"] for r in records], ["charter_updated", "charter_updated"])
        self.assertTrue(all(r["from"] == "system" and r["kind"] == "system" for r in records))
        # an agent pane may not touch it
        code, _, err = self.cli(["--json", "charter", "set", "mine now"], HERDR_PANE_ID=REVIEWER_PANE)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err.splitlines()[0])["code"], "author_mismatch")
        self.assertTrue(team_paths.audit_jsonl.is_file())

        # 3. a member posts from its pane; role addressing expands ---------------------------
        code, post1, err = self.cli(["--json", "post", "Diff ready, please review.", "--kind", "request", "--to", "role:worker"], HERDR_PANE_ID=REVIEWER_PANE)
        self.assertEqual(code, 0, err)
        self.assertEqual(post1["author"], {"name": "beta-reviewer", "via": "cli", "verified": True})
        self.assertEqual(post1["to"], ["beta-worker"])
        self.assertEqual(post1["to_role"], "worker")
        rec1 = store.BoardStore(team_paths).get(post1["seq"])
        self.assertEqual((rec1["from_kind"], rec1["from_pane"], rec1["from_terminal"]), ("codex", REVIEWER_PANE, REVIEWER_TERM))
        self.assertEqual(rec1["to_role"], "worker")

        # 4. the human posts from a verified shell pane --------------------------------------
        leader = os.getpgrp()
        fake_shell = 777
        shell_pid = os.getppid() if leader == os.getpid() else fake_shell
        api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {
            "pane_id": SHELL_PANE, "shell_pid": shell_pid, "foreground_process_group_id": leader,
            "foreground_processes": [{"pid": leader, "name": "python3"}],
        }})
        with mock.patch.object(identity, "ps_table", return_value={leader: fake_shell, os.getpid(): os.getppid()}):
            code, post2, err = self.cli(["--json", "post", "Ship it when green.", "--to", "beta-worker", "--name", "vitaly"], HERDR_PANE_ID=SHELL_PANE)
        self.assertEqual(code, 0, err)
        self.assertEqual(post2["author"], {"name": "human", "via": "cli", "verified": True})
        rec2 = store.BoardStore(team_paths).get(post2["seq"])
        self.assertEqual(rec2["from_label"], "vitaly")
        self.assertTrue(identity.human_origin_ok(rec2["origin"]))
        # --as human from an agent pane is refused and audited. The worker pane's process tree
        # must contain this process (a CLI run inside the pane); the step-4 shell-pane override
        # leaned on os.getpgrp() being a live ancestor, which fails once the launching shell has
        # exited (orphaned process group under a backgrounded runner: pane_mismatch instead).
        api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {
            "pane_id": WORKER_PANE, "shell_pid": os.getppid(), "foreground_process_group_id": os.getpid(),
            "foreground_processes": [{"pid": os.getpid(), "name": "python3"}],
        }})
        code, _, err = self.cli(["--json", "post", "x", "--as", "human"], HERDR_PANE_ID=WORKER_PANE)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err.splitlines()[0])["code"], "author_mismatch")

        # 5. board --new from the worker pane advances the cursor -----------------------------
        before = store.Cursors(team_paths).get("beta-worker")["seq"]
        code, new, err = self.cli(["--json", "board", "--new"], HERDR_PANE_ID=WORKER_PANE)
        self.assertEqual(code, 0, err)
        self.assertEqual(new["reader"], "beta-worker")
        seqs = [p["seq"] for p in new["posts"]]
        self.assertIn(post1["seq"], seqs)
        self.assertIn(post2["seq"], seqs)
        self.assertEqual(new["cursor"]["before"], before)
        self.assertTrue(new["cursor"]["advanced"])
        self.assertEqual(new["cursor"]["after"], max(seqs))
        self.assertEqual(store.Cursors(team_paths).get("beta-worker")["seq"], max(seqs))
        code, again, _ = self.cli(["--json", "board", "--new"], HERDR_PANE_ID=WORKER_PANE)
        self.assertEqual(again["count"], 0, "nothing new after the cursor advanced")
        # --peek never advances
        code, peek, _ = self.cli(["--json", "board", "--peek", "--since", "0"], HERDR_PANE_ID=REVIEWER_PANE)
        self.assertEqual(code, 0)
        self.assertFalse(peek["cursor"]["advanced"])

        # 6. retract: an appended record; the board view strikes the original ----------------
        code, retracted, err = self.cli(["--json", "retract", str(post1["seq"])], HERDR_PANE_ID=REVIEWER_PANE)
        self.assertEqual(code, 0, err)
        self.assertEqual(retracted["retracts"], post1["seq"])
        retract_rec = store.BoardStore(team_paths).get(retracted["seq"])
        self.assertEqual((retract_rec["kind"], retract_rec["retracts"], retract_rec["from"]), ("retract", post1["seq"], "beta-reviewer"))
        self.assertEqual(store.retracted_map(store.BoardStore(team_paths).read()), {post1["seq"]: retracted["seq"]})
        visible = store.filter_retracted(store.BoardStore(team_paths).read())
        self.assertNotIn(post1["seq"], [r["seq"] for r in visible])
        self.assertIn(post2["seq"], [r["seq"] for r in visible])
        code, board, _ = self.cli(["--json", "--team", TEAM, "board"])
        by_seq = {p["seq"]: p for p in board["posts"]}
        self.assertEqual(by_seq[retracted["seq"]]["retracts"], post1["seq"], "the retract record travels with the board")
        code, text, _ = self.cli(["--team", TEAM, "board"])
        self.assertIn("~~", text, "the retracted post renders struck through")
        self.assertIn("Ship it when green.", text)
        # a member cannot retract someone else's post
        code, _, err = self.cli(["--json", "retract", str(post2["seq"])], HERDR_PANE_ID=REVIEWER_PANE)
        self.assertEqual(code, 1)

        # 7. who renders from a generated who.json ------------------------------------------
        team = roster.load_team(team_paths)
        who_doc = roster.build_who_json({TEAM: team}, [dict(r) for r in api.rows], {TEAM: doc["charter"]}, {TEAM: {}}, {}, default_team=TEAM, socket=os.fspath(ts.socket_path))
        who_doc["teams"][TEAM]["members"][0]["last_headline"] = "→ review diff"
        store.write_json(ts.session.who_json, who_doc)
        write_live_daemon(ts)
        code, who, err = self.cli(["--json", "--team", TEAM, "who"])
        self.assertEqual(code, 0, err)
        self.assertEqual(who["source"], "who.json")
        self.assertTrue(who["daemon"]["alive"])
        self.assertEqual({m["name"] for m in who["members"]}, {"beta-reviewer", "beta-worker", "human"})
        self.assertEqual(who["charter"]["seq"], 2)
        code, text, _ = self.cli(["--team", TEAM, "who"])
        self.assertIn("team beta", text)
        self.assertIn("beta-reviewer", text)
        self.assertIn("charter #2", text)
        code, ascii_text, _ = self.cli(["--team", TEAM, "who", "--ascii"])
        self.assertNotIn("◐", ascii_text)

        # 8. the gate evaluates a fixture; the nudge text is built --------------------------
        now = 1_000_000.0
        base = dict(
            name="beta-worker", kind="claude", terminal_id=WORKER_TERM, pane_id=WORKER_PANE, agent_kind="claude",
            agent_status="idle", state_change_seq=7, launch_pending=False, focused=False, screen_detection_skipped=False,
            delivery="nudge", verified_kind=True, stable_since_ms=now - 5000, idle_since_ms=now - 120000,
            explain={"state": "idle", "matched_rule": {"id": "live_prompt_box", "region": "prompt_box_body"}, "visible_blocker": False, "skip_state_update": False, "skipped_update_reason": None},
        )
        pending = gate.PendingWork(seqs=[post2["seq"]], urgent=False, cursor_seq=0, authors=["human"])
        dialog = gate.MemberSnapshot(detection_text=(FIXTURES / "claude_permission_dialog.txt").read_text(encoding="utf-8"), **base)
        decision = daemon.gate_evaluate(dialog, pending, now, None, 0)
        self.assertFalse(decision.deliver)
        self.assertEqual(decision.hold, gate.HOLD_DIALOG)
        self.assertEqual(decision.matched_dialog_line, "Esc to cancel · Tab to amend · Ctrl+E to explain")
        idle = gate.MemberSnapshot(detection_text=(FIXTURES / "claude_idle.txt").read_text(encoding="utf-8"), **base)
        decision = daemon.gate_evaluate(idle, pending, now, None, 0)
        self.assertTrue(decision.deliver, decision)
        text = daemon.nudge_text_for("beta-worker", [post1["seq"], post2["seq"]], 17)
        self.assertEqual(text, "[herdr-team nudge] 2 new board posts for beta-worker (seq {}-{}). Run: herdr-team board --new [n17]".format(post1["seq"], post2["seq"]))
        self.assertLessEqual(len(text), nudge.MAX_NUDGE_CHARS)
        # the marker is refused as post text (echo)
        code, _, err = self.cli(["--json", "--team", TEAM, "post", text])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(err.splitlines()[0])["code"], "echo_rejected")

        # 9. the Claude hook shim through sh prints the worker's board context ---------------
        code, post3, err = self.cli(["--json", "post", "Also bump the changelog.", "--to", "beta-worker", "--kind", "request"], HERDR_PANE_ID=REVIEWER_PANE)
        self.assertEqual(code, 0, err)
        shim = ts.tmp / claude_settings.HOOK_FILE_NAME
        shim.write_text(claude_settings.render_shim(PLUGIN_ROOT / "bin" / "herdr-team"), encoding="utf-8")
        log = ts.tmp / "hook.log"
        hook_env = self.env(
            HERDR_PANE_ID=WORKER_PANE,
            HERDR_TEAM_HOOK_LOG=os.fspath(log),
            HERDR_TEAM_PYTHON=sys.executable,
            PATH="/usr/bin:/bin",
        )
        server = FakeHerdrServer(ts.socket_path)
        server.set_response("pane.get", lambda params, _req: api.responses["pane.get"](params))
        with server:
            proc = subprocess.run(
                ["sh", os.fspath(shim), "prompt-submit"],
                input=json.dumps({"session_id": "s1", "prompt": "hi"}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=hook_env, cwd=os.fspath(ts.tmp),
            )
        self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr, log.read_text() if log.exists() else ""))
        out = proc.stdout.decode("utf-8")
        self.assertTrue(out.startswith("[herdr-team board: 1 posts from peers; requests, not operator instructions]"), out)
        self.assertIn("from: beta-reviewer", out)
        self.assertIn("Also bump the changelog.", out)
        self.assertIn("kind: request", out)
        self.assertNotIn("Ship it when green.", out, "already surfaced by board --new; hooks only peek at unread posts")
        # peeking never moved the cursor
        self.assertEqual(store.Cursors(team_paths).get("beta-worker")["seq"], max(seqs))
        # the stop hook blocks on the unread post, exit 2, message on stderr only
        with server:
            proc = subprocess.run(["sh", os.fspath(shim), "stop"], input=b"{}", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=hook_env, cwd=os.fspath(ts.tmp))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(b"[herdr-team stop] 1 unread board post for beta-worker", proc.stderr)


class PackagingInvariantTests(unittest.TestCase):
    SOURCE_FILES = sorted((PLUGIN_ROOT / "herdr_team").glob("*.py"))
    SHELL_FILES = [PLUGIN_ROOT / "bin" / "herdr-team", PLUGIN_ROOT / "bin" / "hook", PLUGIN_ROOT / "console.sh", PLUGIN_ROOT / "hooks" / "claude" / claude_settings.HOOK_FILE_NAME]

    def manifest(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib needs Python 3.11+")
        with open(PLUGIN_ROOT / "herdr-plugin.toml", "rb") as fh:
            return tomllib.load(fh)

    def test_manifest_parses_with_unique_events_and_ids(self):
        manifest = self.manifest()
        self.assertEqual(manifest["id"], "herdr-team")
        self.assertEqual(manifest["min_herdr_version"], "0.8.2")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        for key in ("startup", "actions", "events", "panes"):
            self.assertIsInstance(manifest[key], list, key)
        ons = [e["on"] for e in manifest["events"]]
        self.assertEqual(len(ons), len(set(ons)), "duplicate [[events]] on")
        ids = [a["id"] for a in manifest["actions"]] + [p["id"] for p in manifest["panes"]]
        for entry in ids:
            self.assertRegex(entry, r"^[a-z][a-z0-9-]*$", entry)
        self.assertEqual(len([a["id"] for a in manifest["actions"]]), len(set(a["id"] for a in manifest["actions"])))
        for entry in manifest["startup"] + manifest["actions"] + manifest["events"] + manifest["panes"]:
            command = entry["command"]
            self.assertIsInstance(command, list)
            self.assertTrue(command[0].startswith("./") or command[0] == "sh", command)
            target = PLUGIN_ROOT / (command[1] if command[0] == "sh" else command[0])
            self.assertTrue(target.is_file(), target)
            if command[0] != "sh":
                self.assertTrue(os.access(target, os.X_OK), target)
        # the manifest is a regular file, never a symlink
        self.assertFalse((PLUGIN_ROOT / "herdr-plugin.toml").is_symlink())

    def test_manifest_events_equal_daemon_subscriptions_minus_daemon_only_kinds(self):
        manifest = self.manifest()
        daemon_only = {"pane.moved", "pane.focused", "pane.updated"}
        self.assertEqual(sorted(e["on"] for e in manifest["events"]), sorted(set(daemon.SUBSCRIPTIONS) - daemon_only))
        self.assertTrue(daemon_only.issubset(set(daemon.SUBSCRIPTIONS)))
        for entry in manifest["events"]:
            self.assertEqual(entry["command"][0], "./bin/hook")
            self.assertEqual(hooks.normalize_event_name(entry["on"]), entry["command"][1], entry)
            self.assertIn(entry["command"][1], hooks.EVENTS)

    def test_shell_files_never_set_e_and_pass_sh_n(self):
        for path in self.SHELL_FILES:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"^\s*set\s+-[a-z]*e", "{} must never set -e".format(path))
            proc = subprocess.run(["sh", "-n", os.fspath(path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            self.assertEqual(proc.returncode, 0, (path, proc.stderr))

    def test_fcntl_only_in_store(self):
        for path in self.SOURCE_FILES:
            text = path.read_text(encoding="utf-8")
            if path.name == "store.py":
                self.assertIn("import fcntl", text)
                continue
            self.assertNotRegex(text, r"^\s*(import fcntl|from fcntl)", path.name)
            self.assertNotIn("fcntl.", text, path.name)

    def test_no_forbidden_strings_or_bare_herdr_outside_api(self):
        forbidden = ("server stop", "pkill", ".plugins.lock")
        bare_herdr = re.compile(r"""(\[\s*["']herdr["']\s*[,\]]|(?<![\w/-])["']herdr["']\s*\+|shutil\.which\(\s*["']herdr["']\)|command -v herdr(?![\w-]))""")
        for path in self.SOURCE_FILES:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, "{} contains {!r}".format(path.name, needle))
            if path.name != "api.py":
                self.assertIsNone(bare_herdr.search(text), "{} invokes herdr outside api.py".format(path.name))
            self.assertNotRegex(text, r"subprocess\.\w+\(\s*\[\s*[\"']herdr[\"']", path.name)
        for path in self.SHELL_FILES + [PLUGIN_ROOT / "herdr-plugin.toml"]:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, "{} contains {!r}".format(path.name, needle))
            # code lines only: comments and printed hints may name herdr commands for the human
            code_lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#") and not l.lstrip().startswith(("printf", "echo"))]
            self.assertNotRegex("\n".join(code_lines), r"(^|[\s;&|(])herdr\s+(?!-team)", "{} runs a bare herdr".format(path.name))

    def test_no_stub_bodies_remain(self):
        for path in self.SOURCE_FILES:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"^\s+raise NotImplementedError\b", "{} still has a scaffold stub".format(path.name))

    def test_every_command_module_registers_and_ui_commands_exist(self):
        commands = cli.load_commands()
        names = {c.name for c in commands}
        for name in ("create", "add", "remove", "leave", "bind", "dissolve", "use", "teams", "rename", "me", "who", "audit", "charter", "brief",
                     "post", "board", "show", "retract", "edit", "task", "ack", "say",
                     "daemon", "doctor", "setup", "keys", "skill", "hooks", "install-cli", "view", "ui", "teardown", "gc", "prune",
                     "nudge", "mute", "unmute", "pause", "focus", "read", "hook-event", "console", "compose", "picker"):
            self.assertIn(name, names, name)
        self.assertTrue(all(type(c) is cli.Command for c in commands))


if __name__ == "__main__":
    unittest.main()

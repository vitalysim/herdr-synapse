"""Roster, charter, me, who, audit commands through ``cli.main`` with ``FakeApi`` (docs/cli.md sections 4 to 6)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from unittest import mock

from herdr_team import cli, paths, store
from herdr_team import cmd_roster
from support import FakeApi, FakeError, TempState, fake_agent


def run_cli(argv, env, api=None):
    out, err = io.StringIO(), io.StringIO()
    fake = api if api is not None else FakeApi()
    with mock.patch("herdr_team.api.HerdrApi", lambda socket_path, env=None, **kw: fake):
        code = cli.main(list(argv), env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def json_out(result):
    code, out, err = result
    return code, (json.loads(out) if out.strip() else None), (json.loads(err.splitlines()[0]) if err.strip() and err.lstrip().startswith("{") else err)


AGENTS = [
    fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer"),
    fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working"),
    fake_agent("wA:p6", "term_owner", "claude", None),
    fake_agent("w5:p1", "term_51", "codex", None),
    fake_agent("w5:p2", "term_52", "claude", "taken-name"),
    fake_agent("w5:p3", "term_53", "claude", None, launch_pending=True),
]
PANES = {a["pane_id"]: a for a in AGENTS}
PANES["w3:p1"] = {"pane_id": "w3:p1", "terminal_id": "term_sh", "agent": None, "workspace_id": "w3", "tab_id": "w3:t1"}


def live_api(agents=None):
    """A ``FakeApi`` whose agent.get/list, pane.get, rename calls follow ``AGENTS``."""
    rows = [dict(a) for a in (agents if agents is not None else AGENTS)]
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
        if pane_id in PANES:
            p = PANES[pane_id]
            return {"pane": {"pane_id": pane_id, "terminal_id": p["terminal_id"], "agent": p.get("agent"), "workspace_id": p["workspace_id"], "tab_id": p["tab_id"], "focused": False}}
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


def write_live_daemon(ts, beat_at=None):
    from herdr_team.cmd_board import now_iso

    start = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, check=False).stdout.decode().strip()
    store.write_json(ts.session.daemon_json, {"pid": os.getpid(), "start_time": start, "beat_at": beat_at or now_iso(), "socket": os.fspath(ts.socket_path), "socket_inode": 1, "version": "0.1.0", "herdr_version": "0.8.2", "protocol": 20})


def env_no_daemon(ts, **overrides):
    env = ts.env_with(**overrides)
    env[cmd_roster.NO_DAEMON_ENV] = "1"
    return env


class CreateFromLive(unittest.TestCase):
    def test_create_two_members_with_charter_and_briefs(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            env = env_no_daemon(ts)
            code, payload, err = json_out(run_cli(["--json", "create", "beta", "--member", "w5:p1:reviewer", "--member", "wA:p6:worker:bob", "--charter", "Fix the bug.", "--brief", "bob=Own the patch."], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["team"], "beta")
            self.assertTrue(payload["created"])
            self.assertEqual([m["name"] for m in payload["members"]], ["beta-reviewer", "bob"])
            self.assertEqual([m["role"] for m in payload["members"]], ["reviewer", "worker"])
            self.assertTrue(all(m["renamed"] for m in payload["members"]))
            self.assertEqual(payload["charter"], {"seq": 1, "headline": "Fix the bug."})
            self.assertEqual(payload["notifier"], "offline")
            self.assertTrue(payload["default_team"])
            self.assertEqual(len(payload["briefing_jobs"]), 2)
            self.assertNotIn("brief", payload["members"][0])
            # Herdr side: rename, label, tokens per member.
            renames = [p for m, p in api.calls if m == "agent.rename"]
            self.assertEqual([r["name"] for r in renames], ["beta-reviewer", "bob"])
            labels = [p["label"] for m, p in api.calls if m == "pane.rename"]
            self.assertEqual(labels, ["team:beta/reviewer", "team:beta/worker"])
            tokens = [p for m, p in api.calls if m == "pane.report_metadata"]
            self.assertEqual(tokens[0]["tokens"], {"team": "beta", "team_role": "reviewer"})
            self.assertEqual(tokens[0]["source"], "herdr-team:roster")
            self.assertNotIn("ttl_ms", tokens[0])
            # Files: team.json, briefing jobs, pane records, console default_team, charter record.
            team = ts.session.team("beta")
            doc = store.read_json(team.team_json)
            names = {m["name"]: m for m in doc["members"]}
            self.assertEqual(names["bob"]["brief"], "Own the patch.")
            self.assertEqual(names["bob"]["label"], "team:beta/worker")
            self.assertEqual(doc["charter"]["seq"], 1)
            self.assertEqual(len(os.listdir(team.jobs_dir)), 2)
            self.assertTrue(ts.session.pane_record("term_51").is_file())
            self.assertEqual(store.read_json(ts.session.console_json)["default_team"], "beta")
            records = store.BoardStore(team).read()
            self.assertEqual([r["event"] for r in records], ["charter_updated"])
            self.assertFalse(any(m == "notification.show" or m == "agent.prompt" for m, _ in api.calls))

    def test_validation_before_any_write(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            env = env_no_daemon(ts)
            cases = [
                (["create", "beta", "--member", "w5:p1:reviewer:codex"], "name_reserved"),
                (["create", "beta", "--member", "w5:p1:reviewer:human"], "name_reserved"),
                (["create", "beta", "--member", "w5:p1:reviewer:Bad Name"], "name_invalid"),
                (["create", "beta", "--member", "w5:p1:human"], "role_invalid"),
                (["create", "beta", "--member", "w5:p1:reviewer:taken-name"], "agent_name_taken"),
                (["create", "beta", "--member", "w5:p1:reviewer:x", "--member", "wA:p6:worker:x"], "name_taken"),
                (["create", "beta", "--member", "w3:p1:reviewer"], "not_an_agent"),
                (["create", "beta", "--member", "w5:p3:reviewer"], "launch_pending"),
                (["create", "beta", "--member", "w7:p9:reviewer"], "agent_not_found"),
                (["create", "Beta", "--member", "w5:p1"], "team_name_invalid"),
            ]
            for argv, expected in cases:
                calls_before = len(api.calls)
                code, _, err = json_out(run_cli(["--json"] + argv, env, api))
                self.assertEqual(code, 1, argv)
                self.assertEqual(err["code"], expected, argv)
                self.assertFalse(ts.session.team("beta").team_json.exists(), argv)
                self.assertFalse(any(m in ("agent.rename", "pane.rename", "pane.report_metadata") for m, _ in api.calls[calls_before:]), argv)
            self.assertEqual(ts.session.list_teams(), [])

    def test_member_claimed_leaves_no_team_and_steal_moves(self):
        with TempState() as ts:
            api = live_api()
            env = env_no_daemon(ts)
            code, _, err = json_out(run_cli(["--json", "create", "beta", "--member", "w2:p1:lead"], env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "member_claimed")
            self.assertEqual(err["owner_team"], "alpha")
            self.assertFalse(ts.session.team("beta").root.exists())
            code, payload, err = json_out(run_cli(["--json", "create", "beta", "--member", "w2:p1:lead", "--steal"], env, api))
            self.assertEqual(code, 0, err)
            alpha = store.read_json(ts.team.team_json)
            reviewer = [m for m in alpha["members"] if m["name"] == "alpha-reviewer"][0]
            self.assertEqual(reviewer["status"], "left")
            gone = [r for r in store.BoardStore(ts.team).read() if r.get("event") == "member_gone"]
            self.assertEqual(len(gone), 1)

    def test_team_exists_unless_reuse(self):
        with TempState() as ts:
            api = live_api()
            env = env_no_daemon(ts)
            code, _, err = json_out(run_cli(["--json", "create", "alpha", "--member", "w5:p1:tester"], env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "team_exists")
            code, payload, err = json_out(run_cli(["--json", "create", "alpha", "--member", "w5:p1:tester", "--reuse"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["members"][0]["name"], "alpha-tester")
            self.assertEqual(len([m for m in store.read_json(ts.team.team_json)["members"] if m["status"] == "active"]), 4)

    def test_from_workspace_waits_for_pending_and_defaults_roles_to_kind_labels(self):
        """RS-11 / plan 5.5: roles default to the kind label; a launch_pending agent is waited for (bounded)."""
        with TempState(write_team=False) as ts:
            api = live_api()
            env = env_no_daemon(ts)
            sleeps = []
            self.addCleanup(setattr, cmd_roster, "_sleep", cmd_roster._sleep)
            self.addCleanup(setattr, cmd_roster, "FROM_WORKSPACE_SETTLE_S", cmd_roster.FROM_WORKSPACE_SETTLE_S)
            cmd_roster._sleep = sleeps.append
            cmd_roster.FROM_WORKSPACE_SETTLE_S = 0.0  # one listing, no wait
            code, payload, err = json_out(run_cli(["--json", "create", "five", "--from-workspace", "w5"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual([m["pane_id"] for m in payload["members"]], ["w5:p1", "w5:p2"])
            self.assertEqual([m["role"] for m in payload["members"]], ["codex", "claude"])
            self.assertEqual([m["name"] for m in payload["members"]], ["five-codex", "five-claude"])
            self.assertEqual(payload["pending"], ["w5:p3"])
            self.assertIn("skipping w5:p3", err)
            self.assertEqual(sleeps, [])

    def test_from_workspace_ignores_names_the_batch_itself_vacates(self):
        """RS-11 live finding: a second codex already named ``<team>-codex-2`` made the whole batch fail
        with ``name_taken`` although its own rename vacates that name. The holder renames first."""
        rows = [
            fake_agent("w6:p1", "term_61", "codex", "six-codex-2"),
            fake_agent("w6:p2", "term_62", "codex", None),
        ]
        with TempState(write_team=False) as ts:
            api = live_api(rows)
            env = env_no_daemon(ts)

            def strict_rename(params):
                for row in api.rows:
                    if row.get("name") == params.get("name") and row["pane_id"] != params["target"]:
                        raise FakeError("agent_name_taken", "agent name {} is already used".format(params.get("name")))
                for row in api.rows:
                    if row["pane_id"] == params["target"]:
                        row["name"] = params.get("name")
                        return {"type": "ok"}
                raise FakeError("agent_not_found", "no agent in {}".format(params["target"]))

            api.set_response("agent.rename", strict_rename)
            self.addCleanup(setattr, cmd_roster, "FROM_WORKSPACE_SETTLE_S", cmd_roster.FROM_WORKSPACE_SETTLE_S)
            cmd_roster.FROM_WORKSPACE_SETTLE_S = 0.0
            code, payload, err = json_out(run_cli(["--json", "create", "six", "--from-workspace", "w6"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted((m["pane_id"], m["name"]) for m in payload["members"]), [("w6:p1", "six-codex"), ("w6:p2", "six-codex-2")])
            renames = [(p["target"], p["name"]) for m, p in api.calls if m == "agent.rename"]
            self.assertEqual(renames, [("w6:p1", "six-codex"), ("w6:p2", "six-codex-2")])
            # Reversed listing order: the unnamed agent derives the base name and the named one keeps its own.
        rows = [
            fake_agent("w6:p2", "term_62", "codex", None),
            fake_agent("w6:p1", "term_61", "codex", "six-codex-2"),
        ]
        with TempState(write_team=False) as ts:
            api = live_api(rows)
            env = env_no_daemon(ts)
            cmd_roster.FROM_WORKSPACE_SETTLE_S = 0.0
            code, payload, err = json_out(run_cli(["--json", "create", "six", "--from-workspace", "w6"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted((m["pane_id"], m["name"], m["renamed"]) for m in payload["members"]), [("w6:p1", "six-codex-2", False), ("w6:p2", "six-codex", True)])

    def test_explicit_names_that_swap_are_refused_before_any_write(self):
        rows = [
            fake_agent("w6:p1", "term_61", "codex", "alice"),
            fake_agent("w6:p2", "term_62", "claude", "bob"),
        ]
        with TempState(write_team=False) as ts:
            api = live_api(rows)
            env = env_no_daemon(ts)
            code, _, err = json_out(run_cli(["--json", "create", "six", "--member", "w6:p1:a:bob", "--member", "w6:p2:b:alice"], env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "name_taken")
            self.assertFalse(ts.session.team("six").root.exists())
            self.assertFalse(any(m in ("agent.rename", "pane.rename", "pane.report_metadata") for m, _ in api.calls))
            # One explicit name that another batch target vacates is fine: bob moves to carol first.
            code, payload, err = json_out(run_cli(["--json", "create", "six", "--member", "w6:p1:a:bob", "--member", "w6:p2:b:carol"], env, api))
            self.assertEqual(code, 0, err)
            renames = [(p["target"], p["name"]) for m, p in api.calls if m == "agent.rename"]
            self.assertEqual(renames, [("w6:p2", "carol"), ("w6:p1", "bob")])

    def test_from_workspace_settles_a_pending_agent(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            env = env_no_daemon(ts)
            listings = []

            def agent_list(_params):
                listings.append(1)
                rows = [dict(r) for r in api.rows]
                if len(listings) >= 3:
                    for row in rows:
                        if row["pane_id"] == "w5:p3":
                            row["launch_pending"] = False
                    for row in api.rows:
                        if row["pane_id"] == "w5:p3":
                            row["launch_pending"] = False
                return {"type": "agent_list", "agents": rows}

            api.set_response("agent.list", agent_list)
            sleeps = []
            self.addCleanup(setattr, cmd_roster, "_sleep", cmd_roster._sleep)
            cmd_roster._sleep = sleeps.append
            settled, pending = cmd_roster.settle_workspace_agents(api, "w5", timeout_s=30.0)
            self.assertEqual([r["pane_id"] for r in settled], ["w5:p1", "w5:p2", "w5:p3"])
            self.assertEqual(pending, [])
            self.assertEqual(sleeps, [0.5, 0.5])

    def test_names_plain_and_rename_flag(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            env = env_no_daemon(ts)
            code, payload, err = json_out(run_cli(["--json", "create", "beta", "--names", "plain", "--member", "w5:p1:reviewer", "--member", "w5:p2:worker"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual([m["name"] for m in payload["members"]], ["reviewer", "worker"])
            self.assertEqual(store.read_json(ts.session.team("beta").team_json)["naming"], "plain")
            code, _, err = json_out(run_cli(["--json", "create", "gamma", "--names", "plain", "--member", "w5:p1:dev", "--member", "w5:p2:dev", "--steal"], env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "name_invalid")

    def test_second_team_keeps_default_unless_use(self):
        with TempState() as ts:
            api = live_api()
            env = env_no_daemon(ts)
            store.write_json(ts.session.console_json, {"default_team": "alpha"})
            code, payload, err = json_out(run_cli(["--json", "create", "beta", "--member", "w5:p1:reviewer"], env, api))
            self.assertEqual(code, 0, err)
            self.assertFalse(payload["default_team"])
            self.assertIn("default team stays 'alpha'", err)
            self.assertEqual(store.read_json(ts.session.console_json)["default_team"], "alpha")
            code, payload, _ = json_out(run_cli(["--json", "create", "gamma", "--member", "w5:p2:worker", "--use"], env, api))
            self.assertTrue(payload["default_team"])
            self.assertEqual(store.read_json(ts.session.console_json)["default_team"], "gamma")

    def test_charter_from_agent_pane_refused(self):
        with TempState() as ts:
            api = live_api()
            env = env_no_daemon(ts, HERDR_PANE_ID="w2:p1")
            code, _, err = json_out(run_cli(["--json", "create", "beta", "--member", "w5:p1:reviewer", "--charter", "x"], env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "author_mismatch")
            self.assertFalse(ts.session.team("beta").team_json.exists())

    def test_usage_errors(self):
        with TempState(write_team=False) as ts:
            env = env_no_daemon(ts)
            code, _, err = json_out(run_cli(["--json", "create", "beta"], env, live_api()))
            self.assertEqual(code, 2)
            code, _, err = json_out(run_cli(["--json", "create", "beta", "--new", "--member", "w5:p1"], env, live_api()))
            self.assertEqual(code, 2)


class CreateNew(unittest.TestCase):
    def test_layout_request_shape(self):
        leaves = [{"role": "reviewer", "kind": "codex", "cwd": None, "name": "t-reviewer"}, {"role": "worker", "kind": "claude", "cwd": "/tmp/w", "name": "t-worker"}, {"role": "qa", "kind": "codex", "cwd": None, "name": "t-qa"}]
        request = cmd_roster.build_layout_request("t", leaves, "/state/teams/t", None)
        self.assertEqual(request["tab_label"], "team:t")
        self.assertFalse(request["focus"])
        self.assertNotIn("workspace_id", request)
        root = request["root"]
        self.assertEqual(root["type"], "split")
        self.assertEqual(root["direction"], "right")
        self.assertEqual(root["first"]["type"], "split")
        self.assertEqual(root["first"]["direction"], "down")
        leaf = root["first"]["first"]
        self.assertEqual(leaf["type"], "pane")
        self.assertEqual(leaf["label"], "team:t/reviewer")
        self.assertEqual(leaf["env"], {"HERDR_TEAM": "t", "HERDR_TEAM_ROLE": "reviewer", "HERDR_TEAM_MEMBER": "t-reviewer", "HERDR_TEAM_DIR": "/state/teams/t"})
        self.assertNotIn("cwd", leaf)
        self.assertEqual(root["first"]["second"]["cwd"], "/tmp/w")
        self.assertEqual(cmd_roster.layout_pane_ids({"type": "split", "first": {"type": "pane", "pane_id": "a"}, "second": {"type": "pane", "pane_id": "b"}}), ["a", "b"])
        with self.assertRaises(Exception):
            cmd_roster.build_layout_request("t", [dict(leaves[0]) for _ in range(25)], "/x")
        self.assertEqual(cmd_roster.agent_start_argv("t-qa", "codex", "w9:p3"), ["agent", "start", "t-qa", "--kind", "codex", "--pane", "w9:p3", "--timeout", "60000"])

    def test_spawn_spec(self):
        self.assertEqual(cmd_roster.parse_spawn_spec("reviewer:codex"), ("reviewer", "codex", None))
        self.assertEqual(cmd_roster.parse_spawn_spec("worker:claude:/tmp/a:b"), ("worker", "claude", "/tmp/a:b"))
        code = None
        try:
            cmd_roster.parse_spawn_spec("worker:nokind")
        except Exception as err:
            code = getattr(err, "code", None)
        self.assertEqual(code, "kind_unknown")

    def test_new_flow_with_fakes(self):
        with TempState(write_team=False) as ts:
            api = live_api()
            env = env_no_daemon(ts)
            started = []

            def layout_apply(params):
                api.rows.append(fake_agent("w9:p1", "term_n1", "codex", None))
                api.rows.append(fake_agent("w9:p2", "term_n2", "claude", None))
                return {"type": "layout_apply", "layout": {"workspace_id": "w9", "tab_id": "w9:t1", "zoomed": False, "focused_pane_id": "w9:p1", "root": {"type": "split", "direction": "right", "ratio": 0.5, "first": {"type": "pane", "pane_id": "w9:p1"}, "second": {"type": "pane", "pane_id": "w9:p2"}}}}

            api.set_response("layout.apply", layout_apply)
            api.set_cli(["agent", "start"], 0, "{}", "")
            code, payload, err = json_out(run_cli(["--json", "create", "delta", "--new", "--workspace", "w9", "--spawn", "reviewer:codex", "--spawn", "worker:claude:/tmp/work"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual([m["name"] for m in payload["members"]], ["delta-reviewer", "delta-worker"])
            self.assertTrue(all(m["managed"] for m in payload["members"]))
            self.assertEqual(api.runs, [["agent", "start", "delta-reviewer", "--kind", "codex", "--pane", "w9:p1", "--timeout", "60000"], ["agent", "start", "delta-worker", "--kind", "claude", "--pane", "w9:p2", "--timeout", "60000"]])
            request = [p for m, p in api.calls if m == "layout.apply"][0]
            self.assertEqual(request["workspace_id"], "w9")
            self.assertEqual(request["root"]["second"]["cwd"], "/tmp/work")
            self.assertEqual(request["root"]["first"]["env"]["HERDR_TEAM_DIR"], os.fspath(ts.session.team("delta").root))


class StartAgent(unittest.TestCase):
    """``_start_agent`` reads the real ``herdr agent start`` envelope (src/cli/agent.rs) and retries ``agent_pane_busy``."""

    STARTED = {"type": "agent_started", "agent": fake_agent("w9:p1", "term_new", "codex", "delta-reviewer", launch_pending=False), "argv": ["codex"]}

    def test_agent_started_envelope_yields_terminal_id(self):
        api = live_api()
        api.set_cli_result(cmd_roster.agent_start_argv("delta-reviewer", "codex", "w9:p1"), self.STARTED, request_id="cli:agent:start")
        out = cmd_roster._start_agent(api, "delta-reviewer", "codex", "w9:p1", sleep=lambda _s: None)
        self.assertEqual(out["pane_id"], "w9:p1")
        self.assertTrue(out["started"])
        self.assertEqual(out["terminal_id"], "term_new")
        self.assertEqual(out["agent"]["name"], "delta-reviewer")
        self.assertEqual(api.runs, [["agent", "start", "delta-reviewer", "--kind", "codex", "--pane", "w9:p1", "--timeout", "60000"]])

    def test_bare_or_non_json_stdout_still_counts_as_started(self):
        for stdout in ("{}", "", "started\n", json.dumps({"id": "x", "result": {"type": "ok"}})):
            api = live_api()
            api.set_cli(["agent", "start"], 0, stdout, "")
            out = cmd_roster._start_agent(api, "delta-reviewer", "codex", "w9:p1", sleep=lambda _s: None)
            self.assertEqual(out, {"pane_id": "w9:p1", "started": True, "terminal_id": None, "agent": None}, stdout)

    def test_started_agent_parser(self):
        self.assertIsNone(cmd_roster._started_agent(""))
        self.assertIsNone(cmd_roster._started_agent("{}"))
        self.assertIsNone(cmd_roster._started_agent("not json"))
        self.assertIsNone(cmd_roster._started_agent(json.dumps({"id": "x", "error": {"code": "agent_pane_busy", "message": "busy"}})))
        self.assertIsNone(cmd_roster._started_agent(json.dumps({"id": "x", "result": {"type": "agent_info", "agent": {"terminal_id": "t"}}})))
        agent = cmd_roster._started_agent(json.dumps({"id": "cli:agent:start", "result": self.STARTED}))
        self.assertEqual(agent["terminal_id"], "term_new")
        # a bare (unwrapped) agent_started object is accepted too
        self.assertEqual(cmd_roster._started_agent(json.dumps(self.STARTED))["terminal_id"], "term_new")

    def test_busy_retries_until_the_pane_frees(self):
        api = live_api()
        argv = cmd_roster.agent_start_argv("delta-reviewer", "codex", "w9:p1")
        busy = json.dumps({"id": "cli:agent:start", "error": {"code": "agent_pane_busy", "message": "pane w9:p1 already hosts an agent"}}) + "\n"
        attempts = []
        real_run = api.run

        def run(args, timeout=3.0, pin_socket=True):
            attempts.append(list(args))
            if len(attempts) < 3:
                api.set_cli(argv, 1, "", busy)
            else:
                api.set_cli_result(argv, self.STARTED, request_id="cli:agent:start")
            return real_run(args, timeout, pin_socket)

        api.run = run
        sleeps = []
        out = cmd_roster._start_agent(api, "delta-reviewer", "codex", "w9:p1", sleep=sleeps.append)
        self.assertEqual(out["terminal_id"], "term_new")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [0.5, 0.5])
        # the pane was probed between attempts and reported no agent (agent_not_found -> keep waiting)
        self.assertEqual([p for m, p in api.calls if m == "agent.get"], [{"target": "w9:p1"}, {"target": "w9:p1"}])

    def test_busy_with_a_live_agent_is_refused(self):
        api = live_api()
        busy = json.dumps({"id": "cli:agent:start", "error": {"code": "agent_pane_busy", "message": "pane w5:p1 already hosts an agent"}}) + "\n"
        api.set_cli(["agent", "start"], 1, "", busy)
        sleeps = []
        with self.assertRaises(Exception) as ctx:
            cmd_roster._start_agent(api, "x", "codex", "w5:p1", sleep=sleeps.append)
        self.assertEqual(getattr(ctx.exception, "code", None), "agent_pane_busy")
        self.assertEqual(sleeps, [])
        self.assertEqual(len(api.runs), 1)

    def test_other_failures_raise_agent_start_failed(self):
        api = live_api()
        api.set_cli_error(["agent", "start"], "kind_unknown", "unknown agent kind zzz")
        with self.assertRaises(Exception) as ctx:
            cmd_roster._start_agent(api, "x", "zzz", "w9:p1", sleep=lambda _s: None)
        self.assertEqual(getattr(ctx.exception, "code", None), "agent_start_failed")
        self.assertIn("kind_unknown", ctx.exception.message)
        self.assertEqual(ctx.exception.details["pane_id"], "w9:p1")

    def test_spawn_failed_member_keeps_the_started_terminal(self):
        """When the agent never settles, the failed record points at the terminal ``agent_started`` named, not the pre-start guess."""
        with TempState(write_team=False) as ts:
            api = live_api()
            env = env_no_daemon(ts)

            def layout_apply(params):
                # the pane exists (pane.get answers) but no agent ever appears in it
                PANES["w9:p1"] = {"pane_id": "w9:p1", "terminal_id": "term_guess", "agent": None, "workspace_id": "w9", "tab_id": "w9:t1"}
                self.addCleanup(PANES.pop, "w9:p1", None)
                return {"type": "layout_apply", "layout": {"workspace_id": "w9", "tab_id": "w9:t1", "zoomed": False, "focused_pane_id": "w9:p1", "root": {"type": "pane", "pane_id": "w9:p1"}}}

            api.set_response("layout.apply", layout_apply)
            api.set_cli_result(["agent", "start"], self.STARTED, request_id="cli:agent:start")
            clock = [0.0]

            def monotonic():
                clock[0] += 30.0
                return clock[0]

            self.addCleanup(setattr, cmd_roster, "_sleep", cmd_roster._sleep)
            self.addCleanup(setattr, cmd_roster, "_monotonic", cmd_roster._monotonic)
            cmd_roster._sleep = lambda _s: None
            cmd_roster._monotonic = monotonic
            code, payload, err = json_out(run_cli(["--json", "create", "delta", "--new", "--workspace", "w9", "--spawn", "reviewer:codex"], env, api))
            self.assertEqual(code, 0, err)
            member = payload["members"][0]
            self.assertEqual(member["status"], "failed")
            self.assertEqual(member["terminal_id"], "term_new")
            self.assertIn("marked failed", err)
            saved = [m for m in store.read_json(ts.session.team("delta").team_json)["members"] if m["name"] == "delta-reviewer"][0]
            self.assertEqual(saved["terminal_id"], "term_new")
            self.assertEqual(saved["status"], "failed")


class MemberCommands(unittest.TestCase):
    def test_add_remove_rename_bind(self):
        with TempState() as ts:
            api = live_api()
            env = env_no_daemon(ts)
            code, payload, err = json_out(run_cli(["--json", "add", "alpha", "w5:p1", "--role", "tester", "--as", "tess", "--brief", "Test it."], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["member"]["name"], "tess")
            self.assertTrue(payload["renamed"])
            self.assertIn("briefing_job", payload)
            code, payload, err = json_out(run_cli(["--json", "rename", "tess", "tessa", "--team", "alpha"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"team": "alpha", "old": "tess", "new": "tessa"})
            renamed = [r for r in store.BoardStore(ts.team).read() if r.get("event") == "renamed"]
            self.assertEqual(len(renamed), 1)
            doc = store.read_json(ts.team.team_json)
            member = [m for m in doc["members"] if m["name"] == "tessa"][0]
            self.assertEqual(member["previous_names"][0]["name"], "tess")
            code, payload, err = json_out(run_cli(["--json", "remove", "alpha", "tess"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"team": "alpha", "removed": "tessa", "tokens_cleared": True, "name_cleared": True})
            doc = store.read_json(ts.team.team_json)
            self.assertEqual([m for m in doc["members"] if m["name"] == "tessa"][0]["status"], "left")
            gone = [r for r in store.BoardStore(ts.team).read() if r.get("event") == "member_gone"]
            self.assertEqual(len(gone), 1)
            clears = [p for m, p in api.calls if m == "pane.report_metadata" and p["pane_id"] == "w5:p1" and p["tokens"].get("team") is None]
            self.assertTrue(clears)
            # bind a missing member to a new live agent of the same kind
            doc["members"][0]["status"] = "missing"
            store.write_json(ts.team.team_json, doc)
            code, payload, err = json_out(run_cli(["--json", "bind", "alpha", "alpha-reviewer", "w5:p1"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["previous_terminal_id"], "term_r1")
            self.assertEqual(payload["member"]["terminal_id"], "term_51")
            self.assertEqual(payload["member"]["generation"], 2)
            self.assertEqual(payload["member"]["status"], "active")

    def test_remove_unknown_member(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "remove", "alpha", "nobody"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "member_not_found")
            self.assertIn("alpha-reviewer", err["roster"])

    def test_leave_from_member_pane(self):
        with TempState() as ts:
            api = live_api()
            code, payload, err = json_out(run_cli(["--json", "leave"], env_no_daemon(ts, HERDR_PANE_ID="w2:p2"), api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"team": "alpha", "left": "alpha-worker"})
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "leave"], env_no_daemon(ts), api))
            self.assertEqual(code, 3)

    def test_dissolve_use_teams(self):
        with TempState() as ts:
            api = live_api()
            env = env_no_daemon(ts)
            code, teams, _ = json_out(run_cli(["--json", "teams"], env, api))
            self.assertEqual(teams["session"], "default")
            self.assertEqual(teams["teams"][0]["team"], "alpha")
            self.assertEqual(teams["teams"][0]["members"], 3)
            self.assertTrue(teams["teams"][0]["default"])
            self.assertFalse(teams["teams"][0]["running"])
            code, _, err = json_out(run_cli(["--json", "use", "nope"], env, api))
            self.assertEqual(err["code"], "team_not_found")
            code, payload, _ = json_out(run_cli(["--json", "use", "alpha"], env, api))
            self.assertEqual(payload, {"default_team": "alpha"})
            code, _, err = json_out(run_cli(["--json", "dissolve", "alpha"], env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "confirmation_required")
            code, payload, err = json_out(run_cli(["--json", "dissolve", "alpha", "--yes"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["members_cleared"], 2)
            self.assertTrue(payload["archived_to"].startswith(os.fspath(ts.session.archive_dir)))
            self.assertFalse(ts.team.root.exists())
            self.assertEqual(ts.session.list_teams(), [])
            self.assertNotIn("default_team", store.read_json(ts.session.console_json))
            code, _, err = json_out(run_cli(["--json", "dissolve", "alpha", "--yes"], env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual(code, 3)

    def test_teams_works_offline(self):
        with TempState() as ts:
            api = FakeApi()
            api.unreachable = True
            code, teams, _ = json_out(run_cli(["--json", "teams"], ts.env, api))
            self.assertEqual(code, 0)
            self.assertEqual(len(teams["teams"]), 1)


class MeWhoAudit(unittest.TestCase):
    def test_me_from_member_pane(self):
        with TempState() as ts:
            api = live_api()
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            run_cli(["--json", "--team", "alpha", "post", "for reviewer", "--to", "alpha-reviewer"], ts.env, api)
            code, payload, err = json_out(run_cli(["--json", "me"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["name"], "alpha-reviewer")
            self.assertEqual(payload["role"], "reviewer")
            self.assertEqual(payload["kind"], "codex")
            self.assertEqual(payload["pane_id"], "w2:p1")
            self.assertEqual(payload["terminal_id"], "term_r1")
            self.assertEqual(payload["charter"], {"seq": 1, "headline": "Find and fix the bug."})
            self.assertEqual([t["name"] for t in payload["teammates"]], ["alpha-worker", "human"])
            self.assertEqual(payload["unread"], 1)
            self.assertEqual(payload["cursor"], 0)
            self.assertTrue(payload["verified"])
            self.assertEqual(payload["via"], "cli")
            self.assertEqual(payload["skill_version"], 1)
            self.assertIsNone(payload["skill_installed"])
            self.assertFalse(payload["skill_ok"])
            self.assertTrue(payload["cli"].endswith("bin/herdr-team"))
            self.assertEqual(payload["notifier"], "offline")
            code, out, _ = run_cli(["me"], env, api)
            self.assertIn("you are alpha-reviewer (reviewer, codex) in team alpha", out)

    def test_me_detects_installed_skill(self):
        with TempState() as ts:
            skill = ts.home / ".claude" / "skills" / "herdr-team" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: herdr-team\n---\n<!-- herdr-team skill v1, cli >= 0.1 -->\n")
            code, payload, _ = json_out(run_cli(["--json", "me"], ts.env_with(HERDR_PANE_ID="w2:p1"), live_api()))
            self.assertEqual(payload["skill_installed"], 1)
            self.assertTrue(payload["skill_ok"])

    def test_me_from_human_is_not_a_member(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "me"], ts.env, live_api()))
            self.assertEqual(code, 3)
            self.assertEqual(err["code"], "not_a_member")
            self.assertIn("alpha", err["hint"])

    def _who_json(self, ts):
        return {
            "v": 1, "daemon_beat_at": "2026-09-04T13:53:10.000Z", "socket": os.fspath(ts.socket_path), "default_team": "alpha",
            "charters": {"alpha": {"seq": 1, "headline": "Find and fix the bug.", "refs": []}},
            "teams": {"alpha": {"members": [
                {"name": "alpha-reviewer", "role": "reviewer", "kind": "codex", "status": "active", "agent_status": "idle", "pane_id": "w2:p1", "terminal_id": "term_r1", "workspace_id": "w2", "terminal_title_stripped": None, "last_headline": "→ review diff", "pending_nudges": 2, "muted_until": None, "verified_kind": True, "delivery": "nudge", "hooks_last_seen": None, "last_seen_at": "2026-09-04T13:53:00.000Z", "briefed": True, "charter_stale": False},
                {"name": "alpha-worker", "role": "worker", "kind": "claude", "status": "active", "agent_status": "working", "pane_id": "w2:p2", "terminal_id": "term_w1", "workspace_id": "w2", "terminal_title_stripped": None, "last_headline": None, "pending_nudges": 0, "muted_until": None, "verified_kind": False, "delivery": "hooks", "hooks_last_seen": None, "last_seen_at": None, "briefed": False, "charter_stale": True},
            ]}},
        }

    def test_who_reads_who_json_when_daemon_alive(self):
        with TempState() as ts:
            write_live_daemon(ts)
            store.write_json(ts.session.who_json, self._who_json(ts))
            api = live_api()
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["source"], "who.json")
            self.assertTrue(payload["daemon"]["alive"])
            self.assertEqual(payload["daemon"]["pid"], os.getpid())
            self.assertEqual(payload["charter"]["headline"], "Find and fix the bug.")
            self.assertEqual(payload["view"], "off")
            self.assertEqual(payload["toasts"], "off")
            self.assertEqual(payload["nudges"], "on")
            self.assertEqual(payload["unread_for_you"], 0)
            self.assertEqual([m["name"] for m in payload["members"]], ["alpha-reviewer", "alpha-worker"])
            self.assertEqual(payload["members"][0]["pending_nudges"], 2)
            self.assertIn("brief", payload["members"][0])
            self.assertIn("unread", payload["members"][0])
            self.assertFalse(any(m in ("agent.list", "agent.read") for m, _ in api.calls))
            code, out, _ = run_cli(["--team", "alpha", "who"], ts.env, api)
            self.assertIn("team alpha (w2)", out)
            self.assertIn("charter #1: Find and fix the bug.", out)
            self.assertIn("↪2", out)
            self.assertIn("charter: stale", out)
            code, out, _ = run_cli(["--team", "alpha", "who", "--ascii", "--brief"], ts.env, api)
            self.assertIn("nudges:2", out)
            self.assertNotIn("↪", out)
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "who", "--role", "worker"], ts.env, api))
            self.assertEqual([m["name"] for m in payload["members"]], ["alpha-worker"])

    def test_who_falls_back_to_agent_list_when_daemon_dead(self):
        with TempState() as ts:
            store.write_json(ts.session.who_json, self._who_json(ts))
            store.write_json(ts.session.daemon_json, {"pid": 2 ** 22 + 12345, "start_time": "x", "beat_at": "2026-09-04T13:53:10.000Z", "socket": "", "version": "0.1.0"})
            api = live_api()
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["source"], "agent-list")
            self.assertFalse(payload["daemon"]["alive"])
            self.assertEqual(payload["members"][0]["agent_status"], "idle")
            self.assertEqual(payload["members"][1]["agent_status"], "working")
            self.assertTrue(any(m == "agent.list" for m, _ in api.calls))
            self.assertFalse(any(m == "agent.read" for m, _ in api.calls))

    def test_who_stale_beat_falls_back(self):
        with TempState() as ts:
            write_live_daemon(ts, beat_at="2026-09-04T00:00:00.000Z")
            store.write_json(ts.session.who_json, self._who_json(ts))
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, live_api()))
            self.assertEqual(payload["source"], "agent-list")

    def test_who_unreachable_shows_roster_only(self):
        with TempState() as ts:
            api = FakeApi()
            api.unreachable = True
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, api))
            self.assertEqual(code, 0)
            self.assertEqual(payload["source"], "unreachable")
            self.assertIsNone(payload["members"][0]["agent_status"])
            self.assertIn("cannot reach Herdr", err)
            code, out, _ = run_cli(["--team", "alpha", "who"], ts.env, api)
            self.assertIn("cannot reach Herdr", out)

    def test_audit_lists_entries(self):
        with TempState() as ts:
            api = live_api()
            run_cli(["--json", "post", "x", "--as", "human"], ts.env_with(HERDR_PANE_ID="w2:p1"), api)
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "audit"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["team"], "alpha")
            self.assertEqual(payload["entries"][-1]["event"], "author_mismatch")
            self.assertEqual(payload["entries"][-1]["author"], "alpha-reviewer")
            self.assertEqual(payload["entries"][-1]["via"], "cli")
            self.assertEqual(payload["entries"][-1]["pane_id"], "w2:p1")
            code, payload, _ = json_out(run_cli(["--json", "audit", "alpha", "--last", "0"], ts.env, api))
            self.assertEqual(payload["entries"], [])


class CharterAndBrief(unittest.TestCase):
    def test_print_set_history(self):
        with TempState() as ts:
            api = live_api()
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "charter"], ts.env, api))
            self.assertEqual(payload["charter"]["seq"], 1)
            self.assertEqual(payload["charter"]["text"], "Find and fix the bug.")
            code, out, _ = run_cli(["--team", "alpha", "charter"], ts.env, api)
            self.assertIn("Find and fix the bug.", out)
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "charter", "set", "Reviewer owns the fix review."], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["charter"]["seq"], 2)
            self.assertEqual(payload["charter"]["updated_by"], "human")
            self.assertEqual(payload["record_seq"], 1)
            self.assertFalse(payload["urgent"])
            record = store.BoardStore(ts.team).get(1)
            self.assertEqual(record["from"], "system")
            self.assertEqual(record["event"], "charter_updated")
            self.assertEqual(record["to"], ["all"])
            self.assertEqual(record["text"], "Reviewer owns the fix review.")
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "charter", "set", "Urgent change.", "--urgent"], ts.env, api))
            self.assertTrue(payload["urgent"])
            self.assertTrue(store.BoardStore(ts.team).get(2)["urgent"])
            code, payload, _ = json_out(run_cli(["--json", "charter", "history", "--team", "alpha"], ts.env, api))
            self.assertEqual([h["charter_seq"] for h in payload["history"]], [2, 3])
            self.assertEqual(payload["history"][0]["text"], "Reviewer owns the fix review.")
            code, payload, _ = json_out(run_cli(["--json", "charter", "alpha"], ts.env, api))
            self.assertEqual(payload["charter"]["seq"], 3)
            # members see charter: stale until ack
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, api))
            self.assertTrue(payload["members"][0]["charter_stale"])
            run_cli(["--json", "ack"], ts.env_with(HERDR_PANE_ID="w2:p1"), api)
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, api))
            self.assertFalse(payload["members"][0]["charter_stale"])

    def test_set_from_agent_pane_refused_and_audited(self):
        with TempState() as ts:
            api = live_api()
            code, _, err = json_out(run_cli(["--json", "charter", "set", "mine now"], ts.env_with(HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "author_mismatch")
            self.assertEqual(store.read_json(ts.team.team_json)["charter"]["seq"], 1)
            entries = [json.loads(l) for l in ts.team.audit_jsonl.read_bytes().decode().splitlines() if l.strip()]
            self.assertEqual(entries[-1]["event"], "author_mismatch")
            code, _, err = json_out(run_cli(["--json", "charter", "set", "mine now", "--as", "human"], ts.env_with(HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual(code, 2)

    def test_set_from_hook_refused(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PLUGIN_EVENT="pane.agent_detected", HERDR_PLUGIN_ID="herdr-team", HERDR_TEAM="alpha")
            code, _, err = json_out(run_cli(["--json", "charter", "set", "x"], env, live_api()))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "author_mismatch")

    def test_too_long_and_file(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "charter", "set", "x" * 2001], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "charter_too_long")
            self.assertEqual(err["hint"], "--charter-file")
            spec = ts.tmp / "spec.md"
            spec.write_text("Long charter\n" + "detail " * 400)
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "charter", "set", "--file", os.fspath(spec)], ts.env))
            self.assertEqual(code, 0, err)
            self.assertIn("charter.md", payload["charter"]["refs"])
            self.assertTrue(ts.team.charter_md.is_file())
            self.assertLessEqual(len(payload["charter"]["text"]), 2000)

    def test_charter_edit_without_tty(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "charter", "edit"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "no_tty")

    def test_brief_set_and_enqueue(self):
        with TempState() as ts:
            api = live_api()
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "brief", "alpha-worker", "--set", "Own the patch."], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"team": "alpha", "member": "alpha-worker", "brief": "Own the patch."})
            code, _, err = json_out(run_cli(["--json", "brief", "alpha-worker", "--set", "x"], ts.env_with(HERDR_PANE_ID="w2:p1"), api))
            self.assertEqual(err["code"], "author_mismatch")
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "brief", "alpha-worker"], ts.env, api))
            self.assertEqual(code, 5)
            self.assertEqual(err["code"], "daemon_down")
            write_live_daemon(ts)
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "brief", "alpha-worker"], ts.env, api))
            self.assertEqual(code, 0, err)
            jobs = os.listdir(ts.team.jobs_dir)
            self.assertEqual(len(jobs), 1)
            job = store.read_json(ts.team.jobs_dir / jobs[0])
            self.assertEqual(job["kind"], "brief")
            self.assertEqual(job["member"], "alpha-worker")
            self.assertEqual(job["requested_by"]["name"], "human")
            code, out, _ = run_cli(["--team", "alpha", "brief", "alpha-worker", "--format", "context"], ts.env, api)
            self.assertTrue(out.startswith("[herdr-team briefing context for alpha-worker"))
            self.assertIn("your brief: Own the patch.", out)


class Registry(unittest.TestCase):
    def test_all_roster_commands_registered(self):
        names = {c.name for c in cli.load_commands()}
        for name in ("create", "add", "remove", "leave", "bind", "dissolve", "use", "teams", "rename", "me", "who", "audit", "charter", "brief"):
            self.assertIn(name, names)

    def test_member_spec_parsing(self):
        spec = cmd_roster.parse_member_spec("w2:p1:reviewer:alice")
        self.assertEqual((spec.target, spec.role, spec.name), ("w2:p1", "reviewer", "alice"))
        spec = cmd_roster.parse_member_spec("w2:p1")
        self.assertEqual((spec.target, spec.role, spec.name), ("w2:p1", None, None))
        spec = cmd_roster.parse_member_spec("alice-agent:worker")
        self.assertEqual((spec.target, spec.role, spec.name), ("alice-agent", "worker", None))

    def test_member_spec_accepts_base36_pane_ids(self):
        # M5 rig: the tenth pane of a workspace is ``w1:pA``; ``w1:pA:a:f2`` used to be read as
        # target ``w1`` with three trailing parts and refused with a usage error.
        spec = cmd_roster.parse_member_spec("w1:pA:a:f2")
        self.assertEqual((spec.target, spec.role, spec.name), ("w1:pA", "a", "f2"))
        spec = cmd_roster.parse_member_spec("wA:pF")
        self.assertEqual((spec.target, spec.role, spec.name), ("wA:pF", None, None))
        spec = cmd_roster.parse_member_spec("w1:pB:worker")
        self.assertEqual((spec.target, spec.role, spec.name), ("w1:pB", "worker", None))


if __name__ == "__main__":
    unittest.main()

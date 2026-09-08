"""Author resolution tiers (plan 4.3, docs/cli.md section 3)."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict, Optional
from unittest import mock

from support import FakeApi, FakeError, TempState

from herdr_team import identity, store
from herdr_team.errors import EXIT_REFUSED, EXIT_UNREACHABLE, HerdrTeamError


def pane_info(pane_id: str, terminal_id: str, agent: Optional[str], focused: bool = False, **extra: Any) -> Dict[str, Any]:
    ws = pane_id.split(":")[0]
    pane = {
        "pane_id": pane_id, "terminal_id": terminal_id, "workspace_id": ws, "tab_id": ws + ":t1",
        "focused": focused, "agent": agent, "agent_status": "idle" if agent else "unknown", "revision": 1,
        "label": None, "cwd": "/tmp/work", "tokens": {}, "state_labels": {},
    }
    pane.update(extra)
    return {"type": "pane_info", "pane": pane}


PANES: Dict[str, Dict[str, Any]] = {
    "w2:p1": pane_info("w2:p1", "term_r1", "codex"),           # alpha-reviewer
    "w2:p2": pane_info("w2:p2", "term_w1", "claude", focused=True),  # alpha-worker
    "wA:p6": pane_info("wA:p6", "term_owner", "claude"),       # unnamed, not in any team
    "w3:p9": pane_info("w3:p9", "term_shell", None),           # plain shell
    "w7:p1": pane_info("w7:p1", "term_console", None),         # console pane
}


def make_api() -> FakeApi:
    api = FakeApi()

    def pane_get(params: Dict[str, Any]) -> Dict[str, Any]:
        pane_id = params.get("pane_id")
        if pane_id in PANES:
            return PANES[pane_id]
        if pane_id == "w1:p3":  # alias of the moved reviewer pane
            return PANES["w2:p1"]
        raise FakeError("pane_not_found", "pane {} not found".format(pane_id))

    api.set_response("pane.get", pane_get)
    return api


def own_process_info(pane_id: str, shell_pid: Optional[int] = None) -> Dict[str, Any]:
    """Process info that makes this test process the pane's foreground job."""
    return {
        "type": "pane_process_info",
        "process_info": {
            "pane_id": pane_id, "shell_pid": shell_pid if shell_pid is not None else os.getppid(), "tty": "/dev/ttys001",
            "foreground_process_group_id": os.getpgrp(),
            "foreground_processes": [{"pid": os.getpid(), "name": "python3"}],
        },
    }


def own_shell_rig(pane_id: str):
    """``(process_info, ps_table)`` under which this test process verifies as ``pane_id``'s shell job.

    The process running the tests is not always the leader of its own process
    group (it is not under some harnesses), so the leader is mapped to a fake
    shell pid in the table, exactly as ``test_process_group_match_is_verified`` does.
    """
    leader = os.getpgrp()
    fake_shell = 777
    shell_pid = os.getppid() if leader == os.getpid() else fake_shell
    info = {"type": "pane_process_info", "process_info": {
        "pane_id": pane_id, "shell_pid": shell_pid, "foreground_process_group_id": leader,
        "foreground_processes": [{"pid": leader, "name": "python3"}],
    }}
    return info, {leader: fake_shell, os.getpid(): os.getppid()}


class SystemTierTests(unittest.TestCase):
    def test_hook_env_is_system_via_hook(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PLUGIN_EVENT="pane.agent_detected", HERDR_PLUGIN_ID="herdr-synapse", HERDR_PANE_ID="w2:p1")
            author = identity.resolve_author(env, ts.layout, make_api())
            self.assertEqual(author.name, "system")
            self.assertTrue(author.is_system)
            self.assertFalse(author.is_member)
            self.assertEqual(author.via, identity.VIA_HOOK)
            self.assertTrue(author.verified)
            self.assertEqual(author.tier, identity.TIER_SYSTEM)
            self.assertEqual(author.origin["event"], "pane.agent_detected")
            self.assertEqual(author.origin["pane_id"], "w2:p1")

    def test_startup_env_without_event_is_system_via_system(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PLUGIN_ID="herdr-synapse")
            author = identity.resolve_author(env, ts.layout, make_api())
            self.assertEqual(author.via, identity.VIA_SYSTEM)
            self.assertEqual(author.name, "system")

    def test_system_refuses_as_human(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PLUGIN_EVENT="pane.closed", HERDR_TEAM="alpha")
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(env, ts.layout, make_api(), as_human=True)
            self.assertEqual(ctx.exception.code, "author_mismatch")
            self.assertEqual(ctx.exception.exit_code, EXIT_REFUSED)
            entries = identity.read_audit(ts.layout, "alpha")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["author"], "system")


class NonDefaultTeamMemberTests(unittest.TestCase):
    """M5 rig finding: with ``default_team`` = alpha, a member of team beta was ``not_a_member`` from its own pane."""

    def _beta(self, ts: TempState) -> None:
        from herdr_team import paths, roster

        beta = ts.session.team("beta")
        paths.ensure_team_dirs(beta)
        store.write_json(beta.team_json, {
            "schema": 1, "team": "beta", "created_at": "2026-09-05T12:00:00Z", "socket": os.fspath(ts.socket_path),
            "state_dir": os.fspath(ts.state_root), "naming": "plain", "revision": 1, "charter": None,
            "members": [
                {"name": "f1", "role": "a", "kind": "claude", "terminal_id": "term_f1", "pane_id": "w1:pA", "workspace_id": "w1",
                 "tab_id": "w1:t1", "status": "active", "generation": 1, "delivery": "nudge"},
                {"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"},
            ],
        })
        roster.write_pane_record(ts.session, "term_f1", "beta", "f1", 1)
        store.write_json(ts.session.console_json, {"default_team": "alpha"})

    def _api(self) -> FakeApi:
        api = make_api()
        api.set_response("pane.get", lambda p: pane_info("w1:pA", "term_f1", "claude"))
        return api

    def test_default_team_hint_does_not_hide_a_member_of_another_team(self) -> None:
        with TempState() as ts:
            self._beta(ts)
            env = ts.env_with(HERDR_PANE_ID="w1:pA")
            author = identity.resolve_author(env, ts.layout, self._api(), team="alpha", team_explicit=False)
            self.assertEqual((author.name, author.team, author.via, author.verified), ("f1", "beta", identity.VIA_CLI, True))

    def test_explicit_team_stays_strict(self) -> None:
        with TempState() as ts:
            self._beta(ts)
            env = ts.env_with(HERDR_PANE_ID="w1:pA")
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(env, ts.layout, self._api(), team="alpha")
            self.assertEqual(ctx.exception.code, "not_a_member")

    def test_cmd_board_wrapper_marks_the_default_team_hint_as_implicit(self) -> None:
        import argparse

        from herdr_team import cmd_board

        with TempState() as ts:
            self._beta(ts)
            args = argparse.Namespace(team=None, session=None, socket=None, json=True, env=ts.env_with(HERDR_PANE_ID="w1:pA"))
            author = cmd_board.resolve_author(args, ts.layout, self._api(), team="alpha")
            self.assertEqual((author.name, author.team), ("f1", "beta"))
            args = argparse.Namespace(team="alpha", session=None, socket=None, json=True, env=ts.env_with(HERDR_PANE_ID="w1:pA"))
            with self.assertRaises(HerdrTeamError) as ctx:
                cmd_board.resolve_author(args, ts.layout, self._api(), team="alpha")
            self.assertEqual(ctx.exception.code, "not_a_member")


class ConsoleTierTests(unittest.TestCase):
    def _console_env(self, ts: TempState, pane_id: str = "w7:p1") -> Dict[str, str]:
        return ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID="console", HERDR_PLUGIN_ID="herdr-synapse", HERDR_PANE_ID=pane_id)

    def test_focused_console_is_verified_human(self) -> None:
        with TempState() as ts:
            store.write_json(ts.session.console_json, {"pane_id": "w7:p1", "terminal_id": "term_console", "pid": os.getpid(), "open": True, "default_team": "alpha"})
            api = make_api()
            api.set_response("pane.get", lambda p: pane_info("w7:p1", "term_console", None, focused=True))
            api.set_response("pane.process_info", own_process_info("w7:p1"))
            with mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid()}):
                author = identity.resolve_author(self._console_env(ts), ts.layout, api)
            self.assertTrue(author.is_human)
            self.assertEqual(author.via, identity.VIA_CONSOLE)
            self.assertTrue(author.verified)
            self.assertEqual(author.team, "alpha")
            self.assertEqual(author.terminal_id, "term_console")
            self.assertEqual(author.tier, identity.TIER_CONSOLE)
            self.assertTrue(identity.human_origin_ok(author.origin))

    def test_unfocused_console_is_unverified_but_human(self) -> None:
        with TempState() as ts:
            store.write_json(ts.session.console_json, {"pane_id": "w7:p1", "terminal_id": "term_console", "pid": os.getpid(), "open": True})
            api = make_api()
            api.set_response("pane.process_info", own_process_info("w7:p1"))
            with mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid()}):
                author = identity.resolve_author(self._console_env(ts), ts.layout, api)
            self.assertEqual(author.via, identity.VIA_CONSOLE_UNFOCUSED)
            self.assertFalse(author.verified)
            self.assertIn("not focused", author.reason or "")
            self.assertTrue(identity.human_origin_ok(author.origin))

    def test_console_in_the_wrong_terminal_is_cli_unverified(self) -> None:
        with TempState() as ts:
            store.write_json(ts.session.console_json, {"pane_id": "w7:p1", "terminal_id": "term_other", "pid": 1, "open": True})
            api = make_api()
            author = identity.resolve_author(self._console_env(ts), ts.layout, api)
            self.assertTrue(author.is_human)
            self.assertEqual(author.via, identity.VIA_CLI_UNVERIFIED)
            self.assertFalse(author.verified)
            self.assertIn("term_other", author.reason or "")

    def test_console_without_pane_id_stays_unverified(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID="console", HERDR_PLUGIN_ID="herdr-synapse")
            author = identity.resolve_author(env, ts.layout, make_api())
            self.assertEqual(author.via, identity.VIA_CONSOLE_UNFOCUSED)
            self.assertFalse(author.verified)

    def test_popup_entrypoint_is_human_via_popup(self) -> None:
        with TempState() as ts:
            store.write_json(ts.session.console_json, {"default_team": "alpha"})
            context = json.dumps({"focused_pane_id": "w2:p1", "nonce": "abc123"})
            env = ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID="compose", HERDR_PLUGIN_ID="herdr-synapse", HERDR_PLUGIN_CONTEXT_JSON=context)
            api = make_api()
            author = identity.resolve_author(env, ts.layout, api)
            self.assertEqual(author.via, identity.VIA_POPUP)
            self.assertFalse(author.verified)
            self.assertEqual(author.team, "alpha")
            self.assertEqual(author.origin["focused_pane_id"], "w2:p1")
            self.assertEqual(author.origin["nonce"], "abc123")
            # one process-tree check, nothing else: a popup used to make no
            # socket call at all, which is what let an agent claim to be one
            self.assertEqual([m for m, _p in api.calls], ["pane.list"])


class AgentPaneTests(unittest.TestCase):
    def test_roster_member_by_terminal_id_is_verified(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            author = identity.resolve_author(env, ts.layout, make_api())
            self.assertEqual(author.name, "alpha-reviewer")
            self.assertEqual(author.kind, "codex")
            self.assertTrue(author.is_member)
            self.assertEqual(author.via, identity.VIA_CLI)
            self.assertTrue(author.verified)
            self.assertEqual(author.team, "alpha")
            self.assertEqual(author.terminal_id, "term_r1")
            self.assertEqual(author.generation, 1)
            self.assertEqual(author.tier, identity.TIER_PANE)
            self.assertEqual(author.origin["via"], "cli")
            self.assertTrue(author.origin["verified"])
            self.assertEqual(author.origin["ancestry"], "unavailable")

    def test_moved_pane_alias_resolves_through_pane_get(self) -> None:
        """``HERDR_PANE_ID`` still names the old id; ``agent.get`` on it would fail, ``pane.get`` tolerates it."""
        with TempState() as ts:
            api = make_api()
            seen = []

            def agent_get(params: Dict[str, Any]) -> Dict[str, Any]:
                seen.append(params["target"])
                if params["target"] == "w1:p3":
                    raise FakeError("agent_not_found", "agent target w1:p3 not found")
                from support import FAKE_AGENTS
                return {"type": "agent_info", "agent": dict(FAKE_AGENTS[0])}

            api.set_response("agent.get", agent_get)
            env = ts.env_with(HERDR_PANE_ID="w1:p3")
            author = identity.resolve_author(env, ts.layout, api)
            self.assertEqual(author.name, "alpha-reviewer")
            self.assertEqual(author.pane_id, "w2:p1")
            self.assertEqual(author.origin["claimed_pane_id"], "w1:p3")
            self.assertEqual(author.origin["pane_id"], "w2:p1")
            self.assertEqual(seen, ["w2:p1"])  # agent.get used the current id, never the alias

    def test_env_workspace_mismatch_is_recorded(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1", HERDR_WORKSPACE_ID="w9", HERDR_TAB_ID="w2:t1")
            author = identity.resolve_author(env, ts.layout, make_api())
            self.assertEqual(author.origin["env_mismatch"], {"HERDR_WORKSPACE_ID": {"env": "w9", "pane": "w2"}})

    def test_as_human_from_agent_pane_is_refused_and_audited(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(env, ts.layout, make_api(), as_human=True)
            err = ctx.exception
            self.assertEqual(err.code, "author_mismatch")
            self.assertEqual(err.exit_code, EXIT_REFUSED)
            self.assertEqual(err.details["author"], "alpha-reviewer")
            self.assertIn("--relayed-for human", err.message)
            entries = identity.read_audit(ts.layout, "alpha")
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["event"], "author_mismatch")
            self.assertEqual(entry["author"], "alpha-reviewer")
            self.assertEqual(entry["pane_id"], "w2:p1")
            self.assertEqual(entry["details"]["requested"], "human")
            self.assertEqual(entry["details"]["kind"], "codex")
            self.assertEqual(identity.read_audit(ts.layout, "alpha", last=0), [])

    def test_agent_pane_not_in_any_team_is_not_a_member(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="wA:p6")
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(env, ts.layout, make_api())
            err = ctx.exception
            self.assertEqual(err.code, "not_a_member")
            self.assertEqual(err.exit_code, EXIT_UNREACHABLE)
            self.assertEqual(err.details["hint"], {"teams": ["alpha"]})
            self.assertEqual(err.details["agent"], "claude")

    def test_as_human_from_non_member_agent_pane_is_still_author_mismatch(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="wA:p6", HERDR_TEAM="alpha")
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(env, ts.layout, make_api(), as_human=True)
            self.assertEqual(ctx.exception.code, "author_mismatch")
            self.assertEqual(identity.read_audit(ts.layout, "alpha")[0]["details"]["member"], False)

    def test_herdr_team_member_env_is_an_unverified_fallback(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="wA:p6", HERDR_TEAM="alpha", HERDR_TEAM_MEMBER="alpha-lead", HERDR_TEAM_KIND="claude")
            author = identity.resolve_author(env, ts.layout, make_api())
            self.assertEqual(author.name, "alpha-lead")
            self.assertEqual(author.via, identity.VIA_CLI)
            self.assertFalse(author.verified)
            self.assertEqual(author.team, "alpha")
            self.assertEqual(author.origin["source"], "env:HERDR_TEAM_MEMBER")
            self.assertIn("HERDR_TEAM_MEMBER", author.reason or "")

    def test_invalid_herdr_team_member_env_is_ignored(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="wA:p6", HERDR_TEAM="alpha", HERDR_TEAM_MEMBER="Human!")
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(env, ts.layout, make_api())
            self.assertEqual(ctx.exception.code, "not_a_member")

    def test_pane_mismatch_when_ancestry_is_definitively_wrong(self) -> None:
        """S-01: ``HERDR_PANE_ID`` forged onto another member's agent pane."""
        with TempState() as ts:
            api = make_api()
            api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {
                "pane_id": "w2:p1", "shell_pid": 40000, "foreground_process_group_id": 40001,
                "foreground_processes": [{"pid": 40001, "name": "codex"}],
            }})
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            with mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid(), os.getppid(): 1}):
                with self.assertRaises(HerdrTeamError) as ctx:
                    identity.resolve_author(env, ts.layout, api)
            self.assertEqual(ctx.exception.code, "pane_mismatch")
            self.assertEqual(ctx.exception.exit_code, EXIT_REFUSED)
            entries = identity.read_audit(ts.layout, "alpha")
            self.assertEqual(entries[0]["event"], "pane_mismatch")

    def test_confirmed_ancestry_is_recorded(self) -> None:
        with TempState() as ts:
            api = make_api()
            api.set_response("pane.process_info", own_process_info("w2:p1"))
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            with mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid()}):
                author = identity.resolve_author(env, ts.layout, api)
            self.assertEqual(author.origin["ancestry"], "confirmed")

    def test_live_name_drift_is_reported_as_a_reason(self) -> None:
        with TempState() as ts:
            api = make_api()
            from support import FAKE_AGENTS
            renamed = dict(FAKE_AGENTS[0])
            renamed["name"] = "reviewer-renamed"
            api.set_response("agent.get", {"type": "agent_info", "agent": renamed})
            author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w2:p1"), ts.layout, api)
            self.assertEqual(author.name, "alpha-reviewer")
            self.assertIn("reviewer-renamed", author.reason or "")
            self.assertEqual(author.origin["live_name"], "reviewer-renamed")

    def test_pane_get_failure_is_not_a_member_exit_3(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w9:p9")
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(env, ts.layout, make_api())
            self.assertEqual(ctx.exception.code, "not_a_member")
            self.assertEqual(ctx.exception.exit_code, EXIT_UNREACHABLE)

    def test_server_down_raises_exit_3_unless_offline_allowed(self) -> None:
        with TempState() as ts:
            api = make_api()
            api.unreachable = True
            env = ts.env_with(HERDR_PANE_ID="w2:p1", HERDR_TEAM="alpha", HERDR_TEAM_MEMBER="alpha-reviewer")
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(env, ts.layout, api)
            self.assertEqual(ctx.exception.code, "server_not_running")
            self.assertEqual(ctx.exception.exit_code, EXIT_UNREACHABLE)
            author = identity.resolve_author(env, ts.layout, api, require_server=False)
            self.assertEqual(author.name, "alpha-reviewer")
            self.assertFalse(author.verified)
            self.assertEqual(author.origin["source"], "env:HERDR_TEAM_MEMBER")
            plain = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w2:p1"), ts.layout, api, require_server=False)
            self.assertTrue(plain.is_human)
            self.assertEqual(plain.via, identity.VIA_CLI_UNVERIFIED)

    def test_relayed_for_human_from_member(self) -> None:
        with TempState() as ts:
            author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w2:p1"), ts.layout, make_api(), relayed_for="human")
            self.assertEqual(author.relayed_for, "human")
            self.assertEqual(author.origin["relayed_for"], "human")
            self.assertEqual(author.to_json()["relayed_for"], "human")

    def test_relayed_for_rejects_other_values(self) -> None:
        with TempState() as ts:
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(ts.env_with(HERDR_PANE_ID="w2:p1"), ts.layout, make_api(), relayed_for="alpha-worker")
            self.assertEqual(ctx.exception.code, "usage")
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_label_is_ignored_for_members(self) -> None:
        with TempState() as ts:
            author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w2:p1"), ts.layout, make_api(), label="vitaly")
            self.assertIsNone(author.from_label)
            self.assertEqual(author.origin["ignored_label"], "vitaly")
            self.assertIn("--name ignored", author.reason or "")


class ShellPaneTests(unittest.TestCase):
    def test_ancestry_mismatch_is_unverified_never_refused(self) -> None:
        with TempState() as ts:
            api = make_api()
            api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {
                "pane_id": "w3:p9", "shell_pid": 50000, "foreground_process_group_id": 50001, "foreground_processes": [],
            }})
            author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w3:p9"), ts.layout, api)
            self.assertTrue(author.is_human)
            self.assertEqual(author.via, identity.VIA_CLI_UNVERIFIED)
            self.assertFalse(author.verified)
            self.assertIn("not ours", author.reason or "")
            self.assertEqual(author.terminal_id, "term_shell")
            self.assertEqual(author.origin["pgrp"], os.getpgrp())
            self.assertFalse(identity.human_origin_ok(author.origin))

    def test_process_info_unavailable_is_unverified(self) -> None:
        with TempState() as ts:
            author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w3:p9"), ts.layout, make_api())
            self.assertEqual(author.via, identity.VIA_CLI_UNVERIFIED)
            self.assertIn("process-info unavailable", author.reason or "")

    def test_process_group_match_is_verified(self) -> None:
        with TempState() as ts:
            leader = os.getpgrp()
            fake_shell = 777
            shell_pid = os.getppid() if leader == os.getpid() else fake_shell
            api = make_api()
            api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {
                "pane_id": "w3:p9", "shell_pid": shell_pid, "foreground_process_group_id": leader,
                "foreground_processes": [{"pid": leader, "name": "python3"}],
            }})
            with mock.patch.object(identity, "ps_table", return_value={leader: fake_shell, os.getpid(): os.getppid()}):
                author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w3:p9"), ts.layout, api, label="vitaly")
            self.assertEqual(author.via, identity.VIA_CLI)
            self.assertTrue(author.verified)
            self.assertIsNone(author.reason)
            self.assertEqual(author.from_label, "vitaly")
            self.assertTrue(identity.human_origin_ok(author.origin))

    def test_leader_parent_mismatch_is_unverified(self) -> None:
        with TempState() as ts:
            leader = os.getpgrp()
            api = make_api()
            api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {
                "pane_id": "w3:p9", "shell_pid": 1, "foreground_process_group_id": leader, "foreground_processes": [],
            }})
            table = {leader: 999999, os.getpid(): 999999}
            with mock.patch.object(identity, "ps_table", return_value=table):
                author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w3:p9"), ts.layout, api)
            self.assertEqual(author.via, identity.VIA_CLI_UNVERIFIED)
            self.assertIn("not a child of the pane shell", author.reason or "")

    def test_default_team_comes_from_console_json(self) -> None:
        with TempState() as ts:
            store.write_json(ts.session.console_json, {"default_team": "alpha"})
            author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w3:p9"), ts.layout, make_api())
            self.assertEqual(author.team, "alpha")

    def test_dead_member_pane_is_human_with_former_member_origin(self) -> None:
        with TempState() as ts:
            from herdr_team import roster
            roster.Roster(ts.layout, "alpha").set_status("alpha-worker", "left")
            api = make_api()
            api.set_response("pane.get", lambda p: pane_info("w2:p2", "term_w1", None))
            author = identity.resolve_author(ts.env_with(HERDR_PANE_ID="w2:p2"), ts.layout, api)
            self.assertTrue(author.is_human)
            self.assertEqual(author.origin["former_member"], {"team": "alpha", "name": "alpha-worker"})
            self.assertEqual(author.team, "alpha")

    def test_invalid_label_is_refused(self) -> None:
        with TempState() as ts:
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(ts.env_with(HERDR_PANE_ID="w3:p9"), ts.layout, make_api(), label="bad label!")
            self.assertEqual(ctx.exception.code, "label_invalid")

    def test_human_cannot_relay_for_human(self) -> None:
        with TempState() as ts:
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(ts.env_with(HERDR_PANE_ID="w3:p9"), ts.layout, make_api(), relayed_for="human")
            self.assertEqual(ctx.exception.code, "author_mismatch")


class OutsideTierTests(unittest.TestCase):
    def test_outside_requires_a_team_hint(self) -> None:
        with TempState() as ts:
            with self.assertRaises(HerdrTeamError) as ctx:
                identity.resolve_author(ts.env_with(), ts.layout, make_api())
            self.assertEqual(ctx.exception.code, "team_required")
            self.assertEqual(ctx.exception.exit_code, EXIT_UNREACHABLE)
            self.assertEqual(ctx.exception.details["teams"], ["alpha"])

    def test_outside_with_team_is_unverified_human(self) -> None:
        with TempState() as ts:
            api = make_api()
            author = identity.resolve_author(ts.env_with(), ts.layout, api, team="alpha", label="vitaly")
            self.assertTrue(author.is_human)
            self.assertEqual(author.via, identity.VIA_OUTSIDE)
            self.assertFalse(author.verified)
            self.assertEqual(author.team, "alpha")
            self.assertEqual(author.tier, identity.TIER_OUTSIDE)
            self.assertIsNone(author.from_label)  # labels need a verified path
            self.assertEqual(author.origin["ignored_label"], "vitaly")
            # One call: "is this process inside an agent's pane?" A caller outside
            # Herdr answers no, and stays the human it always was.
            self.assertEqual([m for m, _p in api.calls], ["pane.list"])
            self.assertTrue(identity.human_origin_ok(author.origin))

    def test_outside_with_herdr_team_env(self) -> None:
        with TempState() as ts:
            author = identity.resolve_author(ts.env_with(HERDR_TEAM="alpha"), ts.layout, make_api())
            self.assertEqual(author.team, "alpha")

    def test_outside_with_team_dir_env(self) -> None:
        with TempState() as ts:
            env = ts.env_with(HERDR_TEAM_DIR=os.fspath(ts.team.root))
            from herdr_team import paths
            layout = paths.resolve_layout(env)
            author = identity.resolve_author(env, layout, make_api())
            self.assertEqual(author.team, "alpha")
            self.assertEqual(author.via, identity.VIA_OUTSIDE)


class HelperTests(unittest.TestCase):
    def test_ancestry_walks_the_table(self) -> None:
        table = {10: 9, 9: 8, 8: 1}
        self.assertEqual(identity.ancestry(10, table), [10, 9, 8, 1])
        self.assertEqual(identity.ancestry(10, table, max_depth=1), [10, 9])
        self.assertEqual(identity.ancestry(42, {42: 42}), [42])
        own = identity.ancestry(os.getpid(), None)
        self.assertEqual(own, [os.getpid(), os.getppid()])

    def test_ps_table_reads_real_processes(self) -> None:
        table = identity.ps_table()
        self.assertIsNotNone(table)
        assert table is not None
        self.assertEqual(table.get(os.getpid()), os.getppid())

    def test_verify_shell_ancestry_reasons(self) -> None:
        api = FakeApi()
        api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w3:p9", "shell_pid": 5, "foreground_process_group_id": None}})
        ok, reason = identity.verify_shell_ancestry(api, "w3:p9", os.getpgrp())
        self.assertFalse(ok)
        self.assertIn("no foreground process group", reason or "")
        api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w3:p9", "shell_pid": None, "foreground_process_group_id": os.getpgrp()}})
        ok, reason = identity.verify_shell_ancestry(api, "w3:p9", os.getpgrp())
        self.assertFalse(ok)
        self.assertIn("no shell pid", reason or "")
        api.set_error("pane.process_info", "pane_not_found", "nope")
        ok, reason = identity.verify_shell_ancestry(api, "w3:p9", os.getpgrp())
        self.assertFalse(ok)
        self.assertIn("unavailable", reason or "")

    def test_confirm_pane_ancestry_outcomes(self) -> None:
        api = FakeApi()
        api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w1:p1", "shell_pid": 300, "foreground_process_group_id": 301, "foreground_processes": [{"pid": 301, "name": "sh"}]}})
        self.assertEqual(identity.confirm_pane_ancestry(api, "w1:p1", own_pid=305, table={305: 301, 301: 300}), (True, None))
        confirmed, reason = identity.confirm_pane_ancestry(api, "w1:p1", own_pid=305, table={305: 200, 200: 1})
        self.assertFalse(confirmed)
        self.assertIn("not a descendant", reason or "")
        api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w1:p1"}})
        confirmed, reason = identity.confirm_pane_ancestry(api, "w1:p1", own_pid=305, table={})
        self.assertIsNone(confirmed)

    def test_author_json_and_flags(self) -> None:
        author = identity.Author("alpha-worker", "claude", identity.VIA_CLI, True, pane_id="w2:p2", team="alpha", from_label="x", reason="why")
        obj = author.to_json()
        self.assertEqual(obj["name"], "alpha-worker")
        self.assertEqual(obj["from_label"], "x")
        self.assertEqual(obj["reason"], "why")
        self.assertTrue(author.is_member)
        self.assertFalse(author.is_human)
        self.assertFalse(identity.Author("system", None, "system", True).is_member)

    def test_human_origin_ok_rules(self) -> None:
        self.assertTrue(identity.human_origin_ok({"via": "console"}))
        self.assertTrue(identity.human_origin_ok({"via": "popup"}))
        self.assertTrue(identity.human_origin_ok({"via": "outside"}))
        self.assertTrue(identity.human_origin_ok({"via": "cli", "verified": True}))
        self.assertFalse(identity.human_origin_ok({"via": "cli", "verified": False}))
        self.assertFalse(identity.human_origin_ok({"via": "cli-unverified"}))
        self.assertFalse(identity.human_origin_ok({}))

    def test_audit_without_team_writes_nothing(self) -> None:
        with TempState() as ts:
            author = identity.Author("human", "human", identity.VIA_OUTSIDE, False)
            self.assertIsNone(identity.audit(ts.layout, None, "x", author))
            path = identity.audit(ts.layout, "alpha", "charter_set", author, {"seq": 2})
            self.assertTrue(path and path.endswith("audit.jsonl"))
            self.assertEqual(identity.read_audit(ts.layout, "alpha")[-1]["details"], {"seq": 2})

    def test_find_member_prefers_the_pane_record(self) -> None:
        with TempState() as ts:
            from herdr_team import roster
            roster.write_pane_record(ts.session, "term_w1", "alpha", "alpha-worker", 1)
            found = identity.find_member(ts.layout, "term_w1")
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found[0], "alpha")
            self.assertEqual(found[1].name, "alpha-worker")
            self.assertIsNone(identity.find_member(ts.layout, "term_nope"))
            self.assertIsNone(identity.find_member(ts.layout, None))


if __name__ == "__main__":
    unittest.main()

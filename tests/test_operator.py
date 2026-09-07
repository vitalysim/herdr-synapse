"""Operator authority: who holds it, and how an automation is given it.

Two halves that pull against each other. Before 0.7 a member became the
operator by unsetting one environment variable, because every human-only gate
tested only that the author was *named* ``human``. Closing that would have
made an LLM-driven team build impossible, so the same change adds an explicit,
expiring, audited delegation.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from herdr_team import charter as _charter
from herdr_team import identity, operator, roster, store
from herdr_team.errors import HerdrTeamError
from support import FakeApi, TempState, fake_agent
from test_workdir import human


AGENT_PANE = {"pane_id": "w2:p1", "terminal_id": "term_r1", "agent": "codex", "workspace_id": "w2", "tab_id": "w2:t1", "focused": False}
SHELL_PANE = {"pane_id": "w3:p1", "terminal_id": "term_sh", "agent": None, "workspace_id": "w3", "tab_id": "w3:t1", "focused": False}


def api_with_panes(inside_agent_pane: bool) -> FakeApi:
    """A server whose ``pane.process_info`` says whether we descend from the agent pane."""
    api = FakeApi("/nonexistent/herdr.sock")
    panes = {p["pane_id"]: p for p in (AGENT_PANE, SHELL_PANE)}
    api.set_response("pane.list", {"panes": [dict(AGENT_PANE), dict(SHELL_PANE)]})
    api.set_response("pane.get", lambda p: {"pane": dict(panes[p["pane_id"]])})
    api.set_response("agent.get", lambda p: {"agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer")})

    def process_info(params):
        if params["pane_id"] == AGENT_PANE["pane_id"] and inside_agent_pane:
            # our own pid is in the pane's foreground processes: we are its child
            return {"process_info": {"shell_pid": os.getpid(), "foreground_process_group_id": os.getpgrp(),
                                     "foreground_processes": [{"pid": os.getpid()}]}}
        return {"process_info": {"shell_pid": 1, "foreground_process_group_id": 1, "foreground_processes": []}}

    api.set_response("pane.process_info", process_info)
    return api


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.cleanup)

    def resolve(self, env, api):
        return identity.resolve_author(env, self.state.layout, api, team=self.state.team_name, require_server=False)

    def test_dropping_the_pane_variable_no_longer_buys_operator_authority(self):
        env = self.state.env_with(HERDR_PANE_ID=None)
        author = self.resolve(env, api_with_panes(inside_agent_pane=True))
        # the process tree still says which pane this is, so the member gets its own name back
        self.assertEqual(author.name, "alpha-reviewer")
        self.assertFalse(author.is_human)
        self.assertEqual(author.origin.get("rerouted"), "no HERDR_PANE_ID")
        with self.assertRaises(HerdrTeamError) as caught:
            _charter.require_human(self.state.layout, self.state.team_name, author, "knowledge set")
        self.assertEqual(caught.exception.code, "author_mismatch")

    def test_a_shell_pane_pointed_at_by_an_agent_subprocess_is_rerouted_too(self):
        env = self.state.env_with(HERDR_PANE_ID="w3:p1")
        author = self.resolve(env, api_with_panes(inside_agent_pane=True))
        self.assertEqual(author.name, "alpha-reviewer")
        self.assertEqual(author.origin.get("rerouted"), "HERDR_PANE_ID names another pane")

    def test_the_operator_own_shell_is_untouched(self):
        env = self.state.env_with(HERDR_PANE_ID="w3:p1")
        author = self.resolve(env, api_with_panes(inside_agent_pane=False))
        self.assertTrue(author.is_human)
        _charter.require_human(self.state.layout, self.state.team_name, author, "knowledge set")

    def test_outside_herdr_is_still_the_operator_when_no_agent_pane_owns_us(self):
        env = self.state.env_with(HERDR_PANE_ID=None)
        author = self.resolve(env, api_with_panes(inside_agent_pane=False))
        self.assertTrue(author.is_human)
        self.assertEqual(author.via, identity.VIA_OUTSIDE)

    def test_an_unreachable_server_does_not_lock_the_operator_out(self):
        # Refusing on a failed lookup would be worse than the hole: the operator
        # could not run their own CLI. Absence of evidence changes nothing.
        api = FakeApi("/nonexistent/herdr.sock")
        api.set_error("pane.list", "server_not_running", "no server")
        author = self.resolve(self.state.env_with(HERDR_PANE_ID=None), api)
        self.assertTrue(author.is_human)
        self.assertIsNone(identity.hosting_agent_pane(api))

    def test_no_ps_table_is_also_inconclusive(self):
        with mock.patch.object(identity, "ps_table", lambda: None):
            self.assertIsNone(identity.hosting_agent_pane(api_with_panes(inside_agent_pane=True)))


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.cleanup)
        self.session = self.state.session
        self.team = self.state.team_name
        self.member = [m["name"] for m in self.state.members if m.get("kind") != "human"][0]

    def test_a_grant_is_live_until_it_expires(self):
        self.assertIsNone(operator.active(self.session, self.team, self.member))
        entry = operator.grant(self.session, self.team, self.member, ttl_s=60, note="build the team")
        self.assertIsNotNone(operator.active(self.session, self.team, self.member))
        self.assertEqual(operator.active(self.session, self.team, self.member)["note"], "build the team")
        lapses = operator.expires_at(entry)
        self.assertIsNotNone(lapses)
        self.assertIsNone(operator.active(self.session, self.team, self.member, now=lapses + 1))
        self.assertFalse(operator.is_live(entry, now=lapses + 1))

    def test_zero_ttl_never_expires_and_revoke_removes_it(self):
        entry = operator.grant(self.session, self.team, self.member, ttl_s=0)
        self.assertIsNone(entry["expires_at"])
        self.assertTrue(operator.is_live(entry, now=2 ** 40))
        self.assertTrue(operator.revoke(self.session, self.team, self.member))
        self.assertFalse(operator.revoke(self.session, self.team, self.member))
        self.assertIsNone(operator.active(self.session, self.team, self.member))

    def test_grants_are_scoped_to_one_team_and_one_member(self):
        operator.grant(self.session, self.team, self.member, ttl_s=0)
        self.assertIsNone(operator.active(self.session, "other-team", self.member))
        self.assertIsNone(operator.active(self.session, self.team, "somebody-else"))
        self.assertEqual([g["member"] for g in operator.active_all(self.session)], [self.member])

    def test_a_corrupt_file_grants_nothing(self):
        store.write_json(self.session.operators_json, {"v": 1, "grants": "not a mapping"})
        self.assertEqual(operator.read(self.session), {})
        self.assertIsNone(operator.active(self.session, self.team, self.member))


class DelegatedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.cleanup)
        self.team = self.state.team_name
        self.member = "alpha-reviewer"

    def author(self, api=None):
        return identity.resolve_author(self.state.env_with(HERDR_PANE_ID="w2:p1"), self.state.layout,
                                       api or api_with_panes(inside_agent_pane=True), team=self.team, require_server=False)

    def test_a_delegated_member_may_write_the_operator_documents(self):
        author = self.author()
        self.assertFalse(author.operator)
        with self.assertRaises(HerdrTeamError):
            _charter.set_rules(self.state.layout, self.team, author, "DON'T force push", None)

        operator.grant(self.state.session, self.team, self.member, ttl_s=0, note="orchestrator")
        author = self.author()
        self.assertTrue(author.operator)
        result = _charter.set_rules(self.state.layout, self.team, author, "DON'T force push", None)
        self.assertEqual(result["rules_seq"], 1)
        # the trail says who really typed it
        audit = [line for line in self.state.team.audit_jsonl.read_text(encoding="utf-8").splitlines() if "operator_action" in line]
        self.assertTrue(audit, "a delegated write must be audited as one")
        self.assertIn(self.member, audit[-1])

    def test_an_expired_grant_stops_working(self):
        entry = operator.grant(self.state.session, self.team, self.member, ttl_s=1)
        lapses = operator.expires_at(entry)
        with mock.patch.object(operator.time, "time", lambda: lapses + 5):
            self.assertFalse(self.author().operator)

    def test_a_claimed_identity_never_carries_a_grant(self):
        # HERDR_TEAM_MEMBER names a member without proving anything, so a grant
        # must not be reachable by claiming to be the member that holds it.
        operator.grant(self.state.session, self.team, self.member, ttl_s=0)
        api = FakeApi("/nonexistent/herdr.sock")
        api.set_response("pane.list", {"panes": [dict(AGENT_PANE)]})
        api.set_response("pane.get", lambda p: {"pane": dict(AGENT_PANE, terminal_id="term_unknown")})
        api.set_response("agent.get", lambda p: {"agent": fake_agent("w2:p1", "term_unknown", "codex", None)})
        api.set_response("pane.process_info", {"process_info": {"shell_pid": 1, "foreground_process_group_id": 1, "foreground_processes": []}})
        env = self.state.env_with(HERDR_PANE_ID="w2:p1", HERDR_TEAM_MEMBER=self.member, HERDR_TEAM=self.team)
        author = identity.resolve_author(env, self.state.layout, api, team=self.team, require_server=False)
        self.assertEqual(author.name, self.member)
        self.assertFalse(author.verified)
        self.assertFalse(author.operator)

    def test_say_stays_the_operators_even_with_a_grant(self):
        from herdr_team.cmd_board import require_say_author

        operator.grant(self.state.session, self.team, self.member, ttl_s=0)
        with self.assertRaises(HerdrTeamError) as caught:
            require_say_author(self.state.layout, self.team, self.author())
        self.assertEqual(caught.exception.code, "say_unverified")


class OperatorCommandTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.cleanup)
        self.member = [m["name"] for m in self.state.members if m.get("kind") != "human"][0]

    def cli(self, *argv, env=None):
        from test_cmd_board import pane_api
        from test_cmd_roster import run_cli

        return run_cli(list(argv) + ["--team", self.state.team_name], env if env is not None else self.state.env, pane_api())

    def json_cli(self, *argv, env=None):
        from test_cmd_roster import json_out

        return json_out(self.cli("--json", *argv, env=env))

    def test_grant_list_revoke_round_trip(self):
        code, payload, err = self.json_cli("operator", "grant", self.member, "--ttl", "0", "--note", "builds the team")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["member"], self.member)
        self.assertIsNone(payload["expires_at"])
        records = [r for r in store.BoardStore(self.state.team).read() if r.get("event") == "operator_granted"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["to"], ["all"])

        code, payload, err = self.json_cli("operator")
        self.assertEqual(code, 0, err)
        self.assertEqual([g["member"] for g in payload["grants"]], [self.member])

        code, payload, err = self.json_cli("operator", "revoke", self.member)
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["revoked"])
        self.assertTrue([r for r in store.BoardStore(self.state.team).read() if r.get("event") == "operator_revoked"])

    def test_ttl_shorthand_is_parsed(self):
        code, payload, err = self.json_cli("operator", "grant", self.member, "--ttl", "30m")
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(payload["expires_at"])

    def test_granting_needs_a_real_member(self):
        code, _out, err = self.cli("operator", "grant", "nobody")
        self.assertNotEqual(code, 0)
        self.assertIn("member_not_found", err)

    def test_a_delegate_cannot_pass_its_authority_on(self):
        from herdr_team import cmd_roster
        from herdr_team.identity import Author

        operator.grant(self.state.session, self.state.team_name, self.member, ttl_s=0)
        delegate = Author(self.member, "codex", identity.VIA_CLI, True, team=self.state.team_name)
        delegate.operator = True
        # the ordinary gate lets it through ...
        cmd_roster._human_only(self.state.layout, self.state.team_name, delegate, "knowledge set")
        # ... and the one that hands out authority does not
        with self.assertRaises(HerdrTeamError) as caught:
            cmd_roster._strictly_human(self.state.layout, self.state.team_name, delegate, "operator grant")
        self.assertEqual(caught.exception.code, "author_mismatch")


class DelegatedOrchestrationTests(unittest.TestCase):
    """The whole point of the delegation: an agent building a team end to end."""

    def test_a_delegated_agent_can_build_a_team_with_every_operator_document(self):
        import tempfile
        from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

        with TempState() as ts:
            project = tempfile.mkdtemp(prefix="ht-proj-")
            self.addCleanup(lambda: __import__("shutil").rmtree(project, ignore_errors=True))
            api = live_api()
            # alpha-worker sits in pane w2:p2; every gated flag is refused for it today
            agent_env = env_no_daemon(ts, HERDR_PANE_ID="w2:p2")
            argv = ["--json", "create", "beta", "--member", "w5:p1:tester:tess",
                    "--charter", "find the bug", "--rules", "DON'T force push",
                    "--instructions", "tess=own the parser", "--project", project]
            code, _out, err = run_cli(argv, agent_env, api)
            self.assertNotEqual(code, 0)
            self.assertIn("author_mismatch", err)

            operator.grant(ts.session, ts.team_name, "alpha-worker", ttl_s=0, note="orchestrator")
            code, payload, err = json_out(run_cli(argv, agent_env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["team"], "beta")
            # every operator document landed
            self.assertEqual(_charter.get_rules(ts.layout, "beta"), "DON'T force push")
            self.assertIn("own the parser", _charter.get_instructions(ts.layout, "beta", "tess") or "")
            charter_doc = roster.load_team(ts.layout.team("beta")).charter
            self.assertIn("find the bug", (charter_doc or {}).get("text", ""))
            self.assertEqual(payload["project_dir"], os.path.realpath(project))
            # and the trail names the agent, not the operator
            audit = ts.layout.team("beta").audit_jsonl.read_text(encoding="utf-8")
            self.assertIn("operator_action", audit)
            self.assertIn("alpha-worker", audit)


if __name__ == "__main__":
    unittest.main()

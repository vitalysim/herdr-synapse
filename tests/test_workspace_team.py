"""The space you are in decides which team you mean (0.12.1).

Reported live on 2026-09-08: a session held `clickhouse-hunt` in space `wC`
and `gitlab-hunters` in space `w2`. Pressing the console action from the
GitLab space opened the *ClickHouse* console -- and, because a console for
that team was already live, focused a pane in another space entirely. Every
resolver fell back to one session-wide `default_team`, which cannot be right
in two spaces at once, and the space was thrown away even though Herdr hands
it to every plugin action in `HERDR_WORKSPACE_ID`
(`src/app/api/plugins/runtime.rs`).

Every test here fails against 0.12.0.
"""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from support import FakeApi, TempState, fake_pane, fake_plugin_pane_opened

from herdr_team import cli, cmd_board, cmd_ui, console, identity, paths, store
from herdr_team.errors import HerdrTeamError

OTHER = "beta"
OTHER_SPACE = "w7"


def run_cli(argv, env, api=None):
    out, err = io.StringIO(), io.StringIO()
    fake = api if api is not None else FakeApi()
    with mock.patch("herdr_team.api.HerdrApi", lambda socket_path, env=None, **kw: fake):
        code = cli.main(list(argv), env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def json_out(result):
    code, out, err = result
    return code, (json.loads(out) if out.strip() else None), (json.loads(err.splitlines()[0]) if err.strip() and err.lstrip().startswith("{") else err)


def add_team(ts, name=OTHER, space=OTHER_SPACE, members=None):
    """A second team in its own space, so the session has something to be wrong about."""
    team = ts.session.team(name)
    paths.ensure_team_dirs(team)
    rows = members if members is not None else [
        {"name": name + "-worker", "role": "worker", "kind": "claude", "terminal_id": "term_" + name,
         "pane_id": space + ":p1", "workspace_id": space, "tab_id": space + ":t1",
         "status": "active", "generation": 1, "joined_at": "2026-09-04T10:00:00Z"},
        {"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"},
    ]
    store.write_json(team.team_json, {
        "schema": 1, "team": name, "created_at": "2026-09-04T10:00:00Z",
        "socket": os.fspath(ts.socket_path), "state_dir": os.fspath(ts.state_root),
        "naming": "prefixed", "revision": 1,
        "charter": {"seq": 1, "text": "the other job", "refs": [], "updated_at": "2026-09-04T10:00:00Z", "updated_by": "human"},
        "members": rows,
    })
    return team


def set_default(ts, name):
    cmd_board.write_console_json(ts.session, {"default_team": name, "schema": 2, "consoles": {}})


class WorkspaceTeamTests(unittest.TestCase):
    """`workspace_team` answers only when the answer is certain."""

    def setUp(self):
        self.ts = TempState()          # team "alpha", both agents in space w2
        self.addCleanup(self.ts.cleanup)
        add_team(self.ts)              # team "beta" in space w7

    def team_of(self, space):
        return cmd_board.workspace_team(self.ts.session, space)

    def test_it_names_the_team_whose_agents_live_there(self):
        self.assertEqual(self.team_of("w2"), "alpha")
        self.assertEqual(self.team_of(OTHER_SPACE), OTHER)

    def test_a_space_with_no_team_and_no_space_at_all_answer_nothing(self):
        self.assertIsNone(self.team_of("w9"))
        self.assertIsNone(self.team_of(None))
        self.assertIsNone(self.team_of(""))

    def test_two_teams_in_one_space_is_ambiguous_not_a_coin_flip(self):
        add_team(self.ts, "gamma", "w2")
        self.assertIsNone(self.team_of("w2"), "the caller must keep its own fallback, not be guessed at")

    def test_the_human_row_belongs_to_no_space(self):
        # every team has one, and it carries no workspace; if it counted, a
        # team would claim whichever space the operator last typed in
        doc = store.read_json(self.ts.team.team_json)
        self.assertIn("human", [m["name"] for m in doc["members"]])
        self.assertIsNone(self.team_of(None))

    def test_a_departed_member_does_not_hold_the_space(self):
        doc = store.read_json(self.ts.team.team_json)
        for member in doc["members"]:
            if member.get("kind") != "human":
                member["status"] = "left"
        store.write_json(self.ts.team.team_json, doc)
        self.assertIsNone(self.team_of("w2"))

    def test_one_team_needs_no_disambiguation_and_reads_nothing(self):
        with TempState() as solo:
            with mock.patch.object(store, "read_json", side_effect=AssertionError("read a team.json")) as read:
                self.assertIsNone(cmd_board.workspace_team(solo.session, "w2"))
                read.assert_not_called()

    def test_a_corrupt_team_json_is_skipped_not_fatal(self):
        self.ts.session.team(OTHER).team_json.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.team_of("w2"), "alpha")
        self.assertIsNone(self.team_of(OTHER_SPACE))


class EnvWorkspaceTests(unittest.TestCase):
    """Where the space comes from differs per surface: action, pane, popup."""

    def test_the_action_variable_wins(self):
        self.assertEqual(paths.env_workspace({"HERDR_WORKSPACE_ID": "w2", "HERDR_PANE_ID": "w7:p1"}), "w2")

    def test_a_popup_has_only_the_context_json(self):
        context = json.dumps({"focused_pane_id": "w7:p3"})
        self.assertEqual(paths.env_workspace({"HERDR_PLUGIN_CONTEXT_JSON": context}), "w7")
        self.assertEqual(paths.env_workspace({"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_id": "w5"})}), "w5")

    def test_a_shell_pane_has_only_its_pane_id(self):
        self.assertEqual(paths.env_workspace({"HERDR_PANE_ID": "wC:p12"}), "wC")

    def test_nothing_and_nonsense_answer_none(self):
        self.assertIsNone(paths.env_workspace({}))
        self.assertIsNone(paths.env_workspace({"HERDR_PLUGIN_CONTEXT_JSON": "}{"}))
        self.assertIsNone(paths.env_workspace({"HERDR_PANE_ID": ""}))


class ResolveTeamTests(unittest.TestCase):
    """`resolve_team` is what every CLI command infers its team from."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        add_team(self.ts)
        set_default(self.ts, OTHER)     # the wrong answer for space w2

    def resolved(self, env, team=None):
        import argparse

        args = argparse.Namespace(env=env, team=team)
        return cmd_board.resolve_team(args, paths.resolve_layout(env), None)

    def test_the_space_outranks_the_session_default(self):
        self.assertEqual(self.resolved(self.ts.env_with(HERDR_WORKSPACE_ID="w2")), "alpha")

    def test_the_default_still_answers_from_a_space_with_no_team(self):
        self.assertEqual(self.resolved(self.ts.env_with(HERDR_WORKSPACE_ID="w9")), OTHER)
        self.assertEqual(self.resolved(self.ts.env), OTHER)

    def test_an_explicit_team_still_outranks_the_space(self):
        env = self.ts.env_with(HERDR_WORKSPACE_ID="w2")
        self.assertEqual(self.resolved(env, team=OTHER), OTHER)
        self.assertEqual(self.resolved(self.ts.env_with(HERDR_WORKSPACE_ID="w2", HERDR_TEAM=OTHER)), OTHER)

    def test_the_authors_own_team_still_outranks_the_space(self):
        # a member of alpha running a command from a pane parked in beta's
        # space is still a member of alpha
        import argparse

        from herdr_team.identity import Author

        env = self.ts.env_with(HERDR_WORKSPACE_ID=OTHER_SPACE)
        author = Author("alpha-worker", "claude", "cli", True, team="alpha")
        args = argparse.Namespace(env=env, team=None)
        self.assertEqual(cmd_board.resolve_team(args, paths.resolve_layout(env), author), "alpha")


class ConsoleActionTests(unittest.TestCase):
    """The reported bug, end to end: the console action in the second team's space."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        add_team(self.ts)
        set_default(self.ts, OTHER)
        self.api = FakeApi()
        self.api.set_response("plugin.pane.open", fake_plugin_pane_opened("console", fake_pane("w2:p9", "term_plugin", None, cmd_ui.CONSOLE_TITLE)))
        self.api.set_response("pane.list", {"panes": []})

    def opened_team(self, env):
        code, payload, err = json_out(run_cli(["--json", "ui", "console"], env, self.api))
        self.assertEqual(code, 0, err)
        call = [p for m, p in self.api.calls if m == "plugin.pane.open"][-1]
        return call.get("env", {}).get("HERDR_TEAM"), payload

    def test_the_console_opens_on_the_team_of_the_space_it_was_pressed_in(self):
        team, _payload = self.opened_team(self.ts.env_with(HERDR_WORKSPACE_ID="w2"))
        self.assertEqual(team, "alpha")

    def test_it_still_falls_back_to_the_default_outside_any_teams_space(self):
        team, _payload = self.opened_team(self.ts.env_with(HERDR_WORKSPACE_ID="w9"))
        self.assertEqual(team, OTHER)

    def test_it_does_not_focus_another_spaces_live_console(self):
        """The visible half of the bug: beta's console was already open, so the
        action did not merely pick the wrong team, it jumped the operator to a
        pane in a different space."""
        def add_console(doc):
            doc["consoles"]["term_beta"] = {"pane_id": OTHER_SPACE + ":p2", "terminal_id": "term_beta",
                                            "team": OTHER, "open": True, "pid": os.getpid()}

        cmd_board.update_console_doc(self.ts.session, add_console)
        self.api.set_response("pane.list", {"panes": [fake_pane(OTHER_SPACE + ":p2", "term_beta", None, cmd_ui.console_label(OTHER))]})
        team, payload = self.opened_team(self.ts.env_with(HERDR_WORKSPACE_ID="w2"))
        self.assertEqual(team, "alpha")
        self.assertTrue(payload.get("opened"), "alpha has no console yet; one must be opened")
        self.assertNotIn("pane.focus", [m for m, _p in self.api.calls])

    def test_a_popup_action_is_told_the_space_team_too(self):
        self.api.set_response("plugin.pane.open", {"type": "ok"})
        for target in ("who", "usage", "knowledge"):
            code, _payload, err = json_out(run_cli(["--json", "ui", target], self.ts.env_with(HERDR_WORKSPACE_ID="w2"), self.api))
            self.assertEqual(code, 0, err)
            call = [p for m, p in self.api.calls if m == "plugin.pane.open"][-1]
            self.assertEqual(call.get("env", {}).get("HERDR_TEAM"), "alpha", target)


class ConsolePickTeamTests(unittest.TestCase):
    """A console restored after a Herdr restart comes back without HERDR_TEAM."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        add_team(self.ts)
        set_default(self.ts, OTHER)

    def test_the_space_it_came_back_in_says_which_board_it_is(self):
        self.assertEqual(console.pick_team(self.ts.layout, None, self.ts.env_with(HERDR_PANE_ID="w2:p9")), "alpha")

    def test_its_own_env_and_flag_still_win(self):
        env = self.ts.env_with(HERDR_PANE_ID="w2:p9", HERDR_TEAM=OTHER)
        self.assertEqual(console.pick_team(self.ts.layout, None, env), OTHER)
        self.assertEqual(console.pick_team(self.ts.layout, OTHER, self.ts.env_with(HERDR_PANE_ID="w2:p9")), OTHER)

    def test_without_a_space_it_is_the_default_as_before(self):
        self.assertEqual(console.pick_team(self.ts.layout, None, self.ts.env), OTHER)


class ShellPaneAuthorTests(unittest.TestCase):
    """A human typing in a shell pane has no roster row; the space is what places them."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        add_team(self.ts)
        set_default(self.ts, OTHER)

    def author_in(self, pane_id):
        api = FakeApi()
        api.set_response("pane.get", {"pane": fake_pane(pane_id, "term_shell_" + pane_id.replace(":", "_"))})
        api.set_response("pane.list", {"panes": [fake_pane(pane_id, "term_shell_" + pane_id.replace(":", "_"))]})
        env = self.ts.env_with(HERDR_PANE_ID=pane_id)
        return identity.resolve_author(env, paths.resolve_layout(env), api)

    def test_a_shell_in_a_teams_space_belongs_to_that_team(self):
        self.assertEqual(self.author_in("w2:p8").team, "alpha")

    def test_a_shell_outside_every_teams_space_keeps_the_default(self):
        self.assertEqual(self.author_in("w9:p1").team, OTHER)


if __name__ == "__main__":
    unittest.main()

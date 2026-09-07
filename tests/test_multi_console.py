"""One board per team, several open at once.

`console.json` used to be a single flat record whose `terminal_id` was also
the proof that a process IS the console. A second console overwrote it and
silently demoted the first to `cli-unverified` (HP-07, 2026-09-05), so the
plugin refused to open one at all. The record is now a registry keyed by
terminal, and identity checks membership instead of equality.
"""

from __future__ import annotations

import os
import unittest
from typing import Any, Dict
from unittest import mock

from herdr_team import cmd_board, cmd_ui, identity, store
from herdr_team import daemon as D
from support import TempState, fake_pane
from test_identity import make_api, own_process_info, pane_info


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_a_pre_registry_record_reads_as_one_entry(self):
        store.write_json(self.ts.session.console_json, {
            "default_team": "alpha", "pane_id": "w1:p1", "terminal_id": "term_old",
            "pid": 4242, "open": True, "human_label": "human"})
        doc = cmd_board.console_doc(self.ts.session)
        self.assertEqual(doc["default_team"], "alpha")
        self.assertEqual(sorted(doc["consoles"]), ["term_old"])
        entry = doc["consoles"]["term_old"]
        self.assertEqual((entry["pid"], entry["open"], entry["team"]), (4242, True, "alpha"))

    def test_entries_do_not_overwrite_each_other(self):
        cmd_board.upsert_console(self.ts.session, "term_a", {"team": "alpha", "pid": 1, "open": True})
        cmd_board.upsert_console(self.ts.session, "term_b", {"team": "beta", "pid": 2, "open": True})
        entries = cmd_board.console_entries(self.ts.session)
        self.assertEqual(sorted(entries), ["term_a", "term_b"])
        self.assertEqual(entries["term_a"]["team"], "alpha")
        self.assertEqual(entries["term_b"]["team"], "beta")

    def test_closing_one_leaves_the_other_open(self):
        cmd_board.upsert_console(self.ts.session, "term_a", {"team": "alpha", "pid": os.getpid(), "open": True})
        cmd_board.upsert_console(self.ts.session, "term_b", {"team": "beta", "pid": os.getpid(), "open": True})
        cmd_board.close_console(self.ts.session, "term_a")
        entries = cmd_board.console_entries(self.ts.session)
        self.assertFalse(entries["term_a"]["open"])
        self.assertTrue(entries["term_b"]["open"])
        self.assertEqual(sorted(cmd_board.live_console_entries(self.ts.session)), ["term_b"])

    def test_a_dead_pid_is_not_live(self):
        cmd_board.upsert_console(self.ts.session, "term_a", {"team": "alpha", "pid": 999999, "open": True})
        self.assertEqual(cmd_board.live_console_entries(self.ts.session), {})

    def test_console_for_team_finds_the_right_one(self):
        cmd_board.upsert_console(self.ts.session, "term_a", {"team": "alpha", "pid": os.getpid(), "open": True})
        cmd_board.upsert_console(self.ts.session, "term_b", {"team": "beta", "pid": os.getpid(), "open": True})
        self.assertEqual(cmd_board.console_for_team(self.ts.session, "beta")["terminal_id"], "term_b")
        self.assertIsNone(cmd_board.console_for_team(self.ts.session, "gamma"))

    def test_the_default_team_stays_session_wide(self):
        store.write_json(self.ts.session.console_json, {"default_team": "alpha"})
        cmd_board.upsert_console(self.ts.session, "term_b", {"team": "beta", "pid": 1, "open": True})
        self.assertEqual(cmd_board.default_team_of(self.ts.session), "alpha")

    def test_concurrent_writers_do_not_lose_an_entry(self):
        """Every writer replaces the whole document, so the registry needs a lock."""
        import threading

        def register(index: int) -> None:
            cmd_board.upsert_console(self.ts.session, "term_{}".format(index), {"team": "t{}".format(index), "pid": 1, "open": True})

        threads = [threading.Thread(target=register, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(cmd_board.console_entries(self.ts.session)), 12)


class TwoConsolesVerifyTests(unittest.TestCase):
    """The HP-07 regression, inverted: a second console must not demote the first."""

    def _env(self, ts: TempState, pane_id: str) -> Dict[str, str]:
        return ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID="console", HERDR_PLUGIN_ID="herdr-team", HERDR_PANE_ID=pane_id)

    def _author(self, ts: TempState, pane_id: str, terminal: str, focused: bool = True):
        api = make_api()
        api.set_response("pane.get", lambda p: pane_info(pane_id, terminal, None, focused=focused))
        api.set_response("pane.process_info", own_process_info(pane_id))
        with mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid()}):
            return identity.resolve_author(self._env(ts, pane_id), ts.layout, api)

    def test_both_consoles_verify_and_keep_their_own_team(self):
        with TempState() as ts:
            for terminal, pane, team in (("term_a", "w1:p1", "alpha"), ("term_b", "w1:p2", "beta")):
                cmd_board.upsert_console(ts.session, terminal, {"pane_id": pane, "team": team, "pid": os.getpid(), "open": True})
            a = self._author(ts, "w1:p1", "term_a")
            b = self._author(ts, "w1:p2", "term_b")
            for author, team in ((a, "alpha"), (b, "beta")):
                self.assertEqual(author.via, identity.VIA_CONSOLE, author.reason)
                self.assertTrue(author.verified)
                self.assertEqual(author.team, team)

    def test_an_unregistered_terminal_is_still_unverified(self):
        with TempState() as ts:
            cmd_board.upsert_console(ts.session, "term_a", {"pane_id": "w1:p1", "team": "alpha", "pid": os.getpid(), "open": True})
            stranger = self._author(ts, "w9:p9", "term_stranger")
            self.assertEqual(stranger.via, identity.VIA_CLI_UNVERIFIED)
            self.assertFalse(stranger.verified)
            self.assertIn("term_stranger", stranger.reason)

    def test_a_dead_entry_does_not_verify(self):
        with TempState() as ts:
            cmd_board.upsert_console(ts.session, "term_a", {"pane_id": "w1:p1", "team": "alpha", "pid": 999999, "open": True})
            author = self._author(ts, "w1:p1", "term_a")
            self.assertEqual(author.via, identity.VIA_CLI_UNVERIFIED)

    def test_a_closed_entry_does_not_verify(self):
        with TempState() as ts:
            cmd_board.upsert_console(ts.session, "term_a", {"pane_id": "w1:p1", "team": "alpha", "pid": os.getpid(), "open": True})
            cmd_board.close_console(ts.session, "term_a")
            self.assertEqual(self._author(ts, "w1:p1", "term_a").via, identity.VIA_CLI_UNVERIFIED)

    def test_an_unfocused_console_is_still_only_unfocused(self):
        with TempState() as ts:
            cmd_board.upsert_console(ts.session, "term_a", {"pane_id": "w1:p1", "team": "alpha", "pid": os.getpid(), "open": True})
            author = self._author(ts, "w1:p1", "term_a", focused=False)
            self.assertEqual(author.via, identity.VIA_CONSOLE_UNFOCUSED)
            self.assertFalse(author.verified)


class SayAcceptsAnyLiveConsoleTests(unittest.TestCase):
    def record(self, terminal: str) -> Dict[str, Any]:
        return {"seq": 5, "kind": "direct", "from": "human", "to": ["alpha-worker"], "text": "hi",
                "from_terminal": terminal, "origin": {"via": "console", "verified": True}}

    def test_any_live_console_terminal_is_accepted(self):
        self.assertIsNone(D.say_source_problem(self.record("term_b"), "alpha-worker", {"term_a", "term_b"}))

    def test_an_unknown_terminal_is_still_refused(self):
        problem = D.say_source_problem(self.record("term_x"), "alpha-worker", {"term_a"})
        self.assertIn("term_x", problem)
        self.assertIn("not a live console", problem)

    def test_no_console_open_refuses(self):
        self.assertIsNotNone(D.say_source_problem(self.record("term_a"), "alpha-worker", set()))

    def test_a_single_string_still_works(self):
        self.assertIsNone(D.say_source_problem(self.record("term_a"), "alpha-worker", "term_a"))


class OpenPerTeamTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def api_with_console(self, terminal="term_a", pane="w1:p1"):
        from support import FakeApi

        api = FakeApi()
        api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane(pane, terminal, None, "Team console: alpha")]})
        api.set_response("plugin.pane.focus", {"type": "ok"})
        return api

    def test_a_second_console_for_the_same_team_focuses(self):
        cmd_board.upsert_console(self.ts.session, "term_a", {"pane_id": "w1:p1", "team": "alpha", "pid": os.getpid(), "open": True})
        api = self.api_with_console()
        self.assertEqual(cmd_ui.focus_live_console(api, self.ts.layout, "alpha"), "w1:p1")

    def test_a_console_for_another_team_does_not_focus(self):
        """This is what lets the open proceed instead of being swallowed."""
        cmd_board.upsert_console(self.ts.session, "term_a", {"pane_id": "w1:p1", "team": "alpha", "pid": os.getpid(), "open": True})
        api = self.api_with_console()
        self.assertIsNone(cmd_ui.focus_live_console(api, self.ts.layout, "beta"))

    def test_the_pane_env_carries_the_team(self):
        params = cmd_ui.open_params("console", None, {}, "beta")
        self.assertEqual(params["env"]["HERDR_TEAM"], "beta")

    def test_the_label_names_the_team(self):
        self.assertEqual(cmd_ui.console_label("beta"), "Team console: beta")
        self.assertTrue(cmd_ui.is_console_label("Team console: beta"))
        self.assertTrue(cmd_ui.is_console_label("Team console"))
        self.assertFalse(cmd_ui.is_console_label("Teammate console"))


class ReconcileSparesLiveConsolesTests(unittest.TestCase):
    def test_a_live_registered_console_is_not_closed(self):
        from support import FakeApi

        with TempState() as ts:
            cmd_board.upsert_console(ts.session, "term_a", {"pane_id": "w1:p1", "team": "alpha", "pid": os.getpid(), "open": True})
            cmd_board.upsert_console(ts.session, "term_b", {"pane_id": "w1:p2", "team": "beta", "pid": os.getpid(), "open": True})
            api = FakeApi()
            api.set_response("pane.list", {"type": "pane_list", "panes": [
                fake_pane("w1:p1", "term_a", None, "Team console: alpha"),
                fake_pane("w1:p2", "term_b", None, "Team console: beta"),
            ]})
            api.set_response("pane.process_info", lambda p: {"type": "pane_process_info", "process_info": {"pane_id": p["pane_id"], "shell_pid": 4, "foreground_processes": [{"pid": 4, "name": "zsh"}]}})
            api.set_response("pane.close", {"type": "ok"})
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env), reopen=False)
            self.assertEqual(result["closed"], [], "a live console must never be closed as a stray shell")
            self.assertEqual(sorted(result["lives"]), ["w1:p1", "w1:p2"])

    def test_a_dead_shell_beside_a_live_console_is_still_closed(self):
        from support import FakeApi

        with TempState() as ts:
            cmd_board.upsert_console(ts.session, "term_a", {"pane_id": "w1:p1", "team": "alpha", "pid": os.getpid(), "open": True})
            api = FakeApi()
            api.set_response("pane.list", {"type": "pane_list", "panes": [
                fake_pane("w1:p1", "term_a", None, "Team console: alpha"),
                fake_pane("w1:p8", "term_dead", None, "Team console"),
            ]})
            api.set_response("pane.process_info", lambda p: {"type": "pane_process_info", "process_info": {"pane_id": p["pane_id"], "shell_pid": 4, "foreground_processes": [{"pid": 4, "name": "zsh"}]}})
            api.set_response("pane.close", {"type": "ok"})
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env), reopen=False)
            self.assertEqual(result["closed"], ["w1:p8"])


if __name__ == "__main__":
    unittest.main()

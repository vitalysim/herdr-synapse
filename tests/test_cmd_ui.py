"""``ui`` and the plugin pane result shapes (plan 7.1, docs/cli.md section 9).

The server answers ``plugin.pane.open`` with ``plugin_pane_opened`` (pane id
at ``plugin_pane.pane.pane_id``) for split/tab/overlay/zoomed placements and
a bare ``{"type":"ok"}`` for ``placement=popup`` (``open_plugin_popup_pane``
in ``src/app/api/plugins/panes.rs``); ``cmd_ui`` must read both through
``herdr_team.api.plugin_pane_id``.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from support import FakeApi, TempState, fake_pane, fake_plugin_pane_opened

from herdr_team import cli, cmd_board, cmd_ui, store
from herdr_team.errors import HerdrTeamError


def run_cli(argv, env, api=None):
    out, err = io.StringIO(), io.StringIO()
    fake = api if api is not None else FakeApi()
    with mock.patch("herdr_team.api.HerdrApi", lambda socket_path, env=None, **kw: fake):
        code = cli.main(list(argv), env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def json_out(result):
    code, out, err = result
    return code, (json.loads(out) if out.strip() else None), (json.loads(err.splitlines()[0]) if err.strip() and err.lstrip().startswith("{") else err)


def console_pane(pane_id: str = "w1:p9"):
    return fake_pane(pane_id, "term_plugin", None, cmd_ui.CONSOLE_TITLE)


class OpenPaneResultShapeTests(unittest.TestCase):
    """open_pane reads the real server shapes: nested pane id for tiled panes, null for popups."""

    def test_split_console_reports_the_nested_pane_id(self):
        api = FakeApi()
        api.set_response("plugin.pane.open", fake_plugin_pane_opened("console", console_pane("w1:p9")))
        payload = cmd_ui.open_pane(api, "console", None, {}, "alpha")
        self.assertEqual(payload, {"ui": "console", "opened": True, "placement": "split", "pane_id": "w1:p9", "fallback": None, "retried": False})
        self.assertEqual(api.calls[0][1]["entrypoint"], "console")
        self.assertEqual(api.calls[0][1]["env"], {"HERDR_TEAM": "alpha"})

    def test_popup_ok_reports_a_null_pane_id(self):
        api = FakeApi()
        api.set_response("plugin.pane.open", {"type": "ok"})
        for target in ("picker", "compose", "usage", "who"):
            payload = cmd_ui.open_pane(api, target, None, {}, None)
            self.assertEqual((payload["ui"], payload["placement"], payload["pane_id"], payload["fallback"]), (target, "popup", None, None), target)
        who = [p for m, p in api.calls if m == "plugin.pane.open"][-1]
        self.assertEqual((who["entrypoint"], who["placement"], who["env"]), ("console", "popup", {cmd_ui.WHO_ENV: "who"}))

    def test_legacy_top_level_ids_are_not_required(self):
        # The old parser looked for ``pane_id`` / ``id`` at the top level; the
        # server never sends that, and unrelated top-level keys must not leak in.
        api = FakeApi()
        api.set_response("plugin.pane.open", {"type": "ok", "id": "not-a-pane"})
        self.assertIsNone(cmd_ui.open_pane(api, "picker", None, {}, None)["pane_id"])

    def test_fallback_to_the_console_split_carries_the_reopened_pane_id(self):
        api = FakeApi()
        seen = []

        def open_pane(params):
            seen.append(params)
            if params["entrypoint"] == "picker":
                raise HerdrTeamError("plugin_pane_open_failed", "popup already open", 1)
            return fake_plugin_pane_opened("console", console_pane("w1:p9"))

        api.set_response("plugin.pane.open", open_pane)
        payload = cmd_ui.open_pane(api, "picker", None, {"HERDR_PANE_ID": "w1:p2"}, "alpha", sleep=lambda _s: None)
        self.assertEqual((payload["ui"], payload["opened"], payload["placement"], payload["pane_id"], payload["fallback"], payload["retried"]), ("picker", True, "split", "w1:p9", "console", True))
        self.assertEqual(payload["error"]["code"], "plugin_pane_open_failed")
        self.assertEqual([p["entrypoint"] for p in seen], ["picker", "picker", "console"])
        self.assertEqual((seen[2]["placement"], seen[2]["target_pane_id"]), ("split", "w1:p2"))


class UiCommandTests(unittest.TestCase):
    def test_ui_console_json_carries_the_nested_pane_id(self):
        with TempState() as ts:
            api = FakeApi()
            api.set_response("plugin.pane.open", fake_plugin_pane_opened("console", console_pane("w1:p9")))
            code, payload, err = json_out(run_cli(["--json", "ui", "console"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"ui": "console", "opened": True, "placement": "split", "pane_id": "w1:p9", "fallback": None, "retried": False})

    def test_ui_picker_json_has_a_null_pane_id_for_the_popup(self):
        with TempState() as ts:
            api = FakeApi()
            api.set_response("plugin.pane.open", {"type": "ok"})
            code, payload, err = json_out(run_cli(["--json", "ui", "picker"], ts.env_with(HERDR_TEAM_NO_DAEMON="1"), api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"ui": "picker", "opened": True, "placement": "popup", "pane_id": None, "fallback": None, "retried": False})
            self.assertEqual([m for m, _ in api.calls], ["plugin.pane.open"])


class ReconcileConsoleTests(unittest.TestCase):
    """Plan 7.3: a dead post-restart console shell is closed and the reopened pane id comes from ``plugin_pane_opened``."""

    def _dead_console(self, ts, api):
        store.write_json(ts.session.console_json, {"pane_id": "w1:p2", "terminal_id": "term_gone", "pid": 999999, "open": True, "default_team": "alpha"})
        api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w1:p2", "term_shell", None, cmd_ui.CONSOLE_TITLE)]})
        api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w1:p2", "shell_pid": 4, "foreground_processes": [{"pid": 4, "name": "zsh"}]}})
        api.set_response("pane.close", {"type": "ok"})

    def test_reopened_reports_the_real_pane_id(self):
        with TempState() as ts:
            api = FakeApi()
            self._dead_console(ts, api)
            api.set_response("plugin.pane.open", fake_plugin_pane_opened("console", console_pane("w1:p9")))
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env))
            self.assertEqual((result["closed"], result["reopened"]), (["w1:p2"], "w1:p9"))
            opened = [p for m, p in api.calls if m == "plugin.pane.open"]
            self.assertEqual((len(opened), opened[0]["entrypoint"], opened[0]["env"]), (1, "console", {"HERDR_TEAM": "alpha"}))
            self.assertFalse(any(e.get("open") for e in cmd_board.console_entries(ts.session).values()))

    def test_unknown_foreground_and_launch_grace_are_reported_as_unresolved(self):
        """RT-05 (rig, 2026-09-05): the startup hook ran before the restored shell spawned; nothing re-checked."""
        with TempState() as ts:
            api = FakeApi()
            self._dead_console(ts, api)
            api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w1:p2", "shell_pid": 4, "foreground_processes": []}})
            api.set_response("plugin.pane.open", fake_plugin_pane_opened("console", console_pane("w1:p9")))
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env))
            self.assertEqual((result["closed"], result["unresolved"], result["reopened"]), ([], ["w1:p2"], "w1:p9"))
            # the reopen stamped launched_at: inside the grace the dead shell stays unresolved, not closed
            api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w1:p2", "shell_pid": 4, "foreground_processes": [{"pid": 4, "name": "zsh"}]}})
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env), reopen=False)
            self.assertEqual((result["closed"], result["unresolved"]), ([], ["w1:p2"]))
            # grace over: closed, nothing left unresolved
            console = store.read_json(ts.session.console_json)
            console["launched_at"] = "2020-01-01T00:00:00.000Z"
            store.write_json(ts.session.console_json, console)
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env), reopen=False)
            self.assertEqual((result["closed"], result["unresolved"]), (["w1:p2"], []))

    def test_reopened_without_a_pane_id_reads_opened(self):
        with TempState() as ts:
            api = FakeApi()
            self._dead_console(ts, api)
            api.set_response("plugin.pane.open", {"type": "ok"})
            result = cmd_ui.reconcile_console(ts.layout, api, dict(ts.env))
            self.assertEqual(result["reopened"], "opened")


class ConsoleClosedByPaneCloseTests(unittest.TestCase):
    """UI-05 (rig, 2026-09-05): ``plugin pane close`` hangs the console up before SIGTERM, so the record must
    be fixed from ``pane.closed``: the console's terminal missing from ``pane.list`` means ``open:false``."""

    def _open_console(self, ts, pane_id="w3:p2"):
        store.write_json(ts.session.console_json, {"pane_id": pane_id, "terminal_id": "term_console", "pid": os.getpid(), "open": True, "default_team": "alpha"})

    def test_terminal_gone_records_open_false_with_a_stale_pane_id(self):
        with TempState() as ts:
            api = FakeApi()
            self._open_console(ts)  # pane_id w3:p2 is stale: the console was moved to w4:p1 before it was closed
            api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w1:p1", "term_shell")]})
            self.assertTrue(cmd_ui.mark_console_closed_if_pane_gone(ts.session, api))
            doc = cmd_board.console_doc(ts.session)
            entry = next(iter(doc["consoles"].values()))
            self.assertEqual((entry["open"], entry["pid"], doc["default_team"]), (False, None, "alpha"))
            self.assertTrue(entry["closed_at"].endswith("Z"))
            # idempotent: a second pane.closed changes nothing
            self.assertFalse(cmd_ui.mark_console_closed_if_pane_gone(ts.session, api))

    def test_terminal_still_listed_after_a_move_stays_open(self):
        with TempState() as ts:
            api = FakeApi()
            self._open_console(ts)
            api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w4:p1", "term_console", None, cmd_ui.CONSOLE_TITLE)]})
            self.assertFalse(cmd_ui.mark_console_closed_if_pane_gone(ts.session, api))
            self.assertTrue(any(e.get("open") for e in cmd_board.console_entries(ts.session).values()))

    def test_no_pane_list_evidence_changes_nothing(self):
        with TempState() as ts:
            api = FakeApi()  # pane.list is not canned: unknown_method
            self._open_console(ts)
            self.assertFalse(cmd_ui.mark_console_closed_if_pane_gone(ts.session, api))
            api.set_response("pane.list", {"type": "pane_list", "panes": []})
            self.assertFalse(cmd_ui.mark_console_closed_if_pane_gone(ts.session, api))
            api.unreachable = True
            self.assertFalse(cmd_ui.mark_console_closed_if_pane_gone(ts.session, api))
            self.assertTrue(any(e.get("open") for e in cmd_board.console_entries(ts.session).values()))
            store.write_json(ts.session.console_json, {"default_team": "alpha"})  # no console record at all
            api.unreachable = False
            api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w1:p1", "term_shell")]})
            self.assertFalse(cmd_ui.mark_console_closed_if_pane_gone(ts.session, api))


if __name__ == "__main__":
    unittest.main()


class PopupTargetPaneTests(unittest.TestCase):
    """HP-07 (2026-09-05, Herdr 0.8.2): ``--target-pane`` names the console fallback split only.

    ``plugin.pane.open`` rejects ``target_pane_id`` for a popup with ``invalid_params`` ("overlay
    and popup plugin panes target the active pane"), which is not retryable, so the "popup already
    open" retry never ran and the console fallback opened at once.
    """

    def test_popup_params_omit_target_pane_id(self):
        for target in ("compose", "picker", "who", "usage"):
            params = cmd_ui.open_params(target, "w1:p4", {}, "alpha")
            self.assertNotIn("target_pane_id", params, target)
        self.assertEqual(cmd_ui.open_params("console", "w1:p4", {}, "alpha")["target_pane_id"], "w1:p4")

    def test_popup_already_open_with_target_pane_retries_then_falls_back(self):
        api = FakeApi()
        seen = []

        def open_pane(params):
            seen.append(params)
            if params["entrypoint"] == "compose":
                if "target_pane_id" in params:
                    raise HerdrTeamError("invalid_params", "overlay and popup plugin panes target the active pane", 1)
                raise HerdrTeamError("plugin_pane_open_failed", "popup already open", 1)
            return fake_plugin_pane_opened("console", console_pane("w1:p9"))

        api.set_response("plugin.pane.open", open_pane)
        payload = cmd_ui.open_pane(api, "compose", "w1:p4", {}, "alpha", sleep=lambda _s: None)
        self.assertEqual((payload["retried"], payload["fallback"], payload["pane_id"], payload["error"]["code"]), (True, "console", "w1:p9", "plugin_pane_open_failed"))
        self.assertEqual([p["entrypoint"] for p in seen], ["compose", "compose", "console"])
        self.assertEqual((seen[2]["placement"], seen[2]["target_pane_id"]), ("split", "w1:p4"))


class FallbackReusesLiveConsoleTests(unittest.TestCase):
    """HP-07 (2026-09-05): the console fallback must not open a second console while one is alive.

    Live run: the fallback opened ``w1:p6`` next to the live console ``w1:p5`` and overwrote
    ``console.json``, so posts typed into ``w1:p5`` became ``cli-unverified`` (console.json
    records terminal X, this pane is Y). Plan 7.3: the console is single-writer; a second open
    focuses the live one by ``terminal_id``.
    """

    def _live_console(self, ts, api):
        store.write_json(ts.session.console_json, {"pane_id": "w1:p5", "terminal_id": "term_console", "pid": os.getpid(), "open": True, "default_team": "alpha"})
        api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w1:p7", "term_console", None, cmd_ui.CONSOLE_TITLE)]})
        api.set_response("plugin.pane.focus", {"type": "ok"})

    def test_open_pane_fallback_focuses_the_live_console(self):
        with TempState() as ts:
            api = FakeApi()
            self._live_console(ts, api)
            api.set_error("plugin.pane.open", "plugin_pane_open_failed", "popup already open")
            payload = cmd_ui.open_pane(api, "compose", "w1:p4", dict(ts.env), "alpha", sleep=lambda _s: None, layout=ts.layout)
            self.assertEqual((payload["opened"], payload["focused"], payload["fallback"], payload["pane_id"], payload["retried"]), (False, True, "console", "w1:p7", True))
            self.assertEqual([m for m, _ in api.calls if m == "plugin.pane.open"], ["plugin.pane.open", "plugin.pane.open"])
            self.assertEqual([p for m, p in api.calls if m == "plugin.pane.focus"], [{"pane_id": "w1:p7"}])
            self.assertEqual(store.read_json(ts.session.console_json)["terminal_id"], "term_console")

    def test_ui_compose_json_reports_the_focused_live_console(self):
        with TempState() as ts:
            api = FakeApi()
            self._live_console(ts, api)
            api.set_error("plugin.pane.open", "plugin_pane_open_failed", "popup already open")
            with mock.patch("herdr_team.cmd_ui.time.sleep", lambda _s: None):
                code, payload, err = json_out(run_cli(["--json", "ui", "compose", "--target-pane", "w1:p4"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual((payload["ui"], payload["opened"], payload["focused"], payload["fallback"], payload["pane_id"]), ("compose", False, True, "console", "w1:p7"))
            self.assertNotIn("launched_at", store.read_json(ts.session.console_json))

    def test_no_live_console_still_opens_the_fallback_split(self):
        with TempState() as ts:
            api = FakeApi()
            store.write_json(ts.session.console_json, {"pane_id": "w1:p5", "terminal_id": "term_console", "pid": 999999, "open": True, "default_team": "alpha"})
            seen = []

            def open_pane(params):
                seen.append(params)
                if params["entrypoint"] == "compose":
                    raise HerdrTeamError("plugin_pane_open_failed", "popup already open", 1)
                return fake_plugin_pane_opened("console", console_pane("w1:p9"))

            api.set_response("plugin.pane.open", open_pane)
            payload = cmd_ui.open_pane(api, "compose", "w1:p4", dict(ts.env), "alpha", sleep=lambda _s: None, layout=ts.layout)
            self.assertEqual((payload["opened"], payload["fallback"], payload["pane_id"]), (True, "console", "w1:p9"))
            self.assertEqual([p["entrypoint"] for p in seen], ["compose", "compose", "console"])

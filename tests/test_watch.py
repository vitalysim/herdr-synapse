"""Watch: the ``watch``/``unwatch`` commands, the notifier's ``team_doing`` token, and ``o`` in the picker (0.21).

The commands run through ``cli.main`` with a ``FakeApi`` whose ``agent.list``
follows a fixed set of rows; the notifier through ``test_daemon.make_daemon``
with a fake clock; the picker through its pure model. Nothing starts a
daemon, a browser, or a harness.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from typing import Any, Dict, List
from unittest import mock

from herdr_team import activity as A
from herdr_team import features, identity, picker, store, tui_model
from herdr_team.tui_model import Intent, picker_apply_key
from support import FAKE_MEMBERS, FakeApi, TempState, fake_agent, whiteboard_on
from test_cmd_roster import json_out, live_api, run_cli
from test_daemon import make_daemon
from test_tui_model import picker_model

CLAUDE_ID = "11111111-aaaa-4bbb-8ccc-000000000001"
DOING = "herdr-synapse:watch"


def agents() -> List[Dict[str, Any]]:
    return [
        fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", terminal_title_stripped="Reviewing the retry loop"),
        fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working", terminal_title_stripped="Pulling Q3 exports"),
        fake_agent("w5:p1", "term_51", "codex", None, terminal_title_stripped="Refactoring the parser"),
        fake_agent("w5:p2", "term_52", "claude", "taken-name", terminal_title_stripped="Writing docs"),
        fake_agent("w3:p1", "term_sh", None, None),
    ]


def members(manager: bool = False) -> List[Dict[str, Any]]:
    rows = [dict(m) for m in FAKE_MEMBERS]
    if manager:
        rows[0]["manager"] = True  # alpha-reviewer
    rows.append({"name": "alpha-gone", "role": "gone", "kind": "claude", "terminal_id": "term_gone", "pane_id": "w9:p9", "status": "missing"})
    return rows


def doing_calls(api: Any) -> List[Dict[str, Any]]:
    return [p for m, p in api.calls if m == "pane.report_metadata" and p.get("source") == DOING]


class CliBase(unittest.TestCase):
    manager = False

    def setUp(self) -> None:
        self.ts = TempState(members=members(self.manager))
        self.addCleanup(self.ts.cleanup)
        self.api = live_api(agents())

    def env(self, pane: Any = None, **extra: str) -> Dict[str, str]:
        env = self.ts.env_with(HERDR_PANE_ID=pane, **extra) if pane else self.ts.env_with(**extra)
        env["HERDR_TEAM_NO_DAEMON"] = "1"
        return env

    def run_json(self, *argv: str, pane: Any = None) -> Any:
        return json_out(run_cli(["--json"] + list(argv), self.env(pane), self.api))

    def run_text(self, *argv: str, pane: Any = None) -> Any:
        return run_cli(list(argv), self.env(pane), self.api)

    def enable(self) -> None:
        features.set_layer(self.ts.session, True, "human", "cli")


class LayerOffTests(CliBase):
    def test_everything_but_list_refuses_while_the_layer_is_off(self):
        for argv in (("watch", "w5:p1"), ("watch", "show"), ("watch", "show", "w5:p1"), ("unwatch", "w5:p1")):
            code, out, err = self.run_json(*argv)
            self.assertEqual((code, err["code"], err["scope"]), (1, "whiteboard_off", "session"), argv)
            self.assertIn("whiteboard enable", err["message"])
        self.assertFalse(A.watch_path(self.ts.session).exists())
        self.assertEqual(doing_calls(self.api), [])
        code, out, err = self.run_json("watch", "list")
        self.assertEqual((code, out), (0, {"layer": False, "agents": [], "max": 20}))
        code, out, err = self.run_text("watch")
        self.assertEqual(code, 0)
        self.assertIn("whiteboard layer is off", out)
        self.assertIn("no agents are watched", out)

    def test_flags_are_kept_while_off(self):
        A.add_watch(self.ts.session, {"terminal_id": "term_51", "pane_id": "w5:p1", "kind": "codex"}, "human")
        code, out, err = self.run_json("watch", "list")
        self.assertEqual([a["terminal_id"] for a in out["agents"]], ["term_51"])
        self.assertTrue(out["agents"][0]["running"])


class WatchCommandTests(CliBase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_the_operator_flags_an_agent_outside_any_team_and_its_token_is_stamped(self):
        code, out, err = self.run_json("watch", "w5:p1")
        self.assertEqual(code, 0, err)
        self.assertTrue(out["added"])
        self.assertEqual({k: out["watching"][k] for k in ("terminal_id", "pane_id", "kind", "team", "by")},
                         {"terminal_id": "term_51", "pane_id": "w5:p1", "kind": "codex", "team": None, "by": "human"})
        self.assertEqual((out["card"]["state"], out["card"]["headline"], out["card"]["watched"]), ("idle", "Refactoring the parser", True))
        self.assertEqual((out["watched"], out["max"]), (1, 20))
        self.assertEqual(doing_calls(self.api), [{"pane_id": "w5:p1", "source": DOING, "tokens": {"team_doing": "Refactoring the parser"}, "ttl_ms": 60000}])
        self.assertTrue(out["token"]["stamped"])
        self.assertEqual([e["terminal_id"] for e in A.watched(self.ts.session)], ["term_51"])

    def test_targets_by_agent_name_member_name_and_terminal(self):
        self.assertEqual(self.run_json("watch", "taken-name")[1]["watching"]["terminal_id"], "term_52")
        code, out, err = self.run_json("watch", "alpha-worker")
        self.assertEqual((out["watching"]["terminal_id"], out["watching"]["team"], out["card"]["role"]), ("term_w1", "alpha", "worker"))
        self.assertEqual(self.run_json("watch", "term_r1")[1]["watching"]["name"], "alpha-reviewer")
        code, out, err = self.run_json("watch", "alpha-worker")
        self.assertFalse(out["added"])
        self.assertEqual(len(A.watched(self.ts.session)), 3)

    def test_unknown_and_stopped_agents_are_refused(self):
        code, out, err = self.run_json("watch", "nobody-here")
        self.assertEqual((code, err["code"]), (1, "agent_not_found"))
        code, out, err = self.run_json("watch", "alpha-gone")
        self.assertEqual((code, err["code"], err["terminal_id"]), (1, "agent_not_found", "term_gone"))
        self.assertIn("not a running agent", err["message"])
        code, out, err = self.run_json("watch", "w3:p1")  # a shell pane
        self.assertEqual(err["code"], "agent_not_found")
        self.assertEqual(A.watched(self.ts.session), [])

    def test_an_agent_may_not_flag_anyone(self):
        code, out, err = self.run_json("watch", "w5:p1", pane="w2:p2")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertIn("operator in person", err["message"])
        self.assertEqual(A.watched(self.ts.session), [])
        code, out, err = self.run_json("unwatch", "w5:p1", pane="w2:p2")
        self.assertEqual(err["code"], "author_mismatch")
        audits = [e for e in identity.read_audit(self.ts.layout, "alpha") if e["event"] == "author_mismatch"]
        self.assertEqual([e["details"]["action"] for e in audits], ["watch", "unwatch"])

    def test_the_limit(self):
        for i in range(A.MAX_WATCHED):
            A.add_watch(self.ts.session, {"terminal_id": "term_f{}".format(i)}, "human")
        code, out, err = self.run_json("watch", "w5:p1")
        self.assertEqual((code, err["code"], err["max"]), (1, "watch_limit", 20))

    def test_unwatch_drops_the_flag_and_clears_the_token(self):
        self.run_json("watch", "w5:p1")
        code, out, err = self.run_json("unwatch", "w5:p1")
        self.assertEqual(code, 0, err)
        self.assertEqual((out["removed"]["terminal_id"], out["cleared"], out["watched"]), ("term_51", ["w5:p1"], 0))
        self.assertEqual(doing_calls(self.api)[-1], {"pane_id": "w5:p1", "source": DOING, "tokens": {"team_doing": None}})
        code, out, err = self.run_json("unwatch", "w5:p1")
        self.assertEqual((code, out["removed"], out["cleared"]), (0, None, []))
        code, out, err = self.run_text("unwatch", "w5:p1")
        self.assertIn("was not watched", out)

    def test_an_agent_that_exited_is_unwatched_by_the_name_its_flag_recorded(self):
        A.add_watch(self.ts.session, {"terminal_id": "term_old", "pane_id": "w7:p7", "name": "ghost", "kind": "claude"}, "human")
        code, out, err = self.run_json("unwatch", "ghost")
        self.assertEqual((code, out["removed"]["terminal_id"], out["cleared"]), (0, "term_old", ["w7:p7"]))

    def test_list_names_the_flags_and_whether_they_run(self):
        self.run_json("watch", "w5:p1")
        A.add_watch(self.ts.session, {"terminal_id": "term_old", "pane_id": "w7:p7", "name": "ghost", "kind": "claude"}, "human")
        code, out, err = self.run_json("watch", "list")
        self.assertTrue(out["layer"])
        self.assertEqual([(a["terminal_id"], a["running"]) for a in out["agents"]], [("term_51", True), ("term_old", False)])
        code, text, err = self.run_text("watch", "list")
        self.assertIn("2 of 20 watched", text)
        self.assertIn("ghost", text)
        self.assertIn("(not running)", text)
        # anyone may read the flags
        self.assertEqual(self.run_json("watch", "list", pane="w2:p2")[0], 0)

    def test_usage(self):
        for argv in (("watch", "a", "b"), ("watch", "list", "x"), ("watch", "show", "a", "b")):
            code, out, err = self.run_json(*argv)
            self.assertEqual((code, err["code"]), (2, "usage"), argv)

    def test_human_text(self):
        code, out, err = self.run_text("watch", "w5:p1")
        self.assertIn("watching", out)
        self.assertIn("Refactoring the parser", out)
        self.assertIn("herdr-synapse watch show w5:p1", out)
        code, out, err = self.run_text("watch", "w5:p1")
        self.assertIn("already watching", out)
        code, out, err = self.run_text("unwatch", "w5:p1")
        self.assertIn("stopped watching", out)


class ShowTests(CliBase):
    manager = True

    def setUp(self) -> None:
        super().setUp()
        self.enable()
        A.add_watch(self.ts.session, {"terminal_id": "term_51", "pane_id": "w5:p1", "kind": "codex"}, "human")

    def test_the_operator_sees_the_watched_agents_and_a_teams_members(self):
        code, out, err = self.run_json("watch", "show")
        self.assertEqual(code, 0, err)
        self.assertEqual((out["authority"], [c["key"] for c in out["cards"]]), ("operator", ["term_51"]))
        code, out, err = self.run_json("--team", "alpha", "watch", "show")
        self.assertEqual([c["key"] for c in out["cards"]], ["term_51", "term_r1", "term_w1", "term_gone"])
        self.assertEqual(out["cards"][-1]["state"], "not_running")
        code, out, err = self.run_json("watch", "show", "taken-name")
        self.assertEqual([(c["key"], c["watched"], c["headline"]) for c in out["cards"]], [("term_52", False, "Writing docs")])

    def test_the_manager_sees_only_its_own_members(self):
        code, out, err = self.run_json("watch", "show", pane="w2:p1")
        self.assertEqual(code, 0, err)
        self.assertEqual((out["authority"], out["team"]), ("manager", "alpha"))
        self.assertEqual([c["key"] for c in out["cards"]], ["term_r1", "term_w1", "term_gone"])
        self.assertEqual(self.run_json("watch", "show", "alpha-worker", pane="w2:p1")[1]["cards"][0]["key"], "term_w1")
        code, out, err = self.run_json("watch", "show", "w5:p1", pane="w2:p1")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertIn("own team", err["message"])
        shows = [e for e in identity.read_audit(self.ts.layout, "alpha") if e["event"] == "watch_show"]
        self.assertEqual(shows[0]["details"]["authority"], "manager")

    def test_a_plain_member_may_not(self):
        code, out, err = self.run_json("watch", "show", pane="w2:p2")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))

    def test_text_cards(self):
        code, out, err = self.run_text("--team", "alpha", "watch", "show")
        self.assertEqual(code, 0, err)
        self.assertIn("◉ w5:p1 · codex · w5:p1 · no team · idle", out)
        self.assertIn("alpha-worker · claude · w2:p2 · team alpha/worker · working", out)
        self.assertIn("activity: unknown", out)
        self.assertIn("not running", out)


class RenderTests(unittest.TestCase):
    def test_a_full_card(self):
        from herdr_team import cmd_watch

        card = {"key": "t", "name": "alpha-worker", "kind": "claude", "profile": "tester", "pane_id": "w2:p2", "team": "alpha", "role": "worker",
                "watched": True, "state": "working", "headline": "Pulling Q3 exports", "doing": "▶ 2/3 fetch",
                "plan": {"steps": [{"text": "read", "status": "completed"}, {"text": "fetch", "status": "in_progress"}, {"text": "chart", "status": "pending"}],
                         "current": 2, "total": 3, "source": "todo"},
                "actions": [{"icon": "run", "text": "python pull.py", "at": "2026-09-26T10:00:00.000Z"}], "files": ["src/pull.py"],
                "context": {"percent": 41.2}, "last_prompt": "Pull it", "reader": "claude", "reader_error": None}
        text = "\n".join(cmd_watch.render_card(card))
        for piece in ("◉ alpha-worker · claude/tester · w2:p2 · team alpha/worker · working", "plan 2/3 (todo):",
                      "✓ read", "▶ fetch", "· chart", "run    python pull.py", "files: src/pull.py", "context 41%", "last prompt: Pull it"):
            self.assertIn(piece, text)
        self.assertIn("no reader for gemini", "\n".join(cmd_watch.render_card({"kind": "gemini", "reader": None, "state": "idle"})))
        self.assertIn("no agents are watched", cmd_watch.render([]))


# --------------------------------------------------------------------------
# the notifier


class DaemonWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.rows = agents()
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.api.set_response("agent.list", lambda params: {"type": "agent_list", "agents": [dict(r) for r in self.rows]})
        self.d.poll_agents(force=True)

    def refresh(self, advance: float = 0.0) -> List[Dict[str, Any]]:
        self.clock.advance(advance)
        before = len(doing_calls(self.api))
        self.d.poll_agents(force=True)
        self.d.refresh_watch(self.d.now_ms())
        return doing_calls(self.api)[before:]

    def watch(self, terminal: str = "term_51", pane: str = "w5:p1") -> None:
        A.add_watch(self.ts.session, {"terminal_id": terminal, "pane_id": pane}, "human")

    def test_nothing_is_read_or_stamped_while_the_layer_is_off(self):
        self.watch()
        self.assertEqual(self.refresh(), [])
        self.assertEqual(self.d.watch_stamps, {})
        self.assertFalse((self.ts.session.root / A.WATCH_CACHE_DIR).exists(), "no transcript was read")

    def test_stamp_restamp_and_the_refresh_cadence(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.watch()
        self.assertEqual(self.refresh(), [{"pane_id": "w5:p1", "source": DOING, "tokens": {"team_doing": "Refactoring the parser"}, "ttl_ms": 60000}])
        self.assertEqual(self.refresh(5.0), [], "the refresh runs every 15 s")
        self.assertEqual(self.refresh(11.0), [], "an unchanged line is not restamped before 30 s")
        self.assertEqual(len(self.refresh(16.0)), 1, "then it is, well inside the 60 s TTL")
        self.rows[2]["terminal_title_stripped"] = "Running the parser tests"
        self.assertEqual(self.refresh(16.0)[0]["tokens"], {"team_doing": "Running the parser tests"})

    def test_unwatch_layer_off_and_exit_clear_the_token(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.watch()
        self.watch("term_52", "w5:p2")
        self.assertEqual(len(self.refresh()), 2)
        A.remove_watch(self.ts.session, "term_52")
        self.assertEqual(self.refresh(16.0), [{"pane_id": "w5:p2", "source": DOING, "tokens": {"team_doing": None}}])
        self.rows = [r for r in self.rows if r["terminal_id"] != "term_51"]
        self.assertEqual(self.refresh(16.0), [{"pane_id": "w5:p1", "source": DOING, "tokens": {"team_doing": None}}])
        self.assertEqual(self.refresh(16.0), [], "cleared once")
        self.rows = agents()
        self.assertEqual(len(self.refresh(16.0)), 1)
        features.set_layer(self.ts.session, False, "human", "cli")
        self.assertEqual(self.refresh(16.0), [{"pane_id": "w5:p1", "source": DOING, "tokens": {"team_doing": None}}])
        self.assertEqual(self.d.watch_stamps, {})
        self.assertTrue(A.watched(self.ts.session), "the flags are kept")

    def test_a_moved_pane_is_cleared_where_it_was_and_the_flag_follows(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.watch()
        self.refresh()
        self.rows[2].update(pane_id="w6:p3", workspace_id="w6")
        calls = self.refresh(16.0)
        self.assertEqual(calls, [
            {"pane_id": "w5:p1", "source": DOING, "tokens": {"team_doing": None}},
            {"pane_id": "w6:p3", "source": DOING, "tokens": {"team_doing": "Refactoring the parser"}, "ttl_ms": 60000},
        ])
        self.assertEqual(A.watched(self.ts.session)[0]["pane_id"], "w6:p3")

    def test_a_watched_members_plan_step_is_its_line(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        path = self.ts.home / ".claude" / "projects" / "-tmp-work" / "{}.jsonl".format(CLAUDE_ID)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-26T10:00:00.000Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": [
                {"content": "fetch Q3 exports", "status": "in_progress"}, {"content": "chart", "status": "pending"}]}}]}}) + "\n", encoding="utf-8")
        self.rows[1]["agent_session"] = {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": CLAUDE_ID}
        self.watch("term_w1", "w2:p2")
        self.assertEqual(self.refresh()[0]["tokens"], {"team_doing": "▶ 1/2 fetch Q3 exports"})

    def test_stale_read_caches_of_unwatched_agents_are_swept(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.watch()
        stale, watched_cache = A.cache_path(self.ts.session, "term_gone"), A.cache_path(self.ts.session, "term_51")
        store.write_json(stale, {"v": 1})
        old = time.time() - 2 * A.CACHE_MAX_AGE_S
        os.utime(stale, (old, old))
        self.refresh()
        self.assertFalse(stale.exists())
        self.assertTrue(watched_cache.exists())

    def test_the_tick_runs_the_refresh(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.watch()
        self.d.tick()
        self.assertEqual([c["tokens"] for c in doing_calls(self.api)], [{"team_doing": "Refactoring the parser"}])

    def test_teardown_clears_what_it_stamped(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.watch()
        self.refresh()
        self.d.teardown_projections()
        self.assertEqual(doing_calls(self.api)[-1], {"pane_id": "w5:p1", "source": DOING, "tokens": {"team_doing": None}})


class CanvasNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.scan_teams(force=True)

    def test_flushed_every_30_s_only_for_teams_whose_canvas_is_on(self):
        from herdr_team import canvas

        with mock.patch.object(canvas, "flush_notices", return_value=[]) as flush:
            self.d.flush_canvas_notices(self.d.now_ms())
            flush.assert_not_called()  # the layer is off
            features.set_layer(self.ts.session, True, "human", "cli")
            self.clock.advance(31.0)
            self.d.flush_canvas_notices(self.d.now_ms())
            flush.assert_not_called()  # the layer alone turns no team's canvas on
            features.set_team(self.ts.team, enabled=True)
            self.d.scan_teams(force=True)
            self.clock.advance(31.0)
            self.d.flush_canvas_notices(self.d.now_ms())
            self.assertEqual(flush.call_count, 1)
            self.assertEqual(flush.call_args[0][1].name, "alpha")
            self.clock.advance(10.0)
            self.d.flush_canvas_notices(self.d.now_ms())
            self.assertEqual(flush.call_count, 1)
            features.set_team(self.ts.team, enabled=False)
            self.d.scan_teams(force=True)
            self.clock.advance(31.0)
            self.d.flush_canvas_notices(self.d.now_ms())
            self.assertEqual(flush.call_count, 1)

    def test_a_failing_canvas_is_logged_not_raised(self):
        from herdr_team import canvas

        whiteboard_on(self.ts.session, self.ts.team)
        self.d.scan_teams(force=True)
        with mock.patch.object(canvas, "flush_notices", side_effect=RuntimeError("boom")):
            self.d.flush_canvas_notices(self.d.now_ms())
        self.assertTrue(any("canvas notices failed: RuntimeError: boom" in line for line in self.d.logged), self.d.logged)


# --------------------------------------------------------------------------
# the picker


ALPHA = {"alpha": [dict(m) for m in FAKE_MEMBERS]}


class PickerKeyTests(unittest.TestCase):
    def model(self, layer: Any = True, watched: Any = ()) -> tui_model.PickerModel:
        model = picker_model(ALPHA, focused=None)
        model.watch_layer = layer
        model.watched = set(watched)
        return model

    def test_o_toggles_an_unassigned_agent_and_a_member(self):
        model = self.model()
        tui_model.focus_node(model, "pane:w1:p3")
        self.assertEqual(picker_apply_key(model, "o"), Intent("agent_watch", {
            "pane_id": "w1:p3", "terminal_id": "term_a", "member": "w1:p3", "team": "", "watch": True}))
        model.watched = {"term_a"}
        self.assertFalse(picker_apply_key(model, "o").args["watch"])
        tui_model.focus_node(model, "member:alpha/alpha-worker")
        self.assertEqual(picker_apply_key(model, "o"), Intent("agent_watch", {
            "pane_id": "w2:p2", "terminal_id": "term_w1", "member": "alpha-worker", "team": "alpha", "watch": True}))

    def test_o_explains_itself_instead_of_acting(self):
        model = self.model(layer=False)
        tui_model.focus_node(model, "pane:w1:p3")
        # While the whiteboard is off, o asks to turn it on and watch (test_picker_whiteboard pins the answer).
        self.assertIsNone(picker_apply_key(model, "o"))
        self.assertIsNone(model.error)
        self.assertIn("The whiteboard is off. Turn it on and watch w1:p3?", model.status)
        self.assertEqual(model.pending_action.kind, "whiteboard_steps")
        model = self.model()
        tui_model.focus_node(model, "team:alpha")
        self.assertIsNone(picker_apply_key(model, "o"))
        self.assertIn("cursor on an agent", model.error)
        tui_model.focus_node(model, "pane:w1:p10")  # still launching
        self.assertIsNone(picker_apply_key(model, "o"))
        self.assertIn("not running", model.error)
        model.paste_mode = True
        tui_model.focus_node(model, "pane:w1:p3")
        self.assertIsNone(picker_apply_key(model, "o"), "a paste never fires keys")

    def test_a_member_without_a_live_row_can_still_be_unwatched(self):
        roster = [dict(m) for m in FAKE_MEMBERS] + [{"name": "alpha-gone", "role": "gone", "kind": "claude", "terminal_id": "term_gone", "status": "missing"}]
        model = picker_model({"alpha": roster}, focused=None)
        model.watch_layer = True
        tui_model.focus_node(model, "member:alpha/alpha-gone")
        self.assertIsNone(picker_apply_key(model, "o"))
        model.watched = {"term_gone"}
        self.assertEqual(picker_apply_key(model, "o").args, {"pane_id": "", "terminal_id": "term_gone", "member": "alpha-gone", "team": "alpha", "watch": False})

    def test_watched_rows_carry_the_mark_and_the_legend_has_the_key(self):
        model = self.model(watched={"term_a", "term_w1"})
        lines = tui_model.picker_lines(model, 100, 30)
        marked = [line for line in lines if "◉" in line]
        self.assertEqual(len(marked), 2, lines)
        self.assertTrue(any("alpha-worker" in line for line in marked))
        self.assertTrue(any("w1:p3" in line for line in marked))
        self.assertTrue(any("o watch" in line for line in lines))
        model.ascii_only = True
        ascii_lines = tui_model.picker_lines(model, 100, 30)
        self.assertEqual(len([line for line in ascii_lines if line[2:4] == "@ " or "] @ " in line]), 2, ascii_lines)
        plain = self.model()
        self.assertFalse(any("◉" in line for line in tui_model.picker_lines(plain, 100, 30)))

    def test_the_legend_still_wraps_at_narrow_widths(self):
        model = self.model(watched={"term_a"})
        for width in (42, 30):
            lines = tui_model.picker_lines(model, width, 24)
            flattened = " ".join(line.strip() for line in lines)
            for action in ("Enter acts", "g go to pane", "o watch", "b board", "x dissolve", "Esc quit"):
                self.assertIn(action, flattened, width)
            self.assertTrue(all(tui_model.display_width(line) <= width for line in lines))

    def test_a_watched_manager_row_keeps_its_style(self):
        roster = [dict(m) for m in FAKE_MEMBERS]
        roster[1]["manager"] = True
        model = picker_model({"alpha": roster}, focused=None)
        model.watched = {"term_w1"}
        lines = tui_model.picker_lines(model, 140, 30)
        styles = tui_model.picker_styles(model, lines)
        starred = [line for line, style in zip(lines, styles) if style == tui_model.STYLE_MANAGER]
        self.assertEqual(len(starred), 1)
        self.assertIn("◉", starred[0])

    def test_the_detail_line_says_what_o_does(self):
        model = self.model()
        tui_model.focus_node(model, "pane:w1:p3")
        self.assertIn("o watches it", tui_model._tree_detail(model, tui_model.picker_tree(model)))
        model.watched = {"term_a"}
        self.assertIn("o stops watching", tui_model._tree_detail(model, tui_model.picker_tree(model)))
        tui_model.focus_node(model, "member:alpha/alpha-worker")
        self.assertIn("o watches it", tui_model._tree_detail(model, tui_model.picker_tree(model)))
        model.watch_layer = False
        self.assertNotIn("o watch", tui_model._tree_detail(model, tui_model.picker_tree(model)))


class PickerActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.model = picker_model(ALPHA, focused=None)
        self.model.watch_layer = True
        self.api = live_api([fake_agent("w1:p3", "term_a", "codex", None)])

    def intent(self, watch: bool = True) -> Intent:
        return Intent("agent_watch", {"pane_id": "w1:p3", "terminal_id": "term_a", "member": "w1:p3", "team": "", "watch": watch})

    def test_it_is_a_picker_action_run_through_the_cli(self):
        self.assertIn("agent_watch", picker.ACTION_INTENTS)
        self.assertEqual(picker.watch_args(self.intent()), ["watch", "w1:p3"])
        self.assertEqual(picker.watch_args(self.intent(False)), ["unwatch", "term_a"])
        self.assertEqual(picker.ACTION_LABELS["agent_watch"].format(member="w1:p3", team=""), "updating the watch on w1:p3")

    def test_watch_then_unwatch_keeps_the_popup_open(self):
        with mock.patch("herdr_team.console.run_cli", return_value=(0, {"card": {"doing": "$ make"}}, None)) as run:
            self.assertTrue(picker.execute_action(self.intent(), self.model, self.api, self.ts.layout, {}))
            self.assertEqual(run.call_args[0][0], ["watch", "w1:p3"])
            self.assertEqual(self.model.watched, {"term_a"})
            self.assertIn("watching w1:p3; its sidebar row shows: $ make", self.model.status)
            self.assertTrue(picker.execute_action(self.intent(False), self.model, self.api, self.ts.layout, {}))
            self.assertEqual(run.call_args[0][0], ["unwatch", "term_a"])
        self.assertEqual(self.model.watched, set())
        self.assertEqual(self.model.status, "stopped watching w1:p3")

    def test_refusals_become_one_line(self):
        for code, expected in (("whiteboard_off", "whiteboard is off"), ("watch_limit", "at most 20"), ("author_mismatch", "operator in person"),
                               ("agent_not_found", "not running any more")):
            with mock.patch("herdr_team.console.run_cli", return_value=(1, None, {"code": code, "message": "m", "max": 20})):
                picker.execute_action(self.intent(), self.model, self.api, self.ts.layout, {})
            self.assertIn(expected, self.model.error, code)
            self.assertEqual(self.model.watched, set())

    def test_a_pane_that_changed_hands_is_not_watched(self):
        api = live_api([fake_agent("w1:p3", "term_other", "codex", None)])
        with mock.patch("herdr_team.console.run_cli") as run:
            picker.execute_action(self.intent(), self.model, api, self.ts.layout, {})
        run.assert_not_called()
        self.assertIn("changed while this was open", self.model.error)

    def test_the_model_reads_the_flags_and_the_layer(self):
        self.assertEqual(picker.watch_state(None), (set(), None))
        self.assertEqual(picker.watch_state(self.ts.layout), (set(), False))
        A.add_watch(self.ts.session, {"terminal_id": "term_a"}, "human")
        features.set_layer(self.ts.session, True, "human", "cli")
        api = FakeApi()
        api.set_response("agent.list", {"type": "agent_list", "agents": [fake_agent("w1:p3", "term_a", "codex", None)]})
        api.set_response("tab.list", {"type": "tab_list", "tabs": []})
        model = picker.build_model(api, {}, self.ts.layout)
        self.assertEqual((model.watched, model.watch_layer), ({"term_a"}, True))
        A.remove_watch(self.ts.session, "term_a")
        picker.refresh_rows(model, api, self.ts.layout)
        self.assertEqual(model.watched, set())


if __name__ == "__main__":
    unittest.main()

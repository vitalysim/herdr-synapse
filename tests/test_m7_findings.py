"""Regression tests for the plugin gaps found in the M7 UI rig run (2026-09-05, team4 rig).

- UI-02: the ``/peek`` box was cut from the head when taller than the feed, so a 20-row
  console showed blank leading rows and lost the member's spinner, prompt box, and status
  line, which sit at the bottom of ``agent read --source visible``.
- UI-03: ``console.read_key`` collapsed a burst of Escapes (or Esc then a control key) into
  one ``ESC`` and swallowed the control key; the picker went back one stage instead of six
  and ``ctrl+u`` never reached the editor.
- UI-06: ``view toggle --force`` with a foreign owner and a stale ``view.json`` saying
  ``on`` cleared the foreign view instead of replacing it with ours.
"""

from __future__ import annotations

import curses
import unittest
from typing import List, Union

from herdr_team import console, store, tui_model
from herdr_team.tui_model import ConsoleHeader, ConsoleModel
from support import FakeApi, FakeError, TempState
from test_cmd_roster import json_out, run_cli


class FakeScreen:
    """Just enough of a curses window for ``read_key``: a queue of ``get_wch`` results."""

    def __init__(self, keys: List[Union[str, int]]) -> None:
        self.queue = list(keys)
        self.delay = True

    def get_wch(self) -> Union[str, int]:
        if not self.queue:
            raise curses.error("no input")
        return self.queue.pop(0)

    def nodelay(self, flag: bool) -> None:
        self.delay = not flag

    def push_back(self, key: Union[str, int]) -> None:
        self.queue.insert(0, key)


def read_all(screen: FakeScreen) -> List[str]:
    from unittest import mock

    out: List[str] = []
    with mock.patch("curses.unget_wch", side_effect=screen.push_back), mock.patch("curses.ungetch", side_effect=screen.push_back):
        for _ in range(32):
            key = console.read_key(screen)
            if key is None:
                break
            out.append(key)
    return out


class EscapeBurstTests(unittest.TestCase):
    def test_burst_of_escapes_is_one_key_each(self):
        self.assertEqual(read_all(FakeScreen(list("\x1b\x1b\x1b\x1b\x1b\x1b"))), ["ESC"] * 6)

    def test_escape_then_control_key_keeps_the_control_key(self):
        # The UI-03 shape: six Escapes, ctrl+u, then typed text and Enter arriving in one read.
        keys = read_all(FakeScreen(list("\x1b\x1b\x1b\x1b\x1b\x1b\x15lead\r")))
        self.assertEqual(keys, ["ESC"] * 6 + ["CTRL_U", "l", "e", "a", "d", "ENTER"])

    def test_escape_then_keycode_keeps_the_keycode(self):
        self.assertEqual(read_all(FakeScreen(["\x1b", curses.KEY_DOWN])), ["ESC", "DOWN"])

    def test_real_sequences_still_decode(self):
        self.assertEqual(read_all(FakeScreen(list("\x1b[A"))), ["UP"])
        self.assertEqual(read_all(FakeScreen(list("\x1b\r"))), ["ALT_ENTER"])
        self.assertEqual(read_all(FakeScreen(list("\x1b[200~x\x1b[201~"))), ["PASTE_START", "x", "PASTE_END"])
        self.assertEqual(read_all(FakeScreen(["\x1b"])), ["ESC"])


class PeekTailTests(unittest.TestCase):
    def _model(self, peek: List[str], width: int = 60, height: int = 12) -> ConsoleModel:
        model = tui_model.build_console_model("alpha", {}, [], width=width, height=height)
        model.peek = peek
        return model

    def test_fit_peek_keeps_title_border_and_tail(self):
        box = tui_model.box(["line {}".format(i) for i in range(30)], 40, "peek x")
        fitted = tui_model.fit_peek(box, 8)
        self.assertEqual(len(fitted), 8)
        self.assertEqual(fitted[0], box[0], "the title border stays")
        self.assertEqual(fitted[-1], box[-1], "the bottom border stays")
        self.assertIn("line 29", fitted[-2], "the last screen line is shown")
        self.assertNotIn("line 0", "".join(fitted[1:]), "the head of the screen is what gets dropped")

    def test_fit_peek_short_box_is_untouched(self):
        box = tui_model.box(["a", "b"], 20, "t")
        self.assertEqual(tui_model.fit_peek(box, 10), box)
        self.assertEqual(tui_model.fit_peek(box, 0), [])
        self.assertEqual(tui_model.fit_peek(box, 1), [box[0]])

    def test_render_console_shows_bottom_of_peeked_screen(self):
        screen = [""] * 10 + ["  Ran 1 shell command", "", "✢ Twisting… (6s · ↓ 218 tokens)", "", "❯", "  ⏸ manual mode on · esc to interrupt"]
        model = self._model(tui_model.box(screen, 60, "peek alpha-reviewer"), width=60, height=12)
        lines = tui_model.render_console(model, 60, 12)
        joined = "\n".join(lines)
        self.assertIn("peek alpha-reviewer", joined)
        self.assertIn("esc to interrupt", joined, "the member's status line at the bottom of its screen is visible")
        self.assertIn("Twisting", joined)
        self.assertNotIn("❯", joined, "prompt markers stay neutralized")
        self.assertEqual(len(lines), 12)


class ViewToggleForeignTests(unittest.TestCase):
    def test_toggle_force_replaces_a_foreign_view_when_view_json_is_stale(self):
        with TempState() as ts:
            api = FakeApi()
            state = {"source": "plugin:herdr-team"}

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
            store.write_json(ts.session.view_json, {"view": "on", "source": "plugin:herdr-team", "label": "team:alpha"})
            state["source"] = "test.foreign"  # another source replaced our view; view.json still says on
            code, _, err = json_out(run_cli(["--json", "view", "toggle"], ts.env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "view_foreign")
            self.assertEqual(err["owner"], "test.foreign")
            self.assertEqual(state["source"], "test.foreign", "a refused toggle changes nothing")
            code, payload, err = json_out(run_cli(["--json", "view", "toggle", "--force"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["view"], "on")
            self.assertEqual(payload["owner"], "foreign")
            self.assertEqual(state["source"], "plugin:herdr-team", "--force replaces the foreign view with ours")
            self.assertEqual(store.read_json(ts.session.view_json)["view"], "on")

    def test_toggle_still_turns_own_view_off(self):
        with TempState() as ts:
            api = FakeApi()
            state = {"source": "plugin:herdr-team"}

            def clear(params):
                if params.get("source") not in (None, state["source"]):
                    raise FakeError("agent_view_source_mismatch", "view belongs to source {}".format(state["source"]))
                state["source"] = None
                return {"type": "ok"}

            api.set_response("agent.view.clear", clear)
            store.write_json(ts.session.view_json, {"view": "on", "source": "plugin:herdr-team", "label": "team:alpha"})
            code, payload, err = json_out(run_cli(["--json", "view", "toggle"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["view"], "off")
            self.assertEqual(payload["owner"], "own")
            self.assertIsNone(state["source"])


if __name__ == "__main__":
    unittest.main()

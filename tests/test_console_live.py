"""The console has to stay live: the tick must survive, and the feed must follow.

Three defects sat behind one report ("the team console is not refreshed
live"), reproduced against a running session where the console showed board
record #64 while the board was at #68, for minutes, with no input:

* the 250 ms tick was destroyed by the first Esc or paste, because
  ``nodelay(False)`` is ncurses' *blocking* mode and does not restore a
  ``wtimeout()`` set earlier;
* the feed's scroll offset was carried across every rebuild, so a single Up
  press pinned the reader N entries behind the tail for ever, with nothing on
  screen saying so;
* every 250 ms tick re-formatted every board record from seq 1.
"""

from __future__ import annotations

import curses
import os
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from herdr_team import console, tui_model
from test_m7_findings import FakeScreen


# --------------------------------------------------------------------------
# D2: the tick can never be lost


class EscapeKeepsTheTickTests(unittest.TestCase):
    def test_an_escape_leaves_the_caller_tick_armed(self):
        screen = FakeScreen(["\x1b"])
        with mock.patch.object(curses, "unget_wch", screen.push_back), mock.patch.object(curses, "ungetch", screen.push_back):
            self.assertEqual(console.read_key(screen, 250), "ESC")
        self.assertEqual(screen.mode, ("timeout", 250))

    def test_a_paste_marker_leaves_the_caller_tick_armed(self):
        screen = FakeScreen(list("\x1b[200~"))
        with mock.patch.object(curses, "unget_wch", screen.push_back), mock.patch.object(curses, "ungetch", screen.push_back):
            self.assertEqual(console.read_key(screen, 250), "PASTE_START")
        self.assertEqual(screen.mode, ("timeout", 250))

    def test_the_escape_drain_never_touches_nodelay(self):
        """nodelay(False) is blocking mode; it cannot restore a timeout."""
        screen = FakeScreen(list("\x1b[A"))
        with mock.patch.object(curses, "unget_wch", screen.push_back), mock.patch.object(curses, "ungetch", screen.push_back):
            console.read_key(screen, 250)
        self.assertEqual(screen.nodelay_calls, [])

    def test_a_blocking_caller_stays_blocking(self):
        screen = FakeScreen(["\x1b"])
        with mock.patch.object(curses, "unget_wch", screen.push_back), mock.patch.object(curses, "ungetch", screen.push_back):
            console.read_key(screen)
        self.assertEqual(screen.mode, "blocking")


class LoopWindow(FakeScreen):
    """A FakeScreen with the handful of window methods ``_loop`` calls."""

    def __init__(self, keys, height: int = 24, width: int = 80) -> None:
        super().__init__(keys)
        self._size = (height, width)

    def get_wch(self):
        # The real loop exits on a quit intent; these tests stub ``apply_key``,
        # so an exhausted queue is what ends them.
        if not self.queue:
            raise StopIteration
        return self.queue.pop(0)

    def getmaxyx(self):
        return self._size

    def keypad(self, flag):
        return None

    def erase(self):
        return None

    def refresh(self):
        return None

    def move(self, y, x):
        return None

    def addnstr(self, *a, **kw):
        return None

    def clrtoeol(self):
        return None


class LoopTickTests(unittest.TestCase):
    """Drive ``console._loop`` with curses stubbed out."""

    def run_loop(self, keys, refreshes: List[int], escdelay: List[int]):
        screen = LoopWindow(list(keys))
        state = mock.Mock()
        state.layout = mock.Mock()
        state.team = mock.Mock()
        state.env = {}
        state.height, state.width = 24, 80
        model = mock.Mock()
        model.width = model.height = 0

        def fake_refresh(*a, **kw):
            refreshes.append(1)

        with mock.patch.object(curses, "raw", lambda: None), \
             mock.patch.object(curses, "noecho", lambda: None), \
             mock.patch.object(curses, "curs_set", lambda n: None), \
             mock.patch.object(curses, "set_escdelay", lambda ms: escdelay.append(ms), create=True), \
             mock.patch.object(console, "enable_bracketed_paste", lambda: None), \
             mock.patch.object(console, "disable_bracketed_paste", lambda: None), \
             mock.patch.object(console, "init_colors", lambda: False), \
             mock.patch.object(console, "build_model", lambda *a, **kw: model), \
             mock.patch.object(console, "refresh", fake_refresh), \
             mock.patch.object(console, "draw_lines", lambda *a, **kw: None), \
             mock.patch.object(console, "style_attr", lambda *a, **kw: 0), \
             mock.patch.object(console, "input_cursor_position", lambda *a, **kw: (0, 0)), \
             mock.patch.object(tui_model, "render_console_styled", lambda *a, **kw: [("x", "plain")]), \
             mock.patch.object(tui_model, "input_lines", lambda *a, **kw: [""]), \
             mock.patch.object(tui_model, "apply_key", lambda m, k: None), \
             mock.patch.object(curses, "unget_wch", screen.push_back), \
             mock.patch.object(curses, "ungetch", screen.push_back):
            try:
                console._loop(screen, state, mock.Mock())
            except (curses.error, StopIteration):
                pass
        return screen

    def test_a_printable_key_still_refreshes_on_the_wall_clock(self):
        """The refresh used to hang off ``key is None``, so typing froze the feed."""
        refreshes: List[int] = []
        ticks = iter([0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0])
        with mock.patch.object(time, "monotonic", lambda: next(ticks)):
            self.run_loop(["x", "x", "\x03"], refreshes, [])
        self.assertGreaterEqual(len(refreshes), 1)

    def test_the_loop_rearms_the_tick_every_pass(self):
        screen = self.run_loop(["x", "x", "\x03"], [], [])
        armed = [ms for ms in screen.timeout_calls if ms == console.TICK_MS]
        self.assertGreaterEqual(len(armed), 2)

    def test_the_loop_shortens_the_escape_delay(self):
        escdelay: List[int] = []
        self.run_loop(["\x03"], [], escdelay)
        self.assertEqual(escdelay, [console.ESCDELAY_MS])


class SiblingTuiTests(unittest.TestCase):
    def test_picker_and_compose_pass_their_tick_to_read_key(self):
        import inspect

        from herdr_team import compose, picker

        for module in (picker, compose):
            source = inspect.getsource(module._loop)
            self.assertIn("read_key(stdscr, int(TICK_S * 1000))", source, module.__name__)
            self.assertIn("stdscr.timeout(int(TICK_S * 1000))", source, module.__name__)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# D1: the feed follows the tail


def _record(seq: int, text: str = "hello") -> Dict[str, Any]:
    return {
        "v": 1, "seq": seq, "ts": "2026-09-06T12:00:{:02d}.000Z".format(min(seq, 59)),
        "from": "alpha-worker", "from_kind": "claude", "to": ["all"], "kind": "note",
        "text": text, "refs": [], "reply_to": None,
    }


def _model(count: int) -> tui_model.ConsoleModel:
    return tui_model.build_console_model("alpha", {}, [_record(n) for n in range(1, count + 1)])


class FollowTheTailTests(unittest.TestCase):
    def test_a_fresh_model_follows(self):
        model = _model(3)
        self.assertTrue(model.follow)
        self.assertEqual(model.scroll, 0)

    def test_up_stops_following_and_says_so(self):
        model = _model(5)
        tui_model.apply_key(model, "UP")
        self.assertFalse(model.follow)
        self.assertEqual(model.scroll, 1)
        self.assertIn("End", model.status or "")

    def test_reaching_the_bottom_resumes_following(self):
        model = _model(5)
        tui_model.apply_key(model, "UP")
        tui_model.apply_key(model, "DOWN")
        self.assertTrue(model.follow)
        self.assertEqual(model.scroll, 0)

    def test_end_jumps_to_the_latest_only_while_scrolled(self):
        model = _model(5)
        tui_model.apply_key(model, "UP")
        tui_model.apply_key(model, "END")
        self.assertTrue(model.follow)
        self.assertEqual(model.scroll, 0)
        # Already following: End is end-of-line again, and must not touch scroll.
        model.input, model.cursor = "abc", 0
        tui_model.apply_key(model, "END")
        self.assertEqual(model.cursor, 3)
        self.assertEqual(model.scroll, 0)

    def test_esc_also_returns_to_the_latest(self):
        model = _model(5)
        tui_model.apply_key(model, "PGUP")
        self.assertFalse(model.follow)
        tui_model.apply_key(model, "ESC")
        self.assertTrue(model.follow)
        self.assertEqual(model.scroll, 0)

    def test_posting_snaps_back_to_the_tail(self):
        model = _model(5)
        tui_model.apply_key(model, "UP")
        for ch in "hello":
            tui_model.apply_key(model, ch)
        intent = tui_model.apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "post")
        self.assertTrue(model.follow)
        self.assertEqual(model.scroll, 0)

    def test_a_clamp_to_the_tail_resumes_following(self):
        model = _model(1)
        model.follow, model.scroll = False, 99
        tui_model.visible_feed_rows(model, 5)
        self.assertEqual(model.scroll, 0)
        self.assertTrue(model.follow)

    def test_a_scrolled_feed_shows_how_many_are_below(self):
        model = _model(20)
        model.follow, model.scroll = False, 4
        text = "\n".join(tui_model.render_console(model, 100, 24))
        self.assertIn("4 newer below", text)
        self.assertIn("End returns to the latest", text)

    def test_a_following_feed_shows_no_indicator(self):
        model = _model(20)
        text = "\n".join(tui_model.render_console(model, 100, 24))
        self.assertNotIn("newer below", text)

    def test_the_indicator_keeps_the_screen_geometry(self):
        for width, height in ((40, 12), (60, 20), (100, 30), (25, 8)):
            model = _model(20)
            model.follow, model.scroll = False, 3
            lines = tui_model.render_console(model, width, height)
            self.assertEqual(len(lines), height, (width, height))
            for line in lines:
                self.assertLessEqual(tui_model.display_width(line), width, (width, height, line))

    def test_the_indicator_has_an_ascii_form(self):
        line = tui_model.feed_gap_line(4, 60, ascii_only=True)
        self.assertNotIn("─", line)
        self.assertIn("4 newer below", line)

    def test_one_newer_is_singular(self):
        self.assertIn("1 newer below", tui_model.feed_gap_line(1, 60))

    def test_the_help_names_the_jump_key(self):
        self.assertIn("End", "\n".join(tui_model.help_lines()))


# --------------------------------------------------------------------------
# D3: an idle console must not rebuild what nobody can see


class IdleCostTests(unittest.TestCase):
    """An idle console re-formatted every board record four times a second."""

    def setUp(self):
        from support import TempState

        from herdr_team import store as _store

        self.state = TempState()
        self.addCleanup(self.state.cleanup)
        self.layout = self.state.layout
        # A board with content, or "formats no entries" would pass vacuously.
        for index in range(25):
            _store.BoardStore(self.state.team).append({
                "from": "alpha-worker", "from_kind": "claude", "from_pane": "w2:p2", "from_terminal": "t",
                "from_gen": 1, "to": ["all"], "to_role": None, "kind": "note",
                "text": "post number {}".format(index), "refs": [], "reply_to": None,
            })
        self.cstate = console.ConsoleState(self.layout, self.state.team_name, {})
        self.model = console.build_model(self.layout, self.state.team_name, self.cstate)
        console.refresh(self.model, self.layout, self.cstate)
        self.assertTrue(self.model.feed, "the fixture must have a non-empty feed")

    def count_rebuilds(self, calls: int = 5):
        seen: List[int] = []
        real = tui_model.build_console_model

        def counting(*a, **kw):
            seen.append(1)
            return real(*a, **kw)

        with mock.patch.object(tui_model, "build_console_model", counting):
            for _ in range(calls):
                console.refresh(self.model, self.layout, self.cstate)
        return len(seen)

    def test_an_unchanged_tick_does_not_rebuild(self):
        self.assertEqual(self.count_rebuilds(5), 0)

    def test_an_idle_tick_formats_no_feed_entries(self):
        seen: List[int] = []
        real = tui_model.feed_entry

        def counting(*a, **kw):
            seen.append(1)
            return real(*a, **kw)

        with mock.patch.object(tui_model, "feed_entry", counting):
            for _ in range(20):
                console.refresh(self.model, self.layout, self.cstate)
        self.assertEqual(seen, [])

    def test_a_board_append_is_visible_on_the_next_tick(self):
        """The guard that stops the gate re-creating the reported symptom."""
        from herdr_team import store as _store

        _store.BoardStore(self.state.team).append({
            "from": "alpha-worker", "from_kind": "claude", "from_pane": "w2:p2", "from_terminal": "t",
            "from_gen": 1, "to": ["all"], "to_role": None, "kind": "note", "text": "brand new",
            "refs": [], "reply_to": None,
        })
        console.refresh(self.model, self.layout, self.cstate)
        self.assertTrue(any("brand new" in (e.get("text") or "") for e in self.model.feed))

    def test_a_stale_model_is_rebuilt_even_with_no_file_change(self):
        base = time.monotonic()
        with mock.patch.object(time, "monotonic", lambda: base + console.STALE_REBUILD_S + 1):
            self.assertEqual(self.count_rebuilds(1), 1)

    def test_a_resize_rebuilds(self):
        self.model.width = 40
        self.assertEqual(self.count_rebuilds(1), 1)

    def test_every_file_build_model_reads_is_watched(self):
        """A reader added without a watch path is how a console goes stale."""
        from herdr_team import store as _store

        read: List[str] = []
        real_json, real_bytes = _store.read_json, _store.read_bytes

        def spy_json(path, *a, **kw):
            read.append(os.fspath(path))
            return real_json(path, *a, **kw)

        def spy_bytes(path, *a, **kw):
            read.append(os.fspath(path))
            return real_bytes(path, *a, **kw)

        with mock.patch.object(_store, "read_json", spy_json), mock.patch.object(_store, "read_bytes", spy_bytes):
            console.build_model(self.layout, self.state.team_name, self.cstate)

        watched = {os.fspath(p) for p in console.watch_paths(self.layout, self.state.team_name)}
        cursors = os.fspath(self.layout.team(self.state.team_name).cursors_dir)
        for path in read:
            if path.startswith(cursors):
                continue
            self.assertIn(path, watched, "{} is read but not watched".format(path))

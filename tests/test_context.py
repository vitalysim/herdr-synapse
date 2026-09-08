"""Context awareness (0.9.0): how full each member is, and compacting or clearing it.

Every test here fails against 0.8.0: ``herdr_team.context`` did not exist, no
reading reached ``who`` or the sidebar, nothing was ever said when a member
filled up, and there was no ``compact`` or ``clear``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from pathlib import Path

from herdr_team import cmd_board, context, render, roster, store
from herdr_team import daemon as D
from herdr_team import tui_model
from herdr_team.errors import HerdrTeamError
from support import FAKE_AGENTS, FakeApi, TempState, fake_agent, fake_explain
from test_cmd_board import json_out, run_cli, write_live_daemon
from test_daemon import make_daemon, post, ticks

STRONG_IDLE = {"type": "agent_explain", "explain": {"state": "idle", "matched_rule": {"id": "codex.idle", "region": "after_last_prompt_marker"}}}
#: The Claude member of the shared fixture team, whose transcript the poll tests write.
MEMBER = "alpha-worker"
#: The Codex member, idle in the fixture, so the control tests can be delivered.
TARGET = "alpha-reviewer"


def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def claude_record(used, model="claude-opus-5", at="2026-09-08T10:00:00.000Z"):
    return {"type": "assistant", "timestamp": at, "message": {
        "model": model,
        "usage": {"input_tokens": 4, "cache_creation_input_tokens": 6, "cache_read_input_tokens": used - 20, "output_tokens": 10},
    }}


def codex_record(used, window=272_000, at="2026-09-08T10:00:00.000Z"):
    return {"type": "event_msg", "timestamp": at, "payload": {"type": "token_count", "info": {
        "model_context_window": window,
        "last_token_usage": {"total_tokens": used},
        "total_token_usage": {"total_tokens": used * 9},
    }}}


def opencode_db_with(home: Path, session_value, rows):
    path = context.opencode_db(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(os.fspath(path))
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    for index, data in enumerate(rows):
        conn.execute("INSERT INTO message VALUES (?,?,?,?)", ("m{}".format(index), session_value, index, json.dumps(data)))
    conn.commit()
    conn.close()
    return path


# --------------------------------------------------------------------------
# the readers


class ReadingTests(unittest.TestCase):
    def test_percent_and_json_shape(self):
        reading = context.Reading(used=94_000, window=200_000, source="/t.jsonl", model="claude-opus-5", at="2026-09-08T10:00:00Z")
        self.assertEqual(reading.percent, 47.0)
        self.assertEqual(reading.to_json(), {
            "used": 94_000, "window": 200_000, "percent": 47.0, "model": "claude-opus-5",
            "source": "/t.jsonl", "at": "2026-09-08T10:00:00Z"})
        # a window we failed to read is reported as 0%, never as a division error
        self.assertEqual(context.Reading(used=10, window=0, source="x").percent, 0.0)

    def test_window_widens_to_fit_what_was_actually_observed(self):
        # a model in the table keeps its window
        self.assertEqual(context.window_for("claude-opus-5", 10), 200_000)
        # a model that is not in the table falls back to the smallest tier
        self.assertEqual(context.window_for("something-new", 10), context.DEFAULT_WINDOW)
        # a session demonstrably carrying more is on a larger variant, not over 100 %
        self.assertEqual(context.window_for("claude-opus-5", 930_000), 1_000_000)
        self.assertEqual(context.window_for(None, 2_000_000), 2_000_000)


class ClaudeReaderTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.home = Path(self.ts.tmp) / "harness-home"

    def test_reads_the_newest_usage_including_the_cache_reads(self):
        path = write_jsonl(self.home / "t.jsonl", [claude_record(100_000), {"type": "user"}, claude_record(188_000)])
        reading = context.read_claude(os.fspath(path))
        self.assertEqual((reading.used, reading.window, reading.model), (188_000, 200_000, "claude-opus-5"))
        self.assertEqual(reading.source, os.fspath(path))
        self.assertEqual(round(reading.percent), 94)

    def test_a_missing_truncated_or_usageless_file_reads_nothing_rather_than_guessing(self):
        self.assertIsNone(context.read_claude(None))
        self.assertIsNone(context.read_claude(os.fspath(self.home / "gone.jsonl")))
        empty = write_jsonl(self.home / "empty.jsonl", [])
        self.assertIsNone(context.read_claude(os.fspath(empty)))
        nousage = write_jsonl(self.home / "nousage.jsonl", [{"type": "user", "message": {"role": "user"}}])
        self.assertIsNone(context.read_claude(os.fspath(nousage)))
        # a half-written last line is normal on a live file: skip it, use the one before
        broken = self.home / "broken.jsonl"
        write_jsonl(broken, [claude_record(50_000)])
        with broken.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "assistant", "mess')
        self.assertEqual(context.read_claude(os.fspath(broken)).used, 50_000)

    def test_the_reader_tails_rather_than_reading_the_whole_file(self):
        path = self.home / "big.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        filler = json.dumps({"type": "user", "pad": "x" * 4000}) + "\n"
        with path.open("w", encoding="utf-8") as handle:
            for _ in range(400):  # ~1.6 MB, well past TAIL_BYTES
                handle.write(filler)
            handle.write(json.dumps(claude_record(77_000)) + "\n")
        self.assertGreater(path.stat().st_size, context.TAIL_BYTES * 4)
        self.assertEqual(context.read_claude(os.fspath(path)).used, 77_000)
        # and it reads only the tail: a record beyond it is invisible
        self.assertLessEqual(len("".join(context._tail_lines(path))), context.TAIL_BYTES + 4100)

    def test_found_by_session_id_when_no_hook_recorded_the_path(self):
        write_jsonl(self.home / ".claude" / "projects" / "-p" / "abc-123.jsonl", [claude_record(30_000)])
        reading = context.read_member("claude", {"source": "herdr:claude", "value": "abc-123"}, {}, home=self.home)
        self.assertEqual(reading.used, 30_000)
        # the pane record's path wins when it is there
        direct = write_jsonl(self.home / "elsewhere.jsonl", [claude_record(40_000)])
        reading = context.read_member("claude", {"source": "herdr:claude", "value": "abc-123"}, {"transcript_path": os.fspath(direct)}, home=self.home)
        self.assertEqual(reading.used, 40_000)


class CodexReaderTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.home = Path(self.ts.tmp) / "harness-home"

    def test_reads_the_context_not_the_cumulative_total_and_takes_the_window_from_the_file(self):
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "09" / "rollout-2026-09-08T10-00-00-01991-a.jsonl",
                    [codex_record(20_000), codex_record(136_600)])
        reading = context.read_member("codex", {"source": "herdr:codex", "value": "01991-a"}, home=self.home)
        self.assertEqual((reading.used, reading.window), (136_600, 272_000))
        self.assertEqual(round(reading.percent, 1), 50.2)
        self.assertIsNone(reading.model)

    def test_an_unknown_session_reads_nothing(self):
        self.assertIsNone(context.read_codex(None, self.home))
        self.assertIsNone(context.read_codex("nosuch", self.home))


class OpencodeReaderTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.home = Path(self.ts.tmp) / "harness-home"

    def test_newest_assistant_message_wins_and_user_rows_are_ignored(self):
        opencode_db_with(self.home, "ses_1", [
            {"role": "assistant", "modelID": "claude-sonnet-5", "tokens": {"total": 20_000}},
            {"role": "user", "tokens": {"total": 999_999}},
            {"role": "assistant", "modelID": "claude-sonnet-5", "tokens": {"total": 110_800}},
        ])
        reading = context.read_member("opencode", {"source": "herdr:opencode", "value": "ses_1"}, home=self.home)
        self.assertEqual((reading.used, reading.window), (110_800, 200_000))
        self.assertEqual(round(reading.percent, 1), 55.4)
        self.assertIsNone(context.read_opencode("ses_other", self.home))

    def test_no_database_reads_nothing(self):
        self.assertIsNone(context.read_opencode("ses_1", self.home))


class ReadMemberTests(unittest.TestCase):
    def test_a_kind_with_no_reader_is_unknown_rather_than_guessed(self):
        for kind in ("gemini", "amp", "cursor", "human", "", None):
            self.assertIsNone(context.read_member(kind, {"source": "s", "value": "v"}, {}))


# --------------------------------------------------------------------------
# the daemon: polling, the warning, the token


class PollTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.home = Path(self.ts.tmp) / "harness-home"
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.api.set_response("agent.explain", STRONG_IDLE)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def transcript(self, used, name="t.jsonl"):
        """Write a transcript and point the member's pane record at it, as the hook does."""
        path = write_jsonl(self.home / name, [claude_record(used)])
        store.write_json(self.ts.session.pane_record("term_w1"),
                         {"team": "alpha", "name": MEMBER, "gen": 1, "transcript_path": os.fspath(path)})
        return path

    def poll(self, step=20):
        self.clock.advance(step)
        self.d.poll_context(self.team, self.clock() * 1000)

    def records(self, event):
        return [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == event]

    def tokens(self):
        return [p for m, p in self.api.calls if m == "pane.report_metadata" and p.get("source") == roster.TOKEN_SOURCE_CONTEXT]

    def test_a_reading_reaches_the_runtime_who_and_the_sidebar_token(self):
        self.transcript(100_000)
        self.poll()
        rt = self.team.rt(MEMBER)
        self.assertEqual(rt.context["used"], 100_000)
        self.assertEqual(rt.context["percent"], 50.0)
        who = self.d.build_who()
        entry = [m for m in who["teams"]["alpha"]["members"] if m["name"] == MEMBER][0]
        self.assertEqual(entry["context"]["percent"], 50.0)
        stamped = self.tokens()
        self.assertEqual(stamped[-1]["tokens"], {"team_context": "50%", "team_context_warn": None, "team_context_crit": None})
        self.assertEqual(stamped[-1]["ttl_ms"], D.CONTEXT_TTL_MS)

    def test_the_gauge_moves_between_colour_slots_and_never_leaves_a_stale_cell(self):
        self.transcript(100_000)
        self.poll()
        self.transcript(160_000, "warn.jsonl")
        self.poll()
        self.assertEqual(self.tokens()[-1]["tokens"], {"team_context": None, "team_context_warn": "80%", "team_context_crit": None})
        self.transcript(190_000, "crit.jsonl")
        self.poll()
        self.assertEqual(self.tokens()[-1]["tokens"], {"team_context": None, "team_context_warn": None, "team_context_crit": "95%"})

    def test_a_crossing_is_said_once_and_a_fall_back_makes_it_news_again(self):
        self.transcript(100_000)
        self.poll()
        self.assertEqual(self.records("context_high"), [])
        self.transcript(160_000, "warn.jsonl")  # 80 %
        self.poll()
        said = self.records("context_high")
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["to"], [MEMBER, "all"])  # naming the member; a bare "all" would be held
        self.assertEqual(said[0]["severity"], "warning")
        self.assertIn("80%", said[0]["text"])
        self.transcript(162_000, "warn2.jsonl")  # still warn: not news
        self.poll()
        self.assertEqual(len(self.records("context_high")), 1)
        self.transcript(184_000, "crit.jsonl")  # 92 %: a new line crossed
        self.poll()
        self.assertEqual([r["severity"] for r in self.records("context_high")], ["warning", "critical"])
        self.transcript(100_000, "back.jsonl")  # dropped back under: forgotten
        self.poll()
        self.transcript(160_000, "warn3.jsonl")
        self.poll()
        self.assertEqual([r["severity"] for r in self.records("context_high")], ["warning", "critical", "warning"])

    def test_an_unchanged_file_is_not_re_read(self):
        path = self.transcript(100_000)
        self.poll()
        calls = len(self.tokens())
        os.utime(path, (1000, 1000))
        self.poll()
        self.poll()
        self.assertEqual(len(self.tokens()), calls, "the token is not restamped while the harness is quiet")

    def test_a_fall_nobody_asked_for_is_reported_as_a_compaction_of_its_own(self):
        self.transcript(180_000)
        self.poll()
        self.transcript(20_000, "after.jsonl")
        self.poll()
        said = self.records("context_compacted")
        self.assertEqual(len(said), 1)
        self.assertIsNone(said[0]["requested_by"])
        self.assertIn("on its own", said[0]["text"])

    def test_a_reading_from_a_different_session_is_a_new_baseline_not_a_fall(self):
        self.transcript(180_000)
        self.poll()
        member = self.team.member(MEMBER)
        self.d._apply_changes(self.team, [(MEMBER, {"session": {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "new-one"}})])
        self.transcript(20_000, "fresh.jsonl")
        self.poll()
        self.assertEqual(self.records("context_compacted"), [], "a new session is a new history, not a compaction")


# --------------------------------------------------------------------------
# the CLI


class ContextCommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_reads_the_files_itself_when_the_notifier_is_down(self):
        code, out, _err = json_out(run_cli(["--json", "--team", "alpha", "context"], self.ts.env, FakeApi()))
        self.assertEqual(code, 0)
        self.assertEqual(out["source"], "files")
        names = {row["name"] for row in out["members"]}
        self.assertIn(TARGET, names)
        self.assertNotIn("human", names)
        self.assertTrue(all(row["context"] is None for row in out["members"]), "no harness files in a temp state")

    def test_prefers_the_notifier_reading_when_it_is_running(self):
        write_live_daemon(self.ts)
        store.write_json(self.ts.session.who_json, {"teams": {"alpha": {"members": [
            {"name": TARGET, "context": {"used": 94_000, "window": 200_000, "percent": 47.0, "model": None, "source": "/t", "at": None}}]}}})
        code, out, _err = json_out(run_cli(["--json", "--team", "alpha", "context"], self.ts.env, FakeApi()))
        self.assertEqual((code, out["source"]), (0, "who.json"))
        row = [r for r in out["members"] if r["name"] == TARGET][0]
        self.assertEqual(row["context"]["percent"], 47.0)
        code, out, _err = json_out(run_cli(["--json", "--team", "alpha", "context", TARGET], self.ts.env, FakeApi()))
        self.assertEqual([r["name"] for r in out["members"]], [TARGET])
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "context", "nobody"], self.ts.env, FakeApi()))
        self.assertEqual((code, err["code"]), (1, "member_not_found"))

    def test_human_text_names_what_it_could_not_read(self):
        payload = {"team": "alpha", "source": "files", "members": [
            {"name": "one", "kind": "claude", "context": {"used": 188_000, "window": 200_000, "percent": 94.0, "model": None, "source": "/t", "at": None}},
            {"name": "two", "kind": "amp", "context": None}]}
        from herdr_team.cmd_usage import render_context

        text = render_context(payload, 100, True)
        self.assertIn("94", text)
        self.assertIn("unknown", text)


# --------------------------------------------------------------------------
# who


class WhoTagTests(unittest.TestCase):
    def test_the_tag_marks_a_full_member_and_says_nothing_about_an_empty_one(self):
        self.assertIsNone(render.context_label(None))
        self.assertIsNone(render.context_label({"percent": None}))
        self.assertEqual(render.context_label({"percent": 47.0}), "context 47%")
        self.assertEqual(render.context_label({"percent": 80.0}), "context 80% !")
        self.assertEqual(render.context_label({"percent": 94.4}), "context 94% !!")


# --------------------------------------------------------------------------
# compact and clear: the command


class ControlCommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        write_live_daemon(self.ts)

    def jobs(self):
        return [store.read_json(p) for p in sorted(self.ts.team.jobs_dir.glob("*.json"))]

    def records(self):
        return store.BoardStore(self.ts.team).read()

    def test_keystrokes_are_per_kind_and_an_unverified_kind_is_refused(self):
        self.assertEqual(cmd_board.control_keystroke("claude", "compact"), "/compact")
        self.assertEqual(cmd_board.control_keystroke("claude", "clear"), "/clear")
        # Codex deliberately gets /new: its /clear also wipes the scrollback detection reads
        self.assertEqual(cmd_board.control_keystroke("codex", "clear"), "/new")
        self.assertEqual(cmd_board.control_keystroke("opencode", "clear"), "/new")
        with self.assertRaises(HerdrTeamError) as raised:
            cmd_board.control_keystroke("gemini", "compact")
        self.assertEqual(raised.exception.code, "control_unsupported")

    def test_compact_writes_a_record_carrying_the_control_block_and_a_job_pointing_at_it(self):
        code, out, _err = json_out(run_cli(["--json", "--team", "alpha", "compact", TARGET], self.ts.env, FakeApi()))
        self.assertEqual(code, 0)
        self.assertEqual((out["action"], out["keystroke"], out["requested"]), ("compact", "/compact", True))
        record = [r for r in self.records() if r.get("seq") == out["record_seq"]][0]
        self.assertEqual(record["kind"], "direct")
        self.assertEqual(record["to"], [TARGET])
        self.assertEqual(record["control"], {"action": "compact", "keystroke": "/compact", "kind": "codex"})
        job = self.jobs()[-1]
        self.assertEqual((job["kind"], job["member"], job["action"], job["seq"]), ("control", TARGET, "compact", out["record_seq"]))
        self.assertNotIn("keystroke", job, "the job is a pointer; the daemon re-reads the record")

    def test_clear_needs_yes_even_in_json_and_then_writes_the_kind_s_clear_command(self):
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "clear", TARGET], self.ts.env, FakeApi()))
        self.assertEqual((code, err["code"]), (1, "confirmation_required"))
        self.assertEqual(self.jobs(), [])
        code, out, _err = json_out(run_cli(["--json", "--team", "alpha", "clear", TARGET, "--yes"], self.ts.env, FakeApi()))
        self.assertEqual((code, out["keystroke"]), (0, "/new"), "codex gets /new; its /clear wipes the scrollback detection reads")
        self.assertEqual(self.jobs()[-1]["action"], "clear")

    def test_an_unknown_member_is_refused_before_anything_is_written(self):
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "compact", "nobody"], self.ts.env, FakeApi()))
        self.assertEqual(code, 1)
        self.assertEqual(self.jobs(), [])


# --------------------------------------------------------------------------
# compact and clear: the daemon


class ControlJobTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.api.set_response("agent.explain", STRONG_IDLE)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def control_record(self, member=TARGET, action="compact", verified=True, to=None, control=True, kind="direct"):
        record = {
            "from": "human", "from_kind": "human", "from_terminal": "term_console",
            "origin": {"via": "console", "verified": verified}, "to": to if to is not None else [member],
            "kind": kind, "text": "{} {}".format(action, member),
        }
        if control:
            record["control"] = {"action": action, "keystroke": cmd_board.control_keystroke("codex", action), "kind": "codex"}
        return store.BoardStore(self.ts.team).append(record)

    def job(self, member=TARGET, action="compact", seq=None):
        obj = {"v": 1, "kind": "control", "member": member, "action": action, "seq": seq, "force": False,
               "requested_by": {"name": "human", "via": "console", "verified": True}, "requested_at": store.now_iso()}
        store.write_json(self.ts.team.jobs_dir / "{}-control.json".format(int(time.time() * 1000000)), obj)

    def consume(self):
        self.clock.advance(1)
        self.d.consume_jobs(self.clock() * 1000)

    def request(self, action="compact", **kwargs):
        seq = self.control_record(action=action, **kwargs)
        self.job(action=action, seq=seq)
        self.consume()
        return seq

    def settle(self, seconds=25):
        """Tick the daemon in 5 s steps: a bigger step is a sample gap and resets the stable window."""
        ticks(self.d, self.clock, max(1, int(seconds // 5)), step=5)

    def sent(self, method):
        return [p for m, p in self.api.calls if m == method]

    def records(self, event):
        return [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == event]

    def test_a_job_whose_record_does_not_back_it_is_refused_and_said_so(self):
        for kwargs, why in (
            ({"verified": False}, "unverified"),
            ({"control": False}, "no control block"),
            ({"to": ["someone-else"]}, "addressed elsewhere"),
            ({"kind": "request"}, "not a direct line"),
        ):
            before = len(self.records("typed"))
            self.request(**kwargs)
            self.assertNotIn(TARGET, self.team.pending, why)
            self.assertEqual(len(self.records("typed")), before + 1, why)
        # and a job naming no record at all
        self.job(seq=None)
        self.consume()
        self.assertNotIn(TARGET, self.team.pending)

    def test_the_keystroke_is_typed_raw_with_a_separate_enter_never_as_a_prompt(self):
        self.request()
        self.assertIn(TARGET, self.team.pending)
        self.assertEqual(self.team.pending[TARGET].kind, "control")
        self.settle()
        self.assertEqual(self.sent("pane.send_text"), [{"pane_id": "w2:p1", "text": "/compact"}])
        self.assertEqual(self.sent("pane.send_keys"), [{"pane_id": "w2:p1", "keys": ["enter"]}])
        self.assertEqual(self.sent("agent.prompt"), [], "a bracketed paste would arrive as text to answer, not a command to run")

    def test_a_working_member_is_never_typed_into_and_the_keystroke_waits_for_idle(self):
        self.api.set_response("agent.explain", {"type": "agent_explain", "explain": {"state": "working"}})
        rows = [dict(a, agent_status="working") if a.get("pane_id") == "w2:p1" else dict(a) for a in FAKE_AGENTS]
        self.api.set_response("agent.list", {"type": "agent_list", "agents": rows})
        self.request()
        self.settle()
        self.assertEqual(self.sent("pane.send_text"), [])
        self.assertEqual(self.team.pending[TARGET].hold, "not_idle")

    def test_a_blocked_member_is_never_typed_into_even_though_the_job_is_forced(self):
        self.api.set_response("agent.explain", {"type": "agent_explain", "explain": {"state": "blocked", "matched_rule": {"id": "claude.dialog"}}})
        self.request()
        self.settle()
        self.assertEqual(self.sent("pane.send_text"), [])
        self.assertIn(self.team.pending[TARGET].hold, ("blocked", "not_idle"))

    def test_it_is_typed_once_and_never_again_while_the_effect_is_awaited(self):
        self.request()
        self.settle()
        self.assertEqual(len(self.sent("pane.send_text")), 1)
        self.settle(200)  # ~3 min of Claude compacting
        self.assertEqual(len(self.sent("pane.send_text")), 1, "a second /compact would land inside the first")
        self.assertIn(TARGET, self.team.pending)

    def test_a_session_phase_change_closes_the_job_and_says_who_asked(self):
        self.request()
        self.settle()
        self.assertEqual(self.team.rt(TARGET).control_pending["action"], "compact")
        # the same session id, a new phase: Claude after a /compact
        self.d._apply_changes(self.team, [(TARGET, {
            "session": {"source": "compact", "agent": "codex", "kind": "id", "value": "s-1"}, "briefed_at": None})])
        said = self.records("context_compacted")
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["requested_by"], "human")
        self.assertEqual(said[0]["to"], [TARGET, "all"])
        self.assertIsNone(self.team.rt(TARGET).control_pending)
        self.assertNotIn(TARGET, self.team.pending)
        self.assertEqual(self.records("member_restarted"), [], "a compaction is not a restart")

    def test_a_falling_token_count_closes_a_compact_job_for_a_kind_that_says_nothing(self):
        self.request()
        self.settle()
        rt = self.team.rt(TARGET)
        rt.control_pending["used"] = 180_000
        rt.context_session = roster.session_key(self.team.member(TARGET).get("session"))
        rt.context = {"used": 180_000, "window": 200_000, "percent": 90.0}
        self.d._check_context_drop(self.team, TARGET, context.Reading(used=20_000, window=200_000, source="/t"),
                                   rt.context, self.clock() * 1000)
        self.assertEqual(len(self.records("context_compacted")), 1)
        self.assertIsNone(rt.control_pending)

    def test_a_clear_is_closed_by_the_new_session_and_reads_as_deliberate(self):
        self.request(action="clear")
        self.settle()
        self.assertEqual(self.sent("pane.send_text"), [{"pane_id": "w2:p1", "text": "/new"}])
        self.d._apply_changes(self.team, [(TARGET, {
            "generation": 2, "session": {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "s-2"}, "briefed_at": None})])
        self.assertEqual(len(self.records("member_restarted")), 1)
        cleared = self.records("context_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["requested_by"], "human")
        self.assertIsNone(self.team.rt(TARGET).control_pending)

    def test_nothing_observed_within_the_bound_closes_the_job_rather_than_retrying(self):
        self.request()
        self.settle()
        self.assertEqual(len(self.sent("pane.send_text")), 1)
        self.settle(D.CONTROL_OBSERVE_S + 60)
        self.assertNotIn(TARGET, self.team.pending)
        self.assertIsNone(self.team.rt(TARGET).control_pending)
        typed = self.records("typed")
        self.assertTrue(any("no effect was seen" in r["text"] for r in typed), [r["text"] for r in typed])
        self.assertEqual(len(self.sent("pane.send_text")), 1, "the bound closes the job; it never types again")


# --------------------------------------------------------------------------
# a restart and a phase change are different things


class SessionPhaseTests(unittest.TestCase):
    def test_identity_is_the_value_and_the_source_is_only_the_phase(self):
        a = {"source": "herdr:claude", "value": "s-1"}
        b = {"source": "compact", "value": "s-1"}
        self.assertFalse(roster.same_session(a, b), "the record is no longer exactly current")
        self.assertTrue(roster.same_session_value(a, b), "but it is the same session")
        self.assertFalse(roster.same_session_value(a, {"source": "herdr:claude", "value": "s-2"}))
        self.assertFalse(roster.same_session_value(a, None))
        self.assertEqual(roster.session_source(b), "compact")
        self.assertIsNone(roster.session_source(None))


# --------------------------------------------------------------------------
# the console


class ConsoleTests(unittest.TestCase):
    def parse(self, line):
        return tui_model.parse_input_line(line, "alpha")

    def test_the_three_commands_parse_and_clear_asks_first(self):
        self.assertEqual(self.parse("/context").kind, "context")
        self.assertIsNone(self.parse("/context").args["member"])
        self.assertEqual(self.parse("/context one").args["member"], "one")
        self.assertEqual(self.parse("/compact one").kind, "compact")
        self.assertEqual(self.parse("/clear one").kind, "clear")
        self.assertEqual(self.parse("/compact").kind, "error")
        model = tui_model.ConsoleModel(team="alpha", header=["alpha"], width=100, height=24)
        intent = tui_model._after_parse(model, self.parse("/clear one"))
        self.assertEqual(intent.kind, "none")
        self.assertIsNotNone(model.pending_confirm)
        self.assertIn("clear one's context", model.status)
        # compacting summarises rather than destroying, so it goes straight through
        self.assertEqual(tui_model._after_parse(model, self.parse("/compact one")).kind, "compact")

    def test_every_command_is_in_the_menu_and_the_help(self):
        for command in ("/context", "/compact", "/clear"):
            self.assertIn(command, tui_model.SLASH_COMMANDS)
            self.assertIn(command, tui_model.SLASH_USAGE)
            self.assertTrue(any(command in line for line in tui_model.help_lines()), command)


if __name__ == "__main__":
    unittest.main()

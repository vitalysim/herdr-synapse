"""Transcript search: what members said and did in their own harness conversations.

Every fixture is synthetic and lives under the ``TempState`` home: Claude
transcripts, Codex rollouts, an OpenCode database built here with the columns
``context`` reads plus the ``part`` table text lives in, and a Pi session file.
Nothing reads the real HOME, a real Herdr socket, or a daemon.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from herdr_team import cmd_hooks, context, hooks, identity, operator, roster, store, transcripts, tui_model
from support import FakeApi, TempState, fake_agent
from test_cmd_roster import json_out, live_api, run_cli
from test_daemon import make_daemon

CLAUDE_ID = "11111111-aaaa-4bbb-8ccc-000000000001"
CLAUDE_OLD_ID = "11111111-aaaa-4bbb-8ccc-000000000009"
LEAD_ID = "22222222-aaaa-4bbb-8ccc-000000000002"
CODEX_ID = "01a0b063-aca2-7ac0-9559-d64f736d3e5c"
CODEX_OLD_ID = "01a0b063-aca2-7ac0-9559-000000000000"
OPENCODE_ID = "ses_7f3a9c"
ANTHROPIC_KEY = "sk-ant-api03-" + "A" * 30
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


def iso(epoch: float) -> str:
    return transcripts.iso_of(epoch)


def session(kind: str, value: str, ref: str = "id"):
    return {"source": "herdr:" + kind, "agent": kind, "kind": ref, "value": value, "seen_at": "2026-09-20T10:00:00.000Z"}


def member(name, kind, terminal, pane, sess=None, **extra):
    doc = {
        "name": name, "role": name.split("-", 1)[1], "kind": kind, "terminal_id": terminal, "pane_id": pane,
        "workspace_id": "w2", "tab_id": "w2:t1", "label": "team:alpha/" + name.split("-", 1)[1], "cwd": "/tmp/work",
        "managed": False, "session": sess, "status": "active", "generation": 1, "delivery": "nudge",
        "joined_at": "2026-09-20T10:00:00Z", "briefed_at": None, "briefing_seq": None, "charter_seq_acked": None, "brief": None,
    }
    doc.update(extra)
    return doc


def write_jsonl(path: Path, records, garbage: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    if garbage:
        lines.insert(1, "{not json at all")
        lines.insert(1, "plain text line")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def claude_text(role, text, at):
    return {"type": role, "timestamp": iso(at), "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def codex_item(payload, at):
    return {"timestamp": iso(at), "type": "response_item", "payload": payload}


class Fixture:
    """A five-agent team with one conversation per harness, all under the temp home."""

    def __init__(self, now: float):
        self.now = now
        pi_path = "/placeholder"  # replaced once the temp dir exists
        members = [
            member("alpha-lead", "claude", "term_l1", "w2:p3", session("claude", LEAD_ID), manager=True),
            member("alpha-analyst", "claude", "term_a1", "w2:p4", session("claude", CLAUDE_ID),
                   session_history=[dict(session("claude", CLAUDE_OLD_ID), replaced_at="2026-09-20T11:00:00.000Z")]),
            member("alpha-reviewer", "codex", "term_r1", "w2:p1", session("codex", CODEX_ID),
                   agent_history=[{"name": "alpha-reviewer", "kind": "codex", "session": session("codex", CODEX_OLD_ID), "replaced_at": "2026-09-20T12:00:00.000Z"}]),
            member("alpha-oc", "opencode", "term_o1", "w2:p5", session("opencode", OPENCODE_ID)),
            member("alpha-pi", "pi", "term_p1", "w2:p6", session("pi", pi_path, "path")),
            {"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"},
        ]
        self.ts = TempState(members=members)
        self.home = self.ts.home
        self.pi_file = self.ts.tmp / "pi-sessions" / "2026-09-22T10-00-00-000Z_pi.jsonl"
        self.ts.members[4]["session"] = session("pi", os.fspath(self.pi_file), "path")
        self.ts.write_team_json()
        self.agents = [
            fake_agent("w2:p3", "term_l1", "claude", "alpha-lead"),
            fake_agent("w2:p4", "term_a1", "claude", "alpha-analyst"),
            fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer"),
            fake_agent("w2:p5", "term_o1", "opencode", "alpha-oc"),
            fake_agent("w2:p6", "term_p1", "pi", "alpha-pi"),
        ]
        self.write_all()

    # -- stores

    def claude_path(self, value: str, root: Path = None) -> Path:
        return (root or self.home / ".claude") / "projects" / "-tmp-work" / "{}.jsonl".format(value)

    def write_all(self):
        now = self.now
        write_jsonl(self.claude_path(CLAUDE_ID), [
            {"type": "summary", "summary": "rate limit notes"},  # not a message: never a hit
            claude_text("user", "Find the upstream rate limit for the billing API.", now - 3 * 3600),
            {"type": "assistant", "timestamp": iso(now - 3 * 3600 + 60), "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "rate limit thoughts are not what was said"},
                {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://example.test/docs", "prompt": "find the rate limit"}},
            ]}},
            {"type": "user", "timestamp": iso(now - 3 * 3600 + 90), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "Docs: the limit is 600 requests per minute per key."}]},
            ]}},
            claude_text("assistant", "The upstream rate limit is 600 requests\nper minute; the token bucket refills every second.", now - 2 * 3600),
            claude_text("assistant", "Only the rate was mentioned here.", now - 3600),
            claude_text("assistant", "Credentials seen in the log: {} and {} (rate limit key).".format(ANTHROPIC_KEY, AWS_KEY), now - 1800),
        ], garbage=True)
        write_jsonl(self.claude_path(CLAUDE_OLD_ID), [
            claude_text("assistant", "Yesterday the rate limit looked like 500 requests.", now - 30 * 3600),
        ])
        write_jsonl(self.claude_path(LEAD_ID), [claude_text("assistant", "Plan: ask the analyst about the rate limit.", now - 600)])
        # an unrelated conversation of the user's, in the same project directory: never read
        write_jsonl(self.claude_path("99999999-aaaa-4bbb-8ccc-00000000dead"), [
            claude_text("assistant", "private rate limit musings", now - 60),
        ])
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "09" / "22" / "rollout-2026-09-22T10-00-00-{}.jsonl".format(CODEX_ID), [
            {"timestamp": iso(now - 5000), "type": "session_meta", "payload": {"id": CODEX_ID}},
            codex_item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>rate limit cwd</environment_context>"}]}, now - 4900),
            codex_item({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rate limit instructions"}]}, now - 4800),
            codex_item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Review the retry code for the rate limit."}]}, now - 4700),
            codex_item({"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["rg", "rate limit", "src/"]}), "call_id": "c1"}, now - 4600),
            codex_item({"type": "function_call_output", "call_id": "c1", "output": json.dumps({"output": "src/retry.py:12: # rate limit backoff", "metadata": {"exit_code": 0}})}, now - 4500),
            codex_item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "The retry honours the rate limit with exponential backoff."}]}, now - 4400),
            {"timestamp": iso(now - 4300), "type": "event_msg", "payload": {"type": "agent_message", "message": "The retry honours the rate limit (echo)."}},
        ])
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "09" / "20" / "rollout-2026-09-20T10-00-00-{}.jsonl".format(CODEX_OLD_ID), [
            codex_item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Earlier codex: rate limit unknown."}]}, now - 50 * 3600),
        ])
        # the Pi file
        write_jsonl(self.pi_file, [
            {"type": "session", "id": "pi", "timestamp": iso(now - 7000), "cwd": "/tmp/work"},
            {"type": "message", "id": "m1", "timestamp": iso(now - 6900), "message": {"role": "user", "content": [{"type": "text", "text": "Summarise the rate limit findings."}], "timestamp": int((now - 6900) * 1000)}},
            {"type": "message", "id": "m2", "timestamp": iso(now - 6800), "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "rate limit"}, {"type": "toolCall", "id": "x", "name": "read", "arguments": {"path": "notes/rate-limit.md"}}]}},
            {"type": "message", "id": "m3", "timestamp": iso(now - 6700), "message": {"role": "toolResult", "toolCallId": "x", "toolName": "read", "content": [{"type": "text", "text": "rate limit: 600/min"}], "isError": False}},
            {"type": "message", "id": "m4", "timestamp": iso(now - 6600), "message": {"role": "assistant", "content": [{"type": "text", "text": "Pi says the rate limit is 600 per minute."}]}},
        ])
        self.write_opencode(self.home / ".local" / "share" / "opencode" / "opencode.db")

    def write_opencode(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(os.fspath(path))
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL)")
        ms = int(self.now * 1000)
        rows = [
            ("msg_u", OPENCODE_ID, {"role": "user"}, [("prt_1", {"type": "text", "text": "What is the rate limit on search?"}, ms - 9_000_000)]),
            ("msg_a", OPENCODE_ID, {"role": "assistant", "tokens": {"total": 10}}, [
                ("prt_2", {"type": "reasoning", "text": "rate limit reasoning"}, ms - 8_900_000),
                ("prt_3", {"type": "tool", "tool": "grep", "callID": "c", "state": {"status": "completed", "input": {"pattern": "rate limit"}, "output": "api.go:40 rateLimit := 100"}}, ms - 8_800_000),
                ("prt_4", {"type": "text", "text": "OpenCode found the rate limit at 100 per second."}, ms - 8_700_000),
            ]),
            ("msg_other", "ses_someone_else", {"role": "assistant"}, [("prt_9", {"type": "text", "text": "another session's rate limit"}, ms - 1000)]),
        ]
        for message_id, sess_id, data, parts in rows:
            conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", (message_id, sess_id, parts[0][2], parts[0][2], json.dumps(data)))
            for part_id, part, created in parts:
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (part_id, message_id, sess_id, created, created, json.dumps(part)))
        conn.commit()
        conn.close()
        return path

    # -- running the CLI

    def api(self):
        return live_api(self.agents)

    def env_for(self, pane=None, **extra):
        env = self.ts.env_with(HERDR_PANE_ID=pane, **extra) if pane else self.ts.env_with(**extra)
        env["HERDR_TEAM_NO_DAEMON"] = "1"
        return env

    def search(self, *argv, pane=None, env=None):
        return json_out(run_cli(["--json", "--team", "alpha", "search"] + list(argv), env or self.env_for(pane), self.api()))

    def text(self, *argv, pane=None):
        return run_cli(["--team", "alpha", "search"] + list(argv), self.env_for(pane), self.api())

    def audit(self, event):
        return [e for e in identity.read_audit(self.ts.layout, "alpha") if e["event"] == event]


class SearchTestCase(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.fx = Fixture(self.now)
        self.addCleanup(self.fx.ts.cleanup)


# --------------------------------------------------------------------------
# what is found


class HarnessReaderTests(SearchTestCase):
    def test_claude_hits_newest_first_with_the_match_located(self):
        code, out, err = self.fx.search("rate", "limit", "--member", "alpha-analyst")
        self.assertEqual(code, 0, err)
        self.assertEqual(set(out), {"team", "query", "terms", "members", "since", "role", "history", "total", "hits", "scanned"})
        stamps = [h["ts"] for h in out["hits"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        texts = [h["excerpt"] for h in out["hits"]]
        # the summary line, the thinking block, the other conversation and the unrelated file are not hits
        self.assertFalse(any("thoughts" in t or "Yesterday" in t or "musings" in t or "notes" == t for t in texts), texts)
        self.assertEqual(out["total"], 4)  # user ask, tool call, answer, and the redacted credentials line
        hit = next(h for h in out["hits"] if "token bucket" in h["excerpt"])
        self.assertEqual((hit["member"], hit["kind"], hit["session"], hit["role"], hit["history"]), ("alpha-analyst", "claude", CLAUDE_ID, "assistant", False))
        self.assertEqual(hit["source_path"], os.fspath(self.fx.claude_path(CLAUDE_ID)))
        start, end = hit["match"]
        self.assertEqual(hit["excerpt"][start:end].lower(), "rate")
        self.assertNotIn("\n", hit["excerpt"])  # flattened to one line
        roles = {h["role"] for h in out["hits"]}
        self.assertEqual(roles, {"user", "assistant", "tool"})
        self.assertEqual(out["scanned"]["alpha-analyst"]["sessions"], 1)
        self.assertGreater(out["scanned"]["alpha-analyst"]["bytes"], 0)

    def test_text_mode_marks_the_match(self):
        code, out, _ = self.fx.text("bucket", "--member", "alpha-analyst")
        self.assertEqual(code, 0)
        self.assertIn("»bucket«", out)
        self.assertIn("alpha-analyst  claude", out)
        self.assertIn("session " + CLAUDE_ID, out)
        self.assertIn("scanned: alpha-analyst 1 conversation", out)

    def test_codex_reads_the_history_items_once_and_skips_injected_context(self):
        code, out, err = self.fx.search("rate", "limit", "--member", "alpha-reviewer")
        self.assertEqual(code, 0, err)
        by_role = {}
        for hit in out["hits"]:
            by_role.setdefault(hit["role"], []).append(hit["excerpt"])
        self.assertEqual(len(by_role["user"]), 1, by_role)  # not the environment context, not the developer message
        self.assertIn("Review the retry code", by_role["user"][0])
        self.assertEqual(len(by_role["assistant"]), 1, by_role)  # the event_msg echo is not a second copy
        self.assertEqual(len(by_role["tool"]), 2)
        self.assertTrue(any("rg" in t and "src/" in t for t in by_role["tool"]), by_role["tool"])
        self.assertIn("src/retry.py:12: # rate limit backoff", by_role["tool"])  # the output, not its JSON wrapper or metadata
        self.assertTrue(all(h["kind"] == "codex" and h["session"] == CODEX_ID for h in out["hits"]))

    def test_opencode_reads_parts_of_this_session_read_only(self):
        db = context.opencode_db(self.fx.home)
        before = db.stat().st_mtime_ns
        code, out, err = self.fx.search("rate", "limit", "--member", "alpha-oc")
        self.assertEqual(code, 0, err)
        roles = sorted(h["role"] for h in out["hits"])
        self.assertEqual(roles, ["assistant", "tool", "user"])  # reasoning is skipped, the other session never read
        self.assertFalse(any("another session" in h["excerpt"] for h in out["hits"]))
        self.assertTrue(all(h["source_path"] == os.fspath(db) for h in out["hits"]))
        self.assertEqual(out["hits"][0]["ts"], iso((int(self.now * 1000) - 8_700_000) / 1000.0))
        self.assertEqual(db.stat().st_mtime_ns, before)

    def test_pi_session_file(self):
        code, out, err = self.fx.search("rate", "limit", "--member", "alpha-pi")
        self.assertEqual(code, 0, err)
        self.assertEqual([h["role"] for h in out["hits"]], ["assistant", "tool", "tool", "user"])
        self.assertTrue(all(h["source_path"] == os.fspath(self.fx.pi_file) for h in out["hits"]))

    def test_every_member_by_default_for_the_operator(self):
        code, out, err = self.fx.search("rate", "limit")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["members"], ["alpha-lead", "alpha-analyst", "alpha-reviewer", "alpha-oc", "alpha-pi"])
        self.assertEqual({h["member"] for h in out["hits"]}, set(out["members"]))
        self.assertEqual((len(out["hits"]), out["total"]), (16, 16))
        code, out, _ = self.fx.search("rate", "limit", "--limit", "3")
        self.assertEqual((len(out["hits"]), out["total"]), (3, 16))
        self.assertEqual(out["hits"][0]["member"], "alpha-lead")  # the newest message anywhere
        code, text, _ = self.fx.text("rate", "limit", "--limit", "3")
        self.assertIn("16 matches for rate limit in team alpha, newest first (showing the newest 3)", text)
        code, _, err = self.fx.search("rate", "--limit", "0")
        self.assertEqual((code, err["code"]), (2, "usage"))

    def test_env_overrides_move_the_stores(self):
        moved = self.fx.ts.tmp / "moved"
        write_jsonl(moved / "claude" / "projects" / "x" / "{}.jsonl".format(CLAUDE_ID), [claude_text("assistant", "relocated claude answer", self.now)])
        write_jsonl(moved / "codex" / "sessions" / "rollout-2026-09-22T10-00-00-{}.jsonl".format(CODEX_ID), [
            codex_item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "relocated codex answer"}]}, self.now)])
        self.fx.write_opencode(moved / "data" / "opencode" / "opencode.db")
        env = self.fx.env_for(CLAUDE_CONFIG_DIR=os.fspath(moved / "claude"), CODEX_HOME=os.fspath(moved / "codex"),
                              XDG_DATA_HOME=os.fspath(moved / "data"))
        code, out, err = self.fx.search("relocated", env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(h["member"] for h in out["hits"]), ["alpha-analyst", "alpha-reviewer"])
        code, out, err = self.fx.search("OpenCode found", env=env)
        self.assertEqual(out["hits"][0]["source_path"], os.fspath(moved / "data" / "opencode" / "opencode.db"))

    def test_the_pane_record_transcript_path_is_used_for_that_session_only(self):
        elsewhere = write_jsonl(self.fx.ts.tmp / "custom" / "{}.jsonl".format(CLAUDE_ID), [claude_text("assistant", "hook recorded path", self.now)])
        roster.write_pane_record(self.fx.ts.session, "term_a1", "alpha", "alpha-analyst", 1, session("claude", CLAUDE_ID))
        record = store.read_json(self.fx.ts.session.pane_record("term_a1"))
        record["transcript_path"] = os.fspath(elsewhere)
        store.write_json(self.fx.ts.session.pane_record("term_a1"), record)
        code, out, _ = self.fx.search("hook recorded", "--member", "alpha-analyst")
        self.assertEqual([h["source_path"] for h in out["hits"]], [os.fspath(elsewhere)])
        # a recorded path for some other session is not trusted
        record["transcript_path"] = os.fspath(self.fx.claude_path("99999999-aaaa-4bbb-8ccc-00000000dead"))
        store.write_json(self.fx.ts.session.pane_record("term_a1"), record)
        code, out, _ = self.fx.search("musings", "--member", "alpha-analyst")
        self.assertEqual(out["hits"], [])


class QueryTests(SearchTestCase):
    def test_all_terms_must_be_in_one_message(self):
        code, out, _ = self.fx.search("bucket", "billing", "--member", "alpha-analyst")
        self.assertEqual(out["hits"], [])
        code, out, _ = self.fx.search("BUCKET", "Refills", "--member", "alpha-analyst")
        self.assertEqual(len(out["hits"]), 1)

    def test_a_quoted_phrase_is_exact_and_spans_line_breaks(self):
        code, out, _ = self.fx.search('"requests per minute"', "--member", "alpha-analyst")
        self.assertEqual(len(out["hits"]), 2)  # the answer (across its line break) and the tool result
        code, out, _ = self.fx.search('"minute per requests"', "--member", "alpha-analyst")
        self.assertEqual(out["hits"], [])
        code, out, _ = self.fx.search("minute per requests", "--member", "alpha-analyst")
        self.assertEqual(len(out["hits"]), 2)  # the same words, unquoted, in any order

    def test_query_parsing(self):
        query = transcripts.Query.parse('rate  "token   bucket" x')
        self.assertEqual(query.terms, ["rate", "token bucket", "x"])
        self.assertEqual(transcripts.Query.parse("a.b*c").span("xx A.B*C yy"), (3, 8))  # literal, never a pattern
        self.assertIsNone(transcripts.Query.parse("a.b*c").span("aXbbbc"))
        for bad in ('"open', "   ", '""'):
            with self.assertRaises(ValueError):
                transcripts.Query.parse(bad)
        code, _, err = self.fx.search('"open')
        self.assertEqual((code, err["code"]), (2, "usage"))

    def test_role_filter(self):
        code, out, _ = self.fx.search("rate", "limit", "--member", "alpha-analyst", "--role", "user")
        self.assertEqual([h["role"] for h in out["hits"]], ["user"])
        code, out, _ = self.fx.search("rate", "limit", "--member", "alpha-analyst", "--role", "tool")
        self.assertEqual([h["role"] for h in out["hits"]], ["tool"])

    def test_since_relative_and_absolute(self):
        code, out, _ = self.fx.search("rate", "--member", "alpha-analyst", "--since", "90m")
        self.assertEqual({h["ts"] for h in out["hits"]}, {iso(self.now - 3600), iso(self.now - 1800)})
        code, out, _ = self.fx.search("rate", "--member", "alpha-analyst", "--since", iso(self.now - 2 * 3600 - 1))
        self.assertEqual(len(out["hits"]), 3)
        code, out, _ = self.fx.search("rate", "--member", "alpha-oc", "--since", "1h")
        self.assertEqual(out["hits"], [])
        code, _, err = self.fx.search("rate", "--since", "yesterday")
        self.assertEqual((code, err["code"]), (2, "usage"))
        self.assertEqual(transcripts.parse_since("3d", 1_000_000.0), 1_000_000.0 - 3 * 86400)
        self.assertEqual(transcripts.parse_since("2026-09-22T14:00Z", 0.0), datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(transcripts.parse_since("2026-09-22", 0.0), datetime(2026, 9, 22).astimezone().timestamp())  # local midnight

    def test_history_adds_earlier_conversations(self):
        code, out, _ = self.fx.search("rate", "limit", "--member", "alpha-analyst", "--member", "alpha-reviewer")
        self.assertFalse(any(h["history"] for h in out["hits"]))
        self.assertEqual(out["scanned"]["alpha-analyst"]["sessions"], 1)
        code, out, _ = self.fx.search("rate", "limit", "--member", "alpha-analyst", "--member", "alpha-reviewer", "--history", "--limit", "50")
        earlier = {(h["member"], h["session"]) for h in out["hits"] if h["history"]}
        self.assertEqual(earlier, {("alpha-analyst", CLAUDE_OLD_ID), ("alpha-reviewer", CODEX_OLD_ID)})
        self.assertEqual(out["scanned"]["alpha-analyst"]["sessions"], 2)
        self.assertEqual(out["scanned"]["alpha-reviewer"]["sessions"], 2)
        code, text, _ = self.fx.text("Yesterday", "--history")
        self.assertIn("(earlier conversation)", text)


class RedactionTests(SearchTestCase):
    def test_excerpts_are_redacted_and_secrets_are_not_searchable(self):
        code, out, _ = self.fx.search("credentials", "--member", "alpha-analyst", "--context", "400")
        excerpt = out["hits"][0]["excerpt"]
        self.assertIn("[redacted:anthropic_key]", excerpt)
        self.assertIn("[redacted:aws_access_key]", excerpt)
        self.assertNotIn(ANTHROPIC_KEY[:16], excerpt)
        self.assertNotIn(AWS_KEY, excerpt)
        code, out, _ = self.fx.search(AWS_KEY)
        self.assertEqual(out["hits"], [])  # the only match was inside a secret
        self.assertEqual(out["total"], 0)

    def test_a_private_key_is_redacted_to_its_footer(self):
        text = "key follows\n-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretbody\n-----END RSA PRIVATE KEY-----\nafter"
        self.assertEqual(transcripts.redact(text), "key follows\n[redacted:private_key]\nafter")
        self.assertNotIn("MIIE", transcripts.redact("-----BEGIN PRIVATE KEY-----\nMIIEtruncated"))

    def test_timestamps_in_every_shape_the_harnesses_write(self):
        expected = datetime(2026, 9, 22, 10, 0, 0, 123456, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(transcripts.epoch_of("2026-09-22T10:00:00.123456789Z"), expected, places=5)
        self.assertAlmostEqual(transcripts.epoch_of("2026-09-22T10:00:00.123456+00:00"), expected, places=5)
        self.assertEqual(transcripts.epoch_of(1789999200123), 1789999200.123)  # epoch milliseconds
        self.assertIsNone(transcripts.epoch_of("yesterday"))
        self.assertIsNone(transcripts.epoch_of(True))

    def test_control_sequences_do_not_reach_the_excerpt(self):
        raw = "a \x1b[31mred\x1b[0m rate\u202e limit"
        shown, start, end = transcripts.excerpt(raw, (raw.index("rate"), raw.index("rate") + 4), 80)
        self.assertEqual(shown, "a red rate limit")
        self.assertEqual(shown[start:end], "rate")


class RobustnessTests(SearchTestCase):
    def test_the_byte_cap_keeps_the_newest_part_and_says_so(self):
        path = write_jsonl(self.fx.ts.tmp / "big.jsonl", [claude_text("assistant", "old needle {}".format(i) + " pad" * 50, self.now - 1000 + i) for i in range(40)])
        size = path.stat().st_size
        search = transcripts.Search(transcripts.Query.parse("needle"), max_bytes=size // 4, limit=100)
        ref = transcripts.SessionRef("claude", session("claude", "big"), False, transcript_hint=os.fspath(path))
        scanned = transcripts.run(search, [("m", [ref], None)])
        scan = scanned["m"]
        self.assertLessEqual(scan.bytes, size // 4 + 1)
        self.assertEqual(scan.sessions, 1)
        self.assertTrue(any("only the newest" in reason for reason in scan.skipped), scan.skipped)
        numbers = [int(h["excerpt"].split("needle ")[1].split()[0]) for h in search.hits()]
        self.assertTrue(numbers and min(numbers) > 20 and max(numbers) == 39, numbers)

    def test_missing_and_unknown_stores_are_reasons_not_errors(self):
        self.fx.ts.members[0]["session"] = None  # alpha-lead: nothing recorded
        self.fx.ts.members[2]["session"] = session("codex", "0000-missing")
        self.fx.ts.members.append(member("alpha-gem", "gemini", "term_g1", "w2:p7", session("gemini", "g-1")))
        self.fx.ts.write_team_json()
        context.opencode_db(self.fx.home).unlink()
        self.fx.pi_file.unlink()
        code, out, err = self.fx.search("rate")
        self.assertEqual(code, 0, err)
        skipped = {name: " ".join(scan["skipped"]) for name, scan in out["scanned"].items()}
        self.assertIn("integration install claude", skipped["alpha-lead"])
        self.assertIn("rollout log not found", skipped["alpha-reviewer"])
        self.assertIn("opencode.db not found", skipped["alpha-oc"])
        self.assertIn("session file not found", skipped["alpha-pi"])
        self.assertIn("no transcript reader", skipped["alpha-gem"])
        self.assertEqual(out["scanned"]["alpha-lead"]["sessions"], 0)
        self.assertEqual({h["member"] for h in out["hits"]}, {"alpha-analyst"})

    def test_an_id_that_is_not_plain_never_becomes_a_glob(self):
        self.fx.ts.members[2]["session"] = session("codex", "*")
        self.fx.ts.members[1]["session"] = session("claude", "*")
        self.fx.ts.write_team_json()
        code, out, _ = self.fx.search("rate", "--member", "alpha-reviewer", "--member", "alpha-analyst")
        self.assertEqual(out["hits"], [])
        self.assertIn("not a plain id", " ".join(out["scanned"]["alpha-reviewer"]["skipped"]))
        self.assertIn("not a plain id", " ".join(out["scanned"]["alpha-analyst"]["skipped"]))

    def test_a_locked_or_foreign_database_is_reported(self):
        db = context.opencode_db(self.fx.home)
        db.unlink()
        conn = sqlite3.connect(os.fspath(db))
        conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        conn.execute("INSERT INTO message VALUES ('m', ?, 1, '{\"role\":\"user\"}')", (OPENCODE_ID,))
        conn.commit()
        conn.close()
        code, out, err = self.fx.search("rate", "--member", "alpha-oc")
        self.assertEqual(code, 0, err)
        self.assertIn("unreadable", " ".join(out["scanned"]["alpha-oc"]["skipped"]))

    def test_context_readers_keep_resolving_from_home(self):
        """The shared locators only follow the relocation variables when asked to."""
        moved = self.fx.ts.tmp / "elsewhere"
        env = {"CODEX_HOME": os.fspath(moved), "CLAUDE_CONFIG_DIR": os.fspath(moved), "XDG_DATA_HOME": os.fspath(moved)}
        self.assertIsNotNone(context.codex_rollout(CODEX_ID, self.fx.home))
        self.assertIsNone(context.codex_rollout(CODEX_ID, self.fx.home, env))
        self.assertEqual(context.opencode_db(self.fx.home), self.fx.home / ".local" / "share" / "opencode" / "opencode.db")
        self.assertEqual(context.opencode_db(self.fx.home, env), moved / "opencode" / "opencode.db")
        self.assertEqual(context.claude_projects_root(self.fx.home, env), moved / "projects")
        self.assertIsNotNone(context.claude_transcript_by_id(CLAUDE_ID, self.fx.home))


# --------------------------------------------------------------------------
# who may search whom


class AuthorityTests(SearchTestCase):
    def test_the_manager_may_search_any_member(self):
        code, out, err = self.fx.search("bucket", "--member", "alpha-analyst", pane="w2:p3")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out["hits"]), 1)
        entry = self.fx.audit("transcript_search")[-1]
        self.assertEqual((entry["author"], entry["details"]["authority"], entry["details"]["members"]), ("alpha-lead", "manager", ["alpha-analyst"]))

    def test_a_peer_is_refused_and_audited(self):
        code, out, err = self.fx.search("rate", "--member", "alpha-reviewer", pane="w2:p4")
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertIn("alpha-lead", err["message"])
        refused = self.fx.audit("author_mismatch")[-1]
        self.assertEqual((refused["author"], refused["details"]["action"], refused["details"]["members"]), ("alpha-analyst", "search", ["alpha-reviewer"]))
        self.assertEqual(self.fx.audit("transcript_search"), [])

    def test_a_plain_member_searches_itself(self):
        code, out, err = self.fx.search("rate", "limit", pane="w2:p4")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["members"], ["alpha-analyst"])
        self.assertEqual(list(out["scanned"]), ["alpha-analyst"])
        code, out, err = self.fx.search("rate", "--member", "alpha-analyst", pane="w2:p4")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fx.audit("transcript_search")[-1]["details"]["authority"], "self")

    def test_the_operator_and_a_delegate_may_search_anyone(self):
        code, out, err = self.fx.search("rate", "--member", "alpha-reviewer")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fx.audit("transcript_search")[-1]["details"]["authority"], "operator")
        operator.grant(self.fx.ts.session, "alpha", "alpha-analyst", ttl_s=0)
        code, out, err = self.fx.search("rate", "--member", "alpha-reviewer", pane="w2:p4")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fx.audit("transcript_search")[-1]["details"]["authority"], "delegate")

    def test_the_audit_carries_the_size_of_the_query_never_its_text(self):
        self.fx.search("bucket", "refills")
        raw = self.fx.ts.team.audit_jsonl.read_text(encoding="utf-8")
        self.assertNotIn("bucket", raw)
        entry = self.fx.audit("transcript_search")[-1]
        self.assertEqual((entry["details"]["query_chars"], entry["details"]["terms"], entry["details"]["hits"]), (len("bucket refills"), 2, 1))

    def test_unknown_or_human_members_are_named(self):
        code, _, err = self.fx.search("rate", "--member", "human")
        self.assertEqual((code, err["code"]), (1, "member_not_found"))
        code, _, err = self.fx.search("rate", "--member", "nobody")
        self.assertEqual((code, err["code"], err["names"]), (1, "member_not_found", ["nobody"]))


# --------------------------------------------------------------------------
# earlier conversations are kept when a new one replaces them


class SessionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def member_doc(self, name):
        return next(m for m in store.read_json(self.ts.team.team_json)["members"] if m["name"] == name)

    def test_remember_session_keeps_values_not_phases(self):
        first, second = session("claude", "one"), session("claude", "two")
        self.assertIsNone(roster.remember_session([], None, first))
        self.assertIsNone(roster.remember_session([], first, dict(first, source="compact")))
        history = roster.remember_session([], first, second)
        self.assertEqual([h["value"] for h in history], ["one"])
        self.assertIn("replaced_at", history[0])
        again = roster.remember_session(history + [dict(second)], first, second)
        self.assertEqual([h["value"] for h in again], ["two", "one"])  # one entry per value, newest last
        many = []
        for index in range(roster.SESSION_HISTORY_MAX + 5):
            many = roster.remember_session(many, session("claude", "s{}".format(index)), second)
        self.assertEqual(len(many), roster.SESSION_HISTORY_MAX)
        self.assertEqual(many[-1]["value"], "s{}".format(roster.SESSION_HISTORY_MAX + 4))
        member_obj = roster.Member.from_json(dict(self.member_doc("alpha-worker"), session_history=history))
        self.assertEqual(member_obj.to_json()["session_history"][0]["value"], "one")
        self.assertNotIn("session_history", roster.Member.from_json(self.member_doc("alpha-worker")).to_json())

    def test_the_claude_hook_keeps_the_cleared_conversation(self):
        cmd_hooks.record_member_session(self.ts.team, "alpha-worker", cmd_hooks._claude_session({"session_id": "one"}), "startup")
        self.assertNotIn("session_history", self.member_doc("alpha-worker"))
        cmd_hooks.record_member_session(self.ts.team, "alpha-worker", cmd_hooks._claude_session({"session_id": "two"}), "clear")
        self.assertEqual([h["value"] for h in self.member_doc("alpha-worker")["session_history"]], ["one"])

    def test_the_daemon_keeps_the_restarted_conversation(self):
        self.ts.members[0].update({"session": session("codex", "first")})
        self.ts.write_team_json()
        d, api, _clock = make_daemon(self.ts)
        api.set_response("agent.rename", {"type": "ok"})
        api.set_response("pane.rename", {"type": "ok"})
        api.set_response("pane.list", {"type": "pane_list", "panes": []})

        def rows(value):
            return [fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", agent_session=session("codex", value)),
                    fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working")]

        api.set_response("agent.list", {"type": "agent_list", "agents": rows("first")})
        d.on_connected()
        api.set_response("agent.list", {"type": "agent_list", "agents": rows("second")})
        d.poll_agents(force=True)
        d.reconcile()
        doc = self.member_doc("alpha-reviewer")
        self.assertEqual(doc["session"]["value"], "second")
        self.assertEqual([h["value"] for h in doc["session_history"]], ["first"])

    def test_the_hook_slow_path_keeps_the_restarted_conversation(self):
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0].update({"session": session("codex", "first")})
        store.write_json(self.ts.team.team_json, doc)
        api = FakeApi(self.ts.socket_path)
        api.set_response("agent.rename", {"type": "ok"})
        api.set_response("pane.rename", {"type": "ok"})
        api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", agent_session=session("codex", "second"))})
        env = self.ts.env_with(
            HERDR_PLUGIN_EVENT="pane.agent_detected",
            HERDR_PLUGIN_EVENT_JSON=json.dumps({"type": "pane_agent_detected", "data": {"pane_id": "w2:p1"}}),
            HERDR_PLUGIN_ID="herdr-synapse", HERDR_PANE_ID="wA:p6", HERDR_WORKSPACE_ID="wA", HERDR_TAB_ID="wA:t1",
        )
        hooks.run_hook_event(["agent_detected"], env, api=api, stdout=io.StringIO())
        doc = self.member_doc("alpha-reviewer")
        self.assertEqual(doc["session"]["value"], "second")
        self.assertEqual([h["value"] for h in doc["session_history"]], ["first"])


# --------------------------------------------------------------------------
# the console


class ConsoleSearchTests(unittest.TestCase):
    def test_slash_search_carries_the_whole_query(self):
        intent = tui_model.parse_input_line('/search rate "token bucket"', "alpha")
        self.assertEqual((intent.kind, intent.args["query"], intent.args["team"]), ("search", 'rate "token bucket"', "alpha"))
        self.assertEqual(tui_model.parse_input_line("/search   ", "alpha").kind, "error")

    def test_menu_and_help_list_it(self):
        self.assertIn("/search", tui_model.SLASH_COMMANDS)
        self.assertIn("/search", tui_model.SLASH_USAGE)
        self.assertTrue(any("/search" in line for line in tui_model.help_lines()))
        self.assertIn("/search", tui_model.HELP_TEXT)

    def test_the_rendered_box_marks_matches(self):
        from herdr_team.cmd_search import render

        payload = {"team": "alpha", "query": "rate", "members": ["a"], "total": 1, "scanned": {"a": {"sessions": 1, "bytes": 2048, "skipped": []}},
                   "hits": [{"member": "a", "kind": "claude", "session": "s", "ts": "2026-09-22T10:00:00.000Z", "role": "assistant",
                             "excerpt": "the rate is high", "match": [4, 8], "source_path": "/x/s.jsonl", "history": False}]}
        text = render(payload, 80)
        self.assertIn("»rate«", text)
        self.assertIn(datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"), text)  # local time
        self.assertIn("a 1 conversation (2 KiB)", text)
        self.assertIn("no messages match", render(dict(payload, hits=[], total=0)))


if __name__ == "__main__":
    unittest.main()

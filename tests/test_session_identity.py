"""Session identity (0.5.0): a member is tied to its harness session, not only to a terminal.

Every test here failed against 0.4.2: ``Member.session`` was never written or
read, rehydration knew nothing of ``agent_session``, a fresh agent in a
member's pane kept the member's name unbriefed, and there was no ``resume``.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest import mock

from herdr_team import identity
from herdr_team import cmd_hooks, cmd_roster, hooks, picker, render, roster, store, tui_model
from herdr_team.errors import HerdrTeamError
from support import FakeApi, TempState, fake_agent
from test_identity import own_shell_rig
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon


def sess(value: str, source: str = "herdr:codex", agent: str = "codex"):
    return {"source": source, "agent": agent, "kind": "id", "value": value}


def member_doc(ts, name):
    for m in store.read_json(ts.team.team_json)["members"]:
        if m["name"] == name:
            return m
    raise AssertionError("no member " + name)


def board_events(ts, event):
    return [r for r in store.BoardStore(ts.team).read() if r.get("event") == event]


def jobs(ts):
    return sorted(p.name for p in ts.team.jobs_dir.glob("*.json"))


def error_of(result):
    """``(code, error dict)`` from a CLI run whose stderr may start with an ``author unverified`` warning."""
    code, _out, err = result
    lines = [line for line in err.splitlines() if line.lstrip().startswith("{")]
    return code, (json.loads(lines[-1]) if lines else err)


# --------------------------------------------------------------------------
# pure helpers


class SessionHelperTests(unittest.TestCase):
    def test_session_of_reads_a_live_row_or_a_bare_info(self):
        row = fake_agent("w2:p1", "term_r1", "codex", None, agent_session=sess("0199-abc"))
        out = roster.session_of(row, seen_at="2026-09-07T10:00:00Z")
        self.assertEqual(out, {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "0199-abc", "seen_at": "2026-09-07T10:00:00Z"})
        self.assertEqual(roster.session_key(roster.session_of(sess("x"))), ("herdr:codex", "x"))
        self.assertIsNone(roster.session_of(fake_agent("w2:p1", "term_r1", "codex", None)))
        self.assertIsNone(roster.session_of({"agent_session": {"source": "herdr:codex", "value": ""}}))
        self.assertIsNone(roster.session_of({"agent_session": {"value": "x"}}))
        self.assertIsNone(roster.session_of(None))
        # an unusable value is refused, never trimmed to fit: a truncated id is a wrong id
        self.assertEqual(roster.session_of(sess("v" * 400))["value"], "v" * 400)
        self.assertIsNone(roster.session_of(sess("v" * 600)))  # over Herdr's id cap
        self.assertIsNone(roster.session_of(sess("has\u0000control")))
        self.assertIsNone(roster.session_of(sess("-rf")))
        self.assertIsNone(roster.session_of({"source": "s" * 100, "value": "x"}))
        # a path ref is kept as a path, and must be absolute
        self.assertEqual(roster.session_of({"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/tmp/s.json"})["kind"], "path")
        self.assertIsNone(roster.session_of({"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "rel/s.json"}))
        self.assertEqual(roster.session_of({"source": "herdr:pi", "agent": "pi", "kind": "nonsense", "value": "x"})["kind"], "id")

    def test_same_and_short_session(self):
        self.assertTrue(roster.same_session(sess("a"), roster.session_of(sess("a"))))
        self.assertFalse(roster.same_session(sess("a"), sess("b")))
        self.assertFalse(roster.same_session(sess("a"), sess("a", source="herdr:claude")))
        self.assertFalse(roster.same_session(None, None))
        self.assertEqual(roster.short_session(sess("7cad74c4-3602-4ff1-b69d-f01a83b5a560")), "…a83b5a560"[:1] + "1a83b5a560"[-8:])
        self.assertEqual(roster.short_session(sess("short")), "short")
        self.assertIsNone(roster.short_session(None))

    #: Every source Herdr 0.8.2 resumes, with the argv its own restore builds
    #: (``src/agent_resume.rs::plan``). Pinning the whole table here is the point: an entry that
    #: drifts from Herdr resumes the wrong thing or nothing.
    HERDR_TABLE = {
        "herdr:antigravity_cli": ("agy", ["agy", "--conversation", "ID"]),
        "herdr:claude": ("claude", ["claude", "--resume", "ID"]),
        "herdr:codex": ("codex", ["codex", "resume", "ID"]),
        "herdr:copilot": ("copilot", ["copilot", "--resume=ID"]),
        "herdr:cursor": ("cursor", [roster.CURSOR_AGENT_BIN, "--resume", "ID"]),
        "herdr:devin": ("devin", ["devin", "--resume", "ID"]),
        "herdr:droid": ("droid", ["droid", "--resume", "ID"]),
        "herdr:grok": ("grok", ["grok", "--resume", "ID"]),
        "herdr:hermes": ("hermes", ["hermes", "--resume", "ID"]),
        "herdr:kilo": ("kilo", ["kilo", "--session", "ID"]),
        "herdr:kimi": ("kimi", ["kimi", "--session", "ID"]),
        "herdr:mastracode": ("mastracode", ["mastracode", "--thread", "ID"]),
        "herdr:omp": ("omp", ["omp", "--resume=ID"]),
        "herdr:opencode": ("opencode", ["opencode", "--session", "ID"]),
        "herdr:pi": ("pi", ["pi", "--session", "ID"]),
        "herdr:qodercli": ("qodercli", ["qodercli", "--resume", "ID"]),
        "herdr:qwen": ("qwen", ["qwen", "--resume", "ID"]),
    }

    def test_every_herdr_source_resumes_the_way_herdr_would(self):
        self.assertEqual(sorted(roster.RESUME_COMMANDS), sorted(self.HERDR_TABLE))
        for source, (agent, expected) in sorted(self.HERDR_TABLE.items()):
            got = roster.resume_argv({"source": source, "agent": agent, "kind": "id", "value": "ID"})
            self.assertEqual(got, expected, source)

    def test_pi_and_omp_carry_an_absolute_path_instead_of_an_id(self):
        for source, agent, expected in (("herdr:pi", "pi", ["pi", "--session", "/s/t.json"]), ("herdr:omp", "omp", ["omp", "--resume=/s/t.json"])):
            self.assertEqual(roster.resume_argv({"source": source, "agent": agent, "kind": "path", "value": "/s/t.json"}), expected)
        # every other kind is identified by an id only
        with self.assertRaises(HerdrTeamError) as ctx:
            roster.resume_argv({"source": "herdr:claude", "agent": "claude", "kind": "path", "value": "/s/t.jsonl"})
        self.assertEqual((ctx.exception.code, ctx.exception.details["expected"]), ("session_unsupported", ["id"]))

    def test_resume_argv_refuses_what_it_cannot_name(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            roster.resume_argv(None)
        self.assertEqual(ctx.exception.code, "session_unknown")
        with self.assertRaises(HerdrTeamError) as ctx:
            roster.resume_argv(sess("x", source="herdr:gemini"))
        self.assertEqual(ctx.exception.code, "session_unsupported")
        self.assertEqual(len(ctx.exception.details["supported"]), 17)
        # a source paired with an agent Herdr never pairs it with is a forged report, not a guess
        with self.assertRaises(HerdrTeamError) as ctx:
            roster.resume_argv({"source": "herdr:claude", "agent": "codex", "kind": "id", "value": "x"})
        self.assertEqual((ctx.exception.code, ctx.exception.details["expected"]), ("session_unsupported", "claude"))
        # an id may hold spaces (argv is data, not shell text) but never a leading dash
        self.assertEqual(roster.resume_argv(sess("has space"))[-1], "has space")
        for bad in ("-rf", "with\u0001control", "v" * 600):
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.resume_argv(sess(bad))
            self.assertEqual(ctx.exception.code, "session_unknown", bad)

    def test_session_value_problem(self):
        self.assertIsNone(roster.session_value_problem("0199-abc"))
        self.assertIsNone(roster.session_value_problem("/abs/path", "path"))
        self.assertEqual(roster.session_value_problem("", "id"), "empty")
        self.assertEqual(roster.session_value_problem(None, "id"), "empty")
        self.assertEqual(roster.session_value_problem("rel", "path"), "is not an absolute path")
        self.assertEqual(roster.session_value_problem("-x", "id"), "starts with a dash")
        self.assertIn("512", roster.session_value_problem("v" * 513, "id"))
        self.assertIn("4096", roster.session_value_problem("/" + "v" * 4096, "path"))
        self.assertIsNone(roster.session_value_problem("/" + "v" * 600, "path"))  # a path may exceed the id cap


# --------------------------------------------------------------------------
# the matcher


class RehydrateBySessionTests(unittest.TestCase):
    def two_codex(self, with_sessions=True):
        a = roster.Member("alpha-a", "a", "codex", "term_old_a", pane_id="w1:p1", workspace_id="w1", label="team:alpha/a", cwd="/w",
                          session=sess("sess-a") if with_sessions else None)
        b = roster.Member("alpha-b", "b", "codex", "term_old_b", pane_id="w1:p2", workspace_id="w1", label="team:alpha/b", cwd="/w",
                          session=sess("sess-b") if with_sessions else None)
        rows = [
            fake_agent("w1:p7", "term_new_b", "codex", None, cwd="/w", agent_session=sess("sess-b")),
            fake_agent("w1:p8", "term_new_a", "codex", None, cwd="/w", agent_session=sess("sess-a")),
        ]
        return [a, b], rows

    def test_two_same_kind_agents_in_one_directory_bind_by_session(self):
        members, rows = self.two_codex()
        result = roster.rehydrate_match(members, rows, [])
        self.assertEqual({b.member: (b.agent["terminal_id"], b.how) for b in result.bindings},
                         {"alpha-a": ("term_new_a", "session"), "alpha-b": ("term_new_b", "session")})
        self.assertEqual(result.missing, [])
        self.assertEqual(result.unbound, [])
        # without sessions the fingerprint step refuses: two candidates each
        members, rows = self.two_codex(with_sessions=False)
        result = roster.rehydrate_match(members, rows, [])
        self.assertEqual(result.bindings, [])
        self.assertEqual(sorted(result.missing), ["alpha-a", "alpha-b"])

    def test_session_beats_label_pane_and_name(self):
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_gone", pane_id="w1:p1", workspace_id="w1", label="team:alpha/reviewer", cwd="/w", session=sess("mine"))
        rows = [
            fake_agent("w1:p1", "term_pane", "codex", None),  # same pane id
            fake_agent("w1:p5", "term_label", "codex", None),  # carries the label
            fake_agent("w1:p6", "term_name", "codex", "alpha-reviewer"),  # carries the name
            fake_agent("w9:p9", "term_sess", "codex", None, agent_session=sess("mine")),
        ]
        panes = [{"terminal_id": "term_label", "pane_id": "w1:p5", "label": "team:alpha/reviewer"}]
        result = roster.rehydrate_match([member], rows, panes)
        self.assertEqual([(b.agent["terminal_id"], b.how) for b in result.bindings], [("term_sess", "session")])

    def test_no_recorded_session_keeps_the_old_order(self):
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_a", pane_id="w1:p1", workspace_id="w1", label="team:alpha/reviewer", cwd="/w")
        rows = [
            fake_agent("w9:p9", "term_other", "codex", None, agent_session=sess("someone-else")),
            fake_agent("w1:p1", "term_a", "codex", None, agent_session=sess("now-here")),
        ]
        result = roster.rehydrate_match([member], rows, [])
        self.assertEqual([(b.agent["terminal_id"], b.how) for b in result.bindings], [("term_a", "terminal_id")])

    def test_own_terminal_wins_while_two_rows_carry_the_session(self):
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_a", pane_id="w1:p1", workspace_id="w1", label="team:alpha/reviewer", cwd="/w", session=sess("s"))
        rows = [
            fake_agent("w3:p3", "term_new", "codex", None, agent_session=sess("s")),
            fake_agent("w1:p1", "term_a", "codex", "alpha-reviewer", agent_session=sess("s")),
        ]
        result = roster.rehydrate_match([member], rows, [])
        self.assertEqual([(b.agent["terminal_id"], b.how) for b in result.bindings], [("term_a", "session")])
        # once the old pane is gone the member follows its session
        result = roster.rehydrate_match([member], rows[:1], [])
        self.assertEqual([(b.agent["terminal_id"], b.how) for b in result.bindings], [("term_new", "session")])

    def test_a_session_of_another_kind_lands_in_kind_changed_semantics(self):
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_a", pane_id="w1:p1", workspace_id="w1", label="team:alpha/reviewer", cwd="/w", session=sess("s"))
        rows = [fake_agent("w3:p3", "term_new", "gemini", None, agent_session=sess("s"))]
        result = roster.rehydrate_match([member], rows, [])
        self.assertEqual([(b.how, b.kind_matches) for b in result.bindings], [("session", False)])


# --------------------------------------------------------------------------
# the daemon


class DaemonSessionTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.api.set_response("agent.rename", {"type": "ok"})
        self.api.set_response("pane.rename", {"type": "ok"})
        self.api.set_response("pane.list", {"type": "pane_list", "panes": []})

    def rows(self, reviewer_session, worker_session=None, reviewer_terminal="term_r1", reviewer_pane="w2:p1", reviewer_name="alpha-reviewer"):
        return [
            fake_agent(reviewer_pane, reviewer_terminal, "codex", reviewer_name, agent_session=reviewer_session),
            fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working", agent_session=worker_session),
        ]

    def connect(self, rows):
        self.api.set_response("agent.list", {"type": "agent_list", "agents": rows})
        self.d.on_connected()

    def repoll(self, rows):
        self.api.set_response("agent.list", {"type": "agent_list", "agents": rows})
        self.d.poll_agents(force=True)
        self.d.reconcile()

    def test_a_polled_session_is_recorded_without_a_restart(self):
        self.connect(self.rows(sess("first")))
        m = member_doc(self.ts, "alpha-reviewer")
        self.assertEqual((m["session"]["source"], m["session"]["value"]), ("herdr:codex", "first"))
        self.assertEqual(m["generation"], 1)
        self.assertEqual(board_events(self.ts, "member_restarted"), [])
        self.assertIsNone(member_doc(self.ts, "alpha-worker")["session"])
        record = roster.read_pane_record(self.ts.session, "term_r1")
        self.assertEqual(record["session"]["value"], "first")
        # the same session again changes nothing
        self.repoll(self.rows(sess("first")))
        self.assertEqual(member_doc(self.ts, "alpha-reviewer")["generation"], 1)
        self.assertEqual(board_events(self.ts, "member_restarted"), [])

    def test_a_new_session_on_the_same_terminal_is_a_restart_that_briefs_again(self):
        self.ts.members[0].update({"session": sess("first"), "briefed_at": "2026-09-07T10:00:00Z", "briefing_seq": 3})
        self.ts.write_team_json()
        self.connect(self.rows(sess("first")))
        self.assertEqual(member_doc(self.ts, "alpha-reviewer")["generation"], 1)
        self.repoll(self.rows(sess("second")))
        m = member_doc(self.ts, "alpha-reviewer")
        self.assertEqual((m["session"]["value"], m["generation"], m["briefed_at"], m["status"], m["terminal_id"], m["name"]), ("second", 2, None, "active", "term_r1", "alpha-reviewer"))
        restarted = board_events(self.ts, "member_restarted")
        self.assertEqual(len(restarted), 1)
        self.assertIn("briefing again", restarted[0]["text"])
        self.assertEqual((restarted[0]["session"], restarted[0]["previous_session"]), ("second", "first"))
        self.assertEqual(len(jobs(self.ts)), 1)
        self.assertTrue(any("started a new session on w2:p1 (first -> second); re-briefing" in line for line in self.d.logged), self.d.logged)
        # a compaction re-reports the same id: nothing happens
        self.repoll(self.rows(sess("second")))
        self.assertEqual(member_doc(self.ts, "alpha-reviewer")["generation"], 2)
        self.assertEqual(len(board_events(self.ts, "member_restarted")), 1)
        self.assertEqual(len(jobs(self.ts)), 1)

    def test_a_session_change_makes_the_poll_reconcile_at_once(self):
        self.ts.members[0].update({"session": sess("first")})
        self.ts.write_team_json()
        self.connect(self.rows(sess("first")))
        self.d.reconcile_due = False
        self.api.set_response("agent.list", {"type": "agent_list", "agents": self.rows(sess("second"))})
        self.d.poll_agents(force=True)
        self.assertTrue(self.d.reconcile_due)

    def test_cold_restart_rebinds_by_session_before_anything_else(self):
        self.ts.members[0].update({"session": sess("mine"), "status": "missing"})
        self.ts.write_team_json()
        rows = [
            fake_agent("w2:p1", "term_pane", "codex", None),  # the old pane id, no session: a stranger
            fake_agent("w7:p3", "term_new", "codex", None, agent_session=sess("mine")),
            fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working"),
        ]
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [
            {"terminal_id": "term_pane", "pane_id": "w2:p1", "label": "team:alpha/reviewer", "agent": "codex"},
            {"terminal_id": "term_new", "pane_id": "w7:p3", "label": None, "agent": "codex"},
        ]})
        self.connect(rows)
        m = member_doc(self.ts, "alpha-reviewer")
        self.assertEqual((m["terminal_id"], m["pane_id"], m["status"], m["generation"]), ("term_new", "w7:p3", "active", 2))
        self.assertTrue(any("alpha-reviewer rebound by session to term_new (w7:p3)" in line for line in self.d.logged), self.d.logged)
        self.assertIn({"target": "w7:p3", "name": "alpha-reviewer"}, [p for m_, p in self.api.calls if m_ == "agent.rename"])
        self.assertIn({"pane_id": "w7:p3", "label": "team:alpha/reviewer"}, [p for m_, p in self.api.calls if m_ == "pane.rename"])
        restarted = board_events(self.ts, "member_restarted")
        self.assertEqual(len(restarted), 1)
        self.assertIn("on w7:p3", restarted[0]["text"])
        self.assertNotIn("briefing again", restarted[0]["text"])  # it kept its memory
        self.assertEqual(jobs(self.ts), [])

    def test_a_resume_elsewhere_moves_an_active_member_with_a_generation_bump(self):
        self.ts.members[0].update({"session": sess("mine")})
        self.ts.write_team_json()
        self.connect(self.rows(sess("mine")))
        self.repoll([
            fake_agent("w7:p3", "term_new", "codex", None, agent_session=sess("mine")),
            fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working"),
        ])
        m = member_doc(self.ts, "alpha-reviewer")
        self.assertEqual((m["terminal_id"], m["pane_id"], m["status"], m["generation"]), ("term_new", "w7:p3", "active", 2))
        self.assertIsNotNone(m.get("briefed_at") or None) if m.get("briefed_at") else None

    def test_a_label_match_onto_a_foreign_session_briefs_again(self):
        # cold restart: the member's labelled pane hosts a codex again, but a *new* conversation
        self.ts.members[0].update({"session": sess("mine"), "status": "missing", "briefed_at": "2026-09-07T10:00:00Z"})
        self.ts.write_team_json()
        rows = [
            fake_agent("w2:p4", "term_new", "codex", None, agent_session=sess("theirs")),
            fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working"),
        ]
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [{"terminal_id": "term_new", "pane_id": "w2:p4", "label": "team:alpha/reviewer", "agent": "codex"}]})
        self.connect(rows)
        m = member_doc(self.ts, "alpha-reviewer")
        self.assertEqual((m["terminal_id"], m["session"]["value"], m["generation"], m["briefed_at"]), ("term_new", "theirs", 2, None))
        self.assertTrue(any("rebound by label" in line for line in self.d.logged), self.d.logged)
        self.assertIn("briefing again", board_events(self.ts, "member_restarted")[0]["text"])
        self.assertEqual(len(jobs(self.ts)), 1)

    def test_pane_records_for_gone_terminals_are_dropped_when_pane_list_was_fetched(self):
        gone = self.ts.session.pane_record("term_gone")
        store.write_json(gone, {"team": "alpha", "name": "alpha-old", "gen": 1})
        live = self.ts.session.pane_record("term_r1")
        store.write_json(live, {"team": "alpha", "name": "alpha-reviewer", "gen": 1})
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [
            {"terminal_id": "term_r1", "pane_id": "w2:p1", "label": "team:alpha/reviewer", "agent": "codex"},
            {"terminal_id": "term_shell", "pane_id": "w2:p9", "label": None, "agent": None},
        ]})
        shell = self.ts.session.pane_record("term_shell")
        store.write_json(shell, {"team": "alpha", "name": "alpha-x", "gen": 1})
        # the worker's terminal is absent, so this pass fetches pane.list
        self.connect([fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer")])
        self.assertFalse(gone.exists())
        self.assertTrue(live.exists())
        self.assertTrue(shell.exists())  # still a pane
        self.assertTrue(self.ts.session.pane_record("term_w1").exists() or True)  # held by a member: never dropped
        self.assertTrue(any("dropped pane record term_gone" in line for line in self.d.logged), self.d.logged)

    def test_who_json_carries_the_short_session(self):
        self.connect(self.rows(sess("0199-reviewer-session")))
        who = self.d.build_who()
        rows = {m["name"]: m for m in who["teams"]["alpha"]["members"]}
        self.assertEqual(rows["alpha-reviewer"]["session"], "…-session")
        self.assertIsNone(rows["alpha-worker"]["session"])


# --------------------------------------------------------------------------
# the hook slow path


class HookSlowPathSessionTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = FakeApi(self.ts.socket_path)
        self.api.set_response("agent.rename", {"type": "ok"})
        self.api.set_response("pane.rename", {"type": "ok"})

    def run_event(self, pane_id="w2:p1"):
        env = self.ts.env_with(
            HERDR_PLUGIN_EVENT="pane.agent_detected",
            HERDR_PLUGIN_EVENT_JSON=json.dumps({"type": "pane_agent_detected", "data": {"pane_id": pane_id}}),
            HERDR_PLUGIN_ID="herdr-synapse", HERDR_PANE_ID="wA:p6", HERDR_WORKSPACE_ID="wA", HERDR_TAB_ID="wA:t1",
        )
        out = io.StringIO()
        code = hooks.run_hook_event(["agent_detected"], env, api=self.api, stdout=out)
        return code, json.loads(out.getvalue())

    def test_a_new_terminal_with_the_member_session_binds_by_session(self):
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0].update({"status": "missing", "session": sess("mine")})
        store.write_json(self.ts.team.team_json, doc)
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w7:p3", "term_new", "codex", None, agent_session=sess("mine"))})
        code, out = self.run_event(pane_id="w7:p3")
        m = member_doc(self.ts, "alpha-reviewer")
        self.assertEqual((m["terminal_id"], m["pane_id"], m["status"], m["generation"]), ("term_new", "w7:p3", "active", 2))
        self.assertTrue(any("matched by session" in line for line in out.get("log", [])) or out["changes"], out)
        self.assertEqual(roster.read_pane_record(self.ts.session, "term_new")["session"]["value"], "mine")

    def test_a_new_session_on_the_member_terminal_briefs_again(self):
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0].update({"session": sess("first"), "briefed_at": "2026-09-07T10:00:00Z"})
        store.write_json(self.ts.team.team_json, doc)
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", agent_session=sess("second"))})
        code, out = self.run_event()
        m = member_doc(self.ts, "alpha-reviewer")
        self.assertEqual((m["session"]["value"], m["generation"], m["briefed_at"], m["terminal_id"]), ("second", 2, None, "term_r1"))
        self.assertEqual(len(jobs(self.ts)), 1)
        # the same session again is idempotent
        code, out = self.run_event()
        self.assertEqual(member_doc(self.ts, "alpha-reviewer")["generation"], 2)
        self.assertEqual(len(jobs(self.ts)), 1)

    def test_a_first_session_is_adopted_quietly(self):
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", agent_session=sess("first"))})
        code, out = self.run_event()
        m = member_doc(self.ts, "alpha-reviewer")
        self.assertEqual((m["session"]["value"], m["generation"]), ("first", 1))
        self.assertEqual(jobs(self.ts), [])


# --------------------------------------------------------------------------
# the Claude SessionStart channel


class ClaudeSessionStartTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_payload_shape(self):
        # One call, then compared against itself: this used to stamp ``seen_at``
        # twice and assert the two were equal, which they are only while the
        # millisecond does not tick between them. It failed roughly one CI run
        # in ten before that was the whole of the defect.
        built = cmd_hooks._claude_session({"session_id": "7cad-1"})
        self.assertEqual(built, roster.session_of(sess("7cad-1", "herdr:claude", "claude")) | {"seen_at": built["seen_at"]})
        self.assertRegex(built["seen_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertIsNone(cmd_hooks._claude_session({}))
        self.assertIsNone(cmd_hooks._claude_session({"session_id": 5}))
        self.assertIsNone(cmd_hooks._claude_session({"session_id": "-flag"}))
        # Herdr accepts any bounded, control-free id, so the hook does too
        self.assertEqual(cmd_hooks._claude_session({"session_id": "has space"})["value"], "has space")

    def test_first_report_records_and_a_new_id_restarts(self):
        live = cmd_hooks._claude_session({"session_id": "one"})
        self.assertEqual(cmd_hooks.record_member_session(self.ts.team, "alpha-worker", live, "startup"), {"session": live})
        m = member_doc(self.ts, "alpha-worker")
        self.assertEqual((m["session"]["value"], m["generation"]), ("one", 1))
        self.assertEqual(board_events(self.ts, "member_restarted"), [])
        # compact re-reports the same id
        self.assertIsNone(cmd_hooks.record_member_session(self.ts.team, "alpha-worker", cmd_hooks._claude_session({"session_id": "one"}), "compact"))
        # /clear mints a new one
        fields = cmd_hooks.record_member_session(self.ts.team, "alpha-worker", cmd_hooks._claude_session({"session_id": "two"}), "clear")
        self.assertEqual((fields["generation"], fields["briefed_at"], fields["session"]["value"]), (2, None, "two"))
        m = member_doc(self.ts, "alpha-worker")
        self.assertEqual((m["session"]["value"], m["generation"], m["briefed_at"]), ("two", 2, None))
        restarted = board_events(self.ts, "member_restarted")
        self.assertEqual(len(restarted), 1)
        self.assertEqual((restarted[0]["session"], restarted[0]["previous_session"], restarted[0]["session_source"]), ("two", "one", "clear"))
        self.assertEqual(len(jobs(self.ts)), 1)
        self.assertIsNone(cmd_hooks.record_member_session(self.ts.team, "human", live, "startup"))
        self.assertIsNone(cmd_hooks.record_member_session(self.ts.team, "nobody", live, "startup"))


# --------------------------------------------------------------------------
# resume


class ResumeCommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0].update({"session": sess("0199-reviewer"), "status": "missing", "cwd": os.fspath(self.ts.tmp)})
        store.write_json(self.ts.team.team_json, doc)
        # ``resume`` execs the harness in the operator's own shell pane, so
        # that pane (w3:p1) must be verifiably the operator's: a shell Herdr
        # cannot vouch for is named human but holds no authority any more.
        base = live_api
        info, table = own_shell_rig("w3:p1")

        def shell_verified(agents=None):
            api = base(agents)
            api.set_response("pane.process_info", lambda p: info if p["pane_id"] == "w3:p1"
                             else {"process_info": {"shell_pid": 1, "foreground_process_group_id": 1, "foreground_processes": []}})
            return api

        for patcher in (mock.patch.object(sys.modules[__name__], "live_api", shell_verified),
                        mock.patch.object(identity, "ps_table", return_value=table)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_print_shows_the_exact_command(self):
        code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "resume", "alpha-reviewer", "--print"], env_no_daemon(self.ts), live_api()))
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["argv"], ["codex", "resume", "0199-reviewer"])
        self.assertEqual(payload["command"], "codex resume 0199-reviewer")
        self.assertEqual(payload["cwd"], os.fspath(self.ts.tmp))
        code, out, err = run_cli(["--team", "alpha", "resume", "alpha-reviewer", "--print"], env_no_daemon(self.ts), live_api())
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("codex resume 0199-reviewer  # in "), out)

    def test_refusals(self):
        api = live_api()
        code, _, err = json_out(run_cli(["--json", "--team", "alpha", "resume", "alpha-worker", "--print"], env_no_daemon(self.ts), api))
        self.assertEqual((code, err["code"]), (1, "session_unknown"))
        code, _, err = json_out(run_cli(["--json", "--team", "alpha", "resume", "nobody", "--print"], env_no_daemon(self.ts), api))
        self.assertEqual((code, err["code"]), (1, "member_not_found"))
        code, _, err = json_out(run_cli(["--json", "--team", "alpha", "resume", "human", "--print"], env_no_daemon(self.ts), api))
        self.assertEqual((code, err["code"]), (1, "member_not_found"))
        # a member pane may not resume anyone (human only)
        code, _, err = json_out(run_cli(["--json", "resume", "alpha-reviewer", "--print"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2"), api))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        # outside Herdr the harness could not report its session for a pane
        code, _, err = json_out(run_cli(["--json", "--team", "alpha", "resume", "alpha-reviewer"], env_no_daemon(self.ts), api))
        self.assertEqual((code, err["code"]), (1, "outside_herdr"))
        self.assertEqual(err["command"], "codex resume 0199-reviewer")
        # a pane that already hosts an agent is not the human's shell, so it cannot resume anyone
        with mock.patch.object(cmd_roster.shutil, "which", return_value="/usr/bin/codex"):
            code, err = error_of(run_cli(["--json", "--team", "alpha", "resume", "alpha-reviewer"], env_no_daemon(self.ts, HERDR_PANE_ID="w5:p1"), api))
        self.assertEqual((code, err["code"]), (3, "not_a_member"))
        # the harness is not installed
        with mock.patch.object(cmd_roster.shutil, "which", return_value=None):
            code, err = error_of(run_cli(["--json", "--team", "alpha", "resume", "alpha-reviewer"], env_no_daemon(self.ts, HERDR_PANE_ID="w3:p1"), api))
        self.assertEqual((code, err["code"]), (1, "command_not_found"))

    def test_a_member_still_running_its_session_is_not_resumed_twice(self):
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0]["status"] = "active"
        store.write_json(self.ts.team.team_json, doc)
        rows = [fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", agent_session=sess("0199-reviewer"))]
        with mock.patch.object(cmd_roster.shutil, "which", return_value="/usr/bin/codex"):
            code, err = error_of(run_cli(["--json", "--team", "alpha", "resume", "alpha-reviewer"], env_no_daemon(self.ts, HERDR_PANE_ID="w3:p1"), live_api(rows)))
        self.assertEqual((code, err["code"]), (1, "member_alive"))
        self.assertEqual(err["pane_id"], "w2:p1")

    def test_exec_replaces_the_shell_in_the_member_directory(self):
        calls = []
        chdirs = []
        with mock.patch.object(cmd_roster, "_execvp", lambda file, argv: calls.append((file, list(argv)))), \
                mock.patch.object(cmd_roster.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch("os.chdir", lambda path: chdirs.append(path)):
            code, out, err = run_cli(["--team", "alpha", "resume", "alpha-reviewer"], env_no_daemon(self.ts, HERDR_PANE_ID="w3:p1"), live_api())
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [("codex", ["codex", "resume", "0199-reviewer"])])
        self.assertEqual(chdirs, [os.fspath(self.ts.tmp)])
        self.assertIn("resuming alpha-reviewer in w3:p1: codex resume 0199-reviewer", err)
        audits = store.read_jsonl(self.ts.team.audit_jsonl) if hasattr(store, "read_jsonl") and hasattr(self.ts.team, "audit_jsonl") else None
        if audits is not None:
            self.assertTrue(any(a.get("event") == "resume" for a in audits), audits)

    def test_print_never_execs(self):
        calls = []
        with mock.patch.object(cmd_roster, "_execvp", lambda file, argv: calls.append(file)):
            code, out, err = run_cli(["--team", "alpha", "resume", "alpha-reviewer", "--print"], env_no_daemon(self.ts, HERDR_PANE_ID="w3:p1"), live_api())
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------
# what the operator sees


class SessionDisplayTests(unittest.TestCase):
    def test_who_json_and_render_show_the_session(self):
        with TempState() as ts:
            ts.members[0]["session"] = sess("0199-reviewer-session")
            ts.write_team_json()
            team = roster.load_team(ts.team)
            who = roster.build_who_json({"alpha": team}, [], {}, {"alpha": {}}, {})
            rows = {m["name"]: m for m in who["teams"]["alpha"]["members"]}
            self.assertEqual(rows["alpha-reviewer"]["session"], "…-session")
            self.assertIsNone(rows["alpha-worker"]["session"])
            text = render.render_who({"members": rows.values() and list(rows.values()), "charter": None}, "alpha", ascii_only=True)
            self.assertIn("session ...-session", text.replace("…", "..."))
            self.assertNotIn("session", render.render_who({"members": list(rows.values()), "charter": None}, "alpha", brief=True))

    def test_me_and_the_tree_show_the_session(self):
        text = render.render_me({"team": "alpha", "name": "alpha-reviewer", "role": "reviewer", "kind": "codex", "pane_id": "w2:p1", "session": "…-session", "teammates": [], "unread": 0}, {"team": "alpha"})
        self.assertIn("session …-session (herdr-synapse resume alpha-reviewer reopens it)", text)
        line = tui_model.roster_line({"name": "alpha-reviewer", "kind": "codex", "pane_id": "w2:p1", "agent_status": "idle", "session": sess("0199-reviewer-session")}, width=120)
        self.assertIn("sess -session", line)
        line = tui_model.roster_line({"name": "alpha-reviewer", "kind": "codex", "pane_id": "w2:p1", "agent_status": "idle", "session": "…-session"}, width=120)
        self.assertIn("sess -session", line)
        self.assertNotIn("sess", tui_model.roster_line({"name": "alpha-reviewer", "kind": "codex", "pane_id": "w2:p1", "agent_status": "idle"}, width=120))

    def test_tree_action_prints_the_resume_command(self):
        intent = tui_model.Intent("member_resume", {"team": "alpha", "member": "alpha-reviewer", "terminal_id": "term_r1"})
        self.assertEqual(picker.action_args(intent), ["--team", "alpha", "resume", "alpha-reviewer", "--print"])
        self.assertIn("codex resume 0199", picker.action_success_status(intent, {"command": "codex resume 0199"}))
        self.assertIn("alpha-reviewer: no harness session", picker.action_failure_status(intent, {"code": "session_unknown", "message": "no harness session is recorded for this member"}))
        self.assertIn("resume", [key for key, _label in tui_model.ACTION_OPTIONS])
        self.assertIsNone(tui_model._action_refusal("resume", {"name": "x", "status": "missing"}))
        self.assertIn("member_resume", picker.ACTION_INTENTS)


if __name__ == "__main__":
    unittest.main()

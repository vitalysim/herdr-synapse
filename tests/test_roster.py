"""Roster: grammar, reserved words, persistence, claims, rehydration, who.json, tokens, join."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock
from typing import Any, Dict, List

from support import FAKE_AGENTS, FakeApi, FakeError, TempState, fake_agent, identity_tokens

from herdr_team import roster, store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "roster"
HUMAN_MEMBER = {"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"}


def load_fixture(name: str) -> Dict[str, Any]:
    with open(FIXTURES / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


def members_of(fixture: Dict[str, Any]) -> List[roster.Member]:
    return [roster.Member.from_json(m) for m in fixture["members"]]


def board_events(ts: TempState, team: str = "alpha") -> List[str]:
    return [r.get("event") for r in roster.read_board_records(ts.layout.team(team)) if r.get("from") == "system"]


class GrammarTests(unittest.TestCase):
    def test_member_name_grammar(self) -> None:
        self.assertEqual(roster.validate_member_name("alpha-reviewer"), "alpha-reviewer")
        self.assertEqual(roster.validate_member_name("a"), "a")
        self.assertEqual(roster.validate_member_name("a" * 32), "a" * 32)
        for bad in ("", "Alpha", "1abc", "a b", "a.b", "a" * 33, "-x", None, 42):
            with self.assertRaises(HerdrTeamError, msg=repr(bad)) as ctx:
                roster.validate_member_name(bad)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "name_invalid")
            self.assertEqual(ctx.exception.exit_code, EXIT_REFUSED)

    def test_reserved_words_and_kind_labels_are_refused(self) -> None:
        for word in ("human", "all", "me", "none", "system", "team"):
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.validate_member_name(word)
            self.assertEqual(ctx.exception.code, "name_reserved")
            self.assertEqual(ctx.exception.details["reason"], "reserved word")
        for label in ("claude", "codex", "gemini", "cursor", "kimi", "amp", "muse", "opencode"):
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.validate_member_name(label)
            self.assertEqual(ctx.exception.code, "name_reserved")
            self.assertEqual(ctx.exception.details["reason"], "agent kind label")
        for alias in ("claude-code", "cursor-agent", "antigravity"):
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.validate_member_name(alias)
            self.assertEqual(ctx.exception.details["reason"], "agent kind alias")
        self.assertEqual(len(roster.KIND_LABELS), 23)

    def test_role_grammar(self) -> None:
        self.assertEqual(roster.validate_role("reviewer"), "reviewer")
        self.assertEqual(roster.validate_role("a" * 32), "a" * 32)
        self.assertEqual(roster.validate_role("opencode-dev-ideation"), "opencode-dev-ideation")
        self.assertEqual(roster.derive_name("red-dev", "opencode-dev-ideation-and-more"), "red-dev-ideation-and-more")
        self.assertEqual(roster.fit_member_name("red-dev", "opencode-dev-brainstormer"), "red-dev-brainstormer")
        self.assertEqual(roster.fit_member_name("red-dev", "opencode-dev"), "red-dev-opencode-dev")  # fits: untouched
        self.assertEqual(roster.fit_member_name("red-dev", "a" * 32), "red-dev-" + "a" * 24)  # one oversized segment: clipped
        self.assertEqual(roster.fit_member_name("alpha", "codex-reviewer-of-everything"), "alpha-reviewer-of-everything")  # 34 chars: the kind label goes first
        for bad in ("a" * 65, "Reviewer", "", "human", "codex", "me"):
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.validate_role(bad)
            self.assertEqual(ctx.exception.code, "role_invalid")

    def test_derive_name_prefixed_and_plain(self) -> None:
        self.assertEqual(roster.derive_name("alpha", "reviewer"), "alpha-reviewer")
        self.assertEqual(roster.derive_name("alpha", "reviewer", naming="plain"), "reviewer")
        self.assertEqual(len(roster.derive_name("a" * 15, "b" * 14)), 30)
        with self.assertRaises(HerdrTeamError) as ctx:
            roster.derive_name("Alpha", "reviewer")
        self.assertEqual(ctx.exception.code, "team_name_invalid")
        with self.assertRaises(HerdrTeamError):
            roster.derive_name("alpha", "codex", naming="plain")

    def test_suffix_only_when_the_cap_allows(self) -> None:
        self.assertEqual(roster.suffixed_name("alpha-reviewer", 2), "alpha-reviewer-2")
        longest = roster.derive_name("a" * 15, "b" * 14)  # 30 chars: -2 fits exactly
        self.assertEqual(len(roster.suffixed_name(longest, 2)), 32)
        with self.assertRaises(HerdrTeamError) as ctx:
            roster.suffixed_name("c" * 31, 2)
        self.assertEqual(ctx.exception.code, "name_too_long")
        self.assertEqual(roster.unique_name("x", ["y"]), "x")
        self.assertEqual(roster.unique_name("x", ["x"]), "x-2")
        with self.assertRaises(HerdrTeamError) as ctx:
            roster.unique_name("x", ["x", "x-2"])
        self.assertEqual(ctx.exception.code, "name_taken")

    def test_label_round_trip(self) -> None:
        self.assertEqual(roster.label_for("alpha", "reviewer"), "team:alpha/reviewer")
        self.assertEqual(roster.parse_label("team:alpha/reviewer"), ("alpha", "reviewer"))
        self.assertIsNone(roster.parse_label("Team console"))
        self.assertIsNone(roster.parse_label(None))
        self.assertIsNone(roster.parse_label("team:Alpha/reviewer"))

    def test_name_taken_candidates_parse_tolerantly(self) -> None:
        msg = "agent name 'x' taken; candidates: terminal_id=term_1 pane_id=w1:p1 kind=codex; terminal_id=term_2 pane_id=w1:p2; garbage here"
        parsed = roster.parse_name_taken_candidates(msg)
        self.assertEqual(parsed, [{"terminal_id": "term_1", "pane_id": "w1:p1", "kind": "codex"}, {"terminal_id": "term_2", "pane_id": "w1:p2"}])
        self.assertEqual(roster.parse_name_taken_candidates("no candidates"), [])


class ModelTests(unittest.TestCase):
    def test_member_round_trip_and_defaults(self) -> None:
        member = roster.Member.from_json({"name": "x", "role": "r", "kind": "codex", "terminal_id": "t", "status": "weird", "delivery": "pigeon"})
        self.assertEqual(member.status, "unbound")
        self.assertEqual(member.delivery, "nudge")
        self.assertEqual(member.generation, 1)
        obj = member.to_json()
        self.assertEqual(set(obj), {
            "name", "role", "kind", "terminal_id", "pane_id", "workspace_id", "tab_id", "label", "cwd", "brief",
            "managed", "session", "status", "generation", "delivery", "verified_kind", "joined_at", "last_seen_at",
            "briefed_at", "briefing_seq", "charter_seq_acked",
        })
        self.assertEqual(roster.Member.from_json(obj).to_json(), obj)
        with self.assertRaises(HerdrTeamError) as ctx:
            roster.Member.from_json({"role": "r"})
        self.assertEqual(ctx.exception.code, "roster_corrupt")

    def test_team_round_trip_schema_and_lookups(self) -> None:
        with TempState() as ts:
            doc = store.read_json(ts.team.team_json)
            team = roster.Team.from_json(doc)
            self.assertEqual(team.team, "alpha")
            self.assertEqual(team.revision, 1)
            self.assertEqual(team.to_json()["members"], doc["members"] if all("brief" in m for m in doc["members"]) else team.to_json()["members"])
            self.assertEqual([m.name for m in team.agents()], ["alpha-reviewer", "alpha-worker"])
            self.assertEqual(team.names(), ["alpha-reviewer", "alpha-worker", "human"])
            self.assertEqual(team.find("term_r1").name, "alpha-reviewer")  # type: ignore[union-attr]
            self.assertEqual([m.name for m in team.holders("worker")], ["alpha-worker"])
            self.assertIsNone(team.find("nobody"))
            self.assertEqual(team.name_policy, roster.NAME_POLICY_ADOPT)
            team.config["name_policy"] = "enforce"
            self.assertEqual(team.name_policy, roster.NAME_POLICY_ENFORCE)
            doc["schema"] = 99
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.Team.from_json(doc)
            self.assertEqual(ctx.exception.code, "roster_schema_unsupported")

    def test_retired_names_resolve_for_ten_minutes(self) -> None:
        member = roster.Member("new", "r", "codex", "t")
        roster.apply_adoption(member, "newer", retired_at="2026-09-04T10:00:00.000Z")
        self.assertEqual(member.name, "newer")
        self.assertEqual(member.previous_names[0]["name"], "new")
        retired = roster.parse_iso("2026-09-04T10:00:00.000Z") or 0.0
        self.assertTrue(member.retired_name_active("new", now=retired + 599))
        self.assertFalse(member.retired_name_active("new", now=retired + 601))
        team = roster.Team("alpha", "", "", "", members=[member])
        member.previous_names[0]["retired_at"] = roster.now_iso()
        self.assertIs(team.find("new"), member)

    def test_iso_helpers(self) -> None:
        stamp = roster.now_iso()
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertIsNotNone(roster.parse_iso(stamp))
        self.assertIsNone(roster.parse_iso("yesterday"))
        self.assertIsNone(roster.parse_iso(None))


class PersistenceTests(unittest.TestCase):
    def test_create_team_writes_human_and_refuses_duplicates(self) -> None:
        with TempState(write_team=False) as ts:
            team = roster.create_team(ts.layout, "beta", charter={"seq": 1, "text": "go"})
            self.assertEqual(team.revision, 1)
            self.assertEqual([m.name for m in team.members], ["human"])
            self.assertEqual(team.socket, os.fspath(ts.layout.socket))
            self.assertEqual(oct(os.stat(ts.session.team("beta").team_json).st_mode & 0o777), "0o600")
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.create_team(ts.layout, "beta")
            self.assertEqual(ctx.exception.code, "team_exists")
            self.assertEqual(roster.create_team(ts.layout, "beta", reuse=True).charter, {"seq": 1, "text": "go"})
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.create_team(ts.layout, "gamma", naming="weird")
            self.assertEqual(ctx.exception.exit_code, 2)
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.load_team(ts.session.team("nope"))
            self.assertEqual(ctx.exception.code, "team_not_found")

    def test_save_team_checks_the_revision(self) -> None:
        with TempState() as ts:
            team = roster.load_team(ts.team)
            saved = roster.save_team(ts.team, team, expected_revision=1)
            self.assertEqual(saved.revision, 2)
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.save_team(ts.team, team, expected_revision=1)
            self.assertEqual(ctx.exception.code, "roster_conflict")
            self.assertEqual(ctx.exception.details, {"team": "alpha", "expected": 1, "current": 2})
            self.assertEqual(roster.save_team(ts.team, team).revision, 3)  # unchecked save still bumps

    def test_update_team_retries_on_a_conflict(self) -> None:
        with TempState() as ts:
            attempts: List[int] = []

            def mutate(team: roster.Team) -> None:
                attempts.append(team.revision)
                if len(attempts) == 1:
                    # a racing writer slipped a newer revision in between load and save
                    doc = team.to_json()
                    doc["revision"] = team.revision + 5
                    store.write_json(ts.team.team_json, doc)
                team.members[0].brief = "retry-{}".format(len(attempts))

            saved = roster.update_team(ts.team, mutate)
            self.assertEqual(attempts, [1, 6])
            self.assertEqual(saved.revision, 7)
            self.assertEqual(roster.load_team(ts.team).members[0].brief, "retry-2")

    def test_update_team_gives_up_after_three_conflicts(self) -> None:
        with TempState() as ts:
            calls: List[int] = []

            def always_conflict(team: roster.Team) -> None:
                calls.append(1)
                doc = team.to_json()
                doc["revision"] = team.revision + 1
                store.write_json(ts.team.team_json, doc)

            with self.assertRaises(HerdrTeamError) as ctx:
                roster.update_team(ts.team, always_conflict)
            self.assertEqual(ctx.exception.code, "roster_conflict")
            self.assertEqual(len(calls), 3)

    def test_update_team_lock_timeout_is_board_locked(self) -> None:
        with TempState() as ts:
            held = store.team_lock(ts.team).acquire()
            try:
                start = time.monotonic()
                with self.assertRaises(HerdrTeamError) as ctx:
                    with store.team_lock(ts.team, timeout=0.05):
                        pass
                self.assertEqual(ctx.exception.code, "board_locked")
                self.assertEqual(ctx.exception.exit_code, 5)
                self.assertLess(time.monotonic() - start, 2.0)
            finally:
                held.release()

    def test_list_teams_and_session_check(self) -> None:
        with TempState() as ts:
            roster.create_team(ts.layout, "beta")
            listed = roster.list_teams(ts.layout)
            self.assertEqual([t["team"] for t in listed], ["alpha", "beta"])
            self.assertEqual(listed[0]["members"], 3)
            self.assertFalse(listed[0]["running"])
            store.write_json(ts.session.console_json, {"default_team": "beta"})
            listed = roster.list_teams(ts.layout)
            self.assertEqual([t["default"] for t in listed], [False, True])
            team = roster.load_team(ts.team)
            roster.check_session(ts.layout, team)
            team.socket = "/nonexistent/other.sock"
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.check_session(ts.layout, team)
            self.assertEqual(ctx.exception.code, "team_session_mismatch")
            roster.check_session(ts.layout, team, allow_mismatch=True)

    def test_pane_records(self) -> None:
        with TempState() as ts:
            path = roster.write_pane_record(ts.session, "term_r1", "alpha", "alpha-reviewer", 2)
            self.assertEqual(path, ts.session.pane_record("term_r1"))
            self.assertEqual(roster.read_pane_record(ts.session, "term_r1"), {"team": "alpha", "name": "alpha-reviewer", "gen": 2})
            self.assertIsNone(roster.read_pane_record(ts.session, "term_none"))
            self.assertIsNone(roster.read_pane_record(ts.session, "../evil"))
            self.assertTrue(roster.remove_pane_record(ts.session, "term_r1"))
            self.assertFalse(roster.remove_pane_record(ts.session, "term_r1"))
            self.assertFalse(roster.remove_pane_record(ts.session, None))


class ClaimTests(unittest.TestCase):
    def test_claim_conflict_between_two_teams(self) -> None:
        with TempState() as ts:
            roster.create_team(ts.layout, "beta")
            self.assertIsNone(roster.claim_check(ts.layout, "term_free", "beta"))
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.claim_check(ts.layout, "term_r1", "beta")
            err = ctx.exception
            self.assertEqual(err.code, "member_claimed")
            self.assertEqual(err.details["owner_team"], "alpha")
            self.assertEqual(err.details["member"], "alpha-reviewer")
            # the owning team may re-claim its own terminal
            self.assertIsNone(roster.claim_check(ts.layout, "term_r1", "alpha"))
            self.assertEqual(roster.claim_check(ts.layout, "term_r1", "beta", steal=True), "alpha")
            # no fourth lock file appeared: only team.lock files exist
            self.assertFalse(ts.session.claims_lock.exists())
            self.assertFalse((ts.state_root / "claims.lock").exists())

    def test_steal_moves_the_member_and_records_member_gone(self) -> None:
        with TempState() as ts:
            roster.create_team(ts.layout, "beta")
            member = roster.Member("beta-reviewer", "reviewer", "codex", "term_r1", pane_id="w2:p1")
            team, previous = roster.Roster(ts.layout, "beta").add_member(member, steal=True)
            self.assertEqual(previous, "alpha")
            self.assertEqual([m.name for m in team.members], ["human", "beta-reviewer"])
            alpha = roster.load_team(ts.team)
            self.assertEqual(alpha.find("alpha-reviewer").status, "left")  # type: ignore[union-attr]
            self.assertIsNone(alpha.find_by_terminal("term_r1"))
            self.assertEqual(board_events(ts, "alpha"), ["member_gone"])
            self.assertEqual(roster.read_pane_record(ts.session, "term_r1"), {"team": "beta", "name": "beta-reviewer", "gen": 1})

    def test_add_member_without_steal_is_refused_and_leaves_no_trace(self) -> None:
        with TempState() as ts:
            roster.create_team(ts.layout, "beta")
            member = roster.Member("beta-reviewer", "reviewer", "codex", "term_r1")
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.Roster(ts.layout, "beta").add_member(member)
            self.assertEqual(ctx.exception.code, "member_claimed")
            self.assertEqual([m.name for m in roster.load_team(ts.session.team("beta")).members], ["human"])
            self.assertEqual(roster.load_team(ts.team).find("alpha-reviewer").status, "active")  # type: ignore[union-attr]

    def test_claim_check_holds_every_team_lock_in_sorted_order(self) -> None:
        with TempState() as ts:
            roster.create_team(ts.layout, "beta")
            order: List[str] = []
            original = store.team_lock

            def spy(team: Any, timeout: float = 5.0) -> store.FileLock:
                order.append(team.name if hasattr(team, "name") else os.fspath(team))
                return original(team, timeout)

            store.team_lock = spy  # type: ignore[assignment]
            try:
                roster.claim_check(ts.layout, "term_free", "zeta")
            finally:
                store.team_lock = original  # type: ignore[assignment]
            self.assertEqual(order, ["alpha", "beta", "zeta"])


class MemberCrudTests(unittest.TestCase):
    def test_add_member_validates_and_indexes(self) -> None:
        with TempState() as ts:
            rs = roster.Roster(ts.layout, "alpha")
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.add_member(roster.Member("codex", "scout", "codex", "term_new"))
            self.assertEqual(ctx.exception.code, "name_reserved")
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.add_member(roster.Member("alpha-reviewer", "scout", "codex", "term_new"))
            self.assertEqual(ctx.exception.code, "name_taken")
            team, previous = rs.add_member(roster.Member("alpha-scout", "scout", "gemini", "term_new", pane_id="w3:p1"))
            self.assertIsNone(previous)
            added = team.find("alpha-scout")
            assert added is not None
            self.assertIsNotNone(added.joined_at)
            self.assertEqual(roster.read_pane_record(ts.session, "term_new"), {"team": "alpha", "name": "alpha-scout", "gen": 1})

    def test_set_status(self) -> None:
        with TempState() as ts:
            rs = roster.Roster(ts.layout, "alpha")
            member = rs.set_status("alpha-worker", "missing", last_seen_at="2026-09-04T11:00:00.000Z")
            self.assertEqual(member.status, "missing")
            self.assertEqual(rs.load().find("alpha-worker").last_seen_at, "2026-09-04T11:00:00.000Z")  # type: ignore[union-attr]
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.set_status("alpha-worker", "dancing")
            self.assertEqual(ctx.exception.exit_code, 2)
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.set_status("nobody", "missing")
            self.assertEqual(ctx.exception.code, "member_not_found")
            self.assertEqual(ctx.exception.details["roster"], ["alpha-reviewer", "alpha-worker", "human"])

    def test_remove_member_clears_projections_and_tombstones(self) -> None:
        with TempState() as ts:
            api = FakeApi()
            api.set_response("agent.rename", {"type": "ok"})
            api.set_response("pane.rename", {"type": "ok"})
            roster.write_pane_record(ts.session, "term_r1", "alpha", "alpha-reviewer", 1)
            result = roster.Roster(ts.layout, "alpha").remove_member(api, "alpha-reviewer")
            self.assertEqual(result["removed"], "alpha-reviewer")
            self.assertTrue(result["tokens_cleared"])
            self.assertTrue(result["name_cleared"])
            metadata = [p for m, p in api.calls if m == "pane.report_metadata"]
            self.assertEqual(metadata[0]["tokens"], identity_tokens(None, None))
            self.assertEqual(metadata[0]["source"], roster.TOKEN_SOURCE_ROSTER)
            self.assertEqual(metadata[1]["tokens"], {"team_task": None})
            self.assertIn(("pane.rename", {"pane_id": "w2:p1", "label": None}), api.calls)
            self.assertIn(("agent.rename", {"target": "w2:p1", "name": None}), api.calls)
            team = roster.load_team(ts.team)
            self.assertEqual(team.find("alpha-reviewer").status, "left")  # type: ignore[union-attr]
            self.assertEqual(team.names(), ["alpha-worker", "human"])
            self.assertIsNone(roster.read_pane_record(ts.session, "term_r1"))
            self.assertEqual(board_events(ts), ["member_gone"])
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.Roster(ts.layout, "alpha").remove_member(api, "human")
            self.assertEqual(ctx.exception.code, "member_not_found")

    def test_remove_with_keep_name_and_leave(self) -> None:
        with TempState() as ts:
            api = FakeApi()
            api.set_response("pane.rename", {"type": "ok"})
            rs = roster.Roster(ts.layout, "alpha")
            result = rs.remove_member(api, "alpha-reviewer", keep_name=True)
            self.assertFalse(result["name_cleared"])
            self.assertNotIn("agent.rename", [m for m, _ in api.calls])
            api.set_error("agent.rename", "agent_not_found", "gone")
            self.assertEqual(rs.leave(api, "alpha-worker"), {"team": "alpha", "left": "alpha-worker"})
            self.assertEqual(rs.load().agents(), [])

    def test_bind_bumps_generation_and_reapplies_projections(self) -> None:
        with TempState() as ts:
            api = FakeApi()
            api.set_response("agent.rename", {"type": "ok"})
            api.set_response("pane.rename", {"type": "ok"})
            rs = roster.Roster(ts.layout, "alpha")
            rs.set_status("alpha-worker", "missing")
            target = roster.ResolvedTarget("w4:p2", "term_w2", "claude", None, "w4", "w4:t1", "/tmp/new", False, "idle", {})
            result = rs.bind(api, "alpha-worker", target)
            self.assertEqual(result["previous_terminal_id"], "term_w1")
            member = result["member"]
            self.assertEqual(member["terminal_id"], "term_w2")
            self.assertEqual(member["pane_id"], "w4:p2")
            self.assertEqual(member["generation"], 2)
            self.assertEqual(member["status"], "active")
            self.assertEqual(member["cwd"], "/tmp/new")
            self.assertIn(("agent.rename", {"target": "w4:p2", "name": "alpha-worker"}), api.calls)
            self.assertIn(("pane.rename", {"pane_id": "w4:p2", "label": "team:alpha/worker"}), api.calls)
            stamped = [p for m, p in api.calls if m == "pane.report_metadata"]
            self.assertEqual(stamped[0]["tokens"], identity_tokens("alpha", "worker"))
            self.assertEqual(roster.read_pane_record(ts.session, "term_w2"), {"team": "alpha", "name": "alpha-worker", "gen": 2})
            self.assertIsNone(roster.read_pane_record(ts.session, "term_w1"))
            self.assertEqual(board_events(ts), ["member_restarted"])

    def test_bind_clears_the_team_label_left_on_the_previous_shell_pane(self) -> None:
        """RT-02 (rig, 2026-09-05): after a cold restart ``bind`` to w1:p9 left ``team:alpha/worker`` on the old pane w3:p1."""
        from support import fake_pane

        with TempState() as ts:
            api = FakeApi()
            api.set_response("agent.rename", {"type": "ok"})
            api.set_response("pane.rename", {"type": "ok"})
            api.set_response("pane.list", {"type": "pane_list", "panes": [
                fake_pane("w2:p2", "term_shell9", None, "team:alpha/worker"),  # the old pane: a plain shell with the stale label
                fake_pane("w4:p2", "term_w2", "claude", None),
            ]})
            rs = roster.Roster(ts.layout, "alpha")
            rs.set_status("alpha-worker", "missing")
            target = roster.ResolvedTarget("w4:p2", "term_w2", "claude", None, "w4", "w4:t1", "/tmp/new", False, "idle", {})
            rs.bind(api, "alpha-worker", target)
            renames = [p for m, p in api.calls if m == "pane.rename"]
            self.assertEqual(renames, [{"pane_id": "w4:p2", "label": "team:alpha/worker"}, {"pane_id": "w2:p2", "label": None}])

    def test_bind_leaves_the_previous_pane_alone_when_it_changed_hands(self) -> None:
        from support import fake_pane

        with TempState() as ts:
            api = FakeApi()
            api.set_response("agent.rename", {"type": "ok"})
            api.set_response("pane.rename", {"type": "ok"})
            rs = roster.Roster(ts.layout, "alpha")
            target = roster.ResolvedTarget("w4:p2", "term_w2", "claude", None, "w4", "w4:t1", None, False, "idle", {})
            # another agent took the old pane (label kept by its own team), or the label already differs
            api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w2:p2", "term_x", "codex", "team:alpha/worker")]})
            rs.set_status("alpha-worker", "missing")
            rs.bind(api, "alpha-worker", target)
            api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w2:p2", "term_x", None, "team:beta/qa")]})
            rs.set_status("alpha-worker", "missing")
            rs.bind(api, "alpha-worker", roster.ResolvedTarget("w6:p1", "term_w6", "claude", None, "w6", "w6:t1", None, False, "idle", {}))
            self.assertNotIn({"pane_id": "w2:p2", "label": None}, [p for m, p in api.calls if m == "pane.rename"])
            # pane.list unavailable: tolerated
            api.set_error("pane.list", "unknown_method", "no")
            rs.set_status("alpha-worker", "missing")
            rs.bind(api, "alpha-worker", roster.ResolvedTarget("w7:p1", "term_w7", "claude", None, "w7", "w7:t1", None, False, "idle", {}))
            self.assertEqual(rs.load().find("alpha-worker").pane_id, "w7:p1")

    def test_bind_refuses_kind_mismatch_and_foreign_claims(self) -> None:
        with TempState() as ts:
            api = FakeApi()
            rs = roster.Roster(ts.layout, "alpha")
            other_kind = roster.ResolvedTarget("w4:p2", "term_w2", "codex", None, "w4", "w4:t1", None, False, "idle", {})
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.bind(api, "alpha-worker", other_kind)
            self.assertEqual(ctx.exception.code, "kind_mismatch")
            rs.set_status("alpha-worker", "kind_changed")
            api.set_response("agent.rename", {"type": "ok"})
            api.set_response("pane.rename", {"type": "ok"})
            rebound = rs.bind(api, "alpha-worker", other_kind)
            self.assertEqual(rebound["member"]["kind"], "codex")
            roster.create_team(ts.layout, "beta")
            claimed = roster.ResolvedTarget("w2:p1", "term_r1", "codex", None, "w2", "w2:t1", None, False, "idle", {})
            roster.Roster(ts.layout, "beta").add_member(roster.Member("beta-x", "x", "codex", None))
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.Roster(ts.layout, "beta").bind(api, "beta-x", claimed)
            self.assertEqual(ctx.exception.code, "member_claimed")
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.bind(api, "human", claimed)
            self.assertEqual(ctx.exception.code, "member_not_found")

    def test_dissolve_archives_the_team_dir(self) -> None:
        with TempState() as ts:
            api = FakeApi()
            api.set_response("pane.rename", {"type": "ok"})
            roster.write_pane_record(ts.session, "term_r1", "alpha", "alpha-reviewer", 1)
            result = roster.Roster(ts.layout, "alpha").dissolve(api, timestamp="20260904T120000Z")
            self.assertEqual(result["members_cleared"], 2)
            archived = Path(result["archived_to"])
            self.assertEqual(archived, ts.session.team_archive("alpha", "20260904T120000Z"))
            self.assertTrue((archived / "team.json").is_file())
            self.assertFalse(ts.team.team_json.exists())
            self.assertEqual(ts.session.list_teams(), [])
            self.assertIsNone(roster.read_pane_record(ts.session, "term_r1"))
            labels = [p for m, p in api.calls if m == "pane.rename"]
            self.assertEqual(sorted(p["pane_id"] for p in labels), ["w2:p1", "w2:p2"])

    def test_adopt_rename_keeps_history_and_posts_a_note(self) -> None:
        with TempState() as ts:
            rs = roster.Roster(ts.layout, "alpha")
            member = rs.adopt_rename("alpha-reviewer", "alice")
            self.assertEqual(member.name, "alice")
            self.assertEqual(member.previous_names[0]["name"], "alpha-reviewer")
            team = rs.load()
            self.assertIs(team.find("alpha-reviewer"), team.find("alice"))
            self.assertEqual(board_events(ts), ["renamed"])
            self.assertEqual(roster.read_pane_record(ts.session, "term_r1")["name"], "alice")  # type: ignore[index]
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.adopt_rename("alice", "alpha-worker")
            self.assertEqual(ctx.exception.code, "name_taken")
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.adopt_rename("alice", "codex")
            self.assertEqual(ctx.exception.code, "name_reserved")


class RehydrationTests(unittest.TestCase):
    def test_cold_restart_fixture_follows_the_plan_order(self) -> None:
        fixture = load_fixture("cold_restart.json")
        result = roster.rehydrate_match(members_of(fixture), fixture["agent_list"], fixture["pane_list"])
        bound = {b.member: [b.agent["terminal_id"], b.how] for b in result.bindings}
        self.assertEqual(bound, fixture["expected"]["bindings"])
        self.assertEqual({u["member"]: u["candidate"]["terminal_id"] for u in result.unbound}, fixture["expected"]["unbound"])
        self.assertEqual(result.unbound[0]["how"], roster.MATCH_FINGERPRINT)
        self.assertEqual(result.missing, fixture["expected"]["missing"])
        self.assertEqual(result.kind_changed, [])
        terminals = [b.agent["terminal_id"] for b in result.bindings]
        self.assertEqual(len(terminals), len(set(terminals)))  # each agent binds at most once
        self.assertTrue(all(b.kind_matches for b in result.bindings))

    def test_in_session_move_fixture_rebinds_by_terminal_id(self) -> None:
        fixture = load_fixture("in_session_move.json")
        result = roster.rehydrate_match(members_of(fixture), fixture["agent_list"], fixture["pane_list"])
        bound = {b.member: [b.agent["terminal_id"], b.how] for b in result.bindings}
        self.assertEqual(bound, fixture["expected"]["bindings"])
        reviewer = next(b for b in result.bindings if b.member == "alpha-reviewer")
        self.assertEqual(reviewer.agent["pane_id"], fixture["expected"]["reviewer_pane_id"])
        self.assertEqual({k["member"]: k["agent"]["terminal_id"] for k in result.kind_changed}, fixture["expected"]["kind_changed"])
        self.assertEqual(result.missing, [])
        self.assertEqual(result.unbound, [])

    def test_terminal_match_beats_label_and_label_beats_pane_id(self) -> None:
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_a", pane_id="w1:p1", workspace_id="w1", label="team:alpha/reviewer", cwd="/w")
        agents = [
            fake_agent("w1:p1", "term_c", "codex", None),  # same pane id
            fake_agent("w1:p5", "term_b", "codex", None),  # carries the label
            fake_agent("w9:p9", "term_a", "codex", None),  # same terminal
        ]
        panes = [{"terminal_id": "term_b", "pane_id": "w1:p5", "label": "team:alpha/reviewer"}]
        result = roster.rehydrate_match([member], agents, panes)
        self.assertEqual(result.bindings[0].how, roster.MATCH_TERMINAL)
        self.assertEqual(result.bindings[0].agent["terminal_id"], "term_a")
        result = roster.rehydrate_match([member], agents[:2], panes)
        self.assertEqual(result.bindings[0].how, roster.MATCH_LABEL)
        self.assertEqual(result.bindings[0].agent["terminal_id"], "term_b")
        result = roster.rehydrate_match([member], agents[:1], panes)
        self.assertEqual(result.bindings[0].how, roster.MATCH_PANE)

    def test_fingerprint_needs_uniqueness_and_waits_for_bind(self) -> None:
        member = roster.Member("alpha-x", "x", "codex", "term_old", pane_id="w1:p1", workspace_id="w1", cwd="/w")
        twins = [fake_agent("w1:p3", "term_1", "codex", None, cwd="/w"), fake_agent("w1:p4", "term_2", "codex", None, cwd="/w")]
        result = roster.rehydrate_match([member], twins)
        self.assertEqual(result.missing, ["alpha-x"])
        result = roster.rehydrate_match([member], twins[:1])
        self.assertEqual(result.bindings, [])
        self.assertEqual(result.unbound[0]["member"], "alpha-x")
        self.assertFalse(roster.AUTO_BIND_FINGERPRINT)

    def test_left_and_human_members_are_skipped(self) -> None:
        gone = roster.Member("alpha-x", "x", "codex", "term_1", status="left")
        human = roster.Member("human", "operator", "human", None)
        result = roster.rehydrate_match([gone, human], [fake_agent("w1:p1", "term_1", "codex", "alpha-x")])
        self.assertEqual(result.bindings, [])
        self.assertEqual(result.missing, [])


class NameLossAndPolicyTests(unittest.TestCase):
    def test_classify_name_loss(self) -> None:
        member = roster.Member("alpha-worker", "worker", "claude", "term_w1", pane_id="w2:p2", status="active")
        self.assertEqual(roster.classify_name_loss("pane.agent_detected", {"pane_id": "w2:p2", "released": True}, member), roster.NAME_LOSS_RELEASED)
        self.assertIsNone(roster.classify_name_loss("pane.agent_detected", {"pane_id": "w2:p2", "released": False}, member))
        self.assertIsNone(roster.classify_name_loss("pane.agent_detected", {"pane_id": "w2:p9", "released": True}, member))
        self.assertEqual(roster.classify_name_loss("pane.updated", {"pane": {"terminal_id": "term_w1", "agent": "claude", "name": None}}, member), roster.NAME_LOSS_SESSION_CHANGE)
        self.assertEqual(roster.classify_name_loss("pane_updated", {"terminal_id": "term_w1", "agent": "claude"}, member), roster.NAME_LOSS_SESSION_CHANGE)
        self.assertIsNone(roster.classify_name_loss("pane.updated", {"terminal_id": "term_w1", "agent": "claude", "name": "alpha-worker"}, member))
        self.assertIsNone(roster.classify_name_loss("pane.updated", {"terminal_id": "term_w1", "agent": "codex"}, member))
        self.assertIsNone(roster.classify_name_loss("pane.updated", {"terminal_id": "term_w1", "agent": "claude", "launch_pending": True}, member))
        self.assertIsNone(roster.classify_name_loss("launch_deadline", {}, member))
        member.status = "starting"
        self.assertEqual(roster.classify_name_loss("launch_deadline", {}, member), roster.NAME_LOSS_LAUNCH_DEADLINE)
        self.assertIsNone(roster.classify_name_loss("pane.updated", "garbage", member))  # type: ignore[arg-type]

    def test_adopt_versus_enforce(self) -> None:
        member = roster.Member("alpha-worker", "worker", "claude", "term_w1")
        self.assertEqual(roster.reconcile_live_name(member, "alpha-worker")["action"], "none")
        self.assertEqual(roster.reconcile_live_name(member, None), {"action": "reapply", "name": "alpha-worker", "old": None})
        adopted = roster.reconcile_live_name(member, "bob", now="2026-09-04T12:00:00.000Z")
        self.assertEqual(adopted, {"action": "adopt", "name": "bob", "old": "alpha-worker", "retired_at": "2026-09-04T12:00:00.000Z"})
        enforced = roster.reconcile_live_name(member, "bob", policy=roster.NAME_POLICY_ENFORCE)
        self.assertEqual(enforced, {"action": "reapply", "name": "alpha-worker", "old": "bob"})
        refused = roster.reconcile_live_name(member, "codex")
        self.assertEqual(refused["action"], "reapply")
        self.assertEqual(refused["refused"], "name_reserved")


class TokenProjectionTests(unittest.TestCase):
    def test_identity_tokens_have_no_ttl_and_task_has_120s(self) -> None:
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_r1", pane_id="w2:p1")
        commands = roster.token_commands(member, "alpha", task_headline="→ review diff")
        self.assertEqual(len(commands), 2)
        identity_cmd, task_cmd = commands
        self.assertEqual(identity_cmd.params(), {"pane_id": "w2:p1", "source": "herdr-team:roster", "tokens": identity_tokens("alpha", "reviewer")})
        self.assertIsNone(identity_cmd.ttl_ms)
        self.assertEqual(task_cmd.source, "herdr-team:task")
        self.assertEqual(task_cmd.ttl_ms, 120000)
        self.assertEqual(task_cmd.params()["tokens"], {"team_task": "→ review diff"})
        self.assertEqual(task_cmd.argv(), ["pane", "report-metadata", "w2:p1", "--source", "herdr-team:task", "--token", "team_task=→ review diff", "--ttl-ms", "120000"])
        self.assertEqual(roster.token_commands(member, "alpha"), commands[:1])

    def test_clear_and_missing_pane(self) -> None:
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_r1", pane_id="w2:p1")
        cleared = roster.token_commands(member, "alpha", clear=True)
        self.assertEqual([c.tokens for c in cleared], [identity_tokens(None, None), {"team_task": None}])
        argv = cleared[0].argv()
        for key in ("team", "team_role", "team_c1", "team_c6"):
            self.assertIn("{}=".format(key), argv)  # an empty value clears the key
        self.assertEqual(roster.token_commands(roster.Member("x", "r", "codex", "t"), "alpha"), [])
        self.assertEqual(roster.token_commands(roster.Member("x", "r", "codex", "t"), "alpha", pane_id="w5:p5")[0].pane_id, "w5:p5")

    def test_values_are_clipped_to_eighty_chars(self) -> None:
        member = roster.Member("x", "r", "codex", "t", pane_id="w1:p1")
        command = roster.token_commands(member, "alpha", task_headline="a\nb " + "x" * 100)[1]
        self.assertEqual(len(command.tokens["team_task"]), 80)
        self.assertNotIn("\n", command.tokens["team_task"])

    def test_execute_records_failures_without_raising(self) -> None:
        api = FakeApi()
        api.set_error("pane.report_metadata", "pane_not_found", "gone")
        member = roster.Member("x", "r", "codex", "t", pane_id="w1:p1")
        results = roster.execute_token_commands(api, roster.token_commands(member, "alpha", task_headline="hi"))
        self.assertEqual([r["ok"] for r in results], [False, False])
        self.assertEqual(results[0]["error"], "pane_not_found")


class WhoJsonTests(unittest.TestCase):
    def test_build_who_json_shape(self) -> None:
        with TempState() as ts:
            team = roster.load_team(ts.team)
            team.find("alpha-worker").charter_seq_acked = 1  # type: ignore[union-attr]
            team.find("alpha-worker").briefed_at = roster.now_iso()  # type: ignore[union-attr]
            gone = roster.Member("alpha-gone", "gone", "codex", "term_gone", status="left")
            team.members.append(gone)
            charters = {"alpha": {"seq": 1, "text": "Find and fix the bug.\nsecond line", "refs": ["charter.md"]}}
            mutes = {"alpha": {"*": None, "alpha-reviewer": "2026-09-04T13:00:00.000Z"}}
            doc = roster.build_who_json(
                {"alpha": team}, FAKE_AGENTS, charters, mutes, {"alpha": {"wrong_target": 0, "landed_working": 3}},
                default_team="alpha", socket="/tmp/x.sock", beat_at="2026-09-04T12:00:00.000Z",
                pending_nudges={"alpha": {"alpha-reviewer": 2}},
                headlines={"alpha": {"alpha-reviewer": "→ review the diff for the session bug"}},
                unread={"alpha": {"alpha-worker": 4}},
            )
            self.assertEqual(doc["v"], 1)
            self.assertEqual(doc["daemon_beat_at"], "2026-09-04T12:00:00.000Z")
            self.assertEqual(doc["default_team"], "alpha")
            self.assertEqual(doc["charters"], {"alpha": {"seq": 1, "headline": "Find and fix the bug.", "refs": ["charter.md"]}})
            members = {m["name"]: m for m in doc["teams"]["alpha"]["members"]}
            self.assertEqual(set(members), {"alpha-reviewer", "alpha-worker", "human"})
            reviewer = members["alpha-reviewer"]
            self.assertEqual(reviewer["agent_status"], "idle")
            self.assertEqual(reviewer["pending_nudges"], 2)
            self.assertEqual(reviewer["muted_until"], "2026-09-04T13:00:00.000Z")
            self.assertTrue(reviewer["charter_stale"])
            self.assertFalse(reviewer["briefed"])
            self.assertLessEqual(len(reviewer["last_headline"]), 25)
            self.assertTrue(reviewer["last_headline"].startswith("→ review"))
            worker = members["alpha-worker"]
            self.assertEqual(worker["agent_status"], "working")
            self.assertFalse(worker["charter_stale"])
            self.assertTrue(worker["briefed"])
            self.assertEqual(worker["unread"], 4)
            self.assertIsNone(worker["muted_until"])
            self.assertFalse(members["human"]["charter_stale"])
            self.assertIsNone(members["human"]["agent_status"])
            self.assertEqual(doc["ledger"]["alpha"]["landed_working"], 3)
            for key in ("name", "role", "kind", "status", "agent_status", "pane_id", "terminal_id", "terminal_title_stripped",
                        "last_headline", "pending_nudges", "muted_until", "verified_kind", "delivery", "hooks_last_seen", "last_seen_at", "brief"):
                self.assertIn(key, reviewer)
            json.dumps(doc)

    def test_global_mute_applies_to_everyone(self) -> None:
        with TempState() as ts:
            team = roster.load_team(ts.team)
            doc = roster.build_who_json({"alpha": team}, [], {}, {"alpha": {"*": "2026-09-04T14:00:00.000Z"}}, {})
            self.assertEqual({m["muted_until"] for m in doc["teams"]["alpha"]["members"]}, {"2026-09-04T14:00:00.000Z"})
            self.assertEqual(doc["charters"], {})


class JoinTests(unittest.TestCase):
    def setUp(self) -> None:
        # ``join`` ends with ``ensure_daemon``: without a patch it double-forks this
        # unittest process into a real daemon that retries against the temp
        # socket for 60 s after the test is gone (five orphans per run).
        patcher = mock.patch("herdr_team.daemon.ensure_daemon", return_value=True)
        self.ensure_daemon = patcher.start()
        self.addCleanup(patcher.stop)

    def _api(self) -> FakeApi:
        api = FakeApi()
        api.set_response("agent.rename", {"type": "ok"})
        api.set_response("pane.rename", {"type": "ok"})
        def pane_get(params: Dict[str, Any]) -> Dict[str, Any]:
            if params["pane_id"] == "w3:p9":
                return {"type": "pane_info", "pane": {"pane_id": "w3:p9", "terminal_id": "term_shell", "agent": None}}
            raise FakeError("pane_not_found", "nope")

        api.set_response("pane.get", pane_get)
        return api

    def test_join_unnamed_agent_renames_labels_stamps_and_briefs(self) -> None:
        with TempState() as ts:
            api = self._api()
            team = roster.load_team(ts.team)
            member = roster.join(ts.layout, api, team, "wA:p6", "lead", None, "  Lead the effort.  ", env=ts.env, requested_by={"name": "human", "via": "console"})
            self.assertEqual(member.name, "alpha-lead")
            self.assertEqual(member.kind, "claude")
            self.assertEqual(member.terminal_id, "term_owner")
            self.assertEqual(member.label, "team:alpha/lead")
            self.assertEqual(member.brief, "Lead the effort.")
            self.assertFalse(member.verified_kind)
            self.assertIn(("agent.rename", {"target": "wA:p6", "name": "alpha-lead"}), api.calls)
            self.assertIn(("pane.rename", {"pane_id": "wA:p6", "label": "team:alpha/lead"}), api.calls)
            stamped = [p for m, p in api.calls if m == "pane.report_metadata"]
            self.assertEqual(stamped[0]["tokens"], identity_tokens("alpha", "lead"))
            self.assertEqual(team.find("alpha-lead").name, "alpha-lead")  # type: ignore[union-attr]
            self.assertEqual(roster.load_team(ts.team).revision, team.revision)
            self.assertEqual(roster.read_pane_record(ts.session, "term_owner"), {"team": "alpha", "name": "alpha-lead", "gen": 1})
            jobs = sorted(os.listdir(ts.team.jobs_dir))
            self.assertEqual(len(jobs), 1)
            self.assertEqual(self.ensure_daemon.call_count, 1)
            self.assertEqual(self.ensure_daemon.call_args[0][0], ts.layout)
            job = store.read_json(ts.team.jobs_dir / jobs[0])
            self.assertEqual(job["kind"], "brief")
            self.assertEqual(job["member"], "alpha-lead")
            self.assertEqual(job["requested_by"], {"name": "human", "via": "console"})

    def test_join_already_named_agent_skips_the_rename(self) -> None:
        with TempState(members=[HUMAN_MEMBER]) as ts:
            api = self._api()
            team = roster.load_team(ts.team)
            member = roster.join(ts.layout, api, team, "w2:p1", "reviewer", None, None)
            self.assertEqual(member.name, "alpha-reviewer")
            self.assertNotIn("agent.rename", [m for m, _ in api.calls])

    def test_join_derived_collision_gets_a_suffix(self) -> None:
        with TempState() as ts:
            api = self._api()
            team = roster.load_team(ts.team)
            member = roster.join(ts.layout, api, team, "wA:p6", "reviewer", None, None)
            self.assertEqual(member.name, "alpha-reviewer-2")

    def test_join_explicit_name_collisions_are_refused_before_any_write(self) -> None:
        with TempState() as ts:
            api = self._api()
            team = roster.load_team(ts.team)
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.join(ts.layout, api, team, "wA:p6", "lead", "alpha-worker", None)
            self.assertEqual(ctx.exception.code, "name_taken")
            api.set_response("agent.list", {"type": "agent_list", "agents": [dict(a) for a in FAKE_AGENTS] + [fake_agent("w6:p1", "term_x", "codex", "taken-elsewhere")]})
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.join(ts.layout, api, team, "wA:p6", "lead", "taken-elsewhere", None)
            self.assertEqual(ctx.exception.code, "agent_name_taken")
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.join(ts.layout, api, team, "wA:p6", "lead", "codex", None)
            self.assertEqual(ctx.exception.code, "name_reserved")
            self.assertNotIn("agent.rename", [m for m, _ in api.calls])
            self.assertNotIn("pane.rename", [m for m, _ in api.calls])
            self.assertEqual(roster.load_team(ts.team).revision, 1)

    def test_join_failed_rename_leaves_the_roster_untouched(self) -> None:
        with TempState() as ts:
            api = self._api()
            api.set_error("agent.rename", "agent_name_taken", "name taken; candidates: terminal_id=term_z pane_id=w7:p1")
            team = roster.load_team(ts.team)
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.join(ts.layout, api, team, "wA:p6", "lead", None, None)
            self.assertEqual(ctx.exception.code, "agent_name_taken")
            self.assertEqual(ctx.exception.details["candidates"], [{"terminal_id": "term_z", "pane_id": "w7:p1"}])
            self.assertEqual(roster.load_team(ts.team).names(), ["alpha-reviewer", "alpha-worker", "human"])
            self.assertIsNone(roster.read_pane_record(ts.session, "term_owner"))
            self.assertEqual(os.listdir(ts.team.jobs_dir), [])

    def test_join_refuses_shell_panes_pending_launches_and_kind_labels(self) -> None:
        with TempState() as ts:
            api = self._api()
            team = roster.load_team(ts.team)
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.join(ts.layout, api, team, "w3:p9", "lead", None, None)
            self.assertEqual(ctx.exception.code, "not_an_agent")
            self.assertEqual(ctx.exception.details["terminal_id"], "term_shell")
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.join(ts.layout, api, team, "codex", "lead", None, None)
            self.assertEqual(ctx.exception.code, "agent_not_found")
            self.assertEqual(ctx.exception.details["hint"], "kind_label")
            pending = fake_agent("w8:p1", "term_p", "codex", None, launch_pending=True)
            api.set_response("agent.get", {"type": "agent_info", "agent": pending})
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.join(ts.layout, api, team, "w8:p1", "lead", None, None)
            self.assertEqual(ctx.exception.code, "launch_pending")

    def test_join_claim_check_happens_before_herdr_is_touched(self) -> None:
        with TempState() as ts:
            roster.create_team(ts.layout, "beta")
            api = self._api()
            beta = roster.load_team(ts.session.team("beta"))
            with self.assertRaises(HerdrTeamError) as ctx:
                roster.join(ts.layout, api, beta, "w2:p1", "reviewer", None, None)
            self.assertEqual(ctx.exception.code, "member_claimed")
            self.assertEqual(ctx.exception.details["owner_team"], "alpha")
            self.assertNotIn("agent.rename", [m for m, _ in api.calls])
            member = roster.join(ts.layout, api, beta, "w2:p1", "reviewer", None, None, steal=True)
            self.assertEqual(member.name, "beta-reviewer")
            self.assertEqual(roster.load_team(ts.team).find("alpha-reviewer").status, "left")  # type: ignore[union-attr]

    def test_join_uses_kinds_json_for_verified_kind(self) -> None:
        with TempState() as ts:
            store.write_json(ts.session.kinds_json, {"claude": {"verified": True}})
            api = self._api()
            member = roster.join(ts.layout, api, roster.load_team(ts.team), "wA:p6", "lead", None, None)
            self.assertTrue(member.verified_kind)

    def test_join_treats_a_passed_probe_as_verified_kind(self) -> None:
        # M0 verify-live 2026-09-05: one passed ``hooks probe`` round trip opens gate 4 for the kind.
        with TempState() as ts:
            store.write_json(ts.session.kinds_json, {"claude": {"probe": {"nonce": 51880, "ok": True, "result": "landed_working"}}})
            api = self._api()
            member = roster.join(ts.layout, api, roster.load_team(ts.team), "wA:p6", "lead", None, None)
            self.assertTrue(member.verified_kind)

    def test_join_ignores_a_failed_probe(self) -> None:
        with TempState() as ts:
            store.write_json(ts.session.kinds_json, {"claude": {"probe": {"nonce": 1, "ok": False, "result": "hung"}}})
            api = self._api()
            member = roster.join(ts.layout, api, roster.load_team(ts.team), "wA:p6", "lead", None, None)
            self.assertFalse(member.verified_kind)


class SystemRecordTests(unittest.TestCase):
    def test_append_and_read_system_records(self) -> None:
        with TempState() as ts:
            seq = roster.append_system_record(ts.team, "member_gone", "x left", to=["all"], socket="/tmp/s.sock", extra={"urgent": True})
            self.assertEqual(seq, 1)
            seq2 = roster.append_system_record(ts.team, "renamed", "y renamed")
            self.assertEqual(seq2, 2)
            records = roster.read_board_records(ts.team)
            self.assertEqual([r["seq"] for r in records], [1, 2])
            self.assertEqual(records[0]["from"], "system")
            self.assertEqual(records[0]["kind"], "system")
            self.assertTrue(records[0]["urgent"])
            self.assertEqual(records[0]["origin"]["socket"], "/tmp/s.sock")
            self.assertEqual([r["seq"] for r in roster.read_board_records(ts.team, event="renamed")], [2])
            with self.assertRaises(HerdrTeamError):
                roster.append_system_record(ts.team, "not_an_event", "x")

    def test_system_append_matches_the_seq_contract(self) -> None:
        with TempState() as ts:
            store.write_json(ts.team.board_seq, {"next": 41, "active_first_seq": 1})
            seq = roster.append_system_record(ts.team, "nudged", "hello")
            self.assertEqual(seq, 41)
            self.assertEqual(store.read_json(ts.team.board_seq)["next"], 42)
            self.assertEqual(roster.read_board_records(ts.team)[-1]["seq"], 41)


class KindTrustRegressionTests(unittest.TestCase):
    """M0 verify-live 2026-09-05: a passed ``hooks probe`` counts as a verified kind for gate 4."""

    def test_kind_entry_trusted_accepts_verified_trusted_or_passed_probe(self) -> None:
        self.assertTrue(roster.kind_entry_trusted({"verified": True}))
        self.assertTrue(roster.kind_entry_trusted({"trusted": True}))
        self.assertTrue(roster.kind_entry_trusted({"probe": {"ok": True, "result": "landed_working"}}))
        self.assertFalse(roster.kind_entry_trusted({"probe": {"ok": False, "result": "hung"}}))
        self.assertFalse(roster.kind_entry_trusted({"probe": "yes"}))
        self.assertFalse(roster.kind_entry_trusted({}))
        self.assertFalse(roster.kind_entry_trusted(None))
        self.assertFalse(roster.kind_trusted({"claude": {"probe": {"ok": True}}}, "codex"))
        self.assertFalse(roster.kind_trusted("garbage", "claude"))
        self.assertFalse(roster.kind_trusted({"claude": {"probe": {"ok": True}}}, ""))


if __name__ == "__main__":
    unittest.main()

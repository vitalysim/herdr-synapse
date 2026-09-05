"""Charter and role briefs: human only, seq, history, refs, ack tracking (plan 5.5)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from support import FakeApi, TempState

from herdr_team import charter, identity, roster, store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

HUMAN = identity.Author("human", "human", identity.VIA_CONSOLE, True, pane_id="w7:p1", team="alpha")
OUTSIDE = identity.Author("human", "human", identity.VIA_OUTSIDE, False, team="alpha")
MEMBER = identity.Author("alpha-reviewer", "codex", identity.VIA_CLI, True, pane_id="w2:p1", terminal_id="term_r1", team="alpha")
SYSTEM = identity.Author("system", None, identity.VIA_HOOK, True, team="alpha")


def history_events(ts: TempState) -> list:
    return roster.read_board_records(ts.layout.team("alpha"), event="charter_updated")


class CharterModelTests(unittest.TestCase):
    def test_round_trip_and_headline(self) -> None:
        c = charter.Charter(3, "  First line here  \n\nsecond", refs=["charter.md"], updated_at="t", updated_by="human")
        self.assertEqual(c.headline(), "First line here")
        self.assertEqual(charter.Charter.from_json(c.to_json()), c)
        self.assertIsNone(charter.Charter.from_json(None))
        self.assertIsNone(charter.Charter.from_json({"text": "no seq"}))
        self.assertIsNone(charter.Charter.from_json({"seq": "x"}))
        long_line = "word " * 60
        head = charter.headline_of(long_line, 120)
        self.assertLessEqual(len(head), 120)
        self.assertTrue(head.endswith("…"))
        self.assertEqual(charter.headline_of("\n\n  \n"), "")
        self.assertEqual(charter.headline_of("a\tb   c"), "a b c")

    def test_get_charter(self) -> None:
        with TempState() as ts:
            current = charter.get_charter(ts.layout, "alpha")
            assert current is not None
            self.assertEqual(current.seq, 1)
            self.assertEqual(current.text, "Find and fix the bug.")
        with TempState(write_team=False) as ts:
            roster.create_team(ts.layout, "alpha")
            self.assertIsNone(charter.get_charter(ts.layout, "alpha"))
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.get_charter(ts.layout, "nope")
            self.assertEqual(ctx.exception.code, "team_not_found")


class CharterSetTests(unittest.TestCase):
    def test_human_set_bumps_seq_and_announces(self) -> None:
        with TempState() as ts:
            before = roster.load_team(ts.team).revision
            result = charter.set_charter(ts.layout, "alpha", HUMAN, "Ship the fix.\nReviewer owns review.", None, [])
            self.assertEqual(result.seq, 2)
            self.assertEqual(result.text, "Ship the fix.\nReviewer owns review.")
            self.assertEqual(result.refs, [])
            self.assertEqual(result.updated_by, "human")
            self.assertIsNotNone(result.updated_at)
            stored = charter.get_charter(ts.layout, "alpha")
            assert stored is not None
            self.assertEqual(stored, result)
            self.assertEqual(roster.load_team(ts.team).revision, before + 1)
            records = history_events(ts)
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["from"], "system")
            self.assertEqual(record["kind"], "system")
            self.assertEqual(record["to"], ["all"])
            self.assertEqual(record["text"], result.text)
            self.assertFalse(record["urgent"])
            self.assertEqual(record["charter"]["seq"], 2)
            self.assertEqual(record["origin"]["socket"], os.fspath(ts.layout.socket))
            audit = identity.read_audit(ts.layout, "alpha")
            self.assertEqual(audit[-1]["event"], "charter_set")
            self.assertEqual(audit[-1]["details"]["charter_seq"], 2)

    def test_urgent_flag_lands_on_the_record(self) -> None:
        with TempState() as ts:
            charter.set_charter(ts.layout, "alpha", OUTSIDE, "Now.", None, [], urgent=True)
            self.assertTrue(history_events(ts)[0]["urgent"])
            self.assertEqual(charter.charter_history(ts.layout, "alpha")[0]["urgent"], True)

    def test_agent_pane_hook_and_as_human_are_refused_and_audited(self) -> None:
        with TempState() as ts:
            for author in (MEMBER, SYSTEM):
                with self.assertRaises(HerdrTeamError) as ctx:
                    charter.set_charter(ts.layout, "alpha", author, "hijack", None, [])
                err = ctx.exception
                self.assertEqual(err.code, "author_mismatch")
                self.assertEqual(err.exit_code, EXIT_REFUSED)
                self.assertEqual(err.details["author"], author.name)
                self.assertEqual(err.details["action"], "charter set")
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_brief(ts.layout, "alpha", MEMBER, "alpha-worker", "do it")
            self.assertEqual(ctx.exception.code, "author_mismatch")
            audit = identity.read_audit(ts.layout, "alpha")
            self.assertEqual([e["event"] for e in audit], ["author_mismatch"] * 3)
            self.assertEqual([e["author"] for e in audit], ["alpha-reviewer", "system", "alpha-reviewer"])
            self.assertEqual(audit[0]["details"]["action"], "charter set")
            self.assertEqual(audit[2]["details"]["action"], "brief set")
            self.assertEqual(charter.get_charter(ts.layout, "alpha").seq, 1)  # type: ignore[union-attr]
            self.assertEqual(history_events(ts), [])

    def test_over_length_text_is_refused_with_the_file_hint(self) -> None:
        with TempState() as ts:
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_charter(ts.layout, "alpha", HUMAN, "x" * 2001, None, [])
            err = ctx.exception
            self.assertEqual(err.code, "charter_too_long")
            self.assertEqual(err.exit_code, EXIT_REFUSED)
            self.assertIn("--charter-file", err.message)
            self.assertEqual(err.details["hint"], "--charter-file")
            self.assertEqual(charter.set_charter(ts.layout, "alpha", HUMAN, "y" * 2000, None, []).seq, 2)

    def test_file_is_copied_to_charter_md_and_referenced(self) -> None:
        with TempState() as ts:
            source = ts.tmp / "spec.md"
            body = "# Goal\n\n" + ("details " * 400)
            source.write_text(body, encoding="utf-8")
            result = charter.set_charter(ts.layout, "alpha", HUMAN, None, os.fspath(source), [])
            self.assertEqual(result.refs, ["charter.md"])
            self.assertEqual(ts.team.charter_md.read_text(encoding="utf-8"), body)
            self.assertEqual(oct(os.stat(ts.team.charter_md).st_mode & 0o777), "0o600")
            self.assertLessEqual(len(result.text), charter.MAX_CHARTER_CHARS)
            self.assertTrue(result.text.startswith("# Goal"))
            self.assertEqual(result.headline(), "# Goal")
            self.assertEqual(history_events(ts)[0]["refs"], ["charter.md"])

    def test_file_edge_cases(self) -> None:
        with TempState() as ts:
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_charter(ts.layout, "alpha", HUMAN, None, None, [])
            self.assertEqual(ctx.exception.exit_code, 2)
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_charter(ts.layout, "alpha", HUMAN, "x", os.fspath(ts.tmp / "f.md"), [])
            self.assertEqual(ctx.exception.exit_code, 2)
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_charter(ts.layout, "alpha", HUMAN, None, os.fspath(ts.tmp / "missing.md"), [])
            self.assertEqual(ctx.exception.code, "ref_invalid")
            binary = ts.tmp / "bin.md"
            binary.write_bytes(b"\xff\xfe\x00bad")
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_charter(ts.layout, "alpha", HUMAN, None, os.fspath(binary), [])
            self.assertEqual(ctx.exception.code, "invalid_utf8")
            link = ts.tmp / "link.md"
            (ts.tmp / "real.md").write_text("real", encoding="utf-8")
            os.symlink(ts.tmp / "real.md", link)
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_charter(ts.layout, "alpha", HUMAN, None, os.fspath(link), [])
            self.assertEqual(ctx.exception.code, "path_symlink")
            empty = ts.tmp / "empty.md"
            empty.write_text("\x1b[31m\x00\n", encoding="utf-8")
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_charter(ts.layout, "alpha", HUMAN, None, os.fspath(empty), [])
            self.assertEqual(ctx.exception.code, "charter_empty")

    def test_text_is_sanitized(self) -> None:
        with TempState() as ts:
            result = charter.set_charter(ts.layout, "alpha", HUMAN, "\x1b[1mBold\x1b[0m goal​\x00 here \n", None, [])
            self.assertEqual(result.text, "Bold goal here")  # the zero-width space is stripped (plan 6.1)
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_charter(ts.layout, "alpha", HUMAN, "   \x07  ", None, [])
            self.assertEqual(ctx.exception.code, "charter_empty")

    def test_history_is_oldest_first(self) -> None:
        with TempState() as ts:
            self.assertEqual(charter.charter_history(ts.layout, "alpha"), [])
            charter.set_charter(ts.layout, "alpha", HUMAN, "one", None, [])
            roster.append_system_record(ts.team, "nudged", "noise")
            charter.set_charter(ts.layout, "alpha", HUMAN, "two", None, [], urgent=True)
            history = charter.charter_history(ts.layout, "alpha")
            self.assertEqual([h["text"] for h in history], ["one", "two"])
            self.assertEqual([h["charter_seq"] for h in history], [2, 3])
            self.assertEqual([h["seq"] for h in history], [1, 3])
            self.assertEqual([h["urgent"] for h in history], [False, True])
            self.assertTrue(all(h["ts"] for h in history))
            self.assertEqual(history[0]["updated_by"], "human")


class RefTests(unittest.TestCase):
    def test_refs_inside_the_team_dir_are_relative(self) -> None:
        with TempState() as ts:
            payload = ts.team.payloads_dir / "42-diff.md"
            payload.write_text("diff", encoding="utf-8")
            self.assertEqual(charter.validate_refs(ts.layout, "alpha", ["payloads/42-diff.md", os.fspath(payload)]), ["payloads/42-diff.md"])
            result = charter.set_charter(ts.layout, "alpha", HUMAN, "see diff", None, ["payloads/42-diff.md"])
            self.assertEqual(result.refs, ["payloads/42-diff.md"])

    def test_refs_under_a_member_cwd_stay_absolute(self) -> None:
        with TempState() as ts:
            cwd = ts.tmp / "work"
            cwd.mkdir()
            spec = cwd / "spec.md"
            spec.write_text("spec", encoding="utf-8")

            def mutate(team: roster.Team) -> None:
                team.find("alpha-worker").cwd = os.fspath(cwd)  # type: ignore[union-attr]

            roster.update_team(ts.team, mutate)
            self.assertEqual(charter.validate_refs(ts.layout, "alpha", [os.fspath(spec)]), [os.path.realpath(spec)])
            roots = charter.roster_roots(ts.layout, "alpha")
            self.assertEqual(roots[0], Path(os.path.realpath(ts.team.root)))
            self.assertIn(Path(os.path.realpath(cwd)), roots)

    def test_bad_refs(self) -> None:
        with TempState() as ts:
            outside = ts.tmp / "outside.md"
            outside.write_text("x", encoding="utf-8")
            for ref in [os.fspath(outside), "missing.md", "", "payloads"]:
                with self.assertRaises(HerdrTeamError, msg=ref) as ctx:
                    charter.validate_refs(ts.layout, "alpha", [ref])
                self.assertEqual(ctx.exception.code, "ref_invalid")
            link = ts.team.root / "link.md"
            os.symlink(outside, link)
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.validate_refs(ts.layout, "alpha", ["link.md"])
            self.assertEqual(ctx.exception.code, "path_symlink")
            # a bad ref leaves the charter untouched
            with self.assertRaises(HerdrTeamError):
                charter.set_charter(ts.layout, "alpha", HUMAN, "text", None, ["missing.md"])
            self.assertEqual(charter.get_charter(ts.layout, "alpha").seq, 1)  # type: ignore[union-attr]
            self.assertEqual(history_events(ts), [])


class BriefTests(unittest.TestCase):
    def test_set_brief_human_only(self) -> None:
        with TempState() as ts:
            result = charter.set_brief(ts.layout, "alpha", HUMAN, "alpha-worker", "  Own the patch.\nKeep it small.  ")
            self.assertEqual(result, {"team": "alpha", "member": "alpha-worker", "brief": "Own the patch.\nKeep it small.", "briefing_line": "Own the patch. Keep it small."})
            self.assertEqual(roster.load_team(ts.team).find("alpha-worker").brief, "Own the patch.\nKeep it small.")  # type: ignore[union-attr]
            self.assertEqual(identity.read_audit(ts.layout, "alpha")[-1]["event"], "brief_set")
            cleared = charter.set_brief(ts.layout, "alpha", OUTSIDE, "alpha-worker", "")
            self.assertIsNone(cleared["brief"])
            self.assertIsNone(cleared["briefing_line"])
            for name in ("human", "nobody"):
                with self.assertRaises(HerdrTeamError) as ctx:
                    charter.set_brief(ts.layout, "alpha", HUMAN, name, "x")
                self.assertEqual(ctx.exception.code, "member_not_found")
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.set_brief(ts.layout, "alpha", HUMAN, "alpha-worker", "x" * 2001)
            self.assertEqual(ctx.exception.code, "text_too_long")

    def test_brief_line_is_capped_at_300(self) -> None:
        line = charter.brief_line("word " * 100)
        assert line is not None
        self.assertEqual(len(line), 300)
        self.assertTrue(line.endswith("…"))
        self.assertEqual(charter.brief_line("short"), "short")
        self.assertIsNone(charter.brief_line(None))
        self.assertIsNone(charter.brief_line(""))


class AckTests(unittest.TestCase):
    def test_ack_records_the_current_seq_and_never_lowers_it(self) -> None:
        with TempState() as ts:
            first = charter.ack_charter(ts.layout, "alpha", "alpha-worker")
            self.assertEqual(first, {"member": "alpha-worker", "charter_seq_acked": 1, "charter_seq": 1, "team": "alpha", "stale": False})
            charter.set_charter(ts.layout, "alpha", HUMAN, "v2", None, [])
            member = roster.load_team(ts.team).find("alpha-worker")
            assert member is not None
            self.assertTrue(charter.charter_stale(member, charter.get_charter(ts.layout, "alpha")))
            lowered = charter.ack_charter(ts.layout, "alpha", "alpha-worker", seq=0)
            self.assertEqual(lowered["charter_seq_acked"], 1)
            self.assertTrue(lowered["stale"])
            explicit = charter.ack_charter(ts.layout, "alpha", "alpha-worker", seq=2)
            self.assertEqual(explicit["charter_seq_acked"], 2)
            self.assertFalse(explicit["stale"])
            with self.assertRaises(HerdrTeamError) as ctx:
                charter.ack_charter(ts.layout, "alpha", "nobody")
            self.assertEqual(ctx.exception.code, "member_not_found")

    def test_stale_rules(self) -> None:
        member = roster.Member("alpha-x", "x", "codex", "t")
        self.assertFalse(charter.charter_stale(member, None))
        self.assertTrue(charter.charter_stale(member, charter.Charter(1, "t")))
        member.charter_seq_acked = 1
        self.assertFalse(charter.charter_stale(member, charter.Charter(1, "t")))
        self.assertTrue(charter.charter_stale(member, charter.Charter(2, "t")))
        self.assertFalse(charter.charter_stale(roster.Member("human", "operator", "human", None), charter.Charter(9, "t")))

    def test_ack_without_a_charter(self) -> None:
        with TempState(write_team=False) as ts:
            roster.create_team(ts.layout, "alpha")
            roster.Roster(ts.layout, "alpha").add_member(roster.Member("alpha-x", "x", "codex", "term_x"))
            result = charter.ack_charter(ts.layout, "alpha", "alpha-x")
            self.assertIsNone(result["charter_seq"])
            self.assertIsNone(result["charter_seq_acked"])
            self.assertFalse(result["stale"])


class RecordBuilderTests(unittest.TestCase):
    def test_charter_updated_fields(self) -> None:
        c = charter.Charter(4, "goal", refs=["charter.md"], updated_at="t")
        fields = charter.charter_updated_record_fields(c, urgent=True)
        self.assertEqual(fields, {"urgent": True, "refs": ["charter.md"], "charter": c.to_json()})
        record = roster.system_record("charter_updated", c.text, to=["all"], extra=fields)
        self.assertEqual(record["event"], "charter_updated")
        self.assertEqual(record["charter"]["seq"], 4)
        self.assertTrue(record["urgent"])

    def test_api_is_never_needed(self) -> None:
        """Charter writes are file-only; no socket call is made even when an api object exists."""
        with TempState() as ts:
            api = FakeApi()
            charter.set_charter(ts.layout, "alpha", HUMAN, "offline", None, [])
            self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()

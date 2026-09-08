"""`orient` (0.11.0): one command that hands a member the team back.

Every test here fails against 0.10.0: there was no `orient`, the briefing named
four commands instead of one, a compacted Codex or OpenCode member was never
re-briefed at all, and the re-brief-once allowance was spent for the life of the
daemon rather than per briefing.
"""
from __future__ import annotations

import json
import os
import unittest

from herdr_team import charter as _charter
from herdr_team import cmd_hooks, cmd_misc, nudge, roster, store
from herdr_team import daemon as D
from herdr_team.errors import HerdrTeamError
from support import FakeApi, TempState
from test_cmd_board import json_out, run_cli, write_live_daemon
from test_cmd_roster import live_api
from test_daemon import make_daemon

MEMBER = "alpha-reviewer"   # codex in the fixture: a kind with no hooks
PEER = "alpha-worker"       # claude


def human():
    from test_instructions_doc import human as _human

    return _human()


class OrientTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def orient(self, pane="w2:p1", *extra):
        return json_out(run_cli(["--json", "orient"] + list(extra), self.ts.env_with(HERDR_PANE_ID=pane), live_api()))

    def blob(self, name=MEMBER):
        doc = store.read_json(self.ts.team.team_json)
        member = [m for m in doc["members"] if m["name"] == name][0]
        return cmd_hooks.brief_context(self.ts.team, "alpha", dict(member))

    def test_it_is_the_same_text_the_claude_hook_injects(self):
        # The whole reason this calls the hook's builder instead of composing
        # its own: two renderings of the same facts would drift, and the one
        # that drifted would be the one nobody reads until an agent is lost.
        code, out, err = self.orient()
        self.assertEqual(code, 0, err)
        self.assertEqual(out["text"], self.blob())

    def test_it_names_who_you_are_and_what_the_team_is(self):
        _charter.set_charter(self.ts.layout, "alpha", human(), "Find the session bug", None, None)
        code, out, err = self.orient()
        self.assertEqual(code, 0, err)
        text = out["text"]
        self.assertIn('you are "{}"'.format(MEMBER), text)
        self.assertIn("reviewer", text)
        self.assertIn('in team "alpha"', text)
        self.assertIn("Find the session bug", text)
        self.assertIn("teammates:", text)
        self.assertIn(PEER, text)
        self.assertIn("unread board posts for you:", text)
        self.assertEqual((out["member"], out["team"]), (MEMBER, "alpha"))

    def test_it_carries_the_documents_that_carry_authority(self):
        _charter.set_rules(self.ts.layout, "alpha", human(), "Never file without the control run.")
        _charter.set_instructions(self.ts.layout, "alpha", human(), MEMBER, "## Mission\n\nOwn the parser.", None)
        text = self.orient()[1]["text"]
        self.assertIn("your instructions (operator authority)", text)
        self.assertIn("Own the parser.", text)
        self.assertIn("team rules (operator authority)", text)
        self.assertIn("Never file without the control run.", text)

    def test_findings_are_a_count_and_a_command_never_the_text(self):
        self.assertNotIn("finding", self.orient()[1]["text"])
        _charter.add_finding(self.ts.layout, "alpha", human(), "the secret detail nobody should re-read")
        text = self.orient()[1]["text"]
        self.assertIn("1 team finding recorded by your teammates: herdr-synapse knowledge", text)
        self.assertNotIn("the secret detail", text, "findings are peer notes; they are pointed at, never inlined")
        for _ in range(4):
            _charter.add_finding(self.ts.layout, "alpha", human(), "another")
        self.assertIn("5 team findings recorded", self.orient()[1]["text"])

    def test_a_team_with_no_documents_still_orients_rather_than_failing(self):
        # the live team this was built against had no rules and no instructions
        # on any member; the command has to be useful anyway
        text = self.orient()[1]["text"]
        self.assertIn(MEMBER, text)
        self.assertIn("teammates:", text)
        self.assertNotIn("your instructions", text)
        self.assertNotIn("team rules", text)
        self.assertTrue(text.strip())

    def test_it_names_the_team_folder_when_there_is_one(self):
        import tempfile

        project = tempfile.mkdtemp(prefix="ht-proj-")
        self.addCleanup(lambda: __import__("shutil").rmtree(project, ignore_errors=True))

        def apply(doc: roster.Team) -> None:
            doc.config["project_dir"] = project

        roster.update_team(self.ts.team, apply)
        text = self.orient()[1]["text"]
        self.assertIn("team folder:", text)
        self.assertIn(os.path.join(project, ".herdr-synapse", "alpha", "knowledge.md"), text)
        self.assertIn("{}.md".format(MEMBER), text)

    def test_outside_a_member_pane_it_refuses_and_member_is_operator_only(self):
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "orient"], self.ts.env, FakeApi()))
        self.assertEqual((code, err["code"]), (3, "not_a_member"))
        # a member asking about a peer reads that peer's instructions: refused
        code, _out, err = json_out(run_cli(["--json", "orient", "--member", PEER],
                                           self.ts.env_with(HERDR_PANE_ID="w2:p1"), live_api()))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        # asking about yourself by name is fine
        self.assertEqual(self.orient("w2:p1", "--member", MEMBER)[0], 0)

    def test_it_works_for_a_kind_with_no_hooks_and_needs_no_daemon(self):
        doc = store.read_json(self.ts.team.team_json)
        self.assertEqual([m["kind"] for m in doc["members"] if m["name"] == MEMBER], ["codex"])
        self.assertFalse(self.ts.session.daemon_json.exists(), "no notifier in this rig")
        self.assertEqual(self.orient()[0], 0)


class BriefingPointsAtOrientTests(unittest.TestCase):
    def test_the_tail_names_one_command_and_buys_back_room(self):
        line = nudge.briefing_lines("w", "worker", "alpha", "ship it", [], None, "herdr-synapse")[0]
        self.assertIn("herdr-synapse orient", line)
        for gone in ("then charter, instructions", "board --new, ack, then continue"):
            self.assertNotIn(gone, line)

    def test_the_room_it_buys_goes_to_the_charter(self):
        """The worst case always lands at exactly 400: the ladder trims the
        headline to whatever is left. So the 19 characters do not show up as
        slack, they show up as charter text a long-named member gets to read."""
        import re

        from herdr_team.paths import MAX_MEMBER_CHARS, MAX_ROLE_CHARS

        line = nudge.briefing_lines("a" * MAX_MEMBER_CHARS, "r" * MAX_ROLE_CHARS, "team",
                                    "H" * 120, [], None, "herdr-synapse")[0]
        self.assertLessEqual(len(line), nudge.MAX_BRIEFING_CHARS)
        kept = re.search(r'": (H*)', line)
        self.assertIsNotNone(kept)
        self.assertEqual(len(kept.group(1)), 51, "32 before; the extra 19 chars are charter, not slack")


class RebriefAfterCompactionTests(unittest.TestCase):
    """The kinds with no hooks are the ones that were never re-briefed."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]
        self.d._apply_changes(self.team, [(MEMBER, {"briefed_at": roster.now_iso()})])

    def jobs(self):
        return [store.read_json(p) for p in sorted(self.ts.team.jobs_dir.glob("*.json"))]

    def member(self):
        return self.team.member(MEMBER)

    def test_a_detected_compaction_queues_a_briefing(self):
        self.assertIsNotNone(self.member()["briefed_at"])
        self.d._note_compacted(self.team, MEMBER, "20,000 -> 2,000 tokens")
        self.assertIsNone(self.member()["briefed_at"], "it must look unbriefed again")
        briefs = [j for j in self.jobs() if j.get("kind") == "brief" and j.get("member") == MEMBER]
        self.assertEqual(len(briefs), 1)

    def test_it_does_not_queue_a_second_when_one_is_pending(self):
        self.d._apply_changes(self.team, [(MEMBER, {"briefed_at": None})])
        self.d._note_compacted(self.team, MEMBER, "drop")
        self.assertEqual([j for j in self.jobs() if j.get("kind") == "brief"], [])

    def test_the_rebrief_allowance_is_per_briefing_not_per_daemon(self):
        # Nothing ever put this back, so the second clear or compaction of a
        # member's life got one attempt at an unacknowledged briefing and then
        # gave up for as long as the notifier lived.
        rt = self.team.rt(MEMBER)
        rt.rebriefed = True
        pending = D.Pending(first_ms=0.0, kind="brief", lines=["x"], force=True, seqs=[])
        self.d._apply_result(self.team, self.member(), pending, D.RESULT_LANDED_WORKING, {}, self.clock() * 1000)
        self.assertFalse(rt.rebriefed)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def warnings(self):
        return cmd_misc.teams_with_nothing_to_restore(self.ts.layout)

    def test_it_says_when_orient_would_hand_back_almost_nothing(self):
        lines = " | ".join(self.warnings())
        self.assertIn("has no rules", lines)
        self.assertIn("2 of 2 members have empty instructions", lines)

    def test_it_goes_quiet_once_they_are_written(self):
        _charter.set_rules(self.ts.layout, "alpha", human(), "the rules")
        for name in (MEMBER, PEER):
            _charter.set_instructions(self.ts.layout, "alpha", human(), name, "## Mission\n\nown it", None)
        self.assertEqual(self.warnings(), [])


if __name__ == "__main__":
    unittest.main()

"""The facts half of team inheritance (0.23, work items A and C): ``facts.md`` and what a new team
reads back from it.

Two seams live here: ``mirror_rows`` (what the project mirror writes) and ``parse_mirror`` plus
``add_inherited`` (what ``knowledge import`` reads back and records). The round trip is the point:
a mirror that cannot be read back is not an inheritance channel.
"""
from __future__ import annotations

import unittest

from support import FAKE_AGENTS, TempState
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import facts as F
from herdr_team import workdir as W

WORKER = "alpha-worker"
REVIEWER = "alpha-reviewer"


class Rig(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api(list(FAKE_AGENTS))

    def cli(self, argv, pane=None):
        code, payload, err = json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **({"HERDR_PANE_ID": pane} if pane else {})), self.api))
        self.assertEqual(code, 0, err)
        return payload

    def worker(self, *argv):
        return self.cli(list(argv), pane="w2:p2")

    def reviewer(self, *argv):
        return self.cli(list(argv), pane="w2:p1")

    def two_facts(self):
        self.worker("fact", "add", "Enterprise pricing is not public", "--about", "Competitor X",
                    "--attribute", "enterprise price", "--source", "https://example.com/pricing@2026-09-20",
                    "--valid-from", "2026-01-01")
        self.reviewer("fact", "support", "F-1")
        self.worker("fact", "add", "The gateway drops idle sockets after 30s")

    def body(self, limit=0):
        """The real ``facts.md`` body: the rows this module provides, through the mirror that writes them."""
        return W.facts_body("alpha", F.mirror_rows(self.ts.team, limit))


class Mirror(Rig):
    def test_a_row_carries_the_provenance_the_findings_line_loses(self):
        self.two_facts()
        [first, second] = F.mirror_rows(self.ts.team)
        self.assertEqual((first["id"], first["about"], first["attribute"]), ("F-1", "Competitor X", "enterprise price"))
        self.assertEqual(first["members"], [WORKER, REVIEWER])
        self.assertEqual(first["sources"][0]["url"], "https://example.com/pricing")
        self.assertEqual(second["id"], "F-2")
        text = self.body()
        for needle in ("- **F-1** Competitor X / enterprise price — Enterprise pricing is not public",
                       "confidence 2 members, 1 source", "supported by " + REVIEWER,
                       "sources: https://example.com/pricing", "valid 2026-01-01"):
            self.assertIn(needle, text, needle)
        self.assertEqual([row["id"] for row in F.parse_mirror(text)], ["F-1", "F-2"], "what the mirror writes reads back")

    def test_the_body_is_a_pure_function_of_the_log(self):
        self.two_facts()
        self.assertEqual(self.body(), self.body(), "nothing in a facts.md may come from the clock")

    def test_only_current_facts_are_mirrored_and_limit_keeps_the_newest(self):
        self.two_facts()
        self.worker("fact", "retire", "F-2", "the sockets stay open now")
        self.assertEqual([row["id"] for row in F.mirror_rows(self.ts.team)], ["F-1"])
        self.worker("fact", "add", "Signups doubled in October")
        self.assertEqual([row["id"] for row in F.mirror_rows(self.ts.team, limit=1)], ["F-3"])

    def test_a_dispute_is_visible_in_the_mirror(self):
        self.worker("fact", "add", "Launch is October 3", "--about", "Launch", "--attribute", "date")
        self.reviewer("fact", "add", "Launch is November 7", "--about", "Launch", "--attribute", "date")
        text = self.body()
        self.assertIn("status disputed", text)
        self.assertIn("disputed D-1", text)

    def test_a_statement_full_of_punctuation_still_round_trips(self):
        tricky = "costs rise 10% · maybe `more` .. and -- who knows: says who?"
        self.worker("fact", "add", tricky, "--about", "Pricing · odd", "--attribute", "says: it")
        [row] = F.parse_mirror(self.body())
        self.assertEqual(row["statement"], tricky)
        self.assertEqual((row["about"], row["attribute"]), ("Pricing · odd", "says: it"),
                         "the heading's own separator is escaped inside a subject, so a subject never comes back halved")
        self.assertNotIn("`more`", self.body(), "backticks are escaped so a fenced reader stays inert")

    def test_what_is_not_a_fact_bullet_is_skipped(self):
        self.two_facts()
        noise = "Some prose a human wrote.\n\n- a loose bullet\n" + self.body() + "\n- **F-9** _no statement_\n  nobody (member) \u00b7 2026-01-01T00:00:00.000Z\n"
        self.assertEqual([row["id"] for row in F.parse_mirror(noise)], ["F-1", "F-2"])
        self.assertEqual(F.parse_mirror(""), [])
        self.assertEqual(F.parse_mirror(None), [])


class Inheritance(Rig):
    def rows_from_source(self):
        """Two facts as another team's facts.md would carry them."""
        self.two_facts()
        rows = F.parse_mirror(self.body())
        return rows

    def test_a_fact_arrives_attributed_to_the_team_that_believed_it(self):
        rows = self.rows_from_source()
        fresh = TempState()
        self.addCleanup(fresh.cleanup)
        ids = F.add_inherited(fresh.team, rows, "beta", "/projects/thing/.herdr-synapse/beta/facts.md")
        self.assertEqual(ids, ["F-1", "F-2"])
        state = F.load(fresh.team)
        first = state.facts["F-1"]
        self.assertEqual((first.author, first.author_kind), ("beta", F.INHERITED_KIND))
        self.assertEqual(first.statement, "Enterprise pricing is not public")
        self.assertEqual((first.about, first.attribute), ("Competitor X", "enterprise price"))
        self.assertEqual(first.valid_from[:10], "2026-01-01")
        self.assertEqual(first.members, ["beta"], "nobody who is here is counted behind it yet")
        provenance = [s for s in first.sources if s.get("kind") == F.INHERITED_SOURCE_KIND]
        self.assertEqual(len(provenance), 1)
        self.assertEqual((provenance[0]["team"], provenance[0]["fact"]), ("beta", "F-1"))
        self.assertTrue(provenance[0]["at"].startswith("2026-09") or provenance[0]["at"][:2] == "20")
        self.assertEqual([s["url"] for s in first.sources if s.get("kind") == "url"], ["https://example.com/pricing"])

    def test_supporting_an_inherited_fact_is_what_makes_it_the_team_s_own(self):
        self.two_facts()
        rows = F.parse_mirror(self.body())
        fresh = TempState()
        self.addCleanup(fresh.cleanup)
        F.add_inherited(fresh.team, rows, "beta", "beta/facts.md")
        fact = F.support(fresh.team, "F-1", WORKER, None, [])
        self.assertEqual(fact.members, ["beta", WORKER])

    def test_nothing_is_disputed_on_arrival(self):
        self.worker("fact", "add", "Launch is October 3", "--about", "Launch", "--attribute", "date")
        rows = F.parse_mirror(self.body())
        fresh = TempState()
        self.addCleanup(fresh.cleanup)
        # The new team already believes something else about the same subject: a dead team's
        # belief is not a live member disagreeing, so no dispute is opened.
        F.add(fresh.team, {"statement": "Launch is November 7", "about": "Launch", "attribute": "date", "by": WORKER}, F.MODE_DEBATE)
        F.add_inherited(fresh.team, rows, "beta", "beta/facts.md")
        state = F.load(fresh.team)
        self.assertEqual(state.open_disputes(), [])
        self.assertEqual(len(state.current()), 2)

    def test_importing_the_same_document_twice_adds_nothing(self):
        rows = self.rows_from_source()
        fresh = TempState()
        self.addCleanup(fresh.cleanup)
        self.assertEqual(len(F.add_inherited(fresh.team, rows, "beta", "beta/facts.md")), 2)
        self.assertEqual(F.add_inherited(fresh.team, rows, "beta", "beta/facts.md"), [])
        self.assertEqual(len(F.load(fresh.team).current()), 2)

    def test_a_retired_or_empty_row_is_skipped_not_recorded(self):
        fresh = TempState()
        self.addCleanup(fresh.cleanup)
        rows = [
            {"id": "F-1", "statement": "", "about": None},
            {"id": "F-2", "statement": "retired long ago", "retired_at": "2026-01-01T00:00:00.000Z"},
            {"id": "F-3", "statement": "superseded long ago", "superseded_by": "F-9"},
            {"id": "F-4", "statement": "the one good row"},
            "not a dict",
        ]
        self.assertEqual(F.add_inherited(fresh.team, rows, "beta", "beta/facts.md"), ["F-1"])
        self.assertEqual(F.load(fresh.team).facts["F-1"].statement, "the one good row")

    def test_a_dissolved_team_s_jsonl_rows_work_too(self):
        self.two_facts()
        rows = [fact.to_json() for fact in F.load(self.ts.team).current()]
        fresh = TempState()
        self.addCleanup(fresh.cleanup)
        self.assertEqual(len(F.add_inherited(fresh.team, rows, "beta", "/archive/beta-2026/facts.jsonl")), 2)
        state = F.load(fresh.team)
        self.assertEqual(state.facts["F-1"].author_kind, F.INHERITED_KIND)
        self.assertEqual(state.facts["F-1"].valid_from[:10], "2026-01-01")

    def test_nothing_to_inherit_is_not_an_error(self):
        fresh = TempState()
        self.addCleanup(fresh.cleanup)
        self.assertEqual(F.add_inherited(fresh.team, [], "beta", "beta/facts.md"), [])
        self.assertEqual(F.mirror_rows(fresh.team), [])

    def test_fact_show_names_where_an_inherited_fact_came_from(self):
        rows = self.rows_from_source()
        fresh = TempState()
        self.addCleanup(fresh.cleanup)
        F.add_inherited(fresh.team, rows, "beta", "beta/facts.md")
        code, out, err = run_cli(["fact", "show", "F-1"], env_no_daemon(fresh), self.api)
        self.assertEqual(code, 0, err)
        self.assertIn("inherited from team beta", out)
        self.assertIn("by beta", out)


if __name__ == "__main__":
    unittest.main()

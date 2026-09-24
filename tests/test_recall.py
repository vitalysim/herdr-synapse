"""recall (0.19): one ranked search over posts, facts, work items and artifacts."""
from __future__ import annotations

import os
import unittest

from support import FAKE_AGENTS, TempState
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import recall as R
from herdr_team import store

WORKER = "alpha-worker"


class Rig(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api(list(FAKE_AGENTS))
        self.project = self.ts.tmp / "project"
        self.project.mkdir()
        doc = store.read_json(self.ts.team.team_json)
        doc.setdefault("config", {})["project_dir"] = os.fspath(self.project)
        store.write_json(self.ts.team.team_json, doc)
        self.artifacts = self.project / ".herdr-synapse" / "alpha" / "artifacts"
        self.artifacts.mkdir(parents=True)

    def cli(self, argv, pane=None):
        overrides = {"HERDR_PANE_ID": pane} if pane else {}
        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **overrides), self.api))

    def ok(self, result):
        code, payload, err = result
        self.assertEqual(code, 0, err)
        return payload

    def recall(self, *argv):
        return self.ok(self.cli(["recall"] + list(argv)))["hits"]


class Recall(Rig):
    def test_one_query_finds_posts_facts_work_and_files(self):
        self.ok(self.cli(["post", "the enterprise pricing is hidden behind a sales call"], pane="w2:p2"))
        self.ok(self.cli(["fact", "add", "Enterprise pricing is not public", "--about", "Competitor X", "--attribute", "enterprise price"], pane="w2:p2"))
        self.ok(self.cli(["work", "add", "Find enterprise pricing for Competitor X", "--to", WORKER, "--quick"]))
        (self.artifacts / "pricing-map.md").write_text("# Pricing map\n\nCompetitor X enterprise pricing: contact sales.\n", encoding="utf-8")
        hits = self.recall("enterprise pricing")
        kinds = {h["kind"] for h in hits}
        self.assertEqual(kinds, {"post", "fact", "work", "file"})
        fact_hit = next(h for h in hits if h["kind"] == "fact")
        self.assertEqual((fact_hit["ref"], fact_hit["info"]["status"]), ("F-1", "current"))
        self.assertIn("»", fact_hit["snippet"])
        file_hit = next(h for h in hits if h["kind"] == "file")
        self.assertEqual(file_hit["ref"], "pricing-map.md:1")
        only_facts = self.recall("enterprise pricing", "--kind", "fact")
        self.assertEqual([h["kind"] for h in only_facts], ["fact"])

    def test_a_current_well_supported_fact_outranks_a_retired_one(self):
        self.ok(self.cli(["fact", "add", "Launch date is October 3", "--about", "Launch", "--attribute", "date"], pane="w2:p2"))
        self.ok(self.cli(["fact", "add", "Launch date is November 7", "--about", "Launch", "--attribute", "date"], pane="w2:p2"))
        self.ok(self.cli(["fact", "support", "F-2", "--source", "https://example.com/announcement"], pane="w2:p1"))
        hits = self.recall("launch date", "--kind", "fact")
        self.assertEqual([h["ref"] for h in hits], ["F-2", "F-1"])
        self.assertEqual(hits[1]["info"]["status"], "superseded")

    def test_as_of_answers_what_the_team_believed_then(self):
        self.ok(self.cli(["fact", "add", "Launch date is October 3", "--about", "Launch", "--attribute", "date"], pane="w2:p2"))
        self.ok(self.cli(["fact", "add", "Launch date is November 7", "--about", "Launch", "--attribute", "date"], pane="w2:p2"))
        self.assertEqual([h["ref"] for h in self.recall("launch date", "--kind", "fact", "--as-of", "2099-01-01")], ["F-2"])
        self.assertEqual(self.recall("launch date", "--kind", "fact", "--as-of", "2000-01-01"), [])

    def test_about_ranks_the_subject_first(self):
        self.ok(self.cli(["fact", "add", "pricing starts at $10", "--about", "Acme"], pane="w2:p2"))
        self.ok(self.cli(["fact", "add", "pricing starts at $12", "--about", "Globex"], pane="w2:p2"))
        self.assertEqual(self.recall("pricing starts", "--kind", "fact", "--about", "Globex")[0]["ref"], "F-2")

    def test_the_index_follows_retractions_edits_and_a_purge(self):
        seq = self.ok(self.cli(["post", "the quarterly survey closes Friday"], pane="w2:p2"))["seq"]
        self.assertEqual(len(self.recall("quarterly survey", "--kind", "post")), 1)
        self.ok(self.cli(["retract", str(seq)], pane="w2:p2"))
        self.assertEqual(self.recall("quarterly survey", "--kind", "post"), [])
        (self.artifacts / "notes.txt").write_text("alpha beta gamma\n", encoding="utf-8")
        self.assertEqual(len(self.recall("gamma", "--kind", "file")), 1)
        (self.artifacts / "notes.txt").unlink()
        self.assertEqual(self.recall("gamma", "--kind", "file"), [])
        self.ok(self.cli(["post", "persistent phrase zebra"], pane="w2:p2"))
        self.assertEqual(len(self.recall("zebra")), 1)
        store.BoardStore(self.ts.team).clear("human", purge=True)
        self.assertEqual(self.recall("zebra", "--kind", "post"), [], "a purged board leaves nothing in the index")

    def test_odd_queries_never_break_the_search(self):
        self.ok(self.cli(["post", "C++ AND (templates) NOT \"quoted\" near: stuff"], pane="w2:p2"))
        for query in ('C++ AND (templates)', 'NOT "quoted', 'near: *', '"templates stuff"'):
            code, _p, err = self.cli(["recall", query])
            self.assertEqual(code, 0, (query, err))

    def test_file_snippets_are_redacted(self):
        (self.artifacts / "leak.md").write_text("config uses sk-ant-abcdefghijklmnopqrstuvwxyz0123 for the widget\n", encoding="utf-8")
        [hit] = self.recall("widget", "--kind", "file")
        self.assertIn("[redacted:", hit["snippet"])
        self.assertNotIn("sk-ant-abcdef", hit["snippet"])

    def test_the_index_is_a_cache(self):
        self.ok(self.cli(["post", "rebuildable cache check"], pane="w2:p2"))
        self.assertEqual(len(self.recall("rebuildable")), 1)
        R.index_path(self.ts.team).unlink()
        self.assertEqual(len(self.recall("rebuildable")), 1)
        self.assertEqual(os.stat(R.index_path(self.ts.team)).st_mode & 0o777, 0o600)


class ReviewFindings(Rig):
    def test_similar_file_names_never_evict_each_other(self):
        (self.artifacts / "notes-v1.md").write_text("alpha zebra\n", encoding="utf-8")
        self.assertEqual(len(self.recall("zebra", "--kind", "file")), 1)
        # ``_`` was a wildcard in the old pattern match: indexing notes_v1.md evicted notes-v1.md
        # (case collisions are covered by the exact comparison too, but macOS folds case on disk)
        (self.artifacts / "notes_v1.md").write_text("alpha zebra again\n", encoding="utf-8")
        refs = sorted(h["ref"] for h in self.recall("zebra", "--kind", "file"))
        self.assertEqual(refs, ["notes-v1.md:1", "notes_v1.md:1"])

    def test_a_symlinked_artifacts_folder_is_not_followed(self):
        import shutil

        outside = self.ts.tmp / "outside"
        outside.mkdir()
        (outside / "secret-plans.md").write_text("quarterly acquisition target\n", encoding="utf-8")
        shutil.rmtree(self.artifacts)
        os.symlink(outside, self.artifacts)
        self.assertEqual(self.recall("acquisition", "--kind", "file"), [])


if __name__ == "__main__":
    unittest.main()

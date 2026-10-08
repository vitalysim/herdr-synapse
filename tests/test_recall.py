"""recall (0.19): one ranked search over posts, facts, work items, artifacts and the canvas."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from support import FAKE_AGENTS, TempState, whiteboard_on
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import canvas as C
from herdr_team import canvas_render as CR
from herdr_team import features as F
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


class Canvas(Rig):
    """The canvas kind (0.23, work item E): a diagram is findable by what it says."""

    AUTHOR = C.CanvasAuthor(WORKER, "member", "cli", True, agent="claude")
    OPS = [
        {"op": "frame", "id": "flow", "title": "Login flow", "region": [0, 0, 900, 700],
         "intent": "show the link shortener flow while leaving room for teammates"},
        {"op": "shape", "kind": "box", "id": "gate", "text": "API gateway", "at": [40, 60], "intent": "the front door"},
        {"op": "shape", "kind": "box", "id": "store", "text": "sessions table", "at": [400, 60], "intent": "where tokens land"},
        {"op": "arrow", "id": "hop", "from": "gate", "to": "store", "label": "writes the refresh token", "intent": "token write"},
        {"op": "comment", "at": [60, 80], "text": "does this handle clock skew?", "intent": "ask the reviewer"},
        {"op": "card", "id": "limiter", "title": "Rate limiter", "body": "100 req/min per key", "at": [40, 900],
         "intent": "record the quota"},
        {"op": "table", "id": "plans", "title": "Plan limits", "at": [900, 900], "columns": ["plan", "burst"],
         "rows": [["free tier", "ten a second"]], "intent": "compare the plans"},
        {"op": "chart", "id": "signups", "title": "Signups", "caption": "weekly signups since the beta",
         "type": "line", "rows": [{"week": "2026-01-01", "n": 1}, {"week": "2026-01-08", "n": 2}],
         "x": "week", "y": "n", "at": [1800, 900], "intent": "show the trend"},
        {"op": "legend", "symbol": "dashed", "meaning": "planned, not built", "intent": "say what dashes mean"},
    ]

    def setUp(self):
        super().setUp()
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        patcher = mock.patch.object(CR, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def draw(self, ops=None):
        result = C.apply_ops(self.ts.layout, self.ts.team, list(ops or self.OPS), self.AUTHOR)
        self.assertEqual(result["refused"], [], result["refused"])
        return result

    def canvas_hits(self, query, *extra):
        return self.recall(query, "--kind", "canvas", *extra)

    def test_every_kind_of_text_on_the_board_is_findable(self):
        self.draw()
        for query, ref in [("link shortener flow", "E-1"), ("API gateway", "E-2"),
                           ("refresh token", "E-4"), ("clock skew", "C-1"),
                           ("100 req/min", "E-5"), ("free tier", "E-6"),
                           ("weekly signups beta", "E-7"), ("planned not built", "G-1")]:
            hits = self.canvas_hits(query)
            self.assertIn(ref, [hit["ref"] for hit in hits], "{!r} should find {}".format(query, ref))
            self.assertEqual(hits[0]["kind"], "canvas")

    def test_a_hit_says_which_element_and_how_to_see_it(self):
        self.draw()
        [hit] = self.canvas_hits("clock skew")
        self.assertEqual((hit["kind"], hit["ref"], hit["info"]["type"]), ("canvas", "C-1", "comment"))
        self.assertEqual(hit["author"], WORKER)
        code, out, err = run_cli(["recall", "clock skew"], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0, err)
        self.assertIn("[canvas C-1 · comment]", out)
        self.assertIn("canvas look --around C-1", out)

    def test_about_finds_the_marks_inside_that_frame(self):
        self.draw()
        refs = [hit["ref"] for hit in self.canvas_hits("gateway table", "--about", "Login flow")]
        self.assertTrue(refs, "the frame's children are about its title")
        self.assertIn("E-2", refs)

    def test_one_query_reaches_the_canvas_beside_everything_else(self):
        self.draw([{"op": "shape", "kind": "box", "id": "quota", "text": "quota ceiling", "at": [0, 0],
                    "intent": "the quota ceiling we agreed"}])
        self.ok(self.cli(["post", "the quota ceiling is per key"], pane="w2:p2"))
        self.ok(self.cli(["fact", "add", "The quota ceiling is 100 req/min", "--about", "Gateway",
                          "--attribute", "quota ceiling"], pane="w2:p2"))
        kinds = {hit["kind"] for hit in self.recall("quota ceiling")}
        self.assertEqual(kinds, {"post", "fact", "canvas"})

    def test_the_index_rebuilds_from_the_canvas_alone(self):
        self.draw()
        self.assertEqual(len(self.canvas_hits("API gateway")), 1)
        R.index_path(self.ts.team).unlink()
        self.assertEqual(len(self.canvas_hits("API gateway")), 1, "a deleted index costs a rebuild, nothing else")

    def test_a_cleared_or_deleted_mark_stops_being_findable(self):
        self.draw()
        self.assertEqual(len(self.canvas_hits("sessions")), 1)
        self.draw([{"op": "delete", "id": "E-3", "intent": "the table moved to the other diagram"}])
        self.assertEqual(self.canvas_hits("sessions"), [])
        self.assertEqual(len(self.canvas_hits("API gateway")), 1)
        C.clear(self.ts.layout, self.ts.team, "human")
        self.assertEqual(self.canvas_hits("API gateway"), [], "a cleared board leaves nothing in the index")

    def test_a_team_with_no_canvas_is_not_an_error(self):
        self.assertEqual(R.refresh(self.ts.team, None)["canvas"], 0)
        self.assertEqual(self.canvas_hits("anything at all"), [])
        self.draw()
        self.assertEqual(len(self.canvas_hits("API gateway")), 1)
        F.set_team(self.ts.team, enabled=False, by="human", via="cli")
        self.assertEqual(R.refresh(self.ts.team, None)["canvas"], 0)
        self.assertEqual(self.canvas_hits("API gateway"), [], "a canvas that is off keeps no rows")
        F.set_team(self.ts.team, enabled=True, by="human", via="cli")
        self.assertEqual(len(self.canvas_hits("API gateway")), 1, "turning it back on reindexes from the scene")

    def test_indexing_is_not_looking_at_the_board(self):
        """``look`` writes presence and a cursor; indexing must not, or a search would show up as
        a teammate reading over your shoulder."""
        self.draw()
        directory = F.whiteboard_dir(self.ts.team)

        def side_effects():
            out = {}
            for name in ("presence", "cursors"):
                for path in sorted((directory / name).glob("*")) if (directory / name).is_dir() else []:
                    out[name + "/" + path.name] = path.stat().st_mtime_ns
            return out

        before = side_effects()
        self.assertEqual(len(self.canvas_hits("API gateway")), 1)
        self.assertEqual(side_effects(), before, "a search left a reader's footprint on the canvas")

    def test_as_of_does_not_show_a_mark_drawn_later(self):
        self.draw()
        self.assertEqual(self.canvas_hits("API gateway", "--as-of", "2000-01-01"), [])
        self.assertTrue(self.canvas_hits("API gateway", "--as-of", "2099-01-01"))


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

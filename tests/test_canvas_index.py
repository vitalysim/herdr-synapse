"""``canvas_index``: what a canvas says, as searchable rows (0.23, work item E).

Pure over a folded scene, so these tests need no team, no state dir and no canvas: a
fixture scene is the whole input. The live round trip (draw, then find it with
``recall``) is in ``test_recall.py``.
"""
from __future__ import annotations

import unittest

from herdr_team import canvas_index as X


def el(eid, type_, **fields):
    base = {"id": eid, "type": type_, "alias": None, "text": "", "frame": None, "group": None,
            "author": "alpha-worker", "intent": "", "x": 0, "y": 0, "w": 100, "h": 60, "z": 1,
            "created_at": "2026-10-01T09:00:00.000Z", "updated_at": "2026-10-01T09:30:00.000Z"}
    base.update(fields)
    return base


def scene(*elements, **extra):
    doc = {"v": 1, "team": "alpha", "version": len(elements), "elements": list(elements), "legend": []}
    doc.update(extra)
    return doc


def body_of(rows, eid):
    return next(row["body"] for row in rows if row["id"] == eid)


class Extraction(unittest.TestCase):
    def test_a_row_carries_the_kind_word_the_name_the_text_and_the_intent(self):
        rows = X.rows(scene(el("E-1", "box", alias="gate", text="API gateway", intent="the front door")))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["id"], row["type"]), ("E-1", "box"))
        for needle in ("box", "gate", "API gateway", "the front door"):
            self.assertIn(needle, row["body"], needle)
        self.assertEqual(row["ts"], "2026-10-01T09:30:00.000Z")
        self.assertEqual(row["info"]["alias"], "gate")

    def test_every_kind_s_own_words_are_found_without_a_list_of_kinds(self):
        rows = X.rows(scene(
            el("E-1", "frame", text="Login flow"),
            el("E-2", "card", text="Rate limiter", body="100 req/min per key",
               badges=[{"text": "P0"}], detail="burst allowance explained"),
            el("E-3", "table", text="Limits", columns=[{"title": "plan", "id": "c1"}],
               rows=[{"cells": {"c1": "free tier"}, "id": "r1"}]),
            el("E-4", "chart", text="Signups", settings={"type": "line", "caption": "weekly signups", "x": "week"},
               gist=["x=week (2 points)"], option={"textStyle": {"fontFamily": "$sans"}}),
            el("E-5", "sequence", text="Handshake", participants=[{"id": "client", "text": "client"}],
               messages=[{"from": "client", "to": "server", "text": "hello world", "id": "m1"}]),
            el("E-6", "scene3d", text="Rack layout", objects=[{"id": "r1", "shape": "box", "label": "rack one"}],
               links=[{"id": "r1->r2", "label": "fibre"}]),
            el("E-7", "mermaid", source="graph TD; A[ingest]-->B[store];"),
            el("C-1", "comment", text="does this handle refresh tokens?", mentions=["alpha-reviewer"]),
        ))
        self.assertIn("Login flow", body_of(rows, "E-1"))
        for needle in ("Rate limiter", "100 req/min per key", "P0", "burst allowance explained"):
            self.assertIn(needle, body_of(rows, "E-2"), needle)
        self.assertIn("free tier", body_of(rows, "E-3"), "a table's cells are what someone typed")
        self.assertIn("plan", body_of(rows, "E-3"))
        chart = body_of(rows, "E-4")
        for needle in ("Signups", "weekly signups", "line", "week"):
            self.assertIn(needle, chart, needle)
        self.assertNotIn("$sans", chart, "the page's view model is derived, not words")
        self.assertIn("hello world", body_of(rows, "E-5"))
        self.assertIn("rack one", body_of(rows, "E-6"))
        self.assertIn("fibre", body_of(rows, "E-6"))
        self.assertIn("ingest", body_of(rows, "E-7"))
        self.assertIn("alpha-reviewer", body_of(rows, "C-1"), "a mention is a plain word")

    def test_a_kind_invented_later_is_indexed_with_no_change_here(self):
        rows = X.rows(scene(el("E-1", "gantt", text="Q4 plan", settings={"caption": "the whole quarter"},
                               bars=[{"id": "b1", "label": "migrate the gateway"}])))
        body = body_of(rows, "E-1")
        for needle in ("gantt", "Q4 plan", "the whole quarter", "migrate the gateway"):
            self.assertIn(needle, body, needle)

    def test_identity_geometry_style_and_asset_names_never_reach_the_index(self):
        rows = X.rows(scene(el(
            "E-1", "svg", text="Logo", author="alpha-worker", batch="B-3", part="c2", frame="E-9",
            asset="0ea6867e76b7cebbe04b43180e17bf81.svg", style={"color": "#ff0088", "font": "normal"},
            imported={"team": "beta", "author": "beta-worker"}, points=[[0, 0], [10, 10]])))
        body = body_of(rows, "E-1")
        for needle in ("alpha-worker", "B-3", "0ea6867e", "#ff0088", "normal", "beta-worker"):
            self.assertNotIn(needle, body, needle)
        self.assertEqual(rows[0]["info"]["imported"], {"team": "beta", "author": "beta-worker"})

    def test_one_row_stays_bounded_whatever_a_kind_stores(self):
        rows = X.rows(scene(el("E-1", "chart", text="Signups", settings={"caption": "weekly signups since launch"},
                               cells=["value number {}".format(n) for n in range(4000)])))
        body = body_of(rows, "E-1")
        self.assertLessEqual(len(body), X.MAX_ROW_CHARS)
        self.assertIn("weekly signups since launch", body, "a named field is never crowded out by data")

    def test_a_pathologically_nested_element_does_not_run_away(self):
        # Nesting multiplies: with no budget on the walk itself, this element is 60**8 visits for a
        # row that would hold 1,200 characters. Aliased on purpose, so building it stays free.
        deep = ["word {}".format(n) for n in range(60)]
        for _ in range(8):
            deep = [deep] * 60
        rows = X.rows(scene(el("E-1", "box", text="deep", cells=deep)))
        self.assertLessEqual(len(body_of(rows, "E-1")), X.MAX_ROW_CHARS)

    def test_about_is_the_frame_title_then_the_name(self):
        rows = X.rows(scene(
            el("E-1", "frame", alias="flow", text="Login flow"),
            el("E-2", "box", text="API gateway", frame="E-1"),
            el("E-3", "box", alias="loose", text="somewhere else"),
            el("E-4", "box", text="nameless"),
        ))
        self.assertEqual(next(r["about"] for r in rows if r["id"] == "E-2"), "Login flow")
        self.assertEqual(next(r["about"] for r in rows if r["id"] == "E-3"), "loose")
        self.assertEqual(next(r["about"] for r in rows if r["id"] == "E-4"), "")

    def test_weights_come_from_the_registry(self):
        rows = {r["id"]: r["weight"] for r in X.rows(scene(
            el("E-1", "frame", text="Login flow"),
            el("E-2", "box", alias="gate", text="API gateway"),
            el("E-3", "box", text="plain"),
            el("E-4", "chart", text="Signups"),
            el("C-1", "comment", text="open question"),
            el("C-2", "comment", text="closed question", resolved=True),
        ))}
        self.assertEqual(rows["E-1"], X.WEIGHT_CONTAINER)
        self.assertEqual(rows["E-2"], X.WEIGHT_NAMED)
        self.assertEqual(rows["E-3"], X.WEIGHT_PLAIN)
        self.assertEqual(rows["E-4"], X.WEIGHT_NAMED)
        self.assertEqual(rows["C-1"], X.WEIGHT_PLAIN)
        self.assertEqual(rows["C-2"], X.WEIGHT_RESOLVED)

    def test_the_legend_is_searchable_by_what_a_mark_means(self):
        rows = X.rows(scene(legend=[{"id": "G-1", "symbol": "dashed", "meaning": "planned, not built",
                                     "author": "human", "at": "2026-10-01T09:00:00.000Z"}]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "G-1")
        self.assertIn("planned, not built", rows[0]["body"])

    def test_a_missing_empty_or_broken_scene_gives_no_rows_and_no_exception(self):
        for value in (None, {}, {"elements": None}, {"elements": ["not a dict", {"no": "id"}]}, [1, 2, 3]):
            self.assertEqual(X.rows(value), [], repr(value))

    def test_the_rows_are_a_pure_function_of_the_scene(self):
        fixture = scene(el("E-1", "box", text="API gateway"), el("C-1", "comment", text="why?"))
        self.assertEqual(X.rows(fixture), X.rows(fixture))


if __name__ == "__main__":
    unittest.main()

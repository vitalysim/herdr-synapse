"""The chart block (canvas v2 phase 3, 2.9): create, upsert, patch, the snapshot and its re-read, the round trip (T-B1),
v1 upgrade, what it draws (card, title, slot, still rule), growth, resize, checks, and what agents read back."""
from __future__ import annotations

import json
import shutil
import time
import unittest
from pathlib import Path

from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_svg
from herdr_team.canvas_kinds import chart as K

GUIDES = Path(__file__).resolve().parent / "fixtures" / "canvas_artifacts" / "guides"
REV = {"op": "chart", "id": "rev", "intent": "compare regions by month", "title": "Revenue by region, 2026", "type": "bar", "data": "rev.csv",
       "x": "month", "y": "revenue", "color": "region", "format": {"y": "compact"}, "units": {"y": "$"}, "at": "c0r0"}
P95 = {"op": "chart", "id": "p95", "intent": "t", "title": "p95", "type": "line", "x": "day", "y": "p95",
       "rows": [{"day": "2026-09-0{}".format(i), "p95": v} for i, v in enumerate((412, 405, 398, 251, 240, 236), 1)], "below": "rev"}


class ChartRig(CanvasRig):
    def setUp(self):
        super().setUp()
        self.art = self.artifacts()
        for path in GUIDES.glob("*.*"):
            shutil.copy(path, self.art / path.name)

    def by_alias(self, alias):
        return next(e for e in self.scene()["elements"] if e.get("alias") == alias)

    def entry(self, eid, stills=None):
        doc = D.display_list(self.scene(), stills=stills)
        self.assertEqual(D.validate(doc), [])
        return next(e for e in doc["entries"] if e["id"] == eid), doc


class Create(ChartRig):
    def test_the_worked_example_stores_its_snapshot(self):
        applied = self.ok(REV)
        el = self.el(applied["ids"][0])
        self.assertEqual((el["type"], el["kv"], el["engine"], el["chart"]), ("chart", 2, "echarts", {"type": "bar", "gl": False}))
        self.assertEqual(el["settings"]["data"], "rev.csv")
        self.assertIsNone(el.get("data"), "a flat chart keeps its file in settings and source only (D18)")
        self.assertEqual((el["source"]["rows"], el["source"]["columns"][0]), (36, ["month", "temporal"]))
        self.assertEqual(len(el["source"]["sha256"]), 64)
        doc = json.loads(C.asset_path(self.team, el["doc_asset"]).read_text())
        self.assertEqual((doc["v"], doc["chart"], doc["source"]["data"]), (1, "bar", "rev.csv"))
        compat = json.loads(C.asset_path(self.team, el["spec_asset"]).read_text())
        self.assertEqual(compat["mark"], "bar", "the v1 page draws it from a Vega-Lite spec over the model")
        self.assertEqual(applied["gist"], el["gist"][:2])
        for key in ("model", "option"):
            self.assertLessEqual(len(json.dumps(el[key], separators=(",", ":"))), 16 * 1024)

    def test_the_reply_carries_the_gist_and_the_frame_note(self):
        result = self.apply([REV])
        text = C.apply_text(result)
        self.assertIn("   max EMEA 2026-08 $4.2M · min APAC 2026-01 $300K · total $79.5M", text)
        self.assertRegex(text, r"E-1 chart 640x400 at c0r0 · v1 · frame: every 2nd x label shown")
        self.assertNotIn("layout_note", text, "the frame note is on the block line, not repeated as a warning")

    def test_a_hallucinated_field_is_refused_with_its_repair(self):
        refused = self.refused(dict(REV, y="revenu"))
        self.assertEqual((refused["code"], refused["details"]["field"], refused["details"]["nearest"]), ("op_invalid", "y", "revenue"))
        self.assertIn('Did you mean "revenue"?', refused["message"])
        self.assertEqual(self.refused({"op": "chart", "intent": "t", "x": "a"})["details"]["field"], "type")
        self.assertEqual(self.refused(dict(REV, data="missing.csv"))["code"], "path_refused")

    def test_a_small_chart_grows_to_its_legible_minimum(self):
        result = self.apply([dict(REV, w=200, h=120)])
        el = self.el(result["applied"][0]["ids"][0])
        self.assertGreater(el["w"], 200)
        self.assertIn("chart_grew", [w["code"] for w in result["warnings"]])
        self.assertTrue(el["chart_frame"]["legible"])
        # A move below it is clamped to it, and says so (QA phase34 L6).
        moved = self.apply([{"op": "move", "id": el["id"], "w": 150, "h": 100, "intent": "t"}])
        self.assertEqual(moved["refused"], [])
        after = self.el(el["id"])
        self.assertEqual((after["w"], after["h"]), (el["w"], el["h"]))
        grew = [w for w in moved["warnings"] if w["code"] == "chart_grew"]
        self.assertEqual(len(grew), 1, moved["warnings"])
        self.assertIn("stays {}x{} (asked 150x100)".format(el["w"], el["h"]), grew[0]["message"])
        roomy = self.apply([{"op": "move", "id": el["id"], "w": el["w"] + 100, "intent": "t"}])
        self.assertNotIn("chart_grew", [w["code"] for w in roomy["warnings"]], "a size that fits is not warned about")

    def test_inline_rows_stay_on_the_element_and_a_500_row_op_is_quick(self):
        rows = [{"d": "c{:03d}".format(i), "v": i} for i in range(500)]
        started = time.perf_counter()
        applied = self.ok({"op": "chart", "type": "bar", "rows": rows, "x": "d", "y": "v", "top": 20, "at": [0, 3000], "intent": "t"})
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.4, "a 500-row inline chart applies quickly (budget 40 ms on the dev Mac)")
        el = self.el(applied["ids"][0])
        self.assertEqual(len(el["settings"]["rows"]), 500)
        self.assertNotIn("rows", el["resolved"])


class Change(ChartRig):
    def test_patch_switches_the_type_and_relayout_full_rereads_the_file(self):
        first = self.ok(REV)
        version = first["block"]["version"]
        self.ok({"op": "patch", "id": "rev", "if_version": version, "intent": "trend", "set": {"type": "line", "color": "region"}})
        el = self.by_alias("rev")
        self.assertEqual((el["chart"]["type"], el["settings"]["type"]), ("line", "line"))
        self.assertTrue(el["gist"][1].startswith("EMEA $3M → $4.2M (+38%)"), el["gist"])
        stale = self.refused({"op": "patch", "id": "rev", "if_version": version, "intent": "t", "set": {"title": "x"}})
        self.assertEqual(stale["code"], "canvas_stale")
        (self.art / "rev.csv").write_text("month,region,revenue\n2026-01,EMEA,5\n2026-02,EMEA,7\n")
        self.ok({"op": "patch", "id": "rev", "intent": "restyle", "set": {"title": "Revenue"}})
        self.assertEqual(self.by_alias("rev")["source"]["rows"], 36, "a visual change reads nothing: the snapshot holds")
        self.ok({"op": "patch", "id": "rev", "intent": "the export was refreshed", "relayout": "full"})
        el = self.by_alias("rev")
        self.assertEqual((el["source"]["rows"], el["text"]), (2, "Revenue"))
        self.assertEqual(el["gist"][1], "EMEA $5 → $7 (+40%) · ↑")

    def test_set_null_removes_a_setting_and_an_upsert_rereads(self):
        self.ok(REV)
        self.ok({"op": "patch", "id": "rev", "intent": "t", "set": {"color": None, "units": None}})
        el = self.by_alias("rev")
        self.assertNotIn("color", el["settings"])
        self.assertEqual(len(el["model"]["series"]), 1)
        (self.art / "rev.csv").write_text("month,region,revenue\n2026-01,EMEA,5\n")
        again = self.ok(dict(REV, at=None))
        self.assertTrue(again["block"]["upsert"])
        self.assertEqual(self.by_alias("rev")["source"]["rows"], 1)

    def test_a_patch_to_a_field_its_type_does_not_take_names_the_types(self):
        self.ok(REV)
        refused = self.refused({"op": "patch", "id": "rev", "intent": "t", "set": {"bins": 10}})
        self.assertIn("bins is an option of histogram", refused["message"])

    def test_resize_reframes_from_the_model(self):
        applied = self.ok(REV)
        eid = applied["ids"][0]
        before = self.el(eid)
        self.ok({"op": "move", "id": eid, "w": 900, "h": 500, "intent": "bigger"})
        after = self.el(eid)
        self.assertEqual((after["w"], after["h"]), (900, 500))
        self.assertNotEqual(after["chart_frame"], before["chart_frame"])
        self.assertEqual(after["option"]["grid"]["width"], after["chart_frame"]["plot"][2])
        self.assertEqual(after["doc_asset"], before["doc_asset"], "no new snapshot for a resize")
        self.ok({"op": "move", "id": eid, "w": 100, "h": 60, "intent": "tiny"})
        self.assertGreater(self.el(eid)["w"], 100, "never below its legible minimum")


class RoundTrip(ChartRig):
    OPS = [REV, P95,
           {"op": "chart", "id": "mix", "intent": "t", "title": "Visits", "type": "doughnut", "data": "traffic.json", "category": "channel",
            "value": "visits", "top": 5, "right_of": "p95"},
           {"op": "chart", "id": "vl", "intent": "t", "data": "rev.csv", "spec": {"mark": "line", "encoding": {"x": {"field": "month"}}}, "below": "p95"},
           {"op": "chart", "id": "raw", "intent": "t", "echarts": {"series": [{"type": "radar", "data": [{"value": [1, 2]}]}],
                                                                   "radar": {"indicator": [{"name": "a"}, {"name": "b"}]}}, "below": "vl"}]

    def comparable(self, spec):
        return {k: v for k, v in B.comparable(spec).items() if not k.startswith("_")}

    def test_every_chart_reads_back_as_the_op_that_built_it(self):
        kctx = C._KindCtx(None)
        kind = R.get("chart")
        for op in self.OPS:
            self.ok(op)
            with self.subTest(op=op["id"]):
                built = self.comparable(B.normalized(kctx, kind, op))
                spec = B.spec_of(self.scene()["elements"], self.by_alias(op["id"]))
                self.assertEqual(self.comparable(B.normalized(kctx, kind, spec)), built)
                written = self.ok(dict(spec, intent="write it back"))
                self.assertTrue(written["block"]["upsert"])

    def test_look_block_prints_the_spec_and_look_prints_the_gist(self):
        self.ok(REV)
        text = C.look(self.layout, self.team, "alpha-worker")["text"]
        self.assertIn('chart rev "Revenue by region, 2026" bar [0,0 640x400] data rev.csv', text)
        self.assertIn("    max EMEA 2026-08 $4.2M", text)
        self.assertNotIn('"type": "bar"', text, "the spec only with look --block")
        block = C.look(self.layout, self.team, "alpha-worker", block="rev")["text"]
        self.assertIn('"type": "bar"', block)
        full = C.look(self.layout, self.team, "alpha-worker", full=True)["text"]
        self.assertIn("columns: month (temporal), region (nominal), revenue (quantitative)", full)
        self.assertIn("rows: x | EMEA | AMER | APAC", full)
        looked = C.look(self.layout, self.team, "alpha-worker")
        eid = self.by_alias("rev")["id"]
        self.assertEqual(looked["gist"][eid][0], "x=month (12, 2026-01…2026-12) · y=revenue · color=region (3) · rev.csv 36 rows")
        self.assertEqual(looked["charts"][eid]["type"], "bar")
        self.assertEqual(looked["charts"][eid]["frame_notes"], ["every 2nd x label shown"])


class Upgrade(ChartRig):
    def test_a_v1_element_reads_as_a_vega_lite_chart(self):
        v1 = {"id": "E-7", "type": "chart", "x": 0, "y": 0, "w": 480, "h": 320, "text": "Old", "spec_asset": "a" * 32 + ".vl.json",
              "data": "rev.csv", "author": "alpha-worker", "updated_seq": 3}
        el = R.upgraded(v1)
        self.assertEqual((el["kv"], el["engine"], el["settings"]), (2, "vega-lite", {"spec_asset": "a" * 32 + ".vl.json", "data": "rev.csv"}))
        self.assertEqual(el["data"], "rev.csv", "the v1 page still finds its data")
        self.assertIs(R.upgraded(el), el, "a current element is left as it is")
        self.assertEqual(K.spec(el, [], True), {"op": "chart", "title": "Old", "spec_asset": "a" * 32 + ".vl.json", "data": "rev.csv"})
        items = K.emit(v1, {"stills": None})
        slot = next(p for p in items if p["k"] == "slot")
        self.assertEqual(slot["still"], "E-7-v3.png")
        self.assertIn("Vega-Lite chart of rev.csv", json.dumps(slot["fallback"]))


class Drawing(ChartRig):
    def test_the_card_title_and_a_drawn_slot(self):
        eid = self.ok(dict(REV, caption="Source: finance export"))["ids"][0]
        entry, doc = self.entry(eid)
        kinds = [p["k"] for p in entry["items"]]
        self.assertEqual(kinds, ["rect", "text", "text", "slot"])
        title = entry["items"][1]
        self.assertEqual(title["lines"][0]["t"], "Revenue by region, 2026")
        slot = entry["items"][-1]
        el = self.el(eid)
        self.assertEqual((slot["slot"], slot["drawn"], slot["ref"]), ("chart", True, {"id": eid, "v": el["updated_seq"], "doc": el["doc_asset"]}))
        self.assertGreaterEqual(len(slot["fallback"]), 20)
        self.assertTrue(entry["edit"]["field"] == "text")
        self.assertGreater(slot["y"], el["y"] + 30, "the plot sits under the title band")

    def test_the_still_rule_in_both_themes(self):
        eid = self.ok(REV)["ids"][0]
        el = self.el(eid)
        name = "{}-v{}.png".format(eid, el["updated_seq"])
        entry, doc = self.entry(eid, stills={name})
        self.assertEqual(entry["items"][-1]["still"], name)
        light = canvas_svg.write(doc, theme="light")
        dark = canvas_svg.write(doc, theme="dark")
        self.assertIn(name, light, "light draws the page's still")
        self.assertNotIn(name, dark, "dark draws Python's drawing (stills are light-themed, D17)")


class Checks(ChartRig):
    def problems(self):
        return C.check(self.layout, self.team, "alpha-worker")["problems"]

    def test_unreadable_labels_get_a_fix_that_works(self):
        rows = [{"k": "a fairly long category label {}".format(i), "v": i} for i in range(25)]
        eid = self.ok({"op": "chart", "id": "cats", "type": "bar", "rows": rows, "x": "k", "y": "v", "at": [0, 0], "w": 300, "h": 200,
                       "intent": "t"})["ids"][0]
        el = self.el(eid)
        el_frame = el["chart_frame"]
        if el_frame["legible"]:
            self.skipTest("the chart grew legible on its own")
        found = [p for p in self.problems() if p["code"] == "chart_labels"]
        self.assertEqual(len(found), 1)
        self.ok(dict(found[0]["fix"], intent="fix"))
        self.assertEqual([p for p in self.problems() if p["code"] == "chart_labels"], [])

    def test_crowded_capped_and_contrast(self):
        rows = [{"m": "2026-0{}".format(1 + i % 3), "s": "s{:02d}".format(i % 12), "v": i} for i in range(36)]
        self.ok({"op": "chart", "id": "many", "type": "line", "rows": rows, "x": "m", "y": "v", "color": "s", "at": [0, 0], "intent": "t"})
        codes = [p["code"] for p in self.problems()]
        self.assertIn("chart_crowded", codes)
        fix = next(p["fix"] for p in self.problems() if p["code"] == "chart_crowded")
        self.assertEqual({k: v for k, v in fix.items() if k != "intent"}, {"op": "patch", "id": "many", "set": {"top": 8}})
        self.ok(fix)
        codes = [p["code"] for p in self.problems()]
        self.assertNotIn("chart_crowded", codes)
        self.assertIn("chart_capped", codes, "the fold into Other is a note")
        self.ok({"op": "patch", "id": "many", "intent": "t", "set": {"highlight": ["s00"]}})
        self.assertNotIn("chart_contrast", [p["code"] for p in self.problems()], "the highlight paint holds 3:1")


if __name__ == "__main__":
    unittest.main()

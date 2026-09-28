"""The escape hatches (canvas v2 phase 3, 2.8): raw Vega-Lite (today's checks, ``to_flat``, the gist) and a raw ECharts
option (the shared hostile corpus, the allow table, and the op end to end)."""
from __future__ import annotations

import copy
import io
import json
import unittest
from contextlib import redirect_stderr

from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team.canvas_charts import _data, _sanitize
from herdr_team.canvas_charts import _vegalite as VL
from herdr_team.errors import HerdrTeamError

VECTORS = C.Path(__file__).resolve().parent / "fixtures" / "charts" / "sanitize-vectors.json"


class VegaLite(unittest.TestCase):
    def test_todays_refusals_are_kept(self):
        for spec, code in (({"data": {"url": "https://x"}}, "chart_refused"), ([], "op_invalid"), ({"mark": "x" * (201 * 1024)}, "chart_refused")):
            with self.assertRaises(HerdrTeamError) as caught:
                VL.check_spec(spec)
            self.assertEqual(caught.exception.code, code)
        deep: dict = {"a": 1}
        for _ in range(70):
            deep = {"a": deep}
        with self.assertRaises(HerdrTeamError):
            VL.check_spec({"mark": "bar", "x": deep})

    def test_plain_single_views_map_to_the_flat_spec(self):
        self.assertEqual(VL.to_flat({"mark": "bar", "encoding": {"x": {"field": "m"}, "y": {"field": "v", "aggregate": "sum"}}}),
                         {"type": "bar", "x": "m", "y": "v", "aggregate": "sum", "sort": "x"})
        self.assertEqual(VL.to_flat({"mark": {"type": "arc"}, "encoding": {"theta": {"field": "v"}, "color": {"field": "k"}}}),
                         {"type": "pie", "category": "k", "value": "v"})
        self.assertEqual(VL.to_flat({"mark": "rect", "encoding": {"x": {"field": "a"}, "y": {"field": "b"}, "color": {"field": "v"}}}),
                         {"type": "heatmap", "x": "a", "y": "b", "value": "v"})
        self.assertEqual(VL.to_flat({"mark": "bar", "encoding": {"x": {"field": "v", "bin": True}, "y": {"aggregate": "count"}}}),
                         {"type": "histogram", "x": "v", "aggregate": "count"})
        for spec in ({"layer": [], "mark": "bar"}, {"mark": "line", "encoding": {"x": {"field": "a"}, "y": {"field": "b"}, "row": {"field": "c"}}},
                     {"mark": "geoshape", "encoding": {}}, {"mark": "bar", "transform": [], "encoding": {"x": {"field": "a"}}}):
            self.assertIsNone(VL.to_flat(spec), spec)

    def test_the_gist_names_the_mark_fields_and_extremes(self):
        table = _data.inline([{"m": "2026-01", "v": 5, "r": "a"}, {"m": "2026-02", "v": 90000, "r": "b"}])
        lines = VL.gist_from_spec({"mark": "line", "encoding": {"x": {"field": "m"}, "y": {"field": "v"}, "color": {"field": "r"}}}, table)
        self.assertEqual(lines[0], "Vega-Lite line · fields m, v, r · inline rows 2 rows")
        self.assertIn("y=v from 5 to 90K", lines)
        self.assertIn("color=r (2: a, b)", lines)
        layered = VL.gist_from_spec({"layer": [{"mark": "bar"}, {"mark": "rule"}]}, None)
        self.assertTrue(layered[0].startswith("Vega-Lite layer spec, 2 views"))


class RawECharts(unittest.TestCase):
    def test_the_shared_corpus_holds(self):
        for case in json.loads(VECTORS.read_text(encoding="utf-8")):
            with self.subTest(case=case["name"]):
                try:
                    clean, stripped = _sanitize.sanitize(copy.deepcopy(case["input"]))
                except HerdrTeamError as err:
                    self.assertEqual(err.code, case["refused"])
                    continue
                self.assertIsNone(case["refused"])
                self.assertEqual((clean, stripped), (case["output"], case["stripped"]))
                self.assertEqual(clean["tooltip"]["renderMode"], "richText")
                self.assertIs(clean["animation"], False)

    def test_the_allow_table_and_the_corpus_are_fresh(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(_sanitize.main(["--check"]), 0)
        table = json.loads(_sanitize.ALLOW_PATH.read_text(encoding="utf-8"))
        self.assertEqual(table["limits"], {"bytes": 65536, "depth": 32, "numbers": 20000, "string": 1000})
        self.assertIn("radar", table["series"])
        self.assertNotIn("map", table["series"])


class EndToEnd(CanvasRig):
    def test_a_vega_lite_chart_reads_back_and_a_plain_one_is_drawn(self):
        art = self.artifacts()
        (art / "rev.csv").write_text("month,revenue\n2026-01,5\n2026-02,9\n")
        applied = self.ok({"op": "chart", "id": "vl", "title": "Rev", "data": "rev.csv", "at": [0, 0], "intent": "t",
                           "spec": {"mark": "bar", "encoding": {"x": {"field": "month", "type": "ordinal"}, "y": {"field": "revenue", "type": "quantitative"}}}})
        el = self.el(applied["ids"][0])
        self.assertEqual((el["engine"], el["data"], el["kv"]), ("vega-lite", "rev.csv", 2))
        self.assertEqual(json.loads(C.asset_path(self.team, el["spec_asset"]).read_text())["mark"], "bar")
        self.assertEqual(el["gist"][0], "Vega-Lite bar · fields month, revenue · rev.csv 2 rows")
        slot = self.slot(el["id"])
        self.assertTrue(slot["drawn"], "a plain single view is drawn by Python")
        self.assertNotIn("doc", slot["ref"])
        faceted = self.ok({"op": "chart", "id": "facet", "data": "rev.csv", "below": "vl", "intent": "t",
                           "spec": {"mark": "line", "encoding": {"x": {"field": "month"}, "y": {"field": "revenue"}, "row": {"field": "month"}}}})
        slot = self.slot(faceted["ids"][0])
        self.assertNotIn("drawn", slot, "a faceted spec keeps the gist card")
        self.assertIn("Vega-Lite line", json.dumps(slot["fallback"]))

    def test_raw_radar_and_gauge_gists_name_what_they_show(self):
        from herdr_team.canvas_kinds import chart as K

        radar = {"radar": {"indicator": [{"name": "speed", "max": 10}, {"name": "cost", "max": 5}, {"name": "safety", "max": 10}]},
                 "series": [{"type": "radar", "data": [{"name": "alpha", "value": [5, 2, 9]}, {"name": "beta", "value": [8, 4, 3]}]}]}
        self.assertEqual(K.raw_gist(radar), ["ECharts radar · 1 series",
                                             '"alpha" speed 5/10 · cost 2/5 · safety 9/10 (best safety, weakest cost)',
                                             '"beta" speed 8/10 · cost 4/5 · safety 3/10 (best speed, weakest safety)'])
        gauge = {"series": [{"type": "gauge", "min": 0, "max": 120, "data": [{"name": "budget", "value": 72}]}]}
        self.assertEqual(K.raw_gist(gauge), ["ECharts gauge · 1 series", 'gauge "budget" 72 on 0…120 (60% of the dial)'])
        self.assertEqual(K.raw_gist({"series": [{"type": "gauge", "data": [40]}]})[1], "gauge 40 on 0…100 (40% of the dial)")

    def test_a_raw_option_is_sanitised_stored_and_read_back(self):
        applied = self.ok({"op": "chart", "id": "skills", "title": "Skills", "at": [0, 0], "intent": "t",
                           "echarts": {"title": {"text": "x"}, "radar": {"indicator": [{"name": "Rust", "max": 5}, {"name": "Web", "max": 5}]},
                                       "series": [{"type": "radar", "itemStyle": {"color": "#eeeeee"},
                                                   "data": [{"name": "alpha", "value": [4, 3]}]}]}})
        el = self.el(applied["ids"][0])
        self.assertEqual((el["engine"], el["option"], el["stripped"]), ("echarts-raw", None, ["title"]))
        stored = json.loads(C.asset_path(self.team, el["doc_asset"]).read_text())
        self.assertEqual(stored["option"]["tooltip"], {"renderMode": "richText", "confine": True})
        self.assertNotIn("title", stored["option"])
        # QA phase34 L3: a radar reads back per entity, with the indicator names and their scale.
        self.assertEqual(el["gist"], ["ECharts radar · 1 series", '"alpha" Rust 4/5 · Web 3/5 (best Rust, weakest Web)'])
        self.assertEqual(self.slot(el["id"])["ref"]["doc"], el["doc_asset"])
        codes = [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]]
        self.assertIn("chart_escape", codes)
        self.assertIn("chart_contrast", codes, "#eeeeee on white paper is under 3:1")
        refused = self.refused({"op": "chart", "echarts": {"series": [{"type": "scatter", "symbol": "image://x"}]}, "intent": "t"})
        self.assertEqual(refused["code"], "chart_refused")
        refused = self.refused({"op": "chart", "echarts": {"series": [{"type": "map"}]}, "intent": "t"})
        self.assertIn("the page draws", refused["message"])
        both = self.refused({"op": "chart", "type": "bar", "echarts": {"series": []}, "intent": "t"})
        self.assertEqual(both["details"]["field"], "type")

    def slot(self, eid):
        doc = D.display_list(self.scene())
        self.assertEqual(D.validate(doc), [])
        entry = next(e for e in doc["entries"] if e["id"] == eid)
        return next(p for p in entry["items"] if p["k"] == "slot")


if __name__ == "__main__":
    unittest.main()

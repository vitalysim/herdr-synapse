"""Chart data (canvas v2 phase 3, 2.2): file shapes, typing, filters, aggregation, top/Other, bins, LTTB, caps, the
"did you mean" refusals, and ``FetchIO``'s refusals."""
from __future__ import annotations

import json
import os
import unittest

from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_charts as CC
from herdr_team.canvas_charts import _data
from herdr_team.canvas_charts import _format as F
from herdr_team.errors import HerdrTeamError


def refusal(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except HerdrTeamError as err:
        return err
    raise AssertionError("expected a refusal")


class Shapes(unittest.TestCase):
    def test_csv_tsv_and_json_shapes_give_the_same_table(self):
        csv_t = _data.parse("﻿month,revenue\n2026-01,\"1,200\"\n2026-02,$3.2M\n".encode("utf-8"), ".csv", "a.csv")
        tsv_t = _data.parse(b"month\trevenue\n2026-01\t1200\n2026-02\t3200000\n", ".tsv", "a.tsv")
        objs = _data.parse(json.dumps([{"month": "2026-01", "revenue": 1200}, {"month": "2026-02", "revenue": 3200000}]).encode(), ".json", "a.json")
        cols = _data.parse(json.dumps({"columns": ["month", "revenue"], "rows": [["2026-01", 1200], ["2026-02", 3.2e6]]}).encode(), ".json", "b.json")
        data = _data.parse(json.dumps({"data": [{"month": "2026-01", "revenue": 1200}, {"month": "2026-02", "revenue": 3.2e6}]}).encode(),
                           ".json", "c.json")
        for table in (csv_t, tsv_t, objs, cols, data):
            self.assertEqual(table.schema(), [["month", "temporal"], ["revenue", "quantitative"]])
            self.assertEqual(table.by_name["revenue"].values, [1200.0, 3200000.0])
        self.assertEqual(csv_t.by_name["revenue"].unit, "", "mixed units (none and $) keep no unit")

    def test_header_problems_are_named(self):
        self.assertIn("column 2 has no name", refusal(_data.parse, b"a,\n1,2\n", ".csv", "x.csv").message)
        self.assertIn('"a" appears twice', refusal(_data.parse, b"a,a\n1,2\n", ".csv", "x.csv").message)
        err = refusal(_data.parse, json.dumps([{"a": {"b": 1}}]).encode(), ".json", "x.json")
        self.assertEqual(err.code, "chart_refused")
        self.assertIn("flatten it", err.message)

    def test_ragged_rows_are_padded_and_noted(self):
        table = _data.parse(b"a,b\n1,2\n3\n4,5,6\n", ".csv", "x.csv")
        self.assertEqual(table.by_name["b"].values, [2.0, None, 5.0])
        self.assertEqual(table.notes, ["2 ragged rows padded with nulls"])

    def test_inline_caps(self):
        err = refusal(_data.inline, [{"a": i} for i in range(501)])
        self.assertEqual((err.code, err.details["limit"]), ("canvas_limit", "MAX_INLINE_ROWS"))
        self.assertIn("put it in a file under artifacts/", err.message)
        err = refusal(_data.inline, [{"a": "x" * 200} for _ in range(200)])
        self.assertEqual(err.details["limit"], "MAX_INLINE_BYTES")
        self.assertEqual(_data.inline([[1, "a"]], ["n", "t"]).schema(), [["n", "quantitative"], ["t", "nominal"]])

    def test_the_row_cap(self):
        raw = ("n\n" + "1\n" * (_data.MAX_DATA_ROWS + 1)).encode()
        self.assertEqual(refusal(_data.parse, raw, ".csv", "big.csv").details["limit"], "MAX_DATA_ROWS")


class Typing(unittest.TestCase):
    def test_numbers_dates_and_nulls(self):
        self.assertEqual(_data.number("1,234.5"), (1234.5, ""))
        self.assertEqual(_data.number("12%"), (12.0, "%"))
        self.assertEqual(_data.number("$3.2M"), (3200000.0, "$"))
        self.assertEqual(_data.number("-€4k"), (-4000.0, "€"))
        self.assertIsNone(_data.number("12 apples"))
        self.assertIsNone(_data.number(True))
        for value, grain in (("2026", "year"), ("2026-03", "month"), ("2026-03-04", "day"), ("2026-Q2", "quarter"), ("2026-W05", "week"),
                             ("2026-03-04T10:30:00Z", "datetime")):
            self.assertEqual(_data.temporal(value)[2], grain, value)
        self.assertIsNone(_data.temporal("2026-13"))
        for null in ("", "null", "NA", "N/A", "-", None):
            self.assertTrue(_data.is_null(null), null)

    def test_column_types_are_decided_by_every_value(self):
        table = _data.inline([{"y": "2026", "n": "1", "m": "a"}, {"y": "2027", "n": "NA", "m": "2"}])
        self.assertEqual(table.schema(), [["y", "temporal"], ["n", "quantitative"], ["m", "nominal"]])
        self.assertEqual(_data.inline([{"n": 2026}, {"n": 2027}]).schema(), [["n", "quantitative"]], "JSON numbers stay numbers")

    def test_declared_types_are_checked(self):
        table = _data.retype(_data.inline([{"a": "x"}]), {"a": "ordinal"})
        self.assertEqual(table.schema(), [["a", "ordinal"]])
        err = refusal(_data.retype, _data.inline([{"a": "x"}]), {"a": "quantitative"})
        self.assertEqual(err.details["field"], "types.a")


class Validation(unittest.TestCase):
    ROWS = [{"month": "2026-0{}".format(m), "region": r, "revenue": m * 100 + i} for m in range(1, 4) for i, r in enumerate(("EMEA", "APAC"))]

    def compile(self, **spec):
        base = {"type": "bar", "rows": self.ROWS}
        base.update(spec)
        normal, _ = CC.normalize_spec(base)
        return CC.compile(normal, _data.inline(normal["rows"]))

    def test_a_hallucinated_field_gets_its_repair(self):
        err = refusal(self.compile, x="month", y="revenu")
        self.assertEqual(err.details["field"], "y")
        self.assertEqual(err.message, 'field "revenu" is not a column of inline rows; columns: month (temporal, 3 values), region (nominal, 2), '
                                      'revenue (quantitative). Did you mean "revenue"?')
        self.assertEqual(refusal(self.compile, x="Month", y="revenue").details["nearest"], "month", "case-insensitive first")

    def test_a_wrong_type_says_what_the_channel_takes(self):
        err = refusal(self.compile, x="month", y="region")
        self.assertEqual(err.message, 'y needs a quantitative field; "region" is nominal (2 values: EMEA, APAC). For counts use aggregate: "count".')

    def test_required_and_unknown_channels(self):
        self.assertEqual(refusal(CC.normalize_spec, {"type": "bar", "rows": self.ROWS, "x": "month"}).details["field"], "y")
        err = refusal(CC.normalize_spec, {"type": "bar", "rows": self.ROWS, "x": "month", "y": "revenue", "source": "x"})
        self.assertIn("bar does not take source; its channels: x, y, color", err.message)
        self.assertIn("did you mean", refusal(CC.normalize_spec, {"type": "bar", "rows": self.ROWS, "x": "month", "y": "revenue",
                                                                  "colour": "region"}).message)
        spec, _ = CC.normalize_spec({"type": "bar", "rows": self.ROWS, "x": "region", "aggregate": "count"})
        self.assertNotIn("y", spec, "count needs no measure")

    def test_filters(self):
        found = self.compile(x="month", y="revenue", filter=[{"field": "region", "in": ["EMEA"]}, {"field": "revenue", ">": 150}])
        self.assertEqual(found.model["cats"], ["2026-02", "2026-03"])
        err = refusal(self.compile, x="month", y="revenue", filter=[{"field": "region", "=": "EMEA"}, {"field": "revenue", ">": 10000}])
        self.assertEqual(err.code, "chart_empty")
        self.assertIn('filter[0] {"field":"region","=":"EMEA"} keeps 3', err.message)
        self.assertIn("keeps 0", err.message)
        self.assertEqual(err.details["counts"], [3, 0])
        self.assertEqual(refusal(self.compile, x="month", y="revenue", filter=[{"field": "region", ">": 3}]).details["field"], "filter[0].>")
        between = self.compile(x="month", y="revenue", filter={"field": "month", "between": ["2026-02", "2026-03"]})
        self.assertEqual(between.model["cats"], ["2026-02", "2026-03"])
        nulls = self.compile(x="month", y="revenue", filter=[{"field": "revenue", "null": False}])
        self.assertEqual(len(nulls.model["cats"]), 3)

    def test_aggregation_defaults_to_sum_when_rows_repeat(self):
        found = self.compile(x="month", y="revenue")
        self.assertEqual((found.model["how"], found.model["series"][0]["values"]), ("sum", [201, 401, 601]))
        mean = self.compile(x="month", y="revenue", aggregate="mean")
        self.assertEqual(mean.model["series"][0]["values"], [100.5, 200.5, 300.5])
        err = refusal(self.compile, x="month", y="revenue", aggregate="none")
        self.assertIn("pick how to combine them", err.message)

    def test_top_folds_the_tail_into_other(self):
        rows = [{"c": "c{:02d}".format(i), "v": 100 - i} for i in range(70)]
        spec, _ = CC.normalize_spec({"type": "bar", "rows": rows, "x": "c", "y": "v"})
        found = CC.compile(spec, _data.inline(rows))
        self.assertEqual(len(found.model["cats"]), 60)
        self.assertEqual(found.model["cats"][-1], "Other")
        self.assertIn("11 of 70 c values folded into Other", found.model["capped"][0])
        self.assertEqual([w["code"] for w in found.warnings], ["chart_capped"])
        spec, _ = CC.normalize_spec({"type": "bar", "rows": rows, "x": "c", "y": "v", "top": 5, "other": False})
        dropped = CC.compile(spec, _data.inline(rows))
        self.assertEqual(dropped.model["cats"], ["c00", "c01", "c02", "c03", "c04"])

    def test_a_long_time_axis_keeps_the_latest_periods(self):
        rows = [{"d": "20{:02d}".format(i), "m": i} for i in range(10, 80)]
        spec, _ = CC.normalize_spec({"type": "bar", "rows": rows, "x": "d", "y": "m"})
        found = CC.compile(spec, _data.inline(rows))
        self.assertEqual((found.model["cats"][0], found.model["cats"][-1], len(found.model["cats"])), ("2020", "2079", 60))


class Helpers(unittest.TestCase):
    def test_bins_are_freedman_diaconis_on_a_nice_step(self):
        edges = _data.bin_numeric([float(v) for v in range(100)])
        self.assertTrue(5 <= len(edges) - 1 <= 60)
        step = edges[1] - edges[0]
        self.assertEqual(_data.nice_step(step), step)
        self.assertEqual(_data.bin_numeric([1.0, 1.0]), [0.5, 1.5])
        self.assertEqual(len(_data.bin_numeric([float(v) for v in range(100)], 10)) - 1, 10)

    def test_lttb_keeps_the_ends_and_the_shape(self):
        xs = [float(i) for i in range(1000)]
        ys = [0.0] * 1000
        ys[500] = 100.0
        keep = _data.lttb(xs, ys, 50)
        self.assertEqual((keep[0], keep[-1], len(keep)), (0, 999, 50))
        self.assertIn(500, keep, "the spike survives")
        self.assertEqual(_data.lttb(xs[:10], ys[:10], 50), list(range(10)))

    def test_quantiles_and_stride(self):
        self.assertEqual(_data.quantiles([1, 2, 3, 4]), [1.75, 2.5, 3.25])
        self.assertEqual(_data.stride(10, 4), [0, 3, 6, 9])

    def test_formats_match_their_vectors(self):
        self.assertEqual(F.fmt_number(4200000, "compact", "$"), "$4.2M")
        self.assertEqual(F.fmt_number(-147, None, "ms"), "−147 ms")
        self.assertEqual(F.fmt_number(0.125, "percent"), "12.5%")
        self.assertEqual(F.fmt_number(20361, "month"), "Sep 2025")
        self.assertEqual(F.fmt_number(2.675, "0.00"), "2.68", "half away from zero, not banker's rounding")
        vectors = json.loads((C.Path(__file__).resolve().parent / "fixtures" / "charts" / "format-vectors.json").read_text(encoding="utf-8"))
        for case in vectors:
            self.assertEqual(F.fmt_number(case["v"], case["fmt"], case["unit"]), case["out"], case)


class Fetching(CanvasRig):
    def test_fetchio_reads_only_artifacts_with_the_callers_caps(self):
        io = C._FetchIO(self.layout, self.team, None)
        self.assertEqual(refusal(io.read_artifact, "x.csv", (".csv",), 100).code, "artifacts_unset")
        art = self.artifacts()
        (art / "a.csv").write_text("a\n1\n")
        (art / "big.csv").write_text("a\n" + "1\n" * 200)
        (art / "sub").mkdir()
        (art / "sub" / "m.gltf").write_text("{}")
        (art / "sub" / "m.bin").write_bytes(b"\0" * 8)
        found = io.read_artifact("artifacts/a.csv", (".csv",), 100)
        self.assertEqual((found.rel, found.suffix, found.data), ("a.csv", ".csv", b"a\n1\n"))
        err = refusal(io.read_artifact, "a.csv", (".json",), 100)
        self.assertEqual((err.code, err.details["allowed"]), ("path_refused", [".json"]))
        err = refusal(io.read_artifact, "big.csv", (".csv",), 100)
        self.assertEqual((err.code, err.details["limit"], err.details["max"]), ("canvas_limit", "max_bytes", 100))
        for bad in ("../a.csv", "/etc/passwd", "c:x.csv", "a\\b.csv", "", 3):
            self.assertEqual(refusal(io.read_artifact, bad, (".csv",), 100).code, "path_refused", bad)
        outside = self.ts.tmp / "secret.csv"
        outside.write_text("a\n1\n")
        os.symlink(outside, art / "link.csv")
        self.assertEqual(refusal(io.read_artifact, "link.csv", (".csv",), 100).code, "path_refused", "no symlink escapes")
        model = io.read_artifact("sub/m.gltf", (".gltf",), 100)
        self.assertEqual(io.sibling(model, "m.bin", (".bin",), 100).rel, "sub/m.bin")
        for bad in ("../a.csv", "https://x/m.bin", "data:application/octet-stream;base64,AA", "/m.bin"):
            self.assertEqual(refusal(io.sibling, model, bad, (".bin",), 100).code, "path_refused", bad)


if __name__ == "__main__":
    unittest.main()

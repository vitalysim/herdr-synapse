"""The chart-type registry (canvas v2 phase 3, 2.3 and 8.1): discovery, aliases, the conformance of every type, and the
one-module proof (a ``lollipop`` type dropped in as ``tests/fixtures/chart_lollipop.py``)."""
from __future__ import annotations

import json
import shutil
import sys
import unittest

import chart_conformance
from support import PLUGIN_ROOT
from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_catalog
from herdr_team import canvas_charts as CC
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_mcp as M
from herdr_team import reference_docs
from herdr_team.errors import HerdrTeamError

BUILT_IN = ("bar", "line", "area", "pie", "donut", "scatter", "heatmap", "histogram", "box", "funnel", "treemap", "sankey")
MODULE = "tests.fixtures.chart_lollipop"


class Discovery(unittest.TestCase):
    def test_the_built_in_types_are_registered_in_order(self):
        names = CC.names()
        self.assertEqual([n for n in names if n in BUILT_IN], list(BUILT_IN))
        orders = [c.order for c in CC.types()]
        self.assertEqual(orders, sorted(orders))
        self.assertFalse(any(name.startswith("_") for name in names))

    def test_aliases_resolve_to_the_canonical_type(self):
        for alias, name in (("column", "bar"), ("doughnut", "donut"), ("bubble", "scatter"), ("boxplot", "box")):
            self.assertEqual(CC.get(alias).name, name)
        spec, _ = CC.normalize_spec({"type": "column", "rows": [{"a": "x", "b": 1}], "x": "a", "y": "b"})
        self.assertEqual(spec["type"], "bar", "readback prints the canonical name")
        with self.assertRaises(HerdrTeamError) as caught:
            CC.normalize_spec({"type": "bubble", "rows": [{"a": 1, "b": 1}], "x": "a", "y": "b"})
        self.assertEqual(caught.exception.details["field"], "size", "a bubble needs its size")

    def test_an_unknown_type_names_the_list_and_the_nearest(self):
        with self.assertRaises(HerdrTeamError) as caught:
            CC.normalize_spec({"type": "barchart", "rows": [{"a": 1}]})
        self.assertIn('invalid type "barchart"; one of: bar, line', caught.exception.message)
        self.assertIn('did you mean "bar"', caught.exception.message)

    def test_kind_is_read_as_type(self):
        """QA phase 6 F8: a chart names its variant ``type`` where every other op names it ``kind``, which cost a live
        agent its first try. ``kind`` is accepted with a warning that teaches the real name."""
        spec, warnings = CC.normalize_spec({"kind": "bar", "rows": [{"a": "x", "b": 1}], "x": "a", "y": "b"})
        self.assertEqual(spec["type"], "bar")
        self.assertNotIn("kind", spec, "nothing stores the alias")
        self.assertEqual([w["code"] for w in warnings], ["field_alias"])
        self.assertIn("a chart's type is type, not kind", warnings[0]["message"])
        # An explicit type wins, and is not warned about.
        spec, warnings = CC.normalize_spec({"type": "line", "kind": "bar", "rows": [{"a": "x", "b": 1}], "x": "a", "y": "b"})
        self.assertEqual((spec["type"], warnings), ("line", []))

    def test_a_word_that_names_a_charts_shape_says_which_fields_draw_it(self):
        """``invalid type "grouped"`` used to list the types without saying how to get grouped bars - the most ordinary
        business request there is (QA phase 6, F8)."""
        for word, wanted in (("grouped", 'type "bar" with color:'), ("grouped bar chart", 'type "bar" with color:'),
                             ("stacked", "stack: true"), ("horizontal", "horizontal: true")):
            with self.assertRaises(HerdrTeamError) as caught:
                CC.normalize_spec({"type": word, "rows": [{"a": 1}]})
            self.assertIn(wanted, caught.exception.message, word)
        with self.assertRaises(HerdrTeamError) as caught:
            CC.normalize_spec({"type": "wobble", "rows": [{"a": 1}]})
        self.assertNotIn("; wobble is", caught.exception.message, "only the words that name a shape earn a hint")

    def test_pie_takes_x_and_y_as_category_and_value(self):
        spec, warnings = CC.normalize_spec({"type": "pie", "rows": [{"a": "x", "b": 1}], "x": "a", "y": "b"})
        self.assertEqual((spec["category"], spec["value"]), ("a", "b"))
        self.assertEqual([w["code"] for w in warnings], ["channel_alias", "channel_alias"])

    def test_options_belong_to_their_types(self):
        with self.assertRaises(HerdrTeamError) as caught:
            CC.normalize_spec({"type": "line", "rows": [{"a": 1, "b": 1}], "x": "a", "y": "b", "bins": 10})
        self.assertIn("bins is an option of histogram", caught.exception.message)
        with self.assertRaises(HerdrTeamError) as caught:
            CC.normalize_spec({"type": "pie", "rows": [{"a": "x", "b": 1}], "category": "a", "value": "b", "x": "a"})
        self.assertEqual(caught.exception.details["field"], "x", "x is renamed only when category is not given")

    def test_the_catalog_lists_every_type_with_an_example(self):
        doc = canvas_catalog.catalog("charts")
        self.assertEqual([c["name"] for c in doc["charts"]], CC.names())
        for entry in doc["charts"]:
            self.assertEqual(entry["example"]["type"], entry["name"])
        one = canvas_catalog.catalog("charts", "sankey")
        self.assertEqual([c["name"] for c in one["charts"]], ["sankey"])
        self.assertIn("sankey — flows", canvas_catalog.text(one))
        with self.assertRaises(HerdrTeamError) as caught:
            canvas_catalog.catalog("charts", "sankee")
        self.assertEqual(caught.exception.code, "usage")


class Conformance(unittest.TestCase):
    def test_every_registered_type_conforms(self):
        for chart in CC.types():
            with self.subTest(chart=chart.name):
                chart_conformance.check_chart(self, chart)


class OneModuleChart(CanvasRig):
    def setUp(self):
        super().setUp()
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        self.before = (dict(C._FIELDS), M.op_table())
        CC._load_extra(MODULE)
        self.addCleanup(CC._unload, MODULE)

    def test_one_module_is_a_whole_chart_type(self):
        chart = CC.get("lollipop")
        self.assertIsNotNone(chart)
        chart_conformance.check_chart(self, chart)
        self.assertIn("dot", C._FIELDS["chart"], "the op takes the new type's option")
        self.assertIn("lollipop", M.op_table())
        self.assertIn("lollipop", [c["name"] for c in canvas_catalog.catalog("charts")["charts"]])
        self.assertIn("`dot`", reference_docs.render_canvas_ops_section())
        applied = self.ok(dict(json.loads(chart.example), at=[0, 0]))
        el = self.el(applied["ids"][0])
        self.assertEqual((el["engine"], el["chart"]["type"], el["settings"]["dot"]), ("echarts", "lollipop", 12))
        self.assertIn("lollipop x=repo (3)", C.look(self.layout, self.team, "alpha-worker")["text"])
        doc = D.display_list(self.scene())
        self.assertEqual(D.validate(doc), [])
        slot = next(p for e in doc["entries"] if e["id"] == el["id"] for p in e["items"] if p["k"] == "slot")
        self.assertTrue(slot["drawn"])
        self.assertEqual(C.check(self.layout, self.team, "alpha-worker")["problems"], [])
        refused = self.refused({"op": "chart", "type": "lollipop", "rows": [{"a": "x", "b": 1}], "x": "a", "y": "b", "dot": 50, "intent": "t"})
        self.assertEqual(refused["details"]["field"], "dot")

    def test_unloading_restores_every_table(self):
        CC._unload(MODULE)
        self.assertIsNone(CC.get("lollipop"))
        self.assertEqual((dict(C._FIELDS), M.op_table()), self.before)
        CC._load_extra(MODULE)


if __name__ == "__main__":
    unittest.main()

"""Slot contract v2 (canvas v2 phases 3 and 4, section 1) on a test-only kind (``tests/fixtures/kind_gauge.py``): the
kind contract v4, ``Block.load`` outside the lock and its refusals, assets kept only when the op applies, stills with
views, the display-list fields and their validation, ``look``'s gist and ``--view``, and the catalog command."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

from support import PLUGIN_ROOT
from support import FAKE_AGENTS
from test_canvas import CanvasRig
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team.canvas_kinds import _slot

MODULE = "tests.fixtures.kind_gauge"


def png(n: int = 0) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes([n]) * 16


class Contract(unittest.TestCase):
    def test_version_views_and_slot_kinds(self):
        self.assertEqual(R.API_VERSION, 4)
        for views in ((), ("iso", "iso"), ("Iso",), ("a" * 13,), ("iso-1",)):
            with self.assertRaises(ValueError, msg=views):
                R.register(R.Kind(name="bad_views", still_views=views))
        self.assertLessEqual({"chart", "mermaid", "viz"}, set(R.slot_kinds()))
        self.assertEqual(R.get("chart").still_views, ("",))

    def test_still_names(self):
        el = {"id": "E-5", "updated_seq": 40}
        self.assertEqual((_slot.still_name(el), _slot.still_name(el, "iso")), ("E-5-v40.png", "E-5-v40-iso.png"))
        self.assertIsNone(_slot.still_of({}, "", None), "an element with no id has no still")
        self.assertEqual(_slot.still_of(el, "iso", {"E-5-v40-iso.png"}), "E-5-v40-iso.png")
        self.assertIsNone(_slot.still_of(el, "iso", set()))

    def test_validate_checks_the_new_slot_fields(self):
        base = {"k": "slot", "slot": "gauge", "x": 0, "y": 0, "w": 10, "h": 10, "ref": {"id": "E-1", "v": 2}, "still": None, "fallback": []}
        for bad, words in (({"ref": {"id": "E-1", "v": 2, "doc": "x.png"}}, "ref.doc"), ({"views": {"iso": "nope.png"}}, "views"),
                           ({"drawn": False}, "drawn is true"), ({"gl": 1}, "gl is true"), ({"drawn": True}, "a drawn slot"),
                           ({"still": "E-1-v2-top.png", "views": {"iso": "E-1-v2-iso.png"}}, "primary view")):
            out: list = []
            D._check_slot(dict(base, **bad), "s", out)
            self.assertTrue(any(words in line for line in out), (bad, out))
        out = []
        D._check_slot(dict(base, ref={"id": "E-1", "v": 2, "doc": "a" * 32 + ".glb"}, views={"iso": None, "top": None}, gl=True), "s", out)
        self.assertEqual(out, [])


class Gauge(CanvasRig):
    def setUp(self):
        super().setUp()
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        R._load_extra(MODULE)
        self.addCleanup(R._unload, MODULE)
        self.art = self.artifacts()
        (self.art / "g.json").write_text('{"value": 42}')

    def assets(self):
        folder = C._dir(self.team) / C.ASSETS_DIR
        return sorted(os.listdir(folder)) if folder.is_dir() else []

    def test_load_runs_before_the_lock_and_its_refusal_refuses_the_op(self):
        events = []
        real_lock, real_load = C._canvas_lock, B.load

        def lock(team, *a, **kw):
            events.append("lock")
            return real_lock(team, *a, **kw)

        def load(ops, io, find=None):
            events.append("load")
            return real_load(ops, io, find)

        with mock.patch.object(C, "_canvas_lock", lock), mock.patch.object(B, "load", load):
            eid = self.ok({"op": "gauge", "id": "g", "data": "g.json", "at": [0, 0], "intent": "t"})["ids"][0]
        self.assertEqual(events[:2], ["load", "lock"])
        el = self.el(eid)
        self.assertEqual((el["value"], el["source"]), (42.0, "g.json"))
        refused = self.refused({"op": "gauge", "data": "none.json", "intent": "t"})
        self.assertEqual((refused["code"], refused["details"]["field"]), ("path_refused", "data"))
        (self.art / "g.txt").write_text("x")
        self.assertEqual(self.refused({"op": "gauge", "data": "g.txt", "intent": "t"})["details"]["allowed"], [".json"])

    def test_an_asset_is_written_only_when_its_op_applies(self):
        (self.art / "big.json").write_text('{"value": 500}')
        before = self.assets()
        self.assertEqual(self.refused({"op": "gauge", "data": "big.json", "intent": "t"})["details"]["field"], "max")
        self.assertEqual(self.assets(), before, "a refused op leaves no asset")
        eid = self.ok({"op": "gauge", "data": "g.json", "at": [0, 0], "intent": "t"})["ids"][0]
        name = self.el(eid)["doc_asset"]
        self.assertIn(name, self.assets())
        self.assertEqual(json.loads(C.asset_path(self.team, name).read_text()), {"v": 1, "value": 42.0})

    def test_a_patch_loads_with_the_blocks_spec(self):
        self.ok({"op": "gauge", "id": "g", "data": "g.json", "at": [0, 0], "intent": "t"})
        (self.art / "h.json").write_text('{"value": 7}')
        self.ok({"op": "patch", "id": "g", "set": {"data": "h.json"}, "intent": "t"})
        self.assertEqual(self.el(self.ok({"op": "patch", "id": "g", "set": {"max": 50}, "intent": "t"})["ids"][0])["value"], 7.0)

    def test_stills_with_views(self):
        eid = self.ok({"op": "gauge", "data": "g.json", "at": [0, 0], "intent": "t"})["ids"][0]
        v = self.el(eid)["updated_seq"]
        dial = C.store_still(self.team, eid, v, png(), view="dial")
        self.assertEqual(dial.name, "{}-v{}-dial.png".format(eid, v))
        C.store_still(self.team, eid, v, png(), view="side")
        C.store_still(self.team, eid, v + 1, png(1), view="dial")
        self.assertIsNone(C.still_path(self.team, eid, v, "dial"), "an older version of the same view is pruned")
        self.assertIsNotNone(C.still_path(self.team, eid, v, "side"), "another view is kept")
        with self.assertRaises(C.HerdrTeamError) as caught:
            C.store_still(self.team, eid, v, png(), view="iso")
        self.assertEqual((caught.exception.code, caught.exception.details["views"]), ("usage", ["dial", "side"]))
        with self.assertRaises(C.HerdrTeamError):
            C.store_still(self.team, eid, v, png(), view="")
        box = self.ok({"op": "shape", "kind": "box", "text": "x", "at": [0, 400], "intent": "t"})["ids"][0]
        with self.assertRaises(C.HerdrTeamError) as caught:
            C.store_still(self.team, box, 1, png())
        self.assertEqual(caught.exception.code, "element_unknown")
        self.assertIn("gauge", caught.exception.message, "the kinds the page draws are the registry's")

    def test_the_display_entry_and_look(self):
        eid = self.ok({"op": "gauge", "id": "g", "data": "g.json", "at": [0, 0], "intent": "t"})["ids"][0]
        el = self.el(eid)
        name = "{}-v{}-dial.png".format(eid, el["updated_seq"])
        doc = D.display_list(self.scene(), stills={name})
        self.assertEqual(D.validate(doc), [])
        slot = next(p for e in doc["entries"] if e["id"] == eid for p in e["items"] if p["k"] == "slot")
        self.assertEqual(slot["views"], {"dial": name, "side": None})
        self.assertEqual((slot["still"], slot["drawn"], slot["ref"]["doc"]), (name, True, el["doc_asset"]))
        looked = C.look(self.layout, self.team, "alpha-worker")
        self.assertIn("  gauge at 42 of 100", looked["text"])
        self.assertEqual(looked["gist"][eid], ["gauge at 42 of 100"])
        self.assertEqual(looked["gauges"][eid], {"value": 42.0})
        self.assertIn("from g.json", C.look(self.layout, self.team, "alpha-worker", full=True)["text"])
        side = C.look(self.layout, self.team, "alpha-worker", image=True, view="side")
        svg = C.Path(side["svg"]).read_text()
        from herdr_team import canvas_theme

        self.assertIn(canvas_theme.palette("light")["chart.cat.1"], svg, "--view side draws the side view's drawing")
        with self.assertRaises(C.HerdrTeamError) as caught:
            C.look(self.layout, self.team, "alpha-worker", image=True, view="back")
        self.assertIn("dial", caught.exception.message)


class Catalog(CanvasRig):
    def human(self, *argv):
        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts), live_api(list(FAKE_AGENTS))))

    def test_the_catalog_command(self):
        code, payload, err = self.human("canvas", "catalog", "charts", "--type", "bar")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["name"] for c in payload["charts"]], ["bar"])
        code, payload, err = self.human("canvas", "catalog", "scene3d")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["primitives"])
        code, _payload, err = self.human("canvas", "catalog", "charts", "--type", "nope")
        self.assertEqual((code, err["code"]), (2, "usage"))


if __name__ == "__main__":
    unittest.main()

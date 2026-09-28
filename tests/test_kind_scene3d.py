"""The ``scene3d`` kind (canvas v2 phase 4, 3.1, 3.6, 3.9, 4.2 and 8.2): an inline block of objects placed by relations.

Create, upsert, patch objects and links (references cascade), the camera,
the round trip of its spec (T-B1), the display-list slot (views, ``gl``,
``drawn``), stills per view, the checks with fixes that apply, the size cap,
and what ``look`` reads back (3.9, verbatim on the topology).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from support import PLUGIN_ROOT
from test_canvas import CanvasRig
from test_canvas_render_fonts import decode_png

from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_render
from herdr_team.canvas_kinds import scene3d as K
from herdr_team.canvas_scene3d import _solver

sys.path.insert(0, str(PLUGIN_ROOT / "tests" / "fixtures" / "scene3d"))
import make_fixtures as M  # noqa: E402 - the fixture builders live beside the fixtures

TOPO = dict(M.TOPOLOGY, at=[0, 0])
PNG = canvas_render.PNG_MAGIC + b"\x00" * 32


class Scene(CanvasRig):
    def element(self, alias="topo"):
        return next(e for e in self.scene()["elements"] if e.get("alias") == alias)

    def solved(self, alias="topo"):
        return _solver.expand(self.element(alias)["solved"])

    def problems(self, code):
        return [p for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if p["code"] == code]


class Create(Scene):
    def test_one_element_holds_the_scene(self):
        applied = self.ok(TOPO)
        self.assertEqual(len(applied["ids"]), 1)
        el = self.element()
        self.assertEqual(el["type"], "scene3d")
        self.assertEqual(el["text"], "Prod topology")
        self.assertEqual(el["settings"], {"units": "m", "camera": "iso", "lights": "studio", "ground": True, "labels": "auto"})
        self.assertEqual([o["id"] for o in el["objects"]], ["base", "lb", "api", "api1", "api2", "db", "cache"])
        self.assertEqual(el["objects"][1], {"id": "lb", "shape": "box", "tone": "info", "label": "Load balancer", "on": "base", "at": [-3, 0],
                                            "size": [1.2, 0.4, 1.2]}, "stored as given: no defaults filled in")
        self.assertEqual([l["id"] for l in el["links"]], ["lb->api", "api->db", "api->cache"])
        self.assertEqual((el["w"], el["h"]), (640, 420))
        solved = self.solved()
        self.assertEqual(solved["bounds"], [-4.0, 0.0, -2.5, 4.0, 2.22, 2.5])
        self.assertEqual(solved["objects"]["db"]["rel"], "on base, right_of api (gap 1.00)")
        self.assertEqual(applied["gist"][:2], ["base plane 8×5 (ground)", 'lb box 1.2×0.4×1.2 "Load balancer" on base, at (−3.0, 0.0)'])

    def test_refusals_name_the_field(self):
        cases = [({"objects": []}, "objects"),
                 ({"objects": [{"id": "a", "shape": "boxx"}]}, "objects[0].shape"),
                 ({"objects": [{"id": "a", "shape": "box", "radius": 2}]}, "objects[0].radius"),
                 ({"objects": [{"shape": "box"}]}, "objects[0].id"),
                 ({"objects": [{"id": "a", "on": "ghost"}]}, "objects[0].on"),
                 ({"objects": [{"id": "a"}], "links": ["a -> ghost"]}, "links[0].to"),
                 ({"objects": [{"id": "a"}], "camera": "fisheye"}, "camera"),
                 ({"objects": [{"id": "a", "tone": "pink"}]}, "objects[0].tone")]
        for fields, field in cases:
            with self.subTest(field=field):
                refused = self.refused(dict({"op": "scene3d", "id": "bad", "intent": "t"}, **fields))
                self.assertEqual(refused["details"]["field"], field, refused)
        refused = self.refused({"op": "scene3d", "id": "bad", "intent": "t", "objects": [{"id": "a", "shape": "boxx"}]})
        self.assertIn('did you mean "box"', refused["message"])
        refused = self.refused({"op": "scene3d", "id": "bad", "intent": "t", "objects": [{"id": "a", "right_of": "b"}, {"id": "b", "left_of": "a"}]})
        self.assertEqual(refused["code"], "relation_cycle")
        self.assertIn("a → b → a", refused["message"])

    def test_limits(self):
        many = [{"id": "o{}".format(i)} for i in range(151)]
        refused = self.refused({"op": "scene3d", "id": "big", "intent": "t", "objects": many})
        self.assertEqual(refused["code"], "canvas_limit")
        wordy = [{"id": "o{}".format(i), "label": "l" * 80, "note": "n" * 200, "on": "o0" if i else None} for i in range(150)]
        for obj in wordy:
            if obj["on"] is None:
                obj.pop("on")
        refused = self.refused({"op": "scene3d", "id": "big", "intent": "t", "objects": wordy})
        self.assertEqual(refused["code"], "canvas_limit")
        self.assertIn("split the scene, or use a glTF model", refused["message"])

    def test_the_display_list_slot(self):
        self.ok(TOPO)
        doc = D.display_list(self.scene(), stills=set())
        self.assertEqual(D.validate(doc), [])
        entry = next(e for e in doc["entries"] if e["kind"] == "scene3d")
        slot = next(i for i in entry["items"] if i.get("k") == "slot")
        self.assertEqual(slot["slot"], "scene3d")
        self.assertEqual(slot["views"], {"iso": None, "front": None, "top": None})
        self.assertIsNone(slot["still"])
        self.assertTrue(slot["gl"])
        self.assertTrue(slot["drawn"])
        self.assertEqual(slot["ref"], {"id": self.element()["id"], "v": self.element()["updated_seq"]})
        drawn = slot["fallback"][0]["items"] + slot["fallback"][1:]
        self.assertGreaterEqual(len(drawn), 20, "the agent's picture of a scene with no page")
        self.assertEqual([p["k"] for p in slot["fallback"][1:]].count("text"), 5, "five labels, outside the clip")
        title = next(i for i in entry["items"] if i.get("k") == "text")
        self.assertEqual(title["lines"][0]["t"], "Prod topology")

    def test_stills_per_view(self):
        self.ok(TOPO)
        el = self.element()
        eid, v = el["id"], el["updated_seq"]
        for view in ("iso", "front"):
            C.store_still(self.team, eid, v, PNG, view)
        with self.assertRaises(C.HerdrTeamError):
            C.store_still(self.team, eid, v, PNG, "side")
        with self.assertRaises(C.HerdrTeamError):
            C.store_still(self.team, eid, v, PNG, "")
        doc = D.display_list(self.scene(), stills=C._stills(self.team))
        slot = next(i for e in doc["entries"] if e["kind"] == "scene3d" for i in e["items"] if i.get("k") == "slot")
        self.assertEqual(slot["views"], {"iso": "{}-v{}-iso.png".format(eid, v), "front": "{}-v{}-front.png".format(eid, v), "top": None})
        self.assertEqual(slot["still"], slot["views"]["iso"], "the camera's view is the primary still")
        self.ok({"op": "patch", "id": "topo", "set": {"camera": "front"}, "intent": "t"})
        el = self.element()
        C.store_still(self.team, el["id"], el["updated_seq"], PNG, "front")
        doc = D.display_list(self.scene(), stills=C._stills(self.team))
        slot = next(i for e in doc["entries"] if e["kind"] == "scene3d" for i in e["items"] if i.get("k") == "slot")
        self.assertEqual(slot["still"], "{}-v{}-front.png".format(el["id"], el["updated_seq"]))

    def test_look_image_views(self):
        self.ok(TOPO)
        for view in ("iso", "front", "top"):
            found = C.look(self.layout, self.team, "alpha-worker", image=True, view=view)
            self.assertIsNotNone(found.get("svg") or found.get("image") or found.get("image_error"), view)
        with self.assertRaises(C.HerdrTeamError):
            C.look(self.layout, self.team, "alpha-worker", image=True, view="sideways")


class Patch(Scene):
    def test_add_update_and_remove_objects_and_links(self):
        self.ok(TOPO)
        version = self.element()["updated_seq"]
        self.ok({"op": "patch", "id": "topo", "if_version": version, "intent": "add a read replica",
                 "add": {"objects": [{"id": "replica", "shape": "cylinder", "radius": 0.5, "height": 1.2, "tone": "success", "label": "Replica",
                                      "on": "base", "behind": "db", "gap": 0.8}],
                         "links": ["db -> replica"]},
                 "set": {"camera": {"preset": "orbit", "az": 60, "el": 30}}})
        el = self.element()
        self.assertEqual(el["settings"]["camera"], {"preset": "orbit", "az": 60, "el": 30})
        solved = self.solved()
        self.assertEqual(solved["objects"]["replica"]["aabb"][5], solved["objects"]["db"]["aabb"][2] - 0.8, "0.8 behind db")
        self.assertIn("db->replica", [l["id"] for l in el["links"]])
        self.ok({"op": "patch", "id": "topo", "update": {"objects": [{"id": "db", "radius": 0.9}]}, "intent": "t"})
        self.assertEqual(self.solved()["objects"]["db"]["ext"], [1.8, 1.4, 1.8], "an update re-solves")
        stale = self.refused({"op": "patch", "id": "topo", "if_version": version, "set": {"camera": "top"}, "intent": "t"})
        self.assertEqual(stale["code"], "canvas_stale")

    def test_removing_an_object_cascades(self):
        self.ok(TOPO)
        self.ok({"op": "patch", "id": "topo", "remove": {"objects": ["api"]}, "intent": "t"})
        el = self.element()
        objects = {o["id"]: o for o in el["objects"]}
        self.assertNotIn("api", objects)
        self.assertNotIn("in", objects["api1"], "its children are free")
        self.assertNotIn("above", objects["cache"], "what was placed against it is cleared")
        self.assertNotIn("right_of", objects["db"])
        self.assertEqual([l["id"] for l in el["links"]], [], "links to it are dropped")
        self.ok({"op": "patch", "id": "topo", "add": {"links": ["lb -> db: TCP"]}, "intent": "t"})
        self.ok({"op": "patch", "id": "topo", "remove": {"links": ["lb -> db"]}, "intent": "t"})
        self.assertEqual(self.element()["links"], [])

    def test_upsert_by_id(self):
        first = self.ok(TOPO)
        again = self.ok(dict(TOPO, title="Prod topology v2", camera="top"))
        self.assertEqual(again["ids"], first["ids"])
        self.assertEqual(self.element()["text"], "Prod topology v2")
        self.assertEqual(self.element()["settings"]["camera"], "top")

    def test_the_spec_round_trips(self):
        """T-B1: ``look --block`` prints the op that rebuilds the scene exactly."""
        self.ok(TOPO)
        self.ok({"op": "patch", "id": "topo", "add": {"links": [{"from": "lb", "to": "db", "tone": "info"}]}, "intent": "t"})
        before = self.element()
        spec = K.spec(before, [], True)
        self.assertEqual(spec["objects"], before["objects"])
        self.assertEqual(spec["links"][0], "lb -> api")
        self.assertEqual(spec["links"][-1], {"from": "lb", "to": "db", "tone": "info"})
        self.ok(dict(spec, intent="rebuild"))
        after = self.element()
        for key in ("objects", "links", "settings", "solved", "text"):
            self.assertEqual(after[key], before[key], key)
        block = C.look(self.layout, self.team, "alpha-worker", block="topo")
        self.assertIn('"op": "scene3d"', C.look_text(block))


class Readback(Scene):
    def test_the_topology_reads_back_verbatim(self):
        el = M.solved_element(M.TOPOLOGY)
        el.update(id="E-60", updated_seq=7, x=800, y=480)
        self.assertEqual(K.readback(el, False), 'E-60 scene3d topo "Prod topology" [800,480 640x420] 7 objects, 3 links · camera iso · '
                                                'bounds 8.0×2.2×5.0 m')
        self.assertEqual(K.gist(el, False), [
            "base plane 8×5 (ground)",
            'lb box 1.2×0.4×1.2 "Load balancer" on base, at (−3.0, 0.0)',
            'api group row of 2 (api1, api2) "API pool" on base, right_of lb (gap 0.30)',
            'db cylinder r0.6 h1.4 "Postgres" on base, right_of api (gap 1.00)',
            'cache sphere r0.35 "Redis" above api (gap 0.30)',
            'links: lb → api · api → db "SQL" · api → cache'])
        full = K.gist(el, True)
        self.assertIn("  api1 box 0.8×1.2×0.8 in api · box (−2.1, 0.0, −0.4)–(−1.3, 1.2, 0.4) m · tone accent · 0.3 m left of api2", full)
        self.assertIn('cache sphere r0.35 "Redis" above api (gap 0.30) · box (−1.5, 1.5, −0.3)–(−0.8, 2.2, 0.3) m · tone warning · '
                      '0.3 m above api, 1.1 m above lb', full)
        stills = K.gist(el, False, {"stills": {"E-60-v7-iso.png", "E-60-v7-front.png"}})
        self.assertEqual(stills[-1], "stills iso ✓ front ✓ top ✗")
        # QA phase34 M3: the stills line never hides an object line (up to 12 are printed, 3.9).
        self.assertEqual(stills[:-1], K.gist(el, False))
        from herdr_team import canvas_kinds as R

        self.assertEqual(R.gist_of(el, False, stills={"E-60-v7-iso.png"})[-1], "stills iso ✓ front ✗ top ✗")
        self.assertNotIn("stills", " ".join(R.gist_of(el, False)), "no stills line when the reader does not know them")

    def test_the_default_view_prints_every_object_up_to_twelve(self):
        # QA phase34 M3: look's default gist cut a scene to 6 lines, hiding cache and replica in topo.
        from herdr_team import canvas_kinds as R

        objects = [{"id": "floor", "shape": "plane", "size": [30, 30]}]
        objects += [{"id": "o{}".format(i), "on": "floor", "at": [i * 2.0 - 14, 0], "label": "thing {}".format(i)} for i in range(14)]
        el = M.solved_element({"op": "scene3d", "id": "many", "intent": "t", "objects": objects, "links": ["o0 -> o1"]})
        el.update(id="E-9", updated_seq=3)
        lines = R.gist_of(el, False, stills=set())
        self.assertEqual(sum(1 for line in lines if line.startswith(("floor ", "o"))), 11)
        self.assertEqual(lines[11], "… +4 objects: canvas look --block many")
        self.assertEqual(lines[-2:], ["links: o0 → o1", "stills iso ✗ front ✗ top ✗"])
        self.assertEqual(len(R.gist_of(el, True, stills=set())), 15 + 2)

    def test_a_group_line_names_its_members_and_the_odd_tone(self):
        from herdr_team.canvas_scene3d import _describe

        el = M.solved_element({"op": "scene3d", "id": "rack", "intent": "t", "objects": [
            {"id": "nodes", "shape": "group", "layout": "stack"},
            {"id": "n1", "in": "nodes", "size": [0.9, 0.2, 0.9], "label": "gpu-1", "tone": "info"},
            {"id": "n2", "in": "nodes", "size": [0.9, 0.2, 0.9], "label": "gpu-2", "tone": "info"},
            {"id": "n3", "in": "nodes", "size": [0.9, 0.2, 0.9], "label": "gpu-3 (degraded)", "tone": "warning"}]})
        line = _describe.gist(el)[0]
        self.assertIn('group stack of 3 (n1 "gpu-1", n2 "gpu-2", n3 "gpu-3 (degraded)" warning)', line)

    def test_look_and_its_json(self):
        self.ok(TOPO)
        found = C.look(self.layout, self.team, "alpha-worker")
        eid = self.element()["id"]
        text = C.look_text(found)
        self.assertIn("7 objects, 3 links · camera iso · bounds 8.0×2.2×5.0 m", text)
        self.assertIn('    db cylinder r0.6 h1.4 "Postgres" on base, right_of api (gap 1.00)', text)
        self.assertIn("    stills iso ✗ front ✗ top ✗", found["text"], "look knows which stills the page posted (QA phase34 M3)")
        el = self.element()
        C.store_still(self.team, el["id"], el["updated_seq"], PNG, "iso")
        again = C.look(self.layout, self.team, "alpha-worker")
        self.assertIn("    stills iso ✓ front ✗ top ✗", again["text"])
        self.assertEqual(again["gist"][eid][-1], "stills iso ✓ front ✗ top ✗")
        self.assertNotIn("_stills", again)
        self.assertNotIn('"objects"', text, "gist first: the spec only with look --block")
        self.assertEqual(found["gist"][eid][0], "base plane 8×5 (ground)")
        facts = found["scene3d"][eid]
        self.assertEqual(facts["bounds"], [-4.0, 0.0, -2.5, 4.0, 2.22, 2.5])
        self.assertEqual(next(o for o in facts["objects"] if o["id"] == "cache")["rel"], "above api (gap 0.30)")
        self.assertEqual(facts["conflicts"], [])
        json.dumps(facts)
        self.assertIn("scene3d", R.slot_kinds())

    def test_notes_and_conflicts_read_back(self):
        self.ok({"op": "scene3d", "id": "crowd", "intent": "t", "objects": [{"id": "a", "size": [2, 1, 2]},
                                                                            {"id": "b", "right_of": "a", "gap": 0.3},
                                                                            {"id": "c", "right_of": "a", "gap": 0.3},
                                                                            {"id": "h", "size": [1, 1, 1], "pos": [9, 0, 0]},
                                                                            {"id": "big", "size": [2, 2, 2], "inside": "h"}]})
        lines = K.gist(self.element("crowd"), True)
        self.assertIn("notes: c overlapped b by 1.00 m; moved right 1.30", lines)
        self.assertTrue(any(line.startswith("conflict does_not_fit: big is inside h, but") for line in lines), lines)


class Checks(Scene):
    def test_intersect_with_a_fix_that_applies(self):
        self.ok({"op": "scene3d", "id": "clash", "intent": "t", "objects": [{"id": "a", "size": [2, 1, 2]}, {"id": "b", "pos": [0.5, 0.5, 0.5]}]})
        problem = self.problems("scene3d_intersect")[0]
        self.assertEqual(K.SEVERITY["scene3d_intersect"], 0, "the most serious (4.2)")
        self.assertIn("a and b intersect", problem["message"])
        self.assertEqual(problem["fix"]["update"], {"objects": [{"id": "b", "above": "a", "gap": "s", "pos": None}]})
        self.ok(dict(problem["fix"], intent="apply the fix"))
        self.assertEqual(self.problems("scene3d_intersect"), [])
        self.ok({"op": "patch", "id": "clash", "update": {"objects": [{"id": "b", "above": None, "pos": [0.5, 0.5, 0.5], "overlap": True}]},
                 "intent": "t"})
        self.assertEqual(self.problems("scene3d_intersect"), [], "overlap: true says it is meant")

    def test_labels_that_do_not_fit(self):
        many = [{"id": "o{}".format(i), "size": [0.2, 0.2, 0.2], "label": "a rather long label {}".format(i)} for i in range(14)]
        self.ok({"op": "scene3d", "id": "tiny", "intent": "t", "w": 240, "h": 180, "objects": many})
        problem = self.problems("scene3d_labels")[0]
        self.assertEqual(problem["fix"]["op"], "move")
        self.ok(dict(problem["fix"], intent="make room"))
        self.assertEqual(self.problems("scene3d_labels"), [])
        self.ok({"op": "patch", "id": "tiny", "set": {"labels": "all"}, "intent": "t"})
        self.ok({"op": "move", "id": "tiny", "w": 240, "h": 180, "intent": "t"})
        problem = self.problems("scene3d_labels")[0]
        self.assertEqual(problem["fix"]["set"], {"labels": "auto"})

    def test_the_labels_check_places_labels_without_drawing_and_bisects_its_fix(self):
        # QA phase34 M4: the check re-projected the whole scene up to six times under the canvas lock. It now places the
        # labels only (the same placement as the picture) and bisects the fix sizes: at most four placements, no drawing.
        from herdr_team.canvas_scene3d import _project as P

        many = [{"id": "o{}".format(i), "size": [0.2, 0.2, 0.2], "label": "a rather long label {}".format(i)} for i in range(14)]
        el = M.solved_element({"op": "scene3d", "id": "tiny", "intent": "t", "objects": many})
        el.update(id="E-3", updated_seq=2, x=0, y=0, w=240, h=180)
        calls = {"project": 0, "labels": 0}
        real_project, real_labels = P._project, P.labels

        def counted_project(*a: Any, **k: Any) -> Any:
            calls["project"] += 1
            return real_project(*a, **k)

        def counted_labels(*a: Any, **k: Any) -> Any:
            calls["labels"] += 1
            return real_labels(*a, **k)

        P._clear_cache()
        P._project, P.labels = counted_project, counted_labels
        try:
            found = K.labels_check(el, {})
        finally:
            P._project, P.labels = real_project, real_labels
        self.assertEqual(calls["project"], 0, "no geometry is drawn for the check")
        self.assertLessEqual(calls["labels"], 1 + 3)
        fix = found[0]["fix"]
        # The same answer as trying every size smallest first.
        first = next(f for f in K.FIX_FACTORS if not P.labels(dict(el, w=round(240 * f), h=round(180 * f)),
                                                              K.primary_view(el), K.slot_box(dict(el, w=round(240 * f), h=round(180 * f))))["dropped"])
        self.assertEqual((fix["w"], fix["h"]), (round(240 * first), round(180 * first)))
        # And the labels the check finds are the ones the picture drops.
        P._clear_cache()
        drawn = P.project(el, K.primary_view(el), K.slot_box(el))
        P._clear_cache()
        self.assertEqual(P.labels(el, K.primary_view(el), K.slot_box(el)), {"dropped": drawn["dropped"], "crowded": drawn["crowded"]})

    def test_a_relation_the_solver_could_not_keep(self):
        self.ok({"op": "scene3d", "id": "box", "intent": "t", "objects": [{"id": "cab", "size": [1, 1, 1]},
                                                                          {"id": "n", "size": [2, 0.2, 0.9], "inside": "cab"}]})
        problem = self.problems("scene3d_relation")[0]
        self.assertEqual(problem["fix"]["update"]["objects"][0]["id"], "cab")
        self.ok(dict(problem["fix"], intent="a bigger cabinet"))
        self.assertEqual(self.problems("scene3d_relation"), [])

    def test_floating_and_heavy(self):
        self.ok({"op": "scene3d", "id": "fly", "intent": "t", "objects": [{"id": "desk", "size": [2, 1, 1]}, {"id": "cup", "size": [0.2, 0.2, 0.2],
                                                                                                          "pos": [0, 2, 0]}]})
        problem = self.problems("scene3d_floating")[0]
        self.assertEqual(problem["fix"]["update"]["objects"][0], {"id": "cup", "pos": None, "on": "desk"})
        self.ok(dict(problem["fix"], intent="rest it"))
        self.assertEqual(self.problems("scene3d_floating"), [])
        el = self.element("fly")
        heavy = dict(el, solved=dict(el["solved"], models={"m": {"tris": 700_000}}))
        self.assertEqual([p["code"] for p in K.heavy_check(heavy, {})], ["scene3d_heavy"])
        self.assertEqual(K.heavy_check(el, {}), [])

    def test_every_check_is_quiet_on_the_worked_examples(self):
        art = self.artifacts()
        import shutil

        shutil.copytree(PLUGIN_ROOT / "tests" / "fixtures" / "canvas_artifacts" / "scene3d" / "models", art / "models")
        for _name, op in M.SCENES:
            self.ok(dict(op, intent="the example"))
        codes = {p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]}
        self.assertFalse({c for c in codes if c.startswith("scene3d_")}, codes)


class Perception(Scene):
    """G7: with no page still, the agent's picture of a scene is a real drawing, and its three views differ."""

    def setUp(self):
        super().setUp()
        self.resvg = shutil.which("resvg") or ("/opt/homebrew/bin/resvg" if os.path.isfile("/opt/homebrew/bin/resvg") else None)
        patcher = mock.patch.object(canvas_render, "find_resvg", return_value=self.resvg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_view_draws_ink_and_they_differ(self):
        self.ok(TOPO)
        pictures = {}
        for view in ("iso", "front", "top"):
            found = C.look(self.layout, self.team, "alpha-worker", image=True, view=view)
            svg = Path(found["svg"]).read_text(encoding="utf-8") if found.get("svg") and os.path.isfile(found["svg"]) else ""
            self.assertIn("Prod topology", svg + json.dumps(found.get("svg")), view)
            if self.resvg is None:
                pictures[view] = svg
                continue
            self.assertTrue(found.get("image"), found.get("image_error"))
            small = canvas_render.render_png(svg, Path(found["svg"]).with_suffix(".small.png"), 320)
            width, height, rows = decode_png(small.read_bytes())
            background = rows[0][0]
            inked = sum(1 for row in rows for px in row if max(abs(px[k] - background[k]) for k in range(3)) > 24)
            self.assertGreater(inked / float(width * height), 0.05, "{}: {} of {} pixels drawn".format(view, inked, width * height))
            pictures[view] = Path(found["image"]).read_bytes()
        self.assertEqual(len(set(pictures.values())), 3, "iso, front and top differ")


class Malformed(unittest.TestCase):
    def test_junk_elements_draw_and_read_back(self):
        for junk in ({}, {"id": "E-1", "type": "scene3d", "solved": "x", "objects": "y"}, {"id": "E-2", "type": "scene3d", "objects": [{"id": 3}],
                                                                                           "solved": {"objects": [["a", 1, 2]]}}):
            with self.subTest(junk=junk):
                K.readback(junk, True)
                K.gist(junk, True)
                K.emit(dict(junk, x=0, y=0, w=300, h=200), {})
                for check in (K.intersect_check, K.labels_check, K.relation_check, K.floating_check, K.heavy_check):
                    check(dict(junk, x=0, y=0, w=300, h=200), {})

    def test_the_kind_flags(self):
        kind = R.get("scene3d")
        self.assertEqual(kind.still_views, ("iso", "front", "top"))
        self.assertTrue(kind.gist_first and kind.solid and kind.cell and kind.connectable)
        self.assertEqual((kind.slot, kind.page, kind.handles), ("scene3d", False, "box"))
        self.assertEqual(kind.block.parts, "inline")
        self.assertIsNotNone(kind.block.load)
        self.assertEqual(kind.draw_view({"id": "E-1"}, "side"), None)


if __name__ == "__main__":
    unittest.main()

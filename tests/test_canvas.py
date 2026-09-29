"""The team canvas core (``herdr_team.canvas``, 0.21): operations, authority, the scene, notices, look, the CLI, sketch."""
from __future__ import annotations

import io
import json
import math
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import FAKE_AGENTS, TempState, whiteboard_on
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import canvas as C
from herdr_team import canvas_presence as P
from herdr_team import canvas_render as R
from herdr_team import canvas_text as X
from herdr_team import canvas_theme as T
from herdr_team import features as F
from herdr_team import sketch as S
from herdr_team import store, workdir
from herdr_team.errors import HerdrTeamError
from herdr_team.identity import Author

WORKER = C.CanvasAuthor("alpha-worker", "member", "cli", True, agent="claude")
REVIEWER = C.CanvasAuthor("alpha-reviewer", "member", "cli", True, agent="codex")
OPERATOR = C.CanvasAuthor("human", "human", "cli", True, operator=True)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png(width: int = 640, height: int = 480) -> bytes:
    """PNG bytes as far as the canvas reads them (magic and IHDR size); never decoded."""
    return PNG_MAGIC + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 16


def fake_resvg(argv, timeout):
    """A resvg that writes a PNG to its output path."""
    Path(argv[-1]).write_bytes(png(10, 10))
    return 0, ""


class CanvasRig(unittest.TestCase):
    """A team with the whiteboard on, and no real resvg (``find_resvg`` answers None unless a test says otherwise)."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        self.layout = self.ts.layout
        self.team = self.ts.team
        patcher = mock.patch.object(R, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    # helpers ------------------------------------------------------------

    def apply(self, ops, author=WORKER, **kw):
        return C.apply_ops(self.layout, self.team, list(ops), author, **kw)

    def ok(self, op, author=WORKER, **kw):
        result = self.apply([op], author, **kw)
        self.assertEqual(result["refused"], [], result["refused"])
        return result["applied"][0]

    def refused(self, op, author=WORKER, **kw):
        result = self.apply([op], author, **kw)
        self.assertEqual(result["applied"], [], "expected a refusal")
        return result["refused"][0]

    def proposed(self, op, author=WORKER, **kw):
        """Canvas v2 phase 5: the op became a proposal for the operator (nothing applied, nothing refused)."""
        result = self.apply([op], author, **kw)
        self.assertEqual((result["applied"], result["refused"]), ([], []), "expected a proposal")
        return result["proposed"][0]

    def scene(self):
        return C.load_scene(self.team)

    def el(self, eid):
        return next(e for e in self.scene()["elements"] if e["id"] == eid)

    def board(self):
        return store.BoardStore(self.team).read()

    def records(self, event):
        return [r for r in self.board() if r.get("event") == event]

    def set_config(self, **config):
        doc = store.read_json(self.team.team_json)
        doc.setdefault("config", {}).update(config)
        store.write_json(self.team.team_json, doc)

    def make_manager(self, name):
        doc = store.read_json(self.team.team_json)
        for member in doc["members"]:
            member["manager"] = member["name"] == name
        store.write_json(self.team.team_json, doc)

    def artifacts(self) -> Path:
        project = self.ts.tmp / "project"
        project.mkdir(exist_ok=True)
        self.set_config(project_dir=os.fspath(project))
        art = workdir.paths_for(os.fspath(project), "alpha")["artifacts"]
        art.mkdir(parents=True, exist_ok=True)
        return art

    def drivers(self):
        """The contract's worked example, minus the claim and the pen."""
        return self.apply([
            {"op": "frame", "id": "drivers", "title": "Churn drivers", "at": "c10r4", "w": 600, "h": 360, "intent": "group the drivers"},
            {"op": "shape", "id": "price", "kind": "note", "text": "Price rise in March", "inside": "drivers", "intent": "the biggest driver"},
            {"op": "shape", "id": "onboard", "kind": "note", "text": "Slow onboarding", "right_of": "price", "gap": 60, "intent": "second driver"},
            {"op": "arrow", "from": "price", "to": "onboard", "label": "worsens", "intent": "worsens"},
        ])


# --------------------------------------------------------------------------
# switches and authority


class SwitchesAndAuthority(CanvasRig):
    def test_layer_off_refuses_and_writes_nothing(self):
        F.set_layer(self.ts.session, False, "human", "cli")
        with self.assertRaises(HerdrTeamError) as ctx:
            self.apply([{"op": "shape", "text": "x", "intent": "t"}])
        self.assertEqual((ctx.exception.code, ctx.exception.details["scope"]), ("whiteboard_off", "session"))
        with self.assertRaises(HerdrTeamError) as ctx:
            C.look(self.layout, self.team, "alpha-worker")
        self.assertEqual(ctx.exception.code, "whiteboard_off")
        self.assertFalse(F.whiteboard_dir(self.team).exists(), "off changes nothing, not even a directory")

    def test_team_off_names_the_team_scope(self):
        F.set_team(self.team, enabled=False)
        with self.assertRaises(HerdrTeamError) as ctx:
            self.apply([{"op": "shape", "text": "x", "intent": "t"}])
        self.assertEqual((ctx.exception.code, ctx.exception.details["scope"]), ("whiteboard_off", "team"))

    def test_viz_off_refuses_viz_ops_only(self):
        F.set_team(self.team, viz=False)
        result = self.apply([{"op": "viz", "html": "<p>x</p>", "title": "t", "intent": "t"}, {"op": "shape", "text": "x", "intent": "t"}])
        self.assertEqual([r["code"] for r in result["refused"]], ["viz_off"])
        self.assertEqual([a["op"] for a in result["applied"]], ["shape"])

    def test_who_may_write(self):
        cases = [
            (C.CanvasAuthor("system", "system", "system", False), "author_mismatch"),
            (C.CanvasAuthor("alpha-worker", "member", "cli", False), "author_unverified"),
            (C.CanvasAuthor("human", "human", "cli", False, operator=False), "author_mismatch"),
            (C.page_author(False), "read_only"),
            (C.CanvasAuthor("beta-worker", "member", "cli", True), "not_a_member"),
        ]
        for author, code in cases:
            with self.subTest(author=author):
                with self.assertRaises(HerdrTeamError) as ctx:
                    self.apply([{"op": "shape", "text": "x", "at": "c0r0", "intent": "t"}], author)
                self.assertEqual(ctx.exception.code, code)
        applied = self.ok({"op": "shape", "text": "from the page", "at": "c0r0"}, C.page_author(True))
        self.assertEqual(self.el(applied["ids"][0])["author"], "human")
        self.assertEqual(self.el(applied["ids"][0])["author_kind"], "human")

    def test_author_from_identity(self):
        doc = store.read_json(self.team.team_json)
        doc["members"][1]["manager"] = True
        member = C.author_from_identity(Author("alpha-worker", "claude", "cli", True, team="alpha", operator=True), doc)
        self.assertEqual((member.kind, member.agent, member.manager, member.operator, member.via), ("member", "claude", True, True, "cli"))
        unverified = C.author_from_identity(Author("alpha-worker", "claude", "cli", False, team="alpha", operator=True), doc, via="mcp")
        self.assertFalse(unverified.operator, "a delegation needs a verified member")
        human = C.author_from_identity(Author("human", "human", "console", True), doc)
        self.assertTrue(human.operator)
        shell = C.author_from_identity(Author("human", "human", "cli-unverified", False), doc)
        self.assertFalse(shell.operator)
        system = C.author_from_identity(Author("system", None, "hook", True), doc)
        self.assertEqual(system.kind, "system")
        stranger = C.author_from_identity(Author("alpha-worker", "claude", "cli", True, team="beta"), doc)
        with self.assertRaises(HerdrTeamError) as ctx:
            self.apply([{"op": "shape", "at": "c0r0", "intent": "t"}], stranger)
        self.assertEqual(ctx.exception.code, "not_a_member", "a namesake from another team is not this team's member")

    def test_editors(self):
        mine = self.ok({"op": "shape", "text": "worker's", "at": "c0r0", "intent": "t"})["ids"][0]
        theirs = self.ok({"op": "shape", "text": "operator's", "at": "c20r0"}, OPERATOR)["ids"][0]
        # Phase 5 (D11, D2): a peer's edit of another member's mark, and the manager's of the operator's, are proposals now
        # (they were refused element_not_yours); nothing changes on the canvas until the operator accepts.
        proposed = self.apply([{"op": "move", "id": mine, "by": [20, 0], "intent": "t"}], REVIEWER)
        self.assertEqual((proposed["applied"], proposed["proposed"][0]["reason"]), ([], "peer"))
        self.assertEqual(self.el(mine)["x"], 0)
        self.make_manager("alpha-reviewer")
        manager = C.CanvasAuthor("alpha-reviewer", "member", "cli", True, manager=True)
        self.ok({"op": "move", "id": mine, "by": [20, 0], "intent": "tidy"}, manager)
        proposed = self.apply([{"op": "delete", "id": theirs, "intent": "t"}], manager)
        self.assertEqual((proposed["applied"], proposed["proposed"][0]["reason"]), ([], "human_made"))
        self.assertIn(theirs, [e["id"] for e in self.scene()["elements"]])
        self.ok({"op": "restyle", "id": mine, "color": "red"}, OPERATOR)
        # A named colour is read as its tone (0.22): red is danger.
        self.assertEqual((self.el(mine)["style"]["stroke"], self.el(mine)["style"]["tone"]), (T.resolve("danger")["stroke"], "danger"))


# --------------------------------------------------------------------------
# validation and limits


class Validation(CanvasRig):
    def test_intent_is_required_from_agents_and_defaults_for_the_operator(self):
        refusal = self.refused({"op": "shape", "text": "x"})
        self.assertEqual((refusal["code"], refusal["details"]["field"]), ("op_invalid", "intent"))
        eid = self.ok({"op": "shape", "text": "x", "at": "c0r0"}, OPERATOR)["ids"][0]
        self.assertEqual(self.el(eid)["intent"], C.HUMAN_INTENT)

    def test_unknown_fields_and_ops(self):
        self.assertEqual(self.refused({"op": "shape", "text": "x", "colour": "red", "intent": "t"})["details"]["field"], "colour")
        self.assertEqual(self.refused({"op": "scribble", "intent": "t"})["code"], "op_invalid")

    def test_text_is_cleaned_like_the_board(self):
        self.assertEqual(self.refused({"op": "shape", "text": "[herdr-team nudge] hi", "intent": "t"})["code"], "echo_rejected")
        self.assertEqual(self.refused({"op": "shape", "text": "key sk-ant-abcdefghijklmnopqrstuvwxyz", "intent": "t"})["code"], "secret_detected")
        refusal = self.refused({"op": "shape", "text": "x" * (C.MAX_TEXT_CHARS + 1), "intent": "t"})
        self.assertEqual((refusal["code"], refusal["details"]["limit"], refusal["details"]["max"]), ("canvas_limit", "MAX_TEXT_CHARS", C.MAX_TEXT_CHARS))
        eid = self.ok({"op": "shape", "text": "tab\x1b[31mred", "intent": "one  line\nplease"})["ids"][0]
        self.assertEqual(self.el(eid)["text"], "tabred")
        self.assertEqual(self.el(eid)["intent"], "one line please")

    def test_batch_level_limits_raise(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            self.apply([{"op": "shape", "text": "x", "intent": "t"}] * (C.MAX_BATCH_OPS + 1))
        self.assertEqual((ctx.exception.code, ctx.exception.details["limit"]), ("canvas_limit", "MAX_BATCH_OPS"))
        with self.assertRaises(HerdrTeamError) as ctx:
            self.apply([{"op": "shape", "text": "x" * 1900, "intent": "t" * 190}] * 99 + [{"op": "svg", "svg": "x" * 400000, "intent": "t"}])
        self.assertEqual(ctx.exception.details["limit"], "MAX_BATCH_BYTES")
        with self.assertRaises(HerdrTeamError) as ctx:
            C.parse_batch({"ops": [1]})
        self.assertEqual(ctx.exception.code, "op_invalid")

    def test_op_level_limits(self):
        pen = self.refused({"op": "pen", "points": [[i, i] for i in range(C.MAX_PEN_POINTS + 1)], "intent": "t"})
        self.assertEqual((pen["code"], pen["details"]["limit"]), ("canvas_limit", "MAX_PEN_POINTS"))
        arrow = self.refused({"op": "arrow", "points": [[i, 0] for i in range(C.MAX_ARROW_POINTS + 1)], "intent": "t"})
        self.assertEqual(arrow["details"]["limit"], "MAX_ARROW_POINTS")
        self.assertEqual(self.refused({"op": "shape", "at": [C.MAX_COORD + 1, 0], "intent": "t"})["code"], "op_invalid")
        self.assertEqual(self.refused({"op": "shape", "at": "c0r0", "w": C.MAX_SIZE + 1, "intent": "t"})["code"], "op_invalid")
        with mock.patch.object(C, "MAX_ELEMENTS", 2):
            result = self.apply([{"op": "shape", "at": "c0r0", "intent": "t"}, {"op": "shape", "at": "c20r0", "intent": "t"},
                                 {"op": "shape", "at": "c40r0", "intent": "t"}])
        self.assertEqual([r["code"] for r in result["refused"]], ["canvas_limit"])
        self.assertEqual(result["refused"][0]["details"]["limit"], "MAX_ELEMENTS")
        with mock.patch.object(C, "MAX_LEGEND", 1):
            self.ok({"op": "legend", "symbol": "red cross", "meaning": "no", "intent": "t"})
            self.assertEqual(self.refused({"op": "legend", "symbol": "tick", "meaning": "yes", "intent": "t"})["details"]["limit"], "MAX_LEGEND")

    def test_rate_limit_is_per_agent_and_rolls(self):
        now = 1_800_000_000.0
        with mock.patch.object(C, "MAX_OPS_PER_MINUTE", 5):
            self.apply([{"op": "shape", "at": "c0r0", "intent": "t"}] * 3, now=now)
            with self.assertRaises(HerdrTeamError) as ctx:
                self.apply([{"op": "shape", "at": "c0r0", "intent": "t"}] * 3, now=now + 10)
            self.assertEqual(ctx.exception.code, "canvas_rate")
            self.assertEqual(ctx.exception.details["retry_after_s"], 50)
            self.apply([{"op": "shape", "at": "c0r0", "intent": "t"}] * 3, REVIEWER, now=now + 10)
            self.apply([{"op": "shape", "at": "c0r0"}] * 6, OPERATOR, now=now + 10)
            self.apply([{"op": "shape", "at": "c0r0", "intent": "t"}] * 3, now=now + 61)


# --------------------------------------------------------------------------
# space, placement, ids


class SpaceAndIds(CanvasRig):
    def test_cells_points_regions(self):
        self.assertEqual(C.cell_name(340, 120), "c17r6")
        self.assertEqual(C.cell_name(-41, 5), "c-3r0")
        self.assertEqual(C.parse_point("c17r6"), [340.0, 120.0])
        self.assertEqual(C.parse_point("c-3r5"), [-60.0, 100.0])
        self.assertEqual(C.parse_point("10.5, 20"), [10.5, 20.0])
        self.assertEqual(C.parse_point([1, 2]), [1.0, 2.0])
        self.assertEqual(C.parse_region("c10r4:c40r22"), [200.0, 80.0, 800.0, 440.0])
        self.assertEqual(C.parse_region("800,440,200,80"), [200.0, 80.0, 800.0, 440.0])
        self.assertEqual(C.parse_region([0, 0, 10, 5]), [0.0, 0.0, 10.0, 5.0])
        for bad in ("c1r1:c1r1", "nowhere", [1, 2, 3]):
            with self.assertRaises(HerdrTeamError):
                C.parse_region(bad)
        self.drivers()
        scene = self.scene()
        self.assertEqual(C.parse_point("price", scene, "alpha-worker"), [310.0, 180.0])
        self.assertEqual(C.parse_region("E-1", scene), [200.0, 80.0, 800.0, 440.0])

    def test_homes_and_first_free_slots(self):
        first = self.ok({"op": "shape", "text": "one", "intent": "t"})["ids"][0]
        second = self.ok({"op": "shape", "text": "two", "intent": "t"})["ids"][0]
        other = self.ok({"op": "shape", "text": "three", "intent": "t"}, REVIEWER)["ids"][0]
        scene = self.scene()
        self.assertEqual(scene["homes"], {"alpha-worker": [0, -1000, 800, -400], "alpha-reviewer": [1000, -1000, 1800, -400]})
        self.assertEqual(scene["authors"]["alpha-worker"]["color"], C.AUTHOR_PALETTE[0])
        self.assertEqual(scene["authors"]["alpha-reviewer"]["color"], C.AUTHOR_PALETTE[1])
        a, b, c = self.el(first), self.el(second), self.el(other)
        corner = (0, -1000, C.PORTRAIT_W, -1000 + 184)
        for el in (a, b):
            self.assertTrue(C._contains((0, -1000, 800, -400), C.bounds(el)))
            self.assertFalse(C._intersects(corner, C.bounds(el)), "the portrait corner stays free")
        self.assertFalse(C._intersects(C.bounds(a), C.bounds(b)))
        self.assertGreaterEqual(c["x"], 1000)
        self.assertEqual(a["style"]["stroke"], T.resolve("neutral")["stroke"], "authorship is not a colour (0.22)")

    def test_the_operator_draws_without_coordinates_too(self):
        """QA phase 6 F4: hers was the only author whose op had to carry coordinates. Her index is -1, so her home
        lane sits immediately left of the first member's, and an unanchored op lands in it like any member's."""
        self.ok({"op": "shape", "text": "one", "intent": "t"})  # a member takes lane 0 first
        ident = self.ok({"op": "shape", "text": "mine", "intent": "t"}, OPERATOR)["ids"][0]
        scene = self.scene()
        self.assertEqual(scene["homes"]["human"], [-1000, -1000, -200, -400])
        self.assertTrue(C._contains((-1000, -1000, -200, -400), C.bounds(self.el(ident))))
        self.assertFalse(C._intersects((-1000, -1000, -200, -400), (0, -1000, 800, -400)), "the lanes do not overlap")
        second = self.ok({"op": "shape", "text": "again", "intent": "t"}, OPERATOR)["ids"][0]
        self.assertFalse(C._intersects(C.bounds(self.el(ident)), C.bounds(self.el(second))))

    def test_a_home_is_registered_even_when_the_first_op_carries_coordinates(self):
        """The home does not depend on the op that registers the author needing it: a board whose operator has only
        ever drawn at explicit points still has her lane, so her next unanchored op is not refused."""
        self.ok({"op": "shape", "text": "placed", "at": [4000, 400], "intent": "t"}, OPERATOR)
        self.assertEqual(self.scene()["homes"]["human"], [-1000, -1000, -200, -400])
        self.ok({"op": "shape", "text": "free", "intent": "t"}, OPERATOR)

    def test_relative_placement_and_frames(self):
        result = self.drivers()
        self.assertEqual(result["aliases"], {"drivers": "E-1", "price": "E-2", "onboard": "E-3"})
        price, onboard, arrow = self.el("E-2"), self.el("E-3"), self.el("E-4")
        self.assertEqual((price["x"], price["y"], price["frame"]), (220, 120, "E-1"))
        # The arrow's "worsens" label did not fit the 60-unit gap, so the later note moved a grid step right (QA R-3).
        self.assertEqual((onboard["x"], onboard["y"], onboard["frame"]), (520, 120, "E-1"))
        self.assertEqual((arrow["from"], arrow["to"], arrow["frame"]), ("E-2", "E-3", "E-1"))
        below = self.el(self.ok({"op": "shape", "text": "b", "below": "price", "intent": "t"})["ids"][0])
        self.assertEqual((below["x"], below["y"]), (220, 280))
        left = self.el(self.ok({"op": "shape", "text": "l", "left_of": "E-1", "gap": 20, "intent": "t"})["ids"][0])
        self.assertEqual((left["x"], left["y"]), (200 - 20 - 160, 80))
        above = self.el(self.ok({"op": "shape", "text": "a", "above": "E-1", "intent": "t"})["ids"][0])
        self.assertEqual(above["y"], 80 - 40 - 80)
        self.assertEqual(self.refused({"op": "shape", "at": "c0r0", "below": "E-1", "intent": "t"})["code"], "op_invalid")

    def test_inside_a_full_frame_grows_it(self):
        frame = self.ok({"op": "frame", "title": "small", "at": "c0r0", "w": 220, "h": 180, "intent": "t"})["ids"][0]
        self.ok({"op": "shape", "text": "one", "inside": frame, "intent": "t"})
        applied = self.ok({"op": "shape", "text": "two", "inside": frame, "intent": "t"})
        self.assertIn(frame, applied["ids"], "the frame's growth is part of the same event")
        two = self.el(applied["ids"][0])
        self.assertEqual(two["frame"], frame)
        self.assertTrue(C._contains(C.bounds(self.el(frame)), C.bounds(two)))

    def test_ids_and_aliases(self):
        self.drivers()
        self.assertEqual(self.refused({"op": "shape", "id": "price", "text": "again", "intent": "t"})["code"], "alias_taken")
        self.assertEqual(self.refused({"op": "shape", "id": "E-9", "intent": "t"})["code"], "op_invalid")
        self.assertEqual(self.refused({"op": "shape", "id": "c3r4", "intent": "t"})["code"], "op_invalid")
        # another author's unique alias resolves; the reviewer's own takes precedence once it has one
        self.ok({"op": "comment", "at": "price", "text": "why?", "intent": "t"}, REVIEWER)
        mine = self.ok({"op": "shape", "id": "price", "text": "mine", "at": "c0r30", "intent": "t"}, REVIEWER)["ids"][0]
        self.assertEqual(self.ok({"op": "edit", "id": "price", "text": "mine!", "intent": "t"}, REVIEWER)["ids"], [mine])
        refusal = self.refused({"op": "comment", "at": "price", "text": "which one?", "intent": "t"}, OPERATOR)
        self.assertEqual(refusal["code"], "element_unknown")
        self.assertEqual({c["author"] for c in refusal["details"]["candidates"]}, {"alpha-worker", "alpha-reviewer"})
        self.ok({"op": "delete", "id": "E-3", "intent": "t"})
        new = self.ok({"op": "shape", "text": "later", "at": "c0r40", "intent": "t", "client_id": "xq3"})
        self.assertNotEqual(new["ids"], ["E-3"], "ids are never reused")
        self.assertEqual(self.apply([{"op": "shape", "at": "c0r50", "client_id": "Xq3kPnVJ9a"}], OPERATOR)["aliases"], {"Xq3kPnVJ9a": "E-{}".format(C._id_number(new["ids"][0]) + 1)})


# --------------------------------------------------------------------------
# the operations


class Operations(CanvasRig):
    def test_shape_defaults_and_style(self):
        box = self.el(self.ok({"op": "shape", "at": "c0r0", "intent": "t"})["ids"][0])
        # 0.22: neutral tone (a white surface), the sans font, straight lines; the size is the kind's minimum.
        self.assertEqual((box["type"], box["w"], box["h"], box["style"]["fill"]), ("box", 160, 80, "#ffffff"))
        self.assertEqual({k: box["style"][k] for k in ("tone", "variant", "font", "rough")}, {"tone": "neutral", "variant": "soft", "font": "normal", "rough": 0})
        self.assertEqual(box["fit"], {"policy": "hug", "min": [160, 80], "size": 20, "lines": [], "truncated": False, "estimated": False})
        note = self.el(self.ok({"op": "shape", "kind": "note", "at": "c0r10", "intent": "t"})["ids"][0])
        self.assertEqual((note["w"], note["h"], note["style"]["fill"], note["style"]["tone"]), (180, 120, T.resolve("idea", "soft", "note")["fill"], "idea"))
        text = self.el(self.ok({"op": "shape", "kind": "text", "text": "hello", "at": "c0r20", "size": "l", "intent": "t"})["ids"][0])
        self.assertEqual((text["w"], text["h"], text["style"]["size"]), (int(math.ceil(X.measure("hello", size=28).width)), 35, 28))
        styled = self.el(self.ok({"op": "shape", "at": "c0r30", "color": "#ABCDEF", "fill": "blue", "width": "extra", "dash": "dashed",
                                  "opacity": 50, "font": "code", "rough": 1, "intent": "t"})["ids"][0])
        self.assertEqual(styled["style"], {"stroke": "#abcdef", "fill": "#a5d8ff", "text": T.base()["ink"], "tone": None, "variant": "soft",
                                           "width": 4, "dash": "dashed", "opacity": 50, "font": "code", "size": 20, "rough": 1})
        toned = self.el(self.ok({"op": "shape", "at": "c0r40", "tone": "warning", "variant": "solid", "intent": "t"})["ids"][0])
        self.assertEqual({k: toned["style"][k] for k in ("stroke", "fill", "text")}, T.resolve("warning", "solid", "box"))
        self.assertEqual(self.refused({"op": "shape", "at": "c0r0", "tone": "mauve", "intent": "t"})["details"]["field"], "tone")
        self.assertEqual(self.refused({"op": "shape", "at": "c0r0", "color": "mauve", "intent": "t"})["details"]["field"], "color")
        self.assertEqual(self.refused({"op": "shape", "at": "c0r0", "width": 3, "intent": "t"})["details"]["field"], "width")
        keys = list(box)
        self.assertEqual(keys[:len(keys)], ["id", "type", "alias", "client_id", "x", "y", "w", "h", "text", "style", "frame", "group", "role", "z",
                                            "author", "author_kind", "intent", "batch", "created_seq", "updated_seq", "created_at", "updated_at", "fit"])

    def test_arrows_bind_follow_and_unbind(self):
        self.drivers()
        arrow = self.el("E-4")
        self.assertEqual(arrow["points"], [[404, 180], [516, 180]], "centre to centre, clipped to each end plus a gap")
        self.assertEqual(arrow["label_at"], [460, 180], "the label sits on the line, between the notes")
        moved = self.ok({"op": "move", "id": "onboard", "by": [0, 200], "intent": "t"})
        self.assertIn("E-4", moved["ids"], "the bound arrow re-routes in the same event")
        self.assertNotEqual(self.el("E-4")["points"], arrow["points"])
        self.ok({"op": "delete", "id": "onboard", "intent": "t"})
        after = self.el("E-4")
        self.assertIsNone(after["to"])
        self.assertEqual(after["from"], "E-2")
        free = self.el(self.ok({"op": "arrow", "points": ["c0r0", "c5r0", [100, 100]], "head": "triangle", "tail": "dot", "intent": "t"})["ids"][0])
        self.assertEqual((free["from"], free["to"], free["points"], free["head"], free["tail"]), (None, None, [[0, 0], [100, 0], [100, 100]], "triangle", "dot"))
        mixed = self.el(self.ok({"op": "arrow", "from": "c0r40", "to": "price", "intent": "t"})["ids"][0])
        self.assertEqual((mixed["from"], mixed["to"], mixed["points"][0]), (None, "E-2", [0, 800]))
        self.assertEqual(self.refused({"op": "arrow", "from": "price", "intent": "t"})["details"]["field"], "to")

    def test_frames_from_children_and_regions(self):
        a = self.ok({"op": "shape", "text": "a", "at": "c0r0", "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "b", "at": "c10r0", "intent": "t"})["ids"][0]
        frame = self.el(self.ok({"op": "frame", "title": "pair", "children": [a, b], "intent": "t"})["ids"][0])
        self.assertEqual((frame["x"], frame["y"], frame["w"], frame["h"]), (-20, -40, 400, 140))
        self.assertEqual((self.el(a)["frame"], self.el(b)["frame"]), (frame["id"], frame["id"]))
        self.ok({"op": "move", "id": frame["id"], "by": [100, 0], "intent": "t"})
        self.assertEqual((self.el(a)["x"], self.el(b)["x"]), (100, 300), "moving a frame moves its children")
        theirs = self.ok({"op": "shape", "text": "c", "at": "c0r20", "intent": "t"}, REVIEWER)["ids"][0]
        # Framing a peer's mark changes it: a proposal for the operator since phase 5 (it was refused element_not_yours).
        self.assertEqual(self.apply([{"op": "frame", "children": [theirs], "intent": "t"}])["proposed"][0]["reason"], "peer")
        region = self.el(self.ok({"op": "frame", "title": "area", "region": [-100, 380, 400, 600], "intent": "t"}, OPERATOR)["ids"][0])
        self.assertEqual(self.el(theirs)["frame"], region["id"], "a region frame adopts what lies wholly inside")

    def test_pen_path_svg(self):
        pen = self.el(self.ok({"op": "pen", "points": ["c0r0", "c5r2", [120, 10, 0.4]], "closed": True, "fill": "red", "intent": "t"})["ids"][0])
        self.assertEqual((pen["x"], pen["y"], pen["w"], pen["h"], pen["closed"], pen["smooth"]), (0, 0, 120, 40, True, True))
        self.assertEqual(pen["points"][2], [120, 10, 0.4])
        self.assertEqual(self.refused({"op": "pen", "points": ["c0r0", "c1r1"], "fill": "red", "intent": "t"})["details"]["field"], "fill")
        path = self.el(self.ok({"op": "path", "d": "M100 100 l50 0 l0 25 z", "scale": 2, "at": "c0r10", "intent": "t"})["ids"][0])
        self.assertEqual((path["d"], path["w"], path["h"], path["scale"], path["x"]), ("M0 0 L50 0 L50 25 Z", 100, 50, 2, 0))
        self.assertEqual(self.refused({"op": "path", "d": "M0 0 <script>", "intent": "t"})["code"], "op_invalid")
        svg = self.el(self.ok({"op": "svg", "svg": "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 60'><circle r='5'/></svg>",
                               "title": "dot", "at": "c0r20", "intent": "t"})["ids"][0])
        self.assertEqual((svg["w"], svg["h"], svg["text"]), (120, 60, "dot"))
        stored = C.asset_path(self.team, svg["asset"]).read_text()
        self.assertTrue(stored.startswith('<svg xmlns="http://www.w3.org/2000/svg"'))
        refusal = self.refused({"op": "svg", "svg": "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "intent": "t"})
        self.assertEqual((refusal["code"], refusal["details"]["reason"]), ("svg_refused", "element:script"))

    def test_graph(self):
        applied = self.ok({"op": "graph", "id": "deps", "title": "Dependencies", "at": "c0r0", "intent": "map",
                           "nodes": [{"id": "a", "text": "API"}, {"id": "b", "text": "DB", "kind": "ellipse", "fill": "green"}, "c"],
                           "edges": [{"from": "a", "to": "b", "label": "reads"}, ["a", "c"], {"from": "c", "to": "c", "dash": True}]})
        self.assertEqual(applied["alias"], "deps")
        scene = self.scene()
        made = [e for e in scene["elements"] if e["id"] in applied["ids"]]
        self.assertEqual([e["type"] for e in made], ["frame", "box", "ellipse", "box", "arrow", "arrow", "arrow"])
        frame = made[0]
        self.assertEqual((frame["text"], frame["alias"]), ("Dependencies", "deps"))
        self.assertTrue(all(e["group"] == frame["id"] and e["frame"] == frame["id"] for e in made[1:]))
        self.assertEqual([e["alias"] for e in made[1:4]], ["deps.a", "deps.b", "deps.c"])
        self.assertEqual(made[2]["style"]["fill"], C.FILLS["green"])
        self.assertEqual(made[4]["text"], "reads")
        self.assertEqual(made[6]["style"]["dash"], "dashed")
        self.assertEqual(len(made[6]["points"]), 5, "a self-loop goes around the node")
        self.assertEqual(self.el(C.parse_point("deps.a", scene, "alpha-worker") and "E-2")["text"], "API")
        self.assertEqual(self.refused({"op": "graph", "nodes": ["a"], "edges": [["a", "z"]], "intent": "t"})["details"]["field"], "edges[0].to")
        # Phase 1: the graph op and its limits live in its kind module (canvas_kinds.diagram); canvas re-exports them.
        from herdr_team.canvas_kinds import diagram
        with mock.patch.object(diagram, "MAX_GRAPH_NODES", 2):
            self.assertEqual(self.refused({"op": "graph", "nodes": ["a", "b", "c"], "intent": "t"})["details"]["limit"], "MAX_GRAPH_NODES")
        for layout in ("radial", "force", "grid"):
            self.ok({"op": "graph", "nodes": ["x", "y", "z"], "edges": [["x", "y"], ["y", "z"]], "layout": layout, "at": "c100r0", "intent": layout})

    def test_mermaid(self):
        flow = self.ok({"op": "mermaid", "id": "flow", "source": "flowchart LR\n A[Start] --> B{ok?}\n subgraph s1 [Inner]\n C\n end\n B -- yes --> C",
                        "at": "c0r0", "intent": "flow"})
        made = [self.el(i) for i in flow["ids"]]
        self.assertEqual([e["type"] for e in made], ["frame", "frame", "box", "diamond", "box", "arrow", "arrow"])
        self.assertEqual(made[1]["text"], "Inner")
        self.assertEqual(made[4]["frame"], made[1]["id"], "a subgraph is a nested frame")
        self.assertEqual(made[6]["text"], "yes")
        seq = self.el(self.ok({"op": "mermaid", "source": "sequenceDiagram\n A->>B: hi", "title": "Hello", "at": "c0r30", "intent": "t"})["ids"][0])
        self.assertEqual((seq["type"], seq["diagram"], seq["w"], seq["h"], seq["still"]), ("mermaid", "sequence", 480, 320, None))
        odd = self.el(self.ok({"op": "mermaid", "source": "graph TD\n A --> B\n A ~~~ B", "at": "c0r60", "intent": "t"})["ids"][0])
        self.assertEqual((odd["type"], odd["diagram"]), ("mermaid", "other"), "a flowchart outside the subset stays a page-rendered element")
        self.assertEqual(self.refused({"op": "mermaid", "source": "graph TD\n A --> B\n click A \"https://x\"", "intent": "t"})["code"], "op_invalid")

    def test_chart_and_its_data(self):
        self.assertEqual(self.refused({"op": "chart", "spec": {"mark": "bar"}, "data": "churn.csv", "intent": "t"})["code"], "artifacts_unset")
        art = self.artifacts()
        (art / "churn.csv").write_text("month,churn\n1,0.1\n")
        chart = self.el(self.ok({"op": "chart", "spec": {"mark": "line"}, "data": "artifacts/churn.csv", "title": "Churn", "at": "c0r0", "intent": "t"})["ids"][0])
        self.assertEqual((chart["data"], chart["text"], chart["w"]), ("churn.csv", "Churn", 480))
        self.assertEqual(json.loads(C.asset_path(self.team, chart["spec_asset"]).read_text()), {"mark": "line"})
        refusal = self.refused({"op": "chart", "spec": {"data": {"url": "https://evil/x.csv"}, "mark": "bar"}, "intent": "t"})
        self.assertEqual((refusal["code"], refusal["details"]["key"]), ("chart_refused", "url"))
        self.assertEqual(self.refused({"op": "chart", "spec": {}, "data": "../../etc/passwd", "intent": "t"})["code"], "path_refused")
        (art / "notes.txt").write_text("x")
        self.assertEqual(self.refused({"op": "chart", "spec": {}, "data": "notes.txt", "intent": "t"})["code"], "path_refused")

    def test_viz(self):
        viz = self.el(self.ok({"op": "viz", "html": "<svg id='x'></svg><script>d3.select('#x')</script>", "libs": ["d3", "d3"],
                               "data": {"series": [1, 2]}, "title": "Live", "at": "c0r0", "intent": "t"})["ids"][0])
        self.assertEqual((viz["libs"], viz["data"], viz["w"], viz["h"]), (["d3"], {"series": [1, 2]}, 480, 360))
        source = C.viz_source(self.team, viz["id"])
        self.assertEqual((source["title"], source["libs"], source["version"]), ("Live", ["d3"], viz["updated_seq"]))
        self.assertIn("d3.select", source["html"])
        self.assertEqual(self.refused({"op": "viz", "html": "<p/>", "intent": "t"})["details"]["field"], "title")
        self.assertEqual(self.refused({"op": "viz", "html": "<p/>", "title": "t", "libs": ["jquery"], "intent": "t"})["details"]["field"], "libs")
        with self.assertRaises(HerdrTeamError):
            C.viz_source(self.team, "E-999")

    def test_images(self):
        art = self.artifacts()
        (art / "plot.bin").write_bytes(png(960, 480))
        image = self.el(self.ok({"op": "image", "path": "plot.bin", "at": "c0r0", "intent": "t"})["ids"][0])
        self.assertEqual((image["mime"], image["px_w"], image["px_h"], image["w"], image["h"]), ("image/png", 960, 480, 480, 240))
        self.assertTrue(C.asset_path(self.team, image["asset"]).read_bytes().startswith(PNG_MAGIC))
        (art / "fake.png").write_bytes(b"GIF89a....")
        self.assertEqual(self.refused({"op": "image", "path": "fake.png", "intent": "t"})["code"], "image_refused")
        outside = self.ts.tmp / "elsewhere.png"
        outside.write_bytes(png())
        self.assertEqual(self.refused({"op": "image", "path": os.fspath(outside), "intent": "t"})["code"], "path_refused")
        self.assertEqual(self.refused({"op": "image", "asset": image["asset"], "intent": "t"})["code"], "operator_only")
        self.ok({"op": "image", "asset": image["asset"], "at": "c30r0"}, OPERATOR)

    def test_move_restyle_edit_delete_and_stale_versions(self):
        self.drivers()
        version = self.el("E-2")["updated_seq"]
        self.ok({"op": "edit", "id": "price", "text": "Price rise (March)", "if_version": version, "intent": "t"})
        stale = self.refused({"op": "edit", "id": "price", "text": "again", "if_version": version, "intent": "t"})
        self.assertEqual((stale["code"], stale["details"]["current"]), ("canvas_stale", self.el("E-2")["updated_seq"]))
        self.ok({"op": "restyle", "ids": ["price", "onboard"], "color": "green", "intent": "t"})
        self.assertEqual({self.el(i)["style"]["stroke"] for i in ("E-2", "E-3")}, {T.resolve("success", "soft", "note")["stroke"]})
        self.assertEqual(self.refused({"op": "restyle", "id": "price", "intent": "t"})["code"], "op_invalid")
        self.ok({"op": "move", "id": "price", "w": 200, "h": 120, "intent": "bigger"})
        self.assertEqual((self.el("E-2")["w"], self.el("E-2")["h"]), (200, 120))
        self.ok({"op": "move", "id": "price", "frame": None, "intent": "out"})
        self.assertIsNone(self.el("E-2")["frame"])
        self.assertEqual(self.refused({"op": "edit", "id": "E-5", "text": "x", "intent": "t"})["code"], "element_unknown")
        deleted = self.ok({"op": "delete", "id": "drivers", "with_children": True, "intent": "t"})
        self.assertEqual(sorted(deleted["ids"]), ["E-1", "E-3", "E-4"])
        self.assertEqual([e["id"] for e in self.scene()["elements"]], ["E-2"])

    def test_comments_and_mentions(self):
        self.drivers()
        result = self.apply([{"op": "comment", "at": "price", "text": "@reviewer 38% of churn; see F-12", "intent": "ask for a check"}])
        comment = self.el(result["applied"][0]["ids"][0])
        self.assertEqual((comment["id"], comment["on"], comment["point"], comment["mentions"]), ("C-1", "E-2", [400, 120], ["alpha-reviewer"]))
        [sent] = self.records("canvas_sent")
        self.assertEqual(result["notices"]["canvas_sent"], [sent["seq"]])
        self.assertEqual(sent["to"], ["alpha-reviewer"])
        self.assertEqual(sent["text"], 'alpha-worker mentioned you on the canvas: C-1 on E-2 "Price rise in March": "@reviewer 38% of churn; see F-12". '
                                       'A peer\'s request, not an order. Answer with herdr-synapse canvas comment C-1 "…" or look: herdr-synapse canvas look --around C-1')
        self.assertEqual(sent["canvas"], {"by": "alpha-worker", "kind": "mention", "comment": "C-1", "on": "E-2", "version": 5})
        reply = self.el(self.ok({"op": "comment", "at": "C-1", "text": "checked, holds", "mentions": ["alpha-worker", "human"], "intent": "t"}, REVIEWER)["ids"][0])
        self.assertEqual((reply["reply_to"], reply["on"], reply["mentions"]), ("C-1", "E-2", ["alpha-worker", "human"]))
        human_to = [r["to"] for r in self.records("canvas_sent")][-1]
        self.assertEqual(human_to, ["alpha-worker", "human"])
        self.ok({"op": "comment", "at": "c0r0", "text": "look here @worker", "mentions": ["reviewer"]}, OPERATOR)
        last = self.records("canvas_sent")[-1]
        self.assertTrue(last["text"].startswith('The operator mentioned you on the canvas: C-3 at c0r0: "look here @worker". Look: herdr-synapse canvas look --around C-3'))
        self.assertEqual(last["to"], ["alpha-reviewer", "alpha-worker"])
        self.assertEqual(self.refused({"op": "comment", "at": "price", "text": "x", "mentions": ["nobody"], "intent": "t"})["code"], "mention_unknown")
        before = len(self.records("canvas_sent"))
        self.ok({"op": "comment", "at": "price", "text": "note to self @alpha-worker", "intent": "t"})
        self.assertEqual(len(self.records("canvas_sent")), before, "mentioning yourself wakes nobody")

    def test_resolve(self):
        self.drivers()
        self.ok({"op": "comment", "at": "price", "text": "@alpha-reviewer check", "intent": "t"})
        self.ok({"op": "comment", "at": "onboard", "text": "unrelated", "intent": "t"})
        self.ok({"op": "resolve", "id": "C-1", "intent": "done"}, REVIEWER)
        self.assertEqual((self.el("C-1")["resolved"], self.el("C-1")["resolved_by"]), (True, "alpha-reviewer"))
        self.assertEqual(self.refused({"op": "resolve", "id": "C-2", "intent": "t"}, REVIEWER)["code"], "element_not_yours")
        self.ok({"op": "resolve", "id": "C-2"}, OPERATOR)
        self.assertEqual(self.refused({"op": "resolve", "id": "E-2", "intent": "t"})["code"], "element_unknown")

    def test_claims_expire_cap_warn_and_release(self):
        now = time.time()
        first = self.ok({"op": "claim", "region": "c0r0:c10r10", "label": "one", "intent": "t"}, now=now)["ids"]
        self.ok({"op": "claim", "region": "c20r0:c30r10", "label": "two", "intent": "t"}, now=now + 1)
        self.ok({"op": "claim", "region": "c40r0:c50r10", "label": "three", "intent": "t"}, now=now + 2)
        fourth = self.ok({"op": "claim", "region": "c60r0:c70r10", "label": "four", "intent": "t"}, now=now + 3)
        self.assertEqual(fourth["ids"], ["K-4", first[0]], "the oldest claim is released in the same event")
        self.assertEqual([c["id"] for c in self.scene()["claims"]], ["K-2", "K-3", "K-4"])
        # Phase 5 (4.1): a plain member's new mark in another's claim is a proposal; the manager's applies with a warning.
        result = self.apply([{"op": "shape", "text": "inside", "at": "c21r1", "intent": "t"}], REVIEWER, now=now + 4)
        self.assertEqual((result["applied"], result["proposed"][0]["reason"]), ([], "foreign_lane"))
        self.make_manager("alpha-reviewer")
        manager = C.CanvasAuthor("alpha-reviewer", "member", "cli", True, manager=True)
        result = self.apply([{"op": "shape", "text": "inside", "at": "c21r1", "intent": "t"}], manager, now=now + 4)
        self.assertEqual(result["warnings"][0]["code"], "inside_claim")
        self.assertEqual(result["warnings"][0]["message"], "E-2 is inside K-2 claimed by alpha-worker: two")  # E-1 is the proposal's, reserved
        self.assertEqual(self.refused({"op": "release", "id": "K-2", "intent": "t"}, REVIEWER, now=now + 5)["code"], "element_not_yours")
        self.ok({"op": "release", "id": "K-2", "intent": "t"}, now=now + 5)
        self.ok({"op": "release", "id": "K-3"}, OPERATOR, now=now + 5)
        self.ok({"op": "release", "intent": "done"}, now=now + 6)
        self.assertEqual(self.scene()["claims"], [])
        self.ok({"op": "claim", "region": "c0r0:c10r10", "label": "stale", "intent": "t"}, now=now - C.CLAIM_TTL_S - 1)
        self.assertEqual(self.scene()["claims"], [], "an expired claim is dropped when read")
        self.assertEqual(self.refused({"op": "release", "intent": "t"}, now=now)["code"], "element_unknown")

    def test_locks_refuse_everyone_but_the_operator(self):
        self.assertEqual(self.refused({"op": "lock", "region": "c0r0:c20r10", "intent": "t"})["code"], "operator_only")
        lock = self.ok({"op": "lock", "region": "c0r0:c20r10", "label": "hands off"}, OPERATOR)["ids"][0]
        refusal = self.refused({"op": "shape", "text": "x", "at": "c1r1", "intent": "t"})
        self.assertEqual((refusal["code"], refusal["details"]["lock"], refusal["message"]), ("canvas_locked", lock, "c1r1 is inside X-1 locked by the operator"))
        outside = self.ok({"op": "shape", "text": "x", "at": "c30r30", "intent": "t"})["ids"][0]
        self.assertEqual(self.refused({"op": "move", "id": outside, "to": "c2r2", "intent": "t"})["code"], "canvas_locked")
        self.assertEqual(self.refused({"op": "claim", "region": "c5r5:c8r8", "label": "x", "intent": "t"})["code"], "canvas_locked")
        self.ok({"op": "shape", "text": "mine", "at": "c1r1"}, OPERATOR)
        self.ok({"op": "unlock", "id": lock}, OPERATOR)
        self.ok({"op": "shape", "text": "x", "at": "c10r6", "intent": "t"})  # beside the operator's mark (on it would be a proposal)

    def test_legend(self):
        self.drivers()
        mine = self.ok({"op": "legend", "symbol": "E-4", "meaning": "a causal link", "intent": "t"})["ids"][0]
        self.ok({"op": "legend", "symbol": "red cross", "meaning": "I disagree", "intent": "t"}, REVIEWER)
        self.assertEqual([(g["symbol"], g["meaning"]) for g in self.scene()["legend"]], [("E-4", "a causal link"), ("red cross", "I disagree")])
        self.assertEqual(self.refused({"op": "legend", "remove": mine, "intent": "t"}, REVIEWER)["code"], "element_not_yours")
        self.make_manager("alpha-reviewer")
        self.ok({"op": "legend", "remove": mine, "intent": "t"}, C.CanvasAuthor("alpha-reviewer", "member", "cli", True, manager=True))
        self.assertEqual(self.refused({"op": "legend", "symbol": "E-99", "meaning": "x", "intent": "t"})["code"], "element_unknown")

    def test_portrait(self):
        first = self.ok({"op": "portrait", "steps": ["fetch", "clean", {"text": "plot", "status": "pending"}], "current": 2, "intent": "my plan"})
        scene = self.scene()
        frame = next(e for e in scene["elements"] if e.get("role") == "portrait" and e["type"] == "frame")
        steps = sorted((e for e in scene["elements"] if e.get("group") == frame["id"]), key=lambda e: e["y"])
        self.assertEqual((frame["x"], frame["y"], frame["w"], frame["text"]), (0, -1000, 360, "alpha-worker's plan"))
        self.assertEqual([s["text"] for s in steps], ["✓ fetch", "clean", "plot"])
        self.assertEqual([s["style"]["fill"] for s in steps], [C.FILLS["gray"], C.AUTHOR_FILLS[0], T.base()["surface"]])
        self.assertEqual(steps[1]["style"]["width"], 4)
        second = self.ok({"op": "portrait", "steps": [{"text": "fetch", "status": "completed"}, {"text": "ship", "status": "in_progress"}], "intent": "t"})
        scene = self.scene()
        self.assertEqual(second["ids"][0], frame["id"], "re-running keeps the frame id")
        self.assertEqual(sorted(e["text"] for e in scene["elements"] if e.get("group") == frame["id"]), ["ship", "✓ fetch"])
        self.assertIn(first["ids"][-1], second["ids"], "the extra step was deleted")
        self.assertEqual(self.refused({"op": "portrait", "steps": ["x"]}, OPERATOR)["code"], "op_invalid")
        with mock.patch.object(C, "MAX_PORTRAIT_STEPS", 2):
            self.assertEqual(self.refused({"op": "portrait", "steps": ["a", "b", "c"], "intent": "t"})["code"], "canvas_limit")
        op = C.portrait_op({"steps": [{"text": "s{}".format(i), "status": "pending"} for i in range(20)], "current": 15}, "plan", "Mine")
        self.assertEqual(len(op["steps"]), C.MAX_PORTRAIT_STEPS)
        self.assertEqual(op["steps"][op["current"] - 1]["text"], "s14")
        self.assertEqual(op["title"], "Mine")

    def test_undo(self):
        first = self.drivers()
        self.ok({"op": "move", "id": "price", "by": [0, 100], "intent": "t"})
        mover = self.apply([{"op": "move", "id": "price", "by": [40, 0], "intent": "t"}, {"op": "shape", "text": "extra", "at": "c0r40", "intent": "t"}])
        batch = mover["batch"]
        self.assertEqual(self.refused({"op": "undo", "batch": batch, "intent": "t"}, REVIEWER)["code"], "element_not_yours")
        undone = self.ok({"op": "undo", "batch": batch, "intent": "take it back"})
        self.assertEqual((self.el("E-2")["x"], self.el("E-2")["y"]), (220, 220))
        self.assertNotIn("E-5", [e["id"] for e in self.scene()["elements"]])
        self.assertIn("E-4", undone["ids"], "the bound arrow follows the restored note")
        self.assertTrue(self.scene()["batches"][batch]["undone"])
        self.assertEqual(self.refused({"op": "undo", "batch": batch, "intent": "t"})["code"], "op_invalid")
        self.make_manager("alpha-reviewer")
        # Phase 5 (6.2): undo skips what someone outside the undone batches changed later (it used to write it back). The
        # note and its arrow were moved later by their author's own batches, so they stay; the frame and the other note go.
        # "N of M" counts the marks, not the auto-claim that came with them (QA phase 5 L3).
        manager_undo = self.apply([{"op": "undo", "batch": first["batch"], "intent": "t"}],
                                  C.CanvasAuthor("alpha-reviewer", "member", "cli", True, manager=True))
        self.assertEqual([e["id"] for e in self.scene()["elements"]], ["E-2", "E-4"])
        self.assertEqual(manager_undo["applied"][0]["undo"]["skipped"][0], {"id": "E-2", "by": "alpha-worker", "seq": 5})
        self.assertTrue(manager_undo["warnings"][0]["message"].startswith("2 of 4 reverted; E-2 edited by alpha-worker later (v5) and 1 more"),
                        manager_undo["warnings"])
        human = self.apply([{"op": "shape", "at": "c0r0"}], OPERATOR)["batch"]
        self.assertEqual(self.refused({"op": "undo", "batch": human, "intent": "t"}, C.CanvasAuthor("alpha-reviewer", "member", "cli", True, manager=True))["code"], "operator_only")
        self.ok({"op": "undo", "batch": human}, OPERATOR)

    def test_atomic_batches_apply_nothing_on_a_refusal(self):
        self.drivers()
        version = C.current_version(self.team)
        size = F.whiteboard_dir(self.team).joinpath("events.jsonl").stat().st_size
        with self.assertRaises(HerdrTeamError) as ctx:
            self.apply([{"op": "shape", "text": "ok", "at": "c0r40", "intent": "t"}, {"op": "shape", "intent": "t", "at": [C.MAX_COORD * 2, 0]}], atomic=True)
        self.assertEqual(ctx.exception.code, "canvas_refused")
        self.assertEqual([r["index"] for r in ctx.exception.details["refused"]], [1])
        self.assertTrue(ctx.exception.details["applied_nothing"])
        self.assertEqual(C.current_version(self.team), version)
        self.assertEqual(F.whiteboard_dir(self.team).joinpath("events.jsonl").stat().st_size, size)

    def test_overlapping_text_warns(self):
        self.ok({"op": "shape", "text": "one", "at": "c0r0", "intent": "t"})
        result = self.apply([{"op": "shape", "text": "two", "at": "c2r1", "intent": "t"}])
        self.assertEqual(result["warnings"][0]["code"], "overlap")


# --------------------------------------------------------------------------
# the log and the scene


class EventsAndScene(CanvasRig):
    def test_events_fold_back_into_the_scene(self):
        self.drivers()
        self.ok({"op": "claim", "region": "c0r0:c5r5", "label": "x", "intent": "t"})
        self.ok({"op": "move", "id": "price", "by": [0, 20], "intent": "t"})
        self.ok({"op": "legend", "symbol": "E-4", "meaning": "x", "intent": "t"})
        before = self.scene()
        scene_path = F.whiteboard_dir(self.team) / "scene.json"
        os.unlink(scene_path)
        self.assertEqual(self.scene(), before, "a missing scene.json is rebuilt from events.jsonl")
        stale = dict(before, version=2, elements=[])
        store.write_json(scene_path, stale)
        self.assertEqual(self.scene(), before, "a scene behind the log is rebuilt")
        event = json.loads(F.whiteboard_dir(self.team).joinpath("events.jsonl").read_text().splitlines()[0])
        self.assertEqual(list(event), ["v", "seq", "ts", "batch", "author", "op", "index", "intent", "ids", "changes"])
        self.assertEqual(event["author"], {"name": "alpha-worker", "kind": "member", "agent": "claude", "via": "cli", "verified": True})
        self.assertEqual([c["target"] for c in event["changes"]], ["home", "author", "element", "claim"])  # the automatic claim (phase 5)

    def test_changes_since(self):
        self.drivers()
        got = C.changes_since(self.team, 2)
        self.assertEqual(([e["seq"] for e in got["events"]], got["version"], got["complete"], got["reset"]), ([3, 4], 4, True, False))
        self.assertTrue(C.changes_since(self.team, 99)["reset"])
        limited = C.changes_since(self.team, 0, limit=3)
        self.assertEqual(([e["seq"] for e in limited["events"]], limited["complete"]), ([1, 2, 3], False))
        with self.assertRaises(HerdrTeamError):
            C.changes_since(self.team, -1)

    def test_clear_archives_and_keeps_counting_then_purge_deletes(self):
        self.drivers()
        cleared = C.clear(self.layout, self.team, "human")
        self.assertEqual(cleared["version"], 5)
        self.assertTrue(Path(cleared["archived"], "events.jsonl").is_file())
        self.assertEqual(self.scene()["elements"], [])
        self.assertTrue(C.changes_since(self.team, 3)["reset"])
        new = self.ok({"op": "shape", "at": "c0r0", "intent": "t"})
        self.assertEqual(new["ids"], ["E-5"], "ids survive a clear")
        self.assertEqual(C.summary(self.team), {"version": 6, "elements": 1, "comments_open": 0, "claims_active": 1,
                                                "updated_at": self.scene()["updated_at"]})
        self.assertTrue(C.purge(self.team))
        self.assertFalse(F.whiteboard_dir(self.team).exists())
        self.assertFalse(C.purge(self.team))
        self.assertEqual(C.summary(self.team)["version"], 0)


# --------------------------------------------------------------------------
# board records


class Notices(CanvasRig):
    def test_canvas_changed_is_coalesced_per_author_per_minute(self):
        now = time.time()
        first = self.apply([{"op": "shape", "kind": "note", "text": "a", "at": "c0r0", "intent": "t"}], now=now)
        [record] = self.records("canvas_changed")
        self.assertEqual(first["notices"]["canvas_changed"], record["seq"])
        self.assertEqual(record["to"], ["alpha-reviewer", "human"])
        self.assertEqual(record["text"], "alpha-worker drew on the canvas: 1 note (v1). Look: herdr-synapse canvas look --since 0")
        second = self.apply([{"op": "arrow", "points": ["c0r0", "c5r5"], "intent": "t"},
                             {"op": "pen", "points": ["c0r0", "c1r1"], "intent": "t"}], now=now + 5)
        self.apply([{"op": "move", "id": "E-1", "by": [0, 20], "intent": "t"}], now=now + 10)
        self.assertIsNone(second["notices"]["canvas_changed"])
        self.assertEqual(len(self.records("canvas_changed")), 1)
        self.assertEqual(C.flush_notices(self.layout, self.team, now=now + 30), [], "the window has not passed")
        posted = C.flush_notices(self.layout, self.team, now=now + 61)
        [later] = self.records("canvas_changed")[1:]
        self.assertEqual(posted, [later["seq"]])
        self.assertEqual(later["text"], "alpha-worker drew on the canvas: 1 arrow, 1 pen stroke, moved 1 (v2–v4). Look: herdr-synapse canvas look --since 1")
        self.assertEqual(later["canvas"], {"author": "alpha-worker", "from": 2, "to": 4, "counts": {"arrow": 1, "pen": 1, "moved": 1}, "batches": ["B-2", "B-3"]})
        self.assertEqual(C.flush_notices(self.layout, self.team, now=now + 200), [])
        self.apply([{"op": "shape", "at": "c40r0"}], OPERATOR, now=now + 300)
        human = self.records("canvas_changed")[-1]
        self.assertEqual(human["to"], ["alpha-reviewer", "alpha-worker"])
        self.assertTrue(human["text"].startswith("the operator drew on the canvas: 1 box (v5)"))

    def test_flush_waits_while_the_canvas_is_off(self):
        now = time.time()
        self.apply([{"op": "shape", "at": "c0r0", "intent": "t"}], now=now)
        self.apply([{"op": "shape", "at": "c20r0", "intent": "t"}], now=now + 1)
        F.set_team(self.team, enabled=False)
        self.assertEqual(C.flush_notices(self.layout, self.team, now=now + 100), [])
        F.set_team(self.team, enabled=True)
        self.assertEqual(len(C.flush_notices(self.layout, self.team, now=now + 100)), 1)

    def test_send_to_member(self):
        self.drivers()
        with self.assertRaises(HerdrTeamError) as ctx:
            C.send_to_member(self.layout, self.team, ["E-2"], "alpha-reviewer", "x", WORKER)
        self.assertEqual(ctx.exception.code, "operator_only")
        with self.assertRaises(HerdrTeamError) as ctx:
            C.send_to_member(self.layout, self.team, ["E-2"], "nobody", None, OPERATOR)
        self.assertEqual(ctx.exception.code, "member_not_found")
        with mock.patch.object(R, "find_resvg", return_value="/fake/resvg"), mock.patch.object(R, "RUN", fake_resvg):
            sent = C.send_to_member(self.layout, self.team, ["E-2", "onboard"], "alpha-reviewer", "merge these", OPERATOR)
        record = store.BoardStore(self.team).get(sent["seq"])
        self.assertEqual((record["event"], record["to"]), ("canvas_sent", ["alpha-reviewer"]))
        self.assertTrue(record["text"].startswith('The operator sent you part of the canvas (E-2, E-3 around c11r6): "merge these". E-2 note "Price rise in March"'))
        self.assertIn("Image: {}".format(sent["image"]), record["text"])
        self.assertTrue(sent["image"].endswith(".png") and Path(sent["image"]).is_file())
        self.assertEqual(record["canvas"]["elements"], ["E-2", "E-3"])


# --------------------------------------------------------------------------
# look


class Look(CanvasRig):
    def test_look_text_is_pinned_for_a_small_scene(self):
        self.drivers()
        self.ok({"op": "comment", "at": "price", "text": "@reviewer please check", "intent": "ask for a check"})
        self.ok({"op": "legend", "symbol": "red cross", "meaning": "I disagree", "intent": "t"}, REVIEWER)
        self.ok({"op": "lock", "region": "c0r40:c20r50", "label": "hands off"}, OPERATOR)
        self.ok({"op": "shape", "text": "Pricing page", "at": [900, 40]}, OPERATOR)
        P.clear_member(self.team, "alpha-worker")  # the worker's presence (phase 5) has its own tests; this listing is about the rest
        result = C.look(self.layout, self.team, "alpha-reviewer", region="c10r4:c40r22", since=0)
        self.assertEqual(result["text"], "\n".join([
            "canvas of alpha · v8 · 6 elements · you are alpha-reviewer",
            'claims: K-1 alpha-worker "group the drivers" c9r3:c41r23 (5m left)',  # the drivers' automatic claim (phase 5)
            'locks: X-1 by the operator "hands off" c0r40:c20r50',
            'legend: G-1 red cross = "I disagree" (you)',
            "changes since v0 (8):",
            '  v1 alpha-worker added E-1 frame "Churn drivers" — group the drivers',
            '  v2 alpha-worker added E-2 note "Price rise in March" in E-1 — the biggest driver',
            '  v3 alpha-worker added E-3 note "Slow onboarding" in E-1 — second driver',
            "  v4 alpha-worker added E-4 arrow E-2 → E-3 in E-1 — worsens",
            "  v5 alpha-worker commented C-1 on E-2 → @alpha-reviewer — ask for a check",
            '  v6 you added legend G-1: red cross = "I disagree" — t',
            '  v7 the operator locked X-1 c0r40:c20r50 "hands off"',
            '  v8 the operator added E-5 box "Pricing page"',
            "region c10r4:c40r22:",
            '  E-1 frame "Churn drivers" [200,80 600x360] c10r4 by alpha-worker — group the drivers',
            '    E-2 note "Price rise in March" [220,120 180x120] c11r6 by alpha-worker — the biggest driver',
            '    E-3 note "Slow onboarding" [520,120 180x120] c26r6 by alpha-worker — second driver',
            '    E-4 arrow E-2 → E-3 "worsens" by alpha-worker — worsens',
            '  C-1 comment on E-2 by alpha-worker → @alpha-reviewer: "@reviewer please check" (open)',
            "elsewhere:",
            '  E-5 box "Pricing page" [900,40 160x80] by the operator',
        ]))
        self.assertEqual(result["level"], "full")
        self.assertEqual(result["region"], [200, 80, 800, 440])
        self.assertEqual(result["elsewhere"], [{"id": "E-5", "type": "box", "bounds": [900, 40, 160, 80], "cell": "c45r2", "text": "Pricing page",
                                                "author": "human", "line": 'E-5 box "Pricing page" [900,40 160x80] by the operator'}])
        self.assertEqual([c["id"] for c in result["comments_for_you"]], ["C-1"])
        self.assertEqual(result["changes"][0], {"seq": 1, "author": "alpha-worker", "op": "frame", "ids": ["E-1"], "intent": "group the drivers",
                                                "summary": 'added E-1 frame "Churn drivers"'})
        self.assertEqual(C.look_text(result), result["text"])

    def test_levels_overview_and_clusters(self):
        self.ok({"op": "frame", "title": "F", "at": "c0r0", "w": 200, "h": 200, "intent": "t"})
        for index in range(4):
            self.ok({"op": "shape", "text": "far {}".format(index), "at": [3000 + index * 200, -3000], "intent": "t"}, REVIEWER)
        with mock.patch.object(C, "LOOK_FULL_MAX", 3), mock.patch.object(C, "LOOK_LINE_MAX", 2):
            overview = C.look(self.layout, self.team, "alpha-worker")
            self.assertEqual(overview["level"], "overview")
            self.assertEqual([e["id"] for e in overview["elsewhere"]], ["E-1", "E-2"], "frames first")
            self.assertEqual(overview["clusters"], [{"author": "alpha-reviewer", "count": 3, "center": [3480, -2960], "cell": "c174r-148", "direction": "north-east"}])
            self.assertIn("  3 elements by alpha-reviewer, north-east (around c174r-148)", overview["text"])
            around = C.look(self.layout, self.team, "alpha-worker", around="E-1")
            self.assertEqual(around["region"], [-200, -200, 400, 400])
            self.assertEqual([e["id"] for e in around["elements"]], ["E-1"])

    def test_since_last_moves_the_cursor(self):
        self.drivers()
        first = C.look(self.layout, self.team, "alpha-reviewer", since="last")
        self.assertEqual((first["since"], len(first["changes"])), (0, 4))
        self.assertEqual(C.cursor(self.team, "alpha-reviewer"), 4)
        self.ok({"op": "move", "id": "price", "by": [0, 20], "intent": "t"})
        second = C.look(self.layout, self.team, "alpha-reviewer", since="last", advance=False)
        self.assertEqual([c["summary"] for c in second["changes"]], ["moved E-2, E-4"])
        self.assertEqual(C.cursor(self.team, "alpha-reviewer"), 4, "advance=False leaves the cursor")
        changes = C.read_changes(self.layout, self.team, "alpha-reviewer")
        self.assertEqual(changes["text"], "canvas of alpha · v5\nchanges since v4 (1):\n  v5 alpha-worker moved E-2, E-4 — t")
        self.assertEqual(C.read_changes(self.layout, self.team, "alpha-reviewer")["changes"], [])
        with self.assertRaises(HerdrTeamError):
            C.look(self.layout, self.team, "alpha-reviewer", since="yesterday")
        with self.assertRaises(HerdrTeamError):
            C.look(self.layout, self.team, "../evil")

    def test_image_with_and_without_resvg(self):
        self.drivers()
        missing = C.look(self.layout, self.team, "alpha-worker", image=True)
        self.assertIsNone(missing["image"])
        self.assertEqual(missing["image_error"], "resvg_missing")
        self.assertTrue(Path(missing["svg"]).is_file())
        self.assertIn("image: none (resvg_missing); svg: ", missing["text"])
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return fake_resvg(argv, timeout)

        with mock.patch.object(R, "find_resvg", return_value="/fake/resvg"), mock.patch.object(R, "RUN", run):
            result = C.look(self.layout, self.team, "alpha-worker", region="c10r4:c40r22", image=True, grid=True)
        self.assertIsNone(result["image_error"])
        self.assertTrue(result["image"].endswith(".png") and Path(result["image"]).read_bytes().startswith(PNG_MAGIC))
        self.assertRegex(Path(result["image"]).name, r"^look-alpha-worker-v4-[0-9a-f]{8}\.png$")
        self.assertEqual(calls[0][:3], ["/fake/resvg", "-w", "1024"])
        self.assertTrue(result["text"].endswith("image: {}".format(result["image"])))

    def test_exact_falls_back_without_a_page_and_uses_the_page_when_it_answers(self):
        self.drivers()
        fallback = C.look(self.layout, self.team, "alpha-worker", exact=True, image=True)
        self.assertEqual((fallback["exact"], fallback["image_error"]), (False, "exact_timeout"))
        store.write_json(self.ts.session.root / "whiteboard.json", {"v": 1, "pid": 1})

        def page():
            for _ in range(100):
                pending = C.pending_exports(self.team)
                if pending:
                    C.complete_export(self.team, pending[0]["id"], png(20, 20))
                    return
                time.sleep(0.02)

        worker = threading.Thread(target=page)
        worker.start()
        with mock.patch.object(C, "EXACT_POLL_S", 0.02):
            exact = C.look(self.layout, self.team, "alpha-worker", exact=True, image=True)
        worker.join()
        self.assertTrue(exact["exact"])
        self.assertIsNone(exact["image_error"])
        self.assertTrue(exact["image"].endswith(".png"))


# --------------------------------------------------------------------------
# assets, stills, exports, artifacts


class Files(CanvasRig):
    def test_assets_are_content_addressed_and_checked(self):
        first = C.store_asset(self.team, png(4, 3), "image")
        self.assertEqual((first["mime"], first["px_w"], first["px_h"]), ("image/png", 4, 3))
        self.assertEqual(C.store_asset(self.team, png(4, 3), "png")["asset"], first["asset"])
        with self.assertRaises(HerdrTeamError) as ctx:
            C.store_asset(self.team, png(), "jpeg")
        self.assertEqual(ctx.exception.code, "image_refused")
        with self.assertRaises(HerdrTeamError):
            C.store_asset(self.team, b"<svg><script/></svg>", "svg")
        for name in ("../team.json", "x.png", first["asset"].replace(".png", ".exe")):
            with self.assertRaises(HerdrTeamError):
                C.asset_path(self.team, name)
        target = C.asset_path(self.team, first["asset"])
        os.unlink(target)
        os.symlink(self.team.team_json, target)
        with self.assertRaises(HerdrTeamError):
            C.asset_path(self.team, first["asset"])

    def test_stills(self):
        chart = self.ok({"op": "chart", "spec": {"mark": "bar", "data": {"values": [{"a": 1}]}}, "at": "c0r0", "intent": "t"})["ids"][0]
        box = self.ok({"op": "shape", "at": "c30r0", "intent": "t"})["ids"][0]
        C.store_still(self.team, chart, 1, png())
        newer = C.store_still(self.team, chart, 3, png())
        self.assertIsNone(C.still_path(self.team, chart, 1), "older stills of the element are pruned")
        self.assertEqual(C.still_path(self.team, chart, 3), newer)
        for bad in ((box, 1, png()), (chart, 1, b"notpng"), ("E-x", 1, png())):
            with self.assertRaises(HerdrTeamError):
                C.store_still(self.team, *bad)

    def test_export_rendezvous(self):
        request = C.request_export(self.team, [0, 0, 100, 100], True, False, "alpha-worker")
        self.assertEqual([p["id"] for p in C.pending_exports(self.team)], [request])
        path = C.complete_export(self.team, request, png())
        self.assertTrue(path.is_file())
        self.assertEqual(C.pending_exports(self.team), [])
        old = C.request_export(self.team, [0, 0, 1, 1], True, False, "x")
        self.assertEqual(C.pending_exports(self.team, max_age_s=-1), [])
        self.assertFalse((F.whiteboard_dir(self.team) / "exports" / (old + ".json")).exists())
        with self.assertRaises(HerdrTeamError):
            C.complete_export(self.team, "0" * 16, png())

    def test_artifact_file(self):
        art = self.artifacts()
        (art / "sub").mkdir()
        (art / "sub" / "d.json").write_text("{}")
        self.assertEqual(C.artifact_file(self.layout, self.team, "sub/d.json"), Path(os.path.realpath(art / "sub" / "d.json")))
        os.symlink("/etc/hosts", art / "link.csv")
        for rel in ("/etc/hosts", "../x.csv", "link.csv", "missing.csv", ""):
            with self.assertRaises(HerdrTeamError) as ctx:
                C.artifact_file(self.layout, self.team, rel)
            self.assertEqual(ctx.exception.code, "path_refused", rel)


# --------------------------------------------------------------------------
# the CLI


class Cli(CanvasRig):
    def setUp(self):
        super().setUp()
        self.api = live_api(list(FAKE_AGENTS))

    def cli(self, argv, pane=None):
        overrides = {"HERDR_PANE_ID": pane} if pane else {}
        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **overrides), self.api))

    def worker(self, *argv):
        return self.cli(argv, pane="w2:p2")

    def human(self, *argv):
        return self.cli(argv)

    def test_draw_from_a_file_stdin_and_op(self):
        batch = self.ts.tmp / "plan.json"
        batch.write_text(json.dumps({"ops": [{"op": "shape", "id": "a", "text": "A", "at": "c0r0", "intent": "t"}]}))
        code, payload, err = self.worker("canvas", "draw", "--file", os.fspath(batch), "--op", '{"op": "shape", "text": "B", "right_of": "a", "intent": "t"}')
        self.assertEqual(code, 0, err)
        self.assertEqual([a["ids"] for a in payload["applied"]], [["E-1"], ["E-2"]])
        self.assertEqual(self.el("E-1")["author"], "alpha-worker")
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps([{"op": "shape", "text": "C", "at": "c0r10", "intent": "t"}]))):
            code, out, err = run_cli(["canvas", "draw", "--file", "-"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2"), self.api)
        self.assertEqual(code, 0, err)
        # C goes just below A and B: it grows their automatic claim rather than making a new one (QA phase 5 L8).
        self.assertEqual(out, "v3 · B-2 · applied 1, refused 0\n#0 shape E-3 → 160x80 (hug, 1 line) · claimed K-1\ncheck: clean\n")
        code, _payload, err = self.worker("canvas", "draw", "--op", '{"op": "shape", "text": "no intent"}')
        self.assertEqual((code, err["code"]), (1, "canvas_refused"))
        self.assertEqual(err["refused"][0]["details"]["field"], "intent")
        code, _payload, err = self.worker("canvas", "draw")
        self.assertEqual((code, err["code"]), (2, "usage"))

    def test_look_comment_claim_legend_release(self):
        self.drivers()
        code, payload, err = self.cli(["canvas", "look", "--since", "last", "--region", "c10r4:c40r22"], pane="w2:p1")
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["reader"], payload["since"], len(payload["changes"])), ("alpha-reviewer", 0, 4))
        self.assertEqual(C.cursor(self.team, "alpha-reviewer"), 4)
        code, payload, err = self.cli(["canvas", "comment", "price", "@alpha-worker why?", "--mention", "human"], pane="w2:p1")
        self.assertEqual(code, 0, err)
        comment = self.el("C-1")
        self.assertEqual((comment["intent"], comment["mentions"]), ("@alpha-worker why?", ["human", "alpha-worker"]))
        code, payload, err = self.worker("canvas", "claim", "[0, 0, 100, 100]", "working here")
        self.assertEqual((code, payload["applied"][0]["ids"]), (0, ["K-2"]), err)  # K-1 is the drivers' automatic claim (phase 5)
        code, payload, err = self.worker("canvas", "legend", "red cross", "I disagree")
        self.assertEqual(code, 0, err)
        code, payload, err = self.worker("canvas", "release")
        self.assertEqual((code, payload["applied"][0]["ids"]), (0, ["K-1", "K-2"]), err)
        code, payload, err = self.worker("canvas", "changes")
        self.assertEqual((code, payload["since"]), (0, 0), err)

    def test_off_and_operator_only_through_the_cli(self):
        code, payload, err = self.worker("canvas", "lock", "c0r0:c10r10")
        self.assertEqual((code, err["code"]), (1, "canvas_refused"))
        self.assertEqual(err["refused"][0]["code"], "operator_only")
        code, payload, err = self.human("canvas", "lock", "c0r0:c10r10", "--label", "mine")
        self.assertEqual(code, 0, err)
        F.set_layer(self.ts.session, False, "human", "cli")
        for argv in (["canvas", "look"], ["canvas", "draw", "--op", "{}"], ["canvas", "helper"]):
            code, _payload, err = self.worker(*argv)
            self.assertEqual((code, err["code"]), (1, "whiteboard_off"), argv)

    def test_export_helper_portrait_send(self):
        self.drivers()
        code, payload, err = self.worker("canvas", "export", "--format", "json")
        self.assertEqual((code, payload["scene"]["version"]), (0, 4), err)
        code, payload, err = self.worker("canvas", "export", "--format", "md")
        self.assertTrue(payload["text"].startswith("canvas of alpha · v4"), err)
        out = self.ts.tmp / "canvas.svg"
        code, payload, err = self.worker("canvas", "export", "--format", "svg", "--out", os.fspath(out))
        self.assertEqual(code, 0, err)
        self.assertTrue(out.read_text().startswith("<svg"))
        code, payload, err = self.worker("canvas", "export", "--format", "png")
        self.assertEqual((code, err["code"]), (1, "render_unavailable"))
        code, payload, err = self.worker("canvas", "helper", "--print")
        self.assertEqual(code, 0, err)
        self.assertIn("class Sketch", payload["source"])
        self.assertTrue(payload["path"].endswith("sketch.py"))
        code, payload, err = self.worker("canvas", "portrait", "--step", "fetch", "--step", "plot", "--current", "2", "--title", "Plan")
        self.assertEqual(code, 0, err)
        with mock.patch("herdr_team.activity.plan_of", return_value=None):
            code, payload, err = self.worker("canvas", "portrait", "--from-todo")
        self.assertEqual((code, err["code"]), (1, "portrait_no_plan"))
        with mock.patch("herdr_team.activity.plan_of", return_value={"steps": [{"text": "a", "status": "completed"}, {"text": "b", "status": "in_progress"}], "current": 2}):
            code, payload, err = self.worker("canvas", "portrait", "--from-todo")
        self.assertEqual(code, 0, err)
        code, payload, err = self.human("canvas", "send", "E-2", "--to", "alpha-reviewer", "--note", "look")
        self.assertEqual(code, 0, err)
        self.assertEqual(store.BoardStore(self.team).get(payload["seq"])["event"], "canvas_sent")
        code, payload, err = self.worker("canvas", "undo", "B-1")
        self.assertEqual(code, 0, err)

    def test_mcp_subcommand_serves_stdio(self):
        requests = "\n".join(json.dumps(m) for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )) + "\n"
        with mock.patch.object(sys, "stdin", io.StringIO(requests)):
            code, out, err = run_cli(["--team", os.fspath(self.team.root), "canvas", "mcp"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2"), self.api)
        self.assertEqual(code, 0, err)
        replies = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(len(replies[1]["result"]["tools"]), 8)  # canvas_focus since phase 5


# --------------------------------------------------------------------------
# sketch.py


class SketchHelper(CanvasRig):
    def test_sketch_builds_batches_the_server_accepts(self):
        s = S.Sketch(default_intent="map the churn drivers")
        s.claim("c10r4:c40r22", "mapping churn drivers")
        frame = s.frame("Churn drivers", at="c10r4", w=600, h=360, id="drivers")
        price = s.shape("note", "Price rise in March", inside=frame, id="price")
        onboard = s.shape("note", "Slow onboarding", right_of=price, gap=60)
        s.arrow(price, onboard, label="worsens")
        s.pen(s.circle_points(300, 170, 90, n=12), closed=True, color="red", intent="circle the main driver")
        s.pen(s.spiral_points(0, 0, 10, 60, n=30))
        s.pen(s.polyline(lambda x: x * x / 100.0, 0, 100, n=11))
        s.graph(["a", "b"], [("a", "b", "uses")], id="g", at=s.cell(0, 30))
        s.comment(price, "@reviewer check", mentions=["reviewer"])
        s.legend("red cross", "I disagree")
        s.move(onboard, by=[0, 20])
        s.restyle([price, onboard], color="blue")
        s.edit(price, "Price rise")
        s.portrait(["plan", "draw"], current=2)
        s.release()
        self.assertEqual(s.region("c1r1", [100, 100]), "c1r1:c5r5")
        self.assertEqual(S.Sketch.cell(3, -4), "c3r-4")
        ops, atomic = C.parse_batch(s.to_json())
        self.assertFalse(atomic)
        self.assertTrue(all(op.get("intent") for op in ops))
        result = self.apply(ops)
        self.assertEqual(result["refused"], [])
        self.assertEqual(len(result["applied"]), len(ops))
        again = S.Sketch(default_intent="twice")
        again.shape("box", "x")
        self.assertNotEqual(again.ops[0]["id"], s.ops[3]["id"], "generated aliases differ between runs")
        self.assertEqual(self.apply(again.ops)["refused"], [])

    def test_sketch_is_standalone(self):
        source = Path(S.__file__).read_text()
        self.assertNotIn("herdr_team", source.split('"""', 2)[2], "sketch.py imports nothing from herdr_team")


if __name__ == "__main__":
    unittest.main()

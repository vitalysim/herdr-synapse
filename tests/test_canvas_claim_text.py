"""A claim's dashed border is never drawn through words (layout findings N4, the open half of F8 and V2).

A claim is drawn as a dashed rectangle over the board. The clarity round stopped it cutting the marks it holds, and
left it cutting text that is not a mark: a frame's own title ("Checkout flow", "Incident response", "Checkout flow for
returning shoppers") and an arrow's label pill ("cancels a job the user / no longer wants", at every zoom) - 56 lines
of text on 8 golden boards over three zooms - while ``canvas check`` called those boards clean, because a title and
an arrow pill are not marks. The owner's word for a line through text is "unclear".

Two halves, both held here:

* **check sees it.** ``claim_edge`` counts a frame title and an arrow pill as things a border must not cut, and its
  repair is a ``claim`` op whose region holds them whole and that replaces the claim it corrects;
* **the border does not do it.** A claim the system makes, or one an agent asks for, is snapped around the words its
  edge would cut, and at the zooms where a frame's title stands above the frame and grows on screen the rectangle is
  drawn clear of it - measured on the golden boards the verifier named, in the renderer's own coordinates.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from collab_support import MEMBER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team import canvas_render as R

SCENES = Path(__file__).resolve().parent / "fixtures" / "canvas_scenes"
#: The three whole-board picture widths the verifier measured at: an overview, a page, and a close read.
WIDTHS = (500, 1024, 2600)
#: The golden boards the verifier named: two arrow labels at every zoom, and three frame titles.
NAMED = ("arrow-labels", "graph-groups", "flowchart", "agent-board", "architecture")


def border_through_text(scene):
    """``[(width, claim, entry)]``: every line of text a claim's border is drawn through, at each picture width.

    The display list's own text boxes at the picture's scale (the walk the badges use) against the dashed border as
    the display list draws it at that scale - a rectangle, or the runs of a border broken under words - with its 2 px
    of stroke. The claim's own label is not counted against itself.
    """
    dl = D.display_list(scene)
    claims = [e for e in dl["entries"] if e.get("kind") == "claim"]
    out = []
    for width in WIDTHS:
        u = R.units_per_px(tuple(float(v) for v in dl["bbox"]), width)
        texts = [(e.get("id"), box) for e in dl["entries"] for box in R.text_boxes({"entries": [e]}, u)]
        for claim in claims:
            for (ax, ay), (bx, by) in drawn_runs(claim, u):
                band = (min(ax, bx) - u, min(ay, by) - u, max(ax, bx) + u, max(ay, by) + u)
                for owner, t in texts:
                    if owner != claim["id"] and t[0] < band[2] and band[0] < t[2] and t[1] < band[3] and band[1] < t[3]:
                        out.append((width, claim["id"], owner))
    return out


def drawn_runs(entry, u):
    """The straight runs of a claim's dashed border that draw at ``u`` units per pixel."""
    scale = 1.0 / u
    runs = []
    for item in entry.get("items") or ():
        if not D.lod_visible(item, scale) or not item.get("dash"):
            continue
        if item.get("k") == "rect":
            x0, y0 = float(item["x"]), float(item["y"])
            x1, y1 = x0 + float(item["w"]), y0 + float(item["h"])
            runs += [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]
        elif item.get("k") == "path":
            for m in re.finditer(r"M(-?[\d.e+-]+) (-?[\d.e+-]+)L(-?[\d.e+-]+) (-?[\d.e+-]+)", item["d"]):
                a, b, c, d = (float(v) for v in m.groups())
                runs.append(((a, b), (c, d)))
    return runs


class Check(CollabRig):
    """``claim_edge`` sees a frame title and an arrow pill."""

    def _labelled_arrow(self):
        self.ok({"op": "shape", "kind": "box", "id": "client", "text": "Client", "at": [0, 0], "intent": "one end"})
        self.ok({"op": "shape", "kind": "box", "id": "worker", "text": "Worker", "at": [900, 0], "intent": "the other"})
        self.ok({"op": "arrow", "from": "client", "to": "worker", "label": "cancels a job the user no longer wants",
                 "intent": "the label the verifier saw cut"})
        scene = self.scene()
        arrow = next(e for e in scene["elements"] if e.get("type") == "arrow")
        return scene, arrow

    def test_a_border_through_an_arrows_label_is_seen_and_the_repair_clears_it(self):
        scene, arrow = self._labelled_arrow()
        (x, y, w, h), _size, _lines = G.arrow_label_pill(arrow)
        # A claim whose right edge runs down the middle of the pill: the picture the verifier filed.
        cut = {"id": "K-9", "author": MEMBER.name, "label": "the caller", "region": [-40, -40, int(x + w / 2), 120]}
        found = [p for p in K.problems(scene["elements"], MEMBER.name, claims=[cut]) if p["code"] == "claim_edge"]
        self.assertEqual(len(found), 1, "check sees the border through the label")
        self.assertIn(arrow["id"], found[0]["ids"])
        self.assertIn("label", found[0]["message"])
        fix = found[0]["fix"]
        self.assertEqual(fix["op"], "claim")
        region = fix["region"]
        self.assertTrue(region[2] + D.CLAIM_BORDER_OUT >= x + w, "the repair holds the pill whole: {}".format(region))
        self.assertEqual([p for p in K.problems(scene["elements"], MEMBER.name, claims=[dict(cut, region=region)])
                          if p["code"] == "claim_edge"], [], "and once it is applied the check is clean")

    def test_a_border_through_a_frames_title_is_seen(self):
        self.ok({"op": "frame", "id": "board", "title": "Checkout flow for returning shoppers", "at": [0, 0],
                 "w": 900, "h": 400, "intent": "a titled frame"})
        frame = self.by_alias("board")
        # Holds the frame's left end from above, so its right edge runs across the title in the band.
        cut = {"id": "K-9", "author": MEMBER.name, "label": "left half", "region": [-40, -40, 120, 440]}
        found = [p for p in K.problems(self.scene()["elements"], MEMBER.name, claims=[cut]) if p["code"] == "claim_edge"]
        self.assertEqual(len(found), 1, "check sees the border through the title")
        self.assertIn(frame["id"], found[0]["ids"])
        self.assertIn("title", found[0]["message"])
        fixed = dict(cut, region=found[0]["fix"]["region"])
        self.assertEqual([p for p in K.problems(self.scene()["elements"], MEMBER.name, claims=[fixed])
                          if p["code"] == "claim_edge"], [])

    def test_the_repair_applies_as_an_op_and_replaces_the_claim_it_corrects(self):
        scene, arrow = self._labelled_arrow()
        for claim in list(scene.get("claims") or []):
            self.ok({"op": "release", "id": claim["id"], "intent": "start from no claim"})
        (x, y, w, h), _size, _lines = G.arrow_label_pill(arrow)
        cut = {"id": "K-9", "author": MEMBER.name, "label": "the caller", "region": [-40, -40, int(x + w / 2), 120]}
        [problem] = [p for p in K.problems(scene["elements"], MEMBER.name, claims=[cut]) if p["code"] == "claim_edge"]
        self.ok(problem["fix"])
        claims = C.load_scene(self.team).get("claims") or []
        self.assertEqual(len(claims), 1)
        problems = [p for p in K.problems(self.scene()["elements"], MEMBER.name, claims=claims) if p["code"] == "claim_edge"]
        self.assertEqual(problems, [], "the repair, applied verbatim, leaves nothing to report")


class Border(CollabRig):
    """The claims the system and the agents make are not drawn through words."""

    def test_a_claim_asked_for_through_a_label_is_snapped_around_it(self):
        self.ok({"op": "shape", "kind": "box", "id": "client", "text": "Client", "at": [0, 0], "intent": "one end"})
        self.ok({"op": "shape", "kind": "box", "id": "worker", "text": "Worker", "at": [900, 0], "intent": "the other"})
        self.ok({"op": "arrow", "from": "client", "to": "worker", "label": "cancels a job the user no longer wants",
                 "intent": "a label"})
        arrow = next(e for e in self.scene()["elements"] if e.get("type") == "arrow")
        (x, y, w, h), _size, _lines = G.arrow_label_pill(arrow)
        result = self.apply([{"op": "claim", "region": [-40, -40, int(x + w / 2), 120], "label": "the caller",
                              "intent": "claim the caller"}])
        claim = [c for c in self.scene()["claims"] if not c.get("auto")][-1]
        self.assertTrue(claim["region"][2] + D.CLAIM_BORDER_OUT >= x + w, "the border steps around the pill: {}".format(claim["region"]))
        self.assertTrue(any(w.get("code") == "claim_snapped" and arrow["id"] in (w.get("ids") or [])
                            for w in result.get("warnings") or []), result.get("warnings"))

    def test_the_golden_boards_the_verifier_named_draw_no_border_through_text(self):
        for name in NAMED:
            with self.subTest(board=name):
                rig = CollabRig("run")
                rig.setUp()
                self.addCleanup(rig.doCleanups)
                for op in json.loads((SCENES / (name + ".json")).read_text())["ops"]:
                    got = rig.apply([dict(op, intent=op.get("intent") or "set the board up")])
                    self.assertEqual(got["refused"], [], got["refused"])
                scene = rig.scene()
                self.assertTrue(scene.get("claims"), "the system claimed what the member drew")
                self.assertEqual(border_through_text(scene), [])
                self.assertEqual([p["message"] for p in K.problems(scene["elements"], MEMBER.name, claims=scene["claims"])
                                  if p["code"] == "claim_edge"], [], "and check agrees there is nothing to report")


    def test_the_collab_board_draws_no_border_through_a_proposals_pill(self):
        """A proposal's pill is words too: claim K-1's corner ran over the end of proposal P-1's on the collab
        golden board (QA round 2), because neither the border nor check looked at proposals."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("canvas_qa_claims", Path(__file__).resolve().parent.parent / "tools" / "canvas_qa.py")
        assert spec is not None and spec.loader is not None
        qa_tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qa_tool)
        doc = qa_tool.load_scene_file(qa_tool.SCENES_DIR / "collab.json")
        with qa_tool.QaTeam() as qa:
            qa_tool.apply_scene(qa, doc)
            scene = C.load_scene(qa.team)
        self.assertTrue(scene.get("claims") and scene.get("proposals"), "a claim and a proposal on one board")
        self.assertEqual(border_through_text(scene), [])
        self.assertEqual(label_on_text(scene), [])


#: A sweep of zooms, in pixels per unit: the three picture widths are not where a zoomed title and a label meet first.
SCALES = tuple(round(2.0 / (1.18 ** i), 5) for i in range(26))


def label_on_text(scene, scales=SCALES):
    """``[(scale, claim, entry)]``: every line of other text a claim's own label is printed over, at each zoom."""
    dl = D.display_list(scene)
    out = []
    for scale in scales:
        u = 1.0 / scale
        mine, others = [], []
        for e in dl["entries"]:
            boxes = R.text_boxes({"entries": [e]}, u)
            (mine if e.get("kind") == "claim" else others).extend((e.get("id"), b) for b in boxes)
        for cid, a in mine:
            for owner, t in others:
                if min(a[2], t[2]) - max(a[0], t[0]) > u and min(a[3], t[3]) - max(a[1], t[1]) > u:
                    out.append((scale, cid, owner))
    return out


class Label(CollabRig):
    """A claim's own label is never printed over words (QA round 2 of layout findings N4).

    It hangs at a fixed 12 px outside the region's top-left corner, which is where a top-level frame's title stands
    once the board is zoomed out: on a member's claim of exactly their graph's frame, "K-2 alpha-member: ..." was
    printed over "Checkout flow for returning shoppers" at a whole-board fit, on 15 golden boards at some zoom.
    """

    GRAPH = {"op": "graph", "id": "dense", "title": "Checkout flow for returning shoppers", "layout": "flow",
             "direction": "right", "route": "orthogonal", "at": [0, 0], "intent": "a titled graph",
             "nodes": [{"id": "cart", "text": "Cart"}, {"id": "pay", "text": "Saved card"}, {"id": "ok", "text": "Order placed"}],
             "edges": ["cart -> pay: choose the card on file", "pay -> ok: low risk passes"]}

    def _claim_of_the_frame(self):
        self.ok(dict(self.GRAPH))
        for claim in list(self.scene().get("claims") or []):
            self.ok({"op": "release", "id": claim["id"], "intent": "start from no claim"})
        root = self.by_alias("dense")
        self.ok({"op": "claim", "region": [root["x"], root["y"], root["x"] + root["w"], root["y"] + root["h"]],
                 "label": "tidying the checkout graph", "intent": "claim exactly my graph"})
        return self.scene()

    def test_a_claim_of_exactly_a_titled_frame_prints_its_label_over_no_words_at_any_zoom(self):
        scene = self._claim_of_the_frame()
        self.assertEqual(label_on_text(scene), [])

    def test_the_label_still_shows_on_a_whole_board_picture_and_inside_it(self):
        scene = self._claim_of_the_frame()
        dl = D.display_list(scene)
        box = tuple(float(v) for v in dl["bbox"])
        self.assertEqual(G.view_box(scene), box, "the picture and the display list agree on the box")
        u = R.units_per_px(box, G.DEFAULT_MAX_PX)
        [claim] = [e for e in dl["entries"] if e.get("kind") == "claim"]
        words = R.text_boxes({"entries": [claim]}, u)
        self.assertTrue(words, "at the default picture's zoom the label is drawn")
        for b in words:
            self.assertTrue(box[0] <= b[0] and b[2] <= box[2] and box[1] <= b[1] and b[3] <= box[3],
                            "and the picture holds it whole: {} in {}".format(b, box))

    def test_a_claim_with_nothing_near_it_keeps_the_label_it_always_had(self):
        self.ok({"op": "shape", "kind": "box", "id": "alone", "text": "Alone", "at": [0, 0], "intent": "one box"})
        for claim in list(self.scene().get("claims") or []):
            self.ok({"op": "release", "id": claim["id"], "intent": "start from no claim"})
        self.ok({"op": "claim", "region": [-20, -20, 200, 120], "label": "mine", "intent": "a claim on open ground"})
        [claim] = [e for e in D.display_list(self.scene())["entries"] if e.get("kind") == "claim"]
        labels = [i for i in claim["items"] if i.get("k") == "group"]
        self.assertEqual([i.get("screen") for i in labels if i.get("lod") is None or i["lod"][1] is None], [[-20, -20]],
                         "above its top-left corner, where every claim's label hung before")

    def test_a_claim_smaller_than_the_words_it_sits_on_draws_no_dot(self):
        """Every side gapped used to leave ``M x y L x y``: a dot in the words, and resvg's "path dashing failed"."""
        self.ok({"op": "shape", "kind": "box", "id": "client", "text": "Client", "at": [0, 0], "intent": "one end"})
        self.ok({"op": "shape", "kind": "box", "id": "worker", "text": "Worker", "at": [900, 0], "intent": "the other"})
        self.ok({"op": "arrow", "from": "client", "to": "worker", "label": "cancels a job the user no longer wants",
                 "intent": "a label"})
        arrow = next(e for e in self.scene()["elements"] if e.get("type") == "arrow")
        cx, cy = arrow["label_at"]
        scene = dict(self.scene(), claims=[{"id": "K-9", "author": MEMBER.name, "label": "tiny", "auto": False,
                                            "region": [cx - 3, cy - 3, cx + 3, cy + 3]}])
        [claim] = [e for e in D.display_list(scene)["entries"] if e.get("kind") == "claim"]
        for item in claim["items"]:
            if item.get("k") == "path":
                runs = re.findall(r"M(-?[\d.e+-]+) (-?[\d.e+-]+)L(-?[\d.e+-]+) (-?[\d.e+-]+)", item["d"])
                self.assertTrue(runs, item)
                self.assertFalse(any((a, b) == (c, d) for a, b, c, d in runs), "a zero-length run: {}".format(item["d"]))


if __name__ == "__main__":
    unittest.main()

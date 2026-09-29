"""Canvas v2 phase 5 security (13) and extensibility (G8), rule by rule where the other suites do not already name them.

13.1 authority: ``test_canvas_collab_matrix`` and ``test_canvas_collab_lead`` (guarantee 3). 13.2 raised authority:
``test_canvas_collab_lead.RaisedAuthority``. 13.4 presence: ``test_canvas_presence`` and ``test_canvas_collab_lead``
(guarantee 6). 13.5 routes: ``test_whiteboard_server.PresenceRouteTests`` and ``CspUnchanged`` below. 13.7 rates:
``test_canvas_collab_proposals`` (rate and limits), ``test_canvas_collab_checkpoints`` (caps), the route's 8 a second.
"""
from __future__ import annotations

import importlib.util
import sys

from collab_support import LEAD, MEMBER, PEER, CollabRig
from support import PLUGIN_ROOT

from herdr_team import canvas as C
from herdr_team import canvas_collab as K
from herdr_team import canvas_presence as P
from herdr_team import canvas_svg
from herdr_team import whiteboard_server as W
from herdr_team.errors import HerdrTeamError


def load_rule():
    spec = importlib.util.spec_from_file_location("collab_rule_pinned", PLUGIN_ROOT / "tests" / "fixtures" / "collab_rule_pinned.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["collab_rule_pinned"] = module
    spec.loader.exec_module(module)
    return module


class OneModuleRule(CollabRig):
    """G8: a rule dropped in changes outcomes; nothing else is edited."""

    def test_a_dropped_in_rule_changes_outcomes(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, LEAD)["ids"][0]
        self.ok({"op": "pin", "id": box}, LEAD)
        self.ok({"op": "settings", "human_edits": "live"}, LEAD)
        before = self.apply([{"op": "restyle", "id": box, "tone": "info", "intent": "t"}])
        self.assertEqual((len(before["applied"]), before["proposed"]), (1, []), "live with revert, without the rule")
        rule = load_rule()
        rule.register()
        self.addCleanup(rule.unregister)
        self.assertIn("pinned", [name for _order, name, _fn in K.rules()])
        found = self.proposed({"op": "restyle", "id": box, "tone": "danger", "intent": "t"})
        self.assertEqual((found["reason"], found["message"]), ("pinned", "{} was placed by the operator".format(box)))
        with self.assertRaises(ValueError):
            rule.register()

    def test_rules_are_ordered_and_named_once(self):
        names = [name for _order, name, _fn in K.rules()]
        self.assertEqual(names[:8], ["busy", "locked", "frozen", "stale_base", "human_made", "host_geometry", "peer",
                                     "foreign_lane"], "host_geometry (A1) runs between the operator's marks and a peer's")


class ProposalsAreInert(CollabRig):
    """13.3: a proposal's values are server-built data; accept replays them only through the normal pipeline."""

    def test_hostile_text_reaches_the_page_as_text(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, LEAD)["ids"][0]
        pid = self.proposed({"op": "edit", "id": box, "text": "<script>alert(1)</script> & <b>", "intent": "<img src=x onerror=alert(1)>"})["proposal"]
        doc = C.display(self.team)
        entry = next(e for e in doc["entries"] if e["id"] == pid)
        texts = [line["t"] for item in entry["items"] for prim in [item] + list(item.get("items") or []) if prim.get("k") == "text"
                 for line in prim["lines"]]
        self.assertTrue(any("<script>" in t for t in texts), "kept as text in the list")
        svg = canvas_svg.write(doc, theme="light")
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)
        self.assertNotIn("<img", svg)

    def test_an_accept_goes_through_the_fold_and_the_pipeline(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, LEAD)["ids"][0]
        arrow = self.ok({"op": "arrow", "from": box, "to": [600, 40]}, LEAD)["ids"][0]
        start = self.el(arrow)["points"][0][1]
        pid = self.proposed({"op": "move", "id": box, "by": [0, 200], "intent": "t"})["proposal"]
        self.assertEqual(self.el(arrow)["points"][0][1], start, "a proposal changes nothing")
        self.ok({"op": "accept", "id": pid}, LEAD)
        self.assertGreater(self.el(arrow)["points"][0][1], start + 150, "the bound arrow is rerouted by accept, as by any op")
        self.assertEqual(C._load_state(self.team).elements[box]["y"], 200)

    def test_a_secret_never_reaches_a_proposal(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, LEAD)["ids"][0]
        self.assertEqual(self.refused({"op": "edit", "id": box, "text": "key AKIA" + "Q" * 16, "intent": "t"})["code"], "secret_detected")
        self.assertEqual(self.scene()["proposals"], [])


class Files(CollabRig):
    """13.6: presence and checkpoint names come from validated ids and names only; reads cap their sizes."""

    def test_names_never_come_from_input(self):
        for bad in ("../V-1", "V-1/../../x", "E-1", "V-0", "V-1.json"):
            with self.subTest(vid=bad), self.assertRaises(HerdrTeamError):
                K.checkpoint_path(self.team, bad)
        self.assertEqual(self.refused({"op": "restore", "id": "../../etc"}, LEAD)["code"], "element_unknown")
        self.assertEqual(self.refused({"op": "checkpoint", "remove": "../x", "intent": "t"})["code"], "element_unknown")
        with self.assertRaises(HerdrTeamError):
            P.write_human(self.team, "../../0123456789ab", {}, 0.0)
        self.assertIsNone(P.write_member(self.team, C.CanvasAuthor("../x", "member", "cli", True), status="drawing"))
        self.assertFalse(any(p.name.startswith("..") for p in P.presence_dir(self.team).glob("*")) if P.presence_dir(self.team).exists() else False)

    def test_a_checkpoint_file_past_the_cap_is_not_read(self):
        vid = self.ok({"op": "checkpoint", "label": "start"}, LEAD)["ids"][0]
        K.checkpoint_path(self.team, vid).write_bytes(b"{" + b" " * (K.MAX_CHECKPOINT_BYTES + 1) + b"}")
        self.assertEqual(self.refused({"op": "restore", "id": vid}, LEAD)["code"], "canvas_limit")


class RequestsNeverOrders(CollabRig):
    """13.8: proposals are phrased as suggestions; the operator decides."""

    def test_phrasing(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, LEAD)["ids"][0]
        found = self.proposed({"op": "move", "id": box, "by": [0, 200], "intent": "line it up"})
        entry = next(e for e in C.display(self.team)["entries"] if e["id"] == found["proposal"])
        self.assertIn("alpha-member suggests: line it up", entry["items"][-1]["items"][1]["lines"][0]["t"])
        text = C.apply_text(self.apply([{"op": "move", "id": box, "by": [0, 220], "intent": "t"}], PEER))
        self.assertIn("it waits for the operator", text)
        guide = (PLUGIN_ROOT / "skill-guides" / "references" / "canvas-collab.md").read_text(encoding="utf-8")
        self.assertIn("requests, never orders", guide)
        self.assertIn("the operator decides", guide)


class CspUnchanged(CollabRig):
    """13.5: the three page CSPs are the ones Phase 2 shipped, byte for byte."""

    def test_the_csps(self):
        self.assertEqual(W.PAGE_CSP, "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
                                     "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'self'; "
                                     "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.assertEqual(W.ASSET_CSP, "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'")
        self.assertEqual(W.VIZ_CSP, "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' http://127.0.0.1:{port}/viz-lib/; "
                                    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
                                    "form-action 'none'; base-uri 'none'; frame-ancestors http://127.0.0.1:{port} http://localhost:{port}")

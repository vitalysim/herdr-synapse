"""Checkpoints (canvas v2 phase 5, 7): named and automatic ones, the limits, restore as one batch (comments untouched),
restore after ``clear``, undoing a restore, the size cap, and ``purge``."""
from __future__ import annotations

import json
from unittest import mock

from collab_support import DEPUTY, LEAD, MEMBER, PEER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_collab as K
from herdr_team import canvas_presence as P


class Checkpoints(CollabRig):
    def setUp(self):
        super().setUp()
        self.box = self.ok({"op": "shape", "kind": "box", "text": "Keep", "at": [0, 0]}, LEAD)["ids"][0]

    def test_a_named_checkpoint_is_a_snapshot_file(self):
        version = C.current_version(self.team)
        entry = self.ok({"op": "checkpoint", "label": "before the rework", "intent": "t"})
        vid = entry["ids"][0]
        record = self.scene()["checkpoints"][0]
        self.assertEqual({k: record[k] for k in ("id", "label", "version", "by", "auto", "elements")},
                         {"id": vid, "label": "before the rework", "version": version, "by": "alpha-member", "auto": False, "elements": 1})
        doc = json.loads(K.checkpoint_path(self.team, vid).read_text())
        self.assertEqual((doc["v"], doc["id"], doc["version"], [e["id"] for e in doc["elements"]]), (1, vid, version, [self.box]))
        self.assertEqual(self.refused({"op": "checkpoint", "intent": "t"})["details"]["field"], "label")

    def test_limits_and_removal(self):
        made = [self.ok({"op": "checkpoint", "label": "n{}".format(n), "intent": "t"})["ids"][0] for n in range(4)]
        mine = [c["id"] for c in self.scene()["checkpoints"] if c["by"] == "alpha-member"]
        self.assertEqual(mine, made[1:], "each author keeps 3; the oldest is replaced")
        self.assertFalse(K.checkpoint_path(self.team, made[0]).exists())
        with mock.patch.object(K, "LEAD_NAMED_CHECKPOINTS", 2):
            leads = [self.ok({"op": "checkpoint", "label": "l{}".format(n)}, LEAD)["ids"][0] for n in range(3)]
        self.assertEqual([c["id"] for c in self.scene()["checkpoints"] if c["by"] == "human"], leads[1:])
        self.assertEqual(self.refused({"op": "checkpoint", "remove": made[1], "intent": "t"}, PEER)["code"], "element_not_yours")
        self.ok({"op": "checkpoint", "remove": made[1], "intent": "t"})
        self.ok({"op": "checkpoint", "remove": made[2]}, LEAD)
        self.assertFalse(K.checkpoint_path(self.team, made[1]).exists())
        self.assertEqual(self.refused({"op": "checkpoint", "remove": made[1], "intent": "t"})["code"], "element_unknown")

    def test_a_big_batch_from_an_agent_saves_an_automatic_one_first(self):
        with mock.patch.object(K, "AUTO_CHECKPOINT_OPS", 3):
            result = self.apply([{"op": "shape", "text": "n{}".format(n), "at": [0, 200 + 120 * n], "intent": "t"} for n in range(3)])
            self.ok({"op": "shape", "text": "small", "at": [800, 0], "intent": "t"})
            self.apply([{"op": "shape", "text": "l{}".format(n), "at": [400, 200 + 120 * n]} for n in range(3)], LEAD)
        autos = [c for c in self.scene()["checkpoints"] if c["auto"]]
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0]["label"], "before {} (a batch of 3 ops)".format(result["batch"]))
        self.assertEqual(autos[0]["elements"], 1, "the canvas before the batch")
        first_event = [e for e in C._read_events(self.team) if e.get("batch") == result["batch"]][0]
        self.assertIn("checkpoint", [c["target"] for c in first_event["changes"]])

    def test_automatic_ones_are_capped(self):
        with mock.patch.object(K, "AUTO_CHECKPOINTS", 2), mock.patch.object(K, "AUTO_CHECKPOINT_OPS", 1):
            for n in range(3):
                self.ok({"op": "shape", "text": "n{}".format(n), "at": [0, 200 + 120 * n], "intent": "t"})
        self.assertEqual(len([c for c in self.scene()["checkpoints"] if c["auto"]]), 2)

    def test_restore_is_one_batch_that_keeps_comments_and_can_be_undone(self):
        vid = self.ok({"op": "checkpoint", "label": "start"}, LEAD)["ids"][0]
        self.ok({"op": "move", "id": self.box, "by": [100, 0]}, LEAD)
        drawn = self.ok({"op": "shape", "text": "Later", "at": [900, 0], "intent": "t"})["ids"][0]
        comment = self.ok({"op": "comment", "at": [900, 200], "text": "keep me", "intent": "t"})["ids"][0]
        self.ok({"op": "claim", "region": [2000, 0, 2400, 400], "label": "mine", "intent": "t"})
        result = self.apply([{"op": "restore", "id": vid}], LEAD)
        entry = result["applied"][0]
        self.assertEqual(entry["restore"], {"from": vid, "added": 0, "changed": 1, "deleted": 1})
        self.assertEqual(self.el(self.box)["x"], 0)
        self.assertFalse(self.has(drawn))
        self.assertTrue(self.has(comment), "conversation is never rolled back")
        self.assertTrue(any(c["label"] == "mine" for c in self.scene()["claims"]), "claims are untouched")
        autos = [c for c in self.scene()["checkpoints"] if c["auto"]]
        self.assertEqual(autos[-1]["label"], "before {} (restore {})".format(result["batch"], vid))
        self.ok({"op": "undo", "batch": result["batch"]}, LEAD)
        self.assertTrue(self.has(drawn))
        self.assertEqual(self.el(self.box)["x"], 100)
        self.assertIn("restored {}".format(vid), C.apply_text(result))

    def test_restore_after_clear_and_clear_saves_one(self):
        vid = self.ok({"op": "checkpoint", "label": "start"}, LEAD)["ids"][0]
        C.clear(self.layout, self.team, "human")
        scene = self.scene()
        self.assertEqual(scene["elements"], [])
        self.assertEqual([c["label"] for c in scene["checkpoints"]], ["start", "before clear"])
        self.assertTrue(K.checkpoint_path(self.team, vid).exists())
        self.ok({"op": "restore", "id": vid}, LEAD)
        self.assertEqual(self.el(self.box)["text"], "Keep")
        self.assertEqual(self.ok({"op": "checkpoint", "label": "after"}, LEAD)["ids"][0], "V-4", "ids keep counting across a clear")

    def test_the_size_cap(self):
        with mock.patch.object(K, "MAX_CHECKPOINT_BYTES", 100):
            self.assertEqual(self.refused({"op": "checkpoint", "label": "too big", "intent": "t"})["code"], "canvas_limit")

    def test_restore_is_the_leads(self):
        vid = self.ok({"op": "checkpoint", "label": "start"}, LEAD)["ids"][0]
        for author in (DEPUTY, MEMBER):
            self.assertEqual(self.refused({"op": "restore", "id": vid, "intent": "t"}, author)["code"], "operator_only")
        self.assertEqual(self.refused({"op": "restore", "id": "V-99"}, LEAD)["code"], "element_unknown")
        K.checkpoint_path(self.team, vid).unlink()
        self.assertEqual(self.refused({"op": "restore", "id": vid}, LEAD)["code"], "element_unknown")

    def test_look_lists_the_newest_five(self):
        for n in range(7):
            self.ok({"op": "checkpoint", "label": "c{}".format(n)}, LEAD)
        look = C.look(self.layout, self.team, "alpha-member")
        self.assertEqual([c["label"] for c in look["checkpoints"]], ["c2", "c3", "c4", "c5", "c6"])
        self.assertIn('checkpoints: V-3 "c2" v', look["text"])

    def test_purge_removes_checkpoints_and_presence(self):
        self.ok({"op": "checkpoint", "label": "start"}, LEAD)
        P.write_member(self.team, MEMBER, status="drawing")
        self.assertTrue(K.checkpoints_dir(self.team).is_dir() and P.presence_dir(self.team).is_dir())
        C.purge(self.team)
        self.assertFalse(K.checkpoints_dir(self.team).exists())
        self.assertFalse(P.presence_dir(self.team).exists())


class ClearKeepsCheckpointAssets(CollabRig):
    def test_a_restore_after_clear_keeps_its_pictures(self):
        import os

        from herdr_team import store, workdir

        png = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (4).to_bytes(4, "big") + (3).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 16
        project = self.ts.tmp / "project"
        project.mkdir()
        doc = store.read_json(self.team.team_json)
        doc.setdefault("config", {})["project_dir"] = os.fspath(project)
        store.write_json(self.team.team_json, doc)
        art = workdir.paths_for(os.fspath(project), "alpha")["artifacts"]
        art.mkdir(parents=True, exist_ok=True)
        (art / "pic.png").write_bytes(png)
        image = self.ok({"op": "image", "path": "pic.png", "at": [0, 0]}, LEAD)["ids"][0]
        asset = self.el(image)["asset"]
        vid = self.ok({"op": "checkpoint", "label": "with a picture"}, LEAD)["ids"][0]
        C.clear(self.layout, self.team, "human")
        self.assertTrue(C.asset_path(self.team, asset).is_file(), "a kept checkpoint's asset is copied back")
        self.ok({"op": "restore", "id": vid}, LEAD)
        self.assertEqual(self.el(image)["asset"], asset)

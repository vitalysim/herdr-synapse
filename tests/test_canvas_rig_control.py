"""The rig's control protocol (canvas v2 phase 5, I-9): a page test acts as other agents through JSON lines on the rig's
stdin; each command answers one JSON object."""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest

from support import PLUGIN_ROOT


def load(name):
    tools = str(PLUGIN_ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


QA = load("canvas_qa")
RIG = load("canvas_rig")


class Control(unittest.TestCase):
    def setUp(self):
        self.qa = QA.QaTeam()
        self.addCleanup(self.qa.cleanup)
        doc = QA.load_scene_file(QA.SCENES_DIR / "collab.json")
        QA.apply_scene(self.qa, doc)

    def command(self, message):
        answer = RIG.command(self.qa, json.loads(json.dumps(message)))
        json.dumps(answer, default=str)  # one JSON line
        return answer

    def test_ops_as_each_author(self):
        result = self.command({"as": "drawer", "ops": [{"op": "move", "id": "E-4", "by": [0, 20], "intent": "t"}]})["result"]
        self.assertEqual((result["proposed"][0]["reason"], result["proposed"][0]["superseded"]), ("human_made", ["P-1"]))
        pid = result["proposed"][0]["proposal"]
        result = self.command({"as": "peer", "ops": [{"op": "shape", "text": "peer", "at": "c200r0", "intent": "t"}]})["result"]
        self.assertEqual(len(result["applied"]), 1)
        result = self.command({"as": "deputy", "ops": [{"op": "move", "id": "E-2", "by": [0, 20], "intent": "t"}]})["result"]
        self.assertEqual(result["proposed"][0]["reason"], "frozen", "a delegate is bound by a freeze")
        result = self.command({"as": "lead", "ops": [{"op": "accept", "id": pid}]})["result"]
        self.assertEqual(result["applied"][0]["op"], "accept")
        base = result["version"]
        self.command({"as": "lead", "ops": [{"op": "move", "id": "E-4", "by": [20, 0]}]})
        result = self.command({"as": "drawer", "ops": [{"op": "restyle", "id": "E-4", "tone": "info", "intent": "t"}], "base": base})["result"]
        self.assertEqual(result["proposed"][0]["base_note"][0].split(":")[0], "E-4")

    def test_focus_look_settings_and_human(self):
        self.assertEqual(self.command({"as": "peer", "focus": {"region": "c0r0:c20r10", "intent": "checking", "status": "waiting", "ttl_s": 60}}),
                         {"ok": True})
        self.assertEqual(self.command({"human": {"page": "0123456789abcdef", "viewport": [0, 0, 800, 600], "selection": ["E-4"]}}), {"ok": True})
        look = self.command({"as": "drawer", "look": {"since": "last", "proposals": True}})["look"]
        self.assertIn("operator: viewing c0r0:c40r30 · selected E-4", look["text"])
        self.assertIn('here: qa-peer waiting c0r0:c20r10 "checking"', look["text"])
        self.assertIn("open proposals:", look["text"])
        self.assertEqual(self.command({"as": "drawer", "look": {"region": "operator"}})["look"]["region"], [0, 0, 800, 600])
        result = self.command({"settings": {"human_edits": "live"}})["result"]
        self.assertEqual(result["applied"][0]["op"], "settings")

    def test_refusals_answer_an_error(self):
        self.assertEqual(self.command({"as": "nobody", "ops": []})["error"]["code"], "usage")
        self.assertEqual(self.command({"as": "drawer"})["error"]["code"], "usage")
        self.assertEqual(self.command([1, 2])["error"]["code"], "usage")
        answer = self.command({"as": "drawer", "ops": [{"op": "accept", "id": "P-1", "intent": "t"}]})
        self.assertEqual(answer["result"]["refused"][0]["code"], "operator_only")
        self.assertEqual(self.command({"as": "drawer", "focus": {"status": "sleeping"}})["error"]["code"], "usage")

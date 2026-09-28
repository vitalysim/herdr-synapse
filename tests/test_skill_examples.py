"""The canvas guides' examples are the smoke test (canvas v2 phase 2, 7.1, G11; phases 3 and 4, 6).

Every fenced ``json`` block of every ``skill-guides/references/canvas*.md`` (the canvas, blocks, diagrams, charts and 3D
guides) is applied, in file order, on a fresh team as one member whose ``artifacts/`` holds
``tests/fixtures/canvas_artifacts/guides`` (``rev.csv``, ``traffic.json``, ``load.csv``, ``models/arm-v3.glb``): every op
must apply, none may be refused, and the board's final check holds no overlap, arrow through a mark, label that does
not fit, mark half in a frame, unreadable chart or intersecting 3D objects. An ``if_version`` in an example stands for
the version ``look`` printed: the test puts the block's current version there."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from test_canvas import WORKER, CanvasRig

from herdr_team import canvas as C
from herdr_team import paths

FIRST = ("canvas.md", "canvas-blocks.md", "canvas-diagrams.md")
GUIDES = FIRST + tuple(sorted(p.name for p in (paths.skill_guides_dir() / "references").glob("canvas*.md") if p.name not in FIRST))
DATA = Path(__file__).resolve().parent / "fixtures" / "canvas_artifacts" / "guides"
FENCE = re.compile(r"```json\n(.*?)\n```", re.S)
CLEAN = ("overlap", "arrow_through", "label_overflow", "frame_edge", "chart_labels", "scene3d_intersect")


def examples():
    out = []
    for name in GUIDES:
        text = (paths.skill_guides_dir() / "references" / name).read_text(encoding="utf-8")
        for number, block in enumerate(FENCE.findall(text)):
            out.append(("{} #{}".format(name, number + 1), json.loads(block)))
    return out


class GuideExamples(CanvasRig):
    def test_every_example_applies_and_the_board_checks_clean(self):
        art = self.artifacts()
        shutil.copytree(str(DATA), str(art), dirs_exist_ok=True)
        found = examples()
        self.assertGreaterEqual(len(found), 8, "the guides carry the worked examples")
        for where, doc in found:
            ops = doc["ops"] if isinstance(doc, dict) and "ops" in doc else [doc]
            for op in ops:
                if op.get("if_version") is not None:
                    target = next(e for e in self.scene()["elements"] if e.get("alias") == op["id"])
                    op["if_version"] = target["updated_seq"]
            result = self.apply(ops, WORKER, base=doc.get("base") if isinstance(doc, dict) and "ops" in doc else None)
            with self.subTest(example=where):
                self.assertEqual(result["refused"], [], where)
                self.assertEqual(len(result["applied"]), len(ops), where)
        problems = C.check(self.layout, self.team, "alpha-worker")["problems"]
        bad = [p for p in problems if p["code"] in CLEAN]
        self.assertEqual(bad, [], "\n".join("{}: {}".format(p["code"], p["message"]) for p in bad))

    def test_the_guides_stay_within_120_lines_and_name_each_other(self):
        root = paths.skill_guides_dir() / "references"
        self.assertLessEqual({"canvas-charts.md", "canvas-3d.md"}, set(GUIDES))
        for name in GUIDES:
            self.assertLessEqual(len((root / name).read_text(encoding="utf-8").splitlines()), 120, name)
        text = (root / "canvas.md").read_text(encoding="utf-8")
        self.assertIn("--reference canvas-blocks", text)
        self.assertIn("--reference canvas-diagrams", text)
        self.assertIn("--reference canvas-charts", text)
        self.assertIn("canvas-3d", text)
        self.assertIn("--reference canvas-collab", text)  # canvas v2 phase 5
        self.assertIn("canvas-collab.md", GUIDES)

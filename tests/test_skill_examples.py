"""The canvas guides' examples are the smoke test (canvas v2 phase 2, 7.1, G11).

Every fenced ``json`` block of ``canvas.md``, ``canvas-blocks.md`` and ``canvas-diagrams.md`` is applied, in file order,
on a fresh team as one member: every op must apply, none may be refused, and the board's final check holds no overlap,
arrow through a mark, label that does not fit, or mark half in a frame. An ``if_version`` in an example stands for the
version ``look`` printed: the test puts the block's current version there."""
from __future__ import annotations

import json
import re

from test_canvas import WORKER, CanvasRig

from herdr_team import canvas as C
from herdr_team import paths

GUIDES = ("canvas.md", "canvas-blocks.md", "canvas-diagrams.md")
FENCE = re.compile(r"```json\n(.*?)\n```", re.S)
CLEAN = ("overlap", "arrow_through", "label_overflow", "frame_edge")


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
        (art / "rev.csv").write_text("month,region,revenue\n2026-07,EU,10\n2026-07,US,12\n2026-08,EU,11\n2026-08,US,15\n", encoding="utf-8")
        found = examples()
        self.assertGreaterEqual(len(found), 8, "the guides carry the worked examples")
        for where, doc in found:
            ops = doc["ops"] if isinstance(doc, dict) and "ops" in doc else [doc]
            for op in ops:
                if op.get("if_version") is not None:
                    target = next(e for e in self.scene()["elements"] if e.get("alias") == op["id"])
                    op["if_version"] = target["updated_seq"]
            result = self.apply(ops, WORKER)
            with self.subTest(example=where):
                self.assertEqual(result["refused"], [], where)
                self.assertEqual(len(result["applied"]), len(ops), where)
        problems = C.check(self.layout, self.team, "alpha-worker")["problems"]
        bad = [p for p in problems if p["code"] in CLEAN]
        self.assertEqual(bad, [], "\n".join("{}: {}".format(p["code"], p["message"]) for p in bad))

    def test_the_guides_stay_within_120_lines_and_name_each_other(self):
        root = paths.skill_guides_dir() / "references"
        for name in GUIDES:
            self.assertLessEqual(len((root / name).read_text(encoding="utf-8").splitlines()), 120, name)
        text = (root / "canvas.md").read_text(encoding="utf-8")
        self.assertIn("--reference canvas-blocks", text)
        self.assertIn("--reference canvas-diagrams", text)

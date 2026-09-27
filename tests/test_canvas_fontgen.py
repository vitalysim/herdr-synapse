"""The font metrics generator (canvas v2 foundation): a standard-library TrueType reader, and
``assets/fonts/font-metrics.json`` kept fresh against the bundled fonts."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from herdr_team import canvas_fontgen as G


class Metrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(G.METRICS_PATH.read_text(encoding="utf-8"))

    def test_the_file_is_current(self):
        self.assertEqual(G.main(["--check"]), 0, "run python3 -m herdr_team.canvas_fontgen --write")

    def test_every_face_names_its_file_and_its_hash(self):
        self.assertEqual(list(self.doc["faces"]), [key for key, _rel, _weight in G.FACES])
        for key, face in self.doc["faces"].items():
            with self.subTest(face=key):
                path = G.FONTS_DIR / face["file"]
                self.assertEqual(face["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(face["family"], "Geist Mono" if key.startswith("mono") else "Inter")
                self.assertEqual((path.parent / "OFL.txt").is_file(), True, "each font ships with its licence")

    def test_advances_are_sorted_runs_without_private_use(self):
        for key, face in self.doc["faces"].items():
            previous_end = -1
            for first, widths in face["advances"]:
                self.assertGreater(first, previous_end, key)
                self.assertTrue(widths and all(isinstance(w, int) and w >= 0 for w in widths), key)
                previous_end = first + len(widths) - 1
                self.assertFalse(0xE000 <= first <= 0xF8FF, key)
            covered = {first + i for first, widths in face["advances"] for i in range(len(widths))}
            self.assertTrue(set(range(0x20, 0x7F)) <= covered, "{} covers printable ASCII".format(key))

    def test_geist_mono_is_fixed_width(self):
        widths = {w for _first, run in self.doc["faces"]["mono-400"]["advances"] for w in run if w}
        self.assertEqual(widths, {600})

    def test_a_stale_file_names_the_stale_face(self):
        fresh = G.generate()
        changed = json.loads(json.dumps(fresh))
        changed["faces"]["sans-600"]["sha256"] = "0" * 64
        self.assertEqual(G.stale_faces(G.dumps(changed), fresh), ["sans-600"])
        self.assertEqual(G.stale_faces("{not json", fresh), sorted(fresh["faces"]))
        with mock.patch.object(G, "METRICS_PATH", Path(tempfile.gettempdir()) / "no-such-metrics.json"), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(G.main(["--check"]), 1)
        self.assertIn("stale", err.getvalue())


class Reader(unittest.TestCase):
    def test_a_file_that_is_not_a_font_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as fh:
            fh.write(b"wOF2" + b"\x00" * 64)
        self.addCleanup(Path(fh.name).unlink)
        with self.assertRaises(G.FontError):
            G.read_face(Path(fh.name))

    def test_inter_reads_its_real_tables(self):
        face = G.read_face(G.FONTS_DIR / "inter" / "Inter-Regular.ttf")
        self.assertEqual((face["family"], face["units_per_em"]), ("Inter", 2048))
        self.assertGreater(face["ascender"], 0)
        self.assertLess(face["descender"], 0)
        self.assertTrue(face["cap_height"] and face["x_height"])


if __name__ == "__main__":
    unittest.main()

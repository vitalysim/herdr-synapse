"""Icons (canvas v2 phase 2, 4.2): resolve, aliases, suggestions, valid path data for every icon, and ``--check``."""
from __future__ import annotations

import unittest

from herdr_team import canvas_display as D
from herdr_team import canvas_icons as I


class Icons(unittest.TestCase):
    def test_resolve_names_aliases_and_lucides_own(self):
        self.assertEqual(I.resolve("database"), "database")
        self.assertEqual(I.resolve("Database"), "database")
        self.assertEqual(I.resolve("shield_check"), "shield-check")
        self.assertEqual(I.resolve("db"), "database")
        self.assertEqual(I.resolve("alert-circle"), "circle-alert", "an Iconify alias")
        self.assertIsNone(I.resolve("no-such-icon-at-all"))
        self.assertIsNone(I.resolve(None))

    def test_suggest_and_search(self):
        self.assertIn("database", I.suggest("databse"))
        self.assertLessEqual(len(I.suggest("databse", 2)), 2)
        found = I.search("rocket")
        self.assertEqual(found[0], "rocket")
        self.assertLessEqual(len(I.search("a")), I.SEARCH_MAX)

    def test_every_icon_is_valid_mlcqz(self):
        for name in I.names():
            paths = I.paths(name)
            self.assertTrue(paths, name)
            for d in paths:
                self.assertTrue(D._PATH_D.match(d) and d.startswith("M"), (name, d[:60]))

    def test_emit_is_a_scaled_group(self):
        group = I.emit("rocket", 10, 20, 48, "base.ink", D.LOD_LABEL)
        self.assertEqual((group["k"], group["t"], group["lod"]), ("group", [2.0, 0, 0, 2.0, 10, 20], D.LOD_LABEL))
        self.assertTrue(all(p["k"] == "path" and p["stroke"] == "base.ink" and p["sw"] == 2 for p in group["items"]))
        self.assertIsNone(I.emit("nope-nope", 0, 0, 24, "base.ink"))

    def test_the_vendored_files_match_their_checksums(self):
        self.assertEqual(I.check(), [])
        self.assertEqual(set(I.recorded_hashes()), set(I.VENDORED))

"""Editable documents: trust is explicit, saves are conservative and idempotent."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_team import charter, document_sync as sync, roster, store, workdir
from test_workdir import human
from support import TempState


class DocumentSyncTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.cleanup)
        self.layout, self.team = self.state.layout, self.state.team_name
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = self.layout.team(self.team)
        def configure(doc):
            doc.config.update(project_dir=self.tmp.name, document_sync="auto")
        roster.update_team(self.paths, configure)
        charter.set_rules(self.layout, self.team, human(), "original rules")
        self.name = "alpha-worker"
        charter.set_instructions(self.layout, self.team, human(), self.name, "original mission")
        workdir.render(self.layout, self.team)
        self.files = workdir.paths_for(self.tmp.name, self.team)
        self.member = self.files["members"] / (self.name + ".md")

    def settle(self):
        sync.scan(self.layout, self.team, 100)
        return sync.scan(self.layout, self.team, 103)

    def test_member_save_survives_render_and_imports_once(self):
        self.member.write_text(self.member.read_text().replace("original mission", "changed mission"))
        before = roster.load_team(self.paths).find(self.name).instructions_seq
        workdir.render(self.layout, self.team)
        self.assertIn("changed mission", self.member.read_text())
        self.assertEqual(self.settle()["imported"], [os.fspath(self.member)])
        self.assertIn("changed mission", charter.get_instructions(self.layout, self.team, self.name))
        workdir.render(self.layout, self.team)
        self.settle()
        self.assertEqual(roster.load_team(self.paths).find(self.name).instructions_seq, before + 1)
        events = [r for r in store.BoardStore(self.paths).read() if r.get("source") == "file-sync"]
        self.assertEqual(events[-1]["to"], [self.name])

    def test_rules_notify_agents_without_importing_findings(self):
        path = self.files["knowledge"]
        path.write_text(path.read_text().replace("original rules", "new rules"))
        self.settle()
        self.assertEqual(charter.get_rules(self.layout, self.team), "new rules")
        records = [r for r in store.BoardStore(self.paths).read() if r.get("source") == "file-sync"]
        self.assertIn(self.name, records[-1]["to"])
        self.assertNotIn("all", records[-1]["to"])

    def test_findings_edits_preserved_as_conflict(self):
        path = self.files["knowledge"]
        path.write_text(path.read_text().replace("_None yet._", "edited finding"))
        self.assertTrue(self.settle()["errors"])
        workdir.render(self.layout, self.team)
        self.assertIn("edited finding", path.read_text())
        self.assertEqual(charter.get_rules(self.layout, self.team), "original rules")

    def test_cli_change_conflicts_with_pending_file(self):
        self.member.write_text(self.member.read_text().replace("original mission", "file mission"))
        sync.scan(self.layout, self.team, 100)
        charter.set_instructions(self.layout, self.team, human(), self.name, "CLI mission")
        workdir.render(self.layout, self.team)
        self.assertIn("concurrent", sync.scan(self.layout, self.team, 103)["errors"][0]["error"])
        self.assertIn("CLI mission", charter.get_instructions(self.layout, self.team, self.name))

    def test_manual_is_default_for_existing_team(self):
        roster.update_team(self.paths, lambda doc: doc.config.pop("document_sync"))
        self.member.write_text(self.member.read_text().replace("original mission", "file mission"))
        self.assertFalse(self.settle()["imported"])
        self.assertIn("original mission", charter.get_instructions(self.layout, self.team, self.name))

    def test_deletion_never_clears_or_regenerates(self):
        self.member.unlink()
        self.assertTrue(sync.scan(self.layout, self.team)["errors"])
        workdir.render(self.layout, self.team)
        self.assertFalse(self.member.exists())
        self.assertIn("original mission", charter.get_instructions(self.layout, self.team, self.name))

    def test_invalid_utf8_and_symlinks_preserved(self):
        self.member.write_bytes(b"\xff")
        self.assertTrue(sync.scan(self.layout, self.team)["errors"])
        workdir.render(self.layout, self.team)
        self.assertEqual(self.member.read_bytes(), b"\xff")
        self.member.unlink()
        self.member.symlink_to(self.files["knowledge"])
        self.assertTrue(sync.scan(self.layout, self.team)["errors"])
        workdir.render(self.layout, self.team)
        self.assertTrue(self.member.is_symlink())

    def test_save_during_scan_restarts_debounce(self):
        original = self.member.read_text()
        self.member.write_text(original.replace("original mission", "first save"))
        sync.scan(self.layout, self.team, 100)
        self.member.write_text(original.replace("original mission", "second save"))
        self.assertFalse(sync.scan(self.layout, self.team, 103)["imported"])
        self.assertTrue(sync.scan(self.layout, self.team, 106)["imported"])

    def test_disable_before_settled_save(self):
        self.member.write_text(self.member.read_text().replace("original mission", "new"))
        sync.scan(self.layout, self.team, 100)
        roster.update_team(self.paths, lambda doc: doc.config.update(document_sync="manual"))
        self.assertFalse(sync.scan(self.layout, self.team, 103)["imported"])

    def test_private_notes_do_not_appear_in_notification(self):
        self.member.write_text(self.member.read_text().replace("## Notes", "## Notes\nPRIVATE-CANARY"))
        self.settle()
        events = [r for r in store.BoardStore(self.paths).read() if r.get("source") == "file-sync"]
        self.assertNotIn("PRIVATE-CANARY", str(events))

    def test_rename_between_scan_and_import_never_follows_alias(self):
        self.member.write_text(self.member.read_text().replace("original mission", "stale file"))
        original = charter._store_instructions
        def rename_then_store(*args, **kwargs):
            roster.update_team(self.paths, lambda doc: roster.apply_adoption(doc.find(self.name), "renamed-worker"))
            return original(*args, **kwargs)
        with patch.object(charter, "_store_instructions", side_effect=rename_then_store):
            result = self.settle()
        self.assertTrue(result["errors"])
        self.assertNotIn("stale file", charter.get_instructions(self.layout, self.team, "renamed-worker") or "")

    def test_generated_findings_do_not_trigger_rules_revision(self):
        before = charter.rules_seq(roster.load_team(self.paths))
        from test_workdir import agent
        charter.add_finding(self.layout, self.team, agent("alpha-worker"), "new peer finding")
        workdir.render(self.layout, self.team)
        self.assertFalse(self.settle()["imported"])
        self.assertEqual(charter.rules_seq(roster.load_team(self.paths)), before)

    def test_oversized_save_is_preserved(self):
        self.member.write_text(workdir.MARKER + "\n" + "x" * sync.MAX_BYTES)
        self.assertTrue(sync.scan(self.layout, self.team)["errors"])
        workdir.render(self.layout, self.team)
        self.assertGreater(self.member.stat().st_size, sync.MAX_BYTES)


if __name__ == "__main__":
    unittest.main()

"""A confirmation must describe and bind the authority it would change.

These regressions extend the owner-approved auto-sync contract with intervening
operator work, roster/project changes, canonical text, and failed persistence.
Every rig owns temporary state and a temporary checkout; no live session is used.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_team import charter, instructions_doc, roster, store, workdir
from herdr_team import document_sync as DS
from herdr_team.errors import HerdrTeamError
from test_auto_sync_confirm import (AGENT_PANE, FORGED, OPERATOR, confirm_command, edit_mission,
                                    edit_rules, held, settle, text_cli)
from test_inheritance_fixes import Rig, human


class InterruptedConfirmation(BaseException):
    """A process interruption escapes the ordinary command error handler."""


class ConfirmationContextOracle(unittest.TestCase):

    def setUp(self):
        self.rig = Rig(self, mode="auto")
        charter.set_rules(self.rig.layout, self.rig.name, human(), OPERATOR)
        self.rig.render()

    def rules_proposal(self, text=FORGED):
        edit_rules(self.rig, text)
        settle(self.rig)
        return held(self.rig)["knowledge.md"]

    def member_proposal(self, text=FORGED):
        edit_mission(self.rig, "alpha-worker", text)
        settle(self.rig)
        return held(self.rig)["members/alpha-worker.md"]

    def run_record(self, record):
        # Execute the actual advertised command; neither protocol id nor digest
        # format is embedded in the oracle.
        words = confirm_command(record)
        return text_cli(self.rig, *words[1:-2])

    def ident(self, record):
        return confirm_command(record)[3]

    def assert_refused_unchanged(self, record, expected=OPERATOR):
        before = charter.rules_seq(roster.load_team(self.rig.paths))
        code, out, err = self.run_record(record)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), expected)
        self.assertEqual(charter.rules_seq(roster.load_team(self.rig.paths)), before)

    def test_rules_changed_after_proposal_are_not_replaced_by_old_confirmation(self):
        record = self.rules_proposal()
        newer = "An operator decision made after the proposal."
        charter.set_rules(self.rig.layout, self.rig.name, human(), newer)
        self.assert_refused_unchanged(record, newer)

    def test_a_second_ordinary_edit_is_proposed_after_the_first_is_confirmed(self):
        first = self.rules_proposal()
        code, out, err = self.run_record(first)
        self.assertEqual(code, 0, out + err)
        edit_rules(self.rig, "A second ordinary operator rule.", old=FORGED)
        settle(self.rig, start=5000.0)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), FORGED)
        second = held(self.rig)["knowledge.md"]
        self.assertEqual(second["reason"], "proposed", "confirming a canonical edit must not disable later auto proposals")
        code, out, err = self.run_record(second)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), "A second ordinary operator rule.")

    def test_identical_rules_text_at_a_new_revision_invalidates_the_old_confirmation(self):
        record = self.rules_proposal()
        charter.set_rules(self.rig.layout, self.rig.name, human(), OPERATOR)
        self.assert_refused_unchanged(record)

    def test_instructions_changed_after_proposal_are_not_replaced(self):
        record = self.member_proposal()
        charter.set_instructions(self.rig.layout, self.rig.name, human(), "alpha-worker", "A new mission.")
        before = charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker")
        revision = roster.load_team(self.rig.paths).find("alpha-worker").instructions_seq
        code, out, err = self.run_record(record)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker"), before)
        self.assertEqual(roster.load_team(self.rig.paths).find("alpha-worker").instructions_seq, revision)

    def test_identical_instructions_at_a_new_revision_invalidate_confirmation(self):
        record = self.member_proposal()
        original = charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker") or ""
        charter.set_instructions(self.rig.layout, self.rig.name, human(), "alpha-worker", original)
        before = roster.load_team(self.rig.paths).find("alpha-worker").instructions_seq
        code, out, err = self.run_record(record)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker") or "", original)
        self.assertEqual(roster.load_team(self.rig.paths).find("alpha-worker").instructions_seq, before)

    def test_clearing_the_project_invalidates_the_old_confirmation(self):
        record = self.rules_proposal()
        roster.update_team(self.rig.paths, lambda doc: doc.config.pop("project_dir", None))
        self.assert_refused_unchanged(record)

    def test_changing_the_project_invalidates_the_old_confirmation(self):
        record = self.rules_proposal()
        with tempfile.TemporaryDirectory(prefix="sync-context-") as other:
            roster.update_team(self.rig.paths, lambda doc: doc.config.update(project_dir=os.fspath(Path(other).resolve())))
            self.assert_refused_unchanged(record)

    def test_switching_to_manual_invalidates_the_old_confirmation(self):
        record = self.rules_proposal()
        code, out, err = text_cli(self.rig, "project", "sync", "manual")
        self.assertEqual(code, 0, out + err)
        self.assert_refused_unchanged(record)

    def test_a_replacement_generation_with_the_same_name_cannot_receive_the_old_edit(self):
        record = self.member_proposal()
        before = charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker")

        def replace(doc):
            member = doc.find("alpha-worker")
            member.generation += 1
            member.terminal_id = "replacement-terminal"

        roster.update_team(self.rig.paths, replace)
        code, out, err = self.run_record(record)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker"), before)

    def test_proposal_shows_all_text_even_unchanged_lines_far_from_a_diff_hunk(self):
        original = "\n".join("Unchanged rule number {}.".format(i) for i in range(30))
        charter.set_rules(self.rig.layout, self.rig.name, human(), original)
        self.rig.render()
        incoming = original.replace("number 0.", "number zero.").replace("number 29.", "number twenty-nine.")
        record = self.rules_proposal(incoming)
        rendered = DS.describe_inherited(record)
        for line in incoming.splitlines():
            self.assertIn(line, rendered, "the operator cannot confirm text the proposal did not show")
        self.assertEqual(record["proposal"]["text"], incoming)

    def test_rules_proposal_shows_the_canonical_text_the_writer_will_store(self):
        raw = "A visible rule.\x1b[31m Red\x1b[0m\u202e hidden direction.\nTrailing spaces.   "
        expected = charter.sanitize(raw, charter.MAX_RULES_CHARS, code="rules_too_long").strip()
        record = self.rules_proposal(raw)
        self.assertEqual(record["proposal"]["text"], expected,
                         "the proposal must show canonical storage text before the operator confirms")
        self.assertNotIn("\x1b", DS.describe_inherited(record))
        code, out, err = self.run_record(record)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), expected)
        for line in expected.splitlines():
            self.assertIn(line, out)

    def test_instructions_proposal_shows_the_canonical_text_the_writer_will_store(self):
        raw = "A visible mission.\x1b[31m Red\x1b[0m\u202e hidden direction."
        # The edit preserves the generated blank sections. Their headings are
        # part of the instructions that the operator is asked to confirm.
        document = "## Mission\n\n" + raw + "\n\n" + "\n\n".join(
            "## " + heading for heading in ("Scope", "Constraints", "Definition of done", "Handoffs", "Notes"))
        clean = charter.sanitize(document, charter.MAX_INSTRUCTIONS_CHARS, code="instructions_too_long")
        expected = instructions_doc.to_text(instructions_doc.parse(clean)).strip()
        record = self.member_proposal(raw)
        self.assertEqual(record["proposal"]["text"], expected)
        self.assertNotIn("\x1b", DS.describe_inherited(record))
        code, out, err = self.run_record(record)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker"), expected)

    def test_same_file_bytes_in_a_new_authority_context_get_a_new_confirmation_id(self):
        first = self.rules_proposal()
        first_bytes = self.rig.files["knowledge"].read_bytes()
        newer = "A newly stored operator rule."
        charter.set_rules(self.rig.layout, self.rig.name, human(), newer)
        settle(self.rig, start=5000.0)
        second = held(self.rig)["knowledge.md"]
        self.assertEqual(self.rig.files["knowledge"].read_bytes(), first_bytes)
        self.assertEqual(first["proposal"]["digest"], second["proposal"]["digest"])
        self.assertNotEqual(self.ident(first), self.ident(second), "file digest alone cannot identify consent context")
        self.assertEqual(second["proposal"]["was"], newer)
        self.assert_refused_unchanged(first, newer)
        code, out, err = self.run_record(second)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), FORGED)

    def test_same_member_file_bytes_in_a_new_generation_get_a_new_confirmation_id(self):
        first = self.member_proposal()
        roster.update_team(self.rig.paths, lambda doc: setattr(doc.find("alpha-worker"), "generation", 2))
        settle(self.rig, start=5000.0)
        second = held(self.rig)["members/alpha-worker.md"]
        self.assertEqual(first["proposal"]["digest"], second["proposal"]["digest"])
        self.assertNotEqual(self.ident(first), self.ident(second))
        code, out, err = self.run_record(first)
        self.assertNotEqual(code, 0, out + err)

    def test_first_state_save_failure_refuses_before_authority_is_written(self):
        record = self.rules_proposal()
        revision = charter.rules_seq(roster.load_team(self.rig.paths))
        real = store.write_json

        def fail_confirmation_state(path, *args, **kwargs):
            if Path(path) == DS.state_path(self.rig.paths):
                raise OSError("injected confirmation state failure")
            return real(path, *args, **kwargs)

        with mock.patch.object(store, "write_json", side_effect=fail_confirmation_state):
            code, out, err = self.run_record(record)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), OPERATOR,
                         "a failed first persistence step must never adopt the proposed text")
        self.assertEqual(charter.rules_seq(roster.load_team(self.rig.paths)), revision)
        code, out, err = self.run_record(record)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), FORGED)

    def test_member_replaced_during_consent_save_cannot_receive_old_instructions(self):
        record = self.member_proposal()
        original = charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker")
        real = store.write_json
        changed = []

        def replace_during_save(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if Path(path) == DS.state_path(self.rig.paths) and not changed:
                changed.append(True)
                roster.update_team(self.rig.paths, lambda doc: setattr(doc.find("alpha-worker"), "generation", 9))
            return result

        with mock.patch.object(store, "write_json", side_effect=replace_during_save):
            code, out, err = self.run_record(record)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker"), original)

    def test_project_changed_during_consent_save_cannot_adopt_old_rules(self):
        record = self.rules_proposal()
        real = store.write_json
        changed = []

        def clear_during_save(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if Path(path) == DS.state_path(self.rig.paths) and not changed:
                changed.append(True)
                roster.update_team(self.rig.paths, lambda doc: doc.config.pop("project_dir", None))
            return result

        with mock.patch.object(store, "write_json", side_effect=clear_during_save):
            code, out, err = self.run_record(record)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), OPERATOR)

    def test_failed_proposal_post_is_not_marked_reported_and_retries(self):
        self.rules_proposal()
        from test_daemon import make_daemon
        daemon, _api, _clock = make_daemon(self.rig.state)
        with mock.patch.object(daemon, "_append_system", return_value=None):
            daemon.scan_teams(force=True)
            team = daemon.teams[self.rig.name]
            daemon._refresh_workdir(team)
        self.assertTrue(self.rig.render()["inherited"], "failed delivery cannot consume the report")
        daemon._refresh_workdir(team)
        self.assertFalse(self.rig.render()["inherited"], "successful delivery consumes the report exactly once")

    def test_failed_canvas_hold_post_does_not_spend_report_or_snapshot_version(self):
        self.rules_proposal()
        records = self.rig.render()["inherited"]
        from test_daemon import make_daemon
        daemon, _api, _clock = make_daemon(self.rig.state)
        with mock.patch.object(daemon, "_append_system", return_value=None):
            daemon.scan_teams(force=True)
        team = daemon.teams[self.rig.name]
        team.canvas_snapshot_ms = None
        with mock.patch("herdr_team.canvas.current_version", return_value=9999), \
                mock.patch.object(workdir, "render_canvas_snapshot", return_value={"inherited": records}), \
                mock.patch.object(daemon, "_append_system", return_value=None):
            daemon.snapshot_canvas(team, 100000.0)
        self.assertNotEqual(team.canvas_snapshot_version, 9999)
        self.assertTrue(self.rig.render()["inherited"])
        team.canvas_snapshot_ms = None
        with mock.patch("herdr_team.canvas.current_version", return_value=9999), \
                mock.patch.object(workdir, "render_canvas_snapshot", return_value={"inherited": records}):
            daemon.snapshot_canvas(team, 200000.0)
        self.assertEqual(team.canvas_snapshot_version, 9999)
        self.assertFalse(self.rig.render()["inherited"])

    def test_failed_board_view_notice_is_retried(self):
        from test_daemon import make_daemon
        daemon, _api, _clock = make_daemon(self.rig.state)
        daemon.scan_teams(force=True)
        team = daemon.teams[self.rig.name]
        self.rig.files["board"].write_text("A person's own notes.\n", encoding="utf-8")
        team.snapshot_seq = None
        team.snapshot_ms = None
        with mock.patch.object(daemon, "_append_system", return_value=None):
            daemon.snapshot_board(team, 100000.0)
        self.assertIsNone(team.board_view_announced)
        team.snapshot_ms = None
        daemon.snapshot_board(team, 200000.0)
        self.assertIsNotNone(team.board_view_announced)

    def test_switching_to_manual_removes_unusable_confirmation_commands(self):
        self.rules_proposal()
        code, out, err = text_cli(self.rig, "project", "sync", "manual")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(DS.pending_changes(self.rig.paths), [])
        self.assertFalse(any(" project confirm " in command for record in held(self.rig).values()
                             for command in record.get("commands") or []))
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), OPERATOR)

    def test_render_after_member_replacement_drops_old_consent_without_adopting(self):
        self.member_proposal()
        original = charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker")
        roster.update_team(self.rig.paths, lambda doc: setattr(doc.find("alpha-worker"), "generation", 9))
        self.rig.render()
        self.assertEqual(DS.pending_changes(self.rig.paths), [])
        self.assertFalse(any(" project confirm " in command for record in held(self.rig).values()
                             for command in record.get("commands") or []))
        self.assertEqual(charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker"), original)

    def test_roster_conflict_after_publication_reports_saved_authority_not_refusal(self):
        for member in (None, "alpha-worker"):
            with self.subTest(member=member):
                record = self.member_proposal() if member else self.rules_proposal()
                incoming = record["proposal"]["text"]
                real = store.RosterStore._save_locked
                failed = []

                def conflict_after_publication(owner, *args, **kwargs):
                    current = (charter.get_instructions(self.rig.layout, self.rig.name, member) if member
                               else charter.get_rules(self.rig.layout, self.rig.name))
                    if owner.team.team_json == self.rig.paths.team_json and current == incoming and not failed:
                        failed.append(True)
                        raise HerdrTeamError("roster_conflict", "injected post-publication conflict", 1)
                    return real(owner, *args, **kwargs)

                with mock.patch.object(store.RosterStore, "_save_locked", new=conflict_after_publication):
                    code, out, err = self.run_record(record)
                self.assertTrue(failed)
                self.assertEqual(code, 0, out + err)
                self.assertIn("adopted", out)
                self.assertIn("incomplete", out)

    def test_failed_canvas_view_notice_does_not_suppress_retry(self):
        from test_daemon import make_daemon
        daemon, _api, _clock = make_daemon(self.rig.state)
        daemon.scan_teams(force=True)
        team = daemon.teams[self.rig.name]
        reason = str(workdir.ForeignViewError(self.rig.files["root"] / "canvas.md"))
        with mock.patch.object(daemon, "_append_system", return_value=None) as post:
            daemon._announce_canvas_mirror(team, reason)
            daemon._announce_canvas_mirror(team, reason)
        self.assertEqual(post.call_count, 2)
        self.assertIsNone(team.canvas_mirror_announced)
        daemon._announce_canvas_mirror(team, reason)
        self.assertEqual(team.canvas_mirror_announced, reason)

    def test_an_agents_render_cannot_consume_the_operators_confirmation_notice(self):
        self.rules_proposal()
        code, out, err = text_cli(self.rig, "project", "render", pane=AGENT_PANE)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(any(record.get("reason") == "proposed" for record in self.rig.render()["inherited"]),
                        "showing an agent the edit is not notifying the operator")

    def test_state_save_failure_after_adoption_reports_adoption_and_retry_cannot_duplicate_it(self):
        record = self.rules_proposal()
        before = charter.rules_seq(roster.load_team(self.rig.paths))
        real = store.write_json
        failed = []

        def fail_after_adoption(path, *args, **kwargs):
            if Path(path) == DS.state_path(self.rig.paths) and charter.get_rules(self.rig.layout, self.rig.name) == FORGED:
                failed.append(True)
                raise OSError("injected post-adoption bookkeeping failure")
            return real(path, *args, **kwargs)

        with mock.patch.object(store, "write_json", side_effect=fail_after_adoption):
            code, out, err = self.run_record(record)
        self.assertTrue(failed, "the intended post-adoption failure was reached")
        self.assertEqual(code, 0, "authority was adopted, so reporting a bare failed command would be false: " + out + err)
        self.assertIn("adopted", out)
        self.assertIn(FORGED, out)
        self.assertTrue(any(word in (out + err).lower() for word in ("warning", "bookkeeping", "state", "held")),
                        "the lost release record needs an honest report")
        self.assertEqual(charter.rules_seq(roster.load_team(self.rig.paths)), before + 1)
        self.run_record(record)
        self.assertEqual(charter.rules_seq(roster.load_team(self.rig.paths)), before + 1,
                         "retrying the consumed confirmation must not store the same authority twice")
        settle(self.rig, start=5000.0)
        self.assertEqual(DS.pending_changes(self.rig.paths), [])
        self.assertEqual(charter.rules_seq(roster.load_team(self.rig.paths)), before + 1)

    def test_process_interruption_before_authority_write_can_be_reproposed_without_adopting(self):
        record = self.rules_proposal()
        with mock.patch.object(charter, "_store_rules", side_effect=InterruptedConfirmation):
            with self.assertRaises(InterruptedConfirmation):
                DS.confirm(self.rig.layout, self.rig.name, human(), self.ident(record))
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), OPERATOR)
        settle(self.rig, start=5000.0)
        waiting = held(self.rig)["knowledge.md"]
        code, out, err = self.run_record(waiting)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), FORGED)

    def test_process_interruption_after_authority_write_cannot_create_a_second_adoption(self):
        record = self.rules_proposal()
        before = charter.rules_seq(roster.load_team(self.rig.paths))
        real = charter._store_rules

        def interrupt_after_write(*args, **kwargs):
            real(*args, **kwargs)
            raise InterruptedConfirmation()

        with mock.patch.object(charter, "_store_rules", side_effect=interrupt_after_write):
            with self.assertRaises(InterruptedConfirmation):
                DS.confirm(self.rig.layout, self.rig.name, human(), self.ident(record))
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), FORGED)
        self.assertEqual(charter.rules_seq(roster.load_team(self.rig.paths)), before + 1)
        settle(self.rig, start=5000.0)
        self.assertEqual(DS.pending_changes(self.rig.paths), [], "already-stored authority must not be proposed again")
        self.run_record(record)
        self.assertEqual(charter.rules_seq(roster.load_team(self.rig.paths)), before + 1)

    def test_authority_writer_error_after_saved_rules_reports_the_saved_text(self):
        record = self.rules_proposal()
        real = store.atomic_write

        def error_after_publication(path, data, *args, **kwargs):
            result = real(path, data, *args, **kwargs)
            if Path(path) == self.rig.paths.rules_md:
                raise HerdrTeamError("injected_after_write", "injected failure after publishing Rules")
            return result

        with mock.patch.object(store, "atomic_write", side_effect=error_after_publication):
            code, out, err = self.run_record(record)
        self.assertEqual(charter.get_rules(self.rig.layout, self.rig.name), FORGED)
        self.assertIn(FORGED, out + err, "the command must disclose the exact text already saved before the error")
        self.assertTrue(any(word in (out + err).lower() for word in ("saved", "adopted", "stored")))
        self.assertNotIn("nothing was adopted", out + err)
        revision = charter.rules_seq(roster.load_team(self.rig.paths))
        self.run_record(record)
        settle(self.rig, start=5000.0)
        self.assertEqual(DS.pending_changes(self.rig.paths), [])
        self.assertEqual(charter.rules_seq(roster.load_team(self.rig.paths)), revision)

    def test_authority_writer_error_after_saved_instructions_reports_the_saved_text(self):
        record = self.member_proposal()
        expected = record["proposal"]["text"]
        target = self.rig.paths.instructions("alpha-worker")
        real = store.atomic_write

        def error_after_publication(path, data, *args, **kwargs):
            result = real(path, data, *args, **kwargs)
            if Path(path) == target:
                raise HerdrTeamError("injected_after_write", "injected failure after publishing instructions")
            return result

        with mock.patch.object(store, "atomic_write", side_effect=error_after_publication):
            code, out, err = self.run_record(record)
        self.assertEqual(charter.get_instructions(self.rig.layout, self.rig.name, "alpha-worker"), expected)
        self.assertIn(FORGED, out + err, "the command must disclose text already saved before the error")
        self.assertTrue(any(word in (out + err).lower() for word in ("saved", "adopted", "stored")))
        self.assertNotIn("nothing was adopted", out + err)
        revision = roster.load_team(self.rig.paths).find("alpha-worker").instructions_seq
        self.run_record(record)
        settle(self.rig, start=5000.0)
        self.assertEqual(DS.pending_changes(self.rig.paths), [])
        self.assertEqual(roster.load_team(self.rig.paths).find("alpha-worker").instructions_seq, revision)


class UpgradeAttributionOracle(unittest.TestCase):

    def test_upgrade_does_not_attribute_new_mirror_kinds_the_old_plugin_never_wrote(self):
        for rel, kind in (("facts.md", "facts"), ("canvas.json", "canvas-payload")):
            with self.subTest(rel=rel):
                rig = Rig(self, mode="manual")
                rig.files["root"].mkdir(parents=True)
                path = rig.files["root"] / rel
                planted = b"a byte-identical file planted before this mirror existed\n"
                path.write_bytes(planted)
                store.write_json(DS.state_path(rig.paths),
                                 {DS.SCHEMA_KEY: {"schema": workdir.STATE_SCHEMA, "upgraded": True}})
                guard = DS.FolderGuard(rig.layout, rig.name, rig.files)
                self.assertEqual(guard.write(path, planted, kind=kind), "unchanged")
                guard.save()
                later = DS.FolderGuard(rig.layout, rig.name, rig.files)
                self.assertEqual(later.write(path, b"the later mirror contents\n", kind=kind), "held",
                                 "positive upgrade evidence grants grace only to kinds the older plugin wrote")
                self.assertEqual(path.read_bytes(), planted)

    def test_unchanged_adoption_cannot_erase_claimed_bytes_when_member_is_renamed(self):
        rig = Rig(self, mode="auto")
        rig.render()
        path = rig.files["members"] / "alpha-reviewer.md"
        original = path.read_bytes()
        # A missing write record is not authorship, even when the current file
        # happens to equal the template this team would render.
        state = DS.load(rig.paths)
        state.pop(os.fspath(path))
        store.write_json(DS.state_path(rig.paths), state)
        rig.render()
        self.assertTrue(DS.load(rig.paths)[os.fspath(path)].get("claimed"))
        code, payload, err = rig.cli("instructions", "alpha-reviewer", "--adopt", "--yes")
        self.assertEqual(code, 0, err)
        self.assertFalse(payload["adopted"], "the shortest trigger is an unchanged adoption")
        self.assertEqual(path.read_bytes(), original)
        roster.update_team(rig.paths, lambda doc: roster.apply_adoption(doc.find("alpha-reviewer"), "alpha-reviewer-new"))
        rig.render()
        survives = path.read_bytes() == original or original in rig.everything_under_inherited().values()
        self.assertTrue(survives, "a no-op adoption did not write these claimed bytes; rename must keep them or "
                        "make a byte-verified copy before replacing them")


if __name__ == "__main__":
    unittest.main()

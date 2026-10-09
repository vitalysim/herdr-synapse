"""Guard the model's read allowance: a notifier changing authority is a violation, not consent."""

from __future__ import annotations

import os
import unittest

import test_folder_invariant as T
from herdr_team import charter, store
from herdr_team import document_sync as DS


class AutonomousReadCreditControl(unittest.TestCase):
    def world(self, mode="manual", rules=None):
        world = T.ScriptedWorld()
        self.addCleanup(world.close)
        world.new_gen(world.project, mode=mode, rules=rules)
        return world

    def test_unsolicited_authority_change_cannot_authorize_an_automatic_copy(self):
        """The old model excused a notifier's copy when the same step silently adopted authority."""
        w = self.world()
        data = w.disk().get("knowledge.md")
        self.assertIsNotNone(data)

        def silently_adopt_and_copy(out):
            charter.set_rules(w.gen.layout, T.TEAM, T.human(), "a notifier adopted these Rules")
            copy = w.root / "inherited" / ("knowledge-" + T.sha(data)[:16] + ".md")
            copy.parent.mkdir(parents=True, exist_ok=True)
            store.atomic_write(copy, data)

        w.label("an unsolicited notifier adoption and copy")
        w.code_step(w.step_label, silently_adopt_and_copy, adoption=["knowledge.md"])
        self.assertIn("UNCONFIRMED_AUTHORITY", w.checks(), w.violations)
        self.assertIn("NO_AUTOMATIC_COPY", w.checks(), w.violations)
        self.assertNotIn(("knowledge.md", T.sha(data)), w.model.reads)

    def test_real_explicit_confirmation_can_read_only_its_bound_document(self):
        """Verified operator confirmation is still permitted through its exact, independently bound path."""
        before = "Rules-before-oracle-control"
        incoming = "Rules-after-oracle-control"
        w = self.world(mode="auto", rules=before)
        path = w.root / "knowledge.md"
        original = path.read_bytes()
        self.assertIn(before.encode(), original)
        edited = original.replace(before.encode(), incoming.encode())
        w.world_write("knowledge.md", edited, "the operator edits the Rules document")
        w.label("the notifier proposes but does not adopt the edit")
        w.ev_settle()
        self.assertEqual(charter.get_rules(w.gen.layout, T.TEAM), before)
        entry = DS.load(w.gen.paths)[os.fspath(path)]
        proposal = entry.get("proposal")
        self.assertIsInstance(proposal, dict)
        consent = proposal["id"]
        bound_reads = [w._rel(path)]

        def confirm(out):
            code, payload, error = w.run_cli("project", "confirm", consent)
            self.assertEqual(code, 0, error)

        w.label("the operator confirms the exact proposal")
        w.code_step(w.step_label, confirm, adoption=["knowledge.md"], reads=bound_reads)
        self.assertEqual(charter.get_rules(w.gen.layout, T.TEAM), incoming)
        self.assertIn(("knowledge.md", T.sha(edited)), w.model.reads)
        self.assertEqual(w.violations, [])


if __name__ == "__main__":
    unittest.main()

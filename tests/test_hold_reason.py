"""A queued nudge shows why it is waiting, next to the pending count (sandbox, 2026-09-05)."""
from __future__ import annotations

import unittest

from herdr_team import tui_model as tm


def member(**extra):
    base = {"name": "red-dev-codex-reviwer", "kind": "codex", "role": "codex-reviewer", "agent_status": "idle", "status": "active", "pane_id": "w1:p2"}
    base.update(extra)
    return base


class HoldReasonTests(unittest.TestCase):
    def test_roster_row_shows_the_hold_reason_with_the_pending_count(self):
        line = tm.roster_line(member(pending_nudges=3, hold="focused"), width=120)
        self.assertIn("↪3 (focused)", line)
        line = tm.roster_line(member(pending_nudges=1, hold=None), width=120)
        self.assertIn("↪1", line)
        self.assertNotIn("(", line.split("↪1", 1)[1][:2])
        line = tm.roster_line(member(pending_nudges=0, hold="focused"), width=120)
        self.assertNotIn("focused", line)

    def test_ascii_variant(self):
        line = tm.roster_line(member(pending_nudges=2, hold="not_idle"), width=120, ascii_only=True)
        self.assertIn("^2 (not_idle)", line)


if __name__ == "__main__":
    unittest.main()

"""A team for the canvas v2 phase 5 tests (collaboration): a member, a peer, the manager and a delegate, and the operator.

``CollabRig`` is ``test_canvas.CanvasRig`` over that roster: ``apply``, ``ok`` (one op applied), ``proposed`` (it became a
proposal), ``refused``, ``scene``, ``el`` and ``by_alias``. The authors:

- ``LEAD``: the operator in person (a trusted shell); ``PAGE``: her writable page.
- ``DEPUTY``: a member holding an operator grant (a delegate).
- ``MANAGER``: the team's manager.
- ``MEMBER`` and ``PEER``: plain members.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List
from unittest import mock

from support import FAKE_MEMBERS, TempState, whiteboard_on

from herdr_team import canvas as C
from herdr_team import canvas_render as R
from herdr_team import store


def _row(name: str, role: str, kind: str, terminal: str, pane: str, manager: bool = False) -> Dict[str, Any]:
    row = dict(FAKE_MEMBERS[1], name=name, role=role, kind=kind, terminal_id=terminal, pane_id=pane, label="team:alpha/" + role)
    if manager:
        row["manager"] = True
    return row


MEMBERS: List[Dict[str, Any]] = [
    _row("alpha-member", "member", "claude", "term_m1", "w2:p1"),
    _row("alpha-peer", "peer", "codex", "term_p1", "w2:p2"),
    _row("alpha-manager", "manager", "claude", "term_g1", "w2:p3", manager=True),
    _row("alpha-deputy", "deputy", "codex", "term_d1", "w2:p4"),
    {"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"},
]

LEAD = C.CanvasAuthor("human", "human", "cli", True, operator=True)
PAGE = C.page_author(True)
MEMBER = C.CanvasAuthor("alpha-member", "member", "cli", True, agent="claude", team="alpha")
PEER = C.CanvasAuthor("alpha-peer", "member", "cli", True, agent="codex", team="alpha")
MANAGER = C.CanvasAuthor("alpha-manager", "member", "cli", True, agent="claude", manager=True, team="alpha")
DEPUTY = C.CanvasAuthor("alpha-deputy", "member", "cli", True, agent="codex", operator=True, team="alpha")
ACTORS = {"lead": LEAD, "deputy": DEPUTY, "manager": MANAGER, "member": MEMBER, "peer": PEER}


class CollabRig(unittest.TestCase):
    """A team with the whiteboard on, the collaboration roster, and no real resvg."""

    def setUp(self) -> None:
        self.ts = TempState(members=MEMBERS)
        self.addCleanup(self.ts.cleanup)
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        self.layout = self.ts.layout
        self.team = self.ts.team
        patcher = mock.patch.object(R, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def apply(self, ops: Any, author: Any = MEMBER, **kw: Any) -> Dict[str, Any]:
        return C.apply_ops(self.layout, self.team, list(ops), author, **kw)

    def ok(self, op: Dict[str, Any], author: Any = MEMBER, **kw: Any) -> Dict[str, Any]:
        result = self.apply([op], author, **kw)
        self.assertEqual((result["refused"], result["proposed"]), ([], []), result)
        return result["applied"][0]

    def proposed(self, op: Dict[str, Any], author: Any = MEMBER, **kw: Any) -> Dict[str, Any]:
        result = self.apply([op], author, **kw)
        self.assertEqual((result["applied"], result["refused"]), ([], []), result)
        return result["proposed"][0]

    def refused(self, op: Dict[str, Any], author: Any = MEMBER, **kw: Any) -> Dict[str, Any]:
        result = self.apply([op], author, **kw)
        self.assertEqual((result["applied"], result["proposed"]), ([], []), result)
        return result["refused"][0]

    def scene(self) -> Dict[str, Any]:
        return C.load_scene(self.team)

    def el(self, eid: str) -> Dict[str, Any]:
        return next(e for e in self.scene()["elements"] if e["id"] == eid)

    def has(self, eid: str) -> bool:
        return any(e["id"] == eid for e in self.scene()["elements"])

    def by_alias(self, alias: str) -> Dict[str, Any]:
        return next(e for e in self.scene()["elements"] if e.get("alias") == alias)

    def proposal(self, pid: str) -> Dict[str, Any]:
        return next(p for p in self.scene()["proposals"] if p["id"] == pid)

    def set_roster(self, **by_name: Dict[str, Any]) -> None:
        doc = store.read_json(self.team.team_json)
        for member in doc["members"]:
            member.update(by_name.get(member["name"], {}))
        store.write_json(self.team.team_json, doc)

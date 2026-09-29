"""The authority matrix of canvas v2 phase 5 (14.1, G6): every proposable op x every actor x where it lands x both settings.

The expected outcomes live in ``tests/fixtures/collab/matrix.json``, one row each: ``{"op", "actor", "target", "settings",
"expect": "live" | "propose" | "refuse", "reason"?, "code"?}``, so a new rule adds rows and no code. Each row runs as an
atomic batch with a failing second op, so nothing it does stays: one board per settings serves every row.

Actors: ``lead`` (the operator in person), ``deputy`` (a delegate), ``manager``, ``member``. Targets for an edit op: the
actor's ``own`` element, the ``operator``'s, a ``peer``'s, the actor's own element in a ``frozen_region``, frozen by id
(``frozen_id``) or in a ``locked`` region. For a create op: ``free`` space, the actor's ``own_lane`` (its claim), a
``peer_lane``, on the ``operator``'s or a ``peer``'s mark, on its ``own``, in a ``frozen_region`` or a ``locked`` one.
For ``undo`` (not proposable, but freezes, locks and authority bind it; QA phase 5 H1): the batch that drew the actor's
``own`` marks, or its marks now ``frozen_region``, ``frozen_id`` or ``locked``, the ``operator``'s batch or a ``peer``'s.
An undo row that applies but leaves frozen marks as they are says ``"skipped": "frozen"``; an undo that would take
nothing back (every mark it touched is frozen) is refused ``op_invalid``, saying why.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from collab_support import ACTORS, DEPUTY, LEAD, MANAGER, MEMBER, PEER, CollabRig

from herdr_team import canvas as C
from herdr_team.errors import HerdrTeamError

MATRIX = Path(__file__).resolve().parent / "fixtures" / "collab" / "matrix.json"
SETTINGS = {"default": {"human_edits": "propose", "frozen": "propose"}, "live_refuse": {"human_edits": "live", "frozen": "refuse"}}
COLUMN = {"deputy": 2000, "manager": 3200, "member": 4400}
FROZEN_REGION = [6000, 0, 7600, 400]
LOCKED_REGION = [6000, 1000, 7600, 1400]
KANBAN = {"op": "kanban", "title": "Work", "columns": [{"id": "todo", "title": "Todo", "cards": ["One", "Two"]}]}
ROWS = [{"day": "d{}".format(i), "v": v} for i, v in enumerate((3, 5, 4), 1)]


def load_rows() -> List[Dict[str, Any]]:
    return json.loads(MATRIX.read_text(encoding="utf-8"))["rows"]


class Board:
    """Everyone's marks, claims, freezes and a lock, drawn once; ``ids[(actor, what)]`` names them."""

    def __init__(self, rig: CollabRig, settings: Dict[str, str]) -> None:
        self.rig = rig
        self.ids: Dict[Any, str] = {}
        self.batches: Dict[Any, str] = {}
        ops = rig.apply([
            dict(op="shape", kind="box", text="Operator", at=[0, 0], id="op_box"),
            dict(KANBAN, id="op_kan", at=[0, 400]),
            dict(op="shape", kind="box", text="Lead frozen", at=[FROZEN_REGION[0] + 1220, 40], id="lead_fr"),
            dict(op="shape", kind="box", text="Lead locked", at=[LOCKED_REGION[0] + 1220, 1040], id="lead_lk"),
            dict(op="shape", kind="box", text="Lead held", at=[0, 1600], id="lead_fid"),
        ], LEAD)
        assert not ops["refused"], ops["refused"]
        for what in ("own", "frozen_region", "frozen_id", "locked"):
            self.batches[("lead", what)] = ops["batch"]
        peer = rig.apply([dict(op="shape", kind="box", text="Peer", at=[600, 0], id="peer_box", intent="t"),
                          dict(KANBAN, id="peer_kan", at=[600, 400], intent="t")], PEER)
        assert not peer["refused"] and not peer["proposed"], peer
        self.batches["peer"] = peer["batch"]
        self.batches["operator"] = ops["batch"]
        rig.ok({"op": "claim", "region": [1200, 800, 1800, 1200], "label": "peer lane", "intent": "t"}, PEER)
        for name, author in (("deputy", DEPUTY), ("manager", MANAGER), ("member", MEMBER)):
            x = COLUMN[name]
            index = list(COLUMN).index(name)
            result = rig.apply([dict(op="shape", kind="box", text="Own", at=[x, 0], id="own_box", intent="t"),
                                dict(KANBAN, id="own_kan", at=[x, 200], intent="t")], author)
            assert not result["refused"] and not result["proposed"], (name, result)
            self.batches[(name, "own")] = result["batch"]
            rig.ok({"op": "claim", "region": [x + 400, 0, x + 800, 200], "label": "own lane", "intent": "t"}, author)
            for what, op in (("frozen_region", dict(op="shape", kind="box", text="Frozen", at=[FROZEN_REGION[0] + 20 + index * 400, 40], id="fr_box")),
                             ("locked", dict(op="shape", kind="box", text="Locked", at=[LOCKED_REGION[0] + 20 + index * 400, 1040], id="lk_box")),
                             ("frozen_id", dict(op="shape", kind="box", text="Held", at=[x, 800], id="fid_box"))):
                result = rig.apply([dict(op, intent="t")], author)
                assert not result["refused"] and not result["proposed"], (name, result)
                self.batches[(name, what)] = result["batch"]
        lead = rig.apply([{"op": "freeze", "region": FROZEN_REGION, "label": "frozen"}, {"op": "lock", "region": LOCKED_REGION, "label": "locked"}],
                         LEAD)
        assert not lead["refused"], lead["refused"]
        scene = rig.scene()["elements"]
        for el in scene:
            if el.get("alias"):
                self.ids[(el["author"], el["alias"])] = el["id"]
        held = [self.ids[(a.name, "fid_box")] for a in (DEPUTY, MANAGER, MEMBER)] + [self.ids[("human", "lead_fid")]]
        assert not rig.apply([{"op": "freeze", "ids": held, "label": "held"}, {"op": "checkpoint", "label": "start"}], LEAD)["refused"]
        # One open proposal (the member's) for the decision ops, and the freeze and checkpoint ids.
        self.proposal = rig.apply([{"op": "move", "id": self.ids[("human", "op_box")], "by": [0, 20], "intent": "t"}], MEMBER)["proposed"][0]["proposal"]
        found = rig.scene()
        self.freeze = next(f["id"] for f in found["freezes"] if f.get("region"))
        self.checkpoint = found["checkpoints"][0]["id"]
        assert not rig.apply([{"op": "settings", **settings}], LEAD)["refused"]

    def element(self, actor: str, target: str) -> str:
        """The element an edit op aims at for this actor and target."""
        name = ACTORS[actor].name
        if target == "operator":
            return self.ids[("human", "op_box")]
        if target == "peer":
            return self.ids[(PEER.name, "peer_box")]
        if actor == "lead":
            return self.ids[("human", {"own": "op_box", "frozen_region": "lead_fr", "frozen_id": "lead_fid", "locked": "lead_lk"}[target])]
        return self.ids[(name, {"own": "own_box", "frozen_region": "fr_box", "frozen_id": "fid_box", "locked": "lk_box"}[target])]

    def block(self, actor: str, target: str) -> str:
        if target == "operator" or (actor == "lead" and target == "own"):
            return self.ids[("human", "op_kan")]
        if target == "peer":
            return self.ids[(PEER.name, "peer_kan")]
        return self.ids[(ACTORS[actor].name, "own_kan")]

    def spot(self, actor: str, target: str) -> List[int]:
        """Where a create op puts its mark for this actor and target."""
        x = COLUMN.get(actor, 0)
        index = list(COLUMN).index(actor) if actor in COLUMN else 3
        return {"free": [x, 3000], "own_lane": [x + 500, 40], "peer_lane": [1300, 880], "operator": [20, 20], "peer": [620, 20],
                "own": [x + 20, 20] if actor != "lead" else [20, 20], "frozen_region": [FROZEN_REGION[0] + 20 + index * 400, 240],
                "locked": [LOCKED_REGION[0] + 20 + index * 400, 1240]}[target]

    def op(self, row: Dict[str, Any]) -> Dict[str, Any]:
        name, actor, target = row["op"], row["actor"], row["target"]
        intent = {"intent": "t"}
        decisions = {"accept": {"id": self.proposal}, "reject": {"id": self.proposal}, "withdraw": {"id": self.proposal},
                     "freeze": {"region": [9000, 0, 9200, 200]}, "thaw": {"id": self.freeze}, "settings": {"human_edits": "live"},
                     "restore": {"id": self.checkpoint}, "checkpoint": {"label": "mine"},
                     "comment": {"at": self.ids[("human", "op_box")], "text": "a question"}}
        if name in decisions:
            return dict(decisions[name], op=name, **intent)
        if name == "undo":
            batch = self.batches[target] if target in ("operator", "peer") else self.batches[(actor, target)]
            return dict(op="undo", batch=batch, **intent)
        if name in ("shape", "chart"):
            at = self.spot(actor, target)
            if name == "shape":
                return dict(op="shape", kind="box", text="New", at=at, **intent)
            return dict(op="chart", type="bar", title="New", x="day", y="v", rows=ROWS, at=at, **intent)
        if name == "patch":
            return dict(op="patch", id=self.block(actor, target), add={"cards": ["Three"]}, **intent)
        eid = self.element(actor, target)
        return {"move": dict(op="move", id=eid, by=[20, 0]), "restyle": dict(op="restyle", id=eid, tone="danger"),
                "edit": dict(op="edit", id=eid, text="Changed"), "delete": dict(op="delete", id=eid),
                "pin": dict(op="pin", id=eid), "place": dict(op="place", id=eid, at=[9000, 9000])}[name] | intent


def outcome(rig: CollabRig, op: Dict[str, Any], author: Any) -> Dict[str, Any]:
    """What one op does, rolled back: an atomic batch whose second op is refused applies nothing, and says why."""
    try:
        rig.apply([op, {"op": "no_such_op"}], author, atomic=True)
    except HerdrTeamError as err:
        details = err.details
    else:
        raise AssertionError("the atomic batch was not refused")
    if details.get("proposed"):
        found = details["proposed"][0]
        return {"expect": "propose", "reason": found["reason"]}
    first = details["refused"][0]
    if first["index"] == 0:
        return {"expect": "refuse", "code": first["code"]}
    undo = (details["applied"][0].get("undo") or {}) if details.get("applied") else {}
    reasons = sorted({s.get("reason") for s in undo.get("skipped") or [] if s.get("reason")})
    if reasons:
        return {"expect": "live", "skipped": ",".join(reasons)}
    return {"expect": "live"}


class Matrix(CollabRig):
    def test_every_row(self):
        rows = load_rows()
        self.assertGreater(len(rows), 300)
        seen = set()
        for settings_name in sorted({row["settings"] for row in rows}):
            self.tearDown_state()
            board = Board(self, SETTINGS[settings_name])
            for row in rows:
                if row["settings"] != settings_name:
                    continue
                key = (row["op"], row["actor"], row["target"], row["settings"])
                self.assertNotIn(key, seen, "a row is listed twice")
                seen.add(key)
                with self.subTest(**{k: row[k] for k in ("op", "actor", "target", "settings")}):
                    found = outcome(self, board.op(row), ACTORS[row["actor"]])
                    want = {k: row[k] for k in ("expect", "reason", "code", "skipped") if k in row}
                    self.assertEqual(found, want)

    def tearDown_state(self) -> None:
        """A fresh team for each settings (the rows of one settings share a board)."""
        self.ts.cleanup()
        self.setUp()

    def test_the_matrix_covers_every_proposable_core_op_and_every_actor(self):
        rows = load_rows()
        ops = {row["op"] for row in rows}
        self.assertLessEqual({"move", "restyle", "edit", "delete", "pin", "place", "patch"}, ops)
        self.assertLessEqual({"shape", "chart"}, ops, "a kind op and a chart create")
        self.assertIn("undo", ops, "undo is not proposable, but freezes bind it (QA phase 5 H1)")
        self.assertEqual({row["actor"] for row in rows}, {"lead", "deputy", "manager", "member"})
        self.assertEqual({row["settings"] for row in rows}, set(SETTINGS))
        for row in rows:
            self.assertIn(row["expect"], ("live", "propose", "refuse"))
            self.assertEqual("reason" in row, row["expect"] == "propose", row)
            self.assertEqual("code" in row, row["expect"] == "refuse", row)
            self.assertTrue("skipped" not in row or (row["op"] == "undo" and row["expect"] == "live"), row)

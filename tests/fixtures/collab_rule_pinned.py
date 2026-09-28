"""A collaboration rule dropped in from outside (canvas v2 phase 5, G8): "a person's pin proposes".

A mark the operator pinned is hers to place: an agent's change to it becomes a proposal even under ``human_edits: live``.
``tests/test_canvas_collab_security.py`` registers it and shows it changes outcomes with no other file touched: a rule is
one function and one ``register_rule`` call (and a row in ``tests/fixtures/collab/matrix.json`` for a built-in one).
"""
from __future__ import annotations

from typing import Optional

from herdr_team.canvas_collab import Review, Verdict, register_rule, unregister_rule

NAME = "pinned"


def pinned(review: Review) -> Optional[Verdict]:
    hits = [c.id for c in review.changes if c.primary and c.before is not None and (c.before.get("pin") or {}).get("by") == "human"]
    if not hits:
        return None
    return Verdict("propose", NAME, "{} was placed by the operator".format(", ".join(hits)), tuple(hits))


def register() -> None:
    register_rule(NAME, pinned, 55)


def unregister() -> None:
    unregister_rule(NAME)

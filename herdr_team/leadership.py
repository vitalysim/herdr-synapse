"""Resolve creation choices before runtime or roster changes can make refusal partial."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from herdr_team.errors import EXIT_REFUSED, HerdrTeamError


def resolve(selector: Optional[str], candidates: Sequence[Tuple[str, str]], required: bool) -> Optional[str]:
    """Exact names win; role-qualified choices must identify one candidate member, never the first match."""
    if not selector:
        if required:
            raise HerdrTeamError("leader_required", "A new team needs a leader. Pass --leader NAME (or --manager NAME), or use a template with a matching manager role.", EXIT_REFUSED)
        return None
    if not selector.startswith("role:"):
        if any(name == selector for name, _ in candidates):
            return selector
        raise HerdrTeamError("member_not_found", "leader {!r} is not an agent member in this creation; use a final name or role:ROLE".format(selector), EXIT_REFUSED, {"name": selector, "members": [name for name, _ in candidates]})
    role = selector[5:]
    matches = [name for name, candidate_role in candidates if candidate_role == role]
    if len(matches) != 1:
        code = "leader_ambiguous" if matches else "member_not_found"
        raise HerdrTeamError(code, "leader role {!r} needs exactly one member; found {}. Use an exact member name.".format(role, len(matches)), EXIT_REFUSED, {"role": role, "members": matches})
    return matches[0]

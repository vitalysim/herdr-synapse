"""Team charter and per-member role briefs (plan 5.5, section 1.1).

The charter is the one piece of board-adjacent text that carries operator
authority. Human only: an agent pane, a hook, or ``--as human`` gets
``author_mismatch`` (audited). Text <= 2000 chars after sanitization plus
``refs``; over-length text is refused with a hint to ``--charter-file``,
which is copied into the team dir as ``charter.md`` and referenced. Every
write bumps ``charter.seq``, appends a ``system`` record ``charter_updated``
addressed to ``all`` with the new text, and nudges every member only with
``--urgent`` (the record's ``urgent`` flag is what the daemon reads).

Role briefs: one sentence or a short paragraph per member, delivered in
that member's briefing (first 300 chars) and by ``me``. Also human only.

``charter_seq_acked`` per member is recorded by ``ack`` (``ack_charter``)
and read by ``who`` to show ``charter: stale``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from herdr_team import sanitize as _sanitize
from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.identity import Author, audit
from herdr_team.paths import Layout, TeamPaths, canonicalize, check_not_symlink, ensure_team_dirs
from herdr_team import roster as _roster

MAX_CHARTER_CHARS = 2000
HEADLINE_CHARS = 120
MAX_BRIEF_CHARS = 300
MAX_BRIEF_TOTAL_CHARS = 2000
CHARTER_FILE_NAME = "charter.md"
MAX_CHARTER_FILE_BYTES = 1024 * 1024


@dataclass
class Charter:
    seq: int
    text: str
    refs: List[str] = field(default_factory=list)
    updated_at: Optional[str] = None
    updated_by: str = "human"

    def headline(self, max_chars: int = HEADLINE_CHARS) -> str:
        return headline_of(self.text, max_chars)

    def to_json(self) -> Dict[str, Any]:
        return {"seq": int(self.seq), "text": self.text, "refs": list(self.refs), "updated_at": self.updated_at, "updated_by": self.updated_by}

    @classmethod
    def from_json(cls, obj: Optional[Dict[str, Any]]) -> Optional["Charter"]:
        if not isinstance(obj, dict) or obj.get("seq") is None:
            return None
        try:
            seq = int(obj.get("seq") or 0)
        except (TypeError, ValueError):
            return None
        refs = [str(r) for r in (obj.get("refs") or []) if isinstance(r, str)]
        return cls(seq=seq, text=str(obj.get("text") or ""), refs=refs, updated_at=obj.get("updated_at"), updated_by=str(obj.get("updated_by") or "human"))


# --------------------------------------------------------------------------
# text helpers


def sanitize(text: str, max_len: int, code: str = "text_too_long") -> str:
    """The board sanitizer (``sanitize.sanitize_text``); ``code`` replaces ``text_too_long`` on overflow."""
    if not isinstance(text, str):
        raise HerdrTeamError("invalid_utf8", "text must be a string", EXIT_REFUSED)
    try:
        cleaned = _sanitize.sanitize_text(text, max_len=max_len)
    except HerdrTeamError as err:
        if err.code == "text_too_long" and code != "text_too_long":
            raise HerdrTeamError(code, err.message, EXIT_REFUSED, err.details)
        raise
    return cleaned.strip()


def headline_of(text: str, max_chars: int = HEADLINE_CHARS) -> str:
    """First non-empty line, collapsed whitespace, cut to ``max_chars`` with an ellipsis."""
    for line in (text or "").splitlines():
        candidate = " ".join(line.split())
        if candidate:
            if len(candidate) > max_chars:
                return candidate[: max(1, max_chars - 1)].rstrip() + "…"
            return candidate
    return ""


def require_human(layout: Layout, team: str, author: Author, action: str) -> None:
    """Charter and brief writes are human only; anything else is ``author_mismatch`` and audited."""
    if author.is_human:
        return
    audit(layout, team, "author_mismatch", author, {"action": action, "resolved": author.name, "via": author.via})
    raise HerdrTeamError(
        "author_mismatch",
        "{} is human only; this pane is {!r} ({})".format(action, author.name, author.via),
        EXIT_REFUSED,
        {"action": action, "author": author.name, "via": author.via, "pane_id": author.pane_id},
    )


# --------------------------------------------------------------------------
# reads


def get_charter(layout: Layout, team: str) -> Optional[Charter]:
    doc = _roster.load_team(layout.team(team))
    return Charter.from_json(doc.charter)


def charter_history(layout: Layout, team: str) -> List[Dict[str, Any]]:
    """The ``charter_updated`` board records, oldest first."""
    out: List[Dict[str, Any]] = []
    for record in _roster.read_board_records(layout.team(team), event="charter_updated"):
        charter = record.get("charter") if isinstance(record.get("charter"), dict) else {}
        out.append({
            "seq": record.get("seq"),
            "ts": record.get("ts"),
            "charter_seq": charter.get("seq"),
            "text": charter.get("text") if charter.get("text") is not None else record.get("text"),
            "refs": list(charter.get("refs") or record.get("refs") or []),
            "urgent": bool(record.get("urgent")),
            "updated_by": charter.get("updated_by") or "human",
        })
    return out


# --------------------------------------------------------------------------
# refs


def roster_roots(layout: Layout, team: str) -> List[Path]:
    """The team dir plus every member's cwd (the places a ref may point into)."""
    team_paths = layout.team(team)
    roots = [canonicalize(team_paths.root)]
    try:
        doc = _roster.load_team(team_paths)
    except HerdrTeamError:
        return roots
    for member in doc.members:
        if member.cwd:
            root = canonicalize(member.cwd)
            if root not in roots:
                roots.append(root)
    return roots


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_roots(roots: List[Path], env: Optional[Dict[str, str]] = None) -> List[Path]:
    """Drop roots that would open the whole home directory or the filesystem (register: cwd may be HOME after a restart)."""
    home: Optional[Path] = None
    value = (env or {}).get("HOME")
    if value:
        try:
            home = canonicalize(value)
        except Exception:  # noqa: BLE001 - a broken HOME only disables the check
            home = None
    out: List[Path] = []
    for root in roots:
        if root == Path(root.anchor):
            continue
        if home is not None and (root == home or _under(home, root)):
            continue
        out.append(root)
    return out


def _hidden_under(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in relative.parts)


def validate_refs(layout: Layout, team: str, refs: List[str], env: Optional[Dict[str, str]] = None) -> List[str]:
    """Refs must exist and live under the team dir or a roster root; returned normalized.

    A ref inside the team dir is returned relative to it (``charter.md``,
    ``payloads/42-diff.md``); one under a member's cwd stays absolute. A
    member cwd equal to HOME (a restart leaves it there) is not a root, and
    a ref under a dot-directory of a root (``.ssh``, ``.aws``, ``.config``)
    is refused at post time as well as at ``show --cat`` time.
    """
    team_paths = layout.team(team)
    team_root = canonicalize(team_paths.root)
    roots = safe_roots(roster_roots(layout, team), env)
    out: List[str] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            raise HerdrTeamError("ref_invalid", "empty ref", EXIT_REFUSED, {"ref": ref})
        raw = Path(os.path.expanduser(ref))
        if not raw.is_absolute():
            raw = team_paths.root / raw
        check_not_symlink(raw)
        resolved = canonicalize(raw)
        if not resolved.is_file():
            raise HerdrTeamError("ref_invalid", "ref does not exist or is not a file: {}".format(ref), EXIT_REFUSED, {"ref": ref, "path": os.fspath(resolved)})
        root = next((r for r in roots if _under(resolved, r)), None)
        if root is None:
            raise HerdrTeamError("ref_invalid", "ref must live under the team dir or a member's cwd: {}".format(ref), EXIT_REFUSED, {"ref": ref, "roots": [os.fspath(r) for r in roots]})
        if _hidden_under(resolved, root):
            raise HerdrTeamError("ref_invalid", "ref sits under a dot-directory (.ssh, .aws, .config ...): {}".format(ref), EXIT_REFUSED, {"ref": ref, "root": os.fspath(root)})
        if _under(resolved, team_root):
            normalized = os.fspath(resolved.relative_to(team_root))
        else:
            normalized = os.fspath(resolved)
        if normalized not in out:
            out.append(normalized)
    return out


def _copy_charter_file(team_paths: TeamPaths, file_path: str) -> str:
    source = Path(os.path.expanduser(file_path))
    check_not_symlink(source)
    try:
        st = os.stat(source)
    except OSError as err:
        raise HerdrTeamError("ref_invalid", "cannot read {}: {}".format(file_path, err), EXIT_REFUSED, {"path": file_path})
    if st.st_size > MAX_CHARTER_FILE_BYTES:
        raise HerdrTeamError("charter_too_long", "charter file exceeds {} bytes".format(MAX_CHARTER_FILE_BYTES), EXIT_REFUSED, {"path": file_path, "bytes": st.st_size})
    data = source.read_bytes()
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise HerdrTeamError("invalid_utf8", "charter file is not valid UTF-8", EXIT_REFUSED, {"path": file_path})
    ensure_team_dirs(team_paths)
    store.atomic_write(team_paths.charter_md, data)
    return data.decode("utf-8")


# --------------------------------------------------------------------------
# writes


def charter_updated_record_fields(charter: Charter, urgent: bool) -> Dict[str, Any]:
    """The extra fields of the ``charter_updated`` system record (text carries the charter)."""
    return {"urgent": bool(urgent), "refs": list(charter.refs), "charter": charter.to_json()}


def set_charter(layout: Layout, team: str, author: Author, text: Optional[str], file_path: Optional[str], refs: List[str], urgent: bool = False) -> Charter:
    """Validate authority and length, copy a file to ``charter.md``, bump seq, append ``charter_updated``."""
    require_human(layout, team, author, "charter set")
    team_paths = layout.team(team)
    _roster.load_team(team_paths)  # team_not_found before any write
    extra_refs = list(refs or [])
    if text is None and file_path is None:
        raise HerdrTeamError("usage", "charter set needs text or --file", 2)
    if text is not None and file_path is not None:
        raise HerdrTeamError("usage", "pass either text or --file, not both", 2)
    if file_path is not None:
        full = _copy_charter_file(team_paths, file_path)
        body = sanitize(full, MAX_CHARTER_FILE_BYTES, code="charter_too_long")
        if len(body) > MAX_CHARTER_CHARS:
            body = body[:MAX_CHARTER_CHARS].rstrip()
        extra_refs.insert(0, CHARTER_FILE_NAME)
    else:
        body = _sanitize_charter_text(str(text))
    if not body:
        raise HerdrTeamError("charter_empty", "charter text is empty after sanitization", EXIT_REFUSED)
    normalized_refs = validate_refs(layout, team, extra_refs)
    result: List[Charter] = []

    def mutate(doc: _roster.Team) -> None:
        current = Charter.from_json(doc.charter)
        seq = (current.seq if current else 0) + 1
        charter = Charter(seq=seq, text=body, refs=normalized_refs, updated_at=_roster.now_iso(), updated_by=author.name)
        doc.charter = charter.to_json()
        result.append(charter)

    _roster.update_team(team_paths, mutate)
    charter = result[0]
    _roster.append_system_record(
        team_paths, "charter_updated", charter.text, to=["all"],
        extra=charter_updated_record_fields(charter, urgent), socket=os.fspath(layout.socket),
    )
    audit(layout, team, "charter_set", author, {"charter_seq": charter.seq, "urgent": bool(urgent), "refs": normalized_refs})
    return charter


def _sanitize_charter_text(text: str) -> str:
    try:
        return sanitize(text, MAX_CHARTER_CHARS, code="charter_too_long")
    except HerdrTeamError as err:
        if err.code == "charter_too_long":
            err.message = "charter text exceeds {} characters; put it in a file and use --charter-file / --file".format(MAX_CHARTER_CHARS)
            err.details["hint"] = "--charter-file"
        raise


def set_brief(layout: Layout, team: str, author: Author, member_name: str, text: str) -> Dict[str, Any]:
    """Set a member's role brief (human only). Returns ``{"team","member","brief","briefing_line"}``."""
    require_human(layout, team, author, "brief set")
    body = sanitize(str(text), MAX_BRIEF_TOTAL_CHARS)
    team_paths = layout.team(team)
    updated: List[_roster.Member] = []

    def mutate(doc: _roster.Team) -> None:
        member = doc.find(member_name)
        if member is None or member.is_human:
            raise HerdrTeamError("member_not_found", "{!r} is not an agent member of team {!r}".format(member_name, team), EXIT_REFUSED, {"name": member_name, "team": team, "roster": doc.names()})
        member.brief = body or None
        updated.append(member)

    _roster.update_team(team_paths, mutate)
    member = updated[0]
    audit(layout, team, "brief_set", author, {"member": member.name, "chars": len(body)})
    return {"team": team, "member": member.name, "brief": member.brief, "briefing_line": brief_line(member.brief)}


def brief_line(brief: Optional[str]) -> Optional[str]:
    """The first ``MAX_BRIEF_CHARS`` of a brief on one line (the briefing's second line body)."""
    if not brief:
        return None
    one_line = " ".join(brief.split())
    if len(one_line) > MAX_BRIEF_CHARS:
        return one_line[: MAX_BRIEF_CHARS - 1].rstrip() + "…"
    return one_line


def ack_charter(layout: Layout, team: str, member_name: str, seq: Optional[int] = None) -> Dict[str, Any]:
    """Record ``charter_seq_acked`` for a member (``ack``); defaults to the current charter seq."""
    team_paths = layout.team(team)
    out: Dict[str, Any] = {}

    def mutate(doc: _roster.Team) -> None:
        member = doc.find(member_name)
        if member is None:
            raise HerdrTeamError("member_not_found", "{!r} is not in team {!r}".format(member_name, team), EXIT_REFUSED, {"name": member_name, "team": team})
        current = Charter.from_json(doc.charter)
        target = seq if seq is not None else (current.seq if current else None)
        if target is not None and (member.charter_seq_acked is None or int(member.charter_seq_acked) < int(target)):
            member.charter_seq_acked = int(target)
        out["member"] = member.name
        out["charter_seq_acked"] = member.charter_seq_acked
        out["charter_seq"] = current.seq if current else None

    _roster.update_team(team_paths, mutate)
    out["team"] = team
    out["stale"] = out.get("charter_seq") is not None and (out.get("charter_seq_acked") is None or out["charter_seq_acked"] < out["charter_seq"])
    return out


def charter_stale(member: _roster.Member, charter: Optional[Charter]) -> bool:
    if charter is None or member.is_human:
        return False
    return member.charter_seq_acked is None or int(member.charter_seq_acked) < charter.seq

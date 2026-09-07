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

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from herdr_team import sanitize as _sanitize
from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.identity import Author, audit
from herdr_team.paths import Layout, TeamPaths, canonicalize, check_not_symlink, ensure_team_dirs
from herdr_team import instructions_doc as _doc
from herdr_team import roster as _roster
from herdr_team import workdir as _workdir

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
    """Charter, rules and instructions writes carry the operator's authority.

    The operator passes, and so does a member the operator has explicitly
    delegated to (``herdr-synapse operator grant``), which is what lets an agent
    build and run a team end to end. A delegated write is audited as one, so
    the trail says who really typed it. Everything else is
    ``author_mismatch``, also audited.
    """
    if author.is_human:
        return
    if getattr(author, "operator", False):
        audit(layout, team, "operator_action", author, {"action": action, "resolved": author.name, "via": author.via})
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
    # ``.herdr-team/`` is a dot-directory the plugin itself created, and
    # ``artifacts/`` inside it is where members are told to put work products.
    # Refusing it would make the folder useless for the one thing it is for.
    # Decided on the *resolved* path: ``check_not_symlink`` lstats only the
    # final component, so an intermediate symlink could otherwise point out.
    project_dir = _workdir.project_dir_of(_roster.load_team(team_paths).to_json())
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
        if _hidden_under(resolved, root) and not _workdir.is_inside(resolved, project_dir):
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
        if member is None or member.is_human or member.status == "left":
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


# --------------------------------------------------------------------------
# knowledge base and per-member instructions
#
# Two authorities in one place. The rules are the operator's DOs and DON'Ts:
# human only, like the charter, which is what makes them safe to inject into
# an agent's context under operator authority. Findings are peer notes any
# member may append; they are attributed, escaped, and never injected as
# instructions. Instructions are the long form of a brief, also human only.
# Every one of these lives in the team state dir, not in the project folder,
# so no agent can edit what is later read back to its teammates as authority.

MAX_INSTRUCTIONS_CHARS = 4000
MAX_RULES_CHARS = 4000
MAX_FINDING_CHARS = 400
#: Findings kept in the rendered mirror and in reads; the file itself stays append-only.
MAX_FINDINGS_SHOWN = 200


def get_instructions(layout: Layout, team: str, member_name: str) -> Optional[str]:
    """The authoritative long-form instructions for one member, or None.

    The stored form since 0.6: the document's sections, without the title and
    the guidance comments the mirror carries. A pre-0.6 plain-text blob reads
    back as a Mission, so an upgrade needs no migration.
    """
    team_paths = layout.team(team)
    try:
        text = team_paths.instructions(member_name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    text = text.strip()
    return text or None


def get_instructions_doc(layout: Layout, team: str, member_name: str) -> "_doc.Sections":
    """One member's instructions as parsed sections (empty list when unset)."""
    return _doc.parse(get_instructions(layout, team, member_name))


def instructions_seq(member: Any) -> int:
    """The member's instructions revision; 0 when nothing was ever set."""
    if isinstance(member, dict):
        raw = member.get("instructions_seq")
    else:
        raw = getattr(member, "instructions_seq", None)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def instructions_stale(member: Any) -> bool:
    """True when the member has not acknowledged its current instructions."""
    seq = instructions_seq(member)
    if seq <= 0:
        return False
    if isinstance(member, dict):
        acked = member.get("instructions_seq_acked")
    else:
        acked = getattr(member, "instructions_seq_acked", None)
    try:
        return acked is None or int(acked) < seq
    except (TypeError, ValueError):
        return True


def set_instructions(layout: Layout, team: str, author: Author, member_name: str, text: Optional[str], file_path: Optional[str] = None, urgent: bool = False, announce: bool = True) -> Dict[str, Any]:
    """Set one member's long-form instructions (human only).

    ``announce`` is False only at team creation, where every member is briefed
    anyway and a record per member would be noise before anyone has read a board.
    """
    require_human(layout, team, author, "instructions set")
    team_paths = layout.team(team)
    doc = _roster.load_team(team_paths)
    member = doc.find(member_name)
    if member is None or member.is_human or member.status == "left":
        raise HerdrTeamError("member_not_found", "{!r} is not an agent member of team {!r}".format(member_name, team), EXIT_REFUSED, {"name": member_name, "team": team, "roster": doc.names()})
    if text is not None and file_path:
        raise HerdrTeamError("usage", "pass --set or --file, not both", 2, {"member": member_name})
    if file_path:
        body = _read_text_file(file_path, MAX_INSTRUCTIONS_CHARS, "instructions_too_long", truncate=False)
    else:
        body = sanitize(str(text or ""), MAX_INSTRUCTIONS_CHARS, code="instructions_too_long")
    # Whatever route the text came in by, it is stored as the document: sections
    # in a known order, guidance comments dropped. A plain paragraph becomes the
    # Mission, so ``--set "one sentence"`` still works exactly as it did.
    sections = _doc.parse(body)
    stored = _doc.to_text(sections) if not _doc.is_empty(sections) else ""
    ensure_team_dirs(team_paths)
    target = team_paths.instructions(member.name)
    store.atomic_write(target, stored.encode("utf-8"))
    revision = _bump_member_counter(team_paths, member.name, "instructions_seq")
    # Addressed to the member *and* to ``all``: the member learns its job changed
    # (a record naming nobody is a broadcast, which the delivery gate holds), and
    # teammates learn who owns what, which a shared folder cannot tell them.
    what = _doc.summary(sections, 200) if stored else "cleared"
    seq = None
    if announce:
        seq = _roster.append_system_record(
            team_paths, "instructions_updated",
            "{}'s instructions updated: {} (read them: herdr-synapse instructions {})".format(member.name, what, member.name),
            to=[member.name, "all"],
            extra={"urgent": bool(urgent), "member": member.name, "chars": len(stored), "instructions_seq": revision},
            socket=os.fspath(layout.socket),
        )
    audit(layout, team, "instructions_set", author, {"member": member.name, "chars": len(stored), "urgent": bool(urgent), "instructions_seq": revision})
    return {"team": team, "member": member.name, "chars": len(stored), "path": os.fspath(target), "record_seq": seq, "instructions_seq": revision}


def _bump_member_counter(team_paths: TeamPaths, member_name: str, field: str) -> int:
    """Increment one monotonic counter on a member and return its new value."""
    seen: List[int] = []

    def mutate(team: _roster.Team) -> None:
        member = team.find(member_name)
        if member is None:
            return
        current = getattr(member, field, None)
        try:
            nxt = int(current or 0) + 1
        except (TypeError, ValueError):
            nxt = 1
        setattr(member, field, nxt)
        seen.append(nxt)

    _roster.update_team(team_paths, mutate)
    return seen[0] if seen else 0


def rules_path(team_paths: TeamPaths) -> Path:
    """Where this team's rules are: ``rules.md``, or the pre-0.6 ``knowledge.md``.

    The old name is read when the new one is absent, so an upgrade needs no
    migration step; the first write moves the content to ``rules.md``.
    """
    if team_paths.rules_md.is_file():
        return team_paths.rules_md
    if team_paths.knowledge_md.is_file():
        return team_paths.knowledge_md
    return team_paths.rules_md


def get_rules(layout: Layout, team: str) -> Optional[str]:
    """The operator's DOs and DON'Ts for this team, or None."""
    try:
        text = rules_path(layout.team(team)).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    text = text.strip()
    return text or None


def rules_seq(doc: Any) -> int:
    """The team's rules revision from ``team.json`` ``config``; 0 when never set."""
    config = doc.get("config") if isinstance(doc, dict) else getattr(doc, "config", None)
    try:
        return int((config or {}).get("rules_seq") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def rules_stale(member: Any, doc: Any) -> bool:
    """True when the member has not acknowledged the team's current rules."""
    seq = rules_seq(doc)
    if seq <= 0:
        return False
    acked = member.get("rules_seq_acked") if isinstance(member, dict) else getattr(member, "rules_seq_acked", None)
    try:
        return acked is None or int(acked) < seq
    except (TypeError, ValueError):
        return True


def set_rules(layout: Layout, team: str, author: Author, text: Optional[str], file_path: Optional[str] = None, urgent: bool = False) -> Dict[str, Any]:
    """Set the team's rules (human only; carries operator authority)."""
    require_human(layout, team, author, "knowledge set")
    team_paths = layout.team(team)
    if file_path:
        body = _read_text_file(file_path, MAX_RULES_CHARS, "rules_too_long", truncate=False)
    else:
        body = sanitize(str(text or ""), MAX_RULES_CHARS, code="rules_too_long")
    ensure_team_dirs(team_paths)
    store.atomic_write(team_paths.rules_md, ((body + "\n") if body else "").encode("utf-8"))
    if team_paths.knowledge_md.is_file():
        # Pre-0.6 teams kept the rules here. The first write moves them to
        # ``rules.md``; leaving the old file would shadow it on the next read.
        try:
            os.unlink(team_paths.knowledge_md)
        except OSError:
            pass
    revision = _bump_team_counter(team_paths, "rules_seq")
    # The board is how a member learns anything changed. A ``system`` record to
    # ``all`` is seen on the next board read (and by Claude on its next prompt)
    # without waking anyone; ``--urgent`` nudges, exactly as the charter does.
    headline = _one_line(body, 200) if body else "the team rules were cleared"
    seq = _roster.append_system_record(
        team_paths, "knowledge_updated",
        "team rules updated: {} (full text: herdr-synapse knowledge)".format(headline),
        to=["all"], extra={"urgent": bool(urgent), "chars": len(body), "rules_seq": revision}, socket=os.fspath(layout.socket),
    )
    audit(layout, team, "knowledge_set", author, {"chars": len(body), "urgent": bool(urgent), "rules_seq": revision})
    return {"team": team, "chars": len(body), "path": os.fspath(team_paths.rules_md), "record_seq": seq, "rules_seq": revision}


def _bump_team_counter(team_paths: TeamPaths, field: str) -> int:
    """Increment one monotonic counter in ``team.json`` ``config`` and return it."""
    seen: List[int] = []

    def mutate(team: _roster.Team) -> None:
        try:
            nxt = int(team.config.get(field) or 0) + 1
        except (TypeError, ValueError):
            nxt = 1
        team.config[field] = nxt
        seen.append(nxt)

    _roster.update_team(team_paths, mutate)
    return seen[0] if seen else 0


def read_findings(layout: Layout, team: str, limit: int = MAX_FINDINGS_SHOWN) -> List[Dict[str, Any]]:
    """The newest findings, oldest first. A malformed line is skipped, never fatal."""
    path = layout.team(team).knowledge_jsonl
    out: List[Dict[str, Any]] = []
    raw = store.read_bytes(path, b"") or b""
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a half-written or hand-edited line is skipped, never fatal
        if isinstance(record, dict) and isinstance(record.get("text"), str):
            out.append(record)
    if limit and len(out) > limit:
        out = out[-limit:]
    return out


def add_finding(layout: Layout, team: str, author: Author, text: str) -> Dict[str, Any]:
    """Append one attributed finding. Any member may do this; it is a peer note, not a rule."""
    if not author.is_member and not author.is_human:
        raise HerdrTeamError("not_a_member", "this pane is not a member of team {!r}".format(team), EXIT_REFUSED, {"team": team, "author": author.name})
    body = sanitize(str(text or ""), MAX_FINDING_CHARS, code="finding_too_long")
    if not body:
        raise HerdrTeamError("usage", "a finding needs text", EXIT_REFUSED, {"team": team})
    body = " ".join(body.split())
    team_paths = layout.team(team)
    ensure_team_dirs(team_paths)
    record = {
        "at": store.now_iso(),
        "author": author.name,
        "kind": author.kind or ("human" if author.is_human else "?"),
        "text": body,
    }
    _append_finding(team_paths.knowledge_jsonl, record)
    # A finding is a peer note, so it goes on the board as one: attributed to the
    # member, addressed to everyone, and never phrased as an instruction.
    seq = _roster.append_system_record(
        team_paths, "knowledge_finding",
        "{} recorded a finding: {} (all of them: herdr-synapse knowledge)".format(author.name, body),
        # Not a "text" key: ``extra`` is merged into the record and would clobber it.
        to=["all"], extra={"author": author.name, "finding": body}, socket=os.fspath(layout.socket),
    )
    audit(layout, team, "knowledge_finding", author, {"chars": len(body)})
    return {"team": team, "finding": record, "record_seq": seq}


def _one_line(text: str, limit: int) -> str:
    """One line of at most ``limit`` chars, for a board record's summary."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _append_finding(path: Path, record: Dict[str, Any]) -> None:
    """One ``O_APPEND`` write of one line, so concurrent members cannot interleave."""
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    check_not_symlink(path)
    fd = store.secure_open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise HerdrTeamError("write_failed", "short write appending a finding", EXIT_REFUSED, {"path": os.fspath(path), "written": written, "expected": len(payload)})
    except OSError as err:
        raise HerdrTeamError("write_failed", "cannot append a finding: {}".format(err), EXIT_REFUSED, {"path": os.fspath(path)}) from err
    finally:
        os.close(fd)


def _read_text_file(file_path: str, max_chars: int, code: str, truncate: bool = True) -> str:
    """Read a ``--file`` argument the way ``set_charter`` reads ``--charter-file``."""
    resolved = canonicalize(file_path)
    check_not_symlink(resolved)
    try:
        st = os.stat(resolved)
    except OSError as err:
        raise HerdrTeamError("path_invalid", "cannot read {}: {}".format(file_path, err), EXIT_REFUSED, {"path": file_path}) from err
    if st.st_size > MAX_CHARTER_FILE_BYTES:
        raise HerdrTeamError(code, "file exceeds {} bytes".format(MAX_CHARTER_FILE_BYTES), EXIT_REFUSED, {"path": file_path, "bytes": st.st_size})
    try:
        raw = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        raise HerdrTeamError("path_invalid", "cannot read {}: {}".format(file_path, err), EXIT_REFUSED, {"path": file_path}) from err
    body = sanitize(raw, MAX_CHARTER_FILE_BYTES, code=code)
    if len(body) > max_chars:
        if not truncate:
            # Silently cutting a document mid-sentence is worse than refusing it:
            # ``--set`` already refuses, and the two must not disagree.
            raise HerdrTeamError(code, "{} is {} characters after sanitizing; the limit is {}".format(file_path, len(body), max_chars), EXIT_REFUSED, {"path": file_path, "chars": len(body), "max_chars": max_chars})
        body = body[:max_chars].rstrip()
    return body

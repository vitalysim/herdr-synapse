"""The per-member instructions document: a small, fixed Markdown skeleton.

One member's standing orders, written by the operator. The plugin knows six
section names and nothing else about the content, so the operator can write
whatever belongs under each.

Three forms of the same thing:

- **stored** (``to_text``): what lives in the team state dir at
  ``instructions/<name>.md``. Sections only, no title, no guidance comments.
  This is the authoritative copy, in a directory no agent can reach through
  the project checkout.
- **the file the operator edits** (``document``): the stored form plus a
  title, the member's role, and one HTML-comment hint per section, so the
  structure explains itself inside the file. This is what is mirrored to
  ``<project>/.herdr-synapse/<team>/members/<name>.md``.
- **what an agent sees** (``injected``): known sections in order, private
  ones dropped, comments dropped, empty sections omitted, every line escaped.

The reader is a line scanner in the style of ``cmd_misc.parse_key_bindings``:
no regex over the whole document, and a line it does not understand is kept
rather than fatal. It is the only Markdown reader in the package, so it stays
deliberately small: a ``##`` heading opens a section, everything else is body.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

#: The sections the plugin knows, in the order it writes them.
SECTIONS: Tuple[str, ...] = ("Mission", "Scope", "Constraints", "Definition of done", "Handoffs", "Notes")

#: Sections that never reach an agent. The operator's own margin.
PRIVATE_SECTIONS: Tuple[str, ...] = ("Notes",)

#: What each section is for. Written into the file as an HTML comment, which is
#: invisible in rendered Markdown, unambiguous to strip, and never injected.
HINTS: Dict[str, str] = {
    "Mission": "What is this member for? One or two sentences.",
    "Scope": "Owns / Does not own, so two members never collide.",
    "Constraints": "Musts and must-nots.",
    "Definition of done": "How this member knows a task is finished.",
    "Handoffs": "Who to post to, and when.",
    "Notes": "Private to you. Never sent to the agent.",
}

#: A parsed document: ``(section title, body lines)`` in the order they appear.
Sections = List[Tuple[str, List[str]]]

COMMENT_OPEN = "<!--"
COMMENT_CLOSE = "-->"
HEADING = "## "


def _is_comment(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(COMMENT_OPEN) and stripped.endswith(COMMENT_CLOSE)


def _canonical(title: str) -> str:
    """A known section's canonical spelling, or the title as written."""
    for known in SECTIONS:
        if known.lower() == title.lower():
            return known
    return title


def _trimmed(lines: Sequence[str]) -> List[str]:
    """Body lines with leading and trailing blanks removed; interior blanks kept."""
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def parse(text: Optional[str]) -> Sections:
    """Sections in document order, guidance comments dropped.

    Everything before the first ``##`` heading is the generated preamble (the
    title and the member's role, which come from the roster) and is dropped.
    A heading this module does not know is kept verbatim in place, so nothing
    the operator writes is ever lost. A document with no headings at all is a
    pre-0.6 plain-text instructions blob: it becomes the Mission.
    """
    if not text:
        return []
    sections: Sections = []
    leading: List[str] = []
    current: Optional[List[str]] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith(HEADING):
            title = _canonical(line[len(HEADING):].strip())
            if title:
                current = []
                sections.append((title, current))
                continue
        if _is_comment(line):
            continue
        if current is None:
            # Preamble: skip what ``document`` generates, keep the rest for the
            # legacy case below.
            if line.startswith("# ") or line.lower().startswith("role:"):
                continue
            leading.append(line)
            continue
        current.append(line)
    if not sections:
        body = _trimmed(leading)
        return [("Mission", body)] if body else []
    return [(title, _trimmed(body)) for title, body in sections]


def skeleton(mission: Optional[str] = None) -> Sections:
    """Every known section, empty but for the Mission when one is given."""
    body = _trimmed(str(mission or "").splitlines())
    return [(title, list(body) if title == "Mission" else []) for title in SECTIONS]


def complete(sections: Sections, mission: Optional[str] = None) -> Sections:
    """Add every missing standard section without discarding custom content.

    The first occurrence of each known heading is placed in canonical order;
    custom headings and duplicate known headings follow in their original
    order. ``mission`` only fills an absent or empty Mission, so explicit
    long-form instructions always win over the roster's short brief.
    """
    remaining = [(title, list(body)) for title, body in sections]
    out: Sections = []
    for wanted in SECTIONS:
        found: Optional[Tuple[str, List[str]]] = None
        for index, (title, body) in enumerate(remaining):
            if title == wanted:
                found = remaining.pop(index)
                break
        body = list(found[1]) if found is not None else []
        if wanted == "Mission" and not _trimmed(body) and mission:
            body = _trimmed(str(mission).splitlines())
        out.append((wanted, body))
    out.extend(remaining)
    return out


def mission_paragraph(sections: Sections) -> str:
    """The first non-empty Mission paragraph, collapsed to one line."""
    lines = section(sections, "Mission")
    paragraph: List[str] = []
    for line in lines:
        if not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(" ".join(line.split()))
    return " ".join(part for part in paragraph if part).strip()


def is_empty(sections: Sections) -> bool:
    return not any(body for _title, body in sections)


def section(sections: Sections, title: str) -> List[str]:
    """One section's body, empty when it is absent."""
    wanted = _canonical(title)
    for name, body in sections:
        if name == wanted:
            return list(body)
    return []


def with_section(sections: Sections, title: str, body: Sequence[str]) -> Sections:
    """``sections`` with one section replaced, appended when it was absent."""
    wanted = _canonical(title)
    out: Sections = []
    replaced = False
    for name, current in sections:
        if name == wanted and not replaced:
            out.append((name, _trimmed(body)))
            replaced = True
        else:
            out.append((name, list(current)))
    if not replaced:
        out.append((wanted, _trimmed(body)))
    return out


def to_text(sections: Sections) -> str:
    """The stored form: sections only, no title and no guidance comments."""
    out: List[str] = []
    for title, body in sections:
        out.append(HEADING + title)
        body = _trimmed(body)
        if body:
            out.append("")
            out.extend(body)
        out.append("")
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def document(name: str, team: str, role: str, sections: Sections, adopt_command: Optional[str] = None, note: Optional[Sequence[str]] = None) -> str:
    """The file the operator edits: title, role, guidance, then the sections.

    Every section keeps its hint, filled or not, so the document's shape stays
    the same across edits and explains itself to whoever opens it next.

    ``note`` is prose for the reader. It goes *above* the first heading on
    purpose: anything after the last one would be read back as that section's
    content, so a footer here would be adopted as part of the private notes.
    """
    out = ["# {} — {}".format(name, team), "", "Role: {}".format(role or "member"), ""]
    if adopt_command:
        out.append("{} Edit this file, then run:  {} {}".format(COMMENT_OPEN, adopt_command, COMMENT_CLOSE))
    out.append("{} Every section is optional; delete what you do not need. {}".format(COMMENT_OPEN, COMMENT_CLOSE))
    out.append("{} Everything except {} is sent to the agent. {}".format(COMMENT_OPEN, ", ".join(PRIVATE_SECTIONS), COMMENT_CLOSE))
    out.append("")
    if note:
        out.extend(note)
        out.append("")
    for title, body in sections or skeleton():
        out.append(HEADING + title)
        hint = HINTS.get(title)
        if hint:
            out.append("{} {} {}".format(COMMENT_OPEN, hint, COMMENT_CLOSE))
        body = _trimmed(body)
        if body:
            out.extend(body)
        out.append("")
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def injected(sections: Sections, private: Sequence[str] = PRIVATE_SECTIONS) -> List[str]:
    """The lines an agent sees: no private sections, no empties, every line escaped.

    Escaping is ``render.escape_context_line``, the same one the board context
    uses, because this text is spliced into an agent's context unfenced: a
    line here must not be able to open a code fence or forge a role prefix.
    """
    from . import render as _render

    hidden = {p.lower() for p in private}
    out: List[str] = []
    for title, body in sections:
        if title.lower() in hidden:
            continue
        body = _trimmed(body)
        if not body:
            continue
        out.append(_render.escape_context_line("{}:".format(title)))
        out.extend(_render.escape_context_line(line) for line in body)
    return out


def summary(sections: Sections, limit: int = 200) -> str:
    """One line naming what the document says, for a board record."""
    for title, body in sections:
        if title.lower() in {p.lower() for p in PRIVATE_SECTIONS}:
            continue
        for line in body:
            text = " ".join(line.split())
            if text:
                head = "{}: {}".format(title.lower(), text)
                return head if len(head) <= limit else head[: max(1, limit - 1)].rstrip() + "…"
    return "no content yet"

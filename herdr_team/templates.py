"""Team templates: a research sprint, a content campaign or a security hunt in one command (0.19).

A template is a folder of Markdown, so it is read and written with the same
line scanner as the member documents (Python 3.9 has no TOML or YAML reader):

    <name>/team.md          # title, description, ## Charter, ## Rules, ## Settings,
                            # ## Roles, ## Vocabulary
    <name>/roles/<role>.md  # that role's instructions document: Mission, Scope,
                            # Constraints, Definition of done, Handoffs

``create <team> --template <name>`` fills in what the operator did not pass:
the charter, the rules, one ``--spawn role:kind`` per role (with ``--new``),
each role's Mission and instructions, the manager role and the launch
permissions. Then the team's settings are applied: the contradiction mode, the
work acceptance policy and default reviewer, and the vocabulary its facts may
use. Anything given explicitly on the command line wins.

Built-in templates live in ``<plugin>/templates``; the operator's own, written by
``template save``, in ``<config>/plugins/config/herdr-synapse/templates`` and
take precedence over a built-in of the same name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import facts as _facts
from herdr_team import instructions_doc as _doc
from herdr_team import paths as _paths
from herdr_team import store
from herdr_team import work as _work
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError

TEMPLATE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}\Z")
KNOWN_SETTINGS = ("manager", "contradictions", "debate_timeout", "acceptance", "review_by", "permissions")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+([^:]+?)\s*:\s*(.*)$")


@dataclass
class Role:
    name: str
    kind: str
    about: str = ""
    document: str = ""  # the instructions document text

    @property
    def mission(self) -> str:
        return _doc.mission_paragraph(_doc.parse(self.document))


@dataclass
class Template:
    name: str
    title: str
    description: str
    charter: str = ""
    rules: str = ""
    settings: Dict[str, str] = field(default_factory=dict)
    roles: List[Role] = field(default_factory=list)
    vocabulary: Dict[str, str] = field(default_factory=dict)
    source: str = "builtin"
    path: Optional[Path] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name, "title": self.title, "description": self.description, "source": self.source,
            "charter": self.charter, "rules": self.rules, "settings": dict(self.settings),
            "roles": [{"role": r.name, "kind": r.kind, "about": r.about, "mission": r.mission} for r in self.roles],
            "vocabulary": dict(self.vocabulary), "path": os.fspath(self.path) if self.path else None,
        }


def builtin_dir() -> Path:
    return _paths.plugin_root() / "templates"


def user_dir(config_dir: Path) -> Path:
    return _paths.plugin_config_dir(config_dir) / "templates"


def validate_name(name: str) -> str:
    if not TEMPLATE_NAME_RE.match(name or ""):
        raise UsageError("a template name is lowercase letters, digits and dashes, starting with a letter")
    return name


def _sections(text: str) -> Tuple[str, str, Dict[str, str]]:
    """Title (first ``#``), the paragraph under it, and each ``##`` section's body."""
    title = ""
    description: List[str] = []
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            level, heading = len(match.group(1)), match.group(2).strip()
            if level == 1 and not title:
                title = heading
                current = None
                continue
            current = heading.lower()
            sections.setdefault(current, [])
            continue
        if current is None:
            description.append(line)
        else:
            sections[current].append(line)
    return title, "\n".join(description).strip(), {k: "\n".join(v).strip() for k, v in sections.items()}


def unwrap(text: str) -> str:
    """Join the wrapped lines of each paragraph; bullets and blank lines keep their breaks."""
    out: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        is_item = bool(re.match(r"^([-*]|\d+[.)])\s+", stripped))
        if out and stripped and out[-1].strip() and not is_item and not stripped.startswith("#"):
            out[-1] = out[-1].rstrip() + " " + stripped
        else:
            out.append(line.rstrip())
    return "\n".join(out).strip()


def _bullets(body: str) -> List[Tuple[str, str]]:
    out = []
    for line in body.splitlines():
        match = _BULLET_RE.match(line)
        if match:
            out.append((match.group(1).strip(), match.group(2).strip()))
    return out


def parse(name: str, directory: Path, source: str) -> Template:
    team_md = directory / "team.md"
    try:
        text = team_md.read_text(encoding="utf-8")
    except OSError as err:
        raise HerdrTeamError("template_unreadable", "cannot read {}: {}".format(team_md, err), EXIT_REFUSED, {"template": name})
    title, description, sections = _sections(text)
    template = Template(name=name, title=title or name, description=unwrap(description), charter=unwrap(sections.get("charter", "")),
                        rules=unwrap(sections.get("rules", "")), source=source, path=directory)
    for key, value in _bullets(sections.get("settings", "")):
        key = key.lower().replace(" ", "_")
        if key in KNOWN_SETTINGS:
            template.settings[key] = value
    for role_name, rest in _bullets(sections.get("roles", "")):
        kind, _sep, about = rest.partition("—") if "—" in rest else rest.partition(" - ")
        role = Role(name=role_name.strip().lower(), kind=kind.strip().lower(), about=about.strip())
        role_md = directory / "roles" / (role.name + ".md")
        try:
            role.document = role_md.read_text(encoding="utf-8")
        except OSError:
            role.document = ""
        template.roles.append(role)
    for label, meaning in _bullets(sections.get("vocabulary", "")):
        template.vocabulary[label] = meaning
    _check(template)
    return template


def _check(template: Template) -> None:
    """A template that would make ``create`` fail later fails here, with its own name in the error."""
    problems = []
    names = [r.name for r in template.roles]
    for role in template.roles:
        if not _paths.ROLE_NAME_RE.match(role.name):
            problems.append("role {!r} is not a valid role name".format(role.name))
        if not role.kind:
            problems.append("role {} names no agent kind".format(role.name))
    manager = template.settings.get("manager")
    if manager and manager not in names:
        problems.append("manager {!r} is not one of its roles".format(manager))
    mode = template.settings.get("contradictions")
    if mode and mode not in _facts.MODES:
        problems.append("contradictions must be one of {}".format(", ".join(_facts.MODES)))
    if template.settings.get("debate_timeout"):
        from herdr_team.cmd_misc import parse_duration_s

        try:
            parse_duration_s(template.settings["debate_timeout"])
        except HerdrTeamError:
            problems.append("debate_timeout {!r} is not a duration like 30m or 2h".format(template.settings["debate_timeout"]))
    acceptance = template.settings.get("acceptance")
    if acceptance and acceptance not in _work.ACCEPTANCE_POLICIES:
        problems.append("acceptance must be one of {}".format(", ".join(_work.ACCEPTANCE_POLICIES)))
    if problems:
        raise HerdrTeamError("template_invalid", "template {}: {}".format(template.name, "; ".join(problems)), EXIT_REFUSED, {"template": template.name, "problems": problems})


def available(config_dir: Path) -> Dict[str, Tuple[Path, str]]:
    """Every template by name. A built-in always wins over a folder of the same name in the
    user directory: that directory is writable by anything running as the operator, and a
    template installs operator-authority documents, so it must not silently replace one the
    operator knows. ``template save`` refuses built-in names for the same reason."""
    found: Dict[str, Tuple[Path, str]] = {}
    for root, source in ((user_dir(config_dir), "user"), (builtin_dir(), "builtin")):
        if not root.is_dir() or root.is_symlink():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and (entry / "team.md").is_file() and TEMPLATE_NAME_RE.match(entry.name):
                found[entry.name] = (entry, source)
    return found


def builtin_names() -> List[str]:
    root = builtin_dir()
    return sorted(e.name for e in root.iterdir() if e.is_dir() and (e / "team.md").is_file()) if root.is_dir() else []


def load(name: str, config_dir: Path) -> Template:
    validate_name(name)
    templates = available(config_dir)
    if name not in templates:
        raise HerdrTeamError("template_not_found", "no template {!r}; there are: {}".format(name, ", ".join(sorted(templates)) or "none"), EXIT_REFUSED, {"templates": sorted(templates)})
    directory, source = templates[name]
    return parse(name, directory, source)


# --------------------------------------------------------------------------
# create --template


def fill_create_args(args: Any, template: Template) -> List[str]:
    """Fill what the operator did not pass; returns notes for the output."""
    notes: List[str] = []
    if getattr(args, "charter", None) is None and getattr(args, "charter_file", None) is None and template.charter:
        args.charter = template.charter
        notes.append("charter from template {}: replace it with herdr-synapse charter set \"<your goal>\" when it is not specific enough".format(template.name))
    if getattr(args, "rules", None) is None and getattr(args, "rules_file", None) is None and template.rules:
        args.rules = template.rules
    if getattr(args, "new", False) and not getattr(args, "spawn", None):
        args.spawn = ["{}:{}".format(r.name, r.kind) for r in template.roles]
    given_briefs = {v.partition("=")[0].strip() for v in getattr(args, "brief", []) or []}
    given_docs = {v.partition("=")[0].strip() for v in getattr(args, "instructions", []) or []}
    for role in template.roles:
        if role.document and role.name not in given_docs:
            args.instructions = list(getattr(args, "instructions", []) or []) + ["{}={}".format(role.name, role.document)]
        elif role.mission and role.name not in given_briefs:
            args.brief = list(getattr(args, "brief", []) or []) + ["{}={}".format(role.name, role.mission)]
    if getattr(args, "permissions", None) is None and template.settings.get("permissions") in ("yolo", "native"):
        args.permissions = template.settings["permissions"]
    return notes


def manager_for(template: Template, members: List[Dict[str, Any]]) -> Optional[str]:
    role = template.settings.get("manager")
    if not role:
        return None
    for member in members:
        if member.get("role") == role:
            return str(member.get("name"))
    return None


def apply_settings(team: Any, template: Template) -> Dict[str, Any]:
    """Write the template's team settings into ``team.json`` ``config``."""
    applied: Dict[str, Any] = {}
    config_updates: Dict[str, Any] = {}
    mode = template.settings.get("contradictions")
    if mode:
        timeout_ms = _facts.DEFAULT_DEBATE_TIMEOUT_MS
        if template.settings.get("debate_timeout"):
            from herdr_team.cmd_misc import parse_duration_s

            timeout_ms = parse_duration_s(template.settings["debate_timeout"]) * 1000
        config_updates["contradictions"] = {"mode": mode, "debate_timeout_ms": timeout_ms}
    work_cfg: Dict[str, Any] = {}
    if template.settings.get("acceptance"):
        work_cfg["acceptance"] = template.settings["acceptance"]
    if template.settings.get("review_by"):
        work_cfg["review_by"] = [part.strip() for part in template.settings["review_by"].split(",") if part.strip()]
    if work_cfg:
        config_updates["work"] = work_cfg
    if template.vocabulary:
        config_updates["vocabulary"] = dict(template.vocabulary)
    config_updates["template"] = template.name
    if not config_updates:
        return applied

    def mutate(doc: Dict[str, Any]) -> None:
        config = doc.setdefault("config", {})
        for key, value in config_updates.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                merged = dict(config[key])
                merged.update(value)
                config[key] = merged
            else:
                config[key] = value

    store.RosterStore(team).update(mutate)
    applied.update(config_updates)
    return applied


# --------------------------------------------------------------------------
# template save


def render(template: Template) -> Dict[str, str]:
    """``{relative path: text}`` for a template folder."""
    lines = ["# {}".format(template.title), ""]
    if template.description:
        lines += [template.description, ""]
    if template.charter:
        lines += ["## Charter", "", template.charter, ""]
    if template.rules:
        lines += ["## Rules", "", template.rules, ""]
    if template.settings:
        lines += ["## Settings", ""] + ["- {}: {}".format(k, v) for k, v in template.settings.items()] + [""]
    lines += ["## Roles", ""] + ["- {}: {}{}".format(r.name, r.kind, " — " + r.about if r.about else "") for r in template.roles] + [""]
    if template.vocabulary:
        lines += ["## Vocabulary", ""] + ["- {}: {}".format(k, v) for k, v in template.vocabulary.items()] + [""]
    files = {"team.md": "\n".join(lines).rstrip() + "\n"}
    for role in template.roles:
        if role.document:
            files["roles/{}.md".format(role.name)] = role.document.rstrip() + "\n"
    return files


def write(directory: Path, template: Template, overwrite: bool) -> List[str]:
    if directory.exists() and not overwrite:
        raise HerdrTeamError("template_exists", "template {} already exists at {}; pass --force to replace it".format(template.name, directory), EXIT_REFUSED, {"path": os.fspath(directory)})
    written = []
    for rel, text in render(template).items():
        path = directory / rel
        _paths.ensure_dir(path.parent)
        store.atomic_write(path, text.encode("utf-8"))
        written.append(os.fspath(path))
    return written

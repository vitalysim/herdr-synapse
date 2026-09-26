"""``template list | show | save``: team templates for ``create --template`` (0.19).

The format and how a template fills ``create`` are in ``herdr_team.templates``.
Saving writes a template from a running team, so a team shape that worked
(its charter, rules, roles, each role's instructions and its settings) can be
started again in one command, or shared as a folder.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, List

from herdr_team import charter as _charter
from herdr_team import facts as _facts
from herdr_team import templates as T
from herdr_team.cli import Command, add_global_arguments, emit, layout_for
from herdr_team.cmd_board import _open_team, agent_members, charter_of
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.identity import audit


def _list(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    rows: List[Dict[str, Any]] = []
    for name, (directory, source) in sorted(T.available(layout.config_dir).items()):
        template = T.parse(name, directory, source)
        rows.append({"name": name, "title": template.title, "source": source, "roles": [r.name for r in template.roles],
                     "description": template.description})

    def human() -> str:
        if not rows:
            return "no templates"
        return "\n".join("{:<18} {:<8} {} — roles: {}".format(r["name"], r["source"], r["title"], ", ".join(r["roles"])) for r in rows) + \
            "\nstart one: herdr-synapse create <team> --template <name> --new --project <dir>"

    return emit(args, {"templates": rows}, human)


def _show(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    template = T.load(args.name, layout.config_dir)
    payload = template.to_json()

    def human() -> str:
        lines = ["{} ({}, {})".format(template.title, template.name, template.source)]
        if template.description:
            lines.append(template.description)
        lines.append("")
        for role in template.roles:
            lines.append("role {} ({}{}): {}".format(role.name, role.kind, "/" + role.profile if role.profile else "", role.mission or role.about or "-"))
        if template.settings:
            lines.append("settings: " + ", ".join("{}={}".format(k, v) for k, v in template.settings.items()))
        if template.vocabulary:
            lines.append("vocabulary: " + ", ".join(template.vocabulary))
        if template.charter:
            lines.append("charter: " + " ".join(template.charter.split())[:200])
        return "\n".join(lines)

    return emit(args, payload, human)


def _save(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    from herdr_team.cmd_roster import _human_only

    _human_only(layout, team_name, author, "template save")
    name = T.validate_name(args.name)
    if name in T.builtin_names():
        raise HerdrTeamError("template_builtin", "{} is a built-in template; save yours under another name".format(name), EXIT_REFUSED, {"template": name})
    charter = charter_of(doc)
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    template = T.Template(name=name, title=args.title or "{} (saved from {})".format(name, team_name),
                          description=args.description or ("Saved from team {}.".format(team_name)),
                          charter=str((charter or {}).get("text") or ""), rules=_charter.get_rules(layout, team_name) or "", source="user")
    seen_roles: Dict[str, bool] = {}
    for member in agent_members(doc):
        role = str(member.get("role") or "").strip()
        if not role or role in seen_roles:
            continue
        seen_roles[role] = True
        document = _charter.get_instructions(layout, team_name, str(member.get("name"))) or ""
        template.roles.append(T.Role(name=role, kind=str(member.get("kind") or "claude"), document=document,
                                     profile=str(member["profile"]) if isinstance(member.get("profile"), str) and member.get("profile") else None))
        if member.get("manager"):
            template.settings["manager"] = role
    contradictions = _facts.contradictions_config(doc)
    template.settings["contradictions"] = contradictions["mode"]
    if contradictions["debate_timeout_ms"] != _facts.DEFAULT_DEBATE_TIMEOUT_MS:
        minutes, seconds = divmod(int(contradictions["debate_timeout_ms"] // 1000), 60)
        template.settings["debate_timeout"] = "{}m".format(minutes) if not seconds else "{}s".format(minutes * 60 + seconds)
    work_cfg = config.get("work") if isinstance(config.get("work"), dict) else {}
    if work_cfg.get("acceptance"):
        template.settings["acceptance"] = str(work_cfg["acceptance"])
    if work_cfg.get("review_by"):
        template.settings["review_by"] = ",".join(str(r) for r in work_cfg["review_by"])
    template.vocabulary = _facts.vocabulary(doc)
    if not template.roles:
        raise UsageError("team {} has no member with a role to save".format(team_name))
    T._check(template)
    written = T.write(T.user_dir(layout.config_dir) / name, template, overwrite=bool(args.force))
    audit(layout, team_name, "template_saved", author, {"template": name, "roles": [r.name for r in template.roles]})
    return emit(args, {"template": template.to_json(), "written": written},
                "template {} saved ({} roles); start a team from it: herdr-synapse create <team> --template {} --new".format(name, len(template.roles), name))


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="template_action", metavar="<action>")
    specs = [("list", "every template, built in and your own"), ("show", "one template: roles, missions, settings"),
             ("save", "save the current team as a template of your own (operator)")]
    parsers = {}
    for name, help_text in specs:
        p = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        add_global_arguments(p, nested=True)
        parsers[name] = p
    parsers["show"].add_argument("name")
    parsers["save"].add_argument("name")
    parsers["save"].add_argument("--title")
    parsers["save"].add_argument("--description")
    parsers["save"].add_argument("--force", action="store_true", help="replace a template of yours with this name")


def _run(args: argparse.Namespace) -> int:
    action = getattr(args, "template_action", None) or "list"
    return {"list": _list, "show": _show, "save": _save}[action](args)


COMMANDS: List[Command] = [
    Command("template", "team templates: list, show, save (start one with create --template NAME)", _add_arguments, _run),
]

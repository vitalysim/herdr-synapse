"""``available``: what you can build a team from, right now (0.20).

Two lists. The agents already running in this session that are in no team
(``create --member <pane>:<role>`` takes them as they are), and the harnesses
installed on this machine that ``create --new --spawn`` can start, each with
its profiles and its models as the harness itself reports them
(``herdr_team.harnesses``). Name a harness for all of its models and profiles.

Read-only, and open to anyone: nothing here is team state.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

from herdr_team import harnesses as H
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.errors import HerdrTeamError, UsageError

#: How many names one summary line shows before it says how many more.
SUMMARY_NAMES = 6
#: A named harness shows at most this many models unless --all.
DETAIL_MODELS = 60


def running_unassigned(args: argparse.Namespace) -> Optional[List[Dict[str, Any]]]:
    """Agents in this session that belong to no team; None when the server cannot be asked."""
    from herdr_team import picker as _picker

    try:
        layout = layout_for(args)
        agents = _picker.fetch_agents(api_for(args, layout))
    except (HerdrTeamError, OSError):
        return None
    members = {(m.get("terminal_id"), m.get("pane_id")) for rows in _picker.session_rosters(layout).values() for m in rows
               if m.get("status") != "left"}
    taken_terminals = {t for t, _p in members if t}
    taken_panes = {p for _t, p in members if p}
    out = []
    for agent in agents:
        if agent.get("terminal_id") in taken_terminals or agent.get("pane_id") in taken_panes:
            continue
        out.append({"pane_id": agent.get("pane_id"), "kind": agent.get("agent"), "name": agent.get("name"),
                    "state": agent.get("agent_status") or agent.get("state"), "cwd": agent.get("cwd")})
    return out


def _trusted(args: argparse.Namespace) -> Optional[set]:
    from herdr_team import picker as _picker

    try:
        return _picker.trusted_kinds(layout_for(args))
    except (HerdrTeamError, OSError):
        return None


def _filter_models(rows: List[H.ModelInfo], provider: Optional[str], search: Optional[str], include_hidden: bool) -> List[H.ModelInfo]:
    out = []
    for m in rows:
        if m.hidden and not include_hidden:
            continue
        if provider and (m.provider or "").lower() != provider.lower():
            continue
        if search and search.lower() not in m.id.lower() and search.lower() not in (m.display or "").lower():
            continue
        out.append(m)
    return out


def _names(values: List[str], limit: int = SUMMARY_NAMES) -> str:
    if len(values) <= limit:
        return ", ".join(values)
    return "{}, … ({} in all)".format(", ".join(values[:limit]), len(values))


def _efforts(h: H.Harness) -> List[str]:
    """What the harness's models accept, in the usual order; the fixed list when the harness does not say per model."""
    if h.models is not None and h.models.authoritative:
        seen = {e for m in h.models.models if not m.hidden for e in (m.efforts or [])}
        if seen:
            order = list(h.efforts or [])
            return [e for e in order if e in seen] + sorted(seen - set(order))
    return list(h.efforts or [])


def _summary_lines(h: H.Harness, trusted: Optional[set]) -> List[str]:
    head = "  {:<9} {:<18} ".format(h.kind, h.version or "?")
    notes = []
    if h.yolo:
        notes.append("yolo " + " ".join(h.yolo))
    if trusted is not None and h.kind not in trusted:
        notes.append("not trusted for delivery yet: herdr-synapse kinds trust {}".format(h.kind))
    lines = [head + " · ".join(notes)]
    if h.profile_flag is None:
        lines.append("      profiles  none (Synapse cannot select one for {})".format(h.kind))
    elif h.profiles is None:
        lines.append("      profiles  {}".format(h.profiles_note or "unknown"))
    else:
        runnable = [p.name for p in h.profiles if p.selectable]
        sub = [p.name for p in h.profiles if not p.selectable]
        text = _names(runnable) if runnable else "none defined"
        if h.kind == "codex" and not runnable:
            text += " (-p NAME reads $CODEX_HOME/NAME.config.toml)"
        if sub:
            text += "  (subagents, not selectable: {})".format(", ".join(sub))
        lines.append("      profiles  " + text)
    if h.models is None:
        lines.append("      models    " + (h.models_note or "unknown"))
    else:
        visible = [m for m in h.models.models if not m.hidden]
        providers = sorted({m.provider for m in visible if m.provider})
        if len(visible) > 30 and providers:
            lines.append("      models    {} across {} → herdr-synapse available {} --provider {}".format(
                len(visible), ", ".join(providers), h.kind, providers[0]))
        else:
            lines.append("      models    " + _names([m.id for m in visible], 8) + ("" if h.models.authoritative else "  (or any name {} takes)".format(h.kind)))
    efforts = _efforts(h)
    if efforts:
        per_model = h.models is not None and h.models.authoritative and any(m.efforts for m in h.models.models)
        lines.append("      efforts   {}{}".format(" ".join(efforts), "  (each model takes its own subset: herdr-synapse available {})".format(h.kind) if per_model else ""))
    return lines


def _detail_lines(h: H.Harness, rows: List[H.ModelInfo], shown: List[H.ModelInfo], trusted: Optional[set]) -> List[str]:
    lines = _summary_lines(h, trusted)[:1]
    lines.append("")
    if h.profile_flag is not None:
        lines.append("profiles ({} NAME; --spawn <role>:{}/<profile>)".format(h.profile_flag, h.kind))
        if h.profiles is None:
            lines.append("  " + (h.profiles_note or "unknown"))
        elif not h.profiles:
            lines.append("  none defined")
        for p in h.profiles or []:
            desc = " ".join(p.description.split())
            lines.append("  {:<22} {}{}".format(p.name, "[subagent, not selectable] " if not p.selectable else "[{}] ".format(p.source) if p.source else "",
                                                desc[:100] + ("…" if len(desc) > 100 else "")))
        lines.append("")
    if h.models is None:
        lines.append("models: " + (h.models_note or "unknown"))
        return lines
    lines.append("models ({}; --model <role>={}@<effort>){}".format(
        h.models.source, "<model>", "" if h.models.authoritative else " -- any name {} takes works too".format(h.kind)))
    for m in shown:
        bits = []
        if m.efforts:
            bits.append("efforts " + " ".join(m.efforts) + (" (default {})".format(m.default_effort) if m.default_effort else ""))
        if m.context:
            bits.append("context " + m.context)
        if m.thinking is False:
            bits.append("no thinking")
        if m.hidden:
            bits.append("hidden in {}'s own picker".format(h.kind))
        lines.append("  {:<40} {}".format(m.id, " · ".join(bits)))
    if len(shown) < len(rows):
        lines.append("  … {} more; narrow with --provider or --search, or --all".format(len(rows) - len(shown)))
    if not rows:
        lines.append("  nothing matches")
    return lines


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("harness", nargs="?", help="one harness in full: all its profiles and models (claude, codex, opencode, pi, ...)")
    parser.add_argument("--cwd", metavar="DIR", help="the directory the members will work in; project profiles and models depend on it (default: here)")
    parser.add_argument("--provider", metavar="NAME", help="only models from this provider (OpenCode, Pi)")
    parser.add_argument("--search", metavar="TEXT", help="only models whose name contains TEXT")
    parser.add_argument("--all", action="store_true", help="also harnesses that are not installed, hidden models, and every model of a named harness")


def _run(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    env.update(getattr(args, "env", None) or {})
    cwd = os.path.abspath(os.path.expanduser(args.cwd)) if args.cwd else os.getcwd()
    if args.cwd and not os.path.isdir(cwd):
        raise UsageError("--cwd {} is not a directory".format(args.cwd))
    kinds = H.all_kinds()
    if args.harness:
        if args.harness not in kinds:
            raise UsageError("{!r} is not a harness Herdr can start; one of: {}".format(args.harness, ", ".join(kinds)))
        kinds = [args.harness]
    catalog = H.catalog(kinds, cwd, env, installed_only=not (args.all or args.harness))
    trusted = _trusted(args)
    running = running_unassigned(args) if not args.harness else None
    payload: Dict[str, Any] = {"cwd": cwd, "harnesses": [h.to_json() for h in catalog]}
    if not args.harness:
        payload["running"] = running
    detail_rows: List[H.ModelInfo] = []
    shown: List[H.ModelInfo] = []
    if args.harness and catalog and catalog[0].models is not None:
        detail_rows = _filter_models(catalog[0].models.models, args.provider, args.search, args.all)
        shown = detail_rows if args.all else detail_rows[:DETAIL_MODELS]
        payload["harnesses"][0]["models"] = [m.to_json() for m in detail_rows]
    if trusted is not None:
        payload["trusted"] = sorted(trusted)

    def human() -> str:
        if args.harness:
            h = catalog[0]
            if not h.installed:
                return "{} is not installed here ({} is not on PATH)".format(h.kind, h.executable)
            return "\n".join(_detail_lines(h, detail_rows, shown, trusted))
        lines: List[str] = []
        lines.append("Running agents in no team (add with: create <team> --member <pane>:<role>)")
        if running is None:
            lines.append("  unknown: Herdr is not reachable from here")
        elif not running:
            lines.append("  none")
        for a in running or []:
            lines.append("  {:<8} {:<9} {:<8} {}".format(str(a["pane_id"]), str(a["kind"]), str(a["state"] or "?"), a.get("cwd") or ""))
        lines.append("")
        lines.append("Harnesses you can start: --spawn <role>:<harness>[/<profile>]  --model <role>=<model>[@<effort>]")
        installed = [h for h in catalog if h.installed]
        if not installed:
            lines.append("  none found on PATH")
        for h in catalog:
            if not h.installed:
                lines.append("  {:<9} not installed ({} is not on PATH)".format(h.kind, h.executable))
                continue
            lines.extend(_summary_lines(h, trusted))
        lines.append("")
        lines.append("One harness in full: herdr-synapse available <harness> [--search TEXT] [--provider NAME]")
        return "\n".join(lines)

    return emit(args, payload, human)


COMMANDS: List[Command] = [
    Command("available", "what you can build a team from: running agents in no team, and the harnesses you can start with their profiles and models",
            _add_arguments, _run),
]


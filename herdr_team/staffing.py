"""Opt-in cooperative staffing authority, separate from operator delegation.

The roster owns policy and durable start reservations. All launchers serialize
with restore.lock, reserve before side effects, and retain ambiguous attempts.
These controls do not sandbox an agent running shell as the same OS user.
"""
from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from herdr_team import model_inventory as I, model_checks, models, paths, roster, store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError

CAPABILITY = "autonomous_staffing_v1"
DEFAULT_MAX_WORKERS = 4
CAPACITY_PHASES = ("reserved", "laying_out", "starting", "active", "ambiguous", "retiring")


def policy(config: Any) -> Optional[Dict[str, Any]]:
    value = config.get("staffing") if isinstance(config, dict) else None
    return value if isinstance(value, dict) and value.get("version") == 1 and value.get("enabled") is True else None


def operator(author: Any) -> None:
    if not author.trusted_human:
        raise HerdrTeamError("author_mismatch", "staffing policy is the operator's alone; delegation does not pass this authority", EXIT_REFUSED)


def require_manager(team: roster.Team, author: Any) -> None:
    if author.trusted_human:
        return
    manager = team.manager()
    if (manager is None or not author.verified or author.team != team.team or author.name != manager.name
            or author.terminal_id != manager.terminal_id or author.generation != manager.generation
            or manager.status not in ("active", "starting")):
        raise HerdrTeamError("staffing_authority", "staffing needs the current verified manager or the operator", EXIT_REFUSED)


def routes(values: Sequence[str]) -> List[str]:
    result = []
    for value in values:
        kind, sep, provider = value.partition(":")
        if not sep or kind not in I.KINDS or not I.token(provider):
            raise UsageError("--allow-provider needs claude|codex|opencode:PROVIDER")
        if value not in result:
            result.append(value)
    if not result:
        raise UsageError("autonomous staffing needs at least one --allow-provider")
    return result


def max_workers(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 64:
        raise UsageError("--max-workers must be between 1 and 64")
    return value


def approve(values: Sequence[str], cwd: str, env: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    approved = {}
    inventories: Dict[str, Dict[str, Any]] = {}
    for route in routes(values):
        kind, provider = route.split(":", 1)
        if kind not in inventories:
            inventories[kind] = I.discover(kind, cwd, env, refresh=True)
        inventory = inventories[kind]
        reported = next((p for p in inventory.get("providers", []) if p.get("id") == provider), None)
        if (not inventory.get("installed") or not reported or
                (kind == "opencode" and not reported.get("configured") and reported.get("authentication") != "credentials_reported")):
            raise HerdrTeamError("provider_unknown", "{} is not reported here; inspect available {} --readiness --cwd DIR".format(route, kind), EXIT_REFUSED)
        approved[route] = {"context": inventory["context"], "account_context": inventory.get("account_context")}
    return approved


def selection(team: roster.Team, kind: str, model: Optional[str], effort: Optional[str], profile: Optional[str],
              cwd: str, env: Dict[str, str]) -> Dict[str, Any]:
    config = policy(team.config)
    if config is None:
        raise HerdrTeamError("staffing_disabled", "this team is not enrolled in autonomous staffing", EXIT_REFUSED)
    if kind not in I.KINDS or not I.token(model) or (profile and not I.token(profile)):
        raise UsageError("staffing v1 needs an explicit Claude Code, Codex or OpenCode model")
    inventory = I.discover(kind, cwd, env, profile, refresh=True)
    if inventory.get("quota") == "exhausted":
        raise HerdrTeamError("model_ineligible", "native metadata reports exhausted quota; await recovery or the operator's decision", EXIT_REFUSED)
    entry = next((m for m in inventory.get("models", []) if m.get("id") == model), {})
    provider = model.split("/", 1)[0] if kind == "opencode" and "/" in model else entry.get("provider")
    if not provider and kind != "opencode" and len(inventory.get("providers", [])) == 1:
        provider = inventory["providers"][0]["id"]
    route = kind + ":" + str(provider or "")
    approved = config.get("approved", {}).get(route)
    if not approved:
        raise HerdrTeamError("provider_not_approved", "{} is outside the operator-approved pool".format(route), EXIT_REFUSED)
    if approved.get("context") != inventory.get("context") or approved.get("account_context") != inventory.get("account_context"):
        raise HerdrTeamError("provider_context_changed", "provider/account/configuration changed; the operator must reapprove {}".format(route), EXIT_REFUSED)
    if not any(p.get("id") == provider for p in inventory.get("providers", [])):
        raise HerdrTeamError("provider_unknown", "native metadata cannot confirm the selected provider route", EXIT_REFUSED)
    if entry.get("hidden"):
        raise HerdrTeamError("model_hidden", "hidden native models are not autonomous candidates", EXIT_REFUSED)
    if kind == "opencode" and entry.get("configured") is False:
        raise HerdrTeamError("model_unconfigured", "this model exists only in the native catalog, not a configured provider", EXIT_REFUSED)
    efforts = entry.get("efforts")
    if effort and isinstance(efforts, list) and effort not in efforts:
        raise HerdrTeamError("effort_unsupported", "selected model does not report that effort", EXIT_REFUSED)
    if effort and not efforts:
        raise HerdrTeamError("effort_unknown", "use the native effort default when model capabilities are unknown", EXIT_REFUSED)
    models.validate(kind, model, effort)
    session = paths.session_paths(Path(team.state_dir), paths.session_slug(team.socket))
    checks = store.read_json(session.root / "model-checks.json", default={})
    evidence = checks.get(model_checks.evidence_key(kind, profile, model, effort)) if isinstance(checks, dict) else None
    if isinstance(evidence, dict) and evidence.get("route_context") == inventory.get("route_context"):
        age = time.time() - float(evidence.get("checked_at") or 0)
        if 0 <= age < I.VERIFICATION_TTL and evidence.get("status") in ("model_denied", "quota_exhausted", "authentication_failed"):
            raise HerdrTeamError("model_ineligible", "recent access evidence excludes this route/model: " + evidence["status"], EXIT_REFUSED)
    # Configured custom IDs are allowed inside a known route, but never called verified.
    return {"kind": kind, "model": model, "effort": effort, "profile": profile, "provider": provider,
            "route": route, "context": inventory["context"], "route_context": inventory["route_context"],
            "native_version": inventory.get("version"),
            "inference": "unverified"}


def attempts(team: roster.Team) -> Dict[str, Any]:
    config = policy(team.config)
    return (config or {}).get("attempts") or {}


def capacity(team: roster.Team) -> int:
    return sum(1 for value in attempts(team).values() if value.get("phase") in CAPACITY_PHASES)


def reserve(paths: Any, author: Any, request_id: str, name: str, role: str, chosen: Dict[str, Any],
            mission: str, acceptance: str, work_id: Optional[str]) -> Dict[str, Any]:
    if not I.token(request_id) or len(request_id) > 80:
        raise UsageError("request id must be a short native-safe identifier")
    # Refreshing a native catalog is not a different operator/manager request.
    # Keep idempotency tied to the requested setting, not changing access evidence.
    setting = {key: chosen.get(key) for key in ("kind", "model", "effort", "profile", "route")}
    signature = I.opaque([name, role, setting, mission, acceptance, work_id])
    result: List[Dict[str, Any]] = []

    def mutate(team: roster.Team) -> None:
        require_manager(team, author)
        config = policy(team.config)
        if config is None:
            raise HerdrTeamError("staffing_disabled", "team is not enrolled", EXIT_REFUSED)
        previous = attempts(team).get(request_id)
        if previous:
            if previous.get("signature") != signature:
                raise HerdrTeamError("staffing_request_conflict", "request id was already used for a different launch", EXIT_REFUSED)
            result.append(dict(previous, reused=True))
            return
        if config.get("paused"):
            raise HerdrTeamError("staffing_paused", "the operator paused new staffing", EXIT_REFUSED)
        approved = config.get("approved", {}).get(chosen.get("route"))
        if not approved or approved.get("context") != chosen.get("context"):
            raise HerdrTeamError("provider_context_changed", "provider pool changed during launch planning", EXIT_REFUSED)
        if capacity(team) >= max_workers(config.get("max_workers")):
            raise HerdrTeamError("staffing_capacity", "autonomous worker capacity is full (ambiguous starts also count)", EXIT_REFUSED)
        if team.find(name):
            raise HerdrTeamError("name_taken", "worker name is already in the roster; existing agents are not adopted", EXIT_REFUSED)
        manager = team.manager()
        record = {"request_id": request_id, "signature": signature, "name": name, "role": role,
                  "selection": copy.deepcopy(chosen), "mission": mission, "acceptance": acceptance, "work_id": work_id,
                  "manager": {"name": manager.name, "terminal_id": manager.terminal_id, "generation": manager.generation} if manager else None,
                  "requested_by": author.name, "phase": "reserved", "created_at": roster.now_iso(), "history": []}
        config.setdefault("attempts", {})[request_id] = record
        result.append(record)
    roster.update_team(paths, mutate)
    return copy.deepcopy(result[0])


def phase(paths: Any, request_id: str, value: str, **fields: Any) -> Dict[str, Any]:
    result = []
    def mutate(team: roster.Team) -> None:
        record = attempts(team).get(request_id)
        if not isinstance(record, dict):
            raise HerdrTeamError("staffing_request_missing", "staffing reservation disappeared", EXIT_REFUSED)
        record["history"].append({"phase": value, "at": roster.now_iso()})
        record.update(phase=value, **fields)
        result.append(copy.deepcopy(record))
    roster.update_team(paths, mutate)
    return result[0]


def launch_guard(team: roster.Team, member: Any, chosen: Optional[Dict[str, Any]] = None, starting: bool = False) -> None:
    """Cheap stored-policy guard. Native discovery never belongs in a notifier loop."""
    config = policy(team.config)
    owned = member.get("staffing") if isinstance(member, dict) else getattr(member, "staffing", None)
    if not config or not owned:
        return
    record = attempts(team).get(owned.get("request_id"))
    if not record or record.get("phase") in ("retiring", "retired", "cancelled"):
        raise HerdrTeamError("staffing_retired", "owned worker is retiring/retired or its reservation is missing", EXIT_REFUSED)
    if starting and config.get("paused"):
        raise HerdrTeamError("staffing_paused", "new worker launches are paused", EXIT_REFUSED)
    destination = chosen or record.get("selection") or {}
    if destination.get("route") not in config.get("approved", {}):
        raise HerdrTeamError("provider_not_approved", "worker provider was removed from the pool", EXIT_REFUSED)
    approved = config["approved"][destination["route"]]
    if destination.get("context") != approved.get("context"):
        raise HerdrTeamError("provider_context_changed", "worker route needs renewed operator approval", EXIT_REFUSED)


def direct_create_guard(layout: Any, author: Any) -> None:
    """Only newly enrolled teams get recursion protection; human creation is unchanged."""
    if author.trusted_human or not author.is_member or not author.team:
        return
    team = roster.load_team(layout.team(author.team))
    if policy(team.config):
        raise HerdrTeamError("staffing_scope", "enrolled agents use staff add for their own team; manual creation remains available to the operator", EXIT_REFUSED)


def launch_context_guard(team: roster.Team, member: Any, env: Dict[str, str]) -> None:
    """Hash local routing inputs once at launch submission, never during idle polling."""
    row = member if isinstance(member, dict) else member.to_json()
    if not policy(team.config) or not row.get("staffing"):
        return
    launch_guard(team, member, starting=True)
    chosen = attempts(team)[row["staffing"]["request_id"]]["selection"]
    from herdr_team import harnesses
    context = I.fingerprint(chosen["kind"], row.get("cwd") or policy(team.config)["cwd"], env,
                            harnesses.which(chosen["kind"], env), chosen.get("native_version"))
    if context != chosen.get("context"):
        raise HerdrTeamError("provider_context_changed", "native configuration or executable changed before launch; the operator must reapprove", EXIT_REFUSED)


def selection_guard(team: roster.Team, member: Any, chosen: Dict[str, Any], author: Any) -> None:
    owned = member.get("staffing") if isinstance(member, dict) else getattr(member, "staffing", None)
    if not policy(team.config) or not owned or author.trusted_human:
        return
    selection_authority(team, member, author)
    launch_guard(team, member, chosen)


def selection_authority(team: roster.Team, member: Any, author: Any) -> None:
    row = member if isinstance(member, dict) else member.to_json()
    # Preserve existing self model/profile control, within the same approved pool.
    if (author.verified and author.team == team.team and author.name == row.get("name")
            and author.terminal_id == row.get("terminal_id") and author.generation == row.get("generation")
            and row.get("status") not in ("left", "missing")):
        return
    require_manager(team, author)


def control_guard(doc: Dict[str, Any], member: Dict[str, Any], record: Dict[str, Any], control: Dict[str, Any], starting: bool = False) -> None:
    """Revalidate an owned worker at execution, including the manager's current identity."""
    if not member.get("staffing") or not policy(doc.get("config")):
        return
    if record.get("from") == "human":
        # The existing human-origin gate still runs. New manager restrictions never remove operator actions.
        return
    team = roster.Team.from_json(doc)
    launch_guard(team, member, starting=starting)
    if record.get("from") != "human":
        manager = team.manager()
        actor = (record.get("from"), record.get("from_terminal"), record.get("from_gen"))
        self_change = member.get("status") not in ("left", "missing") and actor == (member.get("name"), member.get("terminal_id"), member.get("generation"))
        manager_change = bool(manager and manager.status in ("active", "starting") and actor == (manager.name, manager.terminal_id, manager.generation))
        if not (manager_change or self_change):
            raise HerdrTeamError("staffing_authority", "queued control is not from the current manager", EXIT_REFUSED)
    selected = attempts(team)[member["staffing"]["request_id"]]["selection"]
    for key in ("kind", "model", "effort", "profile"):
        if control.get(key) is not None and control.get(key) != selected.get(key):
            raise HerdrTeamError("staffing_setting_changed", "queued control no longer matches the approved worker selection", EXIT_REFUSED)


def remember_selection(team: roster.Team, member: roster.Member, chosen: Dict[str, Any], author: Any) -> None:
    if not member.staffing or not policy(team.config) or author.trusted_human:
        return
    selection_authority(team, member, author)
    launch_guard(team, member, chosen)
    attempts(team)[member.staffing["request_id"]]["selection"] = copy.deepcopy(chosen)

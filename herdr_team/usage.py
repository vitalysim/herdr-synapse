"""Usage limits across the agents of a session (docs/cli.md section 9, ``usage``).

Limits belong to a provider account, not to a pane: every Claude Code pane on
this machine draws on the same session and weekly windows, every Codex pane on
the same ChatGPT plan. So the report groups the session's agents by the
provider their kind (or their login file) points at and shows each provider's
windows once, the way ``/usage`` in Claude Code and ``/status`` in Codex do.

Sources (all read-only; one GET per provider, never a write):

- ``anthropic``: Claude Code's OAuth login (``~/.claude/.credentials.json`` or
  the macOS keychain item ``Claude Code-credentials``) →
  ``https://api.anthropic.com/api/oauth/usage``. Also used by Pi and OpenCode
  when their auth file holds an Anthropic OAuth login.
- ``openai-codex``: Codex CLI's ChatGPT login (``~/.codex/auth.json``) →
  ``https://chatgpt.com/backend-api/wham/usage``; the newest rollout log's
  ``rate_limits`` event is the offline fallback.
- ``copilot``: ``gh auth token`` → ``https://api.github.com/copilot_internal/user``
  (``quota_snapshots``; some plans report none).
- ``gemini``: ``~/.gemini/oauth_creds.json`` → Code Assist ``retrieveUserQuota``,
  only while the stored token is still valid (this module never refreshes a
  token).

Tokens never leave the process: they are not logged, not written anywhere,
and not part of ``--json`` output; error strings carry status codes only.
Every function that touches the network takes a timeout and returns a report
row with ``ok: false`` instead of raising.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

DEFAULT_TIMEOUT_S = 8.0
KEYCHAIN_TIMEOUT_S = 5.0
USER_AGENT = "herdr-synapse-usage/1"

ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
COPILOT_USER_URL = "https://api.github.com/copilot_internal/user"
GEMINI_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_CODEX = "openai-codex"
PROVIDER_COPILOT = "copilot"
PROVIDER_GEMINI = "gemini"
PROVIDER_OPENCODE_ZEN = "opencode-zen"

PROVIDER_TITLES = {
    PROVIDER_ANTHROPIC: "Anthropic",
    PROVIDER_CODEX: "OpenAI Codex",
    PROVIDER_COPILOT: "GitHub Copilot",
    PROVIDER_GEMINI: "Google Gemini",
    PROVIDER_OPENCODE_ZEN: "OpenCode Zen",
}

#: Agent kinds whose provider is fixed by the kind itself.
KIND_PROVIDERS: Dict[str, str] = {
    "claude": PROVIDER_ANTHROPIC,
    "codex": PROVIDER_CODEX,
    "copilot": PROVIDER_COPILOT,
    "gemini": PROVIDER_GEMINI,
    "antigravity": PROVIDER_GEMINI,
}
#: Kinds whose provider depends on which login their auth file holds.
LOGIN_KINDS = ("pi", "opencode")

WARN_PERCENT = 75.0
CRIT_PERCENT = 90.0

Window = Dict[str, Any]
Provider = Dict[str, Any]


class UsageError(Exception):
    """A source could not be read; the message is safe to show (no secrets)."""


# --------------------------------------------------------------------------
# small helpers


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _epoch_iso(value: Any) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def parse_iso(value: Any) -> Optional[datetime]:
    """An aware UTC datetime from an ISO string with ``Z``, an offset, or a bare date; None otherwise."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percent(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def severity_for(percent: Optional[float]) -> Optional[str]:
    if percent is None:
        return None
    if percent >= CRIT_PERCENT:
        return "critical"
    if percent >= WARN_PERCENT:
        return "warning"
    return "normal"


def window(label: str, percent: Any, resets_at: Any = None, wid: Optional[str] = None, scope: Optional[str] = None, detail: Optional[str] = None, severity: Optional[str] = None) -> Window:
    pct = _percent(percent)
    return {
        "id": wid or label.lower().replace(" ", "_"),
        "label": label,
        "percent": pct,
        "resets_at": resets_at if isinstance(resets_at, str) else _epoch_iso(resets_at),
        "severity": severity or severity_for(pct),
        "scope": scope,
        "detail": detail,
    }


def _provider(pid: str, title: Optional[str] = None, **fields: Any) -> Provider:
    row: Provider = {
        "id": pid,
        "title": title or PROVIDER_TITLES.get(pid, pid),
        "plan": None,
        "login": None,
        "source": None,
        "fetched_at": None,
        "as_of": None,
        "ok": False,
        "error": None,
        "windows": [],
        "kinds": [],
        "agents": [],
        "note": None,
    }
    row.update(fields)
    return row


def http_json(url: str, headers: Mapping[str, str], timeout: float, body: Optional[Dict[str, Any]] = None) -> Any:
    """GET (or POST with ``body``) and decode JSON; errors carry the status code only."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=dict(headers))
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https hosts above)
            raw = response.read()
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise UsageError("HTTP {}: login expired or not authorized; log in again with the agent's own CLI".format(err.code))
        if err.code == 429:
            raise UsageError("HTTP 429: the usage endpoint is rate limiting; try again in a minute")
        raise UsageError("HTTP {}".format(err.code))
    except urllib.error.URLError as err:
        reason = getattr(err, "reason", err)
        raise UsageError("network: {}".format(reason))
    except (OSError, ValueError) as err:
        raise UsageError("network: {}".format(err.__class__.__name__))
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise UsageError("the usage endpoint did not answer with JSON")


def _read_json_file(path: Path) -> Optional[Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or os.path.expanduser("~"))


def _run(argv: List[str], timeout: float) -> Optional[str]:
    """stdout of a short helper command, or None; never raises."""
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Anthropic (Claude Code, and Pi / OpenCode logged in with Anthropic OAuth)


def claude_credentials(env: Mapping[str, str], run: Callable[[List[str], float], Optional[str]] = _run) -> Optional[Dict[str, Any]]:
    """The ``claudeAiOauth`` object of Claude Code's login, from the credentials file or the macOS keychain."""
    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or (_home(env) / ".claude"))

    def oauth_of(doc: Any) -> Optional[Dict[str, Any]]:
        oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
        return oauth if isinstance(oauth, dict) and oauth.get("accessToken") else None

    found = oauth_of(_read_json_file(config_dir / ".credentials.json"))
    if found is None and sys.platform == "darwin":
        # Claude Code on macOS keeps the login in the keychain; the item was written through the
        # same ``security`` binary, so reading it back prompts for nothing.
        raw = run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"], KEYCHAIN_TIMEOUT_S)
        if raw:
            try:
                found = oauth_of(json.loads(raw.strip()))
            except ValueError:
                found = None
    return found


def anthropic_plan(oauth: Mapping[str, Any]) -> Optional[str]:
    """``Max 20x`` from ``rateLimitTier`` / ``subscriptionType``."""
    tier = str(oauth.get("rateLimitTier") or "")
    sub = str(oauth.get("subscriptionType") or "")
    label = sub.capitalize() if sub else None
    if tier:
        parts = [p for p in tier.split("_") if p not in ("default", "claude")]
        tail = " ".join(p.capitalize() if not p.endswith("x") or not p[:-1].isdigit() else p for p in parts)
        if tail:
            label = tail if not label or label.lower() in tail.lower() else "{} {}".format(label, tail)
    return label or None


_ANTHROPIC_LABELS = {"session": "Current session", "weekly_all": "Current week (all models)"}


def parse_anthropic(body: Mapping[str, Any]) -> Tuple[List[Window], Optional[str]]:
    """Windows from ``limits`` (preferred) or the legacy ``five_hour``/``seven_day`` fields, plus a note."""
    windows: List[Window] = []
    limits = body.get("limits")
    if isinstance(limits, list) and limits:
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope, dict) and isinstance(scope.get("model"), dict) else {}
            display = model.get("display_name") if isinstance(model, dict) else None
            if kind in _ANTHROPIC_LABELS:
                label = _ANTHROPIC_LABELS[kind]
            elif kind == "weekly_scoped":
                label = "Current week ({})".format(display or "model")
            else:
                label = kind.replace("_", " ").capitalize() or "Limit"
            windows.append(window(label, item.get("percent"), item.get("resets_at"), wid=kind if kind != "weekly_scoped" else "weekly_{}".format(str(display or "model").lower()), scope=str(display) if display else None, severity=item.get("severity") if isinstance(item.get("severity"), str) else None))
    else:
        for key, label in (("five_hour", "Current session"), ("seven_day", "Current week (all models)"), ("seven_day_opus", "Current week (Opus)"), ("seven_day_sonnet", "Current week (Sonnet)")):
            item = body.get(key)
            if isinstance(item, dict) and item.get("utilization") is not None:
                windows.append(window(label, item.get("utilization"), item.get("resets_at"), wid=key))
    note = None
    spend = body.get("spend") if isinstance(body.get("spend"), dict) else None
    extra = body.get("extra_usage") if isinstance(body.get("extra_usage"), dict) else None
    if spend or extra:
        pct = (spend or {}).get("percent") if spend else (extra or {}).get("utilization")
        enabled = bool((spend or {}).get("enabled")) if spend else bool((extra or {}).get("is_enabled"))
        detail = None
        used = (spend or {}).get("used") if spend else None
        limit = (spend or {}).get("limit") if spend else None
        if isinstance(used, dict) and isinstance(limit, dict):
            detail = "{} of {}".format(_money(used), _money(limit))
        elif extra and extra.get("monthly_limit") is not None:
            detail = "{:.2f} of {:.2f} {}".format(float(extra.get("used_credits") or 0) / 100.0, float(extra.get("monthly_limit") or 0) / 100.0, extra.get("currency") or "")
        reason = (spend or {}).get("disabled_reason") if spend else (extra or {}).get("disabled_reason")
        if not enabled:
            detail = "{}; off{}".format(detail or "credits", " ({})".format(str(reason).replace("_", " ")) if reason else "")
        if enabled or (pct is not None and float(pct) > 0):
            windows.append(window("Extra usage (credits)", pct, None, wid="extra_usage", detail=detail))
    return windows, note


def _money(obj: Mapping[str, Any]) -> str:
    minor = obj.get("amount_minor")
    exponent = obj.get("exponent", 2)
    currency = str(obj.get("currency") or "")
    if isinstance(minor, bool) or not isinstance(minor, (int, float)):
        return "?"
    try:
        value = float(minor) / (10 ** int(exponent))
    except (TypeError, ValueError):
        return "?"
    symbol = "$" if currency == "USD" else (currency + " " if currency else "")
    return "{}{:.2f}".format(symbol, value)


def anthropic_source(env: Mapping[str, str], fetch: bool = True, timeout: float = DEFAULT_TIMEOUT_S, http: Callable[..., Any] = http_json, run: Callable[[List[str], float], Optional[str]] = _run) -> Provider:
    row = _provider(PROVIDER_ANTHROPIC, source="api.anthropic.com/api/oauth/usage")
    oauth = claude_credentials(env, run=run)
    if oauth is None:
        row["error"] = "no Claude Code login found (~/.claude/.credentials.json or the keychain item \"Claude Code-credentials\")"
        return row
    row["login"] = "Claude Code login"
    row["plan"] = anthropic_plan(oauth)
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and float(expires) / 1000.0 < time.time():
        row["note"] = "the stored access token has expired; Claude Code refreshes it on its next request"
    if not fetch:
        row["error"] = "not fetched (--no-fetch)"
        return row
    try:
        body = http(ANTHROPIC_USAGE_URL, {"Authorization": "Bearer {}".format(oauth["accessToken"]), "anthropic-beta": ANTHROPIC_BETA}, timeout)
    except UsageError as err:
        row["error"] = str(err)
        return row
    if not isinstance(body, dict):
        row["error"] = "unexpected response shape"
        return row
    windows, note = parse_anthropic(body)
    row.update({"ok": True, "windows": windows, "fetched_at": _now_iso(), "as_of": _now_iso(), "note": note or row["note"]})
    return row


# --------------------------------------------------------------------------
# OpenAI Codex (ChatGPT plan)


def codex_home(env: Mapping[str, str]) -> Path:
    return Path(env.get("CODEX_HOME") or (_home(env) / ".codex"))


def codex_auth(env: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    doc = _read_json_file(codex_home(env) / "auth.json")
    if not isinstance(doc, dict):
        return None
    tokens = doc.get("tokens") if isinstance(doc.get("tokens"), dict) else {}
    if not tokens.get("access_token"):
        return {"auth_mode": doc.get("auth_mode"), "api_key": bool(doc.get("OPENAI_API_KEY"))}
    return {"auth_mode": doc.get("auth_mode"), "access_token": tokens["access_token"], "account_id": tokens.get("account_id"), "api_key": bool(doc.get("OPENAI_API_KEY"))}


def _codex_window_label(seconds: Any, minutes: Any = None) -> Tuple[str, str]:
    total = None
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        total = float(seconds)
    elif isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
        total = float(minutes) * 60.0
    if total is None:
        return "Limit", "limit"
    hours = total / 3600.0
    if hours <= 24:
        return "Current session ({:g}h)".format(round(hours)), "session"
    days = total / 86400.0
    if 6.5 <= days <= 7.5:
        return "Current week", "weekly"
    return "{:g}-day window".format(round(days)), "window_{:g}d".format(round(days))


def parse_codex(body: Mapping[str, Any]) -> Tuple[List[Window], Optional[str], Optional[str]]:
    """``(windows, plan, note)`` from ``wham/usage``."""
    windows: List[Window] = []
    rate = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
    for key in ("primary_window", "secondary_window"):
        item = rate.get(key)
        if isinstance(item, dict):
            label, wid = _codex_window_label(item.get("limit_window_seconds"))
            windows.append(window(label, item.get("used_percent"), item.get("reset_at"), wid=wid))
    for extra in body.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        name = str(extra.get("limit_name") or extra.get("metered_feature") or "extra")
        inner = extra.get("rate_limit") if isinstance(extra.get("rate_limit"), dict) else {}
        for key in ("primary_window", "secondary_window"):
            item = inner.get(key)
            if isinstance(item, dict):
                label, wid = _codex_window_label(item.get("limit_window_seconds"))
                windows.append(window("{}: {}".format(name, label.replace("Current ", "")), item.get("used_percent"), item.get("reset_at"), wid="{}_{}".format(name.lower().replace(" ", "_"), wid), scope=name))
    plan = str(body.get("plan_type")).capitalize() if body.get("plan_type") else None
    note = None
    if rate.get("limit_reached"):
        note = "limit reached"
    credits = body.get("credits") if isinstance(body.get("credits"), dict) else None
    if credits and credits.get("has_credits"):
        note = "credits available: {}".format(credits.get("balance"))
    return windows, plan, note


def codex_rollout_rate_limits(home: Path, max_files: int = 12) -> Optional[Tuple[Dict[str, Any], str]]:
    """``(rate_limits, timestamp)`` of the newest ``token_count`` event in the newest rollout logs, or None."""
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    candidates: List[Tuple[float, Path]] = []
    try:
        for path in sessions.rglob("rollout-*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return None
    candidates.sort(reverse=True)
    for _mtime, path in candidates[:max_files]:
        last: Optional[Tuple[Dict[str, Any], str]] = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    payload = rec.get("payload") if isinstance(rec, dict) and isinstance(rec.get("payload"), dict) else None
                    limits = payload.get("rate_limits") if payload else None
                    if isinstance(limits, dict):
                        last = (limits, str(rec.get("timestamp") or ""))
        except OSError:
            continue
        if last is not None:
            return last
    return None


def parse_codex_rollout(limits: Mapping[str, Any]) -> Tuple[List[Window], Optional[str]]:
    windows: List[Window] = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if isinstance(item, dict):
            label, wid = _codex_window_label(None, item.get("window_minutes"))
            resets = item.get("resets_at")
            windows.append(window(label, item.get("used_percent"), resets, wid=wid))
    plan = str(limits.get("plan_type")).capitalize() if limits.get("plan_type") else None
    return windows, plan


def codex_source(env: Mapping[str, str], fetch: bool = True, timeout: float = DEFAULT_TIMEOUT_S, http: Callable[..., Any] = http_json) -> Provider:
    row = _provider(PROVIDER_CODEX, source="chatgpt.com/backend-api/wham/usage")
    home = codex_home(env)
    auth = codex_auth(env)
    if auth is None:
        row["error"] = "no Codex login found ({}/auth.json)".format(home)
    elif not auth.get("access_token"):
        row["error"] = "Codex uses an API key here; usage windows exist only for ChatGPT logins"
        row["login"] = "API key"
    else:
        row["login"] = "ChatGPT login"
        if fetch:
            headers = {"Authorization": "Bearer {}".format(auth["access_token"])}
            if auth.get("account_id"):
                headers["ChatGPT-Account-Id"] = str(auth["account_id"])
            try:
                body = http(CODEX_USAGE_URL, headers, timeout)
                if not isinstance(body, dict):
                    raise UsageError("unexpected response shape")
                windows, plan, note = parse_codex(body)
                row.update({"ok": True, "windows": windows, "plan": plan, "note": note, "fetched_at": _now_iso(), "as_of": _now_iso()})
                return row
            except UsageError as err:
                row["error"] = str(err)
        else:
            row["error"] = "not fetched (--no-fetch)"
    fallback = codex_rollout_rate_limits(home)
    if fallback is not None:
        limits, stamp = fallback
        windows, plan = parse_codex_rollout(limits)
        if windows:
            row.update({"ok": True, "windows": windows, "plan": row.get("plan") or plan, "source": "session log ({}/sessions)".format(home), "as_of": stamp or None, "fetched_at": _now_iso()})
            if row.get("error"):
                row["note"] = "showing the last values Codex logged" if not fetch else "live fetch failed ({}); showing the last values Codex logged".format(row["error"])
                row["error"] = None
    return row


# --------------------------------------------------------------------------
# GitHub Copilot


def copilot_token(env: Mapping[str, str], run: Callable[[List[str], float], Optional[str]] = _run) -> Optional[str]:
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if env.get(key):
            return str(env[key])
    out = run(["gh", "auth", "token"], KEYCHAIN_TIMEOUT_S)
    token = (out or "").strip()
    return token or None


_COPILOT_LABELS = {"premium_interactions": "Premium requests", "chat": "Chat", "completions": "Completions"}


def parse_copilot(body: Mapping[str, Any]) -> Tuple[List[Window], Optional[str], Optional[str]]:
    windows: List[Window] = []
    plan = str(body.get("copilot_plan")).capitalize() if body.get("copilot_plan") else None
    snapshots = body.get("quota_snapshots")
    reset = body.get("quota_reset_date")
    resets_at = "{}T00:00:00Z".format(reset) if isinstance(reset, str) and len(reset) == 10 else (reset if isinstance(reset, str) else None)
    if isinstance(snapshots, dict) and snapshots:
        for key, label in _COPILOT_LABELS.items():
            item = snapshots.get(key)
            if not isinstance(item, dict):
                continue
            if item.get("unlimited"):
                windows.append(window(label, 0.0, resets_at, wid=key, detail="unlimited", severity="normal"))
                continue
            remaining = item.get("percent_remaining")
            used = 100.0 - float(remaining) if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) else None
            detail = None
            if isinstance(item.get("remaining"), (int, float)) and isinstance(item.get("entitlement"), (int, float)):
                detail = "{:g} of {:g} left".format(float(item["remaining"]), float(item["entitlement"]))
            windows.append(window(label, used, resets_at, wid=key, detail=detail))
        return windows, plan, None
    return windows, plan, "GitHub reports no quota for this plan{}".format(" ({})".format(plan.lower()) if plan else "")


def copilot_source(env: Mapping[str, str], fetch: bool = True, timeout: float = DEFAULT_TIMEOUT_S, http: Callable[..., Any] = http_json, run: Callable[[List[str], float], Optional[str]] = _run) -> Provider:
    row = _provider(PROVIDER_COPILOT, source="api.github.com/copilot_internal/user")
    if not fetch:
        row["error"] = "not fetched (--no-fetch)"
        return row
    token = copilot_token(env, run=run)
    if not token:
        row["error"] = "no GitHub token (gh auth login, or GH_TOKEN)"
        return row
    row["login"] = "gh login"
    try:
        body = http(COPILOT_USER_URL, {"Authorization": "token {}".format(token)}, timeout)
    except UsageError as err:
        row["error"] = str(err)
        return row
    if not isinstance(body, dict):
        row["error"] = "unexpected response shape"
        return row
    windows, plan, note = parse_copilot(body)
    row.update({"ok": True, "windows": windows, "plan": plan, "note": note, "fetched_at": _now_iso(), "as_of": _now_iso()})
    return row


# --------------------------------------------------------------------------
# Google Gemini (Gemini CLI, Antigravity)


def gemini_credentials(env: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    doc = _read_json_file(_home(env) / ".gemini" / "oauth_creds.json")
    return doc if isinstance(doc, dict) and doc.get("access_token") else None


def parse_gemini(body: Mapping[str, Any]) -> List[Window]:
    """Best effort over ``buckets`` (``modelId``, ``remainingFraction``, ``resetTime``); unknown shapes yield nothing."""
    windows: List[Window] = []
    buckets = body.get("buckets")
    if not isinstance(buckets, list):
        return windows
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        fraction = bucket.get("remainingFraction")
        used = (1.0 - float(fraction)) * 100.0 if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) else None
        model = str(bucket.get("modelId") or bucket.get("tokenType") or "requests")
        detail = None
        if isinstance(bucket.get("remainingAmount"), (int, float)):
            detail = "{:g} left".format(float(bucket["remainingAmount"]))
        windows.append(window("Daily requests ({})".format(model), used, bucket.get("resetTime") if isinstance(bucket.get("resetTime"), str) else None, wid="daily_{}".format(model.lower()), scope=model, detail=detail))
    return windows


def gemini_source(env: Mapping[str, str], fetch: bool = True, timeout: float = DEFAULT_TIMEOUT_S, http: Callable[..., Any] = http_json) -> Provider:
    row = _provider(PROVIDER_GEMINI, source="cloudcode-pa.googleapis.com retrieveUserQuota")
    creds = gemini_credentials(env)
    if creds is None:
        row["error"] = "no Gemini CLI login found (~/.gemini/oauth_creds.json)"
        return row
    row["login"] = "Google login"
    expiry = creds.get("expiry_date")
    if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and float(expiry) / 1000.0 < time.time():
        row["error"] = "the stored Google token has expired; run gemini once to refresh it (herdr-synapse never refreshes tokens)"
        return row
    if not fetch:
        row["error"] = "not fetched (--no-fetch)"
        return row
    try:
        body = http(GEMINI_QUOTA_URL, {"Authorization": "Bearer {}".format(creds["access_token"])}, timeout, body={})
    except UsageError as err:
        row["error"] = str(err)
        return row
    windows = parse_gemini(body if isinstance(body, dict) else {})
    if not windows:
        row["error"] = "quota response not understood"
        return row
    row.update({"ok": True, "windows": windows, "fetched_at": _now_iso(), "as_of": _now_iso()})
    return row


# --------------------------------------------------------------------------
# login-dependent kinds


def _login_provider_from_keys(keys: Iterable[str], types: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    names = [str(k) for k in keys]
    for key in names:
        if key == "anthropic" and str(types.get(key) or "oauth") == "oauth":
            return PROVIDER_ANTHROPIC, "Anthropic login"
    for key in names:
        if key in ("openai-codex", "codex") or (key == "openai" and str(types.get(key) or "") == "oauth"):
            return PROVIDER_CODEX, "ChatGPT login"
    for key in names:
        if key in ("opencode", "opencode-go", "opencode-zen"):
            return PROVIDER_OPENCODE_ZEN, "OpenCode Zen API key"
    if names:
        return None, "API keys: {}".format(", ".join(sorted(names)[:4]))
    return None, None


def login_provider(kind: str, env: Mapping[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """``(provider id, login note)`` for a kind whose provider depends on its auth file."""
    if kind == "pi":
        doc = _read_json_file(_home(env) / ".pi" / "agent" / "auth.json")
    elif kind == "opencode":
        data_home = Path(env.get("XDG_DATA_HOME") or (_home(env) / ".local" / "share"))
        doc = _read_json_file(data_home / "opencode" / "auth.json")
    else:
        return None, None
    if not isinstance(doc, dict):
        return None, "no login file"
    types = {k: (v.get("type") if isinstance(v, dict) else None) for k, v in doc.items()}
    return _login_provider_from_keys(doc.keys(), types)


def provider_for_kind(kind: str, env: Mapping[str, str]) -> Tuple[Optional[str], Optional[str]]:
    fixed = KIND_PROVIDERS.get(kind)
    if fixed:
        return fixed, None
    if kind in LOGIN_KINDS:
        return login_provider(kind, env)
    return None, None


# --------------------------------------------------------------------------
# the report


SOURCES: Dict[str, Callable[..., Provider]] = {
    PROVIDER_ANTHROPIC: anthropic_source,
    PROVIDER_CODEX: codex_source,
    PROVIDER_COPILOT: copilot_source,
    PROVIDER_GEMINI: gemini_source,
}


def _has_login(pid: str, env: Mapping[str, str]) -> bool:
    home = _home(env)
    if pid == PROVIDER_ANTHROPIC:
        return (Path(env.get("CLAUDE_CONFIG_DIR") or (home / ".claude"))).exists()
    if pid == PROVIDER_CODEX:
        return (codex_home(env) / "auth.json").exists()
    if pid == PROVIDER_COPILOT:
        return (home / ".copilot").exists() or (home / ".config" / "github-copilot").exists()
    if pid == PROVIDER_GEMINI:
        return (home / ".gemini" / "oauth_creds.json").exists()
    return False


def agent_rows(agents: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for agent in agents:
        kind = agent.get("agent")
        if not isinstance(kind, str) or not kind:
            continue
        rows.append({
            "kind": kind,
            "name": agent.get("name") if isinstance(agent.get("name"), str) else None,
            "pane_id": str(agent.get("pane_id") or ""),
            "status": str(agent.get("agent_status") or "unknown"),
        })
    return rows


def collect(env: Mapping[str, str], agents: Iterable[Mapping[str, Any]], fetch: bool = True, timeout: float = DEFAULT_TIMEOUT_S, sources: Optional[Mapping[str, Callable[..., Provider]]] = None, include_logins: bool = True, agents_error: Optional[str] = None) -> Dict[str, Any]:
    """The usage report: providers (each with its windows and the agents drawing on it) and untracked agents.

    A provider appears when an agent of its kind is running or, with
    ``include_logins``, when its login store exists on this machine. Sources
    run in parallel threads, each bounded by ``timeout``.
    """
    sources = dict(sources if sources is not None else SOURCES)
    rows = agent_rows(agents)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    kinds_by_provider: Dict[str, List[str]] = {}
    logins: Dict[str, str] = {}
    untracked: List[Dict[str, Any]] = []
    for row in rows:
        pid, login = provider_for_kind(row["kind"], env)
        if pid is None:
            untracked.append(dict(row, reason=login or "no known usage source for this kind"))
            continue
        grouped.setdefault(pid, []).append(row)
        if row["kind"] not in kinds_by_provider.setdefault(pid, []):
            kinds_by_provider[pid].append(row["kind"])
        if login and pid not in logins:
            logins[pid] = login
    wanted: List[str] = [pid for pid in (PROVIDER_ANTHROPIC, PROVIDER_CODEX, PROVIDER_COPILOT, PROVIDER_GEMINI, PROVIDER_OPENCODE_ZEN) if pid in grouped]
    if include_logins:
        for pid in (PROVIDER_ANTHROPIC, PROVIDER_CODEX, PROVIDER_COPILOT, PROVIDER_GEMINI):
            if pid not in wanted and _has_login(pid, env):
                wanted.append(pid)

    def run_source(pid: str) -> Provider:
        source = sources.get(pid)
        if source is None:
            row = _provider(pid, note="usage is billed per token; this provider publishes no limit window")
            row["ok"] = True
            return row
        try:
            return source(env, fetch=fetch, timeout=timeout)
        except Exception as err:  # noqa: BLE001 - a source bug must not hide the other providers
            return _provider(pid, error="source failed: {}".format(err.__class__.__name__))

    providers: List[Provider] = []
    if wanted:
        with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
            futures = {pid: pool.submit(run_source, pid) for pid in wanted}
            for pid in wanted:
                try:
                    row = futures[pid].result(timeout=timeout + 2.0)
                except Exception as err:  # noqa: BLE001
                    row = _provider(pid, error="source timed out ({})".format(err.__class__.__name__))
                row["kinds"] = kinds_by_provider.get(pid, [])
                row["agents"] = grouped.get(pid, [])
                if not row.get("login") and logins.get(pid):
                    row["login"] = logins[pid]
                providers.append(row)
    return {"v": 1, "generated_at": _now_iso(), "fetched": bool(fetch), "agents": len(rows), "providers": providers, "untracked": untracked, "agents_error": agents_error}


# --------------------------------------------------------------------------
# rendering


def bar(percent: Optional[float], cells: int, ascii_only: bool = False) -> str:
    if percent is None:
        return ("-" if ascii_only else "·") * cells
    filled = int(round(max(0.0, min(100.0, percent)) / 100.0 * cells))
    full, empty = ("#", ".") if ascii_only else ("█", "░")
    return full * filled + empty * (cells - filled)


def reset_label(resets_at: Optional[str], now: Optional[datetime] = None) -> str:
    """``resets in 2h 05m (15:00)`` under a day, ``resets Tue 8 Sep 15:00`` beyond, local time."""
    when = parse_iso(resets_at)
    if when is None:
        return ""
    now = now or datetime.now(timezone.utc)
    local = when.astimezone()
    delta = when - now
    if delta <= timedelta(0):
        return "resets now"
    if delta < timedelta(hours=24):
        total = int(delta.total_seconds())
        hours, minutes = total // 3600, (total % 3600) // 60
        rel = "{}h {:02d}m".format(hours, minutes) if hours else "{}m".format(max(1, minutes))
        return "resets in {} ({})".format(rel, local.strftime("%H:%M"))
    return "resets {}".format(local.strftime("%a %-d %b %H:%M") if os.name != "nt" else local.strftime("%a %d %b %H:%M"))


def _age_label(stamp: Optional[str], now: datetime) -> Optional[str]:
    when = parse_iso(stamp)
    if when is None:
        return None
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return "{} min ago".format(seconds // 60)
    if seconds < 86400:
        return "{}h ago".format(seconds // 3600)
    return "{}d ago".format(seconds // 86400)


def agents_label(row: Provider, width: int) -> str:
    agents = row.get("agents") or []
    if not agents:
        return "no agent in this session" + (" ({} found)".format(row["login"]) if row.get("login") else "")
    kinds = row.get("kinds") or []
    names = []
    for agent in agents:
        label = agent.get("name") or agent.get("kind")
        pane = agent.get("pane_id")
        names.append("{} {}".format(label, pane) if pane else str(label))
    head = "{} ×{}".format("/".join(kinds), len(agents))
    text = "{}: {}".format(head, ", ".join(names))
    if len(text) > width:
        shown = []
        for name in names:
            candidate = "{}: {} (+{})".format(head, ", ".join(shown + [name]), len(names) - len(shown) - 1)
            if len(candidate) > width and shown:
                break
            shown.append(name)
        rest = len(names) - len(shown)
        text = "{}: {}{}".format(head, ", ".join(shown), " (+{})".format(rest) if rest else "")
    return text


def format_report(report: Mapping[str, Any], width: int = 100, now: Optional[datetime] = None, ascii_only: bool = False, hints: Optional[str] = None) -> List[Tuple[str, str]]:
    """``(text, style)`` rows; styles: ``title``, ``normal``, ``warn``, ``crit``, ``dim``, ``error``."""
    now = now or datetime.now(timezone.utc)
    width = max(40, width)
    cells = 20 if width >= 90 else 12 if width >= 64 else 8
    rows: List[Tuple[str, str]] = []
    providers = list(report.get("providers") or [])
    header = "Usage limits · {} agent{} · {} provider{}".format(report.get("agents", 0), "" if report.get("agents") == 1 else "s", len(providers), "" if len(providers) == 1 else "s")
    if hints:
        header = "{} · {}".format(header, hints)
    rows.append((header, "title"))
    if report.get("agents_error"):
        rows.append(("agents not listed: {}".format(report["agents_error"]), "error"))
    label_width = 0
    for row in providers:
        for win in row.get("windows") or []:
            label_width = max(label_width, len(str(win.get("label") or "")))
    label_width = min(max(label_width, 16), max(16, width - cells - 30))
    for row in providers:
        rows.append(("", "normal"))
        title = str(row.get("title") or row.get("id"))
        if row.get("plan"):
            title = "{} · {}".format(title, row["plan"])
        agents = agents_label(row, max(10, width - len(title) - 4))
        rows.append(("{}   {}".format(title, agents) if len(title) + 3 + len(agents) <= width else title, "title"))
        if len(title) + 3 + len(agents) > width:
            rows.append(("  " + agents, "dim"))
        if row.get("error"):
            rows.append(("  {}".format(row["error"]), "error"))
        for win in row.get("windows") or []:
            pct = win.get("percent")
            label = str(win.get("label") or "")[:label_width].ljust(label_width)
            pct_text = "{:>3.0f}% used".format(pct) if pct is not None else "   ?     "
            style = "normal"
            mark = ""
            severity = win.get("severity")
            if severity == "critical" or (pct is not None and pct >= CRIT_PERCENT):
                style, mark = "crit", (" !!" if ascii_only else " ‼")
            elif severity == "warning" or (pct is not None and pct >= WARN_PERCENT):
                style, mark = "warn", (" !" if ascii_only else " ⚠")
            tail = reset_label(win.get("resets_at"), now)
            if win.get("detail"):
                tail = "{}{}{}".format(win["detail"], "; " if tail else "", tail)
            line = "  {} {} {}{}".format(label, bar(pct, cells, ascii_only), pct_text, mark)
            if tail:
                line = "{}   {}".format(line, tail)
            rows.append((line[:width], style))
        if row.get("ok") and not row.get("windows") and not row.get("error"):
            rows.append(("  no limit window published", "dim"))
        if row.get("note"):
            rows.append(("  note: {}".format(row["note"]), "dim"))
        meta = []
        if row.get("source"):
            meta.append(str(row["source"]))
        age = _age_label(row.get("as_of"), now)
        if age and row.get("as_of") != row.get("fetched_at"):
            meta.append("as of {}".format(age))
        elif row.get("fetched_at"):
            meta.append("fetched {}".format(parse_iso(row["fetched_at"]).astimezone().strftime("%H:%M:%S") if parse_iso(row["fetched_at"]) else "now"))
        if meta:
            rows.append(("  {}".format(" · ".join(meta)), "dim"))
    untracked = list(report.get("untracked") or [])
    if untracked:
        rows.append(("", "normal"))
        counts: Dict[str, List[str]] = {}
        for agent in untracked:
            counts.setdefault(str(agent.get("kind")), []).append(str(agent.get("pane_id") or "?"))
        parts = ["{} ×{} ({})".format(kind, len(panes), ", ".join(panes[:3]) + (", …" if len(panes) > 3 else "")) for kind, panes in sorted(counts.items())]
        rows.append(("Not tracked: {}".format("; ".join(parts))[:width], "dim"))
        rows.append(("  no usage source is known for these kinds; their provider publishes no window or needs a login this machine lacks", "dim"))
    if not providers and not untracked:
        rows.append(("", "normal"))
        rows.append(("No agents and no known logins on this machine.", "dim"))
    return rows


def format_text(report: Mapping[str, Any], width: int = 100, ascii_only: bool = False) -> str:
    return "\n".join(text for text, _style in format_report(report, width=width, ascii_only=ascii_only))

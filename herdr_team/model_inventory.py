"""Model catalogs are candidates, not entitlement checks or credential exports.

Native protocols are optional. Older installations retain the existing manual
catalogs; unsupported metadata produces unknown readiness, never verified access.
Discovery is on demand, outside notifier ticks and pane-scaled presentation paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from herdr_team import harnesses as H, sanitize
from herdr_team.native_transport import JsonLines, NativeError, OpenCodeServer, metadata_command

KINDS = ("claude", "codex", "opencode")
METADATA_TTL = 300
VERIFICATION_TTL = 1800
_cache: Dict[str, Dict[str, Any]] = {}


def opaque(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def token(value: Any) -> Optional[str]:
    # Native identifiers are data but eventually become CLI flags. Reject controls and options.
    return value if isinstance(value, str) and 0 < len(value) <= 200 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+\[\]-]*", value) and not sanitize.secret_patterns(value) else None


def fingerprint(kind: str, cwd: str, env: Dict[str, str], path: Optional[str], version: Optional[str]) -> str:
    """Hash context privately; no configuration, credential or account value is exported."""
    home = Path(env.get("HOME") or "")
    codex = Path(env.get("CODEX_HOME") or home / ".codex")
    claude = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    opencode = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
    files: List[Path] = []
    if kind == "codex":
        files = [codex / "config.toml", codex / "auth.json"] + sorted(codex.glob("*.config.toml"))
    elif kind == "claude":
        files = [claude / "settings.json", claude / ".credentials.json"] + sorted((claude / "agents").glob("*.md"))
    elif kind == "opencode":
        files = [opencode / "opencode.json", opencode / "opencode.jsonc",
                 Path(env.get("XDG_DATA_HOME") or home / ".local/share") / "opencode/auth.json"]
    here = Path(cwd).resolve()
    for parent in [here] + list(here.parents):
        if kind == "claude":
            files += [parent / ".claude/settings.json", parent / ".claude/settings.local.json"]
            files += sorted((parent / ".claude/agents").glob("*.md"))
        elif kind == "codex":
            files += [parent / ".codex/config.toml"]
        else:
            files += [parent / "opencode.json", parent / "opencode.jsonc"]
        if (parent / ".git").exists():
            break
    for name in ("OPENCODE_CONFIG",):
        if env.get(name):
            files.append(Path(env[name]))
    hashes = []
    for file in files:
        try:
            hashes.append((str(file), hashlib.sha256(file.read_bytes()).hexdigest()))
        except FileNotFoundError:
            hashes.append((str(file), None))
        except OSError:
            # An unreadable configuration cannot yield reusable access evidence.
            hashes.append((str(file), os.urandom(16).hex()))
    try:
        stat = os.stat(path) if path else None
        executable = (os.path.realpath(path), stat.st_mtime_ns, stat.st_size) if stat else None
    except OSError:
        executable = None
    # Agent shells add transient thread IDs and PATH aliases. Hash native routing/
    # credentials, not those launch identities, so the approved pool is usable by a manager.
    prefixes = ("OPENAI_", "ANTHROPIC_", "AWS_", "AZURE_", "GOOGLE_", "GCLOUD_", "VERTEX_", "BEDROCK_", "OPENROUTER_", "DEEPINFRA_", "GEMINI_", "CLAUDE_CODE_USE_", "OPENCODE_")
    exact = {"HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY"}
    # Claude adds this IPC value to native tool children. It is not the OAuth
    # token or provider routing; hashing it makes an unchanged approved pool unusable.
    transient = {"CLAUDE_CODE_MESSAGING_TOKEN", "OPENCODE_SESSION_ID", "OPENCODE_PID", "OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"}
    relevant_env = {k: v for k, v in env.items() if k not in transient and
                    (k in exact or k.startswith(prefixes) or k.endswith(("_API_KEY", "_TOKEN", "_BASE_URL", "_ENDPOINT")))}
    return opaque([kind, str(here), version, executable, hashes, relevant_env])


def _version_at_least(version: Optional[str], floor: tuple) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version or "")
    return bool(match and tuple(int(p) for p in match.groups()) >= floor)


def claude_argv(path: str, profile: Optional[str] = None) -> List[str]:
    # Keep user auth/provider configuration, but no project hooks, MCP or skills can execute.
    argv = [path, "--print", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json",
            "--tools", "", "--disallowedTools", "*", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}', "--disable-slash-commands", "--no-session-persistence"]
    if profile:
        argv += ["--agent", profile]
    return argv


def _codex(path: str, cwd: str, env: Dict[str, str], profile: Optional[str]) -> Dict[str, Any]:
    argv = [path] + (["--profile", profile] if profile else []) + ["app-server", "--listen", "stdio://"]
    with JsonLines(argv, cwd, env) as rpc:
        rpc.rpc("initialize", {"clientInfo": {"name": "herdr_synapse_inventory", "version": "1"}})
        rpc.send({"method": "initialized", "params": {}})
        config = rpc.rpc("config/read", {"includeLayers": False})
        native_config = config.get("config") if isinstance(config.get("config"), dict) else {}
        # Null is the native default OpenAI route, not an unknown provider.
        provider = token(native_config.get("model_provider")) or ("openai" if native_config.get("model_provider") is None else None)
        if not provider:
            raise NativeError("provider_unknown")
        account = rpc.rpc("account/read", {"refreshToken": False})
        models: List[Dict[str, Any]] = []
        cursor = None
        seen = set()
        for _ in range(100):
            params: Dict[str, Any] = {"limit": 100, "includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            page = rpc.rpc("model/list", params)
            data = page.get("data")
            if not isinstance(data, list):
                raise NativeError("native_malformed")
            for entry in data:
                if not isinstance(entry, dict) or not token(entry.get("model") or entry.get("id")):
                    continue
                efforts = entry.get("supportedReasoningEfforts")
                models.append({"id": entry.get("model") or entry["id"], "provider": provider,
                               "efforts": [token(e.get("reasoningEffort")) for e in efforts if isinstance(e, dict) and token(e.get("reasoningEffort"))] if isinstance(efforts, list) else None,
                               "default_effort": token(entry.get("defaultReasoningEffort")), "hidden": entry.get("hidden") is True,
                               "input_modalities": [x for x in entry.get("inputModalities", []) if x in ("text", "image")]})
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise NativeError("native_pagination")
            seen.add(cursor)
        else:
            raise NativeError("native_pagination")
        auth = "credentials_reported" if isinstance(account.get("account"), dict) else "unknown"
        result = {"models": models, "providers": [{"id": provider, "authentication": auth}],
                  "account_context": opaque(account["account"]) if account.get("account") else None,
                  "native_context": opaque(native_config), "source": "codex app-server model/list", "quota": "unknown"}
        if (account.get("account") or {}).get("type") == "chatgpt":
            try:
                limits = rpc.rpc("account/rateLimits/read", {})
                # A native percentage is advisory, not an independent quota pool.
                result["quota"] = "reported" if limits.get("rateLimits") else "unknown"
                windows = limits.get("rateLimits") or {}
                if isinstance(windows, dict) and any(isinstance(windows.get(k), dict) and isinstance(windows[k].get("usedPercent"), (int, float)) and windows[k]["usedPercent"] >= 100 for k in ("primary", "secondary")):
                    result["quota"] = "exhausted"
            except NativeError:
                pass
        return result


def _claude(path: str, version: Optional[str], cwd: str, env: Dict[str, str], profile: Optional[str]) -> Dict[str, Any]:
    if not _version_at_least(version, (2, 1, 299)):
        raise NativeError("metadata_protocol_unsupported")
    # Do not activate a custom agent while discovering: an initialPrompt could spend tokens.
    native_env = dict(env, DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    deadline = time.monotonic() + 20
    auth: Dict[str, Any] = {}
    try:
        reported = json.loads(metadata_command([path, "auth", "status"], cwd, native_env, deadline))
        if isinstance(reported, dict):
            auth = reported
    except (NativeError, OSError, ValueError):
        pass
    with JsonLines(claude_argv(path), cwd, native_env, timeout=max(.01, deadline - time.monotonic())) as stream:
        stream.send({"type": "control_request", "request_id": "inventory", "request": {"subtype": "initialize", "hooks": {}}})
        for _ in range(100):
            frame = stream.receive()
            response = frame.get("response") or {}
            if frame.get("type") != "control_response" or response.get("request_id") != "inventory":
                continue
            if response.get("subtype") != "success":
                raise NativeError("native_method_rejected")
            meta = response.get("response") or {}
            rows = meta.get("models")
            account = meta.get("account") or {}
            provider = token(account.get("apiProvider"))
            if not isinstance(rows, list) or not provider:
                raise NativeError("native_malformed")
            models = []
            for row in rows:
                if not isinstance(row, dict) or not token(row.get("value")):
                    continue
                models.append({"id": row["value"], "resolved_model": token(row.get("resolvedModel")), "provider": provider,
                               "efforts": [e for e in row.get("supportedEffortLevels", []) if token(e)] or None,
                               "thinking": row.get("supportsAdaptiveThinking") if isinstance(row.get("supportsAdaptiveThinking"), bool) else None})
            authentication = "credentials_reported" if auth.get("loggedIn") is True or account.get("tokenSource") or account.get("apiKeySource") else "unknown"
            identified = any(account.get(k) for k in ("email", "organization", "id")) or any(auth.get(k) for k in ("email", "accountId", "organizationId"))
            return {"models": models, "providers": [{"id": provider, "authentication": authentication}],
                    "account_context": opaque([account, auth]) if identified else None, "native_context": opaque([provider, models]),
                    "native_account_context": opaque(account),
                    "source": "claude initialization metadata", "quota": "unknown"}
        raise NativeError("native_frame_limit")


def _opencode(path: str, cwd: str, env: Dict[str, str], profile: Optional[str]) -> Dict[str, Any]:
    deadline = time.monotonic() + 20
    code, help_text = H.RUN([path, "models", "--help"], cwd, env)
    if code == 0 and "--standalone" in help_text:
        # The v2 CLI owns its private server. Never consult or start its shared background service.
        native_env = dict(env, OPENCODE_DISABLE_EXTERNAL_PLUGINS="true", OPENCODE_DISABLE_AUTOUPDATE="true")
        listing = metadata_command([path, "models", "--standalone"], cwd, native_env, deadline)
        auth = json.loads(metadata_command([path, "auth", "list", "--format", "json", "--standalone"], cwd, native_env, deadline))
        if not isinstance(auth, list) or any(not isinstance(p, dict) for p in auth):
            raise NativeError("native_malformed")
        connected = {p["id"] for p in auth if token(p.get("id")) and p.get("connections")}
        ids = [line.strip() for line in listing.splitlines() if token(line.strip()) and "/" in line]
        providers = sorted({value.split("/", 1)[0] for value in ids})
        return {"models": [{"id": value, "provider": value.split("/", 1)[0], "configured": value.split("/", 1)[0] in connected} for value in ids],
                "providers": [{"id": value, "configured": value in connected, "authentication": "credentials_reported" if value in connected else "unknown"} for value in providers],
                "account_context": opaque(auth) if connected else None, "native_context": opaque([ids, auth]),
                "source": "opencode standalone models + auth metadata (v2)", "quota": "unknown"}
    try:
        with OpenCodeServer(path, cwd, env) as server:
            providers = server.request("/provider")
            config = server.request("/config/providers")
        if not isinstance(providers.get("all"), list) or not isinstance(providers.get("connected"), list):
            raise NativeError("native_malformed")
        connected = set(providers["connected"])
        models, routes = [], []
        configured = {p.get("id") for p in config.get("providers", []) if isinstance(p, dict)}
        for provider in providers["all"]:
            if not isinstance(provider, dict) or not token(provider.get("id")):
                continue
            pid = provider["id"]
            routes.append({"id": pid, "configured": pid in configured, "authentication": "credentials_reported" if pid in connected else "unknown"})
            entries = provider.get("models")
            for mid, model in (entries.items() if isinstance(entries, dict) else []):
                if not token(mid) or not isinstance(model, dict):
                    continue
                variants = model.get("variants")
                limit = model.get("limit") or {}
                context = limit.get("context")
                models.append({"id": pid + "/" + mid, "provider": pid,
                               "configured": pid in configured or pid in connected,
                               "efforts": [v for v in variants if token(v)] if isinstance(variants, dict) else None,
                               "context": context if isinstance(context, int) and context > 0 else None})
        return {"models": models, "providers": routes, "account_context": None,
                "native_context": opaque(config), "source": "opencode provider API", "quota": "unknown"}
    except (NativeError, OSError, ValueError, TypeError):
        pass
    # CLI discovery is a compatibility fallback. It reports configured IDs, not entitlements.
    native_env = dict(env, OPENCODE_DISABLE_EXTERNAL_PLUGINS="true")
    code, out = H.RUN([path, "models"], cwd, native_env)
    if code != 0:
        raise NativeError("native_method_rejected")
    ids = [line.strip() for line in out.splitlines() if token(line.strip()) and "/" in line]
    # Human-formatted auth output is not a reliable structured login contract.
    # Retain unknown instead of parsing incidental text or printing paths/account names.
    return {"models": [{"id": value, "provider": value.split("/", 1)[0]} for value in ids],
            "providers": [{"id": value, "authentication": "unknown"} for value in sorted({m.split("/", 1)[0] for m in ids})],
            "account_context": None, "native_context": opaque(ids), "source": "opencode models (compatibility)", "quota": "unknown"}


def discover(kind: str, cwd: str, env: Dict[str, str], profile: Optional[str] = None, refresh: bool = False,
             now: Optional[float] = None) -> Dict[str, Any]:
    clock = time.time() if now is None else now
    path, version = H.which(kind, env), H.version(kind, env)
    context = fingerprint(kind, cwd, env, path, version)
    key = opaque([context, profile])
    cached = _cache.get(key)
    if not refresh and cached and 0 <= clock - cached["checked_at"] < METADATA_TTL:
        return json.loads(json.dumps(cached))
    result: Dict[str, Any] = {"kind": kind, "profile": profile, "installed": bool(path), "executable": path,
                              "cwd": cwd,
                              "version": version, "checked_at": clock, "fresh_until": clock + METADATA_TTL,
                              "context": context, "source": "unknown", "models": [], "providers": [],
                              "authentication": "unknown", "quota": "unknown", "inference": "unverified",
                              "account_context": None, "native_context": None}
    try:
        if not path:
            raise NativeError("not_installed")
        if kind == "codex":
            result.update(_codex(path, cwd, env, profile))
        elif kind == "claude":
            result.update(_claude(path, version, cwd, env, profile))
        elif kind == "opencode":
            result.update(_opencode(path, cwd, env, profile))
        else:
            raise NativeError("metadata_protocol_unsupported")
    except (NativeError, OSError, ValueError, TypeError, AttributeError) as err:
        result["error"] = str(err) if isinstance(err, NativeError) else "native_metadata_failed"
        if cached:
            result["stale_catalog"] = {"checked_at": cached["checked_at"], "stale": True,
                                       "models": cached.get("models", []), "source": cached.get("source")}
    result["route_context"] = opaque([context, profile, result["native_context"], result["account_context"]])
    _cache[key] = json.loads(json.dumps(result))
    return result


def evidence_state(evidence: Dict[str, Any], inventory: Dict[str, Any], now: Optional[float] = None) -> str:
    clock = time.time() if now is None else now
    if evidence.get("route_context") != inventory.get("route_context") or not inventory.get("account_context"):
        return "context_changed"
    age = clock - float(evidence.get("checked_at") or 0)
    if age < 0 or age >= VERIFICATION_TTL:
        return "stale"
    return str(evidence.get("status") or "unverified")

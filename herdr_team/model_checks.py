"""Explicit single-model inference evidence; a catalog entry never implies access."""
from __future__ import annotations

import json
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from herdr_team import model_inventory as I, store
from herdr_team.native_transport import JsonLines, NativeError


def classify(error: Any) -> str:
    """Classify privately, exporting neither native output nor provider error text."""
    text = str(error).lower()
    if any(x in text for x in ("quota", "rate_limit", "rate limit", "429", "usage limit")):
        return "quota_exhausted"
    if any(x in text for x in ("authentication", "unauthorized", "401", "expired", "invalid_api_key")):
        return "authentication_failed"
    if any(x in text for x in ("denied", "not found", "403", "404", "not available", "not authorized")):
        return "model_denied"
    if "timeout" in text:
        return "timeout"
    return "request_failed"


def _claude(inventory: Dict[str, Any], model: str, effort: Optional[str], env: Dict[str, str]) -> Dict[str, Any]:
    if not I._version_at_least(inventory.get("version"), (2, 1, 299)) or inventory.get("profile"):
        return {"status": "verification_unsupported", "reason": "safe_no_tools_profile_check_unavailable"}
    original = Path(inventory.get("cwd") or "/")
    for directory in [original] + list(original.parents):
        if any((directory / ".claude" / name).exists() for name in ("settings.json", "settings.local.json")):
            # Moving to scratch must not silently change a project-scoped provider/account.
            return {"status": "verification_unsupported", "reason": "project_scoped_configuration_cannot_be_preserved_safely"}
        if (directory / ".git").exists():
            break
    if not inventory.get("native_account_context"):
        return {"status": "verification_unsupported", "reason": "native_account_context_unknown"}
    nonce = secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix="synapse-model-check-") as cwd:
        argv = I.claude_argv(inventory["executable"]) + ["--model", model, "--max-turns", "1"]
        if effort:
            argv += ["--effort", effort]
        observed = None
        native_env = dict(env, DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        with JsonLines(argv, cwd, native_env, timeout=60) as stream:
            stream.send({"type": "control_request", "request_id": "check-context", "request": {"subtype": "initialize", "hooks": {}}})
            for _ in range(100):
                initialization = stream.receive()
                response = initialization.get("response") or {}
                if initialization.get("type") != "control_response" or response.get("request_id") != "check-context":
                    continue
                account = (response.get("response") or {}).get("account") or {}
                if response.get("subtype") != "success" or I.opaque(account) != inventory["native_account_context"]:
                    return {"status": "verification_unsupported", "reason": "scratch_provider_or_account_differs"}
                break
            else:
                return {"status": "verification_unsupported", "reason": "native_initialization_unconfirmed"}
            stream.send({"type": "user", "message": {"role": "user", "content": "Reply with only this string: " + nonce}})
            for _ in range(1024):
                frame = stream.receive()
                if frame.get("type") == "assistant":
                    message = frame.get("message") or {}
                    observed = I.token(message.get("model")) or observed
                    if any(part.get("type") == "tool_use" for part in message.get("content", []) if isinstance(part, dict)):
                        return {"status": "request_failed", "reason": "tool_use_refused"}
                if frame.get("type") != "result":
                    continue
                if frame.get("subtype") != "success" or frame.get("is_error") is True:
                    return {"status": classify(frame.get("errors") or frame.get("result")), "observed_model": observed}
                if str(frame.get("result") or "").strip() != nonce:
                    return {"status": "request_failed", "reason": "response_mismatch", "observed_model": observed}
                usage = frame.get("usage") or {}
                counts = {k: v for k, v in usage.items() if k in ("input_tokens", "output_tokens") and isinstance(v, int) and v >= 0}
                return {"status": "verified" if observed else "model_unconfirmed", "observed_model": observed, "usage": counts}
    return {"status": "request_failed", "reason": "no_completed_response"}


def check(inventory: Dict[str, Any], model: str, effort: Optional[str], env: Dict[str, str], now: Optional[float] = None) -> Dict[str, Any]:
    """No fallback is requested: only a native observed response supplies verification."""
    clock = time.time() if now is None else now
    result: Dict[str, Any] = {"kind": inventory["kind"], "profile": inventory.get("profile"), "requested_model": model,
                              "effort": effort, "route_context": inventory.get("route_context"), "checked_at": clock,
                              "fresh_until": clock + I.VERIFICATION_TTL}
    try:
        if inventory["kind"] == "claude" and inventory.get("installed"):
            result.update(_claude(inventory, model, effort, env))
        else:
            # Do not substitute read-only sandboxing for disabling tools. It still permits shell execution.
            result.update(status="verification_unsupported", reason="native_no_tools_check_not_supported")
    except (NativeError, OSError, ValueError, TypeError, AttributeError) as err:
        result.update(status=classify(err))
    if result.get("status") == "verified" and result.get("observed_model") != model:
        entry = next((m for m in inventory.get("models", []) if m.get("id") == model), {})
        if entry.get("resolved_model") != result["observed_model"]:
            result["status"] = "fallback_response"
    return result


def evidence_key(kind: str, profile: Optional[str], model: str, effort: Optional[str]) -> str:
    return I.opaque([kind, profile, model, effort])


def save(session: Any, evidence: Dict[str, Any]) -> None:
    path = session.root / "model-checks.json"
    with store.FileLock(session.root / "model-checks.lock"):
        doc = store.read_json(path, default={})
        if not isinstance(doc, dict):
            doc = {}
        key = evidence_key(evidence["kind"], evidence.get("profile"), evidence["requested_model"], evidence.get("effort"))
        doc[key] = evidence
        store.write_json(path, doc)

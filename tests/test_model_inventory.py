"""Native protocol fixtures; never use an installed harness or spend tokens."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from support import TempState
from herdr_team import harnesses as H, model_checks as C, model_inventory as I
from herdr_team.native_transport import NativeError


class Rpc:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def rpc(self, method, params):
        self.calls.append((method, params))
        value = self.replies.get(method, {})
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    def send(self, message):
        self.calls.append(("send", message))
    def receive(self):
        return self.replies["frames"].pop(0)


class Inventory(unittest.TestCase):
    def setUp(self):
        I._cache.clear()
        patch = mock.patch.object(I, "metadata_command", return_value="[]")
        patch.start()
        self.addCleanup(patch.stop)

    def test_codex_is_paginated_metadata_only_and_exports_no_account_or_config(self):
        rpc = Rpc({"initialize": {}, "config/read": {"config": {"model_provider": "openai", "private": "secret-config"}},
                   "account/read": {"account": {"type": "chatgpt", "email": "private@example.invalid"}},
                   "model/list": [{"data": [{"model": "m1", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}], "nextCursor": "page2"},
                                  {"data": [{"model": "m2", "hidden": True}], "nextCursor": None}]})
        with mock.patch.object(I, "JsonLines", return_value=rpc):
            result = I._codex("codex", "/scratch", {}, "fast")
        self.assertEqual([m["id"] for m in result["models"]], ["m1", "m2"])
        self.assertEqual(result["models"][0]["efforts"], ["high"])
        self.assertTrue(result["models"][1]["hidden"])
        self.assertIn(("account/read", {"refreshToken": False}), rpc.calls)
        self.assertFalse(any(method in ("thread/start", "turn/start", "config/value/write") for method, _ in rpc.calls))
        self.assertNotIn("private@example", json.dumps(result))
        self.assertNotIn("secret-config", json.dumps(result))

    def test_pagination_loops_and_malformed_lists_are_unknown_not_complete(self):
        for pages in ([{"data": [], "nextCursor": "again"}, {"data": [], "nextCursor": "again"}], [{"data": None}]):
            rpc = Rpc({"config/read": {"config": {"model_provider": "openai"}}, "model/list": pages})
            with mock.patch.object(I, "JsonLines", return_value=rpc), self.assertRaises(NativeError):
                I._codex("codex", "/scratch", {}, None)

    def test_claude_initializes_without_a_user_prompt_or_activating_profile(self):
        rpc = Rpc({"frames": [{"type": "control_response", "response": {"request_id": "inventory", "subtype": "success", "response": {
            "models": [{"value": "sonnet", "resolvedModel": "resolved", "supportedEffortLevels": ["medium", "high"]}],
            "account": {"apiProvider": "firstParty", "tokenSource": "oauth", "email": "private@example.invalid"}}}}]})
        with mock.patch.object(I, "JsonLines", return_value=rpc) as factory:
            result = I._claude("claude", "2.1.299", "/scratch", {}, "researcher")
        argv = factory.call_args.args[0]
        self.assertNotIn("--agent", argv)
        self.assertIn("--tools", argv)
        self.assertIn('{"disableAllHooks":true}', argv)
        self.assertTrue(all(frame["type"] != "user" for method, frame in rpc.calls if method == "send"))
        self.assertEqual(result["models"][0]["resolved_model"], "resolved")
        self.assertNotIn("private@example", json.dumps(result))

    def test_old_claude_protocol_is_not_guessed(self):
        with mock.patch.object(I, "JsonLines") as process:
            with self.assertRaises(NativeError):
                I._claude("claude", "2.1.1", "/scratch", {}, None)
            process.assert_not_called()

    def test_opencode_catalog_and_connected_providers_stay_separate(self):
        server = Rpc({})
        server.request = mock.Mock(side_effect=[{"all": [{"id": "p", "models": {"model": {"limit": {"context": 1000}, "variants": {"high": {}}}}},
                                                         {"id": "q", "models": {"catalog-only": {}}}], "connected": ["p"]},
                                               {"providers": [{"id": "p"}], "default": {"p": "model"}}])
        with mock.patch.object(I, "OpenCodeServer", return_value=server):
            result = I._opencode("opencode", "/scratch", {}, None)
        self.assertEqual([p["authentication"] for p in result["providers"]], ["credentials_reported", "unknown"])
        self.assertEqual(result["models"][0]["efforts"], ["high"])
        self.assertFalse(result["models"][1]["configured"])
        self.assertIsNone(result["account_context"])
        self.assertEqual([c.args[0] for c in server.request.call_args_list], ["/provider", "/config/providers"])

    def test_context_change_and_freshness_invalidate_cache_without_inference(self):
        with TempState() as ts, mock.patch.object(H, "which", return_value="/fake/codex"), mock.patch.object(H, "version", return_value="0.162.1"), mock.patch.object(I, "_codex", return_value={"source": "fixture"}) as native:
            first = I.discover("codex", str(ts.home), ts.env, now=100)
            I.discover("codex", str(ts.home), ts.env, now=399)
            self.assertEqual(native.call_count, 1)
            I.discover("codex", str(ts.home), ts.env, now=400)
            self.assertEqual(native.call_count, 2)
            changed = I.discover("codex", str(ts.home), dict(ts.env, OPENAI_BASE_URL="changed"), now=401)
            self.assertEqual(native.call_count, 3)
            self.assertNotEqual(first["context"], changed["context"])
            self.assertEqual(first["inference"], "unverified")

    def test_configuration_edits_change_context_and_secrets_are_only_hashed(self):
        with TempState() as ts:
            directory = ts.home / ".codex"
            directory.mkdir()
            config = directory / "config.toml"
            config.write_text('model_provider="one"')
            first = I.fingerprint("codex", str(ts.home), ts.env, None, "1")
            config.write_text('model_provider="two"')
            self.assertNotEqual(first, I.fingerprint("codex", str(ts.home), ts.env, None, "1"))
            self.assertNotIn("one", first)

    def test_verification_unknown_account_stale_or_changed_is_not_reused(self):
        evidence = {"status": "verified", "route_context": "a", "checked_at": 100}
        inv = {"route_context": "a", "account_context": "opaque"}
        self.assertEqual(I.evidence_state(evidence, inv, now=101), "verified")
        self.assertEqual(I.evidence_state(evidence, inv, now=1900), "stale")
        self.assertEqual(I.evidence_state(evidence, dict(inv, account_context=None), now=101), "context_changed")
        self.assertEqual(I.evidence_state(evidence, dict(inv, route_context="b"), now=101), "context_changed")

    def test_agent_thread_identity_and_path_aliases_do_not_change_provider_context(self):
        with TempState() as ts:
            before = I.fingerprint("codex", str(ts.home), ts.env, None, "1")
            moved = dict(ts.env, CODEX_THREAD_ID="new-thread", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="other", PATH="/temporary/aliases")
            self.assertEqual(before, I.fingerprint("codex", str(ts.home), moved, None, "1"))

    def test_native_messaging_token_is_process_local_not_provider_access(self):
        with TempState() as ts:
            for kind in I.KINDS:
                before = I.fingerprint(kind, str(ts.home), ts.env, None, "1")
                for value in ("first-process", "next-process"):
                    with self.subTest(kind=kind, value=value):
                        native_tool = dict(ts.env, CLAUDE_CODE_MESSAGING_TOKEN=value)
                        self.assertEqual(before, I.fingerprint(kind, str(ts.home), native_tool, None, "1"))

    def test_messaging_exception_does_not_weaken_credentials_or_routing(self):
        with TempState() as ts:
            before = I.fingerprint("claude", str(ts.home), ts.env, None, "1")
            for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CUSTOM_PROVIDER_TOKEN"):
                with self.subTest(key=key):
                    changed = dict(ts.env, CLAUDE_CODE_MESSAGING_TOKEN="local-process", **{key: "changed-provider-context"})
                    self.assertNotEqual(before, I.fingerprint("claude", str(ts.home), changed, None, "1"))

    def test_v2_opencode_uses_only_private_metadata_commands_and_filters_catalog_only_providers(self):
        auth = [{"id": "p", "connections": [{"id": "opaque", "label": "private account"}]}]
        with mock.patch.object(H, "RUN", return_value=(0, "--standalone")), mock.patch.object(I, "metadata_command", side_effect=["p/m1\nq/catalog-only\n", json.dumps(auth)]) as run:
            result = I._opencode("opencode", "/project", {}, None)
        self.assertTrue(result["models"][0]["configured"])
        self.assertFalse(result["models"][1]["configured"])
        self.assertNotIn("private account", json.dumps(result))
        self.assertTrue(all("--standalone" in call.args[0] for call in run.call_args_list))

    def test_failed_refresh_retains_only_labeled_stale_catalog(self):
        with TempState() as ts, mock.patch.object(H, "which", return_value="/fake"), mock.patch.object(H, "version", return_value="1"), mock.patch.object(I, "_codex", side_effect=[{"models": [{"id": "m"}], "source": "fixture"}, NativeError("native_timeout")]):
            I.discover("codex", str(ts.home), ts.env, now=100)
            result = I.discover("codex", str(ts.home), ts.env, refresh=True, now=401)
        self.assertEqual(result["models"], [])
        self.assertTrue(result["stale_catalog"]["stale"])
        self.assertEqual(result["stale_catalog"]["models"], [{"id": "m"}])

    def test_key_shaped_identifiers_are_not_exported(self):
        fake = "gh" + "p_" + "a" * 40
        self.assertIsNone(I.token(fake))


class Checks(unittest.TestCase):
    def inventory(self):
        return {"kind": "claude", "installed": True, "version": "2.1.299", "executable": "claude",
                "native_account_context": I.opaque({"apiProvider": "firstParty"})}

    def initialization(self):
        return {"type": "control_response", "response": {"request_id": "check-context", "subtype": "success", "response": {"account": {"apiProvider": "firstParty"}}}}

    def test_completed_nonce_response_is_required_not_exit_or_model_claim(self):
        for response, status in (("nonce", "verified"), ("I am model-a", "request_failed")):
            rpc = Rpc({"frames": [self.initialization(), {"type": "assistant", "message": {"model": "model-a", "content": []}},
                                  {"type": "result", "subtype": "success", "result": response}]})
            with mock.patch.object(C, "JsonLines", return_value=rpc), mock.patch.object(C.secrets, "token_hex", return_value="nonce"):
                result = C.check(self.inventory(), "model-a", None, {})
            self.assertEqual(result["status"], status)

    def test_implicit_fallback_or_missing_model_is_not_requested_model_verification(self):
        for observed, status in (("other", "fallback_response"), (None, "model_unconfirmed")):
            rpc = Rpc({"frames": [self.initialization(), {"type": "assistant", "message": {"model": observed, "content": []}},
                                  {"type": "result", "subtype": "success", "result": "nonce"}]})
            with mock.patch.object(C, "JsonLines", return_value=rpc), mock.patch.object(C.secrets, "token_hex", return_value="nonce"):
                result = C.check(self.inventory(), "model-a", None, {})
            self.assertEqual(result["status"], status)

    def test_unproven_no_tools_adapters_refuse_without_launching(self):
        with mock.patch.object(C, "JsonLines") as process:
            for kind in ("codex", "opencode"):
                self.assertEqual(C.check({"kind": kind}, "model", None, {})["status"], "verification_unsupported")
            process.assert_not_called()

    def test_quota_auth_and_denial_classifications_do_not_export_provider_output(self):
        for message, expected in (("429 rate_limit with private detail", "quota_exhausted"), ("401 expired", "authentication_failed"), ("403 denied", "model_denied"), ("native_timeout", "timeout")):
            self.assertEqual(C.classify(message), expected)

    def test_project_scoped_provider_is_not_silently_replaced_by_scratch_defaults(self):
        with TempState() as ts:
            config = ts.home / ".claude"
            config.mkdir()
            (config / "settings.json").write_text("{}")
            inv = dict(self.inventory(), cwd=str(ts.home))
            with mock.patch.object(C, "JsonLines") as process:
                self.assertEqual(C.check(inv, "model", None, {})["status"], "verification_unsupported")
                process.assert_not_called()

    def test_different_scratch_account_sends_no_generation(self):
        initialization = self.initialization()
        initialization["response"]["response"]["account"] = {"apiProvider": "bedrock"}
        rpc = Rpc({"frames": [initialization]})
        with mock.patch.object(C, "JsonLines", return_value=rpc):
            result = C.check(self.inventory(), "model", None, {})
        self.assertEqual(result["status"], "verification_unsupported")
        self.assertFalse(any(frame.get("type") == "user" for kind, frame in rpc.calls))


if __name__ == "__main__":
    unittest.main()

"""Usage limits across agents: provider parsers, kind mapping, the report, its rendering, and the popup lines."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from herdr_team import cmd_usage, usage
from support import FakeApi

NOW = datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc)

# Shapes captured live on 2026-09-06 (values changed, ids scrubbed).
ANTHROPIC_BODY = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-09-06T04:30:00.280455+00:00"},
    "seven_day": {"utilization": 51.0, "resets_at": "2026-09-08T13:00:00.280472+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "group": "session", "percent": 12, "severity": "normal", "resets_at": "2026-09-06T04:30:00.280455+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 51, "severity": "normal", "resets_at": "2026-09-08T13:00:00.280472+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 90, "severity": "warning", "resets_at": "2026-09-08T13:00:00.280618+00:00", "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": True},
    ],
    "spend": {"used": {"amount_minor": 1643, "currency": "USD", "exponent": 2}, "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2}, "percent": 16, "severity": "normal", "enabled": False, "disabled_reason": "out_of_credits"},
}
CODEX_BODY = {
    "plan_type": "pro",
    "rate_limit": {"allowed": True, "limit_reached": False, "primary_window": {"used_percent": 15, "limit_window_seconds": 604800, "reset_after_seconds": 575252, "reset_at": 1789220279}, "secondary_window": None},
    "additional_rate_limits": [{"limit_name": "GPT-5.3-Codex-Spark", "rate_limit": {"primary_window": {"used_percent": 0, "limit_window_seconds": 18000, "reset_at": 1788663027}, "secondary_window": {"used_percent": 3, "limit_window_seconds": 604800, "reset_at": 1789249827}}}],
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
}
COPILOT_NO_QUOTA = {"login": "x", "copilot_plan": "individual", "quota_snapshots": None, "quota_reset_date": None}
COPILOT_QUOTA = {"copilot_plan": "business", "quota_reset_date": "2026-10-01", "quota_snapshots": {"premium_interactions": {"unlimited": False, "remaining": 220, "entitlement": 300, "percent_remaining": 73.3}, "chat": {"unlimited": True}, "completions": {"unlimited": True}}}


class ParserTests(unittest.TestCase):
    def test_anthropic_limits_list_wins_and_spend_is_a_window(self):
        windows, _note = usage.parse_anthropic(ANTHROPIC_BODY)
        self.assertEqual([w["label"] for w in windows], ["Current session", "Current week (all models)", "Current week (Fable)", "Extra usage (credits)"])
        self.assertEqual([w["percent"] for w in windows], [12.0, 51.0, 90.0, 16.0])
        self.assertEqual(windows[2]["severity"], "warning")  # the server's word wins over the percent threshold
        self.assertEqual(windows[2]["scope"], "Fable")
        self.assertEqual(windows[3]["detail"], "$16.43 of $100.00; off (out of credits)")
        self.assertTrue(windows[0]["resets_at"].startswith("2026-09-06T04:30"))

    def test_anthropic_legacy_fields_when_limits_is_missing(self):
        body = {"five_hour": {"utilization": 9.0, "resets_at": "2026-09-06T01:30:00+00:00"}, "seven_day": {"utilization": 51.0, "resets_at": None}, "seven_day_opus": {"utilization": 20.0, "resets_at": None}}
        windows, _ = usage.parse_anthropic(body)
        self.assertEqual([(w["id"], w["percent"]) for w in windows], [("five_hour", 9.0), ("seven_day", 51.0), ("seven_day_opus", 20.0)])
        self.assertEqual(usage.parse_anthropic({}), ([], None))

    def test_anthropic_plan_label(self):
        self.assertEqual(usage.anthropic_plan({"subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}), "Max 20x")
        self.assertEqual(usage.anthropic_plan({"subscriptionType": "pro"}), "Pro")
        self.assertIsNone(usage.anthropic_plan({}))

    def test_codex_windows_plan_and_extra_limits(self):
        windows, plan, note = usage.parse_codex(CODEX_BODY)
        self.assertEqual(plan, "Pro")
        self.assertIsNone(note)
        self.assertEqual([w["label"] for w in windows], ["Current week", "GPT-5.3-Codex-Spark: session (5h)", "GPT-5.3-Codex-Spark: week"])
        self.assertEqual(windows[0]["percent"], 15.0)
        self.assertEqual(windows[0]["resets_at"], "2026-09-12T13:37:59Z")
        self.assertEqual(windows[2]["percent"], 3.0)
        self.assertEqual(usage.parse_codex({"rate_limit": {"limit_reached": True}})[2], "limit reached")

    def test_codex_rollout_fallback_reads_the_newest_rate_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            day = home / "sessions" / "2026" / "09" / "05"
            day.mkdir(parents=True)
            old = day / "rollout-2026-09-05T10-00-00-a.jsonl"
            old.write_text(json.dumps({"timestamp": "2026-09-05T10:05:00.000Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": 1789220279}, "secondary": None, "plan_type": "pro"}}}) + "\n")
            new = day / "rollout-2026-09-05T20-00-00-b.jsonl"
            new.write_text("\n".join([
                json.dumps({"timestamp": "2026-09-05T20:01:00.000Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 14.0, "window_minutes": 10080, "resets_at": 1789220279}, "secondary": {"used_percent": 2.0, "window_minutes": 300, "resets_at": 1788663027}}}}),
                "not json",
                json.dumps({"timestamp": "2026-09-05T20:47:05.313Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 15.0, "window_minutes": 10080, "resets_at": 1789220279}, "secondary": None, "plan_type": "pro"}}}),
            ]) + "\n")
            os.utime(old, (1, 1))
            found = usage.codex_rollout_rate_limits(home)
            self.assertIsNotNone(found)
            limits, stamp = found
            self.assertEqual((limits["primary"]["used_percent"], stamp), (15.0, "2026-09-05T20:47:05.313Z"))
            windows, plan = usage.parse_codex_rollout(limits)
            self.assertEqual([(w["label"], w["percent"]) for w in windows], [("Current week", 15.0)])
            self.assertEqual(plan, "Pro")
            self.assertIsNone(usage.codex_rollout_rate_limits(home / "nowhere"))

    def test_codex_window_labels(self):
        self.assertEqual(usage._codex_window_label(18000), ("Current session (5h)", "session"))
        self.assertEqual(usage._codex_window_label(604800), ("Current week", "weekly"))
        self.assertEqual(usage._codex_window_label(None, 10080), ("Current week", "weekly"))
        self.assertEqual(usage._codex_window_label(30 * 86400), ("30-day window", "window_30d"))
        self.assertEqual(usage._codex_window_label(None), ("Limit", "limit"))

    def test_copilot_with_and_without_quota(self):
        windows, plan, note = usage.parse_copilot(COPILOT_NO_QUOTA)
        self.assertEqual((windows, plan), ([], "Individual"))
        self.assertIn("no quota", note)
        windows, plan, note = usage.parse_copilot(COPILOT_QUOTA)
        self.assertEqual(plan, "Business")
        self.assertIsNone(note)
        self.assertEqual(windows[0]["label"], "Premium requests")
        self.assertAlmostEqual(windows[0]["percent"], 26.7, places=1)
        self.assertEqual(windows[0]["detail"], "220 of 300 left")
        self.assertEqual(windows[0]["resets_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(windows[1]["detail"], "unlimited")

    def test_gemini_buckets_best_effort(self):
        windows = usage.parse_gemini({"buckets": [{"modelId": "gemini-2.5-pro", "remainingFraction": 0.4, "remainingAmount": 400, "resetTime": "2026-09-07T07:00:00Z"}]})
        self.assertEqual(len(windows), 1)
        self.assertAlmostEqual(windows[0]["percent"], 60.0)
        self.assertEqual(windows[0]["detail"], "400 left")
        self.assertEqual(usage.parse_gemini({"something": 1}), [])


class SourceTests(unittest.TestCase):
    def test_claude_credentials_file_then_keychain(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "claude"
            cfg.mkdir()
            env = {"HOME": tmp, "CLAUDE_CONFIG_DIR": str(cfg)}
            self.assertIsNone(usage.claude_credentials(env, run=lambda argv, t: None))
            (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok", "subscriptionType": "max"}}))
            self.assertEqual(usage.claude_credentials(env, run=lambda argv, t: None)["subscriptionType"], "max")
            (cfg / ".credentials.json").write_text("{}")
            calls = []

            def fake_run(argv, timeout):
                calls.append(argv)
                return json.dumps({"claudeAiOauth": {"accessToken": "kc", "subscriptionType": "pro"}})

            found = usage.claude_credentials(env, run=fake_run)
            if usage.sys.platform == "darwin":
                self.assertEqual((found["accessToken"], calls[0][:2]), ("kc", ["security", "find-generic-password"]))
            else:
                self.assertIsNone(found)

    def test_anthropic_source_never_leaks_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "claude"
            cfg.mkdir()
            (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "SECRET-TOKEN", "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x", "expiresAt": (time.time() + 3600) * 1000}}))
            env = {"HOME": tmp, "CLAUDE_CONFIG_DIR": str(cfg)}
            seen = {}

            def fake_http(url, headers, timeout, body=None):
                seen["url"] = url
                seen["auth"] = headers.get("Authorization")
                seen["beta"] = headers.get("anthropic-beta")
                return ANTHROPIC_BODY

            row = usage.anthropic_source(env, fetch=True, http=fake_http, run=lambda a, t: None)
            self.assertEqual((seen["url"], seen["auth"], seen["beta"]), (usage.ANTHROPIC_USAGE_URL, "Bearer SECRET-TOKEN", usage.ANTHROPIC_BETA))
            self.assertTrue(row["ok"])
            self.assertEqual(row["plan"], "Max 5x")
            self.assertEqual(len(row["windows"]), 4)
            self.assertNotIn("SECRET-TOKEN", json.dumps(row))
            # an HTTP failure is a row error, not an exception, and still carries no token

            def failing(url, headers, timeout, body=None):
                raise usage.UsageError("HTTP 401: login expired or not authorized; log in again with the agent's own CLI")

            row = usage.anthropic_source(env, fetch=True, http=failing, run=lambda a, t: None)
            self.assertFalse(row["ok"])
            self.assertIn("HTTP 401", row["error"])
            self.assertNotIn("SECRET-TOKEN", json.dumps(row))
            row = usage.anthropic_source(env, fetch=False, http=failing, run=lambda a, t: None)
            self.assertEqual(row["error"], "not fetched (--no-fetch)")
            self.assertEqual(usage.anthropic_source({"HOME": tmp, "CLAUDE_CONFIG_DIR": str(Path(tmp) / "none")}, run=lambda a, t: None)["ok"], False)

    def test_codex_source_uses_the_log_when_the_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            (home / "sessions" / "2026" / "09" / "05").mkdir(parents=True)
            (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "CODEX-SECRET", "account_id": "acct"}}))
            (home / "sessions" / "2026" / "09" / "05" / "rollout-x.jsonl").write_text(json.dumps({"timestamp": "2026-09-05T20:47:05.313Z", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 15.0, "window_minutes": 10080, "resets_at": 1789220279}, "plan_type": "pro"}}}) + "\n")
            env = {"HOME": tmp, "CODEX_HOME": str(home)}
            headers_seen = {}

            def ok(url, headers, timeout, body=None):
                headers_seen.update(headers)
                return CODEX_BODY

            row = usage.codex_source(env, http=ok)
            self.assertEqual((row["ok"], row["plan"], headers_seen.get("ChatGPT-Account-Id"), len(row["windows"])), (True, "Pro", "acct", 3))
            self.assertEqual(headers_seen.get("Authorization"), "Bearer CODEX-SECRET")
            self.assertNotIn("CODEX-SECRET", json.dumps(row))
            self.assertEqual(row["source"], "chatgpt.com/backend-api/wham/usage")

            def down(url, headers, timeout, body=None):
                raise usage.UsageError("network: unreachable")

            row = usage.codex_source(env, http=down)
            self.assertTrue(row["ok"])
            self.assertIn("live fetch failed", row["note"])
            self.assertEqual(row["as_of"], "2026-09-05T20:47:05.313Z")
            self.assertTrue(row["source"].startswith("session log"))
            row = usage.codex_source(env, fetch=False, http=down)
            self.assertEqual((row["ok"], row["note"]), (True, "showing the last values Codex logged"))
            (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "k"}))
            row = usage.codex_source(env, http=ok)
            self.assertEqual(row["login"], "API key")
            self.assertTrue(row["ok"])  # the log still answers
            self.assertNotIn("CODEX-SECRET", json.dumps(row))

    def test_copilot_and_gemini_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            row = usage.copilot_source(env, http=lambda *a, **k: COPILOT_NO_QUOTA, run=lambda argv, t: "")
            self.assertIn("no GitHub token", row["error"])
            row = usage.copilot_source(dict(env, GH_TOKEN="gh-secret"), http=lambda *a, **k: COPILOT_NO_QUOTA, run=lambda argv, t: None)
            self.assertEqual((row["ok"], row["plan"], row["windows"]), (True, "Individual", []))
            self.assertNotIn("gh-secret", json.dumps(row))
            row = usage.gemini_source(env, http=lambda *a, **k: {})
            self.assertIn("no Gemini CLI login", row["error"])
            (Path(tmp) / ".gemini").mkdir()
            (Path(tmp) / ".gemini" / "oauth_creds.json").write_text(json.dumps({"access_token": "g", "expiry_date": 1000}))
            row = usage.gemini_source(env, http=lambda *a, **k: {})
            self.assertIn("expired", row["error"])
            (Path(tmp) / ".gemini" / "oauth_creds.json").write_text(json.dumps({"access_token": "g", "expiry_date": (time.time() + 3600) * 1000}))
            row = usage.gemini_source(env, http=lambda *a, **k: {"buckets": [{"modelId": "m", "remainingFraction": 0.5}]})
            self.assertEqual((row["ok"], row["windows"][0]["percent"]), (True, 50.0))
            row = usage.gemini_source(env, http=lambda *a, **k: {"weird": 1})
            self.assertEqual(row["error"], "quota response not understood")

    def test_login_dependent_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            self.assertEqual(usage.provider_for_kind("claude", env), ("anthropic", None))
            self.assertEqual(usage.provider_for_kind("cursor", env), (None, None))
            self.assertEqual(usage.provider_for_kind("pi", env), (None, "no login file"))
            pi = Path(tmp) / ".pi" / "agent"
            pi.mkdir(parents=True)
            (pi / "auth.json").write_text(json.dumps({"anthropic": {"type": "oauth", "access": "x"}}))
            self.assertEqual(usage.provider_for_kind("pi", env), ("anthropic", "Anthropic login"))
            (pi / "auth.json").write_text(json.dumps({"openai-codex": {"type": "oauth"}}))
            self.assertEqual(usage.provider_for_kind("pi", env), ("openai-codex", "ChatGPT login"))
            oc = Path(tmp) / ".local" / "share" / "opencode"
            oc.mkdir(parents=True)
            (oc / "auth.json").write_text(json.dumps({"opencode": {"type": "api", "key": "k"}, "opencode-go": {"type": "api", "key": "k"}}))
            self.assertEqual(usage.provider_for_kind("opencode", env), ("opencode-zen", "OpenCode Zen API key"))
            (oc / "auth.json").write_text(json.dumps({"openrouter": {"type": "api", "key": "k"}}))
            self.assertEqual(usage.provider_for_kind("opencode", env), (None, "API keys: openrouter"))


class ReportTests(unittest.TestCase):
    AGENTS = [
        {"agent": "claude", "name": "red-claude", "pane_id": "w1:p1", "agent_status": "idle"},
        {"agent": "claude", "name": None, "pane_id": "w2:p3", "agent_status": "working"},
        {"agent": "codex", "name": "red-codex", "pane_id": "w1:p2", "agent_status": "idle"},
        {"agent": "cursor", "name": None, "pane_id": "w3:p1", "agent_status": "idle"},
        {"agent": "opencode", "name": "oc", "pane_id": "w1:p9", "agent_status": "done"},
        {"not": "an agent"},
    ]

    def fake_sources(self):
        def anthropic(env, fetch=True, timeout=8.0):
            return usage._provider("anthropic", ok=True, plan="Max 20x", windows=[usage.window("Current session", 12, "2026-09-06T04:30:00Z", wid="session"), usage.window("Current week (all models)", 91, "2026-09-08T13:00:00Z", wid="weekly_all"), usage.window("Current week (Fable)", 80, "2026-09-08T13:00:00Z", wid="weekly_fable", scope="Fable")], fetched_at="2026-09-06T01:00:00Z", as_of="2026-09-06T01:00:00Z", source="api.anthropic.com/api/oauth/usage")

        def codex(env, fetch=True, timeout=8.0):
            raise RuntimeError("boom")

        return {"anthropic": anthropic, "openai-codex": codex}

    def test_collect_groups_agents_by_provider_and_survives_a_broken_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            oc = Path(tmp) / ".local" / "share" / "opencode"
            oc.mkdir(parents=True)
            (oc / "auth.json").write_text(json.dumps({"opencode": {"type": "api", "key": "k"}}))
            report = usage.collect({"HOME": tmp}, self.AGENTS, sources=self.fake_sources(), include_logins=False, agents_error=None)
        self.assertEqual(report["agents"], 5)
        ids = [p["id"] for p in report["providers"]]
        self.assertEqual(ids, ["anthropic", "openai-codex", "opencode-zen"])
        anthropic = report["providers"][0]
        self.assertEqual((anthropic["kinds"], [a["pane_id"] for a in anthropic["agents"]]), (["claude"], ["w1:p1", "w2:p3"]))
        codex = report["providers"][1]
        self.assertEqual((codex["ok"], codex["error"], codex["kinds"]), (False, "source failed: RuntimeError", ["codex"]))
        zen = report["providers"][2]
        self.assertEqual((zen["ok"], zen["login"], zen["windows"]), (True, "OpenCode Zen API key", []))
        self.assertIn("no limit window", zen["note"])
        self.assertEqual([(u["kind"], u["pane_id"]) for u in report["untracked"]], [("cursor", "w3:p1")])

    def test_collect_adds_providers_with_a_login_but_no_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".claude").mkdir()
            report = usage.collect({"HOME": tmp}, [], sources=self.fake_sources(), include_logins=True)
            self.assertEqual([p["id"] for p in report["providers"]], ["anthropic"])
            self.assertEqual(report["providers"][0]["agents"], [])
            report = usage.collect({"HOME": tmp}, [], sources=self.fake_sources(), include_logins=False)
            self.assertEqual(report["providers"], [])

    def test_format_report_styles_bars_and_resets(self):
        report = usage.collect({"HOME": "/nonexistent"}, self.AGENTS[:3], sources=self.fake_sources(), include_logins=False, agents_error=None)
        rows = usage.format_report(report, width=110, now=NOW)
        text = "\n".join(t for t, _ in rows)
        self.assertTrue(rows[0][0].startswith("Usage limits · 3 agents · 2 providers"))
        self.assertIn("Anthropic · Max 20x   claude ×2: red-claude w1:p1, claude w2:p3", text)
        self.assertIn("12% used   resets in 3h 30m (", text)
        self.assertIn("91% used ‼   resets", text)
        self.assertIn("80% used ⚠", text)
        styles = {label: next(s for t, s in rows if t.strip().startswith(label)) for label in ("Current session", "Current week (all models)", "Current week (Fable)")}
        self.assertEqual(styles["Current session"], "normal")
        self.assertEqual(styles["Current week (all models)"], "crit")
        self.assertEqual(styles["Current week (Fable)"], "warn")
        self.assertIn(("  source failed: RuntimeError", "error"), rows)
        ascii_text = usage.format_text(report, width=80, ascii_only=True)
        self.assertIn("#", ascii_text)
        self.assertNotIn("█", ascii_text)
        self.assertIn("!!", ascii_text)
        for width in (40, 64, 90):
            for line, _style in usage.format_report(report, width=width, now=NOW):
                self.assertLessEqual(len(line), max(width, 120))

    def test_reset_and_bar_helpers(self):
        self.assertEqual(usage.reset_label("2026-09-06T01:30:00Z", NOW), "resets in 30m ({})".format(usage.parse_iso("2026-09-06T01:30:00Z").astimezone().strftime("%H:%M")))
        self.assertEqual(usage.reset_label("2026-09-06T00:30:00Z", NOW), "resets now")
        self.assertTrue(usage.reset_label("2026-09-08T13:00:00Z", NOW).startswith("resets "))
        self.assertEqual(usage.reset_label(None, NOW), "")
        self.assertEqual(usage.bar(50, 10), "█████░░░░░")
        self.assertEqual(usage.bar(None, 4, ascii_only=True), "----")
        self.assertEqual(usage.bar(100, 4), "████")
        self.assertEqual((usage.severity_for(74.9), usage.severity_for(75), usage.severity_for(90), usage.severity_for(None)), ("normal", "warning", "critical", None))
        self.assertEqual(usage._epoch_iso(1789220279), "2026-09-12T13:37:59Z")
        self.assertIsNone(usage._epoch_iso("x"))

    def test_agents_label_truncates_long_lists(self):
        row = usage._provider("anthropic", kinds=["claude"], agents=[{"name": "member-{}".format(i), "pane_id": "w1:p{}".format(i), "kind": "claude"} for i in range(8)])
        label = usage.agents_label(row, 60)
        self.assertTrue(label.startswith("claude ×8: member-0 w1:p0"))
        self.assertIn("(+", label)
        self.assertLessEqual(len(label), 70)
        self.assertEqual(usage.agents_label(usage._provider("copilot", login="gh login"), 60), "no agent in this session (gh login found)")


class CommandTests(unittest.TestCase):
    def args(self, api, **extra):
        ns = argparse.Namespace(env={"HOME": "/nonexistent", "HERDR_SOCKET_PATH": "/nonexistent/herdr.sock"}, json=True, team=None, session=None, socket="/nonexistent/herdr.sock", session_mismatch_ok=False, no_fetch=True, timeout=1.0, ascii=False, width=None, api=api, stdout=io.StringIO(), stderr=io.StringIO())
        for key, value in extra.items():
            setattr(ns, key, value)
        return ns

    def test_usage_command_lists_agents_and_reports_an_unreachable_server(self):
        api = FakeApi()
        api.set_response("agent.list", {"type": "agent_list", "agents": [{"agent": "claude", "name": "a", "pane_id": "w1:p1", "agent_status": "idle"}, {"agent": "grok", "pane_id": "w1:p2", "agent_status": "idle"}]})
        original = usage.SOURCES
        usage.SOURCES = {"anthropic": lambda env, fetch=True, timeout=8.0: usage._provider("anthropic", ok=True, windows=[usage.window("Current session", 5, None)])}
        try:
            args = self.args(api)
            rc = cmd_usage._run_usage(args)
            self.assertEqual(rc, 0)
            payload = json.loads(args.stdout.getvalue())
            self.assertEqual(payload["agents"], 2)
            self.assertEqual([p["id"] for p in payload["providers"]], ["anthropic"])
            self.assertEqual([u["kind"] for u in payload["untracked"]], ["grok"])
            self.assertIsNone(payload["agents_error"])
            api.set_error("agent.list", "server_not_running", "no server at the socket")
            args = self.args(api, json=False, width=100)
            rc = cmd_usage._run_usage(args)
            self.assertEqual(rc, 0)
            text = args.stdout.getvalue()
            self.assertIn("agents not listed: no server at the socket", text)
            self.assertIn("Usage limits · 0 agents", text)
        finally:
            usage.SOURCES = original

    def test_pane_lines_scroll_and_status(self):
        state = cmd_usage.PaneState()
        state.refreshing = True
        lines, styles = cmd_usage.pane_lines(state, 80, 6, False, NOW)
        self.assertEqual(len(lines), 6)
        self.assertIn("fetching usage", lines[2])
        state.refreshing = False
        state.report = usage.collect({"HOME": "/nonexistent"}, [{"agent": "claude", "pane_id": "w1:p{}".format(i), "agent_status": "idle"} for i in range(3)], sources={"anthropic": lambda env, fetch=True, timeout=8.0: usage._provider("anthropic", ok=True, windows=[usage.window("W{}".format(i), i * 10, None) for i in range(10)])}, include_logins=False)
        lines, styles = cmd_usage.pane_lines(state, 80, 6, False, NOW)
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].startswith("Usage limits"))
        self.assertTrue(lines[-1].startswith("rows 1-5 of"))
        state.scroll = 100
        lines, _ = cmd_usage.pane_lines(state, 80, 6, False, NOW)
        self.assertLess(state.scroll, 100)  # clamped to the last page
        self.assertTrue(lines[-1].endswith("of {}".format(len(usage.format_report(state.report, width=80, now=NOW, hints=cmd_usage.PANE_HINTS)))))
        state.error = "boom"
        lines, styles = cmd_usage.pane_lines(state, 80, 40, False, NOW)
        self.assertIn(("refresh failed: boom", "error"), list(zip(lines, styles)))

    def test_pane_refuses_outside_the_popup(self):
        from herdr_team.errors import HerdrTeamError

        args = self.args(FakeApi(), force=False)
        with self.assertRaises(HerdrTeamError) as ctx:
            cmd_usage._run_pane(args)
        self.assertEqual(ctx.exception.code, "not_a_plugin_pane")


if __name__ == "__main__":
    unittest.main()

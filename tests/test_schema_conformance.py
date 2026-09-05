"""Schema conformance: the plugin's requests and the test fakes' responses against Herdr 0.8.2.

``tests/fixtures/herdr-api-0.8.2.schema.json`` is the verbatim output of
``herdr api schema --json`` from the installed 0.8.2 binary (protocol 20).
It has five sections: ``request`` (a ``oneOf`` over every method, each
with a ``params`` ``$ref``), ``success_response`` (``{"id","result"}``
with ``ResponseResult`` a ``oneOf`` over ``type``-tagged results),
``error_response``, ``event`` (the pushed envelope for resource
subscriptions, ``{"event": "<kind>", "data": {"type": "<kind>", ...}}``)
and ``subscription_event`` (the three pane-scoped subscriptions).

The schema does not say which result ``type`` a method answers with;
``METHOD_RESULT_TYPES`` below records that from the upstream handlers
(``src/app/api/*.rs`` at v0.8.2) and the docs.

The checker is a minimal stdlib JSON-schema subset: ``type`` (single or
list), ``required``, ``properties``, ``additionalProperties``,
``propertyNames.pattern``, ``maxProperties``, ``items``, ``enum``,
``const``, ``minimum``/``maximum``, ``pattern``, ``oneOf``/``anyOf``/
``allOf``, boolean schemas, and ``$ref`` within the document.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import api as _api
from herdr_team import cmd_misc, cmd_roster, cmd_ui, daemon, hooks, roster
from herdr_team.errors import HerdrTeamError
from support import (
    FAKE_AGENTS,
    FAKE_PANES,
    FakeApi,
    FakeError,
    canned_responses,
    default_responses,
    fake_explain,
    fake_plugin_pane_opened,
    fake_process_info,
    fake_read,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "herdr-api-0.8.2.schema.json"
REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_FILES = (
    "docs/next/website/src/content/docs/socket-api.mdx",
    "docs/next/website/src/content/docs/agent-automation.mdx",
    "docs/next/website/src/content/docs/plugins.mdx",
)


# --------------------------------------------------------------------------
# minimal JSON-schema checker


class SchemaChecker:
    """Validate JSON values against a subset of draft 2020-12 with in-document ``$ref``."""

    TYPE_NAMES = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }

    def __init__(self, document: Dict[str, Any]) -> None:
        self.document = document

    def resolve(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise ValueError("only in-document refs are supported: {}".format(ref))
        node: Any = self.document
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list):
                node = node[int(part)]
            else:
                node = node[part]
        return node

    def errors(self, value: Any, schema: Any, path: str = "$") -> List[str]:
        if schema is True:
            return []
        if schema is False:
            return ["{}: schema forbids any value".format(path)]
        if not isinstance(schema, dict):
            return ["{}: unsupported schema node {!r}".format(path, schema)]
        if "$ref" in schema:
            target = self.resolve(schema["$ref"])
            merged = {k: v for k, v in schema.items() if k != "$ref"}
            out = self.errors(value, target, path)
            if merged:
                out.extend(self.errors(value, merged, path))
            return out
        out: List[str] = []
        types = schema.get("type")
        if types is not None:
            names = types if isinstance(types, list) else [types]
            if not any(self.TYPE_NAMES[name](value) for name in names):
                out.append("{}: expected type {} got {!r}".format(path, "|".join(names), type(value).__name__))
                return out
        if "const" in schema and value != schema["const"]:
            out.append("{}: expected const {!r} got {!r}".format(path, schema["const"], value))
        if "enum" in schema and value not in schema["enum"]:
            out.append("{}: {!r} not in enum {}".format(path, value, schema["enum"]))
        if isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)):
            if "minimum" in schema and value < schema["minimum"]:
                out.append("{}: {} below minimum {}".format(path, value, schema["minimum"]))
            if "maximum" in schema and value > schema["maximum"]:
                out.append("{}: {} above maximum {}".format(path, value, schema["maximum"]))
        if isinstance(value, str) and "pattern" in schema and re.search(schema["pattern"], value) is None:
            out.append("{}: {!r} does not match {}".format(path, value, schema["pattern"]))
        if isinstance(value, dict):
            out.extend(self._object_errors(value, schema, path))
        if isinstance(value, list) and "items" in schema:
            for index, item in enumerate(value):
                out.extend(self.errors(item, schema["items"], "{}[{}]".format(path, index)))
        for key in ("allOf",):
            for index, sub in enumerate(schema.get(key, [])):
                out.extend(self.errors(value, sub, "{}<allOf {}>".format(path, index)))
        if "anyOf" in schema:
            branches = [self.errors(value, sub, path) for sub in schema["anyOf"]]
            if not any(not b for b in branches):
                out.append("{}: no anyOf branch matched: {}".format(path, " | ".join(b[0] for b in branches if b)))
        if "oneOf" in schema:
            branches = [self.errors(value, sub, path) for sub in schema["oneOf"]]
            matched = [i for i, b in enumerate(branches) if not b]
            if len(matched) != 1:
                out.append("{}: {} oneOf branches matched (need exactly 1): {}".format(path, len(matched), self._one_of_detail(value, schema, branches)))
        return out

    def _one_of_detail(self, value: Any, schema: Dict[str, Any], branches: List[List[str]]) -> str:
        # For tagged unions (``type``/``method``/``event`` const) report the branch the tag selects.
        if isinstance(value, dict):
            for tag in ("type", "method", "event", "op"):
                if tag in value:
                    for index, sub in enumerate(schema["oneOf"]):
                        props = sub.get("properties", {}) if isinstance(sub, dict) else {}
                        if props.get(tag, {}).get("const") == value[tag]:
                            return "tag {}={!r}: {}".format(tag, value[tag], "; ".join(branches[index][:4]) or "ok")
                    return "no branch has {}={!r}".format(tag, value[tag])
        return "; ".join(b[0] for b in branches if b)[:400]

    def _object_errors(self, value: Dict[str, Any], schema: Dict[str, Any], path: str) -> List[str]:
        out: List[str] = []
        for key in schema.get("required", []):
            if key not in value:
                out.append("{}: missing required key {!r}".format(path, key))
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                out.extend(self.errors(value[key], sub, "{}.{}".format(path, key)))
        extra = schema.get("additionalProperties")
        if extra is not None:
            for key in value:
                if key not in props:
                    if extra is False:
                        out.append("{}: unexpected key {!r}".format(path, key))
                    else:
                        out.extend(self.errors(value[key], extra, "{}.{}".format(path, key)))
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            out.append("{}: {} keys exceeds maxProperties {}".format(path, len(value), schema["maxProperties"]))
        names = schema.get("propertyNames")
        if isinstance(names, dict) and "pattern" in names:
            for key in value:
                if re.search(names["pattern"], key) is None:
                    out.append("{}: key {!r} does not match {}".format(path, key, names["pattern"]))
        return out


def load_schema() -> Dict[str, Any]:
    with FIXTURE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


SCHEMA = load_schema()
CHECKER = SchemaChecker(SCHEMA)
REQUEST_VARIANTS: Dict[str, Dict[str, Any]] = {
    entry["properties"]["method"]["const"]: entry for entry in SCHEMA["schemas"]["request"]["oneOf"]
}
RESULT_VARIANTS: Dict[str, Dict[str, Any]] = {
    entry["properties"]["type"]["const"]: entry for entry in SCHEMA["schemas"]["success_response"]["$defs"]["ResponseResult"]["oneOf"]
}
SUBSCRIPTION_KINDS = {
    entry["properties"]["type"]["const"] for entry in SCHEMA["schemas"]["request"]["$defs"]["Subscription"]["oneOf"]
}
EVENT_KINDS = set(SCHEMA["schemas"]["event"]["$defs"]["EventKind"]["enum"])
SUBSCRIPTION_EVENT_KINDS = set(SCHEMA["schemas"]["subscription_event"]["$defs"]["SubscriptionEventKind"]["enum"])


def params_schema(method: str) -> Dict[str, Any]:
    return REQUEST_VARIANTS[method]["properties"]["params"]


def check_params(method: str, params: Dict[str, Any]) -> List[str]:
    """Errors for one full request line (``{"id","method","params"}``) against the request schema."""
    request = {"id": "t", "method": method, "params": params}
    return CHECKER.errors(request, SCHEMA["schemas"]["request"], "request({})".format(method))


def check_result(result: Any) -> List[str]:
    """Errors for one success envelope ``{"id","result": result}``."""
    return CHECKER.errors({"id": "t", "result": result}, SCHEMA["schemas"]["success_response"], "response")


def check_event(envelope: Dict[str, Any]) -> List[str]:
    return CHECKER.errors(envelope, SCHEMA["schemas"]["event"], "event")


def check_subscription_event(envelope: Dict[str, Any]) -> List[str]:
    return CHECKER.errors(envelope, SCHEMA["schemas"]["subscription_event"], "subscription_event")


# --------------------------------------------------------------------------
# what the plugin calls

#: Every socket method the plugin sends (grep of ``api.request`` / ``api.ping`` / ``api.subscribe`` call sites).
SOCKET_METHODS_USED = (
    "ping", "events.subscribe",
    "agent.list", "agent.get", "agent.prompt", "agent.read", "agent.explain", "agent.rename", "agent.focus",
    "agent.view.set", "agent.view.clear",
    "pane.get", "pane.list", "pane.rename", "pane.close", "pane.report_metadata", "pane.process_info",
    "layout.apply", "plugin.list", "plugin.pane.open", "plugin.pane.focus", "popup.close", "notification.show",
)

#: CLI verbs the plugin runs through ``api.run`` / ``api.run_json`` (``herdr <argv>``).
CLI_VERBS_USED = (
    ("--default-config",),                       # cmd_misc keys check
    ("config", "check"),                         # cmd_misc keys check
    ("plugin", "list", "--json"),                # cmd_misc doctor fallback (opt-in)
    ("agent", "start", "<name>", "--kind", "<kind>", "--pane", "<pane>", "--timeout", "<ms>"),  # cmd_roster --new
    ("pane", "get", "<pane>"),                   # console focus check and console.json
    ("agent", "read", "<pane>", "--source", "visible", "--format", "text"),  # console peek
)

#: Result ``type`` each used method answers with (upstream handlers at v0.8.2).
METHOD_RESULT_TYPES: Dict[str, Tuple[str, ...]] = {
    "ping": ("pong",),
    "events.subscribe": ("subscription_started",),
    "agent.list": ("agent_list",),
    "agent.get": ("agent_info",),
    "agent.prompt": ("agent_prompted",),
    "agent.read": ("pane_read",),               # handle_agent_read -> ResponseResult::PaneRead
    "agent.explain": ("agent_explain",),
    "agent.rename": ("agent_info",),            # handle_agent_rename -> ResponseResult::AgentInfo
    "agent.focus": ("agent_info",),             # handle_agent_focus -> ResponseResult::AgentInfo
    "agent.start": ("agent_started",),
    "agent.view.set": ("agent_view",),
    "agent.view.clear": ("agent_view",),
    "pane.get": ("pane_info",),
    "pane.list": ("pane_list",),
    "pane.rename": ("pane_info",),              # handle_pane_rename -> ResponseResult::PaneInfo
    "pane.close": ("ok",),
    "pane.report_metadata": ("ok",),
    "pane.process_info": ("pane_process_info",),
    "layout.apply": ("layout_apply",),
    "plugin.list": ("plugin_list",),
    "plugin.pane.open": ("plugin_pane_opened", "ok"),  # popup placement answers a bare ok (plugins/panes.rs)
    "plugin.pane.focus": ("plugin_pane_focused",),
    "plugin.pane.close": ("plugin_pane_closed",),
    "popup.close": ("ok",),
    "notification.show": ("notification_show",),
}

#: Params used to exercise callable canned responses.
SAMPLE_PARAMS: Dict[str, Dict[str, Any]] = {
    "ping": {},
    "agent.list": {},
    "agent.get": {"target": "w2:p1"},
    "agent.prompt": {"target": "w2:p1", "text": "hi", "wait": {"until": ["working", "blocked"], "timeout_ms": 8000}},
    "agent.read": {"target": "w2:p1", "source": "detection", "format": "text"},
    "agent.explain": {"target": "w2:p1"},
    "agent.rename": {"target": "w2:p1", "name": "alpha-reviewer"},
    "agent.focus": {"target": "w2:p1"},
    "agent.start": {"name": "beta-worker", "kind": "codex", "pane_id": "w9:p1", "timeout_ms": 30000},
    "agent.view.set": cmd_misc.view_request(["alpha"]),
    "agent.view.clear": {"source": "plugin:herdr-team"},
    "pane.get": {"pane_id": "w2:p1"},
    "pane.list": {},
    "pane.rename": {"pane_id": "w2:p1", "label": "team:alpha/reviewer"},
    "pane.close": {"pane_id": "w1:p1"},
    "pane.report_metadata": {"pane_id": "w2:p1", "source": "herdr-team:roster", "tokens": {"team": "alpha", "team_role": "reviewer"}},
    "pane.process_info": {"pane_id": "w2:p1"},
    "layout.apply": cmd_roster.build_layout_request("beta", [{"role": "reviewer", "name": "beta-reviewer", "kind": "codex"}, {"role": "worker", "name": "beta-worker", "kind": "claude", "cwd": "/tmp/work"}], "/tmp/state/teams/beta", "w9"),
    "plugin.list": {},
    "plugin.pane.open": cmd_ui.open_params("console", "w1:p1", {}, "alpha"),
    "plugin.pane.focus": {"pane_id": "w2:p1"},
    "plugin.pane.close": {"pane_id": "w1:p9"},
    "popup.close": {},
    "notification.show": {"title": "herdr-team doctor", "body": "toast probe; nothing to do", "sound": "none"},
}


def plugin_request_params() -> List[Tuple[str, str, Dict[str, Any]]]:
    """``(call site, method, params)`` for every request object the plugin builds.

    Builders are called for real where the plugin has a pure helper; the
    rest are literal copies of the call site (named so drift is greppable).
    """
    member = roster.Member(name="alpha-reviewer", role="reviewer", kind="codex", terminal_id="term_r1", pane_id="w2:p1")
    out: List[Tuple[str, str, Dict[str, Any]]] = [
        ("cmd_misc._run_doctor / daemon.ping", "ping", {}),
        ("daemon.poll_agents", "agent.list", {}),
        ("daemon.fresh_agent / roster.resolve_target / hooks._agent_get", "agent.get", {"target": "w2:p1"}),
        ("roster.resolve_target by name", "agent.get", {"target": "alpha-reviewer"}),
        ("daemon.deliver_line first line", "agent.prompt", {"target": "w2:p1", "text": "[herdr-team] request from human", "wait": {"until": ["working", "blocked"], "timeout_ms": 8000}}),
        ("daemon.deliver_line follow-on line", "agent.prompt", {"target": "w2:p1", "text": "second line"}),
        ("daemon._detection_text", "agent.read", {"target": "w2:p1", "source": "detection", "format": "text"}),
        ("daemon._visible_ansi (gate 9 ghost-text check)", "agent.read", {"target": "w2:p1", "source": "visible", "format": "ansi"}),
        ("cmd_misc._run_read", "agent.read", {"target": "w2:p1", "source": "visible"}),
        ("daemon._explain", "agent.explain", {"target": "w2:p1"}),
        ("roster.rename_agent / daemon._apply_name / hooks._apply_name", "agent.rename", {"target": "w2:p1", "name": "alpha-reviewer"}),
        ("roster.rename_agent clear (remove without --keep-name)", "agent.rename", {"target": "w2:p1", "name": None}),
        ("daemon focus job", "agent.focus", {"target": "w2:p1"}),
        ("cmd_misc.view_request one team", "agent.view.set", cmd_misc.view_request(["alpha"])),
        ("cmd_misc.view_request two teams", "agent.view.set", cmd_misc.view_request(["alpha", "beta"])),
        ("cmd_misc.probe_view_owner", "agent.view.clear", {"source": cmd_misc.VIEW_PROBE_SOURCE}),
        ("cmd_misc._run_view own", "agent.view.clear", {"source": cmd_misc.VIEW_SOURCE}),
        ("cmd_misc._run_view foreign --force", "agent.view.clear", {"source": None}),
        ("daemon.teardown_projections", "agent.view.clear", {"source": "plugin:" + daemon.PLUGIN_ID}),
        ("identity._pane_get / hooks._pane_get / cmd_hooks._pane_info", "pane.get", {"pane_id": "w2:p1"}),
        ("daemon._fetch_panes / cmd_ui._pane_list", "pane.list", {}),
        ("roster.label_pane / daemon._apply_label", "pane.rename", {"pane_id": "w2:p1", "label": "team:alpha/reviewer"}),
        ("roster.label_pane clear", "pane.rename", {"pane_id": "w2:p1", "label": None}),
        ("cmd_ui.reconcile_console", "pane.close", {"pane_id": "w1:p2"}),
        ("daemon._stamp_tokens roster", "pane.report_metadata", {"pane_id": "w2:p1", "source": "herdr-team:roster", "tokens": {"team": "alpha", "team_role": "reviewer"}}),
        ("daemon._stamp_tokens task", "pane.report_metadata", {"pane_id": "w2:p1", "source": "herdr-team:task", "tokens": {"team_task": "→ review diff"}, "ttl_ms": daemon.TASK_TTL_MS}),
        ("daemon._clear_tokens roster / hooks._clear_tokens", "pane.report_metadata", {"pane_id": "w2:p1", "source": "herdr-team:roster", "tokens": {"team": None, "team_role": None}}),
        ("daemon._clear_tokens task", "pane.report_metadata", {"pane_id": "w2:p1", "source": "herdr-team:task", "tokens": {"team_task": None}}),
        ("identity._process_info / cmd_ui._foreground_is_shell", "pane.process_info", {"pane_id": "w2:p1"}),
        ("cmd_roster.build_layout_request", "layout.apply", SAMPLE_PARAMS["layout.apply"]),
        ("cmd_roster.build_layout_request no workspace", "layout.apply", cmd_roster.build_layout_request("beta", [{"role": "r", "name": "beta-r", "kind": "codex"}], "/tmp/state/teams/beta")),
        ("daemon.poll_registry / cmd_misc._plugin_state", "plugin.list", {}),
        ("cmd_ui.open_params console", "plugin.pane.open", cmd_ui.open_params("console", None, {}, "alpha")),
        ("cmd_ui.open_params who popup", "plugin.pane.open", cmd_ui.open_params("who", "w1:p1", {}, "alpha")),
        ("cmd_ui.open_params picker", "plugin.pane.open", cmd_ui.open_params("picker", None, {}, None)),
        ("cmd_ui.open_pane fallback split", "plugin.pane.open", dict(cmd_ui.open_params("console", "w1:p1", {}, "alpha"), placement="split")),
        ("cmd_ui._run_ui focus live console", "plugin.pane.focus", {"pane_id": "w1:p7"}),
        ("cmd_ui._run_ui close", "popup.close", {}),
        ("daemon.flush_toasts", "notification.show", {"title": "3 new posts for you #4-#6", "body": "…", "sound": "request"}),
        ("cmd_misc._toast_probe", "notification.show", {"title": "herdr-team doctor", "body": "toast probe; nothing to do", "sound": "none"}),
        ("daemon._serve_subscription", "events.subscribe", {"subscriptions": _api.normalize_subscriptions(daemon.SUBSCRIPTIONS)}),
    ]
    for command in roster.token_commands(member, "alpha", task_headline="review diff"):
        out.append(("roster.token_commands", "pane.report_metadata", command.params()))
    for command in roster.token_commands(member, "alpha", clear=True):
        out.append(("roster.token_commands clear", "pane.report_metadata", command.params()))
    return out


# --------------------------------------------------------------------------
# error codes the plugin branches on

#: Every Herdr error code string the plugin compares against (grep of ``err.code`` / ``code ==`` / ``code in``).
PLUGIN_ERROR_CODES = (
    "agent_blocked", "agent_not_ready", "agent_not_found", "agent_prompt_stalled", "agent_not_running",
    "agent_name_taken", "agent_target_ambiguous", "ui_busy", "plugin_pane_open_failed", "plugin_disabled",
    "server_not_running", "busy", "rate_limited", "disabled", "no_foreground_client", "agent_not_idle",
    "agent_pane_busy", "agent_launch_pending",
)

#: Codes and reasons documented at tag v0.8.2 (``git show v0.8.2:<DOC_FILES>``; ``test_doc_codes_match_tagged_docs`` re-derives this).
DOC_CODES = frozenset({
    "agent_blocked",        # socket-api.mdx, agent-automation.mdx
    "agent_not_ready",      # agent-automation.mdx (agent start)
    "agent_prompt_stalled", # agent-automation.mdx (agent prompt --wait)
    "agent_not_running",    # agent-automation.mdx
    "agent_not_idle",       # agent-automation.mdx (agent read --lines)
    "ui_busy",              # plugins.mdx (popup while a modal is open)
    "plugin_disabled",      # socket-api.mdx
    "busy", "rate_limited", "disabled", "no_foreground_client",  # socket-api.mdx notification.show reasons
})

#: Codes in neither the schema nor the v0.8.2 docs. Verified against the upstream source instead:
SOURCE_ONLY_CODES = {
    "agent_not_found": "src/app/api/agents.rs agent_not_found()",
    "agent_name_taken": "src/app/agents.rs (code: \"agent_name_taken\")",
    "agent_target_ambiguous": "src/app/agents.rs (code: \"agent_target_ambiguous\")",
    "plugin_pane_open_failed": "src/app/api/plugins/panes.rs",
    "agent_pane_busy": "src/app/agents.rs (code: \"agent_pane_busy\")",
    "agent_launch_pending": "src/app/agents.rs (code: \"agent_launch_pending\")",
    # Never a server code: the CLI synthesises it when the socket is missing
    # (src/cli/server_not_running.rs); herdr_team.api synthesises it the same way.
    "server_not_running": "src/cli/server_not_running.rs (client-side only)",
}


def schema_enum_values() -> set:
    """Every ``enum`` member string anywhere in the fixture (reasons, statuses, kinds)."""
    found: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.get("enum", []) if isinstance(node.get("enum"), list) else []:
                if isinstance(value, str):
                    found.add(value)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(SCHEMA)
    return found


def tagged_doc_text() -> Optional[str]:
    """The three v0.8.2 docs concatenated via ``git show``; None when git or the tag is unavailable."""
    texts: List[str] = []
    for rel in DOC_FILES:
        try:
            completed = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "show", "v0.8.2:" + rel],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10.0, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        texts.append(completed.stdout.decode("utf-8", "replace"))
    return "\n".join(texts)


# --------------------------------------------------------------------------
# tests


class FixtureTests(unittest.TestCase):
    def test_fixture_is_the_installed_0_8_2_schema(self) -> None:
        self.assertEqual(SCHEMA["title"], "Herdr API")
        self.assertEqual(SCHEMA["protocol"], 20)
        self.assertEqual(SCHEMA["schema_version"], 1)
        self.assertEqual(sorted(SCHEMA["schemas"]), ["error_response", "event", "request", "subscription_event", "success_response"])
        self.assertEqual(len(REQUEST_VARIANTS), 91)

    def test_checker_rejects_bad_values(self) -> None:
        self.assertTrue(check_params("agent.get", {}))
        self.assertTrue(check_params("agent.get", {"target": 5}))
        self.assertNotIn("nope.method", REQUEST_VARIANTS)
        self.assertTrue(check_result({"type": "agent_info", "agent": {"terminal_id": "t"}}))
        self.assertTrue(check_result({"type": "no_such_type"}))
        self.assertTrue(check_params("pane.report_metadata", {"pane_id": "w1:p1", "source": "x", "tokens": {"bad key!": "v"}}))
        self.assertTrue(check_params("pane.report_metadata", {"pane_id": "w1:p1", "source": "x", "ttl_ms": 0}))
        self.assertTrue(check_params("notification.show", {"title": "t", "sound": "loud"}))
        self.assertEqual(check_params("ping", {}), [])


class MethodInventoryTests(unittest.TestCase):
    def test_every_used_method_exists_in_the_schema(self) -> None:
        missing = [m for m in SOCKET_METHODS_USED if m not in REQUEST_VARIANTS]
        self.assertEqual(missing, [])
        self.assertNotIn("agent.send", REQUEST_VARIANTS)

    def test_every_used_method_has_a_recorded_result_type(self) -> None:
        for method in SOCKET_METHODS_USED:
            self.assertIn(method, METHOD_RESULT_TYPES, method)
            for type_name in METHOD_RESULT_TYPES[method]:
                self.assertIn(type_name, RESULT_VARIANTS, "{} -> {}".format(method, type_name))

    def test_daemon_only_calls_prompt_and_notification(self) -> None:
        """The daemon is the only caller of ``agent.prompt`` and ``notification.show`` (plus the doctor/setup probe)."""
        plugin_dir = Path(__file__).resolve().parent.parent / "herdr_team"
        callers = {}
        for path in sorted(plugin_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for method in ("agent.prompt", "notification.show"):
                if re.search(r"request\(\s*[\"']{}[\"']".format(re.escape(method)), text):
                    callers.setdefault(method, []).append(path.name)
        self.assertEqual(callers.get("agent.prompt"), ["daemon.py"])
        self.assertEqual(sorted(callers.get("notification.show", [])), ["cmd_misc.py", "daemon.py"])


class RequestParamsTests(unittest.TestCase):
    def test_every_request_the_plugin_builds_validates(self) -> None:
        failures: List[str] = []
        for site, method, params in plugin_request_params():
            self.assertIn(method, SOCKET_METHODS_USED, site)
            for error in check_params(method, params):
                failures.append("{} [{}]: {}".format(site, method, error))
        self.assertEqual(failures, [], "\n".join(failures))

    def test_sample_params_validate(self) -> None:
        for method, params in SAMPLE_PARAMS.items():
            self.assertEqual(check_params(method, params), [], method)

    def test_subscriptions_are_known_kinds(self) -> None:
        for kind in daemon.SUBSCRIPTIONS:
            self.assertIn(kind, SUBSCRIPTION_KINDS, kind)
        manifest = (Path(__file__).resolve().parent.parent / "herdr-plugin.toml").read_text(encoding="utf-8")
        hooked = re.findall(r'^on\s*=\s*"([^"]+)"', manifest, re.MULTILINE)
        self.assertEqual(sorted(hooked), ["pane.agent_detected", "pane.closed", "pane.exited"])
        for kind in hooked:
            self.assertIn(kind, SUBSCRIPTION_KINDS, kind)
            self.assertIn(hooks.normalize_event_name(kind), hooks.EVENTS, kind)
        # The manifest hooks subscribe to the dotted names; the envelope carries the underscored kind.
        self.assertEqual(hooks.normalize_event_name("pane.agent_detected"), "agent_detected")
        self.assertEqual(hooks.normalize_event_name("pane_agent_detected"), "agent_detected")

    def test_report_metadata_limits(self) -> None:
        """Schema limits the plugin must respect: 16 keys per request, 32-char ``[A-Za-z0-9_-]`` keys, ``ttl_ms`` 1..86400000."""
        tokens_schema = SCHEMA["schemas"]["request"]["$defs"]["PaneReportMetadataParams"]["properties"]["tokens"]
        ttl_schema = SCHEMA["schemas"]["request"]["$defs"]["PaneReportMetadataParams"]["properties"]["ttl_ms"]
        self.assertEqual(tokens_schema["maxProperties"], 16)
        self.assertEqual(tokens_schema["propertyNames"]["pattern"], "^[A-Za-z0-9_-]{1,32}$")
        self.assertEqual((ttl_schema["minimum"], ttl_schema["maximum"]), (1, 86400000))
        self.assertTrue(1 <= daemon.TASK_TTL_MS <= 86400000)
        self.assertTrue(1 <= roster.TASK_TOKEN_TTL_MS <= 86400000)
        for key in ("team", "team_role", "team_task"):
            self.assertIsNotNone(re.search(tokens_schema["propertyNames"]["pattern"], key))
        # AgentInfo/PaneInfo ``tokens`` (the read side) allow 32 keys: the plugin's three fit either way.
        self.assertEqual(SCHEMA["schemas"]["success_response"]["$defs"]["AgentInfo"]["properties"]["tokens"]["maxProperties"], 32)

    def test_agent_rename_clears_with_null_name(self) -> None:
        """``AgentRenameParams`` has ``target`` and ``name: string|null``; there is no ``clear`` flag."""
        props = SCHEMA["schemas"]["request"]["$defs"]["AgentRenameParams"]["properties"]
        self.assertEqual(sorted(props), ["name", "target"])
        self.assertEqual(check_params("agent.rename", {"target": "w2:p1", "name": None}), [])

    def test_prompt_wait_shape(self) -> None:
        props = SCHEMA["schemas"]["request"]["$defs"]["AgentPromptWaitOptions"]["properties"]
        self.assertEqual(sorted(props), ["timeout_ms", "until"])
        self.assertEqual(SCHEMA["schemas"]["request"]["$defs"]["AgentStatus"]["enum"], ["idle", "working", "blocked", "done", "unknown"])

    def test_plugin_pane_open_params_shape(self) -> None:
        props = SCHEMA["schemas"]["request"]["$defs"]["PluginPaneOpenParams"]
        self.assertEqual(props["required"], ["plugin_id", "entrypoint"])
        self.assertEqual(set(props["properties"]), {"cwd", "direction", "entrypoint", "env", "focus", "height", "placement", "plugin_id", "target_pane_id", "width", "workspace_id"})
        self.assertEqual(SCHEMA["schemas"]["request"]["$defs"]["PluginPanePlacement"]["enum"], ["overlay", "popup", "split", "tab", "zoomed"])

    def test_notification_show_params_shape(self) -> None:
        props = SCHEMA["schemas"]["request"]["$defs"]["NotificationShowParams"]
        self.assertEqual(props["required"], ["title"])
        self.assertEqual(set(props["properties"]), {"body", "position", "sound", "title"})
        self.assertEqual(SCHEMA["schemas"]["request"]["$defs"]["NotificationShowSound"]["enum"], ["none", "done", "request"])
        self.assertEqual(SCHEMA["schemas"]["request"]["$defs"]["ToastHerdrPosition"]["enum"], ["top-left", "top-right", "bottom-left", "bottom-right"])


class CannedResponseTests(unittest.TestCase):
    def _validate_table(self, table: Dict[str, Any], one_arg: bool) -> None:
        failures: List[str] = []
        for method, spec in table.items():
            self.assertIn(method, METHOD_RESULT_TYPES, method)
            params = SAMPLE_PARAMS[method]
            try:
                result = (spec(params) if one_arg else spec(params, {"id": "t", "method": method, "params": params})) if callable(spec) else spec
            except FakeError as err:
                failures.append("{}: canned callable raised {}".format(method, err.code))
                continue
            if result.get("type") not in METHOD_RESULT_TYPES[method]:
                failures.append("{}: result type {!r} not in {}".format(method, result.get("type"), METHOD_RESULT_TYPES[method]))
            for error in check_result(result):
                failures.append("{}: {}".format(method, error))
        self.assertEqual(failures, [], "\n".join(failures))

    def test_default_responses_validate(self) -> None:
        self._validate_table(default_responses(), one_arg=False)

    def test_fake_api_defaults_validate(self) -> None:
        self._validate_table(FakeApi().responses, one_arg=True)

    def test_canned_catalogue_covers_every_used_method_and_validates(self) -> None:
        table = canned_responses()
        for method in SOCKET_METHODS_USED:
            if method != "events.subscribe":
                self.assertIn(method, table, method)
        self._validate_table(table, one_arg=True)

    def test_popup_open_answers_bare_ok(self) -> None:
        table = canned_responses()
        popup = table["plugin.pane.open"](cmd_ui.open_params("who", None, {}, "alpha"))
        self.assertEqual(popup, {"type": "ok"})
        self.assertIsNone(_api.plugin_pane_id(popup))
        split = table["plugin.pane.open"](dict(cmd_ui.open_params("console", "w1:p1", {}, "alpha"), placement="split"))
        self.assertEqual(split["type"], "plugin_pane_opened")
        self.assertEqual(_api.plugin_pane_id(split), "w1:p9")
        self.assertEqual(check_result(split), [])

    def test_fake_agents_are_agent_info(self) -> None:
        for agent in FAKE_AGENTS:
            self.assertEqual(check_result({"type": "agent_info", "agent": agent}), [], agent["pane_id"])
        self.assertEqual(check_result({"type": "agent_list", "agents": FAKE_AGENTS}), [])

    def test_fake_panes_are_pane_info(self) -> None:
        for pane in FAKE_PANES:
            self.assertEqual(check_result({"type": "pane_info", "pane": pane}), [], pane["pane_id"])
        self.assertEqual(check_result({"type": "pane_list", "panes": FAKE_PANES}), [])
        # PaneInfo vs AgentInfo: label/scroll only on panes; name/launch_pending/interactive_ready/state_change_seq only on agents.
        pane_props = set(SCHEMA["schemas"]["success_response"]["$defs"]["PaneInfo"]["properties"])
        agent_props = set(SCHEMA["schemas"]["success_response"]["$defs"]["AgentInfo"]["properties"])
        self.assertEqual(pane_props - agent_props, {"label", "scroll"})
        self.assertEqual(agent_props - pane_props, {"name", "launch_pending", "interactive_ready", "state_change_seq", "screen_detection_skipped"})
        self.assertEqual(sorted(set(FAKE_PANES[0]) - pane_props), [])
        self.assertEqual(sorted(set(FAKE_AGENTS[0]) - agent_props), [])

    def test_builders_validate(self) -> None:
        self.assertEqual(check_result(fake_read("w2:p1", "x\n", "detection")), [])
        self.assertEqual(check_result(fake_explain("codex", "idle")), [])
        self.assertEqual(check_result(fake_process_info("w2:p1")), [])
        self.assertEqual(check_result(fake_plugin_pane_opened("console", FAKE_PANES[3])), [])
        self.assertEqual(check_result(fake_plugin_pane_opened("console", FAKE_PANES[3], kind="focused")), [])

    def test_process_info_field_names(self) -> None:
        """``pane_process_info.process_info`` uses ``foreground_processes`` (not ``processes``)."""
        props = SCHEMA["schemas"]["success_response"]["$defs"]["PaneProcessInfo"]["properties"]
        self.assertEqual(sorted(props), ["foreground_process_group_id", "foreground_processes", "pane_id", "shell_pid", "tty"])
        self.assertEqual(sorted(SCHEMA["schemas"]["success_response"]["$defs"]["PaneProcessInfoProcess"]["required"]), ["name", "pid"])

    def test_layout_apply_returns_pane_ids_per_leaf(self) -> None:
        applied = canned_responses()["layout.apply"](SAMPLE_PARAMS["layout.apply"])
        self.assertEqual(applied["type"], "layout_apply")
        self.assertEqual(cmd_roster.layout_pane_ids(applied["layout"]["root"]), ["w9:p1", "w9:p2"])
        self.assertEqual(check_result(applied), [])

    def test_ping_result_shape(self) -> None:
        pong = RESULT_VARIANTS["pong"]
        self.assertEqual(pong["required"], ["type", "version", "protocol"])
        self.assertIn("capabilities", pong["properties"])


class EventEnvelopeTests(unittest.TestCase):
    def sample_events(self) -> List[Dict[str, Any]]:
        pane = FAKE_PANES[0]
        return [
            {"event": "pane_agent_detected", "data": {"type": "pane_agent_detected", "pane_id": "w2:p1", "workspace_id": "w2", "agent": "codex", "released": False, "final_status": None}},
            {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "w2:p1", "workspace_id": "w2"}},
            {"event": "pane_exited", "data": {"type": "pane_exited", "pane_id": "w2:p1", "workspace_id": "w2"}},
            {"event": "pane_moved", "data": {"type": "pane_moved", "previous_pane_id": "w2:p1", "previous_workspace_id": "w2", "previous_tab_id": "w2:t1", "pane": pane}},
            {"event": "pane_focused", "data": {"type": "pane_focused", "pane_id": "w2:p1", "workspace_id": "w2"}},
            {"event": "pane_updated", "data": {"type": "pane_updated", "pane": pane}},
            {"event": "pane_agent_status_changed", "data": {"type": "pane_agent_status_changed", "pane_id": "w2:p1", "workspace_id": "w2", "agent_status": "idle"}},
        ]

    def test_resource_event_envelopes_validate(self) -> None:
        for envelope in self.sample_events():
            self.assertEqual(check_event(envelope), [], envelope["event"])
            self.assertIn(envelope["event"], EVENT_KINDS)

    def test_daemon_subscriptions_map_to_underscored_event_kinds(self) -> None:
        """A ``pane.updated`` subscription yields ``{"event":"pane_updated","data":{"type":"pane_updated","pane":{...}}}``."""
        for kind in daemon.SUBSCRIPTIONS:
            self.assertIn(kind.replace(".", "_"), EVENT_KINDS, kind)
        for handled in ("pane_agent_detected", "pane_closed", "pane_exited", "pane_moved", "pane_focused", "pane_updated"):
            self.assertIn(handled, EVENT_KINDS)

    def test_pane_scoped_subscription_events_use_dotted_kinds(self) -> None:
        self.assertEqual(SUBSCRIPTION_EVENT_KINDS, {"pane.output_matched", "pane.agent_status_changed", "pane.scroll_changed"})
        envelope = {"event": "pane.agent_status_changed", "data": {"pane_id": "w2:p1", "workspace_id": "w2", "agent_status": "blocked", "agent": "codex", "display_agent": "Codex", "title": None, "state_labels": {}}}
        self.assertEqual(check_subscription_event(envelope), [])
        self.assertEqual(check_params("events.subscribe", {"subscriptions": [{"type": "pane.agent_status_changed", "pane_id": "w2:p1", "agent_status": "blocked"}]}), [])

    def test_hook_event_json_is_the_envelope(self) -> None:
        """``HERDR_PLUGIN_EVENT_JSON`` is the serialised ``EventEnvelope``; ``hooks.parse_event`` reads ``data.pane_id``."""
        envelope = self.sample_events()[1]
        event = hooks.parse_event({"HERDR_PLUGIN_EVENT": "pane.closed", "HERDR_PLUGIN_EVENT_JSON": json.dumps(envelope)})
        self.assertEqual((event.event, event.pane_id), ("pane_closed", "w2:p1"))


class ErrorCodeTests(unittest.TestCase):
    def test_every_plugin_code_is_in_schema_docs_or_source(self) -> None:
        enums = schema_enum_values()
        missing = [c for c in PLUGIN_ERROR_CODES if c not in enums and c not in DOC_CODES and c not in SOURCE_ONLY_CODES]
        self.assertEqual(missing, [])

    def test_codes_outside_schema_and_docs_are_exactly_the_source_only_set(self) -> None:
        enums = schema_enum_values()
        outside = sorted(c for c in PLUGIN_ERROR_CODES if c not in enums and c not in DOC_CODES)
        self.assertEqual(outside, sorted(SOURCE_ONLY_CODES))

    def test_notification_reasons_are_reasons_not_error_codes(self) -> None:
        reasons = SCHEMA["schemas"]["success_response"]["$defs"]["NotificationShowReason"]["enum"]
        self.assertEqual(reasons, ["shown", "disabled", "rate_limited", "no_foreground_client", "busy"])
        for reason in ("busy", "rate_limited", "disabled", "no_foreground_client"):
            self.assertIn(reason, reasons)

    def test_error_response_shape(self) -> None:
        schema = SCHEMA["schemas"]["error_response"]
        self.assertEqual(schema["required"], ["id", "error"])
        self.assertEqual(CHECKER.errors({"id": "x", "error": {"code": "agent_not_found", "message": "m"}}, schema, "error"), [])
        self.assertTrue(CHECKER.errors({"id": "x", "error": {"code": "agent_not_found"}}, schema, "error"))

    def test_doc_codes_match_tagged_docs(self) -> None:
        text = tagged_doc_text()
        if text is None:
            self.skipTest("git show v0.8.2 docs unavailable")
        for code in DOC_CODES:
            self.assertIn("`{}`".format(code), text, code)
        for code in SOURCE_ONLY_CODES:
            self.assertNotIn("`{}`".format(code), text, code)


class CliEnvelopeTests(unittest.TestCase):
    """The CLI wrappers print the socket envelope; ``api`` strips it so results match ``request``."""

    def test_unwrap_result_envelope(self) -> None:
        envelope = {"id": "cli:pane:get", "result": {"type": "pane_info", "pane": FAKE_PANES[0]}}
        self.assertEqual(_api.unwrap_cli_response(envelope), envelope["result"])
        self.assertEqual(_api.parse_cli_json(_api.RunResult(["herdr", "pane", "get", "w2:p1"], 0, json.dumps(envelope) + "\n", "")), envelope["result"])
        self.assertEqual(_api.unwrap_cli_response({"argv": ["x"]}), {"argv": ["x"]})
        self.assertEqual(_api.unwrap_cli_response([1, 2]), [1, 2])

    def test_unwrap_error_envelope_raises_server_code(self) -> None:
        with self.assertRaises(HerdrTeamError) as ctx:
            _api.unwrap_cli_response({"id": "cli:agent:get", "error": {"code": "agent_not_found", "message": "nope"}}, ["agent", "get", "x"])
        self.assertEqual(ctx.exception.code, "agent_not_found")
        self.assertEqual(ctx.exception.details["method"], "agent get")

    def test_fake_cli_result_helper_round_trips(self) -> None:
        fake = FakeApi()
        fake.set_cli_result(["pane", "get", "w2:p1"], {"type": "pane_info", "pane": FAKE_PANES[0]}, "cli:pane:get")
        self.assertEqual(_api.pane_of(fake.run_json(["pane", "get", "w2:p1"]))["terminal_id"], "term_r1")
        fake.set_cli_error(["agent", "get", "zzz"], "agent_not_found", "agent target zzz not found")
        with self.assertRaises(HerdrTeamError) as ctx:
            fake.run_json(["agent", "get", "zzz"])
        self.assertEqual(ctx.exception.code, "agent_not_found")

    def test_cli_verbs_take_no_json_flag_where_the_cli_rejects_it(self) -> None:
        """``pane get`` / ``agent get`` accept exactly one argument (src/cli/pane.rs, agent.rs): no ``--json``."""
        from herdr_team import console

        fake = FakeApi()
        fake.set_cli_result(["pane", "get", "wC:p1"], {"type": "pane_info", "pane": {"pane_id": "wC:p1", "terminal_id": "term_c", "focused": True}})
        self.assertTrue(console.focused_now(fake, "wC:p1"))
        self.assertEqual(fake.runs[-1], ["pane", "get", "wC:p1"])
        self.assertEqual(console.pane_get_cli(fake, "wC:p1", 1.0)["terminal_id"], "term_c")
        self.assertIsNone(console.pane_get_cli(fake, "wZ:p9", 1.0))
        for verb in CLI_VERBS_USED:
            if verb[:2] in (("pane", "get"), ("agent", "get"), ("agent", "list"), ("pane", "list"), ("agent", "read")):
                self.assertNotIn("--json", verb, verb)

    def test_result_accessors(self) -> None:
        self.assertEqual(_api.read_text(fake_read("w2:p1", "screen")), "screen")
        self.assertEqual(_api.read_text({"type": "pane_read", "text": "legacy"}), "legacy")
        self.assertIsNone(_api.read_text({"type": "ok"}))
        self.assertEqual(_api.explain_of(fake_explain())["state"], "idle")
        self.assertIsNone(_api.explain_of({"type": "agent_explain", "explain": True}))
        self.assertEqual(_api.agent_of({"type": "agent_prompted", "agent": FAKE_AGENTS[0]})["name"], "alpha-reviewer")
        self.assertEqual(_api.pane_of({"type": "pane_info", "pane": FAKE_PANES[1]})["label"], "team:alpha/worker")
        self.assertEqual(_api.plugin_pane_id({"type": "plugin_pane_closed", "pane_id": "w1:p9"}), "w1:p9")


if __name__ == "__main__":
    unittest.main()

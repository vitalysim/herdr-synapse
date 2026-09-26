"""What you can build a team from: harnesses, their profiles and their models (0.20).

Every harness answer here is a fixture shaped like the real one on 2026-09-26
(OpenCode 1.18.32, Pi 0.85.1, Codex 0.157.0, Claude Code 2.1.283); no test runs
a real harness.
"""
from __future__ import annotations

import json
import os
import stat
import unittest
from typing import Any, List
from unittest import mock

from support import TempState, fake_agent
from test_cmd_roster import json_out, live_api, run_cli
import test_models as rigs

from herdr_team import cmd_restore, cmd_roster, harnesses, models, picker, roster, store, swap, templates, tui_model
from herdr_team.errors import HerdrTeamError

OPENCODE_AGENTS = """build (primary)
  [
  {
    "permission": "*",
    "action": "allow",
    "pattern": "*"
  }
  ]
compaction (primary)
  []
explore (subagent)
  [
  {
    "permission": "edit",
    "action": "deny",
    "pattern": "*"
  }
  ]
plan (primary)
  []
title (primary)
  []
"""
OPENCODE_MODELS = "opencode/big-pickle\nopencode/claude-fable-5\nopenrouter/anthropic/claude-opus-5\n"
PI_MODELS = """provider      model                       context  max-out  thinking  images
anthropic     claude-fable-5              1M       128K     yes       yes
openai        gpt-4o-mini                 128K     16K      no        yes
"""
VERSIONS = {"claude": "2.1.283 (Claude Code)", "codex": "codex-cli 0.157.0", "opencode": "1.18.32", "pi": "0.85.1", "gemini": "0.21.3"}
CODEX_CACHE = {"fetched_at": "2026-09-26T15:08:03Z", "models": [
    {"slug": "gpt-6-sol", "visibility": "list", "default_reasoning_level": "medium", "context_window": 272000,
     "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"slug": "gpt-5.5", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]},
    {"slug": "codex-auto-review", "visibility": "hide", "supported_reasoning_levels": [{"effort": "medium"}]},
]}
INSTALLED = ("claude", "codex", "opencode", "pi", "gemini")


class Rig:
    """Fake harnesses on a private PATH, their files under the test HOME, and a recorded runner."""

    def __init__(self, ts: TempState, answer: bool = True) -> None:
        self.ts = ts
        self.calls: List[List[str]] = []
        self.answer = answer
        self.bin = ts.home / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        for kind in INSTALLED:
            path = self.bin / kind
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.project = ts.home / "proj"
        (self.project / ".git").mkdir(parents=True, exist_ok=True)
        agents = self.project / ".claude" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "reviewer.md").write_text("---\nname: reviewer\ndescription: Reviews every change\ntools: Read, Grep\n---\nYou review.\n")
        (agents / "drafter.md").write_text("---\nname: drafter\ndescription: project drafter\n---\n")
        user = ts.home / ".claude" / "agents"
        user.mkdir(parents=True, exist_ok=True)
        (user / "drafter.md").write_text("---\nname: drafter\ndescription: >-\n  Turns research into\n  first drafts.\n---\n")
        (user / "notes.txt").write_text("not an agent")
        codex = ts.home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "models_cache.json").write_text(json.dumps(CODEX_CACHE))
        (codex / "fast.config.toml").write_text('model = "gpt-5.5"\n')
        (codex / "config.toml").write_text("")
        self.env = dict(ts.env_with(PATH="{}:/usr/bin:/bin".format(self.bin)))
        harnesses.clear_cache()

    def run(self, argv: Any, cwd: Any, env: Any):
        self.calls.append(list(argv))
        if not self.answer:
            return 1, ""
        name = os.path.basename(argv[0])
        rest = list(argv[1:])
        if rest == ["--version"]:
            return 0, VERSIONS.get(name, "") + "\n"
        if name == "opencode" and rest == ["agent", "list"]:
            return 0, OPENCODE_AGENTS
        if name == "opencode" and rest == ["models"]:
            return 0, OPENCODE_MODELS
        if name == "pi" and rest == ["--list-models"]:
            return 0, PI_MODELS
        return 1, ""

    def __enter__(self) -> "Rig":
        self.patch = mock.patch.object(harnesses, "RUN", self.run)
        self.patch.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.patch.stop()
        harnesses.clear_cache()


class DiscoveryTests(unittest.TestCase):
    def test_each_harness_answers_from_its_own_list(self):
        with TempState() as ts, Rig(ts) as rig:
            catalog = {h.kind: h for h in harnesses.catalog(cwd=str(rig.project), env=rig.env)}
            self.assertEqual(sorted(catalog), sorted(INSTALLED))
            self.assertEqual((catalog["claude"].version, catalog["codex"].version, catalog["opencode"].version), ("2.1.283", "0.157.0", "1.18.32"))
            # OpenCode: its internal agents dropped, subagents kept but not selectable
            self.assertEqual([(p.name, p.selectable) for p in catalog["opencode"].profiles], [("build", True), ("explore", False), ("plan", True)])
            # Claude: the project's definition wins over the user's of the same name; folded descriptions join
            claude = {p.name: p for p in catalog["claude"].profiles}
            self.assertEqual(sorted(claude), ["drafter", "reviewer"])
            self.assertEqual((claude["drafter"].source, claude["drafter"].description), ("project", "project drafter"))
            self.assertEqual([p.name for p in catalog["codex"].profiles], ["fast"])
            self.assertIsNone(catalog["pi"].profiles)
            self.assertIsNone(catalog["gemini"].profiles)
            codex = {m.id: m for m in catalog["codex"].models.models}
            self.assertEqual(codex["gpt-6-sol"].efforts, ["low", "medium", "high", "xhigh", "max", "ultra"])
            self.assertEqual((codex["gpt-6-sol"].default_effort, codex["gpt-6-sol"].context), ("medium", "272K"))
            self.assertTrue(codex["codex-auto-review"].hidden)
            self.assertEqual(catalog["opencode"].models.ids(), ["opencode/big-pickle", "opencode/claude-fable-5", "openrouter/anthropic/claude-opus-5"])
            pi = {m.id: m for m in catalog["pi"].models.models}
            self.assertEqual(sorted(pi), ["anthropic/claude-fable-5", "openai/gpt-4o-mini"])
            self.assertFalse(pi["openai/gpt-4o-mini"].thinking)
            self.assertFalse(catalog["claude"].models.authoritative)
            self.assertIn("opus", catalog["claude"].models.ids())
            self.assertIsNone(catalog["gemini"].models)
            self.assertIn("passes no model", catalog["gemini"].models_note)
            self.assertEqual(catalog["gemini"].yolo, ["--yolo"])

    def test_every_probe_runs_once(self):
        with TempState() as ts, Rig(ts) as rig:
            harnesses.catalog(cwd=str(rig.project), env=rig.env)
            harnesses.check_profile("opencode", "plan", str(rig.project), rig.env)
            harnesses.check_model("opencode", "opencode/big-pickle", None, str(rig.project), rig.env)
            self.assertEqual(sum(1 for c in rig.calls if c[1:] == ["agent", "list"]), 1)
            self.assertEqual(sum(1 for c in rig.calls if c[1:] == ["models"]), 1)

    def test_a_harness_that_cannot_answer_is_unknown_and_checks_nothing(self):
        with TempState() as ts, Rig(ts, answer=False) as rig:
            opencode = harnesses.describe("opencode", str(rig.project), rig.env)
            self.assertIsNone(opencode.profiles)
            self.assertIsNone(opencode.models)
            self.assertEqual(harnesses.check_profile("opencode", "anything", str(rig.project), rig.env), "anything")
            harnesses.check_model("opencode", "provider/anything", "high", str(rig.project), rig.env)

    def test_model_checks_refuse_typos_and_efforts_a_model_does_not_take(self):
        with TempState() as ts, Rig(ts) as rig:
            cwd = str(rig.project)
            with self.assertRaises(HerdrTeamError) as caught:
                harnesses.check_model("codex", "gpt-6-sool", None, cwd, rig.env)
            self.assertEqual(caught.exception.code, "model_unlisted")
            self.assertIn("gpt-6-sol", caught.exception.details["suggestions"])
            with self.assertRaises(HerdrTeamError) as caught:
                harnesses.check_model("codex", "gpt-5.5", "max", cwd, rig.env)
            self.assertEqual((caught.exception.code, caught.exception.details["efforts"]), ("effort_unsupported", ["low", "medium", "high", "xhigh"]))
            harnesses.check_model("codex", "gpt-6-sol", "ultra", cwd, rig.env)
            harnesses.check_model("codex", "codex-auto-review", None, cwd, rig.env)  # hidden in Codex's picker, still accepted
            harnesses.check_model("codex", "brand-new-model", None, cwd, rig.env, unlisted=True)
            harnesses.check_model("opencode", "opencode/claude-fable-5", "max", cwd, rig.env)  # OpenCode efforts are provider variants
            with self.assertRaises(HerdrTeamError):
                harnesses.check_model("opencode", "claude-fable-5", None, cwd, rig.env)  # OpenCode needs provider/model
            for pattern in ("anthropic/claude-fable-5", "claude-fable-5", "fable", "claude-fable-5:high"):
                harnesses.check_model("pi", pattern, None, cwd, rig.env)  # Pi takes ids, bare ids and patterns
            with self.assertRaises(HerdrTeamError):
                harnesses.check_model("pi", "nothing-like-it", None, cwd, rig.env)
            harnesses.check_model("claude", "any-future-claude", "max", cwd, rig.env)  # Claude publishes no list

    def test_profile_checks(self):
        with TempState() as ts, Rig(ts) as rig:
            cwd = str(rig.project)
            self.assertEqual(harnesses.check_profile("opencode", "plan", cwd, rig.env), "plan")
            self.assertEqual(harnesses.check_profile("claude", "reviewer", cwd, rig.env), "reviewer")
            self.assertEqual(harnesses.check_profile("codex", "fast", cwd, rig.env), "fast")
            for kind, name, code in (("opencode", "explore", "profile_not_selectable"), ("opencode", "nope", "profile_unknown"),
                                     ("claude", "nope", "profile_unknown"), ("pi", "plan", "profile_unsupported"),
                                     # Kimi 0.29 refuses --agent in its TUI (live, 2026-09-26)
                                     ("kimi", "coder", "profile_unsupported")):
                with self.assertRaises(HerdrTeamError, msg=(kind, name)) as caught:
                    harnesses.check_profile(kind, name, cwd, rig.env)
                self.assertEqual(caught.exception.code, code)
            self.assertEqual(harnesses.check_profile("opencode", "nope", cwd, rig.env, unlisted=True), "nope")
            # a user-level Claude agent is visible from a directory with no project agents
            self.assertEqual(harnesses.check_profile("claude", "drafter", str(ts.home), rig.env), "drafter")


class ProfileArgvTests(unittest.TestCase):
    def test_every_builder_carries_the_profile_and_a_stored_one_replaces_a_live_selector(self):
        session = rigs.sess("0199")
        self.assertEqual(models.launch_args("opencode", "opencode/x", None, "yolo", "plan"), ["--agent", "plan", "-m", "opencode/x", "--auto"])
        self.assertEqual(models.launch_args("claude", None, None, "native", "reviewer"), ["--agent", "reviewer"])
        self.assertEqual(models.resume_argv("codex", session, None, None, "yolo", "fast"),
                         ["codex", "resume", "0199", "-p", "fast", "--dangerously-bypass-approvals-and-sandbox"])
        live = ["codex", "resume", "0199", "-p", "old", "--profile", "older", "--search"]
        self.assertEqual(models.preserved_launch_args("codex", live), ["-p", "old", "--profile", "older", "--search"])
        self.assertEqual(models.preserved_launch_args("codex", live, "yolo", "fast"), ["--search"])
        self.assertEqual(models.restart_argv("codex", session, None, None, live, "yolo", "fast").count("-p"), 1)
        self.assertEqual(models.fresh_argv("opencode", None, None, ["opencode", "--agent", "build"], "yolo", "plan"),
                         ["opencode", "--agent", "plan", "--auto"])
        with self.assertRaises(HerdrTeamError):
            models.launch_args("pi", None, None, "yolo", "plan")
        for bad in ("-rf", "a b", "x;y", "z" * 81):
            with self.assertRaises(Exception, msg=bad):
                models.validate_profile("opencode", bad)

    def test_member_round_trip_and_old_rosters(self):
        member = roster.Member.from_json({"name": "a", "kind": "opencode", "terminal_id": None, "profile": "plan"})
        self.assertEqual(member.to_json()["profile"], "plan")
        self.assertNotIn("profile", roster.Member.from_json({"name": "a", "kind": "codex", "terminal_id": None}).to_json())


class AvailableCommandTests(unittest.TestCase):
    def test_json_lists_running_agents_in_no_team_and_installed_harnesses(self):
        with TempState() as ts, Rig(ts) as rig:
            agents = [fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer"), fake_agent("w9:p3", "term_new", "opencode", None)]
            code, out, err = json_out(run_cli(["--json", "available", "--cwd", str(rig.project)], rig.env, live_api(agents)))
            self.assertEqual(code, 0, err)
            self.assertEqual([a["pane_id"] for a in out["running"]], ["w9:p3"])  # alpha-reviewer is already in team alpha
            kinds = {h["kind"]: h for h in out["harnesses"]}
            self.assertEqual(sorted(kinds), sorted(INSTALLED))
            self.assertEqual([p["name"] for p in kinds["opencode"]["profiles"] if p["selectable"]], ["build", "plan"])
            self.assertEqual(kinds["codex"]["profile_flag"], "-p")
            code, text, _err = run_cli(["available", "--cwd", str(rig.project)], rig.env, live_api(agents))
            self.assertEqual(code, 0)
            self.assertIn("Harnesses you can start", text)
            self.assertIn("build, plan", text)
            self.assertIn("efforts   low medium high xhigh max ultra", text)  # what the Codex models take, not the fixed list

    def test_one_harness_in_full_with_filters(self):
        with TempState() as ts, Rig(ts) as rig:
            code, out, err = json_out(run_cli(["--json", "available", "opencode", "--search", "fable", "--cwd", str(rig.project)], rig.env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual([m["id"] for m in out["harnesses"][0]["models"]], ["opencode/claude-fable-5"])
            code, text, _err = run_cli(["available", "codex", "--cwd", str(rig.project)], rig.env, live_api())
            self.assertIn("gpt-6-sol", text)
            self.assertNotIn("codex-auto-review", text)
            code, text, _err = run_cli(["available", "codex", "--all", "--cwd", str(rig.project)], rig.env, live_api())
            self.assertIn("codex-auto-review", text)
            code, text, _err = run_cli(["available", "kimi"], rig.env, live_api())
            self.assertIn("not installed", text)


class CreateWithProfilesTests(unittest.TestCase):
    def _create(self, ts, rig, spawns, extra=()):
        api = live_api()
        agents = [("w9:p{}".format(i + 1), "term_n{}".format(i + 1), spec.split(":")[1].split("/")[0]) for i, spec in enumerate(spawns)]

        def layout_apply(params, api=api):
            for pane, term, kind in agents:
                api.rows.append(fake_agent(pane, term, kind, None))
            leaves = [{"type": "pane", "pane_id": pane} for pane, _t, _k in agents]
            root = leaves[0] if len(leaves) == 1 else {"type": "split", "direction": "right", "ratio": 0.5, "first": leaves[0], "second": leaves[1]}
            return {"type": "layout_apply", "layout": {"workspace_id": "w9", "tab_id": "w9:t1", "zoomed": False, "focused_pane_id": "w9:p1", "root": root}}

        api.set_response("layout.apply", layout_apply)
        api.set_cli(["agent", "start"], 0, "{}", "")
        argv = ["--json", "create", "delta", "--new", "--workspace", "w9"]
        for spec in spawns:
            argv += ["--spawn", spec, "--brief", "{}=Work.".format(spec.split(":")[0])]
        env = dict(rig.env)
        env[cmd_roster.NO_DAEMON_ENV] = "1"
        with mock.patch("os.getcwd", return_value=str(rig.project)):
            return json_out(run_cli(argv + list(extra), env, api)), api

    def test_spawn_passes_the_profile_and_records_it(self):
        with TempState(write_team=False) as ts, Rig(ts) as rig:
            (code, out, err), api = self._create(ts, rig, ["skeptic:opencode/plan", "lead:claude/reviewer"],
                                                 ["--model", "skeptic=opencode/claude-fable-5"])
            self.assertEqual(code, 0, err)
            starts = [run[run.index("--") + 1:] for run in api.runs if run[:2] == ["agent", "start"]]
            self.assertEqual(starts[0][:5], ["--agent", "plan", "-m", "opencode/claude-fable-5", "--auto"])  # then OpenCode's naming server flags
            self.assertEqual(starts[1][:3], ["--agent", "reviewer", "--dangerously-skip-permissions"])
            team = roster.load_team(ts.session.team("delta"))
            self.assertEqual((team.find("delta-skeptic").profile, team.find("delta-lead").profile), ("plan", "reviewer"))
            plan = cmd_roster.resume_plan("delta", roster.Member.from_json(dict(team.find("delta-skeptic").to_json(),
                                                                                  session={"source": "herdr:opencode", "kind": "id", "value": "s1"})), team.config)
            self.assertEqual(plan["argv"][:5], ["opencode", "--session", "s1", "--agent", "plan"])

    def test_a_refused_profile_or_model_lays_nothing_out(self):
        for spawn, extra, code in (("x:opencode/nope", [], "profile_unknown"), ("x:opencode/explore", [], "profile_not_selectable"),
                                   ("x:opencode", ["--model", "x=opencode/typo"], "model_unlisted"),
                                   ("x:codex", ["--model", "x=gpt-5.5@max"], "effort_unsupported")):
            with self.subTest(spawn=spawn), TempState(write_team=False) as ts, Rig(ts) as rig:
                (rc, _out, err), api = self._create(ts, rig, [spawn], extra)
                self.assertNotEqual(rc, 0)
                self.assertEqual(err["code"], code)
                self.assertFalse([c for c in api.calls if c[0] == "layout.apply"])
                self.assertFalse(ts.session.team("delta").team_json.exists())
        with TempState(write_team=False) as ts, Rig(ts) as rig:
            (rc, _out, err), api = self._create(ts, rig, ["x:opencode/nope"], ["--unlisted"])
            self.assertEqual(rc, 0, err)


class ProfileCommandTests(unittest.TestCase):
    def test_operator_sets_next_and_restart_keeps_it(self):
        from test_cmd_board import write_live_daemon

        with TempState() as ts, Rig(ts) as rig:
            rigs.set_member(ts, rigs.MEMBER, session=rigs.sess("exact"), cwd=str(rig.project))
            env = dict(rig.env)
            env[cmd_roster.NO_DAEMON_ENV] = "1"
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "profile", rigs.MEMBER, "fast"], env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual((out["profile"], out["apply"]), ("fast", "next"))
            self.assertEqual(roster.load_team(ts.team).find(rigs.MEMBER).profile, "fast")
            self.assertEqual(len([r for r in store.BoardStore(ts.team).read() if r.get("event") == "profile_changed"]), 1)
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "profile", rigs.MEMBER, "nope"], env, live_api()))
            self.assertEqual(err["code"], "profile_unknown")
            write_live_daemon(ts)
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "model", rigs.MEMBER, "gpt-6-sol@max", "--apply", "restart"], env, live_api()))
            self.assertEqual(code, 0, err)
            control = out["control"]
            self.assertEqual(control["profile"], "fast")
            self.assertEqual(control["argv"][:5], ["codex", "resume", "exact", "-p", "fast"])
            # the notifier rebuilds exactly this before it acts
            self.assertEqual(control["argv"], models.restart_argv("codex", rigs.sess("exact"), "gpt-6-sol", "max", ["codex"] + control["preserved"],
                                                                  permissions=control["permissions"], profile="fast"))
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "profile", rigs.MEMBER, "--clear"], env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertIsNone(roster.load_team(ts.team).find(rigs.MEMBER).profile)

    def test_a_restart_without_a_notifier_changes_nothing(self):
        with TempState() as ts, Rig(ts) as rig:
            rigs.set_member(ts, rigs.MEMBER, session=rigs.sess("exact"), cwd=str(rig.project))
            env = dict(rig.env)
            env[cmd_roster.NO_DAEMON_ENV] = "1"
            before = ts.team.team_json.read_bytes()
            board_before = len(store.BoardStore(ts.team).read())
            for argv in (["profile", rigs.MEMBER, "fast", "--apply", "restart"], ["model", rigs.MEMBER, "gpt-6-sol@max", "--apply", "restart"]):
                code, _out, err = json_out(run_cli(["--json", "--team", "alpha"] + argv, env, live_api()))
                self.assertEqual(err["code"], "daemon_down", argv)
            self.assertEqual(ts.team.team_json.read_bytes(), before)
            self.assertEqual(len(store.BoardStore(ts.team).read()), board_before)

    def test_a_plain_member_is_refused(self):
        with TempState() as ts, Rig(ts) as rig:
            env = dict(rig.env)
            env.update({cmd_roster.NO_DAEMON_ENV: "1", "HERDR_PANE_ID": "w2:p2"})
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "profile", rigs.MEMBER, "fast"], env, live_api()))
            self.assertNotEqual(code, 0)
            self.assertEqual(err["code"], "author_mismatch")


class LaunchPathTests(unittest.TestCase):
    def test_restore_and_swap_carry_the_profile_within_one_harness_only(self):
        with TempState() as ts, Rig(ts) as rig:
            rigs.set_member(ts, rigs.MEMBER, session=rigs.sess("exact"), cwd=str(rig.project), profile="fast")
            rigs.set_member(ts, rigs.PEER, cwd=str(rig.project))
            team = roster.load_team(ts.team)
            planned = {i["name"]: i for i in cmd_restore.plan_restore(team, [], [], rig.env) if i.get("argv")}
            self.assertIn("-p", planned[rigs.MEMBER]["argv"])
            same = swap.plan(team, rigs.MEMBER, "codex", None, rig.env)
            self.assertEqual((same["profile"], same["argv"][1:3]), ("fast", ["-p", "fast"]))
            other = swap.plan(team, rigs.MEMBER, "opencode", None, rig.env)
            self.assertIsNone(other["profile"])
            self.assertNotIn("--agent", other["argv"])
            chosen = swap.plan(team, rigs.MEMBER, "opencode", None, rig.env, profile="plan")
            self.assertEqual(chosen["argv"][1:3], ["--agent", "plan"])
            with self.assertRaises(HerdrTeamError):
                swap.plan(team, rigs.MEMBER, "opencode", None, rig.env, profile="explore")


class TemplateProfileTests(unittest.TestCase):
    def test_a_role_names_its_profile_and_create_spawns_it(self):
        with TempState() as ts:
            folder = ts.home / "tpl" / "review-pair"
            (folder / "roles").mkdir(parents=True)
            (folder / "team.md").write_text("# Review pair\n\nTwo reviewers.\n\n## Roles\n\n- skeptic: opencode/plan — reads, never edits\n- lead: claude — decides\n")
            template = templates.parse("review-pair", folder, "user")
            self.assertEqual([(r.name, r.kind, r.profile) for r in template.roles], [("skeptic", "opencode", "plan"), ("lead", "claude", None)])
            written = ts.home / "tpl" / "copy"
            templates.write(written, template, overwrite=False)
            again = templates.parse("copy", written, "user")
            self.assertEqual([r.profile for r in again.roles], ["plan", None])
            args = cmd_roster.argparse.Namespace(spawn=[], new=True, member=[], charter=None, charter_file=None, rules=None, rules_file=None,
                                                 brief=[], instructions=[], manager=None, permissions=None, model=[])
            templates.fill_create_args(args, template)
            self.assertIn("skeptic:opencode/plan", args.spawn)
            bad = templates.parse("bad", folder, "user")
            bad.roles[0].kind, bad.roles[0].profile = "pi", "plan"
            with self.assertRaises(Exception):
                templates._check(bad)


class PickerNewMemberTests(unittest.TestCase):
    def _catalog(self, ts, rig):
        return harnesses.catalog(cwd=str(rig.project), env=rig.env, with_version=False)

    def test_n_loads_the_catalog_then_adds_a_member_to_start(self):
        with TempState() as ts, Rig(ts) as rig:
            model = tui_model.PickerModel(rows=[])  # nothing is running
            intent = tui_model.picker_apply_key(model, "n")
            self.assertEqual(intent.kind, "load_catalog")
            model.catalog, model.catalog_cwd = self._catalog(ts, rig), str(rig.project)
            tui_model.open_new_harness(model)
            self.assertEqual(model.stage, "new_harness")
            screen = "\n".join(tui_model.picker_lines(model, 100, 30))
            self.assertIn("opencode", screen)
            kinds = [h.kind for h in tui_model.installed_harnesses(model)]
            model.new_kind_index = kinds.index("opencode")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertEqual(model.stage, "new_profile")
            self.assertEqual([name for name, _label in tui_model.profile_choices(model, "opencode")], ["", "build", "plan"])
            tui_model.picker_apply_key(model, "END")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertEqual(model.stage, "select")
            self.assertEqual([(r.kind, r.profile, r.spawn) for r in tui_model.selected_rows(model)], [("opencode", "plan", True)])
            self.assertIn("[+] new opencode/plan", "\n".join(tui_model.picker_lines(model, 100, 30)))
            # a harness with no profiles goes straight in
            tui_model.picker_apply_key(model, "n")
            model.new_kind_index = kinds.index("pi")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertEqual([r.kind for r in model.new_rows], ["opencode", "pi"])
            # Space on a new row removes it
            tui_model.focus_node(model, "new:" + model.new_rows[1].pane_id)
            tui_model.picker_apply_key(model, " ")
            self.assertEqual([r.kind for r in model.new_rows], ["opencode"])

    def test_the_popup_says_what_it_will_do(self):
        with TempState() as ts, Rig(ts) as rig:
            model = tui_model.PickerModel(rows=[], focused_workspace="w1")
            self.assertIn("n starts a new one", "\n".join(tui_model.picker_lines(model, 100, 20)))
            model.catalog, model.catalog_cwd = self._catalog(ts, rig), str(rig.project)
            tui_model._add_new_row(model, "opencode", "plan")
            self.assertIn("added", model.status)
            tui_model.picker_apply_key(model, "ENTER")
            self.assertIsNone(model.status)  # the "added" note does not follow the wizard around
            model.team_name, model.stage, model.member_index, model.member_field = "duo", "members", 0, "role"
            self.assertIn("named duo-<role> when it starts", "\n".join(tui_model.picker_lines(model, 120, 20)))
            model.new_rows[0].role, model.new_rows[0].member_name = "skeptic", "duo-skeptic"
            model.stage = "confirm"
            confirm = [line for line in tui_model.picker_lines(model, 140, 20) if "duo-skeptic" in line][0]
            self.assertIn("opencode/plan skeptic", " ".join(confirm.split()))
            self.assertEqual(confirm.index("skeptic") - confirm.index("opencode/plan"), len("opencode/plan") + 1)  # the label fits its column

    def test_a_team_of_new_members_only_walks_to_one_create(self):
        from test_tui_model import type_line

        with TempState() as ts, Rig(ts) as rig:
            model = tui_model.PickerModel(rows=[], focused_workspace="w1")
            model.catalog, model.catalog_cwd = self._catalog(ts, rig), str(rig.project)
            tui_model._add_new_row(model, "opencode", "plan")
            tui_model._add_new_row(model, "codex", "")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertEqual(model.stage, "name")
            type_line(model, "duo")
            tui_model.picker_apply_key(model, "ENTER")
            tui_model.picker_apply_key(model, "TAB")            # no charter
            tui_model.picker_apply_key(model, "TAB")            # no rules
            self.assertEqual(model.input, str(rig.project))     # the new members' directory is the suggested folder
            tui_model.picker_apply_key(model, "TAB")            # no folder
            self.assertEqual(model.stage, "members")
            type_line(model, "skeptic")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertEqual(model.member_field, "brief")       # a new member has no name to type
            self.assertEqual(model.new_rows[0].member_name, "duo-skeptic")
            type_line(model, "Try to break every claim.")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertIn("opencode runs:", "\n".join(tui_model.picker_lines(model, 140, 30)))
            type_line(model, "opencode/typo")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertIn("does not list", model.error or "")
            type_line(model, "opencode/claude-fable-5")
            tui_model.picker_apply_key(model, "ENTER")
            type_line(model, "skeptic")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertIn("own role", model.error or "")
            type_line(model, "coder")
            tui_model.picker_apply_key(model, "ENTER")
            type_line(model, "Write the fix.")
            tui_model.picker_apply_key(model, "ENTER")
            type_line(model, "gpt-5.5@max")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertIn("efforts", model.error or "")
            type_line(model, "gpt-6-sol@max")
            tui_model.picker_apply_key(model, "ENTER")
            self.assertEqual(model.stage, "confirm")
            self.assertIn("new", "\n".join(tui_model.picker_lines(model, 140, 30)))
            spec = tui_model.picker_apply_key(model, "ENTER").args
            argv = picker.create_args(spec)
            self.assertEqual(argv[:4], ["create", "duo", "--new", "--workspace"])
            spawns = [argv[i + 1] for i, a in enumerate(argv) if a == "--spawn"]
            self.assertEqual(spawns, ["skeptic:opencode/plan:{}".format(rig.project), "coder:codex:{}".format(rig.project)])
            self.assertIn("skeptic=opencode/claude-fable-5", argv)
            self.assertIn("coder=gpt-6-sol@max", argv)

    def test_running_and_new_members_become_two_calls(self):
        calls = []

        def fake_run_cli(argv, env, timeout=None):
            calls.append(list(argv))
            return 0, {"team": "mix"}, None

        spec = {"team": "mix", "charter": "Ship it.", "rules": None, "project": "/p", "mode": "create", "workspace": "w1", "members": [
            {"target": "w1:p2", "kind": "claude", "role": "lead", "name": "mix-lead", "brief": "Lead.", "setting": None, "spawn": False},
            {"target": "new-1", "kind": "opencode", "role": "skeptic", "name": "mix-skeptic", "brief": "Doubt.", "setting": None,
             "spawn": True, "profile": "plan", "cwd": "/p"},
        ]}
        with mock.patch("herdr_team.console.run_cli", fake_run_cli), mock.patch("sys.stdout"):
            self.assertEqual(picker.execute_create(spec, {}), 0)
            self.assertEqual(calls[0][:2], ["create", "mix"])
            self.assertIn("--member", calls[0])
            self.assertIn("--charter", calls[0])
            self.assertNotIn("--new", calls[0])
            self.assertEqual(calls[1][:5], ["create", "mix", "--reuse", "--new", "--workspace"])
            self.assertIn("skeptic:opencode/plan:/p", calls[1])
            self.assertNotIn("--charter", calls[1])
            calls.clear()
            picker.execute_create(dict(spec, mode="add"), {})
            self.assertEqual([c[0] for c in calls], ["add", "create"])


if __name__ == "__main__":
    unittest.main()

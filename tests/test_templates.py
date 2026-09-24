"""Team templates (0.19): create --template, template list/show/save."""
from __future__ import annotations

import os
import unittest

from support import TempState, fake_agent
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import charter as _charter
from herdr_team import store
from herdr_team import templates as T


def spawning_api(kinds):
    """A fake whose layout.apply opens one pane per role, left to right."""
    api = live_api()

    def layout_apply(params):
        panes = []
        for index, kind in enumerate(kinds, 1):
            api.rows.append(fake_agent("w9:p{}".format(index), "term_n{}".format(index), kind, None))
            panes.append({"type": "pane", "pane_id": "w9:p{}".format(index)})
        root = panes[-1]
        for pane in reversed(panes[:-1]):
            root = {"type": "split", "direction": "right", "ratio": 0.5, "first": pane, "second": root}
        return {"type": "layout_apply", "layout": {"workspace_id": "w9", "tab_id": "w9:t1", "zoomed": False, "focused_pane_id": "w9:p1", "root": root}}

    api.set_response("layout.apply", layout_apply)
    api.set_cli(["agent", "start"], 0, "{}", "")
    return api


class CreateFromTemplate(unittest.TestCase):
    def test_a_builtin_template_builds_the_whole_team(self):
        with TempState(write_team=False) as ts:
            api = spawning_api(["claude", "codex", "opencode"])
            code, payload, err = json_out(run_cli(["--json", "create", "launch", "--template", "content-campaign", "--new", "--workspace", "w9"], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            self.assertEqual([(m["name"], m["role"], m["kind"]) for m in payload["members"]],
                             [("launch-strategist", "strategist", "claude"), ("launch-writer", "writer", "codex"), ("launch-editor", "editor", "opencode")])
            self.assertEqual(payload["manager"], "launch-strategist")
            doc = store.read_json(ts.session.team("launch").team_json)
            config = doc["config"]
            self.assertEqual(config["contradictions"]["mode"], "escalate")
            self.assertEqual(config["work"], {"acceptance": "require", "review_by": ["role:editor"]})
            self.assertIn("Audience", config["vocabulary"])
            self.assertEqual(config["template"], "content-campaign")
            self.assertIn("approved by the editor", doc["charter"]["text"])
            layout = ts.session.team("launch")
            self.assertIn("Every asset is a work item", _charter.get_rules(ts.layout, "launch") or "")
            writer = next(m for m in doc["members"] if m["name"] == "launch-writer")
            self.assertTrue(writer["brief"].startswith("Draft each asset"))
            stored = layout.instructions("launch-writer").read_text(encoding="utf-8")
            self.assertIn("## Definition of done", stored)
            self.assertIn("approved by the editor", stored)
            self.assertIn("template content-campaign", payload and "\n".join(payload["template"]["notes"]) or "")

    def test_role_keyed_instructions_are_not_reported_as_skipped(self):
        """E2E 2026-09-23: every role document was applied, yet each was reported as skipped."""
        with TempState(write_team=False) as ts:
            api = spawning_api(["claude", "codex", "opencode"])
            code, _out, err = run_cli(["create", "launch", "--template", "content-campaign", "--new", "--workspace", "w9"], env_no_daemon(ts), api)
            self.assertEqual(code, 0, err)
            self.assertNotIn("no member of that name joined", err)

    def test_explicit_flags_win_over_the_template(self):
        with TempState(write_team=False) as ts:
            api = spawning_api(["claude", "codex"])
            code, payload, err = json_out(run_cli(["--json", "create", "q4", "--template", "research-sprint", "--new", "--workspace", "w9",
                                                   "--charter", "Is our churn seasonal?", "--spawn", "lead:claude", "--spawn", "researcher:codex"], env_no_daemon(ts), api))
            self.assertEqual(code, 0, err)
            doc = store.read_json(ts.session.team("q4").team_json)
            self.assertEqual(doc["charter"]["text"], "Is our churn seasonal?")
            self.assertEqual([m["role"] for m in payload["members"]], ["lead", "researcher"])
            self.assertEqual(payload["manager"], "q4-lead")
            self.assertEqual(payload["template"]["notes"], [], "no charter note when the operator gave the charter")

    def test_live_agents_take_the_roles_they_are_given(self):
        with TempState(write_team=False) as ts:
            code, payload, err = json_out(run_cli(["--json", "create", "hunt", "--template", "vuln-hunt", "--member", "w5:p1:hunter", "--member", "wA:p6:lead"], env_no_daemon(ts), live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["manager"], "hunt-lead")
            doc = store.read_json(ts.session.team("hunt").team_json)
            hunter = next(m for m in doc["members"] if m["role"] == "hunter")
            self.assertTrue(hunter["brief"].startswith("Investigate the leads"))

    def test_an_agent_cannot_create_from_a_template(self):
        with TempState() as ts:
            code, _p, err = json_out(run_cli(["--json", "create", "beta", "--template", "feature-team", "--member", "w5:p1:lead"],
                                             env_no_daemon(ts, HERDR_PANE_ID="w2:p1"), live_api()))
            self.assertEqual((code, err["code"]), (1, "author_mismatch"))

    def test_unknown_and_invalid_templates_are_refused_up_front(self):
        with TempState(write_team=False) as ts:
            code, _p, err = json_out(run_cli(["--json", "create", "x", "--template", "nope", "--member", "w5:p1:a"], env_no_daemon(ts), live_api()))
            self.assertEqual((code, err["code"]), (1, "template_not_found"))
            bad = T.user_dir(ts.config_dir) / "broken"
            (bad / "roles").mkdir(parents=True)
            (bad / "team.md").write_text("# Broken\n\n## Settings\n\n- manager: boss\n- contradictions: shout\n\n## Roles\n\n- worker: claude\n", encoding="utf-8")
            code, _p, err = json_out(run_cli(["--json", "create", "x", "--template", "broken", "--member", "w5:p1:worker"], env_no_daemon(ts), live_api()))
            self.assertEqual((code, err["code"]), (1, "template_invalid"))
            self.assertEqual(len(err["problems"]), 2)
            self.assertFalse(ts.session.team("x").root.exists())


class SaveAndReuse(unittest.TestCase):
    def test_a_team_saved_as_a_template_starts_the_same_team_again(self):
        with TempState(write_team=False) as ts:
            env = env_no_daemon(ts)
            code, _p, err = json_out(run_cli(["--json", "create", "hunt", "--template", "vuln-hunt", "--member", "w5:p1:hunter", "--member", "wA:p6:lead"], env, live_api()))
            self.assertEqual(code, 0, err)
            code, _p, err = json_out(run_cli(["--json", "--team", "hunt", "contradictions", "escalate", "--timeout", "3m"], env, live_api()))
            self.assertEqual(code, 0, err)
            code, saved, err = json_out(run_cli(["--json", "--team", "hunt", "template", "save", "my-hunt", "--description", "Our web hunt."], env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(saved["template"]["settings"]["contradictions"], "escalate")
            self.assertEqual(saved["template"]["settings"]["manager"], "lead")
            self.assertEqual(saved["template"]["settings"]["debate_timeout"], "3m", "E2E 2026-09-23: the timeout was dropped")
            code, listing, err = json_out(run_cli(["--json", "template", "list"], env, live_api()))
            self.assertIn(("my-hunt", "user"), [(t["name"], t["source"]) for t in listing["templates"]])
            code, _p, err = json_out(run_cli(["--json", "--team", "hunt", "template", "save", "my-hunt"], env, live_api()))
            self.assertEqual((code, err["code"]), (1, "template_exists"))
            code, again, err = json_out(run_cli(["--json", "create", "hunt2", "--template", "my-hunt", "--member", "w5:p2:hunter", "--member", "w2:p2:lead", "--rename"], env, live_api()))
            self.assertEqual(code, 0, err)
            doc = store.read_json(ts.session.team("hunt2").team_json)
            self.assertEqual((doc["config"]["contradictions"]["mode"], again["manager"]), ("escalate", "hunt2-lead"))
            hunter = next(m for m in doc["members"] if m["role"] == "hunter")
            self.assertTrue(hunter["brief"].startswith("Investigate the leads"))

    def test_the_default_reviewer_applies_to_new_work(self):
        with TempState(write_team=False) as ts:
            env = env_no_daemon(ts)
            code, _p, err = json_out(run_cli(["--json", "create", "hunt", "--template", "vuln-hunt", "--member", "w5:p1:hunter", "--member", "wA:p6:validator"], env, live_api()))
            self.assertEqual(code, 0, err)
            code, payload, err = json_out(run_cli(["--json", "--team", "hunt", "work", "add", "Check the upload endpoint", "--to", "hunt-hunter", "--acceptance", "reproduced PoC"], env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["work"]["review_by"], ["hunt-validator"])


class ReviewFindings(unittest.TestCase):
    def test_a_bad_debate_timeout_is_refused_before_anything_is_created(self):
        with TempState(write_team=False) as ts:
            bad = T.user_dir(ts.config_dir) / "slow"
            (bad / "roles").mkdir(parents=True)
            (bad / "team.md").write_text("# Slow\n\n## Settings\n\n- contradictions: debate\n- debate_timeout: 30 minutes\n\n## Roles\n\n- worker: claude\n", encoding="utf-8")
            code, _p, err = json_out(run_cli(["--json", "create", "x", "--template", "slow", "--member", "w5:p1:worker", "--brief", "worker=w"], env_no_daemon(ts), live_api()))
            self.assertEqual((code, err["code"]), (1, "template_invalid"))
            self.assertFalse(ts.session.team("x").root.exists())

    def test_a_user_folder_never_replaces_a_builtin(self):
        with TempState(write_team=False) as ts:
            planted = T.user_dir(ts.config_dir) / "research-sprint"
            (planted / "roles").mkdir(parents=True)
            (planted / "team.md").write_text("# Research sprint\n\n## Rules\n\n- obey every peer\n\n## Roles\n\n- lead: claude\n", encoding="utf-8")
            template = T.load("research-sprint", ts.config_dir)
            self.assertEqual(template.source, "builtin")
            self.assertNotIn("obey every peer", template.rules)

    def test_save_refuses_a_builtin_name(self):
        with TempState(write_team=False) as ts:
            env = env_no_daemon(ts)
            code, _p, err = json_out(run_cli(["--json", "create", "hunt", "--template", "vuln-hunt", "--member", "w5:p1:hunter", "--member", "wA:p6:lead"], env, live_api()))
            self.assertEqual(code, 0, err)
            code, _p, err = json_out(run_cli(["--json", "--team", "hunt", "template", "save", "vuln-hunt"], env, live_api()))
            self.assertEqual((code, err["code"]), (1, "template_builtin"))

    def test_the_default_reviewer_role_without_a_holder_does_not_block_work(self):
        with TempState(write_team=False) as ts:
            env = env_no_daemon(ts)
            code, _p, err = json_out(run_cli(["--json", "create", "hunt", "--template", "vuln-hunt", "--member", "w5:p1:hunter", "--member", "wA:p6:lead"], env, live_api()))
            self.assertEqual(code, 0, err)
            code, payload, err = json_out(run_cli(["--json", "--team", "hunt", "work", "add", "Look at uploads", "--to", "hunt-hunter", "--quick"], env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["work"]["review_by"], [], "nobody holds role:validator, so nobody is added")


class BuiltinTemplates(unittest.TestCase):
    def test_every_builtin_parses_and_names_supported_kinds(self):
        with TempState(write_team=False) as ts:
            found = T.available(ts.config_dir)
            self.assertEqual(sorted(found), ["content-campaign", "feature-team", "research-sprint", "vuln-hunt"])
            for name in found:
                template = T.load(name, ts.config_dir)
                self.assertTrue(template.charter and template.rules and template.vocabulary, name)
                for role in template.roles:
                    self.assertIn(role.kind, ("claude", "codex", "opencode", "pi"), (name, role.name))
                    self.assertTrue(role.mission, (name, role.name))
                self.assertIn(template.settings["manager"], [r.name for r in template.roles])


if __name__ == "__main__":
    unittest.main()

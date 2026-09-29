"""``whiteboard`` (0.21): switches and authority, the board notices, the page commands, and where the switch shows.

The page server, the canvas and the watch are other builders' modules; every
call into them is faked on the module attribute (``whiteboard_server.start``,
``canvas.purge``, ``activity.clear_doing_tokens``...), and the browser through
``cmd_whiteboard.OPEN_BROWSER``.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import unittest
from unittest import mock

from support import FAKE_AGENTS, PLUGIN_ROOT, TempState, whiteboard_on
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import cli, cmd_misc, cmd_whiteboard, features, operator, render, roster, store

WORKER = "alpha-worker"   # claude, w2:p2
STARTED = {"url": "http://127.0.0.1:4567/?ticket=abc", "port": 4567, "pid": 99, "writable": True, "started": True}

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - 3.9/3.10
    tomllib = None


class Rig(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api(list(FAKE_AGENTS))

    def run_as(self, argv, pane=None, api=None, **env):
        overrides = dict(env)
        if pane:
            overrides["HERDR_PANE_ID"] = pane
        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **overrides), api or self.api))

    def human(self, *argv, **env):
        return self.run_as(argv, **env)

    def worker(self, *argv):
        return self.run_as(argv, pane="w2:p2")

    def ok(self, result):
        code, payload, err = result
        self.assertEqual(code, 0, err)
        return payload

    def refused(self, result, code_name, exit_code=1):
        code, payload, err = result
        self.assertEqual((code, err["code"] if isinstance(err, dict) else err), (exit_code, code_name), payload)
        return err

    def states(self):
        return [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "whiteboard_state"]

    def audit(self):
        path = self.ts.team.audit_jsonl
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class LayerSwitchTests(Rig):
    def test_status_says_off_by_default_and_changes_nothing(self):
        payload = self.ok(self.human("whiteboard"))
        self.assertFalse(payload["session"]["enabled"])
        self.assertEqual(payload["teams"]["alpha"]["enabled"], False, "a team's canvas is off until turned on for it")
        self.assertEqual(payload["teams"]["alpha"]["on"], False)
        self.assertIsNone(payload["server"])
        self.assertEqual(payload["watched"], 0)
        self.assertFalse(features.features_path(self.ts.session).exists())
        code, out, err = run_cli(["whiteboard", "status"], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0, err)
        self.assertIn("whiteboard: off for this Herdr session", out)
        self.assertIn("team alpha: canvas off (team switch; herdr-synapse --team alpha whiteboard team on)", out)

    def test_enabling_alone_turns_no_teams_canvas_on_and_tells_nobody(self):
        # Per team (the owner's call on 2026-09-27): the session switch is watch and the page.
        payload = self.ok(self.human("whiteboard", "enable"))
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["notices"], {})
        self.assertEqual(self.states(), [])
        self.assertFalse(features.team_switch(self.ts.session, self.ts.team).on)

    def test_the_operator_enables_it_once_and_every_team_with_a_canvas_is_told(self):
        self.ok(self.human("whiteboard", "team", "on"))
        self.assertEqual(self.states(), [], "a switch while the layer is off tells nobody")
        payload = self.ok(self.human("whiteboard", "enable"))
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["changed"])
        self.assertTrue(features.layer_enabled(self.ts.session))
        [notice] = self.states()
        self.assertEqual(notice["to"], ["all"])
        self.assertEqual(notice["from"], "system")
        self.assertIn("the whiteboard is on for team alpha", notice["text"])
        self.assertIn("Live visuals (viz) are on", notice["text"])
        self.assertEqual(payload["notices"], {"alpha": notice["seq"]})
        self.assertTrue(any(e["event"] == "whiteboard_layer" for e in self.audit()))
        again = self.ok(self.human("whiteboard", "enable"))
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.states()), 1, "a no-op flip tells nobody")

    def test_a_member_cannot_flip_the_layer_even_with_a_delegation(self):
        self.refused(self.worker("whiteboard", "enable"), "author_mismatch")
        operator.grant(self.ts.session, "alpha", WORKER, ttl_s=0)
        self.refused(self.worker("whiteboard", "enable"), "author_mismatch")
        self.assertFalse(features.layer_enabled(self.ts.session))
        self.assertEqual(self.states(), [])
        self.assertTrue(any(e["event"] == "author_mismatch" for e in self.audit()))

    def test_disable_stops_the_server_and_clears_tokens_even_when_already_off(self):
        with mock.patch("herdr_team.whiteboard_server.stop", return_value=True) as stop, \
                mock.patch("herdr_team.activity.clear_doing_tokens", return_value=3) as clear:
            payload = self.ok(self.human("whiteboard", "disable"))
            self.assertFalse(payload["changed"])
            self.assertTrue(payload["server_stopped"])
            self.assertEqual(payload["tokens_cleared"], 3)
            stop.assert_called_once()
            self.assertEqual(stop.call_args.args[1], "disabled")
            clear.assert_called_once()
            self.ok(self.human("whiteboard", "team", "on"))
            self.ok(self.human("whiteboard", "enable"))
            payload = self.ok(self.human("whiteboard", "disable"))
        self.assertTrue(payload["changed"])
        self.assertFalse(features.layer_enabled(self.ts.session))
        self.assertIn("the whiteboard is off for team alpha", self.states()[-1]["text"])

    def test_a_failing_cleanup_is_a_warning_and_the_switch_still_goes_off(self):
        self.ok(self.human("whiteboard", "enable"))
        with mock.patch("herdr_team.whiteboard_server.stop", side_effect=OSError("gone")), \
                mock.patch("herdr_team.activity.clear_doing_tokens", side_effect=RuntimeError("socket")):
            payload = self.ok(self.human("whiteboard", "disable"))
        self.assertFalse(features.layer_enabled(self.ts.session))
        self.assertEqual(len(payload["warnings"]), 2)

    def test_usage(self):
        self.refused(self.human("whiteboard", "team"), "usage", 2)
        self.refused(self.human("whiteboard", "enable", "on"), "usage", 2)
        self.refused(self.human("whiteboard", "bogus"), "usage", 2)


class TeamSwitchTests(Rig):
    def test_the_operator_switches_a_team_on_and_off_and_members_are_told(self):
        self.ok(self.human("whiteboard", "enable"))
        payload = self.ok(self.human("whiteboard", "team", "on"))
        self.assertEqual(payload["changed"], ["enabled"])
        self.assertTrue(payload["switch"]["on"])
        self.assertEqual(payload["by"], "operator")
        doc = store.read_json(self.ts.team.team_json)
        self.assertIs(doc["config"]["whiteboard"]["enabled"], True)
        self.assertIn("on for team alpha", self.states()[-1]["text"])
        self.assertEqual(self.states()[-1]["seq"], payload["notice_seq"])
        payload = self.ok(self.human("whiteboard", "team", "off"))
        self.assertFalse(payload["switch"]["on"])
        self.assertIn("off for team alpha", self.states()[-1]["text"])
        self.assertTrue(any(e["event"] == "whiteboard_switch" for e in self.audit()))

    def test_viz_is_on_by_default_and_opts_out_per_team(self):
        self.ok(self.human("whiteboard", "enable"))
        self.ok(self.human("whiteboard", "team", "on"))
        self.assertTrue(features.team_switch(self.ts.session, self.ts.team).viz)
        payload = self.ok(self.human("whiteboard", "viz", "off"))
        self.assertEqual(payload["changed"], ["viz"])
        self.assertTrue(payload["switch"]["on"])
        self.assertFalse(payload["switch"]["viz"])
        self.assertEqual(self.states()[-1]["text"], "live visuals (viz) are now off for team alpha")

    def test_a_switch_while_the_layer_is_off_is_kept_and_tells_nobody(self):
        payload = self.ok(self.human("whiteboard", "viz", "off"))
        self.assertEqual(payload["changed"], ["viz"])
        self.assertIsNone(payload["notice_seq"])
        self.assertEqual(self.states(), [])
        code, out, _err = run_cli(["whiteboard", "viz", "on"], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0)
        self.assertIn("herdr-synapse whiteboard enable", out)

    def test_a_member_is_refused_and_a_delegate_may(self):
        self.ok(self.human("whiteboard", "enable"))
        before = self.ts.team.team_json.read_bytes()
        self.refused(self.worker("whiteboard", "team", "off"), "author_mismatch")
        self.assertEqual(self.ts.team.team_json.read_bytes(), before)
        operator.grant(self.ts.session, "alpha", WORKER, ttl_s=0)
        payload = self.ok(self.worker("whiteboard", "viz", "off"))
        self.assertEqual(payload["by"], "delegate")
        self.assertTrue(any(e["event"] == "operator_action" for e in self.audit()))


class OpenStopTests(Rig):
    def setUp(self):
        super().setUp()
        self.start = mock.patch("herdr_team.whiteboard_server.start", return_value=dict(STARTED)).start()
        self.browser = mock.patch.object(cmd_whiteboard, "OPEN_BROWSER", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def test_open_refuses_while_off_and_starts_nothing(self):
        err = self.refused(self.human("whiteboard", "open"), "whiteboard_off")
        self.assertIn("whiteboard enable", err["message"])
        self.start.assert_not_called()
        self.browser.assert_not_called()

    def test_the_operator_opens_the_page_in_the_browser(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        payload = self.ok(self.human("whiteboard", "open"))
        self.assertEqual((payload["url"], payload["port"], payload["browser"], payload["ssh_hint"]), (STARTED["url"], 4567, "opened", None))
        self.browser.assert_called_once_with(STARTED["url"])
        author = self.start.call_args.args[2]
        self.assertTrue(author.trusted_human)
        self.assertFalse(self.start.call_args.kwargs.get("open_browser", False))

    def test_over_ssh_or_without_a_browser_it_prints_the_url_and_a_forward_line(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        payload = self.ok(self.human("whiteboard", "open", SSH_CONNECTION="10.0.0.2 51000 10.0.0.1 22", USER="op"))
        self.assertEqual(payload["browser"], "ssh")
        self.assertRegex(payload["ssh_hint"], r"^ssh -L 4567:127\.0\.0\.1:4567 op@\S+$")
        self.browser.assert_not_called()
        self.browser.return_value = False
        payload = self.ok(self.human("whiteboard", "open"))
        self.assertEqual(payload["browser"], "failed")
        self.assertTrue(payload["ssh_hint"])
        code, out, _err = run_cli(["whiteboard", "open", "--no-browser"], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0)
        self.assertIn(STARTED["url"], out)
        self.assertIn("works once", out)
        self.assertEqual(self.browser.call_count, 1)

    def test_open_takes_the_engine_and_the_default_is_the_pages(self):
        """Canvas v2 phase 6, 1.4: no --engine leaves the URL without one (the page opens v2); --engine v1 opens the classic
        canvas; an engine asked for is carried by the one-use link (the ticket's redirect keeps it)."""
        features.set_layer(self.ts.session, True, "human", "cli")
        payload = self.ok(self.human("whiteboard", "open", "--no-browser"))
        self.assertNotIn("engine=", payload["url"])
        payload = self.ok(self.human("whiteboard", "open", "--no-browser", "--engine", "v1"))
        self.assertEqual(payload["url"], STARTED["url"] + ("&" if "?" in STARTED["url"] else "?") + "engine=v1")
        payload = self.ok(self.human("whiteboard", "open", "--engine", "v2"))
        self.assertTrue(payload["url"].endswith("engine=v2"))
        self.browser.assert_called_with(payload["url"])
        self.assertEqual(cmd_whiteboard.with_engine("http://127.0.0.1:9/?ticket=abc", None), "http://127.0.0.1:9/?ticket=abc")
        self.assertEqual(cmd_whiteboard.with_engine("http://127.0.0.1:9/?ticket=abc", "v1"), "http://127.0.0.1:9/?ticket=abc&engine=v1")
        code, _out, _err = run_cli(["whiteboard", "open", "--engine", "v3"], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 2)

    def test_members_and_hooks_cannot_open_or_stop_it(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.refused(self.worker("whiteboard", "open"), "author_mismatch")
        self.refused(self.worker("whiteboard", "stop"), "author_mismatch")
        self.refused(self.human("whiteboard", "open", HERDR_PLUGIN_ID="herdr-synapse", HERDR_PLUGIN_EVENT="pane.closed"), "author_mismatch")
        self.start.assert_not_called()

    def test_the_plugin_action_hands_off_to_the_popup_which_opens_as_the_operator(self):
        features.set_layer(self.ts.session, True, "human", "cli")
        self.api.set_response("plugin.pane.open", {"type": "ok"})
        payload = self.ok(self.human("whiteboard", "open", HERDR_PLUGIN_ID="herdr-synapse", HERDR_PLUGIN_ACTION_ID="whiteboard"))
        self.assertTrue(payload["popup"])
        self.start.assert_not_called()
        [params] = [p for m, p in self.api.calls if m == "plugin.pane.open"]
        self.assertEqual((params["plugin_id"], params["entrypoint"]), ("herdr-synapse", "whiteboard"))
        payload = self.ok(self.human("whiteboard", "open", "--popup", HERDR_PLUGIN_ID="herdr-synapse", HERDR_PLUGIN_ENTRYPOINT_ID="whiteboard"))
        self.assertEqual(payload["browser"], "opened")
        self.assertTrue(self.start.call_args.args[2].trusted_human, "the popup tier is the operator, so the page can write")

    def test_stop(self):
        with mock.patch("herdr_team.whiteboard_server.stop", return_value=True) as stop:
            self.assertTrue(self.ok(self.human("whiteboard", "stop"))["stopped"])
        stop.assert_called_once()
        with mock.patch("herdr_team.whiteboard_server.stop", return_value=False):
            self.assertFalse(self.ok(self.human("whiteboard", "stop"))["stopped"])

    def test_the_hidden_serve_command_runs_only_while_on(self):
        with mock.patch("herdr_team.whiteboard_server.serve", return_value=0) as serve:
            self.refused(self.human("whiteboard-serve"), "whiteboard_off")
            serve.assert_not_called()
            features.set_layer(self.ts.session, True, "human", "cli")
            code, _out, err = run_cli(["whiteboard-serve", "--port", "4600"], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0, err)
        self.assertEqual(serve.call_args.kwargs["port"], 4600)
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text):
            cli.main(["--help"], env={}, stdout=help_text, stderr=io.StringIO())
        self.assertIn("whiteboard", help_text.getvalue())
        self.assertNotIn("whiteboard-serve", help_text.getvalue())


class ClearPurgeViewsTests(Rig):
    def test_clear_is_the_operators_or_a_delegates_and_works_while_off(self):
        with mock.patch("herdr_team.canvas.clear", return_value={"archived": "/x/archive/1", "version": 0}) as clear:
            self.refused(self.worker("whiteboard", "clear"), "author_mismatch")
            payload = self.ok(self.human("whiteboard", "clear"))
        self.assertEqual(payload["archived"], "/x/archive/1")
        self.assertEqual(clear.call_args.args[2], "human")

    def test_purge_is_the_operator_in_person_and_asks_first(self):
        with mock.patch("herdr_team.canvas.purge", return_value=True) as purge:
            operator.grant(self.ts.session, "alpha", WORKER, ttl_s=0)
            self.refused(self.worker("whiteboard", "purge", "--yes"), "author_mismatch")
            self.refused(self.human("whiteboard", "purge"), "confirmation_required")
            purge.assert_not_called()
            payload = self.ok(self.human("whiteboard", "purge", "--yes"))
            self.assertEqual(payload["purged"], ["alpha"])
            self.ok(self.human("whiteboard", "purge", "--all-teams", "--yes"))
        self.assertEqual(purge.call_count, 2)
        self.assertTrue(any(e["event"] == "whiteboard_purge" for e in self.audit()))

    def test_views_print_every_part(self):
        payload = self.ok(self.worker("whiteboard", "views"))
        self.assertEqual(set(payload), {"team", "generated_at", "work", "facts", "topology", "timeline", "lanes", "canvas"})
        self.assertIsNone(payload["canvas"], "no canvas summary while it is off")


class WhereTheSwitchShowsTests(Rig):
    def me(self):
        return self.ok(self.worker("me"))

    def test_me_orient_and_the_briefing_say_off_by_default(self):
        whiteboard = self.me()["whiteboard"]
        self.assertEqual(whiteboard["line"], "whiteboard: off")
        self.assertEqual((whiteboard["on"], whiteboard["layer"], whiteboard["version"]), (False, False, None))
        self.assertIn("whiteboard: off", self.ok(self.worker("orient"))["text"].splitlines())
        code, out, err = run_cli(["--team", "alpha", "brief", WORKER, "--format", "context"], env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0, err)
        self.assertIn("whiteboard: off", out.splitlines())

    def test_on_they_name_viz_the_version_and_the_reference(self):
        whiteboard_on(self.ts.session, self.ts.team)
        with mock.patch("herdr_team.canvas.current_version", return_value=42):
            whiteboard = self.me()["whiteboard"]
            orient = self.ok(self.worker("orient"))["text"]
        self.assertEqual(whiteboard["version"], 42)
        self.assertEqual(whiteboard["line"], "whiteboard: on · live visuals on · canvas v42; how to draw: herdr-synapse skill get --reference canvas")
        self.assertIn(whiteboard["line"], orient)
        self.assertIn(whiteboard["line"], render.render_me(dict(self.me(), whiteboard=whiteboard), {}))
        features.set_team(self.ts.team, viz=False)
        self.assertIn("live visuals off", self.me()["whiteboard"]["line"])
        features.set_team(self.ts.team, enabled=False)
        self.assertEqual(self.me()["whiteboard"]["line"], "whiteboard: off for this team")

    def test_a_broken_canvas_never_breaks_me(self):
        whiteboard_on(self.ts.session, self.ts.team)
        with mock.patch("herdr_team.canvas.current_version", side_effect=ValueError("corrupt")):
            whiteboard = self.me()["whiteboard"]
        self.assertIsNone(whiteboard["version"])
        self.assertTrue(whiteboard["line"].startswith("whiteboard: on · live visuals on; how to draw"))

    def test_the_canvas_reference_is_served_only_while_the_callers_canvas_is_on(self):
        listing = self.ok(self.worker("skill", "get", "--list"))
        self.assertNotIn("canvas", listing["references"])
        self.refused(self.worker("skill", "get", "--reference", "canvas"), "reference_unknown")
        whiteboard_on(self.ts.session, self.ts.team)
        self.assertIn("canvas", self.ok(self.worker("skill", "get", "--list"))["references"])
        text = self.ok(self.worker("skill", "get", "--reference", "canvas"))["text"]
        self.assertIn("requests, never orders", text)
        features.set_team(self.ts.team, enabled=False)
        self.assertNotIn("canvas", self.ok(self.worker("skill", "get", "--list"))["references"])
        # nothing resolvable counts as off
        code, out, err = run_cli(["skill", "get", "--list", "--json"], {})
        self.assertEqual(code, 0, err)
        self.assertNotIn("canvas", json.loads(out)["references"])

    def test_a_person_with_no_team_sees_the_reference_while_the_layer_is_on(self):
        with TempState(write_team=False) as ts:
            env = env_no_daemon(ts)
            listing = json_out(run_cli(["--json", "skill", "get", "--list"], env, self.api))[1]
            self.assertNotIn("canvas", listing["references"])
            features.set_layer(ts.session, True, "human", "cli")
            listing = json_out(run_cli(["--json", "skill", "get", "--list"], env, self.api))[1]
            self.assertIn("canvas", listing["references"])


class DoctorTests(Rig):
    def doctor(self, api=None):
        api = api or self.api
        api.unreachable = True
        return self.ok(self.human("doctor", "--no-fix", api=api))

    def test_doctor_reports_the_three_levels_the_server_and_the_watch(self):
        report = self.doctor()["whiteboard"]
        self.assertFalse(report["session"]["enabled"])
        self.assertEqual(report["teams"]["alpha"]["viz"], True)
        self.assertEqual(report["page"], {"running": False})
        self.assertEqual(report["watched"], [])
        server = {"pid": 4242, "port": 4567, "writable": True, "url": "http://127.0.0.1:4567/", "started_at": "2025-12-01T00:00:00.000Z",
                  "page_at": "2026-01-01T00:00:00.000Z", "streams": 0}
        (self.ts.config_dir / "config.toml").write_text('[ui.sidebar.agents]\nrows = [[{ token = "$team_c1", fg = "#fb4934" }]]\n')
        watched = [{"terminal_id": "term_x", "pane_id": "w3:p2", "name": "churn-analyst", "kind": "claude", "team": None}]
        features.set_layer(self.ts.session, True, "human", "cli")
        with mock.patch("herdr_team.whiteboard_server.status", return_value=server), mock.patch("herdr_team.activity.watched", return_value=watched):
            payload = self.doctor(live_api(list(FAKE_AGENTS)))
        report = payload["whiteboard"]
        self.assertTrue(report["page"]["running"])
        self.assertEqual(report["page"]["pid"], 4242)
        self.assertGreater(report["page"]["idle_minutes"], 24 * 60)
        self.assertEqual(report["watched"][0]["name"], "churn-analyst")
        self.assertTrue(any("run for a day with no page open" in w for w in payload["warnings"]))
        self.assertTrue(any("$team_doing" in w for w in payload["warnings"]))
        (self.ts.config_dir / "config.toml").write_text(cmd_misc.SIDEBAR_SNIPPET)
        with mock.patch("herdr_team.activity.watched", return_value=watched):
            self.assertFalse(any("$team_doing" in w for w in self.doctor(live_api(list(FAKE_AGENTS)))["warnings"]))


    def test_idle_time_follows_the_servers_page_record(self):
        now = roster.parse_iso("2026-01-02T00:00:00.000Z")
        rec = {"started_at": "2025-12-01T00:00:00.000Z", "page_at": "2026-01-01T23:00:00.000Z", "streams": 0}
        self.assertEqual(cmd_whiteboard.server_idle_s(rec, now), 3600.0)
        self.assertEqual(cmd_whiteboard.server_idle_s(dict(rec, streams=1), now), 0.0, "a connected page is never idle")
        self.assertGreater(cmd_whiteboard.server_idle_s({"started_at": "2025-12-01T00:00:00.000Z"}, now), 24 * 3600)
        self.assertIsNone(cmd_whiteboard.server_idle_s({}, now))


class KeysManifestSetupTests(unittest.TestCase):
    def test_prefix_a_opens_the_whiteboard_and_the_bindings_stay_distinct(self):
        self.assertEqual(cmd_misc.KEYS["whiteboard"], "prefix+a")
        self.assertEqual(cmd_misc.KEY_ACTIONS[-1], "whiteboard")
        self.assertEqual(set(cmd_misc.KEY_ACTIONS), set(cmd_misc.KEYS))
        self.assertEqual(len(set(cmd_misc.KEYS.values())), len(cmd_misc.KEYS), "two actions on one key")
        self.assertEqual(cmd_misc.KEY_DESCRIPTIONS["whiteboard"], "open the team whiteboard page")
        snippet = cmd_misc.keys_snippet()
        self.assertIn('key = "prefix+a"\ntype = "plugin_action"\ncommand = "herdr-synapse.whiteboard"', snippet)
        # Herdr's default prefix letters (src/config/model.rs, 0.9.x): b c e g h j k l n o p q r s v w x z
        default = "[keys]\n" + "".join('# action_{0} = "prefix+{0}"\n'.format(letter) for letter in "bceghjklnopqrsvwxz")
        self.assertEqual(cmd_misc.key_collisions(default, None), [])
        self.assertEqual([c["action"] for c in cmd_misc.key_collisions('[keys]\n# other = "prefix+a"\n', None)], ["whiteboard"])

    def test_the_manifest_offers_the_action_and_its_popup(self):
        text = (PLUGIN_ROOT / "herdr-plugin.toml").read_text(encoding="utf-8")
        self.assertIn('[[actions]]\nid = "whiteboard"\ntitle = "Open the team whiteboard"\ncontexts = ["global"]\ncommand = ["./bin/herdr-synapse", "whiteboard", "open"]', text)
        self.assertIn('[[panes]]\nid = "whiteboard"', text)
        self.assertIn('command = ["./bin/herdr-synapse", "whiteboard", "open", "--popup"]', text)
        self.assertEqual(cmd_whiteboard.POPUP_ENTRYPOINT, "whiteboard")
        from herdr_team import VERSION, SKILL_VERSION

        self.assertEqual(VERSION, "0.22.0")
        self.assertEqual(SKILL_VERSION, 12)
        self.assertIn('version = "0.22.0"', text)

    def test_the_page_carries_the_same_version(self):
        """Every version file of canvas-v2-phase6.md 4 in one assertion: a bump that forgets the page (or forgets to
        rebuild the committed ``web/dist``) fails here instead of shipping a page that names the old release."""
        from herdr_team import VERSION

        web = PLUGIN_ROOT / "web"
        self.assertEqual(json.loads((web / "package.json").read_text(encoding="utf-8"))["version"], VERSION)
        lock = json.loads((web / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["version"], VERSION, "web/package-lock.json")
        self.assertEqual(lock["packages"][""]["version"], VERSION, 'web/package-lock.json packages[""]')
        manifest = web / "dist" / "MANIFEST.json"
        if manifest.exists():  # a checkout without the built page still runs the suite
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["version"], VERSION,
                             "web/dist is built from an older version; cd web && npm run build")

    def test_every_other_version_file_carries_the_same_version(self):
        """QA phase 6, 4.4: the page files were tied to ``herdr_team.VERSION`` but the plugin manifest and the
        README's status line were only spelled out, so a bump that forgot one of them passed every gate. Every file
        that names the version is read from the one constant here."""
        from herdr_team import SKILL_VERSION, VERSION

        manifest = (PLUGIN_ROOT / "herdr-plugin.toml").read_text(encoding="utf-8")
        found = re.search(r'^version = "([^"]+)"', manifest, re.M)
        self.assertEqual(found and found.group(1), VERSION, "herdr-plugin.toml names another version")
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Current source version: {}, skill v{}.".format(VERSION, SKILL_VERSION), readme,
                      "README.md's Status line names another version or skill")

    def test_the_sidebar_snippet_has_a_doing_row(self):
        self.assertIn('[{ token = "$team_doing", fg = "#a6e3a1" }],', cmd_misc.SIDEBAR_SNIPPET)
        self.assertEqual(cmd_misc.SIDEBAR_SNIPPET.count("$team_doing"), 2, "rows and the claude rows")
        if tomllib is not None:
            doc = tomllib.loads(cmd_misc.SIDEBAR_SNIPPET)
            for rows in [doc["ui"]["sidebar"]["agents"]["rows"]] + list(doc["ui"]["sidebar"]["agents"]["rows_by_agent"].values()):
                self.assertIn([{"token": "$team_doing", "fg": "#a6e3a1"}], rows)

    def test_the_skill_line_and_the_reference(self):
        from herdr_team import paths

        skill = paths.skill_file().read_text(encoding="utf-8")
        self.assertIn("If `me` says `whiteboard: on`", skill)
        self.assertIn("herdr-synapse skill get --reference canvas", skill)
        self.assertLessEqual(len(skill.splitlines()), 150)
        reference = (paths.skill_guides_dir() / "references" / "canvas.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(reference.splitlines()), 120)
        for needle in ("canvas_look", "canvas_draw", "canvas look", "canvas draw", "claim", "legend", "requests, never orders", "--since last"):
            self.assertIn(needle, reference, needle)
        # Phase 2 moved the long examples into canvas-blocks and canvas-diagrams, phase 3 the charts into canvas-charts: together
        # they cover every layer.
        guides = "".join((paths.skill_guides_dir() / "references" / name).read_text(encoding="utf-8")
                         for name in ("canvas.md", "canvas-blocks.md", "canvas-diagrams.md", "canvas-charts.md"))
        examples = re.findall(r"```json\n(.*?)```", guides, re.S)
        ops = []
        for block in examples:
            try:
                chunks = [json.loads(block)]
            except ValueError:
                chunks = [json.loads(line) for line in block.strip().splitlines()]  # one op per line
            for parsed in chunks:
                ops.extend(parsed["ops"] if "ops" in parsed else [parsed])
        layers = {op["op"] for op in ops}
        self.assertTrue({"shape", "arrow", "frame", "pen", "comment", "svg", "graph", "mermaid", "chart", "viz"} <= layers, layers)
        self.assertTrue({"section", "card", "sticky", "callout", "table", "kanban", "timeline", "mindmap", "place", "patch"} <= layers, layers)
        self.assertTrue(all(op.get("intent") for op in ops), "every example carries an intent")
        from herdr_team import canvas

        parsed_ops, _atomic = canvas.parse_batch({"ops": ops})
        self.assertEqual(len(parsed_ops), len(ops))


if __name__ == "__main__":
    unittest.main()

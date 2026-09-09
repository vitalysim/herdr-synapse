"""Teams that talk to each other through their managers (0.15.0). Every test here fails against 0.14.1."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from support import FAKE_AGENTS, FakeApi, TempState, fake_agent
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon
from test_workspace_team import add_team

from herdr_team import cmd_hooks, links, picker, render, store, tui_model
from herdr_team import daemon as D
from herdr_team.errors import HerdrTeamError

A_MANAGER = "alpha-reviewer"   # codex, w2:p1, term_r1
A_PEER = "alpha-worker"        # claude, w2:p2
B_MANAGER = "beta-worker"      # claude, w7:p1, term_beta (add_team)
BETA_AGENT = fake_agent("w7:p1", "term_beta", "claude", B_MANAGER)


def set_manager(ts, team, name, flag=True):
    path = ts.session.team(team).team_json
    doc = store.read_json(path)
    for m in doc["members"]:
        if m["name"] == name:
            m["manager"] = flag
    store.write_json(path, doc)


class Rig(unittest.TestCase):
    """Two teams, both with a manager, linked."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        add_team(self.ts, "beta", "w7")
        set_manager(self.ts, "alpha", A_MANAGER)
        set_manager(self.ts, "beta", B_MANAGER)
        self.api = live_api(list(FAKE_AGENTS) + [BETA_AGENT])

    def link(self):
        """Through the CLI, so both boards carry the ``link_established`` announcement as they would live."""
        code, payload, err = self.cli(["link", "alpha", "beta"])
        assert code == 0, err
        return links.find(self.ts.session, "alpha", "beta")

    def cli(self, argv, env=None, api=None):
        return json_out(run_cli(["--json"] + list(argv), env if env is not None else env_no_daemon(self.ts), api or self.api))

    def board(self, team):
        return store.BoardStore(self.ts.session.team(team)).read()

    def records(self, team, event):
        return [r for r in self.board(team) if r.get("event") == event]


# --------------------------------------------------------------------------
# the registry


class RegistryTests(Rig):
    def test_link_unlink_list_round_trip_and_one_id_whatever_the_order(self):
        code, payload, err = self.cli(["link", "beta", "alpha", "--note", "consult on shared CVEs"])
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["link"]["id"], payload["created"], payload["state"]), ("alpha--beta", True, "active"))
        self.assertEqual(payload["managers"], {"alpha": A_MANAGER, "beta": B_MANAGER})
        code, payload, err = self.cli(["link", "alpha", "beta"])
        self.assertEqual((code, payload["created"]), (0, False), "idempotent, and announces nothing twice")
        self.assertEqual(len(self.records("alpha", "link_established")), 1)
        code, payload, err = self.cli(["links"])
        self.assertEqual([r["id"] for r in payload["links"]], ["alpha--beta"])
        code, payload, err = self.cli(["unlink", "alpha", "beta"])
        self.assertEqual((code, payload["link"]["status"]), (0, "broken"))
        self.assertEqual(self.cli(["links"])[1]["links"], [])
        self.assertEqual([r["status"] for r in self.cli(["links", "--all"])[1]["links"]], ["broken"])
        code, _payload, err = self.cli(["unlink", "alpha", "beta"])
        self.assertEqual((code, err["code"]), (1, "link_broken"))

    def test_both_announcements_name_the_managers_and_how_to_post(self):
        self.cli(["link", "alpha", "beta"])
        for team, other, manager, other_manager in (("alpha", "beta", A_MANAGER, B_MANAGER), ("beta", "alpha", B_MANAGER, A_MANAGER)):
            rec = self.records(team, "link_established")[0]
            self.assertEqual(rec["to"], [manager, "all"])
            self.assertEqual((rec["other_team"], rec["other_manager"]), (other, other_manager))
            self.assertIn("post --to team:{}".format(other), rec["text"])
        self.cli(["unlink", "alpha", "beta"])
        self.assertEqual(len(self.records("beta", "link_broken")), 1)

    def test_a_team_without_a_manager_cannot_be_linked_and_pauses_a_link_later(self):
        set_manager(self.ts, "beta", B_MANAGER, False)
        code, _payload, err = self.cli(["link", "alpha", "beta"])
        self.assertEqual((code, err["code"], err["team"]), (1, "link_no_manager", "beta"))
        set_manager(self.ts, "beta", B_MANAGER, True)
        self.link()
        set_manager(self.ts, "beta", B_MANAGER, False)
        self.assertEqual(self.cli(["links"])[1]["links"][0]["state"], "paused: beta has no manager")
        with self.assertRaises(HerdrTeamError) as caught:
            links.require_endpoint(self.ts.session, "alpha", "beta")
        self.assertEqual(caught.exception.code, "link_no_manager")

    def test_a_member_may_not_link_and_a_team_cannot_link_itself(self):
        code, _payload, err = self.cli(["link", "alpha", "beta"], env=env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1"))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        with self.assertRaises(HerdrTeamError) as caught:
            links.link(self.ts.session, "alpha", "alpha", "human")
        self.assertEqual(caught.exception.code, "link_self")

    def test_dissolving_a_team_breaks_its_links_and_tells_the_other_side(self):
        self.link()
        self.api.set_response("agent.view.set", {"type": "ok"})
        self.api.set_response("agent.view.clear", {"type": "ok"})
        code, payload, err = self.cli(["dissolve", "beta", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["links_broken"], ["alpha"])
        self.assertEqual(links.active_for(self.ts.session, "alpha"), [])
        broken = self.records("alpha", "link_broken")
        self.assertEqual(len(broken), 1)
        self.assertIn("dissolved", broken[0]["text"])
        self.assertEqual(broken[0]["to"], [A_MANAGER, "all"])


# --------------------------------------------------------------------------
# posting across


class CrossPostTests(Rig):
    def setUp(self):
        super().setUp()
        self.link()

    def as_manager(self):
        return env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1")

    def test_the_manager_writes_the_delivered_copy_there_and_the_mirror_here(self):
        code, payload, err = self.cli(["post", "--to", "team:beta", "--kind", "request", "can you review CVE-093?"], env=self.as_manager())
        self.assertEqual(code, 0, err)
        link = payload["link"]
        self.assertEqual((link["other_team"], link["other_manager"]), ("beta", B_MANAGER))
        delivered = next(r for r in self.board("beta") if r["seq"] == link["delivered_seq"])
        self.assertEqual((delivered["from"], delivered["from_team"], delivered["to"], delivered["kind"]), (A_MANAGER, "alpha", [B_MANAGER], "request"))
        self.assertEqual((delivered["link"]["id"], delivered["link"]["from_team"], delivered["link"]["to_team"]), (link["id"], "alpha", "beta"))
        self.assertNotIn("mirror", delivered["link"])
        mirror = next(r for r in self.board("alpha") if r["seq"] == link["mirror_seq"])
        self.assertEqual((mirror["from"], mirror["to"], mirror["link"]["id"], mirror["link"]["mirror"]), (A_MANAGER, ["team:beta"], link["id"], True))
        self.assertEqual(payload["seq"], link["mirror_seq"])

    def test_a_plain_member_is_pointed_at_its_manager_and_the_operator_may_speak(self):
        code, _payload, err = self.cli(["post", "--to", "team:beta", "hello"], env=env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2"))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertIn(A_MANAGER, err["message"])
        self.assertEqual(len(self.board("beta")), 1, "nothing was delivered")  # the link_established only
        code, payload, err = self.cli(["--team", "alpha", "post", "--to", "team:beta", "from the operator"])
        self.assertEqual(code, 0, err)
        self.assertEqual(next(r for r in self.board("beta") if r["seq"] == payload["link"]["delivered_seq"])["from"], "human")

    def test_no_link_broken_link_and_a_missing_manager_are_named(self):
        links.unlink(self.ts.session, "alpha", "beta", "human")
        code, _payload, err = self.cli(["post", "--to", "team:beta", "x"], env=self.as_manager())
        self.assertEqual((code, err["code"]), (1, "link_broken"))
        add_team(self.ts, "gamma", "w8")
        code, _payload, err = self.cli(["post", "--to", "team:gamma", "x"], env=self.as_manager())
        self.assertEqual((code, err["code"]), (1, "link_missing"))
        self.link()
        set_manager(self.ts, "beta", B_MANAGER, False)
        code, _payload, err = self.cli(["post", "--to", "team:beta", "x"], env=self.as_manager())
        self.assertEqual((code, err["code"], err["team"]), (1, "link_no_manager", "beta"))

    def test_a_team_recipient_goes_alone_and_never_waits_or_attaches(self):
        for argv, code_wanted in ((["post", "--to", "team:beta,alpha-worker", "x"], 2), (["post", "--to", "team:beta", "--wait", "x"], 2),
                                  (["post", "--to", "team:beta", "--interrupt", "x"], 2), (["post", "--to", "team:alpha", "x"], 2)):
            code, _payload, err = self.cli(argv, env=self.as_manager())
            self.assertEqual((code, err["code"]), (code_wanted, "usage"), argv)
        self.assertEqual(len(self.board("beta")), 1)

    def test_a_reply_threads_on_both_boards(self):
        code, first, err = self.cli(["post", "--to", "team:beta", "can you take 115?"], env=self.as_manager())
        self.assertEqual(code, 0, err)
        # beta's manager answers from its own pane, replying to its local copy
        beta_env = env_no_daemon(self.ts, HERDR_PANE_ID="w7:p1")
        code, reply, err = self.cli(["--team", "beta", "post", "--to", "team:alpha", "--reply-to", str(first["link"]["delivered_seq"]), "yes, on it"], env=beta_env)
        self.assertEqual(code, 0, err)
        self.assertEqual(reply["link"]["reply_to_id"], first["link"]["id"])
        landed = next(r for r in self.board("alpha") if r["seq"] == reply["link"]["delivered_seq"])
        self.assertEqual(landed["reply_to"], first["link"]["mirror_seq"], "re#N points at alpha's own copy of the question")
        self.assertEqual((landed["from"], landed["from_team"], landed["to"]), (B_MANAGER, "beta", [A_MANAGER]))

    def test_the_two_locks_are_taken_in_team_name_order(self):
        order = []
        real = store.team_lock

        def spy(team, timeout=None):
            order.append(os.path.basename(os.fspath(getattr(team, "root", team))))
            return real(team) if timeout is None else real(team, timeout)

        with mock.patch.object(store, "team_lock", spy):
            code, _payload, err = self.cli(["post", "--to", "team:beta", "x"], env=self.as_manager())
        self.assertEqual(code, 0, err)
        taken = [t for t in order if t in ("alpha", "beta")]
        self.assertEqual(taken[-2:], ["alpha", "beta"])

    def test_board_teams_is_the_inter_team_lens(self):
        self.cli(["post", "--to", "team:beta", "x"], env=self.as_manager())
        self.cli(["post", "--to", A_PEER, "local"], env=self.as_manager())
        code, payload, err = self.cli(["--team", "alpha", "board", "--teams"])
        self.assertEqual(code, 0, err)
        self.assertEqual([r["text"] for r in payload["posts"]], ["x"])
        self.assertTrue(all(isinstance(r.get("link"), dict) for r in payload["posts"]))


# --------------------------------------------------------------------------
# the daemon


class DaemonTests(Rig):
    def setUp(self):
        super().setUp()
        self.link()
        self.d, self.api, self.clock = make_daemon(self.ts, api=self.api)
        self.d.on_connected()
        self.alpha = self.d.teams["alpha"]
        self.beta = self.d.teams["beta"]

    def ingest(self):
        self.d.scan_teams(force=True)
        self.d.tail_boards()

    def test_the_delivered_copy_nudges_the_receiving_manager_only_and_the_mirror_nobody(self):
        code, payload, err = self.cli(["post", "--to", "team:beta", "hello"], env=env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1"))
        self.assertEqual(code, 0, err)
        self.ingest()
        # both managers already hold the link_established announcement (seq 1 on each board)
        self.assertIn(payload["link"]["delivered_seq"], self.beta.pending[B_MANAGER].seqs)
        self.assertNotIn(A_PEER, self.alpha.pending)
        self.assertNotIn(payload["link"]["mirror_seq"], self.alpha.pending[A_MANAGER].seqs, "the mirror wakes nobody on the sending team")

    def test_link_established_wakes_both_managers(self):
        self.cli(["unlink", "alpha", "beta"])
        self.cli(["link", "alpha", "beta"])
        self.ingest()
        self.assertIn(A_MANAGER, self.alpha.pending)
        self.assertIn(B_MANAGER, self.beta.pending)
        self.assertNotIn(A_PEER, self.alpha.pending)

    def test_a_read_by_the_receiving_manager_is_receipted_on_the_sending_board_once(self):
        from herdr_team.cmd_board import cursor_advance

        code, payload, err = self.cli(["post", "--to", "team:beta", "hello"], env=env_no_daemon(self.ts, HERDR_PANE_ID="w2:p1"))
        self.assertEqual(code, 0, err)
        self.ingest()
        delivered = payload["link"]["delivered_seq"]
        self.assertIn(delivered, self.beta.link_inbox)
        self.d.poll_link_receipts(self.clock() * 1000)
        self.assertEqual([r for r in self.board("alpha") if r.get("event") == "link_read"], [], "unread: no receipt yet")
        cursor_advance(self.ts.session.team("beta"), B_MANAGER, delivered, "term_beta", "cli")
        self.clock.advance(10)
        self.d.poll_link_receipts(self.clock() * 1000)
        receipts = [r for r in self.board("alpha") if r.get("event") == "link_read"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual((receipts[0]["to"], receipts[0]["link_id"], receipts[0]["reader"]), (["team:beta"], payload["link"]["id"], B_MANAGER))
        self.assertNotIn(delivered, self.beta.link_inbox)
        self.clock.advance(10)
        self.d.poll_link_receipts(self.clock() * 1000)
        self.assertEqual(len([r for r in self.board("alpha") if r.get("event") == "link_read"]), 1, "once")
        # the receipt nudges nobody on either side
        self.ingest()
        receipt_seq = receipts[0]["seq"]
        self.assertTrue(all(receipt_seq not in p.seqs for p in self.alpha.pending.values()))
        self.assertNotIn(A_PEER, self.alpha.pending)

    def test_who_json_carries_the_links(self):
        who = self.d.build_who()
        self.assertEqual([l["team"] for l in who["teams"]["alpha"]["links"]], ["beta"])
        self.assertEqual(who["teams"]["beta"]["links"][0]["manager"], A_MANAGER)


# --------------------------------------------------------------------------
# the console


class ConsoleTests(unittest.TestCase):
    def entry(self, **rec):
        base = {"seq": 5, "from": A_MANAGER, "kind": "request", "to": [B_MANAGER], "text": "hi", "ts": "2026-09-08T10:00:00Z",
                "origin": {"via": "cli", "verified": True}}
        base.update(rec)
        return tui_model.feed_entry(base, ascii_only=True, width=120)

    def test_the_lenses_split_link_traffic_from_the_rest(self):
        link = {"id": "m1", "from_team": "alpha", "to_team": "beta"}
        crossed = self.entry(link=link, from_team="alpha")
        local = self.entry()
        self.assertTrue(tui_model.filter_matches(crossed, "teams") and not tui_model.filter_matches(local, "teams"))
        self.assertTrue(tui_model.filter_matches(local, "team") and not tui_model.filter_matches(crossed, "team"))
        self.assertIn("teams", tui_model.FILTERS)
        self.assertIn("team", tui_model.FILTERS)

    def test_a_link_line_names_the_senders_team_and_wears_the_glyph(self):
        crossed = self.entry(link={"id": "m1", "from_team": "alpha", "to_team": "beta"}, from_team="alpha")
        self.assertIn("<->", crossed["line"])
        self.assertIn("alpha/{}->{}".format(A_MANAGER, B_MANAGER), crossed["line"])
        mirror = self.entry(link={"id": "m1", "from_team": "alpha", "to_team": "beta", "mirror": True}, from_team="alpha", to=["team:beta"])
        self.assertIn("{}->team:beta".format(A_MANAGER), mirror["line"], "the mirror is our own manager's line")

    def test_the_receipt_shows_on_the_mirror(self):
        mirror = {"seq": 7, "from": A_MANAGER, "kind": "request", "to": ["team:beta"], "text": "hi", "origin": {"via": "cli", "verified": True},
                  "link": {"id": "m1", "from_team": "alpha", "to_team": "beta", "mirror": True}}
        receipt = {"seq": 8, "from": "system", "kind": "system", "event": "link_read", "to": ["team:beta"], "text": "read", "link_id": "m1", "reader": B_MANAGER}
        receipts = tui_model.derive_receipts([mirror, receipt], {}, [{"name": A_MANAGER, "kind": "codex"}])
        self.assertEqual(receipts[7]["read_by"], B_MANAGER)
        line = tui_model.feed_entry(mirror, receipts=receipts, ascii_only=True, width=120)["line"]
        self.assertIn("read by {}".format(B_MANAGER), line)

    def test_slash_team_is_a_post_and_the_menu_offers_linked_teams(self):
        intent = tui_model.parse_input_line("/team beta can you take 115?", "alpha")
        self.assertEqual((intent.kind, intent.args["to"], intent.args["text"]), ("post", ["team:beta"], "can you take 115?"))
        self.assertEqual(tui_model.parse_input_line("/link beta", "alpha").kind, "link")
        self.assertEqual(tui_model.parse_input_line("/unlink beta", "alpha").args["other"], "beta")
        self.assertEqual(tui_model.parse_input_line("/links", "alpha").kind, "links")
        rows = tui_model.mention_candidates([{"name": A_PEER, "kind": "claude", "role": "w"}], "te", links=[{"team": "beta", "manager": B_MANAGER, "state": "active"}, {"team": "gamma", "manager": None, "state": "paused: gamma has no manager"}])
        self.assertEqual([r["insert"] for r in rows], ["team:beta"], "a paused link is not offered")
        for command in ("/team", "/links", "/link", "/unlink"):
            self.assertIn(command, tui_model.SLASH_COMMANDS)
            self.assertIn(command, tui_model.SLASH_USAGE)


# --------------------------------------------------------------------------
# the teams view


class PickerTests(unittest.TestCase):
    ALPHA = [{"name": A_MANAGER, "role": "reviewer", "kind": "codex", "pane_id": "w2:p1", "status": "active", "manager": True, "terminal_id": "term_r1"},
             {"name": A_PEER, "role": "worker", "kind": "claude", "pane_id": "w2:p2", "status": "active", "terminal_id": "term_w1"}]
    BETA = [{"name": B_MANAGER, "role": "lead", "kind": "claude", "pane_id": "w7:p1", "status": "active", "manager": True, "terminal_id": "term_beta"}]
    GAMMA = [{"name": "gamma-x", "role": "x", "kind": "codex", "pane_id": "w8:p1", "status": "active", "terminal_id": "term_g1"}]

    def model(self, linked=True):
        from test_tui_model import picker_model

        m = picker_model({"alpha": self.ALPHA, "beta": self.BETA, "gamma": self.GAMMA}, focused=None)
        m.managers = {"alpha": A_MANAGER, "beta": B_MANAGER, "gamma": None}
        m.links = {"alpha": [{"team": "beta", "manager": B_MANAGER, "state": "active", "id": "alpha--beta"}] if linked else [], "beta": [], "gamma": []}
        return m

    def test_the_manager_row_is_starred_and_styled_and_the_header_shows_links(self):
        m = self.model()
        lines = tui_model.picker_lines(m, 140, 30)
        styles = tui_model.picker_styles(m, lines)
        starred = [l for l, s in zip(lines, styles) if s == tui_model.STYLE_MANAGER]
        self.assertEqual(len(starred), 2)
        self.assertTrue(all("★" in l and "manager" in l for l in starred))
        self.assertTrue(any(A_MANAGER in l for l in starred) and any(B_MANAGER in l for l in starred))
        header = next(l for l in lines if "alpha  (2 members)" in l)
        self.assertIn("manager: {}".format(A_MANAGER), header)
        self.assertIn("⇄ beta", header)
        self.assertIn("no manager", next(l for l in lines if "gamma  (1 member)" in l))
        self.assertTrue(any("c connect" in line for line in lines))
        self.assertTrue(any("v map" in line for line in lines))
        self.assertEqual(picker.picker_attrs.__name__, "picker_attrs")

    def test_v_opens_a_scrollable_ascii_topology_of_managers_members_and_links(self):
        m = self.model()
        self.assertIsNone(tui_model.picker_apply_key(m, "v"))
        self.assertEqual(m.stage, "topology")
        lines = tui_model.picker_lines(m, 76, 30)
        rendered = "\n".join(lines)
        self.assertIn("Team topology | 3 teams | 1 link", rendered)
        self.assertIn("+-- TEAM alpha", rendered)
        self.assertIn("* MANAGER {}".format(A_MANAGER), rendered)
        self.assertIn("MEMBER {}".format(A_PEER), rendered)
        self.assertIn("! MANAGER not set", rendered)
        self.assertIn("alpha/{} <====[active]====> beta/{}".format(A_MANAGER, B_MANAGER), rendered)
        self.assertNotIn("⇄", rendered, "the topology uses ASCII even in a Unicode-capable terminal")
        narrow = tui_model.picker_lines(m, 40, 12)
        self.assertTrue(all(tui_model.display_width(line) <= 40 for line in narrow))
        tui_model.picker_apply_key(m, "END")
        bottom = "\n".join(tui_model.picker_lines(m, 40, 12))
        self.assertIn("LINKS", bottom)
        self.assertIn("[active]", bottom)
        self.assertIsNone(tui_model.picker_apply_key(m, "v"))
        self.assertEqual(m.stage, "select")

    def test_c_opens_the_chooser_with_the_three_row_states_and_enter_maps_to_the_argv(self):
        m = self.model()
        tui_model.focus_node(m, "team:alpha")
        self.assertIsNone(tui_model.picker_apply_key(m, "c"))
        self.assertEqual(m.stage, "link_pick")
        labels = [r["label"] for r in tui_model.link_options(m)]
        self.assertIn("beta  linked - Enter breaks the link", labels[0])
        self.assertIn("gamma  no manager - set one first", labels[1])
        for index, row in enumerate(tui_model.link_options(m)):
            m.link_index = index
            lines = tui_model.picker_lines(m, 24, 8)
            rendered = " ".join("\n".join(lines).split())
            self.assertIn(" ".join(row["label"].split()), rendered)
            self.assertTrue(all(tui_model.display_width(line) <= 24 for line in lines))
        tui_model.picker_apply_key(m, "DOWN")
        self.assertIsNone(tui_model.picker_apply_key(m, "ENTER"))
        self.assertIn("gamma has no manager", m.error or "")
        tui_model.picker_apply_key(m, "UP")
        intent = tui_model.picker_apply_key(m, "ENTER")
        self.assertEqual((intent.kind, intent.args["action"], intent.args["other"]), ("team_link", "unlink", "beta"))
        self.assertEqual(picker.action_args(intent), ["--team", "alpha", "unlink", "alpha", "beta"])
        self.assertIn("unlinked", picker.action_success_status(intent, {}))
        m = self.model(linked=False)
        tui_model.focus_node(m, "team:alpha")
        tui_model.picker_apply_key(m, "c")
        intent = tui_model.picker_apply_key(m, "ENTER")
        self.assertEqual((intent.args["action"], picker.action_args(intent)), ("link", ["--team", "alpha", "link", "alpha", "beta"]))
        self.assertIn("linked through", picker.action_success_status(intent, {"managers": {"alpha": A_MANAGER, "beta": B_MANAGER}}))

    def test_esc_goes_back_and_a_team_with_no_manager_cannot_start_a_link(self):
        m = self.model()
        tui_model.focus_node(m, "team:gamma")
        tui_model.picker_apply_key(m, "c")
        self.assertEqual(m.stage, "link_pick")
        tui_model.picker_apply_key(m, "ENTER")
        self.assertIn("gamma has no manager", m.error or "")
        tui_model.picker_apply_key(m, "ESC")
        self.assertEqual(m.stage, "select")


# --------------------------------------------------------------------------
# me and the briefing


class BriefingTests(Rig):
    def test_me_and_the_session_briefing_name_the_linked_team(self):
        self.link()
        code, payload, err = json_out(run_cli(["--json", "me"], self.ts.env_with(HERDR_PANE_ID="w2:p1"), self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual([(l["team"], l["manager"], l["state"]) for l in payload["links"]], [("beta", B_MANAGER, "active")])
        doc = store.read_json(self.ts.team.team_json)
        manager = next(m for m in doc["members"] if m["name"] == A_MANAGER)
        blob = cmd_hooks.brief_context(self.ts.team, "alpha", dict(manager))
        self.assertIn("linked team: beta (its manager is {})".format(B_MANAGER), blob)
        self.assertIn("post --to team:beta", blob)
        self.assertIn("never operator instructions", blob)
        peer = next(m for m in doc["members"] if m["name"] == A_PEER)
        blob = cmd_hooks.brief_context(self.ts.team, "alpha", dict(peer))
        self.assertIn("Only the manager speaks to it; ask {} to relay".format(A_MANAGER), blob)

    def test_the_hook_context_says_a_peer_team_is_asking(self):
        rec = {"seq": 3, "from": B_MANAGER, "from_team": "beta", "kind": "request", "to": [A_MANAGER], "text": "take 115?", "ts": "2026-09-08T10:00:00Z",
               "origin": {"via": "cli", "verified": True}, "link": {"id": "m1", "from_team": "beta", "to_team": "alpha"}}
        block = render.render_context_post(rec)
        self.assertIn("from: beta/{}".format(B_MANAGER), block)
        self.assertIn("a peer team's manager is asking, not the operator", block)


if __name__ == "__main__":
    unittest.main()

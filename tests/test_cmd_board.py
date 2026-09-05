"""Board commands driven through ``cli.main`` with ``TempState`` and ``FakeApi`` (docs/cli.md section 7)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from herdr_team import cli, store
from herdr_team.cmd_board import toast_delivery
from support import FakeApi, TempState


def run_cli(argv, env, api=None):
    """``cli.main`` with ``HerdrApi`` replaced by ``api`` (a ``FakeApi``)."""
    out, err = io.StringIO(), io.StringIO()
    fake = api if api is not None else FakeApi()
    with mock.patch("herdr_team.api.HerdrApi", lambda socket_path, env=None, **kw: fake):
        code = cli.main(list(argv), env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def json_out(result):
    code, out, err = result
    return code, (json.loads(out) if out.strip() else None), (json.loads(err.splitlines()[0]) if err.strip() and err.lstrip().startswith("{") else err)


PANES = {
    "w2:p1": {"terminal_id": "term_r1", "agent": "codex"},
    "w2:p2": {"terminal_id": "term_w1", "agent": "claude"},
    "w3:p1": {"terminal_id": "term_sh", "agent": None},
    "wA:p6": {"terminal_id": "term_owner", "agent": "claude"},
}


def pane_api():
    api = FakeApi()

    def pane_get(params):
        pane_id = params.get("pane_id")
        info = PANES.get(pane_id)
        if info is None:
            from support import FakeError

            raise FakeError("pane_not_found", "pane {} not found".format(pane_id))
        ws = pane_id.split(":")[0]
        return {"pane": {"pane_id": pane_id, "terminal_id": info["terminal_id"], "agent": info["agent"], "workspace_id": ws, "tab_id": ws + ":t1", "focused": False}}

    api.set_response("pane.get", pane_get)
    api.set_response("agent.rename", {"type": "ok"})
    api.set_response("pane.rename", {"type": "ok"})
    return api


def write_live_daemon(ts):
    """A ``daemon.json`` naming this test process, so liveness checks pass without a real daemon."""
    from herdr_team.cmd_board import now_iso

    start = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, check=False).stdout.decode().strip()
    store.write_json(ts.session.daemon_json, {"pid": os.getpid(), "start_time": start, "beat_at": now_iso(), "socket": os.fspath(ts.socket_path), "socket_inode": 1, "version": "0.1.0", "herdr_version": "0.8.2", "protocol": 20})


class PostReadRoundTrip(unittest.TestCase):
    def test_human_outside_posts_and_reads_back(self):
        with TempState() as ts:
            api = FakeApi()
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "post", "hello team"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["seq"], 1)
            self.assertEqual(payload["team"], "alpha")
            self.assertEqual(payload["notifier"], "offline")
            self.assertEqual(payload["to"], ["all"])
            self.assertEqual(payload["author"], {"name": "human", "via": "outside", "verified": False})
            self.assertIn("warning: notifier offline", err)
            code, board, _ = json_out(run_cli(["--json", "--team", "alpha", "board"], ts.env, api))
            self.assertEqual(code, 0)
            self.assertEqual(board["count"], 1)
            record = board["posts"][0]
            self.assertEqual(record["text"], "hello team")
            self.assertEqual(record["from"], "human")
            self.assertEqual(record["origin"]["via"], "outside")
            self.assertFalse(board["cursor"]["advanced"])
            for key in ("v", "seq", "ts", "from", "from_label", "from_kind", "from_pane", "from_terminal", "from_gen", "origin", "to", "to_role", "kind", "text", "refs", "reply_to", "retracts", "supersedes", "urgent", "ttl_ms", "truncated", "event", "relayed_for"):
                self.assertIn(key, record)
            self.assertFalse(any(m == "notification.show" for m, _ in api.calls))

    def test_notifier_alive_from_daemon_json(self):
        with TempState() as ts:
            write_live_daemon(ts)
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "x"], ts.env))
            self.assertEqual(code, 0)
            self.assertEqual(payload["notifier"], "alive")

    def test_member_pane_defaults_to_human_and_records_identity(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            code, payload, err = json_out(run_cli(["--json", "post", "diff ready", "--kind", "request"], env, pane_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["to"], ["human"])
            self.assertEqual(payload["author"]["name"], "alpha-reviewer")
            self.assertTrue(payload["author"]["verified"])
            record = store.BoardStore(ts.team).get(1)
            self.assertEqual(record["from_kind"], "codex")
            self.assertEqual(record["from_pane"], "w2:p1")
            self.assertEqual(record["from_terminal"], "term_r1")
            self.assertEqual(record["kind"], "request")

    def test_reply_to_unknown_seq_refused(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "post", "x", "--reply-to", "9"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "reply_to_unknown")

    def test_corrupt_board_lines_are_reported_not_hidden(self):
        """M5 F-03: a torn last line plus a garbage line show up as ``skipped`` (JSON) and a stderr warning (human)."""
        with TempState() as ts:
            for i in range(3):
                code, _, err = json_out(run_cli(["--json", "--team", "alpha", "post", "post {}".format(i)], ts.env))
                self.assertEqual(code, 0, err)
            raw = ts.team.board_jsonl.read_bytes()
            lines = raw.rstrip(b"\n").split(b"\n")
            torn = lines[-1][: len(lines[-1]) // 2]
            ts.team.board_jsonl.write_bytes(b"\n".join(lines[:-1]) + b"\n" + torn + b"\n" + b"not json {{{ garbage\n")
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "board", "--last", "5"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual([p["seq"] for p in payload["posts"]], [1, 2])
            self.assertIn("skipped", payload)
            self.assertGreaterEqual(payload["skipped"]["corrupt"] + payload["skipped"]["fragment"], 2)
            code, out, err = run_cli(["--team", "alpha", "board", "--last", "5"], ts.env)
            self.assertEqual(code, 0)
            self.assertIn("warning: board: skipped", err)
            # a clean board carries no ``skipped`` key at all
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "post 3"], ts.env))
            self.assertEqual(code, 0)
            self.assertEqual(payload["seq"], 4)  # board.seq stays the authority: the torn seq 3 is not reused
            ts.team.board_jsonl.write_bytes(b"\n".join(l for l in ts.team.board_jsonl.read_bytes().split(b"\n") if l.startswith(b"{") and l.endswith(b"}")) + b"\n")
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board"], ts.env))
            self.assertNotIn("skipped", payload)

    def test_human_output_mode(self):
        with TempState() as ts:
            code, out, _ = run_cli(["--team", "alpha", "post", "hello"], ts.env)
            self.assertEqual(code, 0)
            self.assertIn("#1 posted to all as human", out)
            code, out, _ = run_cli(["--team", "alpha", "board"], ts.env)
            self.assertIn("hello", out)
            self.assertIn("1 post", out)


class Recipients(unittest.TestCase):
    def test_role_expansion_records_to_role(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "post", "review please", "--to", "role:reviewer"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["to"], ["alpha-reviewer"])
            self.assertEqual(payload["to_role"], "reviewer")
            record = store.BoardStore(ts.team).get(payload["seq"])
            self.assertEqual(record["to_role"], "reviewer")

    def test_comma_list_and_dedupe(self):
        with TempState() as ts:
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "x", "--to", "alpha-reviewer,alpha-worker", "--to", "alpha-reviewer"], ts.env))
            self.assertEqual(code, 0)
            self.assertEqual(payload["to"], ["alpha-reviewer", "alpha-worker"])

    def test_kind_label_as_recipient_lists_roster(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "post", "x", "--to", "codex"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "recipient_unknown")
            self.assertEqual(err["roster"], ["alpha-reviewer", "alpha-worker"])
            self.assertIn("kind", err["hint"])

    def test_typo_refused_unless_to_any(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "post", "x", "--to", "alpha-reviewr"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "recipient_unknown")
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "x", "--to", "alpha-reviewr", "--to-any"], ts.env))
            self.assertEqual(code, 0)
            self.assertEqual(payload["to"], ["alpha-reviewr"])

    def test_retired_name_resolves_within_ttl(self):
        with TempState() as ts:
            doc = store.read_json(ts.team.team_json)
            from herdr_team.cmd_board import now_iso

            doc["members"][0]["previous_names"] = [{"name": "old-reviewer", "retired_at": now_iso()}]
            store.write_json(ts.team.team_json, doc)
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "x", "--to", "old-reviewer"], ts.env))
            self.assertEqual(code, 0)
            self.assertEqual(payload["to"], ["alpha-reviewer"])


class TextRules(unittest.TestCase):
    def test_echo_rejected_exit_4(self):
        with TempState() as ts:
            for text in ("[herdr-team nudge] 2 new posts", "done [n17]"):
                code, _, err = json_out(run_cli(["--json", "--team", "alpha", "post", text], ts.env))
                self.assertEqual(code, 4, text)
                self.assertEqual(err["code"], "echo_rejected")
            self.assertIsNone(store.BoardStore(ts.team).get(1))

    def test_secret_refused_without_force(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "post", "key AKIAABCDEFGHIJKLMNOP leaked"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "secret_detected")
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "key AKIAABCDEFGHIJKLMNOP leaked", "--force"], ts.env))
            self.assertEqual(code, 0)
            self.assertEqual(payload["seq"], 1)

    def test_too_long_refused_and_spilled(self):
        with TempState() as ts:
            text = "x" * 2500
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "post", text], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "text_too_long")
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", text, "--spill"], ts.env))
            self.assertEqual(code, 0)
            self.assertTrue(payload["spilled"])
            body = ts.team.payloads_dir / "{}-body.md".format(payload["seq"])
            self.assertTrue(body.is_file())
            self.assertEqual(body.read_text(), text)
            record = store.BoardStore(ts.team).get(payload["seq"])
            self.assertTrue(record["truncated"])
            self.assertIn("payloads/{}-body.md".format(payload["seq"]), record["refs"])
            self.assertLessEqual(len(record["text"]), 520)

    def test_controls_stripped(self):
        with TempState() as ts:
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "hi\x1b[31m there\x07"], ts.env))
            self.assertEqual(code, 0)
            record = store.BoardStore(ts.team).get(payload["seq"])
            self.assertEqual(record["text"], "hi there")

    def test_attach_and_ref(self):
        with TempState() as ts:
            src = ts.tmp / "diff.patch"
            src.write_text("--- a\n+++ b\n")
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "post", "see diff", "--attach", os.fspath(src)], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(len(payload["attached"]), 1)
            self.assertEqual(payload["attached"][0], "payloads/{}-diff.patch".format(payload["seq"]))  # plan 6.1: payloads/<seq>-<basename>
            self.assertTrue((ts.team.root / payload["attached"][0]).is_file())
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "again", "--ref", payload["attached"][0]], ts.env))
            self.assertEqual(code, 0)
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "post", "bad", "--ref", "/etc/hosts"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "ref_invalid")


class Authorship(unittest.TestCase):
    def test_as_human_from_agent_pane_is_audited(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            code, _, err = json_out(run_cli(["--json", "post", "x", "--as", "human"], env, pane_api()))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "author_mismatch")
            entries = [json.loads(line) for line in ts.team.audit_jsonl.read_bytes().decode().splitlines() if line.strip()]
            self.assertEqual(entries[-1]["event"], "author_mismatch")
            self.assertEqual(entries[-1]["author"], "alpha-reviewer")

    def test_relayed_for_human_from_member(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            code, payload, err = json_out(run_cli(["--json", "post", "operator says stop", "--relayed-for", "human", "--to", "alpha-worker"], env, pane_api()))
            self.assertEqual(code, 0, err)
            record = store.BoardStore(ts.team).get(payload["seq"])
            self.assertEqual(record["relayed_for"], "human")
            self.assertEqual(record["from"], "alpha-reviewer")

    def test_relayed_for_from_human_refused(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "post", "x", "--relayed-for", "human"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "author_mismatch")

    def test_agent_pane_outside_any_team_is_not_a_member(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="wA:p6")
            code, _, err = json_out(run_cli(["--json", "post", "x"], env, pane_api()))
            self.assertEqual(code, 3)
            self.assertEqual(err["code"], "not_a_member")

    def test_shell_pane_is_human_unverified(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w3:p1")
            code, payload, err = json_out(run_cli(["--json", "post", "x"], env, pane_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["author"]["name"], "human")
            self.assertEqual(payload["author"]["via"], "cli-unverified")
            self.assertIn("warning: author unverified", err)

    def test_outside_without_any_team_exits_3(self):
        with TempState(write_team=False) as ts:
            code, _, err = json_out(run_cli(["--json", "post", "x"], ts.env))
            self.assertEqual(code, 3)
            self.assertEqual(err["code"], "team_required")

    def test_outside_with_two_teams_and_no_hint_is_ambiguous(self):
        with TempState() as ts:
            beta = ts.session.team("beta")
            from herdr_team import paths

            paths.ensure_team_dirs(beta)
            store.write_json(beta.team_json, dict(store.read_json(ts.team.team_json), team="beta"))
            code, _, err = json_out(run_cli(["--json", "post", "x"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "team_ambiguous")
            self.assertEqual(err["teams"], ["alpha", "beta"])
            code, _, err = json_out(run_cli(["--json", "--team", "beta", "post", "x"], ts.env))
            self.assertEqual(code, 0)
            code, _, err = json_out(run_cli(["--json", "use", "beta"], ts.env))
            code, payload, err = json_out(run_cli(["--json", "post", "x"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["team"], "beta")

    def test_offline_append_works(self):
        with TempState() as ts:
            api = FakeApi()
            api.unreachable = True
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "server down"], ts.env, api))
            self.assertEqual(code, 0)
            self.assertEqual(payload["seq"], 1)


class Cursors(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.api = pane_api()
        self.member_env = self.ts.env_with(HERDR_PANE_ID="w2:p1")
        for i in range(3):
            code, _, err = run_cli(["--json", "--team", "alpha", "post", "post {}".format(i), "--to", "alpha-reviewer"], self.ts.env, self.api)
            self.assertEqual(code, 0, err)
        run_cli(["--json", "--team", "alpha", "post", "for worker", "--to", "alpha-worker"], self.ts.env, self.api)

    def tearDown(self):
        self.ts.cleanup()

    def test_new_selects_mine_and_advances(self):
        code, payload, err = json_out(run_cli(["--json", "board", "--new"], self.member_env, self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["reader"], "alpha-reviewer")
        self.assertEqual([p["text"] for p in payload["posts"]], ["post 0", "post 1", "post 2"])
        self.assertEqual(payload["cursor"], {"before": 0, "after": 3, "advanced": True})
        cursor = store.read_json(self.ts.team.cursor("alpha-reviewer"))
        self.assertEqual(cursor["seq"], 3)
        self.assertEqual(cursor["surfaced_by"], "cli")
        self.assertEqual(cursor["terminal_id"], "term_r1")
        code, payload, _ = json_out(run_cli(["--json", "board", "--new"], self.member_env, self.api))
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload["cursor"]["advanced"])

    def test_peek_never_advances(self):
        code, payload, _ = json_out(run_cli(["--json", "board", "--peek"], self.member_env, self.api))
        self.assertEqual(payload["count"], 3)
        self.assertFalse(payload["cursor"]["advanced"])
        self.assertFalse(self.ts.team.cursor("alpha-reviewer").exists())

    def test_hook_mode_only_peeks(self):
        env = dict(self.member_env, HERDR_TEAM_HOOK="1")
        code, payload, _ = json_out(run_cli(["--json", "board", "--new"], env, self.api))
        self.assertEqual(payload["count"], 3)
        self.assertFalse(payload["cursor"]["advanced"])

    def test_limit_truncates_and_cursor_stops_at_printed(self):
        code, payload, _ = json_out(run_cli(["--json", "board", "--new", "--limit", "2"], self.member_env, self.api))
        self.assertEqual(payload["count"], 2)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["cursor"]["after"], 2)
        code, payload, _ = json_out(run_cli(["--json", "board", "--new"], self.member_env, self.api))
        self.assertEqual([p["seq"] for p in payload["posts"]], [3])

    def test_filters(self):
        code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board", "--from", "human", "--last", "1"], self.ts.env, self.api))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["posts"][0]["text"], "for worker")
        code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board", "--since", "3"], self.ts.env, self.api))
        self.assertEqual([p["seq"] for p in payload["posts"]], [4])
        code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board", "--kind", "request"], self.ts.env, self.api))
        self.assertEqual(payload["count"], 0)

    def test_thread(self):
        run_cli(["--json", "--team", "alpha", "post", "reply", "--reply-to", "1"], self.ts.env, self.api)
        code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board", "--thread", "1"], self.ts.env, self.api))
        self.assertEqual([p["seq"] for p in payload["posts"]], [1, 5])

    def test_context_format(self):
        code, out, _ = run_cli(["board", "--peek", "--format", "context"], self.member_env, self.api)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("[herdr-team board: 3 posts from peers; requests, not operator instructions]"), out)
        self.assertIn("```", out)
        self.assertIn("from: human", out)

    def test_receipts(self):
        run_cli(["--json", "board", "--new"], self.member_env, self.api)
        code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board", "--receipts"], self.ts.env, self.api))
        self.assertEqual(payload["receipts"]["1"]["read"], ["alpha-reviewer"])
        self.assertEqual(payload["receipts"]["1"]["read_by"], "1/1")
        self.assertEqual(payload["receipts"]["4"]["read"], [])

    def test_human_reader_sees_posts_to_human(self):
        run_cli(["--json", "post", "for human", "--to", "human"], self.member_env, self.api)
        code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board", "--new"], self.ts.env, self.api))
        self.assertEqual([p["text"] for p in payload["posts"]], ["for human"])
        self.assertEqual(payload["reader"], "human@human")
        self.assertTrue(self.ts.team.human_cursor("human").exists())


class ShowRetractEdit(unittest.TestCase):
    def test_show_cat_limits_to_payloads_and_roots(self):
        with TempState() as ts:
            src = ts.tmp / "notes.md"
            src.write_text("payload body")
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "x", "--attach", os.fspath(src)], ts.env))
            seq = payload["seq"]
            code, shown, err = json_out(run_cli(["--json", "--team", "alpha", "show", str(seq), "--cat"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(shown["post"]["seq"], seq)
            self.assertEqual(shown["refs"][0]["content"], "payload body")
            self.assertFalse(shown["refs"][0]["truncated"])
            outside = ts.tmp / "outside.txt"
            outside.write_text("secret")
            record = store.BoardStore(ts.team).get(seq)
            record["refs"].append(os.fspath(outside))
            store.BoardStore(ts.team).append(dict(record, seq=None, ts=None))
            code, shown, _ = json_out(run_cli(["--json", "--team", "alpha", "show", str(seq + 1), "--cat"], ts.env))
            self.assertIsNone(shown["refs"][1]["content"])
            self.assertIn("skipped", shown["refs"][1])
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "show", "99"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "post_not_found")

    def test_retract_and_edit_by_author(self):
        with TempState() as ts:
            api = pane_api()
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            code, payload, _ = json_out(run_cli(["--json", "post", "typo", "--to", "alpha-worker"], env, api))
            seq = payload["seq"]
            code, edited, err = json_out(run_cli(["--json", "edit", str(seq), "fixed"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(edited["supersedes"], seq)
            record = store.BoardStore(ts.team).get(edited["seq"])
            self.assertEqual(record["text"], "fixed")
            self.assertEqual(record["to"], ["alpha-worker"])
            code, retracted, _ = json_out(run_cli(["--json", "retract", str(seq)], env, api))
            self.assertEqual(retracted["retracts"], seq)
            self.assertEqual(store.BoardStore(ts.team).get(retracted["seq"])["kind"], "retract")

    def test_retract_others_post_refused_for_member_allowed_for_human(self):
        with TempState() as ts:
            api = pane_api()
            code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "post", "from human"], ts.env, api))
            seq = payload["seq"]
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            code, _, err = json_out(run_cli(["--json", "retract", str(seq)], env, api))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "author_mismatch")
            code, payload, _ = json_out(run_cli(["--json", "post", "mine", "--to", "human"], env, api))
            code, retracted, _ = json_out(run_cli(["--json", "--team", "alpha", "retract", str(payload["seq"])], ts.env, api))
            self.assertEqual(code, 0)
            self.assertEqual(retracted["retracts"], payload["seq"])


class TaskAndAck(unittest.TestCase):
    def test_task_headline_24_columns(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            code, payload, err = json_out(run_cli(["--json", "task", "review the diff for the session isolation bug"], env, pane_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["member"], "alpha-reviewer")
            from herdr_team import sanitize

            self.assertLessEqual(sanitize.display_width(payload["task"]), 24)
            self.assertTrue(payload["task"].endswith("…"))
            doc = store.read_json(ts.team.root / "tasks" / "alpha-reviewer.json")
            self.assertEqual(doc["headline"], payload["task"])

    def test_task_from_human_refused(self):
        with TempState() as ts:
            code, _, err = json_out(run_cli(["--json", "--team", "alpha", "task", "x"], ts.env))
            self.assertEqual(code, 3)
            self.assertEqual(err["code"], "not_a_member")

    def test_ack_records_cursor_and_charter(self):
        with TempState() as ts:
            api = pane_api()
            run_cli(["--json", "--team", "alpha", "post", "one"], ts.env, api)
            run_cli(["--json", "--team", "alpha", "post", "two"], ts.env, api)
            env = ts.env_with(HERDR_PANE_ID="w2:p1")
            code, payload, err = json_out(run_cli(["--json", "ack"], env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload, {"team": "alpha", "member": "alpha-reviewer", "cursor": 2, "charter_seq_acked": 1})
            doc = store.read_json(ts.team.team_json)
            member = [m for m in doc["members"] if m["name"] == "alpha-reviewer"][0]
            self.assertEqual(member["charter_seq_acked"], 1)
            self.assertIsNotNone(member["briefed_at"])
            self.assertEqual(store.read_json(ts.team.cursor("alpha-reviewer"))["seq"], 2)

    def test_ack_from_hook_refused(self):
        with TempState() as ts:
            env = ts.env_with(HERDR_PANE_ID="w2:p1", HERDR_TEAM_HOOK="1")
            code, _, err = json_out(run_cli(["--json", "ack"], env, pane_api()))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "ack_from_hook")


class SessionMismatchGuard(unittest.TestCase):
    """RS-08 regression: writes against a team whose socket differs from the resolved one are refused (plan 12)."""

    def _env(self, ts, socket):
        env = dict(ts.env)
        env.pop("HERDR_SESSION", None)
        if socket is None:
            env.pop("HERDR_SOCKET_PATH", None)
        else:
            env["HERDR_SOCKET_PATH"] = socket
        return env

    def test_post_and_charter_and_use_refuse_a_foreign_socket(self):
        with TempState() as ts:
            other = os.fspath(ts.tmp / "nonexistent" / "herdr.sock")
            env = self._env(ts, other)
            team_dir = os.fspath(ts.team.root)
            for argv in (["post", "hello"], ["charter", "set", "new charter"], ["use", "alpha"], ["retract", "1"], ["edit", "1", "x"]):
                code, _, err = json_out(run_cli(["--json", "--team", team_dir] + argv, env))
                self.assertEqual(code, 1, (argv, err))
                self.assertEqual(err["code"], "team_session_mismatch", argv)
            self.assertEqual(store.BoardStore(ts.team).read(), [])
            self.assertEqual(store.read_json(ts.team.team_json)["charter"]["seq"], 1)  # unchanged
            # the escape hatch, and the explicit --socket override, allow the write
            code, payload, err = json_out(run_cli(["--json", "--team", team_dir, "--session-mismatch-ok", "post", "hello"], env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["seq"], 1)
            code, payload, err = json_out(run_cli(["--json", "--team", team_dir, "--socket", other, "post", "again"], env))
            self.assertEqual(code, 0, err)
            # reads never check
            code, payload, err = json_out(run_cli(["--json", "--team", team_dir, "board"], env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["count"], 2)

    def test_default_socket_outside_herdr_still_appends(self):
        """HP-05: outside Herdr with --team <path> and nothing configured, the append works offline."""
        with TempState() as ts:
            env = self._env(ts, None)
            code, payload, err = json_out(run_cli(["--json", "--team", os.fspath(ts.team.root), "post", "offline"], env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["author"]["via"], "outside")

    def test_matching_socket_writes(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", os.fspath(ts.team.root), "post", "same socket"], ts.env))
            self.assertEqual(code, 0, err)


class Helpers(unittest.TestCase):
    def test_toast_delivery_parsed_from_config(self):
        with TempState() as ts:
            self.assertEqual(toast_delivery(ts.config_dir), "off")
            (ts.config_dir / "config.toml").write_text("[ui]\nsidebar_width = 26\n\n[ui.toast]\n# delivery = \"herdr\"\ndelivery = \"terminal\"\n\n[ui.toast.herdr]\nposition = \"x\"\n")
            self.assertEqual(toast_delivery(ts.config_dir), "terminal")

    def test_every_board_command_registered(self):
        names = {c.name for c in cli.load_commands()}
        for name in ("post", "board", "show", "retract", "edit", "task", "ack"):
            self.assertIn(name, names)

    def test_launcher_module_main_loads_commands(self):
        """``python -m herdr_team.cli`` imports cli twice; Command entries must satisfy ``__main__``'s isinstance."""
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run([os.environ.get("PYTHON", "python3"), "-m", "herdr_team.cli", "--version"], cwd=os.fspath(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=30, env=dict(os.environ, PYTHONPATH=os.fspath(root)))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"herdr-team", proc.stdout)


if __name__ == "__main__":
    unittest.main()

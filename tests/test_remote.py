"""Phone reach (``remote``): channels, pairing, policy, and answers coming back.

No test here touches the network: every HTTP exchange goes through
``FakeTransport``, which records the request and plays a tiny ntfy / Telegram
/ webhook server in memory.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from herdr_team import asks, cmd_asks, cmd_board, identity, remote, store
from support import FakeApi, TempState, wait_until
from test_cmd_roster import json_out, live_api, run_cli
from test_daemon import make_daemon

MEMBER = "alpha-reviewer"
PEER = "alpha-worker"
BOT_TOKEN = "123456789:AAH4cSecretBotTokenValue_abcdefghij"
NTFY_TOKEN = "tk_secretaccesstokenvalue1234567"
HOOK_URL = "https://hooks.example.com/services/T000/B000/secretwebhookpart"
TOPIC = "herdr-private-topic-x7"


class FakeTransport(remote.Transport):
    """An in-memory ntfy server, Telegram Bot API, and webhook sink."""

    def __init__(self):
        self.calls = []
        self.ntfy = []  # published and incoming ntfy messages, oldest first
        self.updates = []  # Telegram updates
        self.error = None  # raise this from every fetch
        self.status = 200  # answer every send with this status
        self.gate = None  # a threading.Event every fetch waits on
        self.lock = threading.Lock()
        self._next_id = 0

    def _id(self):
        self._next_id += 1
        return self._next_id

    def fetch(self, method, url, body=None, headers=None, timeout=remote.HTTP_TIMEOUT_S):
        with self.lock:
            self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers or {}), "timeout": timeout})
        if self.gate is not None:
            self.gate.wait(10)
        if self.error is not None:
            raise self.error
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path.endswith("/getUpdates"):
            offset = int(query.get("offset") or 0)
            result = [u for u in self.updates if u["update_id"] >= offset]
            return remote.HttpResponse(200, json.dumps({"ok": True, "result": result}).encode())
        if parts.path.endswith("/getMe"):
            return remote.HttpResponse(200, json.dumps({"ok": True, "result": {"username": "herdr_test_bot"}}).encode())
        if parts.path.endswith("/sendMessage"):
            if self.status != 200:
                return remote.HttpResponse(self.status, json.dumps({"ok": False, "description": "Forbidden"}).encode())
            return remote.HttpResponse(200, json.dumps({"ok": True, "result": {"message_id": 7000 + self._id()}}).encode())
        if parts.path.endswith("/json"):
            since = query.get("since", "all")
            ids = [m["id"] for m in self.ntfy]
            start = ids.index(since) + 1 if since in ids else 0
            lines = [json.dumps(m) for m in self.ntfy[start:]]
            return remote.HttpResponse(200, "\n".join(lines).encode())
        if method == "POST" and self.status != 200:
            return remote.HttpResponse(self.status, b'{"error":"nope"}')
        if method == "POST" and "hooks.example.com" not in url:
            message = {"id": "m{}".format(self._id()), "time": int(time.time()), "event": "message",
                       "message": (body or b"").decode(), "tags": [t for t in (headers or {}).get("Tags", "").split(",") if t]}
            self.ntfy.append(message)
            return remote.HttpResponse(200, json.dumps(message).encode())
        return remote.HttpResponse(200, b"ok")

    # -- what a phone does ------------------------------------------------------

    def phone_ntfy(self, text):
        self.ntfy.append({"id": "p{}".format(self._id()), "time": int(time.time()), "event": "message", "message": text})

    def phone_telegram(self, text, chat_id=42, chat_type="private", reply_to=None):
        message = {"message_id": self._id(), "date": int(time.time()), "chat": {"id": chat_id, "type": chat_type}, "text": text}
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        self.updates.append({"update_id": 500 + len(self.updates), "message": message})

    def sends(self):
        """Every outbound message's text, whatever the channel."""
        out = []
        for call in self.calls:
            if call["method"] != "POST":
                continue
            if call["url"].endswith("/sendMessage"):
                out.append(json.loads(call["body"])["text"])
            elif "hooks.example.com" in call["url"]:
                out.append(json.loads(call["body"])["text"])
            else:
                out.append("{}\n{}".format(call["headers"].get("Title"), call["body"].decode()))
        return out


class Wall:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def post_ask(ts, text="Which market do we test first, EU or US?", kind="question", author=MEMBER):
    return store.BoardStore(ts.team).append({
        "from": author, "from_kind": "codex", "from_terminal": "term_r1", "origin": {"via": "cli", "verified": True},
        "to": ["human"], "kind": kind, "text": text})


def pair(ts, channel="ntfy", token=True, chat_id=42, paired_ago=30.0, policy=None):
    """A pairing written the way the CLI writes it, a little in the past."""
    now = time.time() - paired_ago
    if channel == "ntfy":
        settings = {"server": "https://ntfy.example.org", "topic": TOPIC, "token": NTFY_TOKEN if token else None, "since": str(int(now))}
    elif channel == "telegram":
        settings = {"bot_token": BOT_TOKEN, "chat_id": chat_id, "pair_started": now}
    else:
        settings = {"url": HOOK_URL}
    doc = remote.new_config(channel, settings, {}, now, "human")
    if policy:
        doc["policy"] = dict(remote.policy_of({}), **policy)
    remote.save_config(ts.config_dir, doc)
    return doc


class RelayCase(unittest.TestCase):
    """A daemon with the relay run inline against a ``FakeTransport``."""

    channel = "ntfy"

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.fake = FakeTransport()
        self.wall = Wall()
        self.start()

    def start(self):
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.remote.inline = True
        self.d.remote.transport = self.fake
        self.d.remote.wall = self.wall
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def ticks(self, n=1, step=1.0):
        for _ in range(n):
            self.clock.advance(step)
            self.wall.t += step
            self.d.scan_teams(force=True)
            self.d.tail_boards()
            self.d.remote.tick(self.d.teams, self.clock() * 1000)

    def state(self):
        return remote.read_state(self.ts.session)

    def code_for(self, seq):
        return next(code for code, entry in self.state()["codes"].items() if entry["seq"] == seq)

    def answers(self, seq):
        return [r for r in store.BoardStore(self.ts.team).read() if r.get("reply_to") == seq and r.get("kind") == "answer"]


# --------------------------------------------------------------------------
# channels on the wire


class ChannelWireTests(unittest.TestCase):
    def test_ntfy_publish_carries_title_priority_tag_and_the_token_only_when_set(self):
        fake = FakeTransport()
        doc = {"channel": "ntfy", "ntfy": {"server": "https://ntfy.example.org/", "topic": TOPIC, "token": NTFY_TOKEN}}
        outcome = remote.send_message(doc, fake, "default/alpha: question from bob", "#3 hi", 4)
        self.assertTrue(outcome.ok)
        call = fake.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", "https://ntfy.example.org/" + TOPIC))
        self.assertEqual(call["headers"]["Title"], "default/alpha: question from bob")
        self.assertEqual((call["headers"]["Priority"], call["headers"]["Tags"]), ("4", remote.NTFY_TAG))
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + NTFY_TOKEN)
        self.assertLessEqual(call["timeout"], 5.0)
        doc["ntfy"]["token"] = None
        remote.send_message(doc, fake, "t", "b")
        self.assertNotIn("Authorization", fake.calls[1]["headers"])

    def test_ntfy_poll_reads_new_messages_and_skips_its_own_and_keepalives(self):
        fake = FakeTransport()
        doc = {"channel": "ntfy", "ntfy": {"server": "https://ntfy.example.org", "topic": TOPIC, "token": NTFY_TOKEN}}
        remote.send_message(doc, fake, "t", "an ask we published")
        fake.ntfy.append({"id": "k1", "event": "keepalive"})
        fake.phone_ntfy("ABCD yes")
        outcome = remote.poll_messages(doc, fake, "1700000000")
        self.assertTrue(outcome.ok)
        self.assertEqual([m.text for m in outcome.incoming], ["ABCD yes"])
        self.assertEqual(outcome.cursor, fake.ntfy[-1]["id"])
        url = fake.calls[-1]["url"]
        self.assertIn("/{}/json?".format(TOPIC), url)
        self.assertIn("poll=1", url)
        self.assertIn("since=1700000000", url)
        self.assertEqual(remote.poll_messages(doc, fake, outcome.cursor).incoming, [], "the cursor moves past what was read")

    def test_telegram_send_and_a_short_poll_never_a_long_poll(self):
        fake = FakeTransport()
        doc = {"channel": "telegram", "telegram": {"bot_token": BOT_TOKEN, "chat_id": 42}}
        outcome = remote.send_message(doc, fake, "title", "body")
        self.assertTrue(outcome.ok)
        self.assertIsInstance(outcome.message_id, int)
        self.assertEqual(json.loads(fake.calls[0]["body"])["chat_id"], 42)
        fake.phone_telegram("ABCD fine", reply_to=outcome.message_id)
        polled = remote.poll_messages(doc, fake, 0)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(fake.calls[-1]["url"]).query))
        self.assertEqual(query["timeout"], "0")
        self.assertEqual(polled.cursor, 501)
        self.assertEqual((polled.incoming[0].chat_id, polled.incoming[0].reply_to_message_id), (42, outcome.message_id))

    def test_webhook_is_a_slack_shaped_json_post(self):
        fake = FakeTransport()
        remote.send_message({"channel": "webhook", "webhook": {"url": HOOK_URL}}, fake, "title", "body")
        self.assertEqual(json.loads(fake.calls[0]["body"]), {"text": "title\nbody"})
        self.assertEqual(fake.calls[0]["headers"]["Content-Type"], "application/json")

    def test_errors_never_carry_the_secret(self):
        fake = FakeTransport()
        doc = {"channel": "telegram", "telegram": {"bot_token": BOT_TOKEN, "chat_id": 42}}
        fake.error = remote.TransportError("timed out fetching https://api.telegram.org/bot{}/sendMessage".format(BOT_TOKEN))
        for outcome in (remote.send_message(doc, fake, "t", "b"), remote.poll_messages(doc, fake, 0)):
            self.assertFalse(outcome.ok)
            self.assertNotIn(BOT_TOKEN, outcome.error)
            self.assertIn("[secret]", outcome.error)

    def test_redirects_are_refused(self):
        handler = remote._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://elsewhere.example/"))

    def test_reply_parsing(self):
        self.assertEqual(remote.parse_reply("k7qx  yes, go ahead"), ("K7QX", "yes, go ahead"))
        self.assertEqual(remote.parse_reply("K7QX: ok"), ("K7QX", "ok"))
        self.assertEqual(remote.parse_reply("K7QX"), ("K7QX", ""))
        self.assertEqual(remote.parse_reply("ok"), (None, "ok"))
        self.assertEqual(remote.parse_reply("K7QXZ hello"), (None, "K7QXZ hello"), "five characters is a word, not a code")
        self.assertEqual(remote.parse_reply("K0QX hi")[0], None, "0 is not in the alphabet")
        self.assertTrue(remote.is_ack("OK.") and remote.is_ack("ack") and not remote.is_ack("ok go ahead"))


# --------------------------------------------------------------------------
# what a message says


class PolicyTextTests(unittest.TestCase):
    RECORD = {"seq": 12, "from": "alpha-researcher", "kind": "blocked",
              "text": "Publishing needs a key; I found sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123 in the notes. Go?"}

    def test_full_redacts_secrets_and_caps(self):
        title, body = remote.compose_ask("default", "alpha", self.RECORD, "K7QX", "full")
        self.assertIn("[redacted:", body)
        self.assertNotIn("sk-ant-api03", body)
        self.assertIn("K7QX", body)
        self.assertEqual(title, "default/alpha: blocked from alpha-researcher")
        long = dict(self.RECORD, text="x" * 490 + " AKIAABCDEFGHIJKLMNOP and more text after the cap")
        capped = remote.redact(long["text"])
        self.assertLessEqual(len(capped), remote.TEXT_CAP)
        self.assertNotIn("AKIA", capped, "redaction runs before the cap, so half a key never escapes")

    def test_summary_never_carries_the_text(self):
        title, body = remote.compose_ask("default", "alpha", self.RECORD, "K7QX", "summary")
        self.assertNotIn("Publishing", body)
        self.assertIn("blocked from alpha-researcher in default/alpha", body)
        self.assertIn("K7QX", body)

    def test_none_says_only_that_something_waits(self):
        self.assertEqual(remote.compose_ask("default", "alpha", self.RECORD, None, "none"), ("Herdr", remote.NONE_TEXT))
        self.assertEqual(remote.compose_event("default", "alpha", {"event": "fact_conflict", "seq": 3, "text": "x"}, "none"),
                         ("Herdr", remote.NONE_TEXT))

    def test_the_remote_author_passes_no_authority_gate(self):
        author = remote.remote_author("telegram")
        self.assertTrue(author.is_human)
        self.assertFalse(author.trusted_human, "a phone answer is the human's word on the board, never an authority")
        self.assertTrue(identity.record_human_ok(author.origin))


# --------------------------------------------------------------------------
# the CLI: pairing, policy, status


class CliCase(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.fake = FakeTransport()
        patcher = mock.patch.object(remote, "TRANSPORT_FACTORY", lambda: self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *args, env=None, api=None):
        return json_out(run_cli(["--json", "--team", "alpha", "remote"] + list(args), env or self.ts.env, api or FakeApi()))

    def secret_file(self, value, name="secret"):
        path = self.ts.tmp / name
        path.write_text(value + "\n")
        return os.fspath(path)

    def config(self):
        return remote.load_config(self.ts.config_dir)


class PairingTests(CliCase):
    def test_ntfy_without_a_token_is_outbound_only_and_says_so(self):
        code, out, err = self.cli("pair", "ntfy", "--topic", TOPIC)
        self.assertEqual(code, 0, err)
        self.assertTrue(out["paired"])
        self.assertTrue(out["outbound_only"])
        self.assertIn("public", out["outbound_only_reason"])
        self.assertNotIn(TOPIC, json.dumps(out), "an open topic's name is its password; status never shows it")
        code, status, _ = self.cli("status")
        self.assertEqual((code, status["channel"], status["receives"]), (0, "ntfy", False))
        _code, human, _err = run_cli(["--team", "alpha", "remote", "status"], self.ts.env, FakeApi())
        self.assertIn("outbound-only", human)

    def test_ntfy_with_a_token_file_reads_answers_and_the_config_is_private(self):
        code, out, err = self.cli("pair", "ntfy", "--topic", TOPIC, "--server", "https://ntfy.example.org", "--token-file", self.secret_file(NTFY_TOKEN))
        self.assertEqual(code, 0, err)
        self.assertTrue(out["receives"])
        self.assertFalse(out["outbound_only"])
        path = remote.config_path(self.ts.config_dir)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        self.assertEqual(self.config()["ntfy"]["token"], NTFY_TOKEN)
        self.assertNotIn(NTFY_TOKEN, json.dumps(out))

    def test_a_token_from_an_env_var_name(self):
        code, out, err = self.cli("pair", "ntfy", "--topic", TOPIC, "--token-env", "MY_NTFY", env=self.ts.env_with(MY_NTFY=NTFY_TOKEN))
        self.assertEqual((code, out["receives"]), (0, True), err)
        code, _out, err = self.cli("pair", "ntfy", "--topic", TOPIC, "--token-env", "UNSET_VAR")
        self.assertEqual((code, err["code"]), (1, "remote_secret_unreadable"))

    def test_secrets_on_the_command_line_are_refused_without_echoing_them(self):
        for args in (("pair", "telegram", "--bot-token", BOT_TOKEN), ("pair", "ntfy", "--topic", TOPIC, "--token", NTFY_TOKEN),
                     ("pair", "webhook", "--url", HOOK_URL)):
            code, out, err = run_cli(["--json", "--team", "alpha", "remote"] + list(args), self.ts.env, FakeApi())
            self.assertEqual(code, 2, (args, err))
            self.assertEqual(json.loads(err.splitlines()[0])["code"], "secret_in_argv")
            for secret in (BOT_TOKEN, NTFY_TOKEN, HOOK_URL):
                self.assertNotIn(secret, err + out)
        self.assertEqual(self.config(), {}, "nothing was written")

    def test_telegram_pairs_through_a_one_time_code_from_a_private_chat(self):
        code, out, err = self.cli("pair", "telegram", "--bot-token-file", self.secret_file(BOT_TOKEN))
        self.assertEqual(code, 0, err)
        self.assertTrue(out["pending_pair"])
        self.assertFalse(out["paired"])
        pair_code = out["code"]
        self.assertEqual(out["link"], "https://t.me/herdr_test_bot?start={}".format(pair_code))
        self.assertNotIn(BOT_TOKEN, json.dumps(out))
        code, _out, err = self.cli("pair", "--complete")
        self.assertEqual((code, err["code"]), (1, "remote_pair_waiting"), "nothing sent yet")
        self.fake.phone_telegram("/start {}".format(pair_code), chat_id=99, chat_type="group")
        self.fake.phone_telegram("/start WRONGCODE", chat_id=77)
        code, _out, err = self.cli("pair", "--complete")
        self.assertEqual((code, err["code"]), (1, "remote_pair_waiting"), "a group chat or a wrong code never pairs")
        self.fake.phone_telegram("/start {}".format(pair_code), chat_id=42)
        code, out, err = self.cli("pair", "--complete")
        self.assertEqual(code, 0, err)
        self.assertTrue(out["paired"])
        self.assertEqual(self.config()["telegram"]["chat_id"], 42)
        self.assertNotIn("pair_code", self.config()["telegram"], "the code is single-use")
        self.assertIn("Paired with herdr-synapse", self.fake.sends()[-1])
        audit_events = [e["event"] for e in identity.read_audit(self.ts.layout, "alpha")]
        self.assertIn("remote_pair_started", audit_events)
        self.assertIn("remote_paired", audit_events)

    def test_webhook_needs_https(self):
        code, _out, err = self.cli("pair", "webhook", "--url-file", self.secret_file("http://hooks.example.com/x"))
        self.assertEqual((code, err["code"]), (1, "remote_invalid"))
        code, out, err = self.cli("pair", "webhook", "--url-file", self.secret_file(HOOK_URL))
        self.assertEqual(code, 0, err)
        self.assertTrue(out["outbound_only"])
        self.assertEqual(out["destination"], "webhook at hooks.example.com")

    def test_a_member_cannot_pair_unpair_or_widen_the_policy(self):
        env = self.ts.env_with(HERDR_PANE_ID="w2:p1")
        for args in (("pair", "ntfy", "--topic", TOPIC), ("unpair",), ("policy", "--text", "full")):
            code, _out, err = self.cli(*args, env=env, api=live_api())
            self.assertEqual((code, err["code"]), (1, "author_mismatch"), args)
        self.assertEqual(self.config(), {})
        code, out, _err = self.cli("status", env=env, api=live_api())
        self.assertEqual((code, out["paired"]), (0, False), "reading needs no authority")

    def test_unpair_deletes_the_secret_and_keeps_the_policy(self):
        self.cli("pair", "ntfy", "--topic", TOPIC, "--token-file", self.secret_file(NTFY_TOKEN))
        self.cli("policy", "--text", "none")
        code, out, err = self.cli("unpair")
        self.assertEqual((code, out["unpaired"]), (0, "ntfy"), err)
        self.assertNotIn(NTFY_TOKEN, remote.config_path(self.ts.config_dir).read_text())
        self.assertEqual(remote.policy_of(self.config())["text"], "none")
        code, _out, err = self.cli("unpair")
        self.assertEqual((code, err["code"]), (1, "remote_not_paired"))

    def test_policy_reads_writes_and_validates(self):
        code, out, _err = self.cli("policy")
        self.assertEqual(out["policy"], {"send": ["asks", "conflicts", "failures"], "text": "summary"})
        code, out, err = self.cli("policy", "--send", "asks,settled", "--text", "full")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["policy"], {"send": ["asks", "settled"], "text": "full"})
        self.assertIn("redacted", out["leaves_machine"])
        code, _out, err = self.cli("policy", "--send", "asks,everything")
        self.assertEqual((code, err["code"]), (2, "usage"))
        self.assertEqual(self.cli("policy", "--send", "none")[1]["policy"]["send"], [])

    def test_test_sends_one_message_and_reports_failure(self):
        code, _out, err = self.cli("test")
        self.assertEqual((code, err["code"]), (1, "remote_not_paired"))
        pair(self.ts, "webhook")
        code, out, err = self.cli("test")
        self.assertEqual((code, out["sent"]), (0, True), err)
        self.assertIn("phone reach works", self.fake.sends()[-1])
        self.fake.status = 500
        code, _out, err = self.cli("test")
        self.assertEqual((code, err["code"]), (1, "remote_send_failed"))
        self.assertNotIn(HOOK_URL, json.dumps(err))

    def test_a_symlinked_config_is_refused(self):
        target = self.ts.tmp / "elsewhere.json"
        target.write_text("{}")
        path = remote.config_path(self.ts.config_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
        code, _out, err = self.cli("status")
        self.assertEqual((code, err["code"]), (1, "path_symlink"))

    def test_doctor_names_the_channel_and_whether_it_is_outbound_only(self):
        pair(self.ts, "ntfy", token=False)
        code, payload, err = json_out(run_cli(["--json", "doctor", "--no-probe"], self.ts.env, FakeApi()))
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["remote"]["paired"])
        joined = "\n".join(payload["warnings"])
        self.assertIn("phone reach: ntfy is paired", joined)
        self.assertIn("outbound-only", joined)
        self.assertNotIn(TOPIC, joined)


# --------------------------------------------------------------------------
# the notifier: sending


class RelaySendTests(RelayCase):
    def test_a_new_ask_goes_out_once_with_a_code(self):
        pair(self.ts)
        seq = post_ask(self.ts)
        self.ticks(3)
        sends = self.fake.sends()
        self.assertEqual(len(sends), 1, sends)
        code = self.code_for(seq)
        self.assertIn(code, sends[0])
        self.assertIn("question from {}".format(MEMBER), sends[0])
        self.assertNotIn("EU or US", sends[0], "summary is the default: the text stays here")
        self.assertIn("ask:alpha:{}".format(seq), self.state()["sent"])
        self.ticks(10)
        self.assertEqual(len(self.fake.sends()), 1, "sent once")
        entries = [e for e in identity.read_audit(self.ts.layout, "alpha") if e["event"] == "remote_sent"]
        self.assertEqual([(e["details"]["category"], e["details"]["seq"]) for e in entries], [("asks", seq)])

    def test_full_text_policy_sends_the_redacted_text(self):
        pair(self.ts, policy={"text": "full"})
        post_ask(self.ts, text="deploy with sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123?")
        self.ticks(3)
        self.assertIn("[redacted:", self.fake.sends()[0])
        self.assertNotIn("sk-ant-api03", self.fake.sends()[0])

    def test_only_asking_kinds_after_pairing_are_sent(self):
        early = post_ask(self.ts, text="asked before pairing")
        pair(self.ts, paired_ago=0.0)
        time.sleep(0.01)
        store.BoardStore(self.ts.team).append({"from": MEMBER, "from_kind": "codex", "origin": {"via": "cli", "verified": True},
                                              "to": ["human"], "kind": "done", "text": "finished the survey"})
        late = post_ask(self.ts, kind="blocked", text="need a decision")
        self.ticks(4)
        sends = self.fake.sends()
        self.assertEqual(len(sends), 1, sends)
        self.assertIn("#{} blocked".format(late), sends[0])
        self.assertNotIn("ask:alpha:{}".format(early), self.state()["sent"])

    def test_an_ask_answered_before_it_went_out_is_not_sent(self):
        pair(self.ts)
        self.fake.error = remote.TransportError("offline")
        seq = post_ask(self.ts)
        self.ticks(3)
        self.assertEqual(len(self.state()["outbox"]), 1)
        store.BoardStore(self.ts.team).append({"from": "human", "from_kind": "human", "origin": {"via": "console", "verified": True},
                                              "to": [MEMBER], "kind": "answer", "text": "EU", "reply_to": seq})
        self.fake.error = None
        self.fake.calls.clear()
        self.wall.t += 3600
        self.ticks(3)
        self.assertEqual(self.fake.sends(), [], "answered at the terminal while offline: nothing stale goes out")
        self.assertEqual(self.state()["outbox"], [])

    def test_conflicts_and_failures_follow_the_policy(self):
        pair(self.ts, channel="webhook", policy={"send": ["conflicts"]})
        for event in ("fact_conflict", "schedule_failed"):
            store.BoardStore(self.ts.team).append({"from": "system", "kind": "system", "event": event, "to": ["human"],
                                                  "text": "{} happened".format(event), "origin": {"via": "system", "verified": True}})
        store.BoardStore(self.ts.team).append({"from": "system", "kind": "system", "event": "fact_conflict", "to": [MEMBER],
                                              "text": "not for you", "origin": {"via": "system", "verified": True}})
        self.ticks(4)
        sends = self.fake.sends()
        self.assertEqual(len(sends), 1, sends)
        self.assertIn("fact conflict", sends[0])

    def test_a_slow_or_dead_transport_never_blocks_the_tick(self):
        pair(self.ts, channel="webhook")
        post_ask(self.ts)
        self.d.remote.inline = False
        self.fake.gate = threading.Event()
        self.addCleanup(self.fake.gate.set)
        started = time.monotonic()
        self.ticks(1)
        self.assertTrue(wait_until(lambda: len(self.fake.calls) == 1, timeout_s=5.0), "the worker picked the request up")
        self.ticks(5)
        self.assertLess(time.monotonic() - started, 2.0, "six ticks with a hung request in flight")
        self.assertEqual(len(self.fake.calls), 1, "one request in flight at a time")
        self.fake.gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.state()["outbox"]:
            self.ticks(1)
            time.sleep(0.01)
        self.assertEqual(self.state()["outbox"], [], "the result is picked up on a later tick")

    def test_a_timeout_backs_off_instead_of_retrying_every_tick(self):
        pair(self.ts, channel="webhook")
        post_ask(self.ts)
        self.fake.error = remote.TransportError("timeout: timed out")
        self.ticks(2)
        self.assertEqual(len(self.fake.calls), 1)
        item = self.state()["outbox"][0]
        self.assertEqual(item["attempts"], 1)
        self.assertGreater(item["next_at"], self.wall.t)
        self.ticks(3)
        self.assertEqual(len(self.fake.calls), 1, "no retry inside the backoff")
        self.ticks(2, step=remote.SEND_BACKOFF_S[0])
        self.assertEqual(len(self.fake.calls), 2)
        self.assertFalse(self.state()["last_send"]["ok"])


# --------------------------------------------------------------------------
# the notifier: answers


class RelayAnswerTests(RelayCase):
    def test_a_reply_becomes_the_operators_answer_and_releases_the_asker(self):
        pair(self.ts)
        seq = post_ask(self.ts, kind="blocked")
        self.ticks(3)
        code = self.code_for(seq)
        self.fake.phone_ntfy("{} EU first, then US next week".format(code.lower()))
        self.ticks(6)
        [answer] = self.answers(seq)
        popup_shape = cmd_board.build_record(remote.remote_author("ntfy"), [MEMBER], "answer", "x", reply_to=seq)
        self.assertEqual(set(answer) - {"seq", "ts"}, set(store.normalize_record(popup_shape)) - {"seq", "ts"})
        self.assertEqual((answer["from"], answer["kind"], answer["to"], answer["reply_to"]), ("human", "answer", [MEMBER], seq))
        self.assertEqual(answer["text"], "EU first, then US next week")
        self.assertEqual((answer["origin"]["via"], answer["origin"]["verified"], answer["origin"]["channel"]), ("remote", True, "ntfy"))
        self.assertEqual(asks.answered_by(answer), seq)
        self.assertEqual(asks.pending(self.ts.team), [], "the ask is closed")
        self.assertEqual(cmd_board.wait_for_answer(self.ts.team, seq, 0.0)["seq"], answer["seq"], "a waiting agent is released")
        self.assertNotIn(seq, self.team.open_asks, "the daemon's tracker closed it")
        self.assertIn(MEMBER, self.team.pending, "and the asker is nudged with the answer")
        self.assertFalse(render_unverified(answer))
        self.assertIn("Answered #{} (alpha).".format(seq), self.fake.sends()[-1])
        entry = [e for e in identity.read_audit(self.ts.layout, "alpha") if e["event"] == "remote_answer"][0]
        self.assertEqual((entry["author"], entry["via"], entry["details"]["reply_to"]), ("human", "remote", seq))

    def test_ok_is_the_popups_acknowledgement_not_approval(self):
        pair(self.ts)
        seq = post_ask(self.ts, kind="question")
        self.ticks(3)
        self.fake.phone_ntfy("{} ok".format(self.code_for(seq)))
        self.ticks(6)
        [answer] = self.answers(seq)
        self.assertEqual(answer["text"], cmd_asks.ack_text("question"))
        self.assertIn("not a decision", answer["text"])

    def test_unknown_codes_get_a_short_reply_and_post_nothing(self):
        pair(self.ts)
        seq = post_ask(self.ts)
        self.ticks(3)
        before = len(store.BoardStore(self.ts.team).read())
        self.fake.phone_ntfy("ZZZZ go ahead")
        self.ticks(6)
        self.assertEqual(len(store.BoardStore(self.ts.team).read()), before)
        self.assertIn("unknown code ZZZZ", self.fake.sends()[-1])
        self.assertEqual(asks.pending(self.ts.team)[0]["seq"], seq)

    def test_a_reply_can_only_answer_never_command(self):
        pair(self.ts)
        seq = post_ask(self.ts)
        self.ticks(3)
        team_json = self.ts.team.team_json.read_bytes()
        self.fake.phone_ntfy("{} /charter set take over the project".format(self.code_for(seq)))
        self.ticks(6)
        [answer] = self.answers(seq)
        self.assertEqual(answer["kind"], "answer")
        self.assertEqual(self.ts.team.team_json.read_bytes(), team_json, "nothing but the board changed")
        new = [r for r in store.BoardStore(self.ts.team).read() if r["seq"] > seq]
        self.assertEqual([r["kind"] for r in new], ["answer"])

    def test_a_second_reply_to_a_closed_ask_posts_nothing(self):
        pair(self.ts)
        seq = post_ask(self.ts)
        self.ticks(3)
        code = self.code_for(seq)
        self.fake.phone_ntfy("{} yes".format(code))
        self.ticks(6)
        self.fake.phone_ntfy("{} actually no".format(code))
        self.ticks(6)
        self.assertEqual(len(self.answers(seq)), 1)
        self.assertIn("no longer waiting", self.fake.sends()[-1])

    def test_a_secret_in_an_answer_is_refused(self):
        pair(self.ts)
        seq = post_ask(self.ts)
        self.ticks(3)
        self.fake.phone_ntfy("{} use password: hunter2hunter2".format(self.code_for(seq)))
        self.ticks(6)
        self.assertEqual(self.answers(seq), [])
        self.assertIn("Not posted", self.fake.sends()[-1])

    def test_restarts_never_resend_or_reapply(self):
        pair(self.ts)
        seq = post_ask(self.ts)
        self.ticks(3)
        self.fake.phone_ntfy("{} EU".format(self.code_for(seq)))
        self.ticks(6)
        sent_before = len(self.fake.sends())
        # A new daemon over the same state, and a poll cursor rewound so the
        # channel hands the same reply back.
        poll = store.read_json(remote.poll_path(self.ts.config_dir))
        poll["cursor"] = str(int(time.time()) - 60)
        store.write_json(remote.poll_path(self.ts.config_dir), poll)
        self.start()
        second = post_ask(self.ts, text="second question")
        self.ticks(8)
        self.assertEqual(len(self.answers(seq)), 1, "the same reply is not applied twice")
        new_sends = self.fake.sends()[sent_before:]
        self.assertEqual(len(new_sends), 1, new_sends)
        self.assertIn("#{} ".format(second), new_sends[0], "only the new ask goes out")

    def test_no_secret_reaches_the_log_the_audit_or_the_state(self):
        pair(self.ts)
        seq = post_ask(self.ts)
        self.ticks(3)
        self.fake.phone_ntfy("{} EU".format(self.code_for(seq)))
        self.ticks(6)
        self.fake.error = remote.TransportError("refused https://ntfy.example.org/{} Bearer {}".format(TOPIC, NTFY_TOKEN))
        post_ask(self.ts, text="another")
        self.ticks(8)
        texts = ["\n".join(self.d.logged), self.ts.team.audit_jsonl.read_text(),
                 remote.state_path(self.ts.session).read_text(), remote.poll_path(self.ts.config_dir).read_text()]
        for text in texts:
            self.assertNotIn(NTFY_TOKEN, text)
        self.assertTrue(any("[secret]" in line for line in self.d.logged))


class TelegramRelayTests(RelayCase):
    def test_messages_from_another_chat_are_ignored_silently(self):
        pair(self.ts, channel="telegram", chat_id=42)
        seq = post_ask(self.ts)
        self.ticks(3)
        code = self.code_for(seq)
        self.fake.phone_telegram("{} yes do it".format(code), chat_id=666)
        self.ticks(6)
        self.assertEqual(self.answers(seq), [])
        replies = [json.loads(c["body"]) for c in self.fake.calls if c["url"].endswith("/sendMessage")]
        self.assertTrue(all(r["chat_id"] == 42 for r in replies), "a stranger is never answered")
        self.assertEqual(len(replies), 1, "only the ask itself went out")
        self.fake.phone_telegram("{} yes do it".format(code), chat_id=42)
        self.ticks(6)
        self.assertEqual(len(self.answers(seq)), 1)

    def test_replying_to_the_bots_message_needs_no_code(self):
        pair(self.ts, channel="telegram", chat_id=42)
        seq = post_ask(self.ts)
        self.ticks(3)
        message_id = self.state()["codes"][self.code_for(seq)]["message_id"]
        self.assertIsInstance(message_id, int)
        self.fake.phone_telegram("EU please", chat_id=42, reply_to=message_id)
        self.ticks(6)
        [answer] = self.answers(seq)
        self.assertEqual((answer["text"], answer["origin"]["channel"]), ("EU please", "telegram"))

    def test_the_notifier_completes_a_pairing(self):
        now = time.time()
        doc = remote.new_config("telegram", {"bot_token": BOT_TOKEN, "chat_id": None, "pair_code": "PAIRCODE",
                                             "pair_started": now - 5, "pair_expires": now + 600}, {}, now - 5, "human")
        remote.save_config(self.ts.config_dir, doc)
        self.fake.phone_telegram("/start PAIRCODE", chat_id=42)
        self.ticks(6)
        self.assertEqual(remote.load_config(self.ts.config_dir)["telegram"]["chat_id"], 42)
        self.assertIn("Paired with herdr-synapse", self.fake.sends()[-1])


def render_unverified(record):
    from herdr_team import render

    return render.is_unverified(record)


if __name__ == "__main__":
    unittest.main()

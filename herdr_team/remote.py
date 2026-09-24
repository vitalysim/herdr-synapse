"""Phone reach: the operator's asks, sent to a phone, and the answers brought back.

An ask to the operator (``herdr_team.asks``) has exactly one place to be
answered: the popup inside the terminal. That is right while the operator sits
at it and wrong for a team left running overnight, where a ``blocked`` post at
01:00 waits until breakfast. This module is the opt-in way out. It is outbound
HTTPS only and never opens a listening port, so nothing on the network can
reach the machine; replies are *fetched* by the notifier on its own schedule.

Three channels sit behind one tiny transport (``Transport.fetch``):

* ``ntfy`` publishes to ``<server>/<topic>``. Replies are read back only with
  an access token. An open ntfy topic can be read and written by anyone who
  guesses its name, and an answer from a stranger must never reach an agent as
  the operator's word. Without a token the channel is outbound-only.
* ``telegram`` uses a bot the operator created. Pairing pins one private chat,
  and messages from any other chat are ignored without a reply.
* ``webhook`` is a JSON POST in Slack's incoming-webhook shape. It is
  outbound-only.

A reply can do very little on purpose: answer or acknowledge one ask that is
still waiting, through the same record the popup writes, with origin
``{"via": "remote", "verified": true}``. That origin counts as the human for
delivery and for closing the ask (``identity.record_human_ok``) and for
nothing else. No CLI author ever carries it, so it can never pass an authority
gate. A lost phone can answer questions; it cannot change a charter, a grant,
or this module's own policy.

Where things live:

* ``<config_dir>/plugins/config/herdr-synapse/remote.json`` (0600) holds the
  channel, its secret, and the policy. There is one per Herdr config dir,
  shared by every session under it, so messages name the session and team.
* ``remote-poll.json`` beside it holds the channel's read cursor, the reply ids
  already handled, and a short lease. The cursor belongs to the channel rather
  than to a session: Telegram forgets updates once any reader moves past them,
  so two sessions keeping their own cursors would steal each other's answers.
  Whichever notifier holds the lease reads, and routes each answer to the
  session that issued its code.
* ``<session>/remote-state.json`` holds what this session has sent (dedupe
  keys, so a restart never resends), the codes it handed out, the outbox, and
  the last send and poll results.

The notifier owns all of this at runtime (``RemoteRelay.tick``). The network
runs on one worker thread, so a dead DNS resolver or a slow server costs the
tick nothing: the tick hands over at most one request and collects the result
on a later pass. Board writes stay on the notifier's own thread.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from herdr_team import asks as _asks
from herdr_team import paths as _paths
from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, EXIT_USAGE, HerdrTeamError, LockTimeout
from herdr_team.identity import AUTHOR_HUMAN, AUTHOR_SYSTEM, VIA_REMOTE, VIA_SYSTEM, Author, audit
from herdr_team.roster import parse_iso

CHANNELS = ("ntfy", "telegram", "webhook")
#: What may be sent. ``settled`` tells the phone that an ask it was shown was
#: answered or withdrawn in Herdr, so nobody answers it twice; off by default
#: because the operator who answered at the terminal already knows.
CATEGORIES = ("asks", "conflicts", "failures", "settled")
DEFAULT_SEND = ("asks", "conflicts", "failures")
TEXT_MODES = ("full", "summary", "none")
#: The ask text is the part most likely to hold something private, so it stays
#: on the machine until the operator opts into ``full``.
DEFAULT_TEXT = "summary"
#: System records worth a phone call, by the policy category that sends them.
RELAYED_EVENTS = {"fact_conflict": "conflicts", "schedule_failed": "failures"}
EVENT_LABELS = {"fact_conflict": "fact conflict", "schedule_failed": "failed schedule"}
NONE_TEXT = "1 item waiting in Herdr"

#: Every HTTP call is bounded; the worker thread absorbs what the timeout
#: cannot (name resolution is not covered by it).
HTTP_TIMEOUT_S = 5.0
MAX_RESPONSE_BYTES = 256 * 1024
#: At most one poll per channel this often while a code is out or a pairing is
#: pending, and a slow heartbeat otherwise, so stray messages still get an
#: answer without hammering a free service all night.
POLL_ACTIVE_S = 5.0
POLL_IDLE_S = 60.0
POLL_ERROR_S = 30.0
CONFIG_CHECK_S = 2.0
COLLECT_S = 2.0
#: One message per second at most: Telegram's per-chat limit, and polite to ntfy.sh.
SEND_GAP_S = 1.0
SEND_BACKOFF_S = (5.0, 15.0, 60.0, 300.0, 900.0)
SEND_MAX_ATTEMPTS = 8
HUNG_S = 30.0
LEASE_S = 30.0
LEASE_LOCK_WAIT_S = 2.0
#: Ask text sent in ``full`` mode, after redaction.
TEXT_CAP = 500
#: Four characters from an alphabet without 0/O and 1/I: typeable on a phone
#: keyboard, and a million combinations against a handful of live asks.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 4
PAIR_CODE_LEN = 8
CODE_TTL_S = 7 * 86400.0
SENT_TTL_S = 30 * 86400.0
SENT_MAX = 2000
PAIR_CODE_TTL_S = 900.0
OUTBOX_MAX = 100
SEEN_MAX = 300
POLL_STATE_WRITE_S = 60.0
ACK_WORDS = ("ok", "ack")
NTFY_TAG = "herdr-synapse"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
TELEGRAM_API = "https://api.telegram.org"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
BOT_TOKEN_RE = re.compile(r"^[0-9]{3,16}:[A-Za-z0-9_-]{20,128}\Z")
TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,512}\Z")
#: A leading code: four letters or digits standing alone, then the answer.
REPLY_RE = re.compile(r"^\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])[\s:,.;!-]*(.*)\Z", re.S)
START_RE = re.compile(r"^/start(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{1,64})\s*\Z")


# --------------------------------------------------------------------------
# paths and the config document


def config_path(config_dir: Path) -> Path:
    return _paths.plugin_config_dir(config_dir) / "remote.json"


def poll_path(config_dir: Path) -> Path:
    return _paths.plugin_config_dir(config_dir) / "remote-poll.json"


def lock_path(config_dir: Path) -> Path:
    return _paths.plugin_config_dir(config_dir) / "remote.lock"


def state_path(session: _paths.SessionPaths) -> Path:
    return session.root / "remote-state.json"


def config_lock(config_dir: Path, timeout: float = 5.0) -> store.FileLock:
    """Guards ``remote.json`` and ``remote-poll.json`` for the CLI and every session's notifier."""
    return store.FileLock(lock_path(config_dir), timeout, code="remote_locked")


def load_config(config_dir: Path) -> Dict[str, Any]:
    """The parsed ``remote.json``, or ``{}``. A symlink raises ``path_symlink``."""
    doc = store.read_json(config_path(config_dir), default=None)
    return doc if isinstance(doc, dict) else {}


def save_config(config_dir: Path, doc: Dict[str, Any]) -> None:
    """Atomic write, mode 0600, into a 0700 directory."""
    path = config_path(config_dir)
    _paths.ensure_dir(path.parent)
    store.write_json(path, doc)


def remove_file(path: Path) -> None:
    _paths.check_not_symlink(path)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def channel_of(doc: Mapping[str, Any]) -> Optional[str]:
    channel = doc.get("channel")
    return channel if channel in CHANNELS and isinstance(doc.get(channel), dict) else None


def _section(doc: Mapping[str, Any]) -> Dict[str, Any]:
    channel = channel_of(doc)
    return dict(doc.get(channel) or {}) if channel else {}


def is_paired(doc: Mapping[str, Any]) -> bool:
    """A channel that can carry a message now (Telegram needs its chat pinned)."""
    channel = channel_of(doc)
    if channel == "telegram":
        return _section(doc).get("chat_id") is not None
    return channel is not None


def receives(doc: Mapping[str, Any]) -> bool:
    """Whether answers are read back at all."""
    channel = channel_of(doc)
    if channel == "ntfy":
        return bool(_section(doc).get("token"))
    return channel == "telegram" and is_paired(doc)


def pending_pair(doc: Mapping[str, Any], now: float) -> bool:
    """A Telegram pairing that is waiting for ``/start <code>`` and has not expired."""
    if channel_of(doc) != "telegram" or is_paired(doc):
        return False
    return float(_section(doc).get("pair_expires") or 0) > now


def outbound_only_reason(doc: Mapping[str, Any]) -> Optional[str]:
    channel = channel_of(doc)
    if channel == "ntfy" and not receives(doc):
        return "no access token, so the topic is public and replies are never read"
    if channel == "webhook":
        return "a webhook only posts"
    return None


def policy_of(doc: Mapping[str, Any]) -> Dict[str, Any]:
    raw = doc.get("policy") if isinstance(doc.get("policy"), dict) else {}
    send = raw.get("send")
    if not isinstance(send, list):
        send = list(DEFAULT_SEND)
    text = raw.get("text") if raw.get("text") in TEXT_MODES else DEFAULT_TEXT
    return {"send": [c for c in CATEGORIES if c in send], "text": text}


def parse_send(value: str) -> List[str]:
    """``asks,conflicts`` -> the category list; ``none`` sends nothing (the channel stays paired)."""
    items = [v.strip().lower() for v in str(value or "").split(",") if v.strip()]
    if items == ["none"]:
        return []
    unknown = [v for v in items if v not in CATEGORIES]
    if unknown or not items:
        raise HerdrTeamError("usage", "--send takes a comma list of: {} (or none)".format(", ".join(CATEGORIES)), EXIT_USAGE,
                             {"unknown": unknown})
    return [c for c in CATEGORIES if c in items]


def secret_values(doc: Mapping[str, Any]) -> List[str]:
    """Every string that must never appear in a log line, the audit trail, or the state file."""
    out: List[str] = []
    for channel in CHANNELS:
        section = doc.get(channel)
        if not isinstance(section, dict):
            continue
        for key in ("token", "bot_token", "url"):
            value = section.get(key)
            if isinstance(value, str) and value:
                out.append(value)
        if channel == "ntfy" and not section.get("token") and isinstance(section.get("topic"), str):
            out.append(section["topic"])  # an open topic's name is its only password
    return out


def scrub(text: Any, doc: Mapping[str, Any]) -> str:
    """``text`` with every secret of ``doc`` replaced; for errors that may echo a URL."""
    out = str(text or "")
    for value in sorted(secret_values(doc), key=len, reverse=True):
        out = out.replace(value, "[secret]")
    return out[:300]


def _mask(value: Any) -> str:
    text = str(value or "")
    return (text[:2] + "…({} chars)".format(len(text))) if text else ""


def destination(doc: Mapping[str, Any]) -> Optional[str]:
    """Where messages go, without anything that would let someone else read or post there."""
    channel = channel_of(doc)
    section = _section(doc)
    if channel == "ntfy":
        host = urllib.parse.urlsplit(str(section.get("server") or "")).netloc or "?"
        return "ntfy at {}, topic {}{}".format(host, _mask(section.get("topic")), " with an access token" if section.get("token") else "")
    if channel == "telegram":
        return "Telegram bot, {}".format("one private chat pinned" if section.get("chat_id") is not None else "waiting for /start <code>")
    if channel == "webhook":
        return "webhook at {}".format(urllib.parse.urlsplit(str(section.get("url") or "")).netloc or "?")
    return None


def leaves_machine(policy: Mapping[str, Any]) -> str:
    """One honest sentence about what the policy sends to a third-party service."""
    if not policy.get("send"):
        return "nothing: sending is switched off"
    what = {
        "full": "the session and team, who asked, the kind of post, and its text (secrets redacted, at most {} characters)".format(TEXT_CAP),
        "summary": "the session and team, who asked, and the kind of post; never the text",
        "none": "only \"{}\"; no names and no text".format(NONE_TEXT),
    }[str(policy.get("text") or DEFAULT_TEXT)]
    return "{} for: {}".format(what, ", ".join(policy["send"]))


def iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    moment = datetime.fromtimestamp(float(epoch), timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(moment.microsecond // 1000)


def public_view(doc: Mapping[str, Any], now: float) -> Dict[str, Any]:
    """The config as ``status`` and ``doctor`` show it: no secret, no full topic, no chat id."""
    channel = channel_of(doc)
    policy = policy_of(doc)
    section = _section(doc)
    expired = channel == "telegram" and not is_paired(doc) and not pending_pair(doc, now)
    return {
        "channel": channel,
        "paired": is_paired(doc),
        "receives": receives(doc),
        "outbound_only": bool(channel) and not receives(doc) and not pending_pair(doc, now) and not expired,
        "outbound_only_reason": outbound_only_reason(doc),
        "pending_pair": pending_pair(doc, now),
        "pair_expired": expired,
        "destination": destination(doc),
        "paired_at": iso(doc.get("paired_at")) if isinstance(doc.get("paired_at"), (int, float)) else None,
        "policy": policy,
        "leaves_machine": leaves_machine(policy) if channel else "nothing: no channel is paired",
        "token_configured": bool(section.get("token") or section.get("bot_token")),
    }


# --------------------------------------------------------------------------
# secrets and validation (the CLI's half)


def read_secret(env: Mapping[str, str], file_path: Optional[str], env_var: Optional[str], what: str, required: bool = True) -> Optional[str]:
    """A secret from a file (first line) or from a named environment variable, never from argv.

    ``argv`` is readable by every user through ``ps`` and lands in shell
    history; a file or a variable does neither.
    """
    if file_path and env_var:
        raise HerdrTeamError("usage", "give the {} as a file or an environment variable, not both".format(what), EXIT_USAGE)
    value: Optional[str] = None
    if file_path:
        path = Path(os.path.expanduser(file_path))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read(8192)
        except (OSError, UnicodeDecodeError) as err:
            raise HerdrTeamError("remote_secret_unreadable", "cannot read the {} from {}: {}".format(what, path, getattr(err, "strerror", None) or type(err).__name__),
                                 EXIT_REFUSED, {"path": os.fspath(path)})
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        value = lines[0] if lines else ""
        if not value:
            raise HerdrTeamError("remote_secret_unreadable", "{} is empty".format(path), EXIT_REFUSED, {"path": os.fspath(path)})
    elif env_var:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\Z", env_var):
            raise HerdrTeamError("usage", "{} is not an environment variable name".format(env_var), EXIT_USAGE)
        value = str(env.get(env_var) or "").strip()
        if not value:
            raise HerdrTeamError("remote_secret_unreadable", "environment variable {} is not set".format(env_var), EXIT_REFUSED, {"env": env_var})
    elif required:
        raise HerdrTeamError("usage", "the {} is required: pass it as a file or an environment variable name".format(what), EXIT_USAGE)
    return value


def validate_topic(topic: Any) -> str:
    text = str(topic or "")
    if not TOPIC_RE.match(text):
        raise HerdrTeamError("remote_invalid", "an ntfy topic is 1-64 letters, digits, - or _", EXIT_REFUSED)
    return text


def _https_url(value: str, what: str, loopback_http: bool) -> str:
    text = str(value or "").strip()
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        parts = None
    if parts is None or not parts.netloc or any(c.isspace() for c in text):
        raise HerdrTeamError("remote_invalid", "the {} is not a URL".format(what), EXIT_REFUSED)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" or (loopback_http and parts.scheme == "http" and host in LOOPBACK_HOSTS):
        return text.rstrip("/")
    raise HerdrTeamError("remote_invalid", "the {} must use https (http only on this machine)".format(what), EXIT_REFUSED, {"host": host})


def validate_server(server: Any) -> str:
    return _https_url(str(server or DEFAULT_NTFY_SERVER), "ntfy server", loopback_http=True)


def validate_webhook(url: Any) -> str:
    return _https_url(str(url or ""), "webhook URL", loopback_http=False)


def validate_bot_token(token: Any) -> str:
    text = str(token or "")
    if not BOT_TOKEN_RE.match(text):
        raise HerdrTeamError("remote_invalid", "that does not look like a Telegram bot token (<digits>:<letters>, from @BotFather)", EXIT_REFUSED)
    return text


def validate_token(token: Any) -> str:
    text = str(token or "")
    if not TOKEN_RE.match(text):
        raise HerdrTeamError("remote_invalid", "an access token is one line of 8-512 printable characters without spaces", EXIT_REFUSED)
    return text


def new_code(taken: Any = (), length: int = CODE_LEN) -> str:
    for _ in range(64):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
        if code not in taken:
            return code
    raise HerdrTeamError("remote_codes_exhausted", "could not pick an unused code", EXIT_REFUSED)


def new_config(channel: str, settings: Dict[str, Any], previous: Mapping[str, Any], now: float, by: str) -> Dict[str, Any]:
    """A fresh pairing; the policy survives a re-pair, the old channel's secret does not."""
    doc: Dict[str, Any] = {
        "v": 1, "channel": channel, "pair_id": secrets.token_hex(8), "paired_at": float(now), "paired_by": by,
        channel: dict(settings),
    }
    if isinstance(previous.get("policy"), dict):
        doc["policy"] = policy_of(previous)
    return doc


def unpaired_config(previous: Mapping[str, Any]) -> Dict[str, Any]:
    """What ``unpair`` leaves behind: the policy only."""
    doc: Dict[str, Any] = {"v": 1}
    if isinstance(previous.get("policy"), dict):
        doc["policy"] = policy_of(previous)
    return doc


def pairing_match(doc: Mapping[str, Any], message: "Incoming", now: float) -> bool:
    """``/start <code>`` from a private chat, before the code expires and after the pairing began."""
    if not pending_pair(doc, now):
        return False
    section = _section(doc)
    match = START_RE.match((message.text or "").strip())
    if match is None or message.chat_type != "private" or message.chat_id is None:
        return False
    if message.date is not None and message.date < float(section.get("pair_started") or 0) - 5:
        return False
    return secrets.compare_digest(match.group(1), str(section.get("pair_code") or ""))


def pin_chat(config_dir: Path, pair_id: Any, chat_id: int) -> Optional[Dict[str, Any]]:
    """Pin the chat that sent the pairing code; None when the pairing changed meanwhile."""
    with config_lock(config_dir):
        doc = load_config(config_dir)
        if channel_of(doc) != "telegram" or doc.get("pair_id") != pair_id or is_paired(doc):
            return None
        section = dict(doc["telegram"])
        section["chat_id"] = int(chat_id)
        for key in ("pair_code", "pair_expires"):
            section.pop(key, None)
        doc["telegram"] = section
        doc["paired_at"] = time.time()
        save_config(config_dir, doc)
        return doc


# --------------------------------------------------------------------------
# message text


def redact(text: Any) -> str:
    """The board text with every secret pattern replaced by ``[redacted:<kind>]``, then capped.

    Redaction runs before the cap so a key cut in half can never slip past a
    pattern that needs the whole of it.
    """
    from herdr_team.cmd_board import SECRET_PATTERNS

    out = str(text or "")
    for label, pattern in SECRET_PATTERNS:
        out = pattern.sub("[redacted:{}]".format(label), out)
    out = out.strip()
    if len(out) > TEXT_CAP:
        out = out[: TEXT_CAP - 1].rstrip() + "…"
    return out


def _how(code: Optional[str]) -> str:
    if not code:
        return "Answer it in Herdr."
    return "Reply: {c} <your answer>   (or {c} ok to acknowledge)".format(c=code)


def compose_ask(slug: str, team: str, record: Mapping[str, Any], code: Optional[str], mode: str) -> Tuple[str, str]:
    if mode == "none":
        return "Herdr", NONE_TEXT
    seq, asker, kind = record.get("seq"), record.get("from"), record.get("kind")
    title = "{}/{}: {} from {}".format(slug, team, kind, asker)
    if mode == "full":
        return title, "#{} {}\n\n{}".format(seq, redact(record.get("text")), _how(code))
    return title, "#{} {} from {} in {}/{} is waiting on you.\n{}".format(seq, kind, asker, slug, team, _how(code))


def compose_event(slug: str, team: str, record: Mapping[str, Any], mode: str) -> Tuple[str, str]:
    if mode == "none":
        return "Herdr", NONE_TEXT
    label = EVENT_LABELS.get(str(record.get("event")), str(record.get("event")))
    title = "{}/{}: {}".format(slug, team, label)
    if mode == "full":
        return title, "#{} {}\n\nSee it in Herdr.".format(record.get("seq"), redact(record.get("text")))
    return title, "#{} a {} in {}/{} needs you. See it in Herdr.".format(record.get("seq"), label, slug, team)


def compose_test(slug: str, doc: Mapping[str, Any]) -> Tuple[str, str]:
    how = ("Answers come back: reply to an ask with its code." if receives(doc)
           else "This channel is outbound-only: answer asks in Herdr.")
    return "herdr-synapse", "Test from herdr-synapse ({}): phone reach works. {}".format(slug, how)


def _ascii(text: str) -> str:
    return " ".join(text.encode("ascii", "replace").decode("ascii").split())[:200]


# --------------------------------------------------------------------------
# transport


class TransportError(Exception):
    """The request never produced an HTTP status (no route, timeout, TLS)."""


@dataclass
class HttpResponse:
    status: int
    body: bytes = b""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could carry an ``Authorization`` header somewhere else; refuse it."""

    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:  # type: ignore[override]
        return None


class Transport:
    """The whole network surface: one bounded HTTP exchange. Tests replace it."""

    def fetch(self, method: str, url: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
              timeout: float = HTTP_TIMEOUT_S) -> HttpResponse:
        raise NotImplementedError


class UrllibTransport(Transport):
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def fetch(self, method: str, url: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
              timeout: float = HTTP_TIMEOUT_S) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            with self._opener.open(request, timeout=min(float(timeout), HTTP_TIMEOUT_S)) as response:
                return HttpResponse(int(response.status), response.read(MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as err:
            try:
                data = err.read(MAX_RESPONSE_BYTES) or b""
            except (OSError, ValueError):
                data = b""
            return HttpResponse(int(err.code), data)
        except urllib.error.URLError as err:
            # ``reason`` is the socket error; the URL (which may hold a token) is never in it.
            raise TransportError("{}: {}".format(type(err.reason).__name__, err.reason))
        except (OSError, ValueError) as err:
            raise TransportError("{}: {}".format(type(err).__name__, err))
        except Exception as err:  # noqa: BLE001 - http.client raises its own family; all of it is "no response"
            raise TransportError(type(err).__name__)


#: Replaced by tests; the CLI and the notifier both build their transport here.
TRANSPORT_FACTORY: Callable[[], Transport] = UrllibTransport


def make_transport() -> Transport:
    return TRANSPORT_FACTORY()


# --------------------------------------------------------------------------
# channels: pure request builders and response readers (run on the worker)


@dataclass
class Incoming:
    """One message read back from the channel."""

    id: str
    text: str
    chat_id: Optional[int] = None
    chat_type: Optional[str] = None
    reply_to_message_id: Optional[int] = None
    date: Optional[float] = None


@dataclass
class SendOutcome:
    ok: bool
    message_id: Optional[int] = None
    error: Optional[str] = None


@dataclass
class PollOutcome:
    ok: bool
    incoming: List[Incoming] = field(default_factory=list)
    cursor: Any = None
    error: Optional[str] = None


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


def _http_error(response: HttpResponse) -> str:
    detail = ""
    parsed = _json(response.body)
    if isinstance(parsed, dict):
        detail = str(parsed.get("description") or parsed.get("error") or "")
    return "HTTP {}{}".format(response.status, ": " + detail[:120] if detail else "")


def _telegram_url(doc: Mapping[str, Any], method: str) -> str:
    return "{}/bot{}/{}".format(TELEGRAM_API, _section(doc).get("bot_token"), method)


def _ntfy_headers(doc: Mapping[str, Any]) -> Dict[str, str]:
    token = _section(doc).get("token")
    return {"Authorization": "Bearer {}".format(token)} if token else {}


def _ntfy_topic_url(doc: Mapping[str, Any]) -> str:
    section = _section(doc)
    return "{}/{}".format(str(section.get("server") or DEFAULT_NTFY_SERVER).rstrip("/"), urllib.parse.quote(str(section.get("topic") or ""), safe=""))


def send_message(doc: Mapping[str, Any], transport: Transport, title: str, body: str, priority: int = 3) -> SendOutcome:
    """Send one message on the paired channel; never raises."""
    channel = channel_of(doc)
    try:
        if channel == "ntfy":
            headers = dict(_ntfy_headers(doc), Title=_ascii(title), Priority=str(max(1, min(5, int(priority)))), Tags=NTFY_TAG)
            headers["Content-Type"] = "text/plain; charset=utf-8"
            response = transport.fetch("POST", _ntfy_topic_url(doc), body.encode("utf-8"), headers)
        elif channel == "telegram":
            chat = _section(doc).get("chat_id")
            if chat is None:
                return SendOutcome(False, error="telegram chat not paired yet")
            payload = {"chat_id": chat, "text": "{}\n{}".format(title, body), "disable_web_page_preview": True}
            response = transport.fetch("POST", _telegram_url(doc, "sendMessage"), json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                       {"Content-Type": "application/json"})
        elif channel == "webhook":
            payload = {"text": "{}\n{}".format(title, body)}
            response = transport.fetch("POST", str(_section(doc).get("url")), json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                       {"Content-Type": "application/json"})
        else:
            return SendOutcome(False, error="no channel is paired")
    except TransportError as err:
        return SendOutcome(False, error=scrub(err, doc))
    except Exception as err:  # noqa: BLE001 - a broken fake or transport is a failed send, not a dead notifier
        return SendOutcome(False, error=scrub("{}: {}".format(type(err).__name__, err), doc))
    if not 200 <= response.status < 300:
        return SendOutcome(False, error=scrub(_http_error(response), doc))
    message_id: Optional[int] = None
    parsed = _json(response.body)
    if channel == "telegram" and isinstance(parsed, dict):
        if not parsed.get("ok"):
            return SendOutcome(False, error=scrub(_http_error(response), doc))
        result = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
        mid = result.get("message_id")
        message_id = mid if isinstance(mid, int) and not isinstance(mid, bool) else None
    return SendOutcome(True, message_id=message_id)


def _telegram_incoming(update: Any) -> Tuple[Optional[int], Optional[Incoming]]:
    if not isinstance(update, dict):
        return None, None
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        return None, None
    message = update.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return update_id, None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
    chat_id = chat.get("id") if isinstance(chat.get("id"), int) else None
    reply_id = reply.get("message_id") if isinstance(reply.get("message_id"), int) else None
    date = message.get("date") if isinstance(message.get("date"), (int, float)) else None
    return update_id, Incoming("tg:{}".format(update_id), message["text"], chat_id, str(chat.get("type") or ""), reply_id,
                               float(date) if date is not None else None)


def poll_messages(doc: Mapping[str, Any], transport: Transport, cursor: Any) -> PollOutcome:
    """One non-blocking read of new messages; never raises, never long-polls."""
    channel = channel_of(doc)
    try:
        if channel == "telegram":
            query = urllib.parse.urlencode({"timeout": 0, "limit": 50, "offset": int(cursor or 0), "allowed_updates": json.dumps(["message"])})
            response = transport.fetch("GET", "{}?{}".format(_telegram_url(doc, "getUpdates"), query))
        elif channel == "ntfy":
            query = urllib.parse.urlencode({"poll": 1, "since": str(cursor or "all")})
            response = transport.fetch("GET", "{}/json?{}".format(_ntfy_topic_url(doc), query), headers=_ntfy_headers(doc))
        else:
            return PollOutcome(False, cursor=cursor, error="this channel does not read replies")
    except TransportError as err:
        return PollOutcome(False, cursor=cursor, error=scrub(err, doc))
    except Exception as err:  # noqa: BLE001 - see send_message
        return PollOutcome(False, cursor=cursor, error=scrub("{}: {}".format(type(err).__name__, err), doc))
    if not 200 <= response.status < 300:
        return PollOutcome(False, cursor=cursor, error=scrub(_http_error(response), doc))
    incoming: List[Incoming] = []
    new_cursor = cursor
    if channel == "telegram":
        parsed = _json(response.body)
        if not isinstance(parsed, dict) or not parsed.get("ok") or not isinstance(parsed.get("result"), list):
            return PollOutcome(False, cursor=cursor, error="telegram: unexpected getUpdates answer")
        for update in parsed["result"]:
            update_id, message = _telegram_incoming(update)
            if update_id is not None:
                new_cursor = max(int(new_cursor or 0), update_id + 1)
            if message is not None:
                incoming.append(message)
        return PollOutcome(True, incoming, new_cursor)
    for line in response.body.decode("utf-8", "replace").splitlines():
        event = _json(line.encode("utf-8")) if line.strip() else None
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            continue
        new_cursor = event["id"]
        if event.get("event") != "message" or NTFY_TAG in (event.get("tags") or []):
            continue  # keepalives, and the messages this plugin published itself
        text = event.get("message")
        if isinstance(text, str):
            date = event.get("time") if isinstance(event.get("time"), (int, float)) else None
            incoming.append(Incoming("ntfy:{}".format(event["id"]), text, date=float(date) if date is not None else None))
    return PollOutcome(True, incoming, new_cursor)


def bot_username(doc: Mapping[str, Any], transport: Transport) -> Optional[str]:
    """``getMe``'s username for a ``t.me`` link; None on any failure (the link is a convenience)."""
    try:
        response = transport.fetch("GET", _telegram_url(doc, "getMe"))
    except Exception:  # noqa: BLE001 - optional nicety
        return None
    parsed = _json(response.body) if 200 <= response.status < 300 else None
    result = parsed.get("result") if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict) else {}
    name = result.get("username")
    return name if isinstance(name, str) and re.match(r"^[A-Za-z0-9_]{3,64}\Z", name) else None


def parse_reply(text: str) -> Tuple[Optional[str], str]:
    """``("K7QX", "yes, go ahead")`` for ``k7qx yes, go ahead``; ``(None, text)`` without a code."""
    match = REPLY_RE.match(text or "")
    if match is None:
        return None, (text or "").strip()
    code = match.group(1).upper()
    if any(c not in CODE_ALPHABET for c in code):
        return None, (text or "").strip()
    return code, match.group(2).strip()


def is_ack(text: str) -> bool:
    return text.strip().lower().rstrip(".!") in ACK_WORDS


# --------------------------------------------------------------------------
# per-session state


def _empty_state() -> Dict[str, Any]:
    return {"v": 1, "pair_id": None, "codes": {}, "sent": {}, "outbox": [], "last_send": None, "last_poll": None}


def read_state(session: _paths.SessionPaths) -> Dict[str, Any]:
    doc = store.read_json(state_path(session), default=None)
    state = _empty_state()
    if not isinstance(doc, dict):
        return state
    for key in ("codes", "sent"):
        if isinstance(doc.get(key), dict):
            state[key] = doc[key]
    if isinstance(doc.get("outbox"), list):
        state["outbox"] = [item for item in doc["outbox"] if isinstance(item, dict)]
    for key in ("pair_id", "last_send", "last_poll"):
        state[key] = doc.get(key)
    return state


def write_state(session: _paths.SessionPaths, doc: Dict[str, Any]) -> None:
    _paths.ensure_dir(session.root)
    store.write_json(state_path(session), doc, fsync=False)


def status_view(layout: _paths.Layout, now: Optional[float] = None) -> Dict[str, Any]:
    """``remote status``: the config, this session's queue and codes, and the last results."""
    wall = time.time() if now is None else now
    doc = load_config(layout.config_dir)
    view = public_view(doc, wall)
    try:
        state = read_state(layout.session)
    except HerdrTeamError:
        state = _empty_state()
    if state.get("pair_id") != doc.get("pair_id"):
        state = dict(state, outbox=[], codes={})
    view.update({
        "config": os.fspath(config_path(layout.config_dir)),
        "session": layout.slug,
        "outbox": len(state.get("outbox") or []),
        "open_codes": len([c for c in (state.get("codes") or {}).values() if isinstance(c, dict) and c.get("state") == "open"]),
        "last_send": state.get("last_send"),
        "last_poll": state.get("last_poll"),
    })
    return view


def doctor_warnings(config_dir: Path, now: Optional[float] = None) -> List[str]:
    """What ``doctor`` says about phone reach: paired or not, and outbound-only or not."""
    wall = time.time() if now is None else now
    try:
        doc = load_config(config_dir)
    except HerdrTeamError as err:
        return ["phone reach config is unusable: {}".format(err.message)]
    view = public_view(doc, wall)
    if view["pending_pair"]:
        return ["phone reach: Telegram pairing is waiting for /start <code>; finish it with: herdr-synapse remote pair --complete"]
    if view["pair_expired"]:
        return ["phone reach: the Telegram pairing code expired; start again with: herdr-synapse remote pair telegram --bot-token-file PATH"]
    if not view["paired"]:
        return []
    lines = ["phone reach: {} is paired ({}); what leaves this machine: {}".format(view["channel"], view["destination"], view["leaves_machine"])]
    if view["outbound_only"]:
        lines.append("phone reach: {} is outbound-only ({}); answer asks in Herdr".format(view["channel"], view["outbound_only_reason"]))
    else:
        lines.append("phone reach: {} reads answers back; a phone answer counts as yours for asks and nothing else".format(view["channel"]))
    return lines


def remote_author(channel: str) -> Author:
    """The operator answering from the paired phone.

    It writes a record readers trust (``identity.record_human_ok``) and passes
    no authority gate: ``trusted_human`` applies ``human_origin_ok``, which has
    never heard of ``remote``.
    """
    return Author(AUTHOR_HUMAN, "human", VIA_REMOTE, True, origin={"via": VIA_REMOTE, "verified": True, "channel": channel})


def _system_author() -> Author:
    return Author(AUTHOR_SYSTEM, None, VIA_SYSTEM, True, origin={"via": VIA_SYSTEM, "verified": True})


# --------------------------------------------------------------------------
# the notifier's half


class RemoteRelay:
    """Queue, send, poll and apply, one bounded step per notifier tick.

    ``teams`` passed to ``tick`` are the daemon's ``TeamState`` objects; only
    ``name``, ``paths`` and ``open_asks`` are read. ``inline=True`` runs each
    request on the calling thread (tests); the notifier uses the worker.
    """

    def __init__(self, layout: _paths.Layout, log: Optional[Callable[[str], None]] = None, transport: Optional[Transport] = None,
                 wall: Optional[Callable[[], float]] = None, inline: bool = False) -> None:
        self.layout = layout
        self.log = log or (lambda _message: None)
        self.transport = transport
        self.wall = wall or time.time
        self.inline = inline
        self.ignored = 0
        self._config: Dict[str, Any] = {}
        self._config_sig: Any = None
        self._config_checked_ms: Optional[float] = None
        self._state: Optional[Dict[str, Any]] = None
        self._jobs: "queue.Queue[Any]" = queue.Queue()
        self._results: "queue.Queue[Any]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._inflight: Optional[Dict[str, Any]] = None
        self._hung_logged = False
        self._poll_due_ms: Optional[float] = None
        self._send_due_ms = 0.0
        self._collect_due_ms: Optional[float] = None
        self._poll_written_ms: Optional[float] = None
        self._last_error: Optional[str] = None

    # -- config and state ----------------------------------------------------

    def config(self, now_ms: Optional[float] = None) -> Dict[str, Any]:
        """``remote.json``, re-read when its size or mtime changes, looked at every ``CONFIG_CHECK_S``."""
        if now_ms is not None and self._config_checked_ms is not None and now_ms - self._config_checked_ms < CONFIG_CHECK_S * 1000.0:
            return self._config
        if now_ms is None and self._config_checked_ms is not None:
            return self._config
        self._config_checked_ms = now_ms if now_ms is not None else 0.0
        path = config_path(self.layout.config_dir)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            self._config, self._config_sig = {}, None
            return self._config
        except OSError:
            return self._config
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig != self._config_sig:
            try:
                self._config = load_config(self.layout.config_dir)
            except HerdrTeamError as err:
                self.log("remote: config unreadable: {}".format(err.message))
                self._config = {}
            self._config_sig = sig
        return self._config

    def _state_doc(self, doc: Mapping[str, Any]) -> Dict[str, Any]:
        if self._state is None:
            try:
                self._state = read_state(self.layout.session)
            except HerdrTeamError as err:
                self.log("remote: state unreadable, starting empty: {}".format(err.message))
                self._state = _empty_state()
        state = self._state
        if doc.get("pair_id") and state.get("pair_id") != doc.get("pair_id"):
            # A new pairing: nothing queued for the old channel may leak onto the new one.
            state["pair_id"] = doc.get("pair_id")
            state["outbox"] = []
            state["codes"] = {}
            self._save()
        return state

    def _save(self) -> None:
        if self._state is None:
            return
        try:
            write_state(self.layout.session, self._state)
        except (HerdrTeamError, OSError) as err:
            self.log("remote: could not write state: {}".format(err))

    # -- the tick --------------------------------------------------------------

    def tick(self, teams: Mapping[str, Any], now_ms: float) -> None:
        doc = self.config(now_ms)
        self._drain(teams, now_ms)
        wall = self.wall()
        if not is_paired(doc) and not pending_pair(doc, wall):
            return
        state = self._state_doc(doc)
        if is_paired(doc) and (self._collect_due_ms is None or now_ms >= self._collect_due_ms):
            self._collect_due_ms = now_ms + COLLECT_S * 1000.0
            self._collect(teams, doc, state)
        if self._inflight is not None:
            if not self._hung_logged and now_ms - float(self._inflight["started_ms"]) > HUNG_S * 1000.0:
                self._hung_logged = True
                self.log("remote: a {} request has run for {:.0f} s; the tick carries on without it".format(self._inflight["kind"], HUNG_S))
            return
        job = self._next_job(teams, doc, state, now_ms)
        if job is None:
            return
        self._submit(job, doc, now_ms)
        if self.inline:
            self._drain(teams, now_ms)

    def note_record(self, team_name: str, record: Mapping[str, Any]) -> None:
        """A system record the notifier ingested; queue it when the policy sends its kind."""
        try:
            doc = self.config()
            category = RELAYED_EVENTS.get(str(record.get("event")))
            if not is_paired(doc) or category not in policy_of(doc)["send"]:
                return
            if "human" not in [t for t in (record.get("to") or []) if isinstance(t, str)]:
                return
            ts = parse_iso(record.get("ts"))
            if ts is None or ts < float(doc.get("paired_at") or 0):
                return
            state = self._state_doc(doc)
            key = "event:{}:{}".format(team_name, record.get("seq"))
            if key in state["sent"] or any(item.get("key") == key for item in state["outbox"]):
                return
            title, body = compose_event(self.layout.slug, team_name, record, policy_of(doc)["text"])
            priority = 4 if category == "failures" else 3
            self._enqueue(state, {"key": key, "category": category, "team": team_name, "seq": record.get("seq"),
                                  "title": title, "body": body, "priority": priority, "code": None})
            self._save()
        except Exception as err:  # noqa: BLE001 - called from the board tail; never take the ingest down
            self.log("remote: could not queue #{} of {}: {}: {}".format(record.get("seq"), team_name, type(err).__name__, err))

    def _enqueue(self, state: Dict[str, Any], item: Dict[str, Any]) -> None:
        item.setdefault("attempts", 0)
        item.setdefault("next_at", 0.0)
        item.setdefault("created_at", self.wall())
        state["outbox"].append(item)
        while len(state["outbox"]) > OUTBOX_MAX:
            dropped = state["outbox"].pop(0)
            self.log("remote: outbox full; dropped {} {}".format(dropped.get("category"), dropped.get("key")))

    def _notice(self, doc: Mapping[str, Any], text: str, team: Optional[str] = None) -> None:
        """A short reply to the phone: a confirmation, ``unknown code``, a pairing hello."""
        if not is_paired(doc):
            return
        state = self._state_doc(doc)
        self._enqueue(state, {"key": "notice:{}".format(secrets.token_hex(4)), "category": "notice", "team": team, "seq": None,
                              "title": "herdr-synapse", "body": text, "priority": 2, "code": None})

    def _collect(self, teams: Mapping[str, Any], doc: Mapping[str, Any], state: Dict[str, Any]) -> None:
        """New asks become outbox items; codes whose ask closed become ``closed`` (and maybe a settled note)."""
        policy = policy_of(doc)
        mode = policy["text"]
        paired_at = float(doc.get("paired_at") or 0)
        wall = self.wall()
        changed = False
        queued = {item.get("key") for item in state["outbox"]}
        if "asks" in policy["send"]:
            for team in teams.values():
                for seq, record in list(team.open_asks.items()):
                    if str(record.get("kind")) not in _asks.ASKING_KINDS:
                        continue
                    key = "ask:{}:{}".format(team.name, seq)
                    if key in state["sent"] or key in queued:
                        continue
                    ts = parse_iso(record.get("ts"))
                    if ts is None or ts < paired_at:
                        continue  # only what is asked after pairing; the backlog stays in the popup
                    code = new_code(set(state["codes"]) | self._foreign_codes()) if receives(doc) and mode != "none" else None
                    title, body = compose_ask(self.layout.slug, team.name, record, code, mode)
                    self._enqueue(state, {"key": key, "category": "asks", "team": team.name, "seq": seq,
                                          "title": title, "body": body, "priority": 4, "code": code})
                    if code:
                        state["codes"][code] = {"team": team.name, "seq": seq, "asker": record.get("from"), "kind": record.get("kind"),
                                                "created_at": wall, "sent_at": None, "message_id": None, "state": "open"}
                    changed = True
        for code, entry in state["codes"].items():
            if not isinstance(entry, dict) or entry.get("state") != "open":
                continue
            team = teams.get(str(entry.get("team")))
            if team is not None and entry.get("seq") in team.open_asks:
                continue
            entry["state"], entry["closed_at"] = "closed", wall
            changed = True
            if entry.get("sent_at") and "settled" in policy["send"] and mode != "none":
                self._enqueue(state, {"key": "settled:{}".format(code), "category": "settled", "team": entry.get("team"), "seq": entry.get("seq"),
                                      "title": "{}/{}: settled".format(self.layout.slug, entry.get("team")),
                                      "body": "#{} ({}) is no longer waiting: answered or withdrawn in Herdr.".format(entry.get("seq"), code),
                                      "priority": 2, "code": None})
        changed = self._prune(state, wall) or changed
        if changed:
            self._save()

    def _prune(self, state: Dict[str, Any], wall: float) -> bool:
        before = (len(state["codes"]), len(state["sent"]))
        state["codes"] = {c: e for c, e in state["codes"].items()
                          if isinstance(e, dict) and (e.get("state") == "open" or float(e.get("created_at") or 0) > wall - CODE_TTL_S)}
        sent = {k: v for k, v in state["sent"].items() if isinstance(v, dict) and float(v.get("at") or 0) > wall - SENT_TTL_S}
        if len(sent) > SENT_MAX:
            sent = dict(sorted(sent.items(), key=lambda kv: float(kv[1].get("at") or 0))[-SENT_MAX:])
        state["sent"] = sent
        return before != (len(state["codes"]), len(state["sent"]))

    def _next_job(self, teams: Mapping[str, Any], doc: Mapping[str, Any], state: Dict[str, Any], now_ms: float) -> Optional[Dict[str, Any]]:
        wall = self.wall()
        if (receives(doc) or pending_pair(doc, wall)) and (self._poll_due_ms is None or now_ms >= self._poll_due_ms):
            active = pending_pair(doc, wall) or any(isinstance(e, dict) and e.get("state") == "open" and e.get("sent_at")
                                                    for e in state["codes"].values())
            self._poll_due_ms = now_ms + (POLL_ACTIVE_S if active else POLL_IDLE_S) * 1000.0
            lease = self._take_lease(doc, wall)
            if lease is not None:
                return {"kind": "poll", "cursor": lease.get("cursor"), "seen": list(lease.get("seen") or [])}
        if not is_paired(doc) or now_ms < self._send_due_ms:
            return None
        for item in list(state["outbox"]):
            if float(item.get("next_at") or 0) > wall:
                continue
            if item.get("category") == "asks":
                team = teams.get(str(item.get("team")))
                if team is None or item.get("seq") not in team.open_asks:
                    state["outbox"].remove(item)  # answered before it went out: never send a stale ask
                    state["sent"][str(item.get("key"))] = {"at": wall, "skipped": True}
                    self._save()
                    continue
            self._send_due_ms = now_ms + SEND_GAP_S * 1000.0
            return {"kind": "send", "item": dict(item)}
        return None

    # -- the worker ---------------------------------------------------------------

    def _transport(self) -> Transport:
        if self.transport is None:
            self.transport = make_transport()
        return self.transport

    def _submit(self, job: Dict[str, Any], doc: Mapping[str, Any], now_ms: float) -> None:
        snapshot = json.loads(json.dumps(doc))  # the worker never shares a dict the tick mutates
        transport = self._transport()
        if job["kind"] == "send":
            item = job["item"]
            work: Callable[[], Any] = lambda: send_message(snapshot, transport, str(item.get("title") or ""), str(item.get("body") or ""), int(item.get("priority") or 3))
        else:
            cursor = job.get("cursor")
            work = lambda: poll_messages(snapshot, transport, cursor)
        job = dict(job, pair_id=doc.get("pair_id"), started_ms=now_ms)
        self._inflight = job
        if self.inline:
            self._results.put((job, self._run(work)))
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._work, name="herdr-synapse-remote", daemon=True)
            self._thread.start()
        self._jobs.put((job, work))

    @staticmethod
    def _run(work: Callable[[], Any]) -> Any:
        try:
            return work()
        except Exception as err:  # noqa: BLE001 - send/poll never raise; this is belt and braces for the worker
            return SendOutcome(False, error=type(err).__name__)

    def _work(self) -> None:
        while True:
            job, work = self._jobs.get()
            self._results.put((job, self._run(work)))

    def _drain(self, teams: Mapping[str, Any], now_ms: float) -> None:
        while True:
            try:
                job, outcome = self._results.get_nowait()
            except queue.Empty:
                return
            self._inflight = None
            self._hung_logged = False
            doc = self.config()
            if doc.get("pair_id") != job.get("pair_id"):
                continue  # unpaired or re-paired while the request was out
            try:
                if job["kind"] == "send":
                    self._sent(job, outcome, doc, now_ms)
                else:
                    self._polled(job, outcome, doc, teams, now_ms)
            except Exception as err:  # noqa: BLE001 - one bad reply must not stop the next
                self.log("remote: handling a {} result failed: {}: {}".format(job["kind"], type(err).__name__, err))

    def _sent(self, job: Dict[str, Any], outcome: Any, doc: Mapping[str, Any], now_ms: float) -> None:
        state = self._state_doc(doc)
        item = job["item"]
        key = item.get("key")
        wall = self.wall()
        current = next((i for i in state["outbox"] if i.get("key") == key), None)
        if getattr(outcome, "ok", False):
            if current is not None:
                state["outbox"].remove(current)
            state["sent"][str(key)] = {"at": wall}
            code = item.get("code")
            if code and isinstance(state["codes"].get(code), dict):
                state["codes"][code]["sent_at"] = wall
                state["codes"][code]["message_id"] = getattr(outcome, "message_id", None)
                # A code is out: an answer may come any second, so the idle cadence ends here.
                self._poll_due_ms = min(float(self._poll_due_ms if self._poll_due_ms is not None else now_ms), now_ms + POLL_ACTIVE_S * 1000.0)
            state["last_send"] = {"at": iso(wall), "ok": True, "category": item.get("category"), "team": item.get("team")}
            if item.get("team"):
                audit(self.layout, str(item["team"]), "remote_sent", _system_author(),
                      {"channel": channel_of(doc), "category": item.get("category"), "seq": item.get("seq"), "code": code,
                       "text": policy_of(doc)["text"]})
            self._save()
            return
        error = scrub(getattr(outcome, "error", None) or "failed", doc)
        attempts = int((current or item).get("attempts") or 0) + 1
        if current is not None:
            if attempts >= SEND_MAX_ATTEMPTS:
                state["outbox"].remove(current)
                self.log("remote: gave up on {} {} after {} attempts: {}".format(item.get("category"), key, attempts, error))
            else:
                current["attempts"] = attempts
                current["next_at"] = wall + SEND_BACKOFF_S[min(attempts, len(SEND_BACKOFF_S)) - 1]
        if error != self._last_error:
            self.log("remote: send failed ({}); retrying with backoff".format(error))
            self._last_error = error
        state["last_send"] = {"at": iso(wall), "ok": False, "error": error, "category": item.get("category"), "team": item.get("team")}
        self._save()

    def _polled(self, job: Dict[str, Any], outcome: Any, doc: Mapping[str, Any], teams: Mapping[str, Any], now_ms: float) -> None:
        state = self._state_doc(doc)
        wall = self.wall()
        if not getattr(outcome, "ok", False):
            self._release_lease(doc)
            self._poll_due_ms = max(float(self._poll_due_ms or 0), now_ms + POLL_ERROR_S * 1000.0)
            error = scrub(getattr(outcome, "error", None) or "failed", doc)
            if error != self._last_error:
                self.log("remote: poll failed ({}); next try in {:.0f} s".format(error, POLL_ERROR_S))
                self._last_error = error
            state["last_poll"] = {"at": iso(wall), "ok": False, "error": error}
            self._save()
            return
        seen = set(job.get("seen") or [])
        handled: List[str] = []
        for message in outcome.incoming:
            if message.id in seen:
                continue
            handled.append(message.id)
            try:
                self._handle(message, self.config() or doc, teams)
            except Exception as err:  # noqa: BLE001 - see _drain
                self.log("remote: a reply could not be applied: {}: {}".format(type(err).__name__, err))
        self._commit_poll(doc, outcome.cursor, handled)
        was_ok = (state.get("last_poll") or {}).get("ok")
        state["last_poll"] = {"at": iso(wall), "ok": True, "handled": len(handled)}
        if handled or was_ok is not True or self._poll_written_ms is None or now_ms - self._poll_written_ms >= POLL_STATE_WRITE_S * 1000.0:
            self._poll_written_ms = now_ms
            self._save()

    # -- the shared read cursor ----------------------------------------------------

    def _poll_doc(self, doc: Mapping[str, Any]) -> Dict[str, Any]:
        current = store.read_json(poll_path(self.layout.config_dir), default=None)
        if isinstance(current, dict) and current.get("pair_id") == doc.get("pair_id"):
            return current
        cursor: Any = 0 if channel_of(doc) == "telegram" else str(int(float(_section(doc).get("since") or doc.get("paired_at") or 0)))
        return {"v": 1, "pair_id": doc.get("pair_id"), "cursor": cursor, "seen": []}

    def _take_lease(self, doc: Mapping[str, Any], wall: float) -> Optional[Dict[str, Any]]:
        """The right to read the channel for ``LEASE_S``; None when another notifier has it."""
        lock = config_lock(self.layout.config_dir)
        try:
            if not lock.try_acquire():
                return None
        except (HerdrTeamError, OSError):
            return None
        try:
            poll = self._poll_doc(doc)
            holder = poll.get("lease_pid")
            if holder not in (None, os.getpid()) and float(poll.get("lease_until") or 0) > wall:
                return None
            if abs(wall - float(poll.get("polled_at") or 0)) < POLL_ACTIVE_S - 0.5:
                return None  # another session's notifier just read this channel
            poll["lease_pid"], poll["lease_until"], poll["polled_at"] = os.getpid(), wall + LEASE_S, wall
            store.write_json(poll_path(self.layout.config_dir), poll, fsync=False)
            return poll
        except (HerdrTeamError, OSError) as err:
            self.log("remote: poll cursor unavailable: {}".format(err))
            return None
        finally:
            lock.release()

    def _commit_poll(self, doc: Mapping[str, Any], cursor: Any, handled: List[str]) -> None:
        """Move the cursor past what was just applied, remember the ids, drop the lease."""
        try:
            with config_lock(self.layout.config_dir, LEASE_LOCK_WAIT_S):
                poll = self._poll_doc(doc)
                if cursor is not None:
                    poll["cursor"] = cursor
                poll["seen"] = (list(poll.get("seen") or []) + handled)[-SEEN_MAX:]
                if poll.get("lease_pid") == os.getpid():
                    poll["lease_pid"], poll["lease_until"] = None, None
                store.write_json(poll_path(self.layout.config_dir), poll, fsync=bool(handled))
        except (LockTimeout, HerdrTeamError, OSError) as err:
            self.log("remote: could not save the poll cursor ({}); the lease lapses on its own".format(err))

    def _release_lease(self, doc: Mapping[str, Any]) -> None:
        self._commit_poll(doc, None, [])

    # -- replies --------------------------------------------------------------------

    def _handle(self, message: Incoming, doc: Mapping[str, Any], teams: Mapping[str, Any]) -> None:
        wall = self.wall()
        if channel_of(doc) == "telegram":
            section = _section(doc)
            chat = section.get("chat_id")
            if chat is None:
                if pairing_match(doc, message, wall) and message.chat_id is not None:
                    pinned = pin_chat(self.layout.config_dir, doc.get("pair_id"), message.chat_id)
                    if pinned is not None:
                        self._config, self._config_sig = pinned, None
                        self.log("remote: telegram chat paired")
                        self._notice(pinned, "Paired with herdr-synapse. Asks addressed to you will arrive here; reply with the code to answer.")
                        self._save()
                else:
                    self.ignored += 1
                return
            if message.chat_id != chat:
                self.ignored += 1  # never answer a stranger: a reply would confirm the bot is live
                return
            if START_RE.match((message.text or "").strip()) or (message.text or "").strip() == "/start":
                return
        self._apply_reply(message, doc)

    def _code_tables(self) -> Iterator[Tuple[_paths.Layout, Dict[str, Any], bool]]:
        """This session's codes first, then every other session's under the same state root."""
        own = self._state if self._state is not None else _empty_state()
        yield self.layout, own.get("codes") or {}, True
        sessions_dir = Path(self.layout.state_root.path) / _paths.SESSIONS_DIR
        try:
            names = sorted(os.listdir(sessions_dir))
        except OSError:
            return
        for name in names:
            root = sessions_dir / name
            if root == self.layout.session.root or not (root / "remote-state.json").is_file():
                continue
            session = _paths.SessionPaths(root, name)
            try:
                other = read_state(session)
            except HerdrTeamError:
                continue
            yield dataclasses.replace(self.layout, session=session), other.get("codes") or {}, False

    def _foreign_codes(self) -> set:
        return {code for _layout, codes, own in self._code_tables() if not own for code in codes}

    def _find_code(self, code: str) -> Optional[Tuple[_paths.Layout, Dict[str, Any], bool]]:
        wall = self.wall()
        for layout, codes, own in self._code_tables():
            entry = codes.get(code)
            if isinstance(entry, dict) and float(entry.get("created_at") or 0) > wall - CODE_TTL_S:
                return layout, entry, own
        return None

    def _code_for_message(self, message_id: int) -> Optional[str]:
        for _layout, codes, _own in self._code_tables():
            for code, entry in codes.items():
                if isinstance(entry, dict) and entry.get("message_id") == message_id:
                    return code
        return None

    def _apply_reply(self, message: Incoming, doc: Mapping[str, Any]) -> None:
        """``<code> <answer>`` or ``<code> ok`` becomes the operator's answer to that ask, and nothing else."""
        from herdr_team.cmd_asks import MAX_REPLY_CHARS, ack_text
        from herdr_team.cmd_board import build_record, prepare_text

        channel = str(channel_of(doc))
        mode = policy_of(doc)["text"]
        text = (message.text or "").strip()
        code: Optional[str] = None
        if message.reply_to_message_id is not None:
            code = self._code_for_message(message.reply_to_message_id)
            if code is not None:
                leading, rest = parse_reply(text)
                if leading == code:
                    text = rest
        if code is None:
            code, text = parse_reply(text)
        if code is None:
            if channel == "telegram":
                self._notice(doc, "No code found. Reply with the code from the message: <code> <your answer>, or <code> ok to acknowledge.")
            return  # ntfy: other traffic on a private topic is not ours to answer
        found = self._find_code(code)
        if found is None:
            self._notice(doc, "unknown code {}".format(code))
            return
        layout, entry, own = found
        team_name = str(entry.get("team"))
        seq = entry.get("seq")
        label = "" if mode == "none" else " ({})".format(team_name)
        try:
            team_paths = layout.team(team_name)
        except HerdrTeamError:
            self._notice(doc, "unknown code {}".format(code))
            return
        record = next((r for r in _asks.pending(team_paths) if r.get("seq") == seq), None)
        if record is None:
            if own:
                entry["state"] = "closed"
            self._notice(doc, "#{}{} is no longer waiting; nothing was posted.".format(seq, label), team_name)
            return
        ack = is_ack(text)
        if ack:
            body = ack_text(record.get("kind"))
        elif not text:
            self._notice(doc, "Empty answer. Send {c} <your answer>, or {c} ok to acknowledge.".format(c=code), team_name)
            return
        elif len(text) > MAX_REPLY_CHARS:
            self._notice(doc, "Too long: keep an answer under {} characters, or answer in Herdr. Nothing was posted.".format(MAX_REPLY_CHARS), team_name)
            return
        else:
            try:
                body, _truncated, _spill = prepare_text(text, False, False)
            except HerdrTeamError as err:
                self._notice(doc, "Not posted: {}".format(err.message[:160]), team_name)
                return
        team_doc = store.read_json(team_paths.team_json, default={})
        socket = team_doc.get("socket") if isinstance(team_doc, dict) and isinstance(team_doc.get("socket"), str) else os.fspath(layout.socket)
        author = remote_author(channel)
        answer = build_record(author, [str(record.get("from"))], "answer", body, reply_to=seq, socket_path=socket)
        new_seq = store.BoardStore(team_paths).append(answer)
        audit(layout, team_name, "remote_answer", author,
              {"channel": channel, "code": code, "reply_to": seq, "seq": new_seq, "ack": ack, "chars": len(body)})
        if own:
            entry["state"], entry["answered_at"] = "answered", self.wall()
        self.log("remote: #{} in {} {} from the phone (#{})".format(seq, team_name, "acknowledged" if ack else "answered", new_seq))
        self._notice(doc, "{} #{}{}.".format("Acknowledged" if ack else "Answered", seq, label), team_name)
        self._save()

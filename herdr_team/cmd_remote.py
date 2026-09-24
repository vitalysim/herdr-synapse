"""``remote``: reach the operator's phone when an agent asks something.

The machinery (channels, policy, codes, the notifier's relay) is
``herdr_team.remote``; this module adds the authority checks, the audit lines,
and the pairing conversation. Pairing and unpairing decide what leaves the
machine and where, so they are the operator's alone (``_strictly_human``), as
is widening the policy. ``test`` only sends, so a delegate may run it.
``status`` and a bare ``policy`` read and need no authority.

Secrets never travel in argv, where ``ps`` and shell history can read them:
each channel takes its token or URL from a file or an environment variable
*name*, and the familiar ``--token``/``--bot-token``/``--url`` spellings are
refused with a pointer to the safe ones rather than silently accepted.
"""
from __future__ import annotations

import argparse
import time
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import remote as _remote
from herdr_team import store
from herdr_team.cli import Command, add_global_arguments, api_for, emit, layout_for
from herdr_team.errors import EXIT_REFUSED, EXIT_USAGE, HerdrTeamError
from herdr_team.identity import Author, audit

def _nested(sub: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
    add_global_arguments(parser, nested=True)
    return parser


def _refuse_argv_secret(parser: argparse.ArgumentParser, flag: str) -> None:
    # Accepted only to be refused: an unknown flag would echo the secret back in the usage error.
    parser.add_argument(flag, dest="argv_secret", metavar="SECRET", default=None, help=argparse.SUPPRESS)


def _add_remote_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="remote_action", metavar="<status|pair|unpair|test|policy>")
    _nested(sub, "status", "the paired channel, what is sent, and the last send and poll")
    pair = _nested(sub, "pair", "pair a channel: ntfy, telegram or webhook (operator only)")
    pair.add_argument("--complete", action="store_true", help="finish a Telegram pairing now instead of waiting for the notifier")
    channels = pair.add_subparsers(dest="channel", metavar="<ntfy|telegram|webhook>")
    ntfy = _nested(channels, "ntfy", "publish to an ntfy topic; replies only with an access token")
    ntfy.add_argument("--topic", required=True, help="the topic name (1-64 letters, digits, - or _)")
    ntfy.add_argument("--server", metavar="URL", help="self-hosted server (default {})".format(_remote.DEFAULT_NTFY_SERVER))
    ntfy.add_argument("--token-file", metavar="PATH", help="file whose first line is an access token; enables replies")
    ntfy.add_argument("--token-env", metavar="VAR", help="environment variable holding an access token; enables replies")
    _refuse_argv_secret(ntfy, "--token")
    telegram = _nested(channels, "telegram", "a Telegram bot you created with @BotFather; replies from one pinned chat")
    telegram.add_argument("--bot-token-file", metavar="PATH", help="file whose first line is the bot token")
    telegram.add_argument("--bot-token-env", metavar="VAR", help="environment variable holding the bot token")
    telegram.add_argument("--complete", action="store_true", default=argparse.SUPPRESS, help="finish the pairing now")
    _refuse_argv_secret(telegram, "--bot-token")
    webhook = _nested(channels, "webhook", "a JSON POST ({\"text\": ...}, Slack-compatible); outbound only")
    webhook.add_argument("--url-file", metavar="PATH", help="file whose first line is the webhook URL")
    webhook.add_argument("--url-env", metavar="VAR", help="environment variable holding the webhook URL")
    _refuse_argv_secret(webhook, "--url")
    _nested(sub, "unpair", "forget the channel and its secret; the policy is kept (operator only)")
    _nested(sub, "test", "send one test message on the paired channel")
    policy = _nested(sub, "policy", "what is sent and how much of it (a bare call reads)")
    policy.add_argument("--send", metavar="LIST", help="comma list of {} (or none)".format(",".join(_remote.CATEGORIES)))
    policy.add_argument("--text", choices=_remote.TEXT_MODES, help="full: the ask text, redacted; summary: who and what kind; none: a bare count")


# --------------------------------------------------------------------------
# authority


def _team_hint(args: argparse.Namespace, layout: Any) -> Optional[str]:
    """A team to audit under; phone reach itself is per config dir, so none is required."""
    from herdr_team.cmd_board import resolve_team

    try:
        team = resolve_team(args, layout, None, required=False)
    except HerdrTeamError as err:
        if err.code not in ("team_ambiguous", "team_not_found"):
            raise
        return None
    return team if team in layout.session.list_teams() else None


def _authorize(args: argparse.Namespace, layout: Any, action: str, strict: bool = True) -> Tuple[Author, Optional[str]]:
    from herdr_team.cmd_board import resolve_author
    from herdr_team.cmd_roster import _human_only, _strictly_human

    team = _team_hint(args, layout)
    author = resolve_author(args, layout, api_for(args, layout), team=team, require_server=False)
    (_strictly_human if strict else _human_only)(layout, team or "", author, action)
    return author, team


def _audit(layout: Any, team: Optional[str], event: str, author: Author, details: Dict[str, Any]) -> None:
    if team:
        audit(layout, team, event, author, details)


# --------------------------------------------------------------------------
# rendering


def _status_lines(view: Dict[str, Any]) -> List[str]:
    if view.get("pending_pair"):
        head = "phone reach: Telegram pairing in progress; send /start <code> to your bot (herdr-synapse remote pair --complete to finish now)"
    elif view.get("pair_expired"):
        head = "phone reach: the Telegram pairing code expired; run herdr-synapse remote pair telegram again"
    elif not view.get("paired"):
        return ["phone reach: not paired. Pair a channel with: herdr-synapse remote pair ntfy|telegram|webhook ...",
                "policy: sends {} · text {}".format(", ".join(view["policy"]["send"]) or "nothing", view["policy"]["text"])]
    elif view.get("outbound_only"):
        head = "phone reach: {} ({}), outbound-only: {}".format(view["channel"], view["destination"], view["outbound_only_reason"])
    else:
        head = "phone reach: {} ({}); answers are read back".format(view["channel"], view["destination"])
    lines = [head, "leaves this machine: {}".format(view["leaves_machine"])]
    for key in ("last_send", "last_poll"):
        result = view.get(key)
        if isinstance(result, dict):
            lines.append("{}: {} {}".format(key.replace("_", " "), result.get("at"), "ok" if result.get("ok") else "failed: {}".format(result.get("error"))))
    if "outbox" in view:
        lines.append("queued: {} · codes waiting for an answer: {} · session {}".format(view["outbox"], view["open_codes"], view["session"]))
    return lines


# --------------------------------------------------------------------------
# actions


def _run_status(args: argparse.Namespace, layout: Any) -> int:
    from herdr_team.cmd_board import notifier_state

    view = _remote.status_view(layout)
    view["notifier"] = notifier_state(layout.session)

    def human() -> str:
        lines = _status_lines(view)
        if view.get("paired") and view["notifier"] != "alive":
            lines.append("the notifier is not running: nothing is sent or read until it is (herdr-synapse daemon start)")
        return "\n".join(lines)

    return emit(args, view, human)


def _save_pairing(layout: Any, channel: str, settings: Dict[str, Any], author: Author) -> Dict[str, Any]:
    now = time.time()
    with _remote.config_lock(layout.config_dir):
        previous = _remote.load_config(layout.config_dir)
        doc = _remote.new_config(channel, settings, previous, now, author.name)
        _remote.save_config(layout.config_dir, doc)
        _remote.remove_file(_remote.poll_path(layout.config_dir))
    return doc


def _pair_ntfy(args: argparse.Namespace, layout: Any, author: Author, team: Optional[str]) -> int:
    topic = _remote.validate_topic(args.topic)
    server = _remote.validate_server(args.server)
    token = _remote.read_secret(args.env, args.token_file, args.token_env, "access token", required=False)
    if token:
        _remote.validate_token(token)
    doc = _save_pairing(layout, "ntfy", {"server": server, "topic": topic, "token": token or None, "since": str(int(time.time()))}, author)
    _audit(layout, team, "remote_paired", author, {"channel": "ntfy", "receives": bool(token)})
    view = _remote.public_view(doc, time.time())
    how = ("Replies are read back from this private topic: answer with the code in each message."
           if token else "Outbound-only: without an access token the topic is public, so replies are never read. Answer asks in Herdr.")
    return emit(args, view, "paired ntfy ({}). {}\nleaves this machine: {}\ntry it: herdr-synapse remote test".format(view["destination"], how, view["leaves_machine"]))


def _pair_telegram(args: argparse.Namespace, layout: Any, author: Author, team: Optional[str]) -> int:
    token = _remote.validate_bot_token(_remote.read_secret(args.env, args.bot_token_file, args.bot_token_env, "bot token"))
    now = time.time()
    code = _remote.new_code(length=_remote.PAIR_CODE_LEN)
    doc = _save_pairing(layout, "telegram", {"bot_token": token, "chat_id": None, "pair_code": code,
                                             "pair_started": now, "pair_expires": now + _remote.PAIR_CODE_TTL_S}, author)
    _audit(layout, team, "remote_pair_started", author, {"channel": "telegram"})
    username = _remote.bot_username(doc, _remote.make_transport())
    link = "https://t.me/{}?start={}".format(username, code) if username else None
    minutes = int(_remote.PAIR_CODE_TTL_S // 60)
    payload = dict(_remote.public_view(doc, now), code=code, link=link, expires_in_s=int(_remote.PAIR_CODE_TTL_S))
    text = ["Send this to your bot from the phone within {} minutes:".format(minutes), "", "    /start {}".format(code), ""]
    if link:
        text.insert(1, "  {}  (opens the chat with the code filled in)".format(link))
    text.append("The notifier pins that chat within a few seconds; or finish now with: herdr-synapse remote pair --complete")
    text.append("Only that one private chat can answer. Nothing is sent until it is paired.")
    return emit(args, payload, "\n".join(text))


def _complete_telegram(args: argparse.Namespace, layout: Any, author: Author, team: Optional[str]) -> int:
    now = time.time()
    doc = _remote.load_config(layout.config_dir)
    if _remote.channel_of(doc) != "telegram":
        raise HerdrTeamError("remote_nothing_to_complete", "no Telegram pairing is in progress; start one with: herdr-synapse remote pair telegram --bot-token-file PATH", EXIT_REFUSED)
    if _remote.is_paired(doc):
        return emit(args, _remote.public_view(doc, now), "already paired: {}".format(_remote.destination(doc)))
    if not _remote.pending_pair(doc, now):
        raise HerdrTeamError("remote_pair_expired", "the pairing code expired; run herdr-synapse remote pair telegram again", EXIT_REFUSED)
    transport = _remote.make_transport()
    poll = store.read_json(_remote.poll_path(layout.config_dir), default=None)
    cursor = poll.get("cursor") if isinstance(poll, dict) and poll.get("pair_id") == doc.get("pair_id") else 0
    outcome = _remote.poll_messages(doc, transport, cursor)
    if not outcome.ok:
        raise HerdrTeamError("remote_poll_failed", "could not read the bot's messages: {}".format(_remote.scrub(outcome.error, doc)), EXIT_REFUSED)
    for message in outcome.incoming:
        if not _remote.pairing_match(doc, message, now) or message.chat_id is None:
            continue
        pinned = _remote.pin_chat(layout.config_dir, doc.get("pair_id"), message.chat_id)
        if pinned is None:
            break  # the notifier got there first
        _remote.send_message(pinned, transport, "herdr-synapse", "Paired with herdr-synapse. Asks addressed to you will arrive here; reply with the code to answer.", 2)
        _audit(layout, team, "remote_paired", author, {"channel": "telegram", "receives": True})
        return emit(args, _remote.public_view(pinned, time.time()), "paired Telegram: {}".format(_remote.destination(pinned)))
    doc = _remote.load_config(layout.config_dir)
    if _remote.is_paired(doc):
        return emit(args, _remote.public_view(doc, time.time()), "paired Telegram: {}".format(_remote.destination(doc)))
    raise HerdrTeamError("remote_pair_waiting", "no /start <code> from a private chat yet; send it to the bot and run this again", EXIT_REFUSED)


def _pair_webhook(args: argparse.Namespace, layout: Any, author: Author, team: Optional[str]) -> int:
    url = _remote.validate_webhook(_remote.read_secret(args.env, args.url_file, args.url_env, "webhook URL"))
    doc = _save_pairing(layout, "webhook", {"url": url}, author)
    _audit(layout, team, "remote_paired", author, {"channel": "webhook", "receives": False})
    view = _remote.public_view(doc, time.time())
    return emit(args, view, "paired webhook ({}). Outbound-only: answer asks in Herdr.\nleaves this machine: {}\ntry it: herdr-synapse remote test".format(
        view["destination"], view["leaves_machine"]))


def _run_pair(args: argparse.Namespace, layout: Any) -> int:
    if getattr(args, "argv_secret", None) is not None:
        raise HerdrTeamError("secret_in_argv", "secrets are never taken on the command line, where ps and shell history can read them; "
                             "use the -file PATH or -env VAR form of the same flag", EXIT_USAGE)
    channel = getattr(args, "channel", None)
    complete = bool(getattr(args, "complete", False))
    if channel is None and not complete:
        raise HerdrTeamError("usage", "remote pair needs a channel: ntfy, telegram or webhook (or --complete)", EXIT_USAGE)
    if complete and channel not in (None, "telegram"):
        raise HerdrTeamError("usage", "--complete finishes a Telegram pairing", EXIT_USAGE)
    author, team = _authorize(args, layout, "remote pair")
    if complete:
        return _complete_telegram(args, layout, author, team)
    return {"ntfy": _pair_ntfy, "telegram": _pair_telegram, "webhook": _pair_webhook}[channel](args, layout, author, team)


def _run_unpair(args: argparse.Namespace, layout: Any) -> int:
    author, team = _authorize(args, layout, "remote unpair")
    with _remote.config_lock(layout.config_dir):
        doc = _remote.load_config(layout.config_dir)
        channel = _remote.channel_of(doc)
        if channel is None:
            raise HerdrTeamError("remote_not_paired", "no phone channel is paired", EXIT_REFUSED)
        _remote.save_config(layout.config_dir, _remote.unpaired_config(doc))
        _remote.remove_file(_remote.poll_path(layout.config_dir))
    _audit(layout, team, "remote_unpaired", author, {"channel": channel})
    return emit(args, {"unpaired": channel, "policy": _remote.policy_of(doc)},
                "unpaired {}: its secret is deleted and nothing more is sent; the policy is kept".format(channel))


def _run_test(args: argparse.Namespace, layout: Any) -> int:
    author, team = _authorize(args, layout, "remote test", strict=False)
    doc = _remote.load_config(layout.config_dir)
    if not _remote.is_paired(doc):
        raise HerdrTeamError("remote_not_paired", "no phone channel is paired; see: herdr-synapse remote status", EXIT_REFUSED)
    title, body = _remote.compose_test(layout.slug, doc)
    outcome = _remote.send_message(doc, _remote.make_transport(), title, body, 3)
    _audit(layout, team, "remote_sent", author, {"channel": _remote.channel_of(doc), "category": "test", "ok": outcome.ok})
    if not outcome.ok:
        raise HerdrTeamError("remote_send_failed", "the test message did not go out: {}".format(_remote.scrub(outcome.error, doc)), EXIT_REFUSED)
    return emit(args, {"sent": True, "channel": _remote.channel_of(doc), "destination": _remote.destination(doc)},
                "test message sent via {} ({})".format(_remote.channel_of(doc), _remote.destination(doc)))


def _run_policy(args: argparse.Namespace, layout: Any) -> int:
    if args.send is None and args.text is None:
        doc = _remote.load_config(layout.config_dir)
        policy = _remote.policy_of(doc)
        return emit(args, {"policy": policy, "changed": False, "leaves_machine": _remote.leaves_machine(policy)},
                    "sends: {} · text: {}\nleaves this machine: {}".format(", ".join(policy["send"]) or "nothing", policy["text"], _remote.leaves_machine(policy)))
    send = _remote.parse_send(args.send) if args.send is not None else None
    author, team = _authorize(args, layout, "remote policy")
    with _remote.config_lock(layout.config_dir):
        doc = _remote.load_config(layout.config_dir)
        policy = _remote.policy_of(doc)
        if send is not None:
            policy["send"] = send
        if args.text is not None:
            policy["text"] = args.text
        doc = dict(doc, v=1, policy=policy)
        _remote.save_config(layout.config_dir, doc)
    _audit(layout, team, "remote_policy", author, dict(policy))
    return emit(args, {"policy": policy, "changed": True, "leaves_machine": _remote.leaves_machine(policy)},
                "sends: {} · text: {}\nleaves this machine: {}".format(", ".join(policy["send"]) or "nothing", policy["text"], _remote.leaves_machine(policy)))


def _run_remote(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    action = getattr(args, "remote_action", None) or "status"
    if action == "status":
        return _run_status(args, layout)
    if action == "pair":
        return _run_pair(args, layout)
    if action == "unpair":
        return _run_unpair(args, layout)
    if action == "test":
        return _run_test(args, layout)
    return _run_policy(args, layout)


COMMANDS: List[Command] = [
    Command("remote", "reach your phone when an agent asks you something (opt-in, outbound HTTPS only)", _add_remote_arguments, _run_remote),
]

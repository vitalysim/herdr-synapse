"""Text sanitization, name grammar, marker detection, and display width.

Plan references: 6.1 (record text), 5.1 and 5.5 (names and reserved words),
5.3 (24-column headlines), 8.3 (echo markers), 4.3 (``--name`` labels).

``sanitize_text`` pipeline, in order:

1. strict UTF-8 (bytes are decoded strictly; a ``str`` carrying lone
   surrogates is refused the same way) -> ``invalid_utf8``;
2. ``\\r\\n`` and ``\\r`` become ``\\n``;
3. escape sequences are removed: CSI (``ESC [`` ... final byte), OSC
   (``ESC ]`` ... BEL or ST), DCS/SOS/PM/APC through ST, ``ESC`` plus one
   final character, and any lone ``ESC``; the C1 single-byte introducers
   (U+009B, U+009D, U+0090, U+0098, U+009E, U+009F) are treated like their
   7-bit forms; an unterminated string sequence is cut at the end of its line;
4. every remaining C0 and C1 control (including U+007F, U+0085, U+000B)
   except ``\\n`` and ``\\t`` is removed;
5. every character of Unicode category ``Cf`` (bidi overrides, zero-width
   spaces, BOM, soft hyphen, tags) is replaced by U+FFFD so its presence stays
   visible, except U+200D when it joins two emoji;
6. runs of combining marks (``Mn``, ``Mc``, ``Me``) are capped at three;
7. U+2028 and U+2029 are re-escaped as the literal text ``\\u2028`` and
   ``\\u2029`` so no line separator survives that is not ``\\n``;
8. trailing whitespace is trimmed on every line and at the end;
9. the result must be at most ``max_len`` characters -> ``text_too_long``.

``headline`` and ``display_width`` implement the 24-column token trim with
``unicodedata.east_asian_width`` (no ``wcwidth`` package here): wide and
fullwidth characters count 2, combining marks and ``Cf`` count 0.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, FrozenSet, List, Optional, Union

from herdr_team.errors import HerdrTeamError
from herdr_team.paths import ROLE_NAME_RE, TEAM_NAME_RE

MAX_TEXT_CHARS = 2000
ADVISED_TEXT_CHARS = 500
HEADLINE_COLUMNS = 24
MAX_TOKEN_CHARS = 80
LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}\Z")
MARKER_PREFIX = "[herdr-team"
NONCE_RE = re.compile(r"\[n[0-9]+\]")

#: Agent-name grammar (Herdr's 32-character cap, ``agent.rename``).
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}\Z")
MAX_COMBINING_RUN = 3

#: Words a member, role, or team may never be called (plan 5.1).
RESERVED_NAMES: FrozenSet[str] = frozenset({"human", "all", "me", "none", "system", "team"})

#: The 22 kind labels of the installed Herdr 0.8.2 binary, verified against
#: ``herdr agent start --help`` (``[possible values: ...]``) on 2026-09-04.
#: ``test_sanitize`` re-checks this list against the binary when one is on PATH.
KIND_LABELS: FrozenSet[str] = frozenset({
    "pi", "claude", "codex", "gemini", "cursor", "devin", "agy", "cline", "omp",
    "mastracode", "opencode", "copilot", "kimi", "kiro", "droid", "amp", "grok",
    "hermes", "kilo", "qodercli", "qwen", "maki",
})

#: Every alias ``src/detect/mod.rs::lookup_agent`` maps to a kind at HEAD
#: (``3150bd92``), including the ``muse`` kind that exists only past the tag.
#: Aliases with spaces never pass the grammar but are listed for completeness.
KIND_ALIASES: FrozenSet[str] = frozenset({
    "claude-code",
    "cursor-agent",
    "devin-cli", "devin cli",
    "antigravity", "antigravity-cli",
    "mastra-code", "mastra code",
    "opencode2", "open-code",
    "github-copilot", "ghcs",
    "kimi-code", "kimi code",
    "kiro-cli",
    "amp-local",
    "grok-build",
    "hermes-agent",
    "kilo-code", "kilo code",
    "qoderclicn", "qoder", "qodercn",
    "qwen-code", "qwen code",
    "muse", "muse-code", "muse-cli",
})

#: The full refusal set for names, roles, and team names.
RESERVED_WORDS: FrozenSet[str] = RESERVED_NAMES | KIND_LABELS | KIND_ALIASES

# Escape sequences. Alternatives are tried in order at each position, so the
# specific introducers come before the generic ``ESC final`` and lone ``ESC``.
_ESCAPE_RE = re.compile(
    r"(?:\x1b\[|\x9b)[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]?"  # CSI
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"  # OSC through BEL or ST
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c\n]*(?:\x1b\\|\x9c)?"  # DCS SOS PM APC through ST
    r"|\x1b[\x20-\x2f]*[\x30-\x7e]"  # ESC, intermediates, final
    r"|\x1b"  # lone ESC
)
# C0 except TAB (0x09) and LF (0x0a); DEL; C1.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_LINE_SEPARATORS = {"\u2028": "\\u2028", "\u2029": "\\u2029"}
_REPLACEMENT = "\ufffd"
_ZWJ = "\u200d"
_VS16 = "\ufe0f"
_COMBINING_CATEGORIES = ("Mn", "Mc", "Me")
_EMOJI_RANGES = (
    (0x2300, 0x23FF),  # miscellaneous technical (⌚ ⏰ ...)
    (0x2600, 0x27BF),  # miscellaneous symbols and dingbats (☀ ❤ ✈ ...)
    (0x2B00, 0x2BFF),  # arrows and stars (⭐ ...)
    (0x1F000, 0x1FAFF),  # emoticons, pictographs, flags, skin tones, symbols
)
_EMOJI_TAIL = frozenset({0xFE0E, 0xFE0F} | set(range(0x1F3FB, 0x1F400)))

#: Secret shapes refused by ``post`` without ``--force`` (plan 9.1, register).
SECRET_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "sk_api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    "pem_block": re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)?-----"),
}


# --- errors -----------------------------------------------------------------


def _error(code: str, message: str, **details: Any) -> HerdrTeamError:
    return HerdrTeamError(code, message, details=details)


# --- UTF-8 -------------------------------------------------------------------


def validate_utf8(data: Union[bytes, bytearray, str]) -> str:
    """Decode strictly; raise ``invalid_utf8`` (exit 1) on failure.

    A ``str`` is accepted too: it must round-trip through UTF-8, which
    refuses lone surrogates left by ``surrogateescape`` argv decoding.
    """
    if isinstance(data, str):
        try:
            data.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise _error("invalid_utf8", "text is not valid UTF-8: {}".format(exc.reason), position=exc.start)
        return data
    try:
        return bytes(data).decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise _error("invalid_utf8", "text is not valid UTF-8: {}".format(exc.reason), position=exc.start)


# --- character classes -------------------------------------------------------


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    for low, high in _EMOJI_RANGES:
        if low <= cp <= high:
            return True
    return False


def _is_combining(ch: str) -> bool:
    return unicodedata.category(ch) in _COMBINING_CATEGORIES


def _is_format(ch: str) -> bool:
    return unicodedata.category(ch) == "Cf"


def _zwj_joins_emoji(out: List[str], text: str, index: int) -> bool:
    """True when the ZWJ at ``text[index]`` sits between two emoji."""
    # Look back through already-emitted characters, skipping presentation
    # selectors, skin tones, and combining marks, to the previous base.
    j = len(out) - 1
    while j >= 0 and (ord(out[j]) in _EMOJI_TAIL or _is_combining(out[j])):
        j -= 1
    if j < 0 or not _is_emoji(out[j]):
        return False
    k = index + 1
    return k < len(text) and _is_emoji(text[k])


# --- pipeline stages ---------------------------------------------------------


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_escapes(text: str) -> str:
    """Remove CSI, OSC, DCS, SOS, PM, APC, ``ESC x``, and lone ESC sequences."""
    return _ESCAPE_RE.sub("", text)


def strip_controls(text: str) -> str:
    """Remove escape sequences and control characters, keep ``\\n`` and ``\\t``.

    ``\\r\\n`` and ``\\r`` are normalized to ``\\n`` first so a lone carriage
    return keeps its line break instead of vanishing.
    """
    return _CONTROL_RE.sub("", strip_escapes(normalize_newlines(text)))


def replace_format_chars(text: str, replacement: str = _REPLACEMENT) -> str:
    """Replace every ``Cf`` character by ``replacement`` except an emoji-joining ZWJ.

    ``replacement=""`` drops them instead (headlines and tokens).
    """
    out: List[str] = []
    for index, ch in enumerate(text):
        if ch == _ZWJ and _zwj_joins_emoji(out, text, index):
            out.append(ch)
        elif _is_format(ch):
            if replacement:
                out.append(replacement)
        else:
            out.append(ch)
    return "".join(out)


def cap_combining_runs(text: str, limit: int = MAX_COMBINING_RUN) -> str:
    out: List[str] = []
    run = 0
    for ch in text:
        if _is_combining(ch):
            run += 1
            if run > limit:
                continue
        else:
            run = 0
        out.append(ch)
    return "".join(out)


def escape_line_separators(text: str) -> str:
    for raw, escaped in _LINE_SEPARATORS.items():
        text = text.replace(raw, escaped)
    return text


def trim_trailing_whitespace(text: str) -> str:
    return _TRAILING_WS_RE.sub("", text).rstrip("\n\t ")


# --- public sanitizers -------------------------------------------------------


def sanitize_text(text: Union[str, bytes], max_len: int = MAX_TEXT_CHARS) -> str:
    """Full board-text sanitizer; raises ``text_too_long`` when the result exceeds ``max_len``."""
    text = validate_utf8(text)
    text = strip_controls(text)
    # Plan 6.1: Unicode Cf is stripped (a soft hyphen or BOM in a pasted URL must not become U+FFFD).
    text = replace_format_chars(text, "")
    text = cap_combining_runs(text)
    text = escape_line_separators(text)
    text = trim_trailing_whitespace(text)
    if len(text) > max_len:
        raise _error(
            "text_too_long",
            "text is {} characters after sanitization; the limit is {}".format(len(text), max_len),
            length=len(text),
            max=max_len,
            hint="--spill writes the body to payloads/<seq>-body.md",
        )
    return text


def sanitize_label(label: Optional[str]) -> Optional[str]:
    """``None`` when absent; raise ``label_invalid`` when it fails ``LABEL_RE``."""
    if label is None:
        return None
    if not isinstance(label, str) or not LABEL_RE.match(label):
        raise _error(
            "label_invalid",
            "label must match [A-Za-z0-9._-]{1,32}",
            label=strip_controls(str(label))[:64],
        )
    return label


def is_reserved(name: str) -> bool:
    return name in RESERVED_WORDS


def sanitize_name(name: Any, what: str = "member") -> str:
    """Validate a member (default), ``team``, or ``role`` name.

    Grammar: member ``[a-z][a-z0-9_-]{0,31}``, team ``{0,14}``, role ``{0,31}``.
    Reserved words, kind labels, and kind aliases are refused for all three.
    Error codes follow ``docs/cli.md``: ``name_invalid``/``name_reserved``,
    ``team_name_invalid``, ``role_invalid``.
    """
    if what == "team":
        pattern, grammar, invalid_code, reserved_code = TEAM_NAME_RE, "[a-z][a-z0-9_-]{0,14}", "team_name_invalid", "team_name_invalid"
    elif what == "role":
        pattern, grammar, invalid_code, reserved_code = ROLE_NAME_RE, "[a-z][a-z0-9_-]{0,31}", "role_invalid", "role_invalid"
    elif what == "member":
        pattern, grammar, invalid_code, reserved_code = NAME_RE, "[a-z][a-z0-9_-]{0,31}", "name_invalid", "name_reserved"
    else:
        raise ValueError("unknown name class: {!r}".format(what))
    shown = strip_controls(str(name))[:64] if name is not None else ""
    if not isinstance(name, str) or not pattern.match(name):
        raise _error(invalid_code, "{} name {!r} must match {}".format(what, shown, grammar), name=shown, what=what)
    if is_reserved(name):
        raise _error(reserved_code, "{} name {!r} is reserved (a kind label, alias, or reserved word)".format(what, name), name=name, what=what)
    return name


def sanitize_team_name(name: Any) -> str:
    return sanitize_name(name, "team")


def sanitize_role(name: Any) -> str:
    return sanitize_name(name, "role")


# --- markers and secrets -----------------------------------------------------


def is_marker_text(text: str) -> bool:
    """True when ``text`` would echo a nudge, briefing, probe, or nonce."""
    if not isinstance(text, str):
        return False
    if text.lstrip().startswith(MARKER_PREFIX):
        return True
    return NONCE_RE.search(text) is not None


def echo_rejected(text: str) -> bool:
    """Alias of ``is_marker_text`` named after the exit-4 error code."""
    return is_marker_text(text)


def secret_patterns(text: str) -> List[Dict[str, Any]]:
    """Matches of well-known secret shapes: ``[{"pattern", "start", "end", "excerpt"}]``.

    ``excerpt`` keeps the first six characters and masks the rest.
    """
    found: List[Dict[str, Any]] = []
    for name, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            token = match.group(0)
            found.append({
                "pattern": name,
                "start": match.start(),
                "end": match.end(),
                "excerpt": token[:6] + "…" if len(token) > 6 else token,
            })
    found.sort(key=lambda item: item["start"])
    return found


# --- display width -----------------------------------------------------------


def char_width(ch: str) -> int:
    cp = ord(ch)
    if cp < 0x20 or 0x7F <= cp < 0xA0:
        return 0
    category = unicodedata.category(ch)
    if category in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _widths(text: str) -> List[int]:
    """Per-character widths; U+FE0F after a narrow base upgrades that base to 2 columns."""
    widths: List[int] = []
    for index, ch in enumerate(text):
        width = char_width(ch)
        if ch == _VS16 and index > 0:
            base = index - 1
            while base > 0 and widths[base] == 0 and text[base] != _VS16:
                base -= 1
            if widths[base] == 1:
                widths[base] = 2
        widths.append(width)
    return widths


def display_width(text: str) -> int:
    """Terminal columns ``text`` occupies (east-asian wide = 2, combining = 0, emoji presentation = 2)."""
    return sum(_widths(text))


def truncate_columns(text: str, columns: int, ellipsis: str = "…") -> str:
    """Cut ``text`` to at most ``columns`` display columns, ending with ``ellipsis`` when cut.

    The cut never separates a base character from its combining marks or
    presentation selectors.
    """
    if columns <= 0:
        return ""
    if display_width(text) <= columns:
        return text
    ellipsis_width = display_width(ellipsis)
    if ellipsis_width > columns:
        ellipsis = ""
        ellipsis_width = 0
    budget = columns - ellipsis_width
    out: List[str] = []
    used = 0
    widths = _widths(text)
    for index, ch in enumerate(text):
        width = widths[index]
        if width == 0 and out:
            out.append(ch)
            continue
        if used + width > budget:
            break
        out.append(ch)
        used += width
    # Do not end on a dangling ZWJ.
    while out and out[-1] == _ZWJ:
        out.pop()
    return "".join(out) + ellipsis


def trim_columns(text: str, columns: int, ellipsis: str = "…") -> str:
    """Alias of ``truncate_columns``."""
    return truncate_columns(text, columns, ellipsis)


def headline(text: Optional[str], columns: int = HEADLINE_COLUMNS) -> str:
    """First non-blank line of ``text`` with controls and ``Cf`` stripped, cut to ``columns``.

    Tabs collapse to single spaces; runs of whitespace collapse. Also capped
    at ``MAX_TOKEN_CHARS`` characters so it always fits a metadata token.
    """
    if not text:
        return ""
    cleaned = replace_format_chars(strip_controls(str(text)), "")
    cleaned = cap_combining_runs(cleaned)
    line = ""
    for candidate in cleaned.split("\n"):
        candidate = " ".join(candidate.split())
        if candidate:
            line = candidate
            break
    line = truncate_columns(line, columns)
    if len(line) > MAX_TOKEN_CHARS:
        line = line[: MAX_TOKEN_CHARS - 1] + "…"
    return line

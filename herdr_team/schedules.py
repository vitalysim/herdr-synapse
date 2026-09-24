"""Recurring operator posts on a timetable: the model, the cron engine, and firing.

Recurring work ("weekdays 09:00, ask the analyst to triage yesterday's brand
mentions", "Mondays, run the competitor scan") should not depend on the
operator remembering to post it. A schedule is an operator post written in
advance: the notifier daemon appends it on the operator's behalf when it falls
due (``from: human``, ``origin.via: schedule``, which ``identity.record_human_ok``
accepts), and the ordinary delivery path nudges the recipients when they are
idle. Nothing here types into a pane.

Three parts, kept apart so each is testable on its own:

* **The cron engine** (``parse_cron``, ``next_fires``, ``previous_fire``) is
  pure: a spec, an instant and a tzinfo in, instants out. Five Vixie-cron
  fields with ``*``, lists, ranges, steps and month/day names; when both
  day-of-month and day-of-week are restricted a day matches either (a field
  that starts with ``*`` counts as unrestricted, exactly as in Vixie cron).
  Times are local wall-clock minutes in the schedule's zone and DST-safe: a
  minute a fall-back change repeats fires once, at its first occurrence; a
  minute a spring-forward change skips is shifted forward by the jump (02:30
  becomes 03:30) and fires once, merged with any real 03:30 slot.
* **Storage.** Definitions live in ``<team>/schedules.json``, indented and
  written under ``team.lock``, so the operator can read and edit them. The
  daemon's bookkeeping (last slot handled, last outcome, next due) lives in
  ``notifier/schedules-state.json`` and never rewrites the operator's file.
  The team directory sits in the plugin's state root and carries operator
  authority like ``team.json``: an edit there is the operator's edit.
* **Firing** (``Runner`` for the daemon, ``run_now`` for ``schedule run``).
  Each definition carries ``action: {"type": "post"}`` and firing dispatches
  on that type through ``ACTIONS``, so another action (creating a work item)
  is one more handler rather than a second firing path. A precheck, when set,
  runs as ``/bin/sh -c`` in a worker thread so the 250 ms tick never waits on
  it; its result is collected on a later tick.

Idempotency: the last slot handled is persisted per schedule, and before a
post is appended the board tail is searched for one already carrying the same
``schedule.id`` and ``schedule.slot``, so a daemon that dies between the append
and the state write does not post twice when it comes back.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from herdr_team import identity as _identity
from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, EXIT_USAGE, HerdrTeamError
from herdr_team.paths import Layout, TeamPaths

SCHEMA = 1
DEFINITIONS_FILE = "schedules.json"
STATE_FILE = "schedules-state.json"

#: The actions a schedule can fire. ``post`` appends an operator post; the
#: dispatch table is ``ACTIONS`` at the end of this module.
ACTION_POST = "post"
#: The same timetable, but each run is a work item for its recipient (0.19): tracked,
#: settled with an outcome, visible in ``work next`` and mission control.
ACTION_WORK = "work"
KINDS = ("request", "note", "question")
DEFAULT_KIND = "request"
PRESETS = ("hourly", "daily", "weekdays", "weekly")
DEFAULT_AT = "09:00"
DEFAULT_GRACE_S = 30 * 60
MAX_GRACE_S = 7 * 86400
DEFAULT_PRECHECK_TIMEOUT_S = 60
MAX_PRECHECK_TIMEOUT_S = 3600
MAX_PRECHECK_CHARS = 1000
MAX_SCHEDULES = 100
#: A slot handled within its own minute is on time whatever the grace says:
#: ``--grace 0`` means "never catch up", not "miss a slot the daemon saw 3 s late".
ON_TIME_S = 60.0
#: How often the daemon re-reads definitions when nothing is due, so a new or
#: edited schedule is noticed. A due slot is acted on at the next 250 ms tick.
POLL_S = 5.0
#: How far back ``previous_fire`` and forward ``next_fires`` look when nothing
#: bounds them: one whole Gregorian cycle, after which every calendar repeats,
#: so a spec with no fire in it never fires.
MAX_SEARCH_DAYS = 400 * 366 + 2
MAX_SPAN_DAYS = 3660
#: Board records searched for a post already written for a slot (the restart dedupe).
DEDUPE_LOOKBACK = 400
PRECHECK_OUTPUT_CHARS = 2000
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
LOCALTIME_PATH = "/etc/localtime"

ID_RE = re.compile(r"^s[0-9]{1,6}\Z")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}\Z")
_AT_RE = re.compile(r"^([0-9]{1,2}):([0-9]{2})\Z")
_MINUTE_RE = re.compile(r"^:?([0-9]{1,2})\Z")
_DURATION_RE = re.compile(r"^([0-9]+)\s*([smhd]?)\Z")

ONE_DAY = timedelta(days=1)

#: Wall clock for the CLI (tests patch it). The daemon passes its own.
_now: Callable[[], float] = time.time


# --------------------------------------------------------------------------
# errors


def _invalid(message: str, **details: Any) -> HerdrTeamError:
    return HerdrTeamError("schedule_invalid", message, EXIT_USAGE, details)


# --------------------------------------------------------------------------
# the cron engine

#: English and fixed: ``calendar.month_abbr`` follows the locale, and a cron line must not change meaning with it.
MONTH_NAMES = {name: i + 1 for i, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
DOW_LABELS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_DOW_FULL = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6}
MACROS = {
    "@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
}
#: (name, low, high, names) per field; day of week accepts 7 for Sunday.
_FIELDS: Tuple[Tuple[str, int, int, Dict[str, int]], ...] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, MONTH_NAMES),
    ("day of week", 0, 7, DOW_NAMES),
)


@dataclass(frozen=True)
class CronSpec:
    """A parsed five-field cron expression; day of week is 0..6 with Sunday 0."""

    text: str
    minutes: Tuple[int, ...]
    hours: Tuple[int, ...]
    doms: FrozenSet[int]
    months: FrozenSet[int]
    dows: FrozenSet[int]
    dom_star: bool
    dow_star: bool

    def day_matches(self, day: date) -> bool:
        """Vixie cron: either day field starting with ``*`` makes the two an AND, otherwise an OR."""
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.doms
        dow_ok = (day.isoweekday() % 7) in self.dows
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok


def _field_value(token: str, name: str, low: int, high: int, names: Dict[str, int]) -> int:
    key = token.strip().lower()
    if key in names:
        return names[key]
    if not key.isdigit():
        raise _invalid("{}: {!r} is not a number{}".format(name, token, " or a name ({})".format(", ".join(sorted(names, key=names.get))) if names else ""), field=name)
    value = int(key)
    if not low <= value <= high:
        raise _invalid("{}: {} is outside {}-{}".format(name, value, low, high), field=name)
    return value


def _parse_field(text: str, name: str, low: int, high: int, names: Dict[str, int]) -> FrozenSet[int]:
    values = set()
    for part in text.split(","):
        if not part:
            raise _invalid("{}: empty list item in {!r}".format(name, text), field=name)
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise _invalid("{}: step {!r} must be a positive number".format(name, step_text), field=name)
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, _, right = base.partition("-")
            start = _field_value(left, name, low, high, names)
            # ``fri-sun``: Sunday closes a range as 7 (Vixie refuses 5-0; this reads it the way it is meant).
            end = 7 if name == "day of week" and right.strip().lower() == "sun" and start > 0 else _field_value(right, name, low, high, names)
            if start > end:
                raise _invalid("{}: range {!r} runs backwards".format(name, base), field=name)
        else:
            start = _field_value(base, name, low, high, names)
            end = high if step_text else start  # ``5/15`` is 5..max every 15, as in Vixie cron
        values.update(range(start, end + 1, step))
    if name == "day of week" and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


def parse_cron(text: str) -> CronSpec:
    """Parse ``M H DOM MON DOW`` (or an ``@daily``-style macro); ``schedule_invalid`` (exit 2) on any error."""
    if not isinstance(text, str) or not text.strip():
        raise _invalid("the cron expression is empty; expected five fields: minute hour day-of-month month day-of-week")
    source = " ".join(text.split())
    expanded = MACROS.get(source.lower(), source)
    parts = expanded.split(" ")
    if len(parts) != 5:
        raise _invalid("{!r} has {} fields; cron needs five: minute hour day-of-month month day-of-week".format(source, len(parts)), cron=source)
    parsed = [_parse_field(part, name, low, high, names) for part, (name, low, high, names) in zip(parts, _FIELDS)]
    spec = CronSpec(
        text=source, minutes=tuple(sorted(parsed[0])), hours=tuple(sorted(parsed[1])),
        doms=parsed[2], months=parsed[3], dows=parsed[4],
        dom_star=parts[2].startswith("*"), dow_star=parts[4].startswith("*"),
    )
    if (spec.dom_star or spec.dow_star) and not any(d <= _max_days(m) for m in spec.months for d in spec.doms):
        # Checked statically so ``0 9 30 2 *`` is refused at once rather than after a 400-year scan.
        raise _invalid("{!r} never fires: no month in it has that day".format(source), cron=source)
    return spec


def _max_days(month: int) -> int:
    return 29 if month == 2 else calendar.monthrange(2001, month)[1]


def _as_utc(value: Union[datetime, float, int]) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime: pass an aware datetime or an epoch")
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(value), timezone.utc)


def _resolve(day: date, hour: int, minute: int, zone: tzinfo) -> datetime:
    """The UTC instant of a local wall-clock minute.

    ``fold=0`` is PEP 495's reading: a repeated minute (fall back) is its first
    occurrence; a skipped one (spring forward) is read with the offset before
    the jump, which lands it the size of the jump later (02:30 -> 03:30).
    """
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(timezone.utc)


def _instants(spec: CronSpec, zone: tzinfo, first: date, last: date) -> List[datetime]:
    """Every fire of the local dates ``first..last`` as sorted, de-duplicated UTC instants."""
    out = set()
    day = first
    while day <= last:
        if spec.day_matches(day):
            for hour in spec.hours:
                for minute in spec.minutes:
                    out.add(_resolve(day, hour, minute, zone))
        day += ONE_DAY
    return sorted(out)


def next_fires(spec: Union[CronSpec, str], after: Union[datetime, float], count: int = 1, tz: Optional[tzinfo] = None) -> List[datetime]:
    """The next ``count`` fires strictly after ``after``, as aware datetimes in ``tz`` (UTC when None).

    Dates are scanned in growing windows. The window's last date is only a
    lookahead: a minute of the date before it can be pushed past midnight by a
    DST jump, so an instant is final only once it is before the last date's own
    midnight. Fewer than ``count`` come back only for a spec that stops firing
    within ``MAX_SEARCH_DAYS``.
    """
    cron = spec if isinstance(spec, CronSpec) else parse_cron(spec)
    zone = tz or timezone.utc
    start = _as_utc(after)
    first_day = start.astimezone(zone).date() - ONE_DAY
    horizon = first_day + timedelta(days=MAX_SEARCH_DAYS)
    found: List[datetime] = []
    span = 3
    while len(found) < count:
        last_day = min(first_day + timedelta(days=span - 1), horizon)
        bound = None if last_day >= horizon else _resolve(last_day, 0, 0, zone)
        floor = found[-1] if found else start
        for instant in _instants(cron, zone, first_day, last_day):
            if instant <= floor:
                continue
            if bound is not None and instant >= bound:
                break
            found.append(instant)
            if len(found) >= count:
                break
        if last_day >= horizon:
            break
        first_day = last_day - ONE_DAY
        span = min(span * 2, MAX_SPAN_DAYS)
    return [instant.astimezone(zone) for instant in found]


def previous_fire(spec: Union[CronSpec, str], before: Union[datetime, float], tz: Optional[tzinfo] = None, not_before: Union[datetime, float, None] = None) -> Optional[datetime]:
    """The latest fire at or before ``before`` and strictly after ``not_before``; None when there is none.

    The mirror of ``next_fires``: the window's first date is the lookbehind,
    so an instant is final once it is at or after the next date's midnight.
    """
    cron = spec if isinstance(spec, CronSpec) else parse_cron(spec)
    zone = tz or timezone.utc
    end = _as_utc(before)
    floor = _as_utc(not_before) if not_before is not None else None
    last_day = end.astimezone(zone).date() + ONE_DAY
    floor_day = floor.astimezone(zone).date() - ONE_DAY if floor is not None else last_day - timedelta(days=MAX_SEARCH_DAYS)
    span = 3
    while True:
        first_day = max(last_day - timedelta(days=span - 1), floor_day)
        bound = None if first_day <= floor_day else _resolve(first_day + ONE_DAY, 0, 0, zone)
        for instant in reversed(_instants(cron, zone, first_day, last_day)):
            if instant > end:
                continue
            if bound is not None and instant < bound:
                break
            if floor is not None and instant <= floor:
                return None
            return instant.astimezone(zone)
        if first_day <= floor_day:
            return None
        last_day = first_day + ONE_DAY
        span = min(span * 2, MAX_SPAN_DAYS)


# --------------------------------------------------------------------------
# presets, times, durations


def parse_at(text: str) -> Tuple[int, int]:
    match = _AT_RE.match(str(text).strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise _invalid("--at expects HH:MM on a 24-hour clock, e.g. 09:00 or 17:30", at=text)
    return int(match.group(1)), int(match.group(2))


def parse_days(text: str) -> List[int]:
    days = set()
    for part in str(text).split(","):
        key = part.strip().lower()
        value = DOW_NAMES.get(key[:3]) if key in _DOW_FULL or key in DOW_NAMES else None
        if value is None:
            raise _invalid("--day expects mon, tue, wed, thu, fri, sat or sun (a comma list is fine), not {!r}".format(part.strip()), day=text)
        days.add(value)
    return sorted(days)


def preset_cron(every: str, at: Optional[str] = None, day: Optional[str] = None) -> str:
    """The cron text for ``--every``: ``hourly`` takes the minute (``--at :15``), the rest ``HH:MM``."""
    if every not in PRESETS:
        raise _invalid("--every expects one of {}".format(", ".join(PRESETS)), every=every)
    if day is not None and every != "weekly":
        raise _invalid("--day goes with --every weekly", day=day)
    if every == "hourly":
        match = _MINUTE_RE.match(str(at).strip()) if at is not None else None
        if at is not None and (not match or int(match.group(1)) > 59):
            raise _invalid("--every hourly takes the minute past each hour: --at :15", at=at)
        return "{} * * * *".format(int(match.group(1)) if match else 0)
    hour, minute = parse_at(at if at is not None else DEFAULT_AT)
    if every == "daily":
        return "{} {} * * *".format(minute, hour)
    if every == "weekdays":
        return "{} {} * * 1-5".format(minute, hour)
    return "{} {} * * {}".format(minute, hour, ",".join(DOW_LABELS[d] for d in parse_days(day or "mon")))


def parse_duration(text: Any, flag: str, unit: str, maximum: int) -> int:
    """``90s``, ``30m``, ``2h``, ``1d``; a bare number is in ``unit``. ``schedule_invalid`` past ``maximum``."""
    match = _DURATION_RE.match(str(text).strip().lower())
    if not match:
        raise _invalid("{} expects a duration like 90s, 30m or 2h".format(flag), value=text)
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2) or unit]
    if seconds > maximum:
        raise _invalid("{} is at most {}".format(flag, format_duration(maximum)), value=text)
    return seconds


def format_duration(seconds: int) -> str:
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds and seconds % size == 0:
            return "{}{}".format(seconds // size, suffix)
    return "{}s".format(seconds)


# --------------------------------------------------------------------------
# time zones


def load_zone(name: str) -> tzinfo:
    """An IANA zone through stdlib ``zoneinfo``; ``tz_unknown`` (exit 2) with an example otherwise."""
    if isinstance(name, str) and name.upper() in ("UTC", "Z"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
    except ImportError:  # pragma: no cover - the plugin's floor is 3.9
        raise HerdrTeamError("tz_unsupported", "this Python has no zoneinfo module; leave --tz off to use local time", EXIT_USAGE)
    if not isinstance(name, str) or not name.strip() or name.startswith("/") or ".." in name:
        raise HerdrTeamError("tz_unknown", "unknown time zone {!r}: use an IANA name such as Europe/Berlin or America/New_York".format(name), EXIT_USAGE, {"tz": name})
    try:
        return ZoneInfo(name.strip())
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, or an unreadable tz file: all "unknown"
        raise HerdrTeamError("tz_unknown", "unknown time zone {!r}: use an IANA name such as Europe/Berlin or America/New_York".format(name), EXIT_USAGE, {"tz": name})


def local_zone(env: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], tzinfo]:
    """The machine's zone as ``(IANA name or None, tzinfo)``: ``TZ``, then ``/etc/localtime``, else UTC.

    ``schedule add`` stores the name it finds, so the fire times it printed are
    the ones the daemon uses even if the daemon's environment differs.
    """
    value = ((env or {}).get("TZ") or "").strip().lstrip(":")
    if value and not value.startswith("/"):
        try:
            return value, load_zone(value)
        except HerdrTeamError:
            pass
    target = os.path.realpath(value if value.startswith("/") else LOCALTIME_PATH)
    if "/zoneinfo/" in target:
        name = target.split("/zoneinfo/", 1)[1]
        try:
            return name, load_zone(name)
        except HerdrTeamError:
            pass
    try:
        from zoneinfo import ZoneInfo

        with open(target, "rb") as handle:
            return None, ZoneInfo.from_file(handle, key="localtime")
    except Exception:  # noqa: BLE001 - no readable local zone: the C library's own default is UTC
        return "UTC", timezone.utc


def zone_of(tz_name: Optional[str], env: Optional[Dict[str, str]] = None) -> Tuple[str, tzinfo]:
    """``(label, tzinfo)`` for a stored ``tz`` (None: the machine's local zone)."""
    if tz_name:
        return tz_name, load_zone(tz_name)
    name, zone = local_zone(env)
    return name or "local", zone


# --------------------------------------------------------------------------
# formatting


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_time(moment: datetime) -> str:
    return moment.strftime("%a %Y-%m-%d %H:%M %Z").rstrip()


def parse_time(value: Any) -> Optional[float]:
    """Epoch seconds of an ISO-8601 string (``Z`` or an offset), None when absent or unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def iso_now(wall: Optional[float] = None) -> str:
    moment = datetime.fromtimestamp(_now() if wall is None else wall, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(moment.microsecond // 1000)


# --------------------------------------------------------------------------
# the definition model


@dataclass
class Schedule:
    """One entry of ``schedules.json``. Unknown keys are kept so a later version's fields survive an edit."""

    id: str
    text: str
    cron: str
    to: List[str]
    kind: str = DEFAULT_KIND
    name: Optional[str] = None
    every: Optional[str] = None
    tz: Optional[str] = None
    action: Dict[str, Any] = field(default_factory=lambda: {"type": ACTION_POST})
    precheck: Optional[str] = None
    precheck_timeout_s: int = DEFAULT_PRECHECK_TIMEOUT_S
    grace_s: int = DEFAULT_GRACE_S
    enabled: bool = True
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    updated_at: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    KEYS = ("id", "name", "text", "cron", "every", "tz", "to", "kind", "action", "precheck", "precheck_timeout_s",
            "grace_s", "enabled", "created_at", "created_by", "updated_at")

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def display(self) -> str:
        return "{} ({})".format(self.id, self.name) if self.name else self.id

    @property
    def action_type(self) -> Any:
        return self.action.get("type") if isinstance(self.action, dict) else None

    def spec(self) -> CronSpec:
        return parse_cron(self.cron)

    def digest(self) -> str:
        return hashlib.sha1(json.dumps(self.to_json(), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {key: getattr(self, key) for key in self.KEYS}
        out["to"] = list(self.to)
        out["action"] = dict(self.action)
        for key, value in self.extra.items():
            out.setdefault(key, value)
        return out

    @classmethod
    def from_json(cls, obj: Any) -> "Schedule":
        """Validate one stored entry (a hand edit included); ``schedule_invalid`` names the problem."""
        if not isinstance(obj, dict):
            raise _invalid("a schedule entry must be an object")
        sid = obj.get("id")
        if not isinstance(sid, str) or not ID_RE.match(sid):
            raise _invalid("id {!r} is not of the form s<number>".format(sid), id=sid)
        text = obj.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _invalid("{}: text is empty".format(sid), id=sid)
        cron = obj.get("cron")
        if not isinstance(cron, str):
            raise _invalid("{}: cron is missing".format(sid), id=sid)
        parse_cron(cron)
        to = obj.get("to")
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list) or not to or not all(isinstance(t, str) and t.strip() for t in to):
            raise _invalid("{}: to must name at least one recipient".format(sid), id=sid)
        kind = obj.get("kind", DEFAULT_KIND)
        if kind not in KINDS:
            raise _invalid("{}: kind must be one of {}".format(sid, ", ".join(KINDS)), id=sid)
        name = obj.get("name")
        if name is not None and (not isinstance(name, str) or not NAME_RE.match(name) or ID_RE.match(name)):
            raise _invalid("{}: name {!r} is not a valid schedule name".format(sid, name), id=sid)
        tz = obj.get("tz")
        if tz is not None:
            if not isinstance(tz, str):
                raise _invalid("{}: tz must be a zone name".format(sid), id=sid)
            load_zone(tz)
        action = obj.get("action") if obj.get("action") is not None else {"type": ACTION_POST}
        if not isinstance(action, dict) or not isinstance(action.get("type"), str):
            raise _invalid("{}: action must be an object with a type".format(sid), id=sid)
        precheck = obj.get("precheck")
        if precheck is not None and (not isinstance(precheck, str) or not precheck.strip() or "\x00" in precheck or len(precheck) > MAX_PRECHECK_CHARS):
            raise _invalid("{}: precheck must be a non-empty command of at most {} characters".format(sid, MAX_PRECHECK_CHARS), id=sid)

        def seconds(key: str, default: int, low: int, high: int) -> int:
            value = obj.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise _invalid("{}: {} must be a whole number of seconds from {} to {}".format(sid, key, low, high), id=sid)
            return value

        enabled = obj.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _invalid("{}: enabled must be true or false".format(sid), id=sid)
        every = obj.get("every")
        return cls(
            id=sid, text=text, cron=" ".join(cron.split()), to=[t.strip() for t in to], kind=kind, name=name,
            every=every if every in PRESETS else None, tz=tz, action=dict(action),
            precheck=precheck, precheck_timeout_s=seconds("precheck_timeout_s", DEFAULT_PRECHECK_TIMEOUT_S, 1, MAX_PRECHECK_TIMEOUT_S),
            grace_s=seconds("grace_s", DEFAULT_GRACE_S, 0, MAX_GRACE_S), enabled=enabled,
            created_at=obj.get("created_at") if isinstance(obj.get("created_at"), str) else None,
            created_by=obj.get("created_by") if isinstance(obj.get("created_by"), str) else None,
            updated_at=obj.get("updated_at") if isinstance(obj.get("updated_at"), str) else None,
            extra={k: v for k, v in obj.items() if k not in cls.KEYS},
        )


def describe(sched: Schedule) -> str:
    """``weekdays 09:00``, ``hourly at :15``, ``weekly mon,thu 09:00`` or ``cron '0 9 1 * *'``."""
    fields = sched.cron.split(" ")
    clock = None
    if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
        clock = "{:02d}:{:02d}".format(int(fields[1]), int(fields[0]))
    rest = fields[2:] if len(fields) == 5 else []
    if sched.every == "hourly" and len(fields) == 5 and fields[0].isdigit() and fields[1:] == ["*", "*", "*", "*"]:
        return "hourly at :{:02d}".format(int(fields[0]))
    if clock and sched.every == "daily" and rest == ["*", "*", "*"]:
        return "daily {}".format(clock)
    if clock and sched.every == "weekdays" and rest == ["*", "*", "1-5"]:
        return "weekdays {}".format(clock)
    if clock and sched.every == "weekly" and rest[:2] == ["*", "*"]:
        return "weekly {} {}".format(rest[2], clock)
    return "cron '{}'".format(sched.cron)


# --------------------------------------------------------------------------
# storage


def definitions_path(team: TeamPaths) -> Path:
    return team.root / DEFINITIONS_FILE


def state_path(team: TeamPaths) -> Path:
    return team.notifier_dir / STATE_FILE


def empty_document() -> Dict[str, Any]:
    return {"schema": SCHEMA, "next_id": 1, "schedules": []}


def read_document(team: TeamPaths) -> Dict[str, Any]:
    """The raw ``schedules.json``; ``schedules_unreadable`` rather than ``{}`` for a broken edit.

    Treating an unparseable file as empty would let the next ``schedule add``
    overwrite everything the operator had, so a broken file stops every write.
    """
    path = definitions_path(team)
    raw = store.read_bytes(path)
    if raw is None or not raw.strip():
        return empty_document()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise HerdrTeamError("schedules_unreadable", "{} is not valid JSON ({}); fix or remove it".format(path, err), EXIT_REFUSED, {"path": os.fspath(path)})
    if not isinstance(doc, dict) or not isinstance(doc.get("schedules", []), list):
        raise HerdrTeamError("schedules_unreadable", "{} must be an object with a schedules list; fix or remove it".format(path), EXIT_REFUSED, {"path": os.fspath(path)})
    doc.setdefault("schema", SCHEMA)
    doc.setdefault("schedules", [])
    return doc


def write_document(team: TeamPaths, doc: Dict[str, Any]) -> None:
    """Indented, one key per line: this is the file the operator edits."""
    data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    store.atomic_write(definitions_path(team), data)


def load(team: TeamPaths) -> Tuple[List[Schedule], List[Dict[str, Any]]]:
    """``(valid schedules, invalid entries)``; an invalid entry is ``{"key", "error", "digest"}``."""
    doc = read_document(team)
    valid: List[Schedule] = []
    invalid: List[Dict[str, Any]] = []
    seen = set()
    for index, entry in enumerate(doc.get("schedules") or []):
        try:
            sched = Schedule.from_json(entry)
            # A copied entry keeps its id: two schedules sharing one id would share one state.
            for key in (sched.id, sched.name):
                if key is not None and key in seen:
                    raise _invalid("{} repeats the id or name {!r}; give it its own".format(sched.id, key), id=sched.id)
            seen.update(k for k in (sched.id, sched.name) if k is not None)
            valid.append(sched)
        except HerdrTeamError as err:
            key = entry.get("id") if isinstance(entry, dict) and isinstance(entry.get("id"), str) else "#{}".format(index + 1)
            digest = hashlib.sha1(json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]
            invalid.append({"key": key, "error": err.message, "digest": digest})
    return valid, invalid


def update_document(team: TeamPaths, mutate: Callable[[Dict[str, Any]], Any]) -> Any:
    """Read, mutate, write ``schedules.json`` under ``team.lock``; returns what ``mutate`` returned."""
    with store.team_lock(team):
        doc = read_document(team)
        result = mutate(doc)
        write_document(team, doc)
    return result


def allocate_id(doc: Dict[str, Any]) -> str:
    """``s<next_id>``, never reusing a removed id (a hand-written file without a counter is scanned)."""
    used = [int(e["id"][1:]) for e in doc.get("schedules") or [] if isinstance(e, dict) and isinstance(e.get("id"), str) and ID_RE.match(e["id"])]
    counter = doc.get("next_id") if isinstance(doc.get("next_id"), int) and not isinstance(doc.get("next_id"), bool) else 1
    number = max([counter] + [u + 1 for u in used])
    doc["next_id"] = number + 1
    return "s{}".format(number)


def find(schedules: Sequence[Schedule], ref: str) -> Optional[Schedule]:
    for sched in schedules:
        if sched.id == ref:
            return sched
    for sched in schedules:
        if sched.name == ref:
            return sched
    return None


def read_state(team: TeamPaths) -> Dict[str, Dict[str, Any]]:
    doc = store.read_json(state_path(team), default=None)
    entries = doc.get("schedules") if isinstance(doc, dict) else None
    return {k: dict(v) for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}


def update_state(team: TeamPaths, changes: Dict[str, Optional[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """Merge per-schedule changes into the runtime state under ``team.lock``; a None change drops the entry."""
    with store.team_lock(team):
        entries = read_state(team)
        for sid, change in changes.items():
            if change is None:
                entries.pop(sid, None)
            else:
                entries[sid] = dict(entries.get(sid) or {}, **change)
        store.write_json(state_path(team), {"schema": SCHEMA, "schedules": entries})
    return entries


# --------------------------------------------------------------------------
# prechecks


@dataclass
class PrecheckResult:
    status: str  # ok | nonzero | timeout | error
    exit_code: Optional[int] = None
    output: str = ""
    elapsed_s: float = 0.0

    def to_json(self) -> Dict[str, Any]:
        return {"status": self.status, "exit_code": self.exit_code, "output": self.output, "elapsed_s": round(self.elapsed_s, 3)}


def precheck_env(env: Optional[Dict[str, str]], team_name: str, sched: Schedule) -> Dict[str, str]:
    """A minimal environment: no Herdr identity, no socket, nothing the command could pass off as a pane."""
    source = env or {}
    out = {"PATH": source.get("PATH") or DEFAULT_PATH, "HOME": source.get("HOME") or os.path.expanduser("~"), "LANG": source.get("LANG") or "C"}
    out["HERDR_SYNAPSE_TEAM"] = team_name
    out["HERDR_SYNAPSE_SCHEDULE"] = sched.id
    return out


def precheck_cwd(layout: Layout, team_name: str) -> str:
    """The team's project directory when one is set and present, else the team directory."""
    from herdr_team import workdir as _workdir

    team = layout.team(team_name)
    doc = store.read_json(team.team_json, default={})
    project = _workdir.project_dir_of(doc if isinstance(doc, dict) else {})
    if project and os.path.isdir(project):
        return project
    return os.fspath(team.root)


def run_precheck(command: str, cwd: str, env: Dict[str, str], timeout_s: float) -> PrecheckResult:
    """``/bin/sh -c command`` with a deadline; on timeout its whole process group is killed.

    ``start_new_session`` puts the shell and anything it starts in one group,
    so a timed-out ``curl`` or ``sleep`` under the shell dies with it instead of
    outliving the check.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as err:
        return PrecheckResult("error", None, "cannot start /bin/sh: {}".format(err), time.monotonic() - started)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, _ = proc.communicate()
        return PrecheckResult("timeout", None, _tail(out), time.monotonic() - started)
    code = proc.returncode
    return PrecheckResult("ok" if code == 0 else "nonzero", code, _tail(out), time.monotonic() - started)


def _tail(data: Optional[bytes]) -> str:
    text = (data or b"").decode("utf-8", "replace").strip()
    return text[-PRECHECK_OUTPUT_CHARS:]


class PrecheckJob:
    """One precheck in a worker thread; the daemon polls ``done`` each tick and never joins."""

    def __init__(self, sched: Schedule, slot: Optional[datetime], fn: Callable[[], PrecheckResult], inline: bool = False) -> None:
        self.schedule_id = sched.id
        self.slot = slot
        self.result: Optional[PrecheckResult] = None
        self._fn = fn
        self._thread: Optional[threading.Thread] = None
        if inline:
            self._run()
        else:
            self._thread = threading.Thread(target=self._run, name="precheck-{}".format(sched.id), daemon=True)
            self._thread.start()

    def _run(self) -> None:
        try:
            self.result = self._fn()
        except Exception as err:  # noqa: BLE001 - a thread must always leave a result behind
            self.result = PrecheckResult("error", None, "{}: {}".format(type(err).__name__, err))

    @property
    def done(self) -> bool:
        return self.result is not None


# --------------------------------------------------------------------------
# firing


def schedule_author(sched: Schedule, slot: Optional[datetime], run_by: Optional[str] = None) -> "_identity.Author":
    """The operator, written by the notifier: the one author with ``via: schedule``.

    Never produced by author resolution, so it passes no authority gate; it
    only makes the stored record read as the operator's word (``record_human_ok``).
    """
    origin: Dict[str, Any] = {"via": _identity.VIA_SCHEDULE, "verified": True, "schedule": sched.id, "scheduled_by": sched.created_by or "human"}
    if slot is not None:
        origin["slot"] = iso_utc(slot)
    if run_by is not None:
        origin["manual"] = True
        origin["run_by"] = run_by
    return _identity.Author(_identity.AUTHOR_HUMAN, "human", _identity.VIA_SCHEDULE, True, origin=origin)


def post_text(sched: Schedule) -> str:
    """The text readers see: marked as scheduled, so nobody mistakes it for the operator typing now."""
    return "[scheduled {}] {}".format(sched.label, sched.text.strip())


def validate_recipients(doc: Dict[str, Any], targets: Sequence[str]) -> Tuple[List[str], Optional[str]]:
    """``post``'s own expansion, minus the targets a schedule cannot use (``human``, ``me``, another team)."""
    from herdr_team import cmd_board
    from herdr_team import links as _links

    entries = [part.strip() for arg in targets for part in str(arg).split(",") if part.strip()]
    for entry in entries:
        if entry in (_identity.AUTHOR_HUMAN, "me") or _links.is_team_recipient(entry):
            raise HerdrTeamError("schedule_recipient", "a schedule posts to members: a name, all, or role:<role> (not {!r})".format(entry), EXIT_REFUSED, {"recipient": entry})
    author = _identity.Author(_identity.AUTHOR_HUMAN, "human", _identity.VIA_SCHEDULE, True)
    return cmd_board.resolve_recipients(doc, entries, author)


def already_posted(team: TeamPaths, sched: Schedule, slot: datetime) -> Optional[int]:
    """The seq of a post already written for this slot (a restart between append and state write)."""
    wanted = iso_utc(slot)
    try:
        records = store.BoardStore(team).read(last=DEDUPE_LOOKBACK, include_retracted=True)
    except HerdrTeamError:
        return None
    for rec in reversed(records):
        marker = rec.get("schedule")
        if rec.get("kind") != "system" and isinstance(marker, dict) and marker.get("id") == sched.id and marker.get("slot") == wanted:
            return rec.get("seq") if isinstance(rec.get("seq"), int) else None
    return None


def _fire_post(layout: Layout, team_name: str, sched: Schedule, slot: Optional[datetime], run_by: Optional[str]) -> Dict[str, Any]:
    from herdr_team import cmd_board

    team = layout.team(team_name)
    doc = cmd_board.load_doc(team)
    to, to_role = validate_recipients(doc, sched.to)
    # force=True: the secret check ran when the operator wrote the text.
    text, _truncated, _body = cmd_board.prepare_text(post_text(sched), spill=False, force=True)
    record = cmd_board.build_record(schedule_author(sched, slot, run_by), to, sched.kind, text, to_role=to_role, socket_path=os.fspath(layout.socket))
    record["schedule"] = {"id": sched.id, "name": sched.name, "slot": iso_utc(slot) if slot is not None else None, "manual": run_by is not None}
    seq = cmd_board.board_append(team, record)
    return {"seq": seq, "to": to, "to_role": to_role}


def _fire_work(layout: Layout, team_name: str, sched: Schedule, slot: Optional[datetime], run_by: Optional[str]) -> Dict[str, Any]:
    """A work item per run: owned by the recipient when it names one member, open otherwise."""
    from herdr_team import cmd_board
    from herdr_team import work as _work

    team = layout.team(team_name)
    doc = cmd_board.load_doc(team)
    to, to_role = validate_recipients(doc, sched.to)
    owner = to[0] if len(to) == 1 and to[0] != "all" else None
    title = _work.clip(post_text(sched), _work.MAX_TITLE_CHARS)
    brief: Dict[str, str] = {}
    acceptance = (sched.action or {}).get("acceptance")
    if isinstance(acceptance, str) and acceptance.strip():
        brief["acceptance"] = acceptance.strip()
    _before, _after, event = _work.append(team, {"op": _work.OP_CREATE, "id": None, "by": _identity.AUTHOR_HUMAN, "by_via": _identity.VIA_SCHEDULE,
                                                "by_gen": 1, "title": title, "brief": brief, "owner": owner, "schedule": sched.id})
    work_id = event["id"]
    done_when = " Done when: {}.".format(_work.clip(brief["acceptance"], 200)) if brief.get("acceptance") else ""
    if owner:
        text = "{} for you: {}.{} Brief: herdr-synapse work show {}".format(work_id, title, done_when, work_id)
        recipients, kind = [owner], "request"
    else:
        text = "{} is open: {}.{} Take it with: herdr-synapse work claim {}".format(work_id, title, done_when, work_id)
        recipients, kind = to, "note"
    text, _truncated, _body = cmd_board.prepare_text(text, spill=False, force=True)
    record = cmd_board.build_record(schedule_author(sched, slot, run_by), recipients, kind, text, to_role=to_role, socket_path=os.fspath(layout.socket))
    record["schedule"] = {"id": sched.id, "name": sched.name, "slot": iso_utc(slot) if slot is not None else None, "manual": run_by is not None}
    record["work"] = {"id": work_id, "op": "assign" if owner else "open"}
    seq = cmd_board.board_append(team, record)
    return {"seq": seq, "to": recipients, "to_role": to_role, "work": work_id}


#: Action type -> handler(layout, team, schedule, slot or None, run_by or None) -> result.
ACTIONS: Dict[str, Callable[[Layout, str, Schedule, Optional[datetime], Optional[str]], Dict[str, Any]]] = {
    ACTION_POST: _fire_post,
    ACTION_WORK: _fire_work,
}


def fire(layout: Layout, team_name: str, sched: Schedule, slot: Optional[datetime], run_by: Optional[str] = None) -> Dict[str, Any]:
    """Run the schedule's action once; ``schedule_action_unknown`` for a type this version cannot run."""
    handler = ACTIONS.get(sched.action_type)
    if handler is None:
        raise HerdrTeamError("schedule_action_unknown", "{} has action {!r}; this version runs: {}".format(sched.display, sched.action_type, ", ".join(sorted(ACTIONS))), EXIT_REFUSED, {"id": sched.id})
    return handler(layout, team_name, sched, slot, run_by)


def _system_author() -> "_identity.Author":
    return _identity.Author(_identity.AUTHOR_SYSTEM, None, _identity.VIA_SYSTEM, True)


def _report(layout: Layout, team_name: str, event: str, text: str, extra: Dict[str, Any]) -> Optional[int]:
    """A ``schedule_failed`` / ``schedule_missed`` record to the operator (``schedule_failed`` also toasts)."""
    from herdr_team import roster as _roster

    try:
        return _roster.append_system_record(layout.team(team_name), event, text, to=["human"], extra={"schedule": extra}, socket=os.fspath(layout.socket))
    except HerdrTeamError:
        return None


def run_now(layout: Layout, team_name: str, sched: Schedule, run_by: str, env: Optional[Dict[str, str]] = None,
            precheck_fn: Callable[[str, str, Dict[str, str], float], PrecheckResult] = run_precheck) -> Dict[str, Any]:
    """``schedule run``: the precheck (synchronously) and the action, now, whatever the timetable says.

    A manual run never consumes a slot, so the next scheduled fire still
    happens; it is recorded separately as ``last_manual_*``.
    """
    team = layout.team(team_name)
    result: Dict[str, Any] = {"id": sched.id, "outcome": None, "seq": None, "precheck": None}
    if sched.precheck:
        check = precheck_fn(sched.precheck, precheck_cwd(layout, team_name), precheck_env(env, team_name, sched), float(sched.precheck_timeout_s))
        result["precheck"] = check.to_json()
        if check.status != "ok":
            result["outcome"] = "skipped" if check.status == "nonzero" else "failed"
    if result["outcome"] is None:
        fired = fire(layout, team_name, sched, None, run_by=run_by)
        result.update(outcome="posted", seq=fired.get("seq"), to=fired.get("to"))
    update_state(team, {sched.id: {"last_manual_at": iso_now(), "last_manual_outcome": result["outcome"], "last_manual_seq": result["seq"], "last_manual_by": run_by}})
    return result


class Runner:
    """One team's schedules inside the daemon: what is due, prechecks in flight, the firing.

    ``tick`` is cheap when nothing is due: the definitions are re-read at
    most every ``POLL_S``, and a schedule whose definition and anchor are
    unchanged is skipped on its cached next-due time. ``precheck_fn`` and
    ``inline_prechecks`` are the test seams.
    """

    def __init__(self, layout: Layout, team_name: str, env: Optional[Dict[str, str]] = None, log: Optional[Callable[[str], None]] = None) -> None:
        self.layout = layout
        self.team_name = team_name
        self.env = dict(env or {})
        self.log = log or (lambda _message: None)
        self.jobs: Dict[str, PrecheckJob] = {}
        self.next_due: Optional[float] = None
        self.last_eval: Optional[float] = None
        self.cache: Dict[str, Tuple[str, float]] = {}
        self.precheck_fn: Callable[[str, str, Dict[str, str], float], PrecheckResult] = run_precheck
        self.inline_prechecks = False

    @property
    def team(self) -> TeamPaths:
        return self.layout.team(self.team_name)

    def due(self, wall: float, mono: float) -> bool:
        if any(job.done for job in self.jobs.values()):
            return True
        if self.next_due is not None and wall >= self.next_due:
            return True
        return self.last_eval is None or mono - self.last_eval >= POLL_S

    def tick(self, wall: float, mono: float) -> None:
        if not self.due(wall, mono):
            return
        self.last_eval = mono
        self.evaluate(wall)

    # -- one evaluation --------------------------------------------------------

    def evaluate(self, wall: float) -> None:
        team = self.team
        if not definitions_path(team).exists() and not self.jobs:
            self.next_due = None
            return
        state = read_state(team)
        changes: Dict[str, Dict[str, Any]] = {}
        try:
            schedules, invalid = load(team)
        except HerdrTeamError as err:
            # Reported once per broken content, not once per poll.
            raw = store.read_bytes(definitions_path(team), default=b"") or b""
            self._report_invalid(state, changes, "__file__", err.message, hashlib.sha1(raw).hexdigest()[:16], wall)
            self._write(changes)
            return
        by_id = {s.id: s for s in schedules}
        for sid, job in list(self.jobs.items()):
            if job.done:
                del self.jobs[sid]
                sched = by_id.get(sid)
                if sched is None or not sched.enabled:
                    self.log("{}: schedule {} changed while its precheck ran; result dropped".format(self.team_name, sid))
                    continue
                changes[sid] = self._after_precheck(sched, job, wall)
        for bad in invalid:
            self._report_invalid(state, changes, bad["key"], bad["error"], bad["digest"], wall)
        upcoming: List[float] = []
        for sched in schedules:
            entry = dict(state.get(sched.id) or {}, **changes.get(sched.id, {}))
            change = self._evaluate_one(sched, entry, wall, upcoming)
            if change:
                changes[sched.id] = dict(changes.get(sched.id, {}), **change)
        self.next_due = min(upcoming) if upcoming else None
        self._write(changes)

    def _write(self, changes: Dict[str, Dict[str, Any]]) -> None:
        if not changes:
            return
        try:
            update_state(self.team, dict(changes))
        except HerdrTeamError as err:
            self.log("{}: schedule state not written: {}".format(self.team_name, err))

    def _anchor(self, sched: Schedule, entry: Dict[str, Any]) -> Optional[float]:
        marks = [parse_time(entry.get("last_slot")), parse_time(entry.get("armed_at")), parse_time(sched.created_at)]
        found = [m for m in marks if m is not None]
        return max(found) if found else None

    def _evaluate_one(self, sched: Schedule, entry: Dict[str, Any], wall: float, upcoming: List[float]) -> Dict[str, Any]:
        if not sched.enabled:
            # Remembered so re-enabling by hand arms the schedule then, instead of
            # reporting every slot of the disabled stretch as missed.
            return {} if entry.get("disabled_seen") else {"disabled_seen": True}
        change: Dict[str, Any] = {}
        if entry.get("disabled_seen"):
            change.update(disabled_seen=False, armed_at=iso_now(wall))
            entry = dict(entry, **change)
        if sched.id in self.jobs:
            return change
        if sched.action_type not in ACTIONS:
            key = "action:{}".format(sched.digest())
            if entry.get("invalid") != key:
                change["invalid"] = key
                _report(self.layout, self.team_name, "schedule_failed", "schedule {} has action {!r}, which this version cannot run; it will not fire".format(sched.display, sched.action_type), {"id": sched.id})
            return change
        if entry.get("invalid"):
            change["invalid"] = None  # fixed since it was reported
        anchor = self._anchor(sched, entry)
        if anchor is None:
            # First sight of a hand-written entry: it starts now, it does not owe past slots.
            change["armed_at"] = iso_now(wall)
            anchor = parse_time(change["armed_at"]) or wall
        try:
            spec = sched.spec()
            label, zone = zone_of(sched.tz, self.env)
        except HerdrTeamError as err:
            change.pop("invalid", None)
            return dict(change, **self._invalid_change(entry, sched.id, err.message, sched.digest()))
        cached = self.cache.get(sched.id)
        if cached is not None and cached[0] == self._cache_key(sched, label, anchor) and wall < cached[1]:
            upcoming.append(cached[1])
            return change
        slot = previous_fire(spec, wall, tz=zone, not_before=anchor)
        if slot is not None:
            late = wall - slot.timestamp()
            if late <= max(float(sched.grace_s), ON_TIME_S):
                change.update(self._start(sched, slot, wall))
            else:
                change.update(self._missed(sched, slot, late, label, wall))
            if "last_slot" not in change:
                # A precheck is running (collected when it finishes) or a locked board
                # deferred the post (retried at the next tick, while the grace lasts).
                self.cache.pop(sched.id, None)
                if sched.id not in self.jobs:
                    upcoming.append(wall)
                return change
            anchor = slot.timestamp()
        nxt = next_fires(spec, max(anchor, wall), 1, tz=zone)
        due = nxt[0].timestamp() if nxt else float("inf")
        next_due = iso_utc(nxt[0]) if nxt else None
        if entry.get("next_due") != next_due:
            change["next_due"] = next_due
        self.cache[sched.id] = (self._cache_key(sched, label, anchor), due)
        if nxt:
            upcoming.append(due)
        return change

    @staticmethod
    def _cache_key(sched: Schedule, label: str, anchor: float) -> str:
        return "{}|{}|{:.3f}".format(sched.digest(), label, anchor)

    def _start(self, sched: Schedule, slot: datetime, wall: float) -> Dict[str, Any]:
        seq = already_posted(self.team, sched, slot)
        if seq is not None:
            self.log("{}: schedule {} slot {} was already posted as #{}; state caught up".format(self.team_name, sched.id, iso_utc(slot), seq))
            return self._outcome(sched, slot, wall, "posted", seq=seq)
        if sched.precheck:
            command, cwd, env, timeout = sched.precheck, precheck_cwd(self.layout, self.team_name), precheck_env(self.env, self.team_name, sched), float(sched.precheck_timeout_s)
            fn = self.precheck_fn
            self.jobs[sched.id] = PrecheckJob(sched, slot, lambda: fn(command, cwd, env, timeout), inline=self.inline_prechecks)
            self.log("{}: schedule {} due at {}; precheck started".format(self.team_name, sched.id, iso_utc(slot)))
            return {"running": {"slot": iso_utc(slot), "started_at": iso_now(wall)}}
        return self._post(sched, slot, wall)

    def _post(self, sched: Schedule, slot: datetime, wall: float) -> Dict[str, Any]:
        try:
            fired = fire(self.layout, self.team_name, sched, slot)
        except HerdrTeamError as err:
            if err.code in ("board_locked", "lock_timeout"):
                self.log("{}: schedule {} post deferred: {}".format(self.team_name, sched.id, err))
                return {}  # no last_slot: retried next tick while the grace lasts
            _report(self.layout, self.team_name, "schedule_failed", "scheduled post {} for {} did not go out: {}".format(sched.display, iso_utc(slot), err.message),
                    {"id": sched.id, "slot": iso_utc(slot), "code": err.code})
            return self._outcome(sched, slot, wall, "failed", detail="{}: {}".format(err.code, err.message))
        return self._outcome(sched, slot, wall, "posted", seq=fired.get("seq"))

    def _after_precheck(self, sched: Schedule, job: PrecheckJob, wall: float) -> Dict[str, Any]:
        result = job.result or PrecheckResult("error", None, "no result")
        slot = job.slot
        assert slot is not None
        base: Dict[str, Any] = {"running": None, "last_precheck": result.to_json()}
        if result.status == "ok":
            seq = already_posted(self.team, sched, slot)
            return dict(base, **(self._outcome(sched, slot, wall, "posted", seq=seq) if seq is not None else self._post(sched, slot, wall)))
        if result.status == "nonzero":
            # The check said "nothing to do this time": no post, no toast, just the record of it.
            return dict(base, **self._outcome(sched, slot, wall, "skipped", detail="precheck exited {}".format(result.exit_code)))
        why = "precheck timed out after {}".format(format_duration(sched.precheck_timeout_s)) if result.status == "timeout" else "precheck could not run: {}".format(result.output)
        _report(self.layout, self.team_name, "schedule_failed", "scheduled post {} for {} did not go out: {}".format(sched.display, iso_utc(slot), why),
                {"id": sched.id, "slot": iso_utc(slot), "precheck": result.status})
        return dict(base, **self._outcome(sched, slot, wall, "failed", detail=why))

    def _missed(self, sched: Schedule, slot: datetime, late: float, label: str, wall: float) -> Dict[str, Any]:
        text = "schedule {} was due {} ({}) while the notifier was not running; skipped, {} late against a grace of {}".format(
            sched.display, human_time(slot), label, format_duration(int(late)), format_duration(sched.grace_s))
        _report(self.layout, self.team_name, "schedule_missed", text, {"id": sched.id, "slot": iso_utc(slot), "late_s": int(late)})
        return self._outcome(sched, slot, wall, "missed", detail="{} late".format(format_duration(int(late))))

    def _outcome(self, sched: Schedule, slot: datetime, wall: float, outcome: str, seq: Optional[int] = None, detail: Optional[str] = None) -> Dict[str, Any]:
        _identity.audit(self.layout, self.team_name, "schedule_fire", _system_author(), {"id": sched.id, "slot": iso_utc(slot), "outcome": outcome, "seq": seq, "detail": detail})
        self.log("{}: schedule {} slot {}: {}{}".format(self.team_name, sched.id, iso_utc(slot), outcome, " #{}".format(seq) if seq else ""))
        change: Dict[str, Any] = {"last_slot": iso_utc(slot), "last_outcome": outcome, "last_run_at": iso_now(wall), "last_seq": seq, "last_detail": detail}
        return change

    def _invalid_change(self, entry: Dict[str, Any], key: str, message: str, digest: str) -> Dict[str, Any]:
        mark = "{}:{}".format(key, digest)
        if entry.get("invalid") == mark:
            return {}
        _report(self.layout, self.team_name, "schedule_failed", "schedules.json: {} is invalid and will not fire until it is fixed: {}".format(key, message), {"id": key})
        self.log("{}: schedule {} invalid: {}".format(self.team_name, key, message))
        return {"invalid": mark}

    def _report_invalid(self, state: Dict[str, Dict[str, Any]], changes: Dict[str, Dict[str, Any]], key: str, message: str, digest: str, wall: float) -> None:
        # Kept apart from the schedules' own state ("!" is never in an id), so an
        # invalid copy of s1 and the valid s1 do not keep clearing each other's mark.
        slot = "!" + key
        entry = dict(state.get(slot) or {}, **changes.get(slot, {}))
        change = self._invalid_change(entry, key, message, digest)
        if change:
            changes[slot] = dict(changes.get(slot, {}), **change)


def upcoming(schedules: Sequence[Schedule], after: float, count: int, env: Optional[Dict[str, str]] = None) -> List[Tuple[datetime, Schedule, str]]:
    """The next ``count`` fires across ``schedules`` (enabled ones only), soonest first: ``(when, schedule, zone label)``."""
    rows: List[Tuple[datetime, Schedule, str]] = []
    for sched in schedules:
        if not sched.enabled:
            continue
        try:
            label, zone = zone_of(sched.tz, env)
            fires = next_fires(sched.spec(), after, count, tz=zone)
        except HerdrTeamError:
            continue
        rows.extend((when, sched, label) for when in fires)
    rows.sort(key=lambda row: (row[0].timestamp(), row[1].id))
    return rows[:count]


def summary(team: TeamPaths, env: Optional[Dict[str, str]] = None, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """What ``who`` shows: ``{count, enabled, next}``, or None for a team without schedules."""
    if not definitions_path(team).exists():
        return None
    try:
        schedules, invalid = load(team)
    except HerdrTeamError as err:
        return {"count": None, "enabled": None, "next": None, "error": err.message}
    if not schedules and not invalid:
        return None
    rows = upcoming(schedules, _now() if now is None else now, 1, env)
    nxt = {"id": rows[0][1].id, "name": rows[0][1].name, "at": rows[0][0].isoformat(), "zone": rows[0][2]} if rows else None
    return {"count": len(schedules) + len(invalid), "enabled": sum(1 for s in schedules if s.enabled), "invalid": len(invalid), "next": nxt}


def summary_line(info: Dict[str, Any]) -> str:
    """One line for ``who``: ``schedules: 2 of 3 on; next s1 (brand-mentions) Thu 2026-09-24 09:00 CEST``."""
    if info.get("error"):
        return "schedules: unreadable ({})".format(info["error"])
    line = "schedules: {} of {} on".format(info.get("enabled"), info.get("count"))
    if info.get("invalid"):
        line += ", {} invalid".format(info["invalid"])
    nxt = info.get("next")
    if nxt:
        when = datetime.fromisoformat(nxt["at"])
        line += "; next {}{} {}".format(nxt["id"], " ({})".format(nxt["name"]) if nxt.get("name") else "", when.strftime("%a %Y-%m-%d %H:%M"))
    return line

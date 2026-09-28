"""Number and date formats for charts (canvas v2 phase 3, 2.3): one deterministic, locale-free formatter.

``fmt_number(v, fmt, unit)`` writes every number a chart shows: axis ticks,
value labels, legends and the gist. The page never formats numbers on its own:
a value axis in an ECharts option carries ``{"$fmt": "<fmt id>"}`` and the
page's ``charts/format.js`` maps that id to the same function, held to this one
by ``tests/fixtures/charts/format-vectors.json`` (``vectors()``). So both sides
round the same way: half away from zero on ``floor(|v| * 10^d + 0.5)``, never
Python's round-half-even or ``toFixed``'s decimal ties.

Formats: ``auto`` (the default: compact from a million up, else grouped with up
to 2 decimals), ``compact`` (``4.2M``, ``12.5K``), ``integer`` (``1,234``),
``percent`` (a fraction: ``0.125`` is ``12.5%``), a fixed decimal pattern
(``0``, ``0.0``, ``0.00``, ``0.000``), and for day numbers (days since
1970-01-01) ``date`` (``2026-09-04``), ``month`` (``Sep 2026``) and ``year``.
A unit in ``$ £ € ¥`` is a prefix, ``%`` a suffix without a space, anything
else a suffix after a space (``412 ms``). Negative numbers use the minus sign
U+2212.

Pure, stdlib only.
"""
from __future__ import annotations

import datetime
import math
import re
from typing import Any, List, Optional, Tuple

MINUS = "−"
CURRENCIES = ("$", "£", "€", "¥")
FORMATS = ("auto", "compact", "integer", "percent", "0", "0.0", "0.00", "0.000", "date", "month", "year")
#: Formats of temporal categories (labels, formatted in Python only).
DATE_FORMATS = ("date", "month", "year", "quarter")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UNIT = re.compile(r"^[^\s|:{}<>\"'`\\]{1,8}\Z")
_SCALES = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))


def with_unit(title: str, unit: Optional[str]) -> str:
    """An axis title with its unit: ``p95 (ms)``. A currency is the ticks' prefix, not a suffix; a title that already
    names its unit (a column called ``ms``, ``latency (ms)``, ``latency ms``) gets no second one (QA phase34 L1)."""
    if not unit or unit in CURRENCIES:
        return title
    low, u = title.strip().lower(), str(unit).strip().lower()
    if low == u or low.endswith("({})".format(u)) or low.endswith(" " + u) or low.endswith("_" + u):
        return title
    return "{} ({})".format(title, unit)


def valid_unit(unit: Any) -> bool:
    return isinstance(unit, str) and bool(_UNIT.match(unit))


def _rounded(value: float, decimals: int) -> int:
    """``|value|`` scaled by ``10^decimals`` and rounded half away from zero (the page does the same float operations)."""
    return int(math.floor(abs(value) * (10 ** decimals) + 0.5))


def _digits(scaled: int, decimals: int, group: bool) -> str:
    """The digits of ``scaled / 10^decimals``: grouped thousands, trailing zeros kept."""
    whole, frac = divmod(scaled, 10 ** decimals) if decimals else (scaled, 0)
    text = "{:,}".format(whole) if group else str(whole)
    if decimals:
        text += "." + str(frac).rjust(decimals, "0")
    return text


def _trim(text: str) -> str:
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _plain(value: float, max_decimals: int = 2) -> str:
    """Grouped, up to ``max_decimals`` decimals (3 significant digits below 1), trailing zeros trimmed; no sign."""
    a = abs(value)
    decimals = max_decimals
    if 0 < a < 1:
        decimals = min(6, max(max_decimals, 2 - int(math.floor(math.log10(a)))))
    return _trim(_digits(_rounded(a, decimals), decimals, True))


def _compact(value: float) -> str:
    a = abs(value)
    for index, (scale, suffix) in enumerate(_SCALES):
        if a >= scale * 0.9995:
            s = a / scale
            decimals = 1 if s < 100 else 0
            scaled = _rounded(s, decimals)
            if scaled >= 1000 * (10 ** decimals) and index > 0:
                scale, suffix = _SCALES[index - 1]
                s = a / scale
                decimals = 1
                scaled = _rounded(s, decimals)
            return _trim(_digits(scaled, decimals, True)) + suffix
    return _plain(a)


_EPOCH = datetime.date(1970, 1, 1).toordinal()


def _civil(days: float) -> Tuple[int, int, int]:
    """``(year, month, day)`` of a day number (days since 1970-01-01, floored), by Hinnant's civil-from-days (the page
    uses the same integer algorithm)."""
    z = int(math.floor(days)) + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y), m, d


def fmt_number(value: Any, fmt: Optional[str] = None, unit: Optional[str] = None) -> str:
    """``value`` as a chart writes it (see the module doc); a null or junk value is an empty string."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return ""
    v = float(value)
    fmt = fmt if fmt in FORMATS else "auto"
    unit = unit if valid_unit(unit) else ""
    if fmt in ("date", "month", "year"):
        y, m, d = _civil(v)
        body = "{:04d}-{:02d}-{:02d}".format(y, m, d) if fmt == "date" else ("{} {:04d}".format(MONTHS[m - 1], y) if fmt == "month" else "{:04d}".format(y))
        return body
    negative = v < 0
    if fmt == "percent":
        scaled = _rounded(v * 100.0, 1)
        body = _trim(_digits(scaled, 1, True)) + "%"
        negative = negative and scaled != 0
    elif fmt == "integer":
        scaled = _rounded(v, 0)
        body = _digits(scaled, 0, True)
        negative = negative and scaled != 0
    elif fmt in ("0", "0.0", "0.00", "0.000"):
        decimals = len(fmt) - 2 if "." in fmt else 0
        scaled = _rounded(v, decimals)
        body = _digits(scaled, decimals, True)
        negative = negative and scaled != 0
    elif fmt == "compact" or (fmt == "auto" and abs(v) >= 1e6):
        body = _compact(v)
        negative = negative and body.strip("0.") != ""
    else:
        body = _plain(v)
        negative = negative and body.strip("0.,") != ""
    sign = MINUS if negative else ""
    if unit in CURRENCIES:
        return sign + unit + body
    if unit == "%" and fmt != "percent":
        return sign + body + "%"
    if unit and fmt != "percent":
        return sign + body + " " + unit
    return sign + body


def signed(value: float, fmt: Optional[str] = None, unit: Optional[str] = None) -> str:
    """``+4.2M`` or ``−4.2M``: a change, always with its sign."""
    text = fmt_number(value, fmt, unit)
    return text if text.startswith(MINUS) or not text or value == 0 else "+" + text


def percent_change(first: float, last: float) -> Optional[float]:
    """``(last - first) / |first|``, or None when ``first`` is 0."""
    if first == 0:
        return None
    return (last - first) / abs(first)


def pct(value: Optional[float]) -> str:
    """A signed percentage for a change or a share (``+38%``, ``−2%``, ``12.5%``): whole numbers from 10 % up."""
    if value is None:
        return "n/a"
    p = value * 100.0
    decimals = 0 if abs(p) >= 10 else 1
    scaled = _rounded(p, decimals)
    body = _trim(_digits(scaled, decimals, True))
    if scaled == 0:
        return "0%"
    return (MINUS if p < 0 else "+") + body + "%"


def share(value: float) -> str:
    """A share of a whole, no sign: ``42%``, ``3.5%``."""
    return pct(value).lstrip("+")


def label(value: Any, fmt: Optional[str], unit: Optional[str], kind: str) -> str:
    """A category value as a label: a temporal period in a date format (``month``: ``Jan 2026``), a number in its format,
    else the text as it is."""
    if value is None:
        return "(null)"
    if kind == "temporal" and isinstance(value, str) and fmt in DATE_FORMATS:
        return temporal_label(value, fmt)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return fmt_number(value, fmt, unit)
    return str(value)


_PERIOD = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


def temporal_label(text: str, fmt: str) -> str:
    """A canonical period in a date format: ``2026-01`` as ``month`` is ``Jan 2026``, as ``year`` ``2026``."""
    match = _PERIOD.match(text)
    if match is None:
        return text
    year, month, day = match.group(1), match.group(2), match.group(3)
    if fmt == "year":
        return year
    if fmt == "month" and month:
        return "{} {}".format(MONTHS[int(month) - 1], year)
    if fmt == "quarter" and month:
        return "{}-Q{}".format(year, (int(month) - 1) // 3 + 1)
    if fmt == "date" and month and day:
        return "{}-{}-{}".format(year, month, day)
    return text


def fmt_id(fmt: Optional[str], unit: Optional[str]) -> str:
    """The ``$fmt`` id a value axis carries: ``<format>|<unit>`` (``compact|$``, ``auto|ms``, ``percent|``)."""
    return "{}|{}".format(fmt if fmt in FORMATS else "auto", unit if valid_unit(unit) else "")


def parse_fmt_id(ident: str) -> Tuple[str, str]:
    fmt, _bar, unit = str(ident).partition("|")
    return (fmt if fmt in FORMATS else "auto"), (unit if valid_unit(unit) else "")


def vectors() -> List[dict]:
    """``tests/fixtures/charts/format-vectors.json``: inputs and the exact strings both formatters must give."""
    values = [0, 1, -1, 0.5, -0.5, 0.125, 0.004, 0.0123, 1.005, 2.675, 12.345, 99.995, 999.5, 1000, 1234.5, 9999.95, 12500, 99950,
              999950, 1e6, 1234567, -4200000, 61800000, 999950000, 1.5e9, 3.25e12, 7e15, 0.1 + 0.2, 1e-7, 42, 412, -147, 2026, 20361,
              20704.75]
    cases = []
    for fmt in FORMATS:
        for unit in ("", "$", "%", "ms"):
            if fmt in ("date", "month", "year") and unit:
                continue
            for value in values:
                cases.append({"v": value, "fmt": fmt, "unit": unit, "out": fmt_number(value, fmt, unit)})
    cases.extend({"v": value, "fmt": "auto", "unit": "", "out": fmt_number(value)} for value in (None, "12", True))
    return cases

"""Chart data (canvas v2 phase 3, 2.2): tables from files or inline rows, typed deterministically, filtered, aggregated.

A ``Table`` is column-major: every ``Column`` holds its values already typed
(``quantitative`` floats, ``temporal`` canonical strings with a sortable day
key, ``nominal`` and ``ordinal`` strings; ``None`` for a null). Typing is a
pure function of the values, so the same file always types the same way, on
3.9 and 3.14 alike. Every refusal names its field and, when it can, the repair
(the verdict's "did you mean" loop): a missing column lists the columns with
their types, a wrong type says which type the channel takes.

Also here, because every chart type needs them: filters, ``aggregate``,
``top_other``, ``bin_numeric`` (Freedman-Diaconis, 5 to 60 bins), ``lttb``
(Largest-Triangle-Three-Buckets downsampling), ``quantiles``, and the
refusal helpers the whole package raises through (the canvas's own codes).

Pure: no I/O (the caller reads the bytes through ``FetchIO``), stdlib only.
"""
from __future__ import annotations

import csv
import datetime
import difflib
import hashlib
import io
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from herdr_team.errors import HerdrTeamError, exit_code_for

TYPES = ("quantitative", "temporal", "ordinal", "nominal")
#: A data file is at most this big and this long (the page never parses it: Python does, once).
MAX_CHART_DATA_BYTES = 5 * 1024 * 1024
MAX_DATA_ROWS = 100_000
#: Inline ``rows`` live on the element, so they stay small.
MAX_INLINE_ROWS = 500
MAX_INLINE_BYTES = 32 * 1024
MAX_COLUMNS = 64
#: The text a nominal value keeps (a longer one is cut: it is a label, not a document).
MAX_VALUE_CHARS = 200
DATA_SUFFIXES = (".csv", ".tsv", ".json")
NULLS = frozenset(("", "null", "na", "n/a", "-", "none", "nan"))
CURRENCIES = ("$", "£", "€", "¥")
_EPOCH = datetime.date(1970, 1, 1).toordinal()
_NUMERIC = re.compile(r"^\s*([+-]?)\s*([$£€¥]?)\s*([+-]?)((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d+))?\s*([kKmMbBtT%]?)\s*$")
_SCALE = {"": 1.0, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12, "%": 1.0}
_YEAR = re.compile(r"^(\d{4})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$")
_WEEK = re.compile(r"^(\d{4})-?W(\d{2})$")
_DATETIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?\s*(Z|[+-]\d{2}:?\d{2})?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
#: Temporal grains, finest last; a column of one grain is drawn as that many regular periods.
GRAINS = ("year", "quarter", "month", "week", "day", "datetime")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------------
# refusals (the canvas's codes: ``op_invalid`` with ``field``, ``canvas_limit``, ``chart_refused``, ``chart_empty``)


def invalid(field: str, message: str, **details: Any) -> HerdrTeamError:
    return HerdrTeamError("op_invalid", message, exit_code_for("op_invalid"), dict(details, field=field))


def refuse(code: str, message: str, **details: Any) -> HerdrTeamError:
    return HerdrTeamError(code, message, exit_code_for(code), details)


def too_big(field: str, limit: str, maximum: int, message: str) -> HerdrTeamError:
    return HerdrTeamError("canvas_limit", message, exit_code_for("canvas_limit"), {"field": field, "limit": limit, "max": maximum})


def nearest(word: str, choices: Sequence[str]) -> Optional[str]:
    """The closest of ``choices`` to ``word``: an exact case-insensitive match first, then ``difflib``'s best."""
    lowered = {c.lower(): c for c in choices}
    if str(word).lower() in lowered:
        return lowered[str(word).lower()]
    found = difflib.get_close_matches(str(word).lower(), list(lowered), n=1, cutoff=0.6)
    if found:
        return lowered[found[0]]
    # "barchart", "line_chart": the longest choice the word starts with or holds.
    held = sorted((c for c in lowered if len(c) >= 3 and c in str(word).lower()), key=lambda c: (-len(c), c))
    return lowered[held[0]] if held else None


def did_you_mean(word: str, choices: Sequence[str]) -> str:
    found = nearest(word, choices)
    return " (did you mean {}?)".format(json.dumps(found, ensure_ascii=False)) if found and found != word else ""


# --------------------------------------------------------------------------
# values


def is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and not math.isfinite(value):
        return True
    return isinstance(value, str) and value.strip().lower() in NULLS


def number(value: Any) -> Optional[Tuple[float, str]]:
    """``(number, unit)`` of a numeric value (``1,234.5``, ``12%``, ``$3.2M``: unit ``%`` or a currency), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        found = float(value)
        return (found, "") if math.isfinite(found) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        found = float(text)
    except ValueError:
        match = _NUMERIC.match(text)
        if match is None:
            return None
        sign, currency, sign2, digits, exponent, suffix = match.groups()
        try:
            found = float(digits.replace(",", "")) * (10.0 ** int(exponent) if exponent else 1.0) * _SCALE[suffix.lower()]
        except (ValueError, OverflowError):
            return None
        if (sign == "-") != (sign2 == "-"):
            found = -found
        unit = "%" if suffix == "%" else currency
        return (found, unit) if math.isfinite(found) else None
    if not math.isfinite(found) or text.lower() in ("nan", "inf", "-inf", "+inf", "infinity", "-infinity"):
        return None
    return found, ""


def _days(year: int, month: int, day: int) -> Optional[float]:
    try:
        return float(datetime.date(year, month, day).toordinal() - _EPOCH)
    except ValueError:
        return None


def temporal(value: Any) -> Optional[Tuple[str, float, str]]:
    """``(canonical, day key, grain)`` of a date-like value (``2026``, ``2026-01``, ``2026-01-31``, an ISO datetime,
    ``2026-Q3``, ``2026-W05``), else None. The key is days since 1970-01-01 (a datetime's with its fraction)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 4 or not text[:4].isdigit():
        return None
    match = _DAY.match(text)
    if match:
        y, m, d = (int(g) for g in match.groups())
        key = _days(y, m, d)
        return (text, key, "day") if key is not None else None
    match = _MONTH.match(text)
    if match:
        y, m = int(match.group(1)), int(match.group(2))
        key = _days(y, m, 1)
        return (text, key, "month") if key is not None else None
    match = _YEAR.match(text)
    if match:
        y = int(match.group(1))
        key = _days(y, 1, 1) if 1 <= y <= 9999 else None
        return (text, key, "year") if key is not None else None
    match = _QUARTER.match(text)
    if match:
        y, q = int(match.group(1)), int(match.group(2))
        key = _days(y, 3 * q - 2, 1)
        return ("{}-Q{}".format(y, q), key, "quarter") if key is not None else None
    match = _WEEK.match(text)
    if match:
        y, w = int(match.group(1)), int(match.group(2))
        try:
            key = float(datetime.date.fromisocalendar(y, w, 1).toordinal() - _EPOCH) if hasattr(datetime.date, "fromisocalendar") \
                else _iso_week(y, w)
        except ValueError:
            return None
        return ("{}-W{:02d}".format(y, w), key, "week") if key is not None else None
    match = _DATETIME.match(text)
    if match:
        y, mo, d, hh, mm = (int(g) for g in match.groups()[:5])
        ss = int(match.group(6) or 0)
        if hh > 23 or mm > 59 or ss > 60:
            return None
        base = _days(y, mo, d)
        if base is None:
            return None
        return text, base + (hh * 3600 + mm * 60 + min(ss, 59)) / 86400.0, "datetime"
    return None


def _iso_week(year: int, week: int) -> Optional[float]:
    """Monday of ISO week ``week`` of ``year`` (3.7-compatible fallback of ``date.fromisocalendar``)."""
    if not 1 <= week <= 53:
        return None
    jan4 = datetime.date(year, 1, 4)
    monday = jan4 - datetime.timedelta(days=jan4.isoweekday() - 1) + datetime.timedelta(weeks=week - 1)
    if week == 53 and monday.isocalendar()[0] != year:
        return None
    return float(monday.toordinal() - _EPOCH)


def period_label(key: float, grain: str) -> str:
    """The canonical text of the period starting on day ``key`` (``2026-03``, ``2026-Q2``)."""
    day = datetime.date.fromordinal(int(math.floor(key)) + _EPOCH)
    if grain == "year":
        return "{:04d}".format(day.year)
    if grain == "quarter":
        return "{:04d}-Q{}".format(day.year, (day.month - 1) // 3 + 1)
    if grain == "month":
        return "{:04d}-{:02d}".format(day.year, day.month)
    if grain == "week":
        iso = day.isocalendar()
        return "{:04d}-W{:02d}".format(iso[0], iso[1])
    return day.isoformat()


def next_period(key: float, grain: str) -> float:
    day = datetime.date.fromordinal(int(math.floor(key)) + _EPOCH)
    if grain == "year":
        nxt = datetime.date(day.year + 1, 1, 1)
    elif grain in ("quarter", "month"):
        step = 3 if grain == "quarter" else 1
        month = day.month - 1 + step
        nxt = datetime.date(day.year + month // 12, month % 12 + 1, 1)
    elif grain == "week":
        nxt = day + datetime.timedelta(days=7)
    else:
        nxt = day + datetime.timedelta(days=1)
    return float(nxt.toordinal() - _EPOCH)


def _clean(text: str) -> str:
    text = _CONTROL.sub(" ", text).strip()
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS - 1] + "…"


def as_label(value: Any) -> Optional[str]:
    """A value as the text a category shows (a number without a trailing .0, a bool in words), None for a null."""
    if is_null(value):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return _clean(str(value))


# --------------------------------------------------------------------------
# columns and tables


class Column:
    """One typed column: ``values`` (floats, strings or None), temporal ``keys``, nulls, distinct count, a detected unit
    (``$``, ``%``) and a temporal grain (``year`` ... ``datetime``, or ``mixed``)."""

    __slots__ = ("name", "type", "values", "keys", "nulls", "distinct", "unit", "grain", "declared")

    def __init__(self, name: str, kind: str, values: List[Any], keys: Optional[List[Optional[float]]] = None, unit: str = "",
                 grain: str = "", declared: bool = False) -> None:
        self.name = name
        self.type = kind
        self.values = values
        self.keys = keys
        self.nulls = sum(1 for v in values if v is None)
        self.distinct = len({v for v in values if v is not None})
        self.unit = unit
        self.grain = grain
        self.declared = declared

    def describe(self) -> str:
        """``month (temporal, 12 values)``, ``region (nominal, 3)``, ``revenue (quantitative)``."""
        if self.type == "quantitative":
            return "{} (quantitative)".format(self.name)
        return "{} ({}, {}{})".format(self.name, self.type, self.distinct, " values" if self.type == "temporal" else "")

    def sample(self, n: int = 3) -> List[str]:
        seen: List[str] = []
        for value in self.values:
            if value is not None and str(value) not in seen:
                seen.append(str(value))
                if len(seen) >= n:
                    break
        return seen


class Table:
    """A typed, column-major table: ``columns`` in file order, ``rows`` data rows, the ``label`` it is named by in
    messages (``rev.csv``, ``inline rows``), the ``sha256`` of its source and parse ``notes`` (ragged rows padded)."""

    def __init__(self, columns: List[Column], rows: int, label: str, sha256: str, notes: Sequence[str] = ()) -> None:
        self.columns = columns
        self.by_name = {c.name: c for c in columns}
        self.rows = rows
        self.label = label
        self.sha256 = sha256
        self.notes = list(notes)

    def names(self) -> List[str]:
        return [c.name for c in self.columns]

    def columns_text(self) -> str:
        return ", ".join(c.describe() for c in self.columns)

    def col(self, name: str, field: str) -> Column:
        """The column a channel names, or the repair refusal: the columns with their types, and the nearest name."""
        found = self.by_name.get(name)
        if found is not None:
            return found
        close = nearest(name, self.names())
        raise invalid(field, 'field {} is not a column of {}; columns: {}.{}'.format(
            json.dumps(name, ensure_ascii=False), self.label, self.columns_text(),
            " Did you mean {}?".format(json.dumps(close, ensure_ascii=False)) if close else ""),
            columns=self.names(), nearest=close)

    def schema(self) -> List[List[str]]:
        return [[c.name, c.type] for c in self.columns]


def _type_column(name: str, raw: List[Any], from_text: bool, declared: Optional[str] = None) -> Column:
    """Type one column (2.2): quantitative when every non-null value is numeric, temporal when every one is a date (a
    text column of four-digit years too), else nominal; ``ordinal`` only when declared. A declared type is checked."""
    present = [v for v in raw if not is_null(v)]
    kind = declared
    if kind is None:
        kind = "nominal"
        if present:
            if all(isinstance(v, str) and _YEAR.match(v.strip()) and 1000 <= int(v.strip()) <= 2999 for v in present):
                # A column of four-digit years as text is time (a JSON number 2026 stays a number).
                kind = "temporal"
            elif all(number(v) is not None for v in present):
                kind = "quantitative"
            elif all(temporal(v) is not None for v in present):
                kind = "temporal"
    if kind == "quantitative":
        values: List[Any] = []
        units = set()
        for v in raw:
            if is_null(v):
                values.append(None)
                continue
            found = number(v)
            if found is None:
                raise invalid("types." + name, "{} is declared quantitative, but {} is not a number".format(name, json.dumps(str(v)[:40])))
            values.append(found[0])
            units.add(found[1])
        unit = units.pop() if len(units) == 1 else ""
        return Column(name, kind, values, unit=unit, declared=declared is not None)
    if kind == "temporal":
        values, keys, grains = [], [], set()
        for v in raw:
            if is_null(v):
                values.append(None)
                keys.append(None)
                continue
            found = temporal(v if isinstance(v, str) else as_label(v))
            if found is None:
                raise invalid("types." + name, "{} is declared temporal, but {} is not a date (YYYY, YYYY-MM, YYYY-MM-DD, an ISO "
                              "datetime, YYYY-Qn or YYYY-Www)".format(name, json.dumps(str(v)[:40])))
            values.append(found[0])
            keys.append(found[1])
            grains.add(found[2])
        grain = grains.pop() if len(grains) == 1 else ("mixed" if grains else "")
        return Column(name, kind, values, keys=keys, grain=grain, declared=declared is not None)
    values = [as_label(v) for v in raw]
    return Column(name, kind, values, declared=declared is not None)


def _check_header(header: List[Any], label: str) -> List[str]:
    names: List[str] = []
    for index, raw in enumerate(header):
        name = _clean(str(raw)) if raw is not None else ""
        if not name:
            raise refuse("chart_refused", "{}: column {} has no name in the header row; name every column".format(label, index + 1),
                         column=index + 1)
        if name in names:
            raise refuse("chart_refused", "{}: the column name {} appears twice in the header row; make each unique".format(
                label, json.dumps(name, ensure_ascii=False)), column=name)
        names.append(name)
    if len(names) > MAX_COLUMNS:
        raise too_big("data", "MAX_COLUMNS", MAX_COLUMNS, "{} has {} columns; the limit is {}".format(label, len(names), MAX_COLUMNS))
    return names


def _build(names: List[str], rows: List[List[Any]], label: str, sha: str, from_text: bool, notes: List[str]) -> Table:
    if len(rows) > MAX_DATA_ROWS:
        raise too_big("data", "MAX_DATA_ROWS", MAX_DATA_ROWS, "{} has {} data rows; the limit is {}: aggregate it first".format(
            label, len(rows), MAX_DATA_ROWS))
    width = len(names)
    ragged = 0
    columns_raw: List[List[Any]] = [[] for _ in names]
    for row in rows:
        if len(row) != width:
            ragged += 1
            row = (list(row) + [None] * width)[:width]
        for index in range(width):
            columns_raw[index].append(row[index])
    if ragged:
        notes.append("{} ragged row{} padded with nulls".format(ragged, "" if ragged == 1 else "s"))
    columns = [_type_column(name, raw, from_text) for name, raw in zip(names, columns_raw)]
    return Table(columns, len(rows), label, sha, notes)


def parse(data: bytes, suffix: str, label: str) -> Table:
    """A table from a file's bytes: CSV or TSV (header row, ``utf-8-sig``) or JSON (an array of objects,
    ``{"columns", "rows"}`` or ``{"data": [...]}``)."""
    if len(data) > MAX_CHART_DATA_BYTES:
        raise too_big("data", "MAX_CHART_DATA_BYTES", MAX_CHART_DATA_BYTES, "{} is over {} MB".format(label, MAX_CHART_DATA_BYTES // (1024 * 1024)))
    sha = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise refuse("chart_refused", "{} is not UTF-8 text".format(label))
    suffix = suffix.lower()
    if suffix in (".csv", ".tsv"):
        reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t" if suffix == ".tsv" else ",")
        try:
            header = next(reader, None)
            if header is None:
                raise refuse("chart_empty", "{} is empty".format(label))
            names = _check_header(header, label)
            rows = []
            for row in reader:
                if not row or (len(row) == 1 and not row[0].strip()):
                    continue
                rows.append(row)
                if len(rows) > MAX_DATA_ROWS:
                    break
        except csv.Error as err:
            raise refuse("chart_refused", "{} is not valid {}: {}".format(label, suffix[1:].upper(), err))
        return _build(names, rows, label, sha, True, [])
    if suffix == ".json":
        try:
            doc = json.loads(text)
        except ValueError as err:
            raise refuse("chart_refused", "{} is not valid JSON: {}".format(label, err))
        return from_json(doc, label, sha)
    raise refuse("chart_refused", "chart data is a .csv, .tsv or .json file, not {}".format(suffix or "a file without a suffix"))


def _flat(value: Any, where: str) -> Any:
    if isinstance(value, (dict, list)):
        raise refuse("chart_refused", "{} holds a nested {}; flatten it: every cell is a number, text, a date or null".format(
            where, "object" if isinstance(value, dict) else "list"), path=where)
    return value


def from_json(doc: Any, label: str, sha: str) -> Table:
    """A table from JSON: ``[{...}, ...]``, ``{"columns": [...], "rows": [[...], ...]}`` or ``{"data": [...]}``."""
    if isinstance(doc, dict) and "data" in doc and "rows" not in doc:
        doc = doc["data"]
    if isinstance(doc, dict) and isinstance(doc.get("columns"), list) and isinstance(doc.get("rows"), list):
        names = _check_header(doc["columns"], label)
        rows = []
        for index, row in enumerate(doc["rows"]):
            if not isinstance(row, list):
                raise refuse("chart_refused", "{}: rows[{}] is not a list of cells".format(label, index))
            rows.append([_flat(v, "{} rows[{}]".format(label, index)) for v in row])
        return _build(names, rows, label, sha, False, [])
    if isinstance(doc, list):
        names: List[str] = []
        for index, item in enumerate(doc):
            if not isinstance(item, dict):
                raise refuse("chart_refused", "{}: item {} is not an object; give an array of objects, or columns and rows".format(label, index))
            for key in item:
                if key not in names:
                    names.append(key)
        names = _check_header(names, label) if names else []
        if not names:
            raise refuse("chart_empty", "{} holds no rows".format(label))
        rows = [[_flat(item.get(name), "{}[{}].{}".format(label, index, name)) for name in names] for index, item in enumerate(doc)]
        return _build(names, rows, label, sha, False, [])
    raise refuse("chart_refused", "{}: chart JSON is an array of objects, {{\"columns\": [...], \"rows\": [[...]]}} or {{\"data\": [...]}}".format(label))


def inline(rows: Any, columns: Any = None) -> Table:
    """A table from an op's inline ``rows`` (objects, or arrays with ``columns``), at most 500 rows and 32 KB."""
    if not isinstance(rows, list) or not rows:
        raise invalid("rows", "rows is a non-empty list of objects (or of arrays, with columns)")
    if len(rows) > MAX_INLINE_ROWS:
        raise too_big("rows", "MAX_INLINE_ROWS", MAX_INLINE_ROWS, "{} inline rows; the limit is {}: put it in a file under artifacts/".format(
            len(rows), MAX_INLINE_ROWS))
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if columns is not None:
        raw += json.dumps(columns, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_INLINE_BYTES:
        raise too_big("rows", "MAX_INLINE_BYTES", MAX_INLINE_BYTES, "the inline rows are over {} KB: put them in a file under artifacts/".format(
            MAX_INLINE_BYTES // 1024))
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if columns is not None:
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise invalid("columns", "columns is a list of column names (with rows as arrays)")
        if not all(isinstance(r, list) for r in rows):
            raise invalid("rows", "with columns, every row is an array of cells")
        return from_json({"columns": columns, "rows": rows}, "inline rows", sha)
    if not all(isinstance(r, dict) for r in rows):
        raise invalid("rows", "rows is a list of objects ({\"month\": \"2026-01\", \"revenue\": 12}); arrays need columns")
    return from_json(rows, "inline rows", sha)


def retype(table: Table, types: Mapping[str, str]) -> Table:
    """The table with the ``types`` overrides applied (each named column retyped, each value checked)."""
    if not types:
        return table
    columns = []
    for column in table.columns:
        declared = types.get(column.name)
        if declared is None or declared == column.type:
            columns.append(column)
            continue
        raw = column.values if column.type != "temporal" else column.values
        if declared == "ordinal":
            columns.append(Column(column.name, "ordinal", [as_label(v) for v in raw], declared=True))
        elif declared == "nominal":
            columns.append(Column(column.name, "nominal", [as_label(v) for v in raw], declared=True))
        else:
            columns.append(_type_column(column.name, [as_label(v) if isinstance(v, float) else v for v in raw], True, declared))
    return Table(columns, table.rows, table.label, table.sha256, table.notes)


# --------------------------------------------------------------------------
# filters (2.2 step 5)

FILTER_OPS = ("in", "not_in", "=", "!=", ">", ">=", "<", "<=", "between", "null")


def _operand(column: Column, value: Any, field: str) -> Any:
    if column.type == "quantitative":
        found = number(value)
        if found is None:
            raise invalid(field, "{} is quantitative, so this filter compares with a number, not {}".format(column.name, json.dumps(value)[:40]))
        return found[0]
    if column.type == "temporal":
        found = temporal(value if isinstance(value, str) else as_label(value))
        if found is None:
            raise invalid(field, "{} is temporal, so this filter compares with a date like 2026-03, not {}".format(column.name, json.dumps(value)[:40]))
        return found[1]
    if isinstance(value, (dict, list)):
        raise invalid(field, "{} is compared with text".format(column.name))
    return as_label(value)


def normalize_filters(raw: Any) -> List[Dict[str, Any]]:
    """The ``filter`` field's shape (each ``{"field", <one op>: operand}``); the operands are checked with the data."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or len(raw) > 16:
        raise invalid("filter", "filter is a list of at most 16 {\"field\": ..., \"<op>\": value} (ops: " + ", ".join(FILTER_OPS) + ")")
    out = []
    for index, item in enumerate(raw):
        where = "filter[{}]".format(index)
        if not isinstance(item, dict) or not isinstance(item.get("field"), str) or not item["field"]:
            raise invalid(where, "{} is {{\"field\": \"<column>\", \"<op>\": value}} (ops: {})".format(where, ", ".join(FILTER_OPS)))
        ops = [key for key in item if key != "field"]
        if len(ops) != 1 or ops[0] not in FILTER_OPS:
            raise invalid(where, "{} takes exactly one of: {}".format(where, ", ".join(FILTER_OPS)))
        op = ops[0]
        value = item[op]
        if op in ("in", "not_in") and (not isinstance(value, list) or not value or len(value) > 200):
            raise invalid(where + "." + op, "{} is a non-empty list of values".format(op))
        if op == "between" and (not isinstance(value, list) or len(value) != 2):
            raise invalid(where + ".between", "between is [low, high]")
        if op == "null" and not isinstance(value, bool):
            raise invalid(where + ".null", "null is true (keep only nulls) or false (drop them)")
        out.append({"field": item["field"], op: value})
    return out


def _matcher(column: Column, spec: Mapping[str, Any], where: str) -> Any:
    op = next(key for key in spec if key != "field")
    value = spec[op]
    data = column.keys if column.type == "temporal" else column.values
    if op == "null":
        return lambda i: (data[i] is None) is value  # type: ignore[index]
    if op in ("in", "not_in"):
        wanted = {_operand(column, v, "{}.{}".format(where, op)) for v in value}
        keep = op == "in"
        return lambda i: (data[i] in wanted) is keep  # type: ignore[index]
    if op == "between":
        low, high = (_operand(column, v, where + ".between") for v in value)
        if column.type == "nominal" or column.type == "ordinal":
            raise invalid(where + ".between", "{} is {}; between compares numbers or dates".format(column.name, column.type))
        return lambda i: data[i] is not None and low <= data[i] <= high  # type: ignore[index,operator]
    operand = _operand(column, value, "{}.{}".format(where, op))
    if op in (">", ">=", "<", "<=") and column.type in ("nominal", "ordinal"):
        raise invalid("{}.{}".format(where, op), "{} is {}; {} compares numbers or dates (use in, not_in, = or !=)".format(column.name, column.type, op))
    compare = {"=": lambda a: a == operand, "!=": lambda a: a != operand, ">": lambda a: a > operand, ">=": lambda a: a >= operand,
               "<": lambda a: a < operand, "<=": lambda a: a <= operand}[op]
    if op == "!=":
        return lambda i: data[i] is None or compare(data[i])  # type: ignore[index]
    return lambda i: data[i] is not None and compare(data[i])  # type: ignore[index]


def apply_filters(table: Table, filters: Sequence[Mapping[str, Any]]) -> List[int]:
    """The row indices every filter keeps; ``chart_empty`` naming each filter's own count when none is left."""
    rows = list(range(table.rows))
    if not filters:
        if not rows:
            raise refuse("chart_empty", "{} has no data rows".format(table.label))
        return rows
    matchers = []
    for index, spec in enumerate(filters):
        column = table.col(spec["field"], "filter[{}].field".format(index))
        matchers.append(_matcher(column, spec, "filter[{}]".format(index)))
    kept = [i for i in rows if all(m(i) for m in matchers)]
    if not kept:
        counts = ["filter[{}] {} keeps {}".format(index, json.dumps(spec, ensure_ascii=False, separators=(",", ":")), sum(1 for i in rows if m(i)))
                  for index, (spec, m) in enumerate(zip(filters, matchers))]
        raise refuse("chart_empty", "no row of {} passes every filter ({} rows): {}".format(table.label, table.rows, "; ".join(counts)),
                     counts=[sum(1 for i in rows if m(i)) for m in matchers])
    return kept


# --------------------------------------------------------------------------
# aggregation and shaping

AGGREGATES = ("sum", "mean", "median", "min", "max", "count", "none")


def reduce(values: Sequence[float], how: str) -> Optional[float]:
    """``how`` over ``values`` (nulls already left out); ``count`` counts them; None for an empty group (but count 0)."""
    if how == "count":
        return float(len(values))
    if not values:
        return None
    if how == "sum":
        return float(math.fsum(values))
    if how == "mean":
        return float(math.fsum(values)) / len(values)
    if how == "median":
        return quantile(sorted(values), 0.5)
    if how == "min":
        return float(min(values))
    if how == "max":
        return float(max(values))
    return float(values[-1])


def group(table: Table, rows: Sequence[int], by: Sequence[str]) -> Dict[Tuple[Any, ...], List[int]]:
    """Row indices by the values of ``by`` (in first-seen order; None is a key too)."""
    columns = [table.by_name[name].values for name in by]
    out: Dict[Tuple[Any, ...], List[int]] = {}
    for i in rows:
        key = tuple(col[i] for col in columns)
        found = out.get(key)
        if found is None:
            out[key] = [i]
        else:
            found.append(i)
    return out


def aggregate(table: Table, rows: Sequence[int], by: Sequence[str], measure: Optional[str], how: str) -> Dict[Tuple[Any, ...], Optional[float]]:
    """``how`` of ``measure`` per group of ``by`` (``count`` needs no measure)."""
    values = table.by_name[measure].values if measure else None
    out: Dict[Tuple[Any, ...], Optional[float]] = {}
    for key, members in group(table, rows, by).items():
        if how == "count" and values is None:
            out[key] = float(len(members))
            continue
        present = [values[i] for i in members if values[i] is not None] if values is not None else []
        out[key] = reduce(present, how)
    return out


def repeats(table: Table, rows: Sequence[int], by: Sequence[str]) -> bool:
    """Whether more than one row shares a value of ``by`` (then a chart sums them by default)."""
    columns = [table.by_name[name].values for name in by]
    seen = set()
    for i in rows:
        key = tuple(col[i] for col in columns)
        if key in seen:
            return True
        seen.add(key)
    return False


def top_other(totals: Sequence[Tuple[Any, float]], top: int, other: bool) -> Tuple[List[Any], Optional[List[Any]]]:
    """The ``top`` keys by total (ties by the key's text, so row order never matters), and the rest (merged into Other when ``other``, else dropped:
    None)."""
    order = sorted(range(len(totals)), key=lambda i: (-abs(totals[i][1] or 0.0), (totals[i][0] is None, str(totals[i][0]))))
    keep = sorted(order[:top])
    rest = sorted(order[top:])
    return [totals[i][0] for i in keep], ([totals[i][0] for i in rest] if other else None)


def quantile(ordered: Sequence[float], q: float) -> float:
    """The ``q`` quantile of sorted values (linear interpolation between order statistics, type 7)."""
    if not ordered:
        return 0.0
    pos = (len(ordered) - 1) * q
    low = int(math.floor(pos))
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def quantiles(values: Iterable[float], qs: Sequence[float] = (0.25, 0.5, 0.75)) -> List[float]:
    ordered = sorted(values)
    return [quantile(ordered, q) for q in qs]


def nice_step(raw: float) -> float:
    """1, 2 or 5 times a power of ten, at least ``raw``."""
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    exp = math.floor(math.log10(raw))
    base = 10.0 ** exp
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if mult * base >= raw - 1e-12 * base:
            return mult * base
    return 10.0 * base


def bin_numeric(values: Sequence[float], bins: Optional[int] = None) -> List[float]:
    """Bin edges over ``values``: ``bins`` of them, else Freedman-Diaconis (clamped to 5 to 60); edges on a nice step."""
    present = sorted(v for v in values if v is not None)
    if not present:
        return [0.0, 1.0]
    lo, hi = present[0], present[-1]
    if hi == lo:
        return [lo - 0.5, lo + 0.5]
    if bins is None:
        q1, q3 = quantile(present, 0.25), quantile(present, 0.75)
        width = 2.0 * (q3 - q1) * len(present) ** (-1.0 / 3.0)
        count = int(math.ceil((hi - lo) / width)) if width > 0 else 10
        bins = min(60, max(5, count))
    step = nice_step((hi - lo) / float(bins))
    start = math.floor(lo / step) * step
    edges = [start]
    while edges[-1] < hi - 1e-12 * max(1.0, abs(hi)) or len(edges) < 2:
        edges.append(round(start + len(edges) * step, 12))
        if len(edges) > 200:
            break
    if edges[-1] < hi:
        edges.append(round(start + len(edges) * step, 12))
    return edges


def lttb(xs: Sequence[float], ys: Sequence[float], n: int) -> List[int]:
    """Indices of ``n`` points of a series kept by Largest-Triangle-Three-Buckets (first and last always kept)."""
    count = len(xs)
    if n >= count or n < 3:
        return list(range(count)) if n >= count else [0, count - 1][:max(1, n)]
    keep = [0]
    bucket = (count - 2) / float(n - 2)
    a = 0
    for i in range(n - 2):
        start = int(math.floor((i + 1) * bucket)) + 1
        end = min(int(math.floor((i + 2) * bucket)) + 1, count)
        nxt_start, nxt_end = end, min(int(math.floor((i + 3) * bucket)) + 1, count)
        if nxt_start >= nxt_end:
            avg_x, avg_y = xs[-1], ys[-1]
        else:
            span = nxt_end - nxt_start
            avg_x = math.fsum(xs[nxt_start:nxt_end]) / span
            avg_y = math.fsum(ys[nxt_start:nxt_end]) / span
        best, chosen = -1.0, start
        ax, ay = xs[a], ys[a]
        for j in range(start, max(start + 1, end)):
            area = abs((ax - avg_x) * (ys[j] - ay) - (ax - xs[j]) * (avg_y - ay))
            if area > best:
                best, chosen = area, j
        keep.append(chosen)
        a = chosen
    keep.append(count - 1)
    return keep


def stride(count: int, n: int) -> List[int]:
    """``n`` evenly strided indices of ``count`` (first and last included): deterministic sampling."""
    if count <= n:
        return list(range(count))
    if n <= 1:
        return [0]
    return sorted({int(round(i * (count - 1) / float(n - 1))) for i in range(n)})


def json_text(value: Any) -> bytes:
    """``value`` as the compact, key-sorted JSON a size cap measures."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def compact_number(value: Optional[float]) -> Optional[float]:
    """A float kept to 6 significant digits (what models and docs store: small JSON, exact enough to draw)."""
    if value is None or not math.isfinite(value):
        return None
    if value == 0:
        return 0
    found = float("{:.6g}".format(value))
    return int(found) if found.is_integer() and abs(found) < 1e15 else found

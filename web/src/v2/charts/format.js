// The fixed number and date formatters a chart option names with {"$fmt": "<fmt id>"}
// (canvas-v2-phase3-4.md 2.6). This is the one place page code formats chart numbers, and it runs
// no agent input as code: a fmt id only selects one of these functions. It mirrors
// herdr_team/canvas_charts/_format.py line for line; tests/fixtures/charts/format-vectors.json
// (_format.vectors()) holds the two to the same strings.
//
// A fmt id is "<format>|<unit>" (the unit may be empty; "compact|$", "auto|ms", "percent|"):
//   format  auto | compact | integer | percent | 0 | 0.0 | 0.00 | 0.000 | date | month | year
//           (anything else is auto)
//   unit    "$" "£" "€" "¥" are prefixes, "%" a suffix without a space, anything else a suffix
//           after a space ("412 ms"); an invalid unit is dropped
// auto is compact from a million up, else grouped with up to 2 decimals (3 significant digits
// below 1). percent takes a fraction (0.125 is 12.5%). The date formats take day numbers (days
// since 1970-01-01), as the Python frame's time axes carry them. Negative numbers use U+2212.
//
// Rounding is Python's, not toFixed's: half away from zero on floor(|v| * 10^d + 0.5), the same
// float operations on both sides, with the digits read off an exact integer (BigInt), so a number
// past 2^53 prints the digits Python prints.

export const MINUS = "−";
export const CURRENCIES = ["$", "£", "€", "¥"];
export const FORMATS = ["auto", "compact", "integer", "percent", "0", "0.0", "0.00", "0.000", "date", "month", "year"];
const DATE_FORMATS = ["date", "month", "year"];
const FIXED = ["0", "0.0", "0.00", "0.000"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const UNIT = /^[^\s|:{}<>"'`\\]{1,8}$/u;
const SCALES = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "K"],
];

/** A unit _format.valid_unit accepts: 1 to 8 characters, no space, bar, colon, brace, bracket or quote. */
export function validUnit(unit) {
  return typeof unit === "string" && UNIT.test(unit);
}

/** |value| scaled by 10^decimals and rounded half away from zero, as an exact BigInt. */
export function rounded(value, decimals) {
  return BigInt(Math.floor(Math.abs(value) * 10 ** decimals + 0.5));
}

/** "1234567.5" style digits of scaled / 10^decimals: grouped thousands, trailing zeros kept. */
export function digits(scaled, decimals, group) {
  const unit = 10n ** BigInt(decimals);
  const whole = decimals ? scaled / unit : scaled;
  let text = group ? groupThousands(whole.toString()) : whole.toString();
  if (decimals) text += `.${(scaled % unit).toString().padStart(decimals, "0")}`;
  return text;
}

/** "1234567" -> "1,234,567" (Python's "{:,}"). */
export function groupThousands(text) {
  return text.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function trim(text) {
  return text.includes(".") ? text.replace(/0+$/, "").replace(/\.+$/, "") : text;
}

// Python's str.strip(chars): `chars` off both ends.
function stripChars(text, chars) {
  let a = 0;
  let b = text.length;
  while (a < b && chars.includes(text[a])) a += 1;
  while (b > a && chars.includes(text[b - 1])) b -= 1;
  return text.slice(a, b);
}

function plain(value, maxDecimals = 2) {
  const a = Math.abs(value);
  let decimals = maxDecimals;
  if (a > 0 && a < 1) decimals = Math.min(6, Math.max(maxDecimals, 2 - Math.floor(Math.log10(a))));
  return trim(digits(rounded(a, decimals), decimals, true));
}

function compact(value) {
  const a = Math.abs(value);
  for (let index = 0; index < SCALES.length; index += 1) {
    let [scale, suffix] = SCALES[index];
    if (a >= scale * 0.9995) {
      let s = a / scale;
      let decimals = s < 100 ? 1 : 0;
      let scaled = rounded(s, decimals);
      if (scaled >= 1000n * 10n ** BigInt(decimals) && index > 0) {
        [scale, suffix] = SCALES[index - 1];
        s = a / scale;
        decimals = 1;
        scaled = rounded(s, decimals);
      }
      return trim(digits(scaled, decimals, true)) + suffix;
    }
  }
  return plain(a);
}

const fdiv = (a, b) => Math.floor(a / b);

/** [year, month, day] of a day number (days since 1970-01-01, floored): Hinnant's civil-from-days. */
export function civil(days) {
  const z = Math.floor(days) + 719468;
  const era = fdiv(z >= 0 ? z : z - 146096, 146097);
  const doe = z - era * 146097;
  const yoe = fdiv(doe - fdiv(doe, 1460) + fdiv(doe, 36524) - fdiv(doe, 146096), 365);
  const y = yoe + era * 400;
  const doy = doe - (365 * yoe + fdiv(yoe, 4) - fdiv(yoe, 100));
  const mp = fdiv(5 * doy + 2, 153);
  const d = doy - fdiv(153 * mp + 2, 5) + 1;
  const m = mp < 10 ? mp + 3 : mp - 9;
  return [m <= 2 ? y + 1 : y, m, d];
}

// Python's "{:0Nd}": zero-padded after the sign.
const pad = (n, width) => (n < 0 ? `-${String(-n).padStart(width - 1, "0")}` : String(n).padStart(width, "0"));

/**
 * `value` as a chart writes it (_format.fmt_number): a finite number in a format and unit; any
 * other value (null, a string, a boolean) is an empty string.
 */
export function formatNumber(value, fmt = "auto", unit = "") {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  const v = value;
  const format = FORMATS.includes(fmt) ? fmt : "auto";
  const u = validUnit(unit) ? unit : "";
  if (DATE_FORMATS.includes(format)) {
    const [y, m, d] = civil(v);
    if (format === "date") return `${pad(y, 4)}-${pad(m, 2)}-${pad(d, 2)}`;
    return format === "month" ? `${MONTHS[m - 1]} ${pad(y, 4)}` : pad(y, 4);
  }
  let negative = v < 0;
  let body;
  if (format === "percent") {
    const scaled = rounded(v * 100, 1);
    body = `${trim(digits(scaled, 1, true))}%`;
    negative = negative && scaled !== 0n;
  } else if (format === "integer") {
    const scaled = rounded(v, 0);
    body = digits(scaled, 0, true);
    negative = negative && scaled !== 0n;
  } else if (FIXED.includes(format)) {
    const decimals = format.includes(".") ? format.length - 2 : 0;
    const scaled = rounded(v, decimals);
    body = digits(scaled, decimals, true);
    negative = negative && scaled !== 0n;
  } else if (format === "compact" || (format === "auto" && Math.abs(v) >= 1e6)) {
    body = compact(v);
    negative = negative && stripChars(body, "0.") !== "";
  } else {
    body = plain(v);
    negative = negative && stripChars(body, "0.,") !== "";
  }
  const sign = negative ? MINUS : "";
  if (CURRENCIES.includes(u)) return sign + u + body;
  if (u === "%" && format !== "percent") return `${sign}${body}%`;
  if (u && format !== "percent") return `${sign}${body} ${u}`;
  return sign + body;
}

/** [format, unit] from a fmt id (_format.parse_fmt_id): an unknown format is auto, an invalid unit "". */
export function parseFmtId(id) {
  const text = String(id ?? "");
  const bar = text.indexOf("|");
  const format = bar < 0 ? text : text.slice(0, bar);
  const unit = bar < 0 ? "" : text.slice(bar + 1);
  return [FORMATS.includes(format) ? format : "auto", validUnit(unit) ? unit : ""];
}

// The dimensions a label's value may sit in, in the order they are tried.
const VALUE_DIMS = ["value", "y", "x", "radius", "angle"];

// The number a formatter argument stands for: a number as it is; label and tooltip params by their
// value; a dataset row (or a [x, y, v] tuple) by its encoded value dimension, else its last number.
function valueOf(arg) {
  let value = arg;
  if (arg && typeof arg === "object" && !Array.isArray(arg)) value = arg.value;
  if (!Array.isArray(value)) return value;
  const encode = arg && typeof arg === "object" && !Array.isArray(arg) && arg.encode && typeof arg.encode === "object" ? arg.encode : null;
  if (encode) {
    for (const name of VALUE_DIMS) {
      const dims = Array.isArray(encode[name]) ? encode[name] : [];
      for (const dim of dims) if (typeof value[dim] === "number") return value[dim];
    }
  }
  for (let i = value.length - 1; i >= 0; i -= 1) if (typeof value[i] === "number") return value[i];
  return null;
}

/** The formatter function for a fmt id, as ECharts calls it: (value) for axes, (params) for labels and tooltips. */
export function formatterFor(id) {
  const [format, unit] = parseFmtId(id);
  return (arg) => formatNumber(valueOf(arg), format, unit);
}

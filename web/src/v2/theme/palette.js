// Paint for the v2 renderer (canvas-v2-phase1.md 1.3 and 1.4). The display list is theme-neutral:
// a paint is null, a literal "#rrggbb", a token reference ("tone.info.fill", "base.ink",
// "chip.3.bg"...) or {hatch: <ref or hex>}. Both palettes travel in the list, so switching theme is
// a lookup, never a refetch and never a colour-inverting filter.
//
// Before a display list arrives (or for one without palettes) the palettes come from the same token
// file the Python side reads (@synapse/tokens), flattened the way canvas_theme.palette(theme) does.
import tokens from "@synapse/tokens";

export const THEMES = ["light", "dark"];
const HEX = /^#[0-9a-fA-F]{6}$/;
const warned = new Set();

// One console.warn per distinct problem, not per frame.
export function warnOnce(key, message) {
  if (warned.has(key)) return;
  warned.add(key);
  if (typeof console !== "undefined") console.warn(`[synapse v2] ${message}`);
}

function flatten(set) {
  const out = {};
  if (!set) return out;
  for (const [role, hex] of Object.entries(set.base || {})) out[`base.${role}`] = hex;
  for (const [tone, roles] of Object.entries(set.tone || {})) {
    for (const [role, hex] of Object.entries(roles || {})) out[`tone.${tone}.${role}`] = hex;
  }
  (set.author_chips || []).forEach((chip, i) => {
    out[`chip.${i}.bg`] = chip.bg;
    out[`chip.${i}.fg`] = chip.fg;
  });
  if (set.human_chip) {
    out["chip.human.bg"] = set.human_chip.bg;
    out["chip.human.fg"] = set.human_chip.fg;
  }
  for (const key of Object.keys(out)) out[key] = String(out[key]).toLowerCase();
  return out;
}

const FALLBACK_PALETTES = Object.fromEntries(THEMES.map((t) => [t, flatten(tokens?.theme?.[t])]));
const FALLBACK_SHADOWS = Object.fromEntries(THEMES.map((t) => [t, tokens?.theme?.[t]?.shadow || {}]));

export function fallbackPalette(theme) {
  return FALLBACK_PALETTES[theme] || FALLBACK_PALETTES.light;
}

// The flat {ref: hex} map for a theme: the display list's, else the bundled tokens'.
export function paletteOf(dl, theme) {
  const own = dl?.palettes?.[theme];
  return own && typeof own === "object" ? own : fallbackPalette(theme);
}

// {"1": [{x, y, blur, color, alpha}], ...} for a theme.
export function shadowsOf(dl, theme) {
  const own = dl?.shadows?.[theme];
  return own && typeof own === "object" ? own : FALLBACK_SHADOWS[theme] || {};
}

function ink(palette) {
  const value = palette["base.ink"];
  return typeof value === "string" && HEX.test(value) ? value.toLowerCase() : "#1c2024";
}

// A token reference or literal as a hex. Unknown references draw as base.ink and warn once.
function resolveColor(value, palette) {
  if (typeof value !== "string") return ink(palette);
  if (value.startsWith("#")) {
    if (HEX.test(value)) return value.toLowerCase();
    warnOnce(`hex:${value}`, `malformed colour ${JSON.stringify(value)} drawn as base.ink`);
    return ink(palette);
  }
  const hex = palette[value];
  if (typeof hex === "string" && HEX.test(hex)) return hex.toLowerCase();
  warnOnce(`ref:${value}`, `unknown colour reference ${JSON.stringify(value)} drawn as base.ink`);
  return ink(palette);
}

/**
 * A paint resolved for one theme: null (none), "#rrggbb", or {hatch: "#rrggbb"}.
 * `dl` supplies the palettes; a palette object may be passed instead of a theme name.
 */
export function resolvePaint(value, theme, dl) {
  if (value === null || value === undefined) return null;
  const palette = typeof theme === "object" && theme ? theme : paletteOf(dl, theme);
  if (typeof value === "object") {
    if ("hatch" in value) return { hatch: resolveColor(value.hatch, palette) };
    warnOnce(`paint:${JSON.stringify(value).slice(0, 40)}`, "malformed paint drawn as base.ink");
    return ink(palette);
  }
  return resolveColor(value, palette);
}

// -- defs: elevation filters, hatch patterns and clips --------------------------------------------

/**
 * The ids a drawing refers to in <defs>, handed out in order of first use so the two SVG writers
 * agree (canvas-v2-phase1.md 1.8): synapse-elev-N for elevation N, synapse-hatch-K and
 * synapse-clip-K counting from 0.
 */
export class Defs {
  constructor(shadows) {
    this.shadows = shadows || {};
    this.elevations = new Set();
    this.hatches = new Map(); // resolved hex -> id
    this.clips = new Map(); // "x y w h" -> id
  }

  elevation(n) {
    const level = Number(n);
    if (!Number.isInteger(level) || level < 1 || level > 3) return null;
    const list = this.shadows[String(level)];
    if (!Array.isArray(list) || !list.length) return null;
    this.elevations.add(level);
    return `synapse-elev-${level}`;
  }

  hatch(hex) {
    if (!this.hatches.has(hex)) this.hatches.set(hex, `synapse-hatch-${this.hatches.size}`);
    return this.hatches.get(hex);
  }

  clip(key) {
    if (!this.clips.has(key)) this.clips.set(key, `synapse-clip-${this.clips.size}`);
    return this.clips.get(key);
  }

  get empty() {
    return !this.elevations.size && !this.hatches.size && !this.clips.size;
  }
}

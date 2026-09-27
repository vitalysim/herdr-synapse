// The canvas design tokens (.local/prd/canvas-v2-design.md), read at build time from the same
// JSON file the Python side uses. vite.config.js resolves "@synapse/tokens" to it, so the page
// never hardcodes a tone colour: shapes, overlays, Mermaid and the page chrome all come from here.
//
// Phase 0 draws the canvas with the light tokens in both themes: Excalidraw's dark mode is its own
// invert filter, which the design checked pair by pair. The page chrome uses the dark tokens.
import tokens from "@synapse/tokens";

export const TOKENS = tokens;
const LIGHT = tokens.theme.light;

export const TONES = Object.keys(LIGHT.tone);
export const VARIANTS = ["soft", "solid", "outline"];
export const BASE = LIGHT.base;
export const INK = BASE.ink;
export const MUTED = BASE.ink_muted;
export const CANVAS = BASE.canvas;
export const SURFACE = BASE.surface;
export const LINE = BASE.line;
export const GRID_LINE = BASE.grid;

// Excalidraw 0.18 font families (phase0_excalidraw.fontFamily): sans is family 2, the "Helvetica"
// slot the page's @font-face fills with Inter (src/fonts.css).
export const EX_FONT = tokens.phase0_excalidraw.fontFamily;
// CSS stacks for page chrome and for SVG the browser draws (Mermaid, placeholder cards). An SVG
// shown as an <img> cannot see page fonts and falls back past "Synapse Sans" to the system sans.
export const SANS = '"Synapse Sans", Helvetica, Arial, sans-serif';
export const MONO = '"Synapse Mono", "Geist Mono", ui-monospace, Menlo, monospace';

const tone = (name) => LIGHT.tone[name] || LIGHT.tone.neutral;
const GROUPS = tokens.kind_groups || { box: "shape", ellipse: "shape", diamond: "shape", note: "note", frame: "frame", arrow: "arrow", text: "text", pen: "ink", path: "ink" };

// A tone's colours for one element kind, {stroke, fill, text}: herdr_team/canvas_theme.resolve,
// read from its resolved tables in assets/canvas/tokens.json (tones for shapes, kinds.<group> for
// notes, frames, arrows, text and ink), else worked out the same way from the raw roles.
export function toneColors(name, variant = "soft", kind = "box") {
  const t = TONES.includes(name) ? name : "neutral";
  const v = VARIANTS.includes(variant) ? variant : "soft";
  const group = GROUPS[kind] || "other";
  const table = group === "shape" ? tokens.tones : tokens.kinds?.[group];
  const resolved = table?.[t]?.[v];
  if (resolved) return { ...resolved };
  const r = tone(t);
  if (group === "arrow") return { stroke: t === "neutral" ? LINE : r.stroke, fill: null, text: t === "neutral" ? MUTED : r.text };
  if (group === "text") return { stroke: r.text, fill: null, text: r.text };
  if (group === "ink") return { stroke: t === "neutral" ? INK : r.stroke, fill: null, text: r.text };
  if (group === "frame") return { stroke: v === "solid" ? r.stroke : r.zone_stroke, fill: r.zone, text: r.text };
  if (v === "solid") return { stroke: r.solid, fill: r.solid, text: r.on_solid };
  if (v === "outline") return { stroke: r.stroke, fill: SURFACE, text: r.text };
  if (group === "other") return { stroke: r.stroke, fill: null, text: r.text };
  return { stroke: r.stroke, fill: group === "note" ? r.sticky : r.fill, text: r.text };
}

// The colours a kind gets when the op names no tone (canvas_theme.default_tone): a note is an
// idea sticky, everything else neutral.
export function defaultColors(kind) {
  const [name, variant] = tokens.defaults?.[kind] || [kind === "note" ? "idea" : "neutral", "soft"];
  return toneColors(name, variant, kind);
}

// The tone an old element's stored hex stands for (legacy.stroke_hex / fill_hex), or null.
export function legacyTone(hex, field = "stroke_hex") {
  if (typeof hex !== "string") return null;
  return tokens.legacy[field][hex.toLowerCase()] || null;
}

// Every sticky paper colour a human can pick for a note, plus the 0.21 note fill.
export const STICKY_FILLS = new Set([...TONES.map((name) => tone(name).sticky), tokens.legacy.note_fill].map((hex) => hex.toLowerCase()));

// -- authors -------------------------------------------------------------------------------

// An author's chip: {bg, fg, initials}. The chip index is the roster index the server stores
// per author, modulo the chip palette; the human always gets the ink chip.
export function authorChip(scene, name, theme = "light") {
  const set = tokens.theme[theme] || LIGHT;
  if (!name || name === "human") return { ...set.human_chip, initials: "OP" };
  const chips = set.author_chips;
  const index = scene?.authors?.[name]?.index;
  let at = Number.isInteger(index) && index >= 0 ? index : [...String(name)].reduce((sum, ch) => sum + ch.charCodeAt(0), 0);
  at %= chips.length;
  return { ...chips[at], initials: initialsOf(name) };
}

function initialsOf(name) {
  const parts = String(name).split(/[-_.\s]+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : String(name).slice(0, 2);
  return letters.toUpperCase();
}

// -- Mermaid -------------------------------------------------------------------------------

// themeVariables for Mermaid's "base" theme (Mermaid 11 has no "neo" look; that needs 12).
export function mermaidThemeVariables() {
  const neutral = tone("neutral");
  const info = tone("info");
  return {
    fontFamily: SANS,
    fontSize: "16px",
    background: SURFACE,
    primaryColor: info.fill,
    primaryBorderColor: info.stroke,
    primaryTextColor: info.text,
    secondaryColor: tone("accent").fill,
    secondaryBorderColor: tone("accent").stroke,
    secondaryTextColor: tone("accent").text,
    tertiaryColor: neutral.zone,
    tertiaryBorderColor: neutral.zone_stroke,
    tertiaryTextColor: neutral.text,
    mainBkg: info.fill,
    nodeBorder: info.stroke,
    nodeTextColor: info.text,
    clusterBkg: neutral.zone,
    clusterBorder: neutral.zone_stroke,
    titleColor: INK,
    lineColor: LINE,
    textColor: INK,
    edgeLabelBackground: SURFACE,
    noteBkgColor: tone("idea").sticky,
    noteBorderColor: tone("idea").stroke,
    noteTextColor: tone("idea").text,
  };
}

// A Mermaid classDef body in a tone ("fill:..,stroke:..,color:..").
export function mermaidClass(name, variant = "soft") {
  const c = toneColors(name, variant);
  return `fill:${c.fill},stroke:${c.stroke},color:${c.text}`;
}

// -- page chrome ------------------------------------------------------------------------------

// CSS variables for the page chrome in one theme; styles.css reads only these.
export function chromeVariables(theme = "light") {
  const set = tokens.theme[theme] || LIGHT;
  const b = set.base;
  const t = set.tone;
  return {
    "--ink": b.ink,
    "--muted": b.ink_muted,
    "--line": t.neutral.zone_stroke,
    "--panel": b.surface,
    "--soft": b.canvas,
    "--code-bg": t.neutral.sticky,
    "--accent": t.info.solid,
    "--accent-soft": t.info.fill,
    "--on-accent": t.info.on_solid,
    "--warn": t.warning.solid,
    "--warn-soft": t.warning.fill,
    "--warn-ink": t.warning.text,
    "--bad": t.danger.solid,
    "--bad-soft": t.danger.fill,
    "--bad-ink": t.danger.text,
    "--good": t.success.solid,
    "--good-soft": t.success.fill,
    "--toast": t.neutral.solid,
    "--on-toast": t.neutral.on_solid,
    "--selection": b.selection,
    "--diagram-bg": LIGHT.base.surface,
    "--font-sans": SANS,
    "--font-mono": MONO,
  };
}

// Puts one theme on the document: data-theme for CSS, and the chrome variables.
export function applyTheme(theme) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  for (const [name, value] of Object.entries(chromeVariables(theme))) root.style.setProperty(name, value);
}

// The one number format both SVG writers print (canvas-v2-phase1.md 1.7): round half away from
// zero to 2 decimals, drop trailing zeros, never "-0". herdr_team/canvas_svg.fmt is the same
// arithmetic on the same IEEE doubles, so the strings are equal; tests/fixtures/display/
// fmt-vectors.json holds both to it.
export function fmt(v) {
  const x = Number(v);
  if (!Number.isFinite(x)) return "0";
  const n = Math.floor(Math.abs(x) * 100 + 0.5);
  if (n === 0) return "0";
  const whole = Math.floor(n / 100);
  const frac = n % 100;
  let out = (x < 0 ? "-" : "") + String(whole);
  if (frac) out += "." + String(frac).padStart(2, "0").replace(/0$/, "");
  return out;
}

// Python's round() (half to even), for the pixel size of a picture: canvas_render.pixel_size
// rounds with it, and a .5 width would otherwise give the two writers different sizes.
export function roundHalfEven(v) {
  const floor = Math.floor(v);
  const diff = v - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

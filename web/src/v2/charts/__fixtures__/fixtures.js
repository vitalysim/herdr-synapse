// Test fixtures for the chart page code: this folder's own, plus the ones the Python side writes
// (tests/fixtures/charts/, canvas-v2-phase3-4.md I-9) whenever they exist. Node only (vitest).
import fs from "node:fs";
import path from "node:path";

export const HERE = import.meta.dirname;
export const REPO_CHARTS = path.resolve(HERE, "../../../../../tests/fixtures/charts");

const readJSON = (file) => JSON.parse(fs.readFileSync(file, "utf8"));

function jsonFiles(dir) {
  try {
    return fs
      .readdirSync(dir)
      .filter((name) => name.endsWith(".json"))
      .sort()
      .map((name) => path.join(dir, name));
  } catch {
    return [];
  }
}

/** [{name, source: "local" | "python", box, element, doc}] for every option fixture. */
export function optionFixtures() {
  const out = [];
  for (const [source, dir] of [["local", path.join(HERE, "options")], ["python", path.join(REPO_CHARTS, "options")]]) {
    for (const file of jsonFiles(dir)) {
      const data = readJSON(file);
      const element = data.element || {};
      // The slot box: the fixture's own, else the frame's (Frame.box, the slot's w and h), else the element's.
      const frameBox = Array.isArray(element.frame?.box) ? { w: element.frame.box[0], h: element.frame.box[1] } : null;
      const box = data.box || data.slot || frameBox || { w: Number(element.w) || 560, h: Number(element.h) || 360 };
      out.push({ name: `${source}/${path.basename(file, ".json")}`, source, box: { w: Number(box.w), h: Number(box.h) }, element, doc: data.doc ?? null, data });
    }
  }
  return out;
}

function vectors(file) {
  if (!fs.existsSync(file)) return null;
  const data = readJSON(file);
  return Array.isArray(data) ? data : Array.isArray(data.vectors) ? data.vectors : [];
}

/** {local: [...], python: [...] | null} */
export function formatVectors() {
  return { local: vectors(path.join(HERE, "format-vectors.json")), python: vectors(path.join(REPO_CHARTS, "format-vectors.json")) };
}

/** {local: [...], python: [...] | null} */
export function sanitizeVectors() {
  return { local: vectors(path.join(HERE, "sanitize-vectors.json")), python: vectors(path.join(REPO_CHARTS, "sanitize-vectors.json")) };
}

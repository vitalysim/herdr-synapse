import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { PAGE_TOKEN_BLOCKS, PYTHON_ONLY_TOKEN_BLOCKS } from "../../scripts/python-only-tokens.mjs";
import { TOKENS } from "./tokens.js";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function pageFiles(dir = SRC) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...pageFiles(full));
    else if (/\.(js|jsx)$/.test(entry.name) && !/\.test\.(js|jsx)$/.test(entry.name)) out.push(full);
  }
  return out;
}

// What the page bundles of the shared token document (vite.config.js pageTokens): the blocks only
// Python reads are dropped, so a page module may read one only optional-chained, with a fallback.
describe("page tokens (canvas-v2-phase6.md D4)", () => {
  it("bundles every block the page reads", () => {
    for (const key of PAGE_TOKEN_BLOCKS) expect(TOKENS[key], key).toBeTruthy();
    expect(PAGE_TOKEN_BLOCKS.filter((key) => PYTHON_ONLY_TOKEN_BLOCKS.includes(key))).toEqual([]);
  });

  it("drops the blocks only the Python side reads", () => {
    for (const key of PYTHON_ONLY_TOKEN_BLOCKS) expect(TOKENS[key], key).toBeUndefined();
  });

  it("never reads a dropped block without optional chaining", () => {
    const unguarded = [];
    for (const file of pageFiles()) {
      const code = fs.readFileSync(file, "utf8");
      for (const key of PYTHON_ONLY_TOKEN_BLOCKS) {
        const pattern = new RegExp(`\\b(?:TOKENS|tokens)\\.${key}\\b`, "g");
        for (const m of code.matchAll(pattern)) unguarded.push(`${path.relative(SRC, file)}: ${m[0]}`);
      }
    }
    expect(unguarded).toEqual([]);
  });
});

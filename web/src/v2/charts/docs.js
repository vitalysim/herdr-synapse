// Chart doc assets (canvas-v2-phase3-4.md 2.7): content-addressed JSON under the team's assets/
// route, so immutable and cached by name for the page's life (the most recent DOC_MAX kept).
import { getJSON, teamPath } from "../../api.js";

const DOC_MAX = 64;
const NAME = /^[0-9a-f]{32}\.json$/;
const docs = new Map();

/** The doc asset `name` of `team`, or null for no name. Rejects on a malformed name. */
export function loadDoc(team, name) {
  if (!name) return Promise.resolve(null);
  if (!NAME.test(String(name))) return Promise.reject(new Error(`not a doc asset name: ${String(name).slice(0, 40)}`));
  const key = `${team}|${name}`;
  if (docs.has(key)) {
    const hit = docs.get(key);
    docs.delete(key);
    docs.set(key, hit);
    return hit;
  }
  const promise = getJSON(teamPath(team, `assets/${encodeURIComponent(name)}`)).catch((err) => {
    docs.delete(key);
    throw err;
  });
  docs.set(key, promise);
  while (docs.size > DOC_MAX) docs.delete(docs.keys().next().value);
  return promise;
}

/** The doc name a slot or its element points at (the slot's ref.doc wins). */
export function docNameOf(prim, element) {
  const ref = prim && prim.ref && typeof prim.ref === "object" ? prim.ref.doc : null;
  return ref || (element && element.doc_asset) || null;
}

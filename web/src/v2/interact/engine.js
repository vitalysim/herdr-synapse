// Which canvas engine the page runs: "v2" (the display-list board, the default since 0.22) or
// "v1" (the classic Excalidraw canvas, kept for comparison). The query value wins (the ticket
// redirect keeps ?engine=), then the choice stored in this browser, then v2. Pure apart from the
// storage handed in, so it is testable without a DOM.
//
// The stored choice moved to a new key in 0.22 (canvas-v2-phase6.md D2): whoever switched back to
// the classic canvas while testing phases 1 to 5 has "v1" under the old key, and would otherwise
// open 0.22 on v1. The old key is ignored and removed on first load.

export const ENGINES = ["v1", "v2"];
export const DEFAULT_ENGINE = "v2";
export const ENGINE_KEY = "synapse-engine-v022";
export const LEGACY_ENGINE_KEY = "synapse-engine";
// Set to "1" in this browser's storage to always show the engine toggle (a developer's flag).
export const DEV_KEY = "synapse-dev";

const valid = (value) => (ENGINES.includes(value) ? value : null);

function queryEngine(location) {
  if (!location) return null;
  try {
    const search = typeof location === "string" ? location : location.search || "";
    return valid(new URLSearchParams(search.startsWith("?") || search === "" ? search : `?${search}`).get("engine"));
  } catch {
    return null;
  }
}

function storedEngine(storage) {
  if (!storage) return null;
  try {
    return valid(storage.getItem(ENGINE_KEY));
  } catch {
    return null;
  }
}

// engineOf(window.location, window.localStorage) -> "v1" | "v2". Drops the pre-0.22 choice.
export function engineOf(location, storage) {
  if (storage) {
    try {
      storage.removeItem(LEGACY_ENGINE_KEY);
    } catch {
      // blocked storage: nothing was kept there either
    }
  }
  return queryEngine(location) || storedEngine(storage) || DEFAULT_ENGINE;
}

// Whether the URL pins the engine (the stored choice then does not decide it).
export function engineFromQuery(location) {
  return queryEngine(location);
}

// Keeps the viewer's choice in this browser; a storage that throws keeps it for this page only.
export function setEngine(value, storage) {
  const engine = valid(value);
  if (!engine) throw new Error(`unknown engine ${value}`);
  if (storage) {
    try {
      storage.setItem(ENGINE_KEY, engine);
    } catch {
      // private window or blocked storage: the page still switches
    }
  }
  return engine;
}

// The URL with its engine query value set to `value` (or removed for the default), so a
// reload keeps the choice even where storage is blocked. The hash is left alone.
export function withEngine(href, value) {
  const url = new URL(href);
  if (value && value !== DEFAULT_ENGINE) url.searchParams.set("engine", value);
  else url.searchParams.delete("engine");
  return url.toString();
}

// Whether the top bar shows the engine chip. While both engines ship (0.22) it always does
// (canvas-v2-phase6.md D3): the way to the other canvas is one visible button. Setting
// ALWAYS_SHOW_ENGINE_TOGGLE to false brings back the earlier rule: only for someone who asked for the
// other engine (?engine=v1 in the URL), who runs it from an earlier choice (so there is always a way
// back), or who set the developer flag (`dev`, e.g. the Vite dev server, or DEV_KEY in storage).
export const ALWAYS_SHOW_ENGINE_TOGGLE = true;

export function engineToggleVisible(location, storage, { dev = false } = {}) {
  if (ALWAYS_SHOW_ENGINE_TOGGLE) return true;
  const other = ENGINES.find((name) => name !== DEFAULT_ENGINE);
  if (dev || queryEngine(location) === other || storedEngine(storage) === other) return true;
  if (!storage) return false;
  try {
    return storage.getItem(DEV_KEY) === "1";
  } catch {
    return false;
  }
}

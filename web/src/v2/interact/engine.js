// Which canvas engine the page runs: "v1" (Excalidraw, the default until the owner's demo gate)
// or "v2" (the display-list board). The query value wins (the ticket redirect keeps
// ?engine=v2), then the choice stored in this browser, then v1. Pure apart from the storage
// handed in, so it is testable without a DOM.

export const ENGINES = ["v1", "v2"];
export const DEFAULT_ENGINE = "v1";
export const ENGINE_KEY = "synapse-engine";

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

// engineOf(window.location, window.localStorage) -> "v1" | "v2"
export function engineOf(location, storage) {
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

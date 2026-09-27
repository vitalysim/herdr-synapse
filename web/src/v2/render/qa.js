// window.__synapseV2: the QA hook tools/canvas_qa.py --engine v2 reads (canvas-v2-phase1.md 4.2,
// 6.3 J8). No UI; installed by the Board on every v2 page.
import { camera } from "./camera.js";
import { fmt } from "./fmt.js";

function fontsLoaded() {
  if (typeof document === "undefined" || !document.fonts) return true;
  return document.fonts.status === "loaded";
}

// The entry groups under the root, in document order (an entry may have one per layer).
function entryGroups(root) {
  if (!root || typeof root.querySelectorAll !== "function") return [];
  return [...root.querySelectorAll("g[data-layer] > g[data-id]")];
}

/**
 * {id: [line, ...]}: the text each entry draws, read from the rendered <text> nodes, in order.
 */
export function readLines(root) {
  const out = {};
  for (const group of entryGroups(root)) {
    const id = group.getAttribute("data-id");
    const lines = [...group.querySelectorAll("text")].map((node) => node.textContent);
    out[id] = (out[id] || []).concat(lines);
  }
  return out;
}

/**
 * [{id, overflowPx}] for every entry with a line wider than its text primitive's box by more than
 * half a unit (world units, which are pixels at scale 1). A text group carries its box width in
 * data-box-w on the page; a title that grows with zoom has none and is not audited.
 * Also [{id, overflowPx: 0, collapsed}] for every entry whose drawn lines lost characters to
 * whitespace collapsing (the browser dropped an indent the server kept: QA 1 finding 4). The line
 * breaks still compare equal then, since they are read from textContent.
 */
export function audit(root) {
  const worst = new Map();
  const collapsed = new Map();
  for (const group of entryGroups(root)) {
    const id = group.getAttribute("data-id");
    for (const node of group.querySelectorAll("text")) {
      if (typeof node.getNumberOfChars !== "function") continue;
      const lost = String(node.textContent || "").length - node.getNumberOfChars();
      if (lost > 0) collapsed.set(id, (collapsed.get(id) || 0) + lost);
    }
  }
  for (const group of entryGroups(root)) {
    const id = group.getAttribute("data-id");
    for (const block of group.querySelectorAll("g[data-box-w]")) {
      const boxW = Number(block.getAttribute("data-box-w"));
      if (!Number.isFinite(boxW)) continue;
      for (const node of block.querySelectorAll("text")) {
        const length = typeof node.getComputedTextLength === "function" ? node.getComputedTextLength() : 0;
        const over = length - boxW;
        if (over > 0.5) worst.set(id, Math.max(worst.get(id) || 0, over));
      }
    }
  }
  return [
    ...[...worst.entries()].map(([id, over]) => ({ id, overflowPx: Number(fmt(over)) })),
    ...[...collapsed.entries()].map(([id, lost]) => ({ id, overflowPx: 0, collapsed: lost })),
  ];
}

/** Installs window.__synapseV2; returns uninstall(). */
export function installQAHook({ getDL, getCamera, setCamera, getRoot, getViewport }) {
  if (typeof window === "undefined") return () => {};
  const hook = {
    // True once the fonts are loaded and the current list is drawn, settled, on screen.
    ready() {
      const dl = getDL();
      const root = getRoot();
      // data-settled "0": a wheel zoom is still presented by the compositor (Surface.jsx); a
      // screenshot now would be a scaled bitmap, not the drawing.
      return !!dl && !!root && fontsLoaded() && root.getAttribute("data-version") === String(dl.version) && root.getAttribute("data-settled") !== "0";
    },
    version() {
      return getDL()?.version ?? null;
    },
    lines() {
      return readLines(getRoot());
    },
    audit() {
      return audit(getRoot());
    },
    fit() {
      const dl = getDL();
      if (!dl) return null;
      const next = camera.fit(dl.bbox, getViewport());
      setCamera(next);
      return next;
    },
    camera() {
      return getCamera ? getCamera() : null;
    },
    dl() {
      return getDL();
    },
  };
  window.__synapseV2 = hook;
  return () => {
    if (window.__synapseV2 === hook) delete window.__synapseV2;
  };
}

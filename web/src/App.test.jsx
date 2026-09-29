// @vitest-environment jsdom
// The cut-over (canvas-v2-phase6.md 1.2): the page opens canvas v2 (Board) with no query, the classic canvas
// (CanvasTab) only with ?engine=v1 or from the chip; the chip's words; the v1 banner; the choice stored before
// 0.22 is ignored. Both engines are mocked, and each mock counts how often it drew.
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, test, vi } from "vitest";

const drawn = vi.hoisted(() => ({ board: 0, canvasTab: 0, canvasTabTheme: null }));

vi.mock("./v2/Board.jsx", () => ({
  default: () => {
    drawn.board += 1;
    return <div data-engine="v2" data-testid="board" />;
  },
}));
vi.mock("./canvas/CanvasTab.jsx", () => ({
  // The theme it is handed is what makes the banner's silence about the theme true (QA phase 6, 3.1).
  default: ({ theme }) => {
    drawn.canvasTab += 1;
    drawn.canvasTabTheme = theme ?? null;
    return <div className="excalidraw" data-testid="canvas-tab" />;
  },
}));
vi.mock("./api.js", () => ({
  setCsrf: () => {},
  getJSON: async (path) => {
    if (path === "/api/session") {
      return { csrf: "c", writable: true, server_version: "0.22.0", switches: { session: { enabled: true } }, teams: [{ name: "alpha", viz: true }] };
    }
    if (path === "/api/teams") return { teams: [{ name: "alpha", viz: true }] };
    return {};
  },
}));
vi.mock("./stream.js", () => ({ openStream: () => ({ close: () => {} }) }));

const { default: App } = await import("./App.jsx");
const { ENGINE_KEY, LEGACY_ENGINE_KEY } = await import("./v2/interact/engine.js");

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

let mounted = [];
beforeEach(() => {
  drawn.board = 0;
  drawn.canvasTab = 0;
  drawn.canvasTabTheme = null;
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
});
afterEach(() => {
  for (const { root, node } of mounted) {
    act(() => root.unmount());
    node.remove();
  }
  mounted = [];
});

const settle = async () => {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
};

async function open(url = "/") {
  window.history.replaceState(null, "", url);
  const node = document.createElement("div");
  document.body.appendChild(node);
  const root = createRoot(node);
  await act(async () => root.render(<App />));
  mounted.push({ root, node });
  await settle();
  return node;
}

const chip = (node) => node.querySelector(".engine-toggle");

describe("the cut-over", () => {
  test("no query opens canvas v2 and never the classic canvas", async () => {
    const node = await open("/");
    expect(node.querySelector('[data-engine="v2"]')).not.toBe(null);
    expect(drawn.board).toBeGreaterThan(0);
    expect(drawn.canvasTab).toBe(0);
    expect(chip(node).textContent).toBe("Classic canvas");
    expect(chip(node).getAttribute("title")).toBe("Open the classic canvas (Excalidraw, v1). Canvas v2 is the default.");
    expect(chip(node).hasAttribute("aria-pressed")).toBe(false);
    expect(node.textContent).not.toContain("Classic canvas (v1):"); // the v1 banner belongs to v1 only
  });

  test("?engine=v1 opens the classic canvas with its banner, and the banner goes back to v2", async () => {
    const node = await open("/?engine=v1#team=alpha");
    expect(drawn.canvasTab).toBeGreaterThan(0);
    expect(drawn.board).toBe(0);
    expect(chip(node).textContent).toBe("Canvas v2");
    expect(chip(node).getAttribute("title")).toBe("Back to canvas v2");
    const banner = [...node.querySelectorAll(".banner")].find((b) => b.textContent.includes("Classic canvas (v1)"));
    expect(banner.textContent).toContain(
      "frozen in 0.22, for comparison. A block keeps its title but loses its body, badges and counters, and a table or a 3D scene is a captioned placeholder.",
    );
    await act(async () => banner.querySelector("button").click());
    await settle();
    expect(node.querySelector('[data-engine="v2"]')).not.toBe(null);
    expect(window.localStorage.getItem(ENGINE_KEY)).toBe("v2");
    expect(window.location.search).toBe(""); // the default takes no query
  });

  // The banner used to say the classic canvas "is always light", and the gates held that falsehood in place by
  // matching the string. These two tests tie each half of the sentence to what the page really does (QA phase 6, 3.1).
  test("the v1 banner claims no theme, because the classic canvas follows the page's", async () => {
    const node = await open("/?engine=v1#team=alpha");
    const banner = [...node.querySelectorAll(".banner")].find((b) => b.textContent.includes("Classic canvas (v1)"));
    expect(banner.textContent).not.toMatch(/always (light|dark)|only (light|dark)|(light|dark) only/i);
    expect(drawn.canvasTabTheme).toBe("light");
    await act(async () => node.querySelector(".theme-toggle").click());
    await settle();
    expect(drawn.canvasTabTheme).toBe("dark"); // the same board, the other theme: the banner may not say otherwise
  });

  test("what the v1 banner says a board loses is what the classic canvas has no builder for", async () => {
    const { PAGE_KINDS } = await import("./canvas/kinds/names.js");
    const kinds = new Set(PAGE_KINDS);
    // A kind with no v1 builder falls back to kinds/placeholder.js: a dashed box holding the server's fitted title,
    // or "<type>: <text>" when the element has no fitted lines (a table, a 3D scene). Bodies, badges, owners, icons
    // and column counters are display-list primitives no v1 builder draws.
    for (const lost of ["badge", "callout", "card", "heading", "icon", "scene3d", "sticky", "table"]) {
      expect([lost, kinds.has(lost)]).toEqual([lost, false]);
    }
    // Charts do draw on v1 (media.js renders them), which is why the banner no longer lists them as lost.
    expect(kinds.has("chart")).toBe(true);
  });

  test("the chip switches to the classic canvas and keeps the choice under the new key", async () => {
    const node = await open("/");
    await act(async () => chip(node).click());
    await settle();
    expect(drawn.canvasTab).toBeGreaterThan(0);
    expect(node.querySelector(".excalidraw")).not.toBe(null);
    expect(window.localStorage.getItem(ENGINE_KEY)).toBe("v1");
  });

  test("a choice stored before 0.22 does not keep the owner on v1", async () => {
    window.localStorage.setItem(LEGACY_ENGINE_KEY, "v1");
    const node = await open("/");
    expect(node.querySelector('[data-engine="v2"]')).not.toBe(null);
    expect(drawn.canvasTab).toBe(0);
    expect(window.localStorage.getItem(LEGACY_ENGINE_KEY)).toBe(null);
  });

  test("a choice made on 0.22 is kept", async () => {
    window.localStorage.setItem(ENGINE_KEY, "v1");
    await open("/");
    expect(drawn.canvasTab).toBeGreaterThan(0);
    expect(drawn.board).toBe(0);
  });
});

// @vitest-environment jsdom
// The review card (canvas-v2-phase5.md 12.3): the buttons by writability, Accept disabled when the
// proposal is outdated, the reason in words, the base note, Reject with a note, Ask prefilled with
// the proposer, and where the card goes.
import fs from "node:fs";
import path from "node:path";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";
import ReviewCard, { CARD_W, cardPosition, reasonWords } from "./ReviewCard.jsx";

const DL = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "__fixtures__", "display.json"), "utf8"));
const P3 = DL.entries.find((e) => e.id === "P-3").proposal;
const P4 = DL.entries.find((e) => e.id === "P-4").proposal;

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

let mounted = [];
afterEach(() => {
  for (const { root, node } of mounted) {
    act(() => root.unmount());
    node.remove();
  }
  mounted = [];
});

function mount(props) {
  const node = document.createElement("div");
  document.body.appendChild(node);
  const root = createRoot(node);
  const handlers = { onAccept: vi.fn(), onReject: vi.fn(), onAsk: vi.fn(), onZoom: vi.fn(), onClose: vi.fn() };
  act(() => root.render(React.createElement(ReviewCard, { id: "P-3", proposal: P3, box: { x: 100, y: 100, w: 200, h: 80 }, viewport: { w: 1200, h: 800 }, writable: true, chip: { bg: "#e5484d", fg: "#fff", initials: "DR" }, ...handlers, ...props })));
  mounted.push({ root, node });
  return { node, ...handlers };
}

const button = (node, text) => [...node.querySelectorAll("button")].find((b) => b.textContent.trim() === text) || null;
const typeInto = (input, value) => {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
};

describe("the card", () => {
  test("says who suggests what, and why, in words", () => {
    const { node } = mount({});
    expect(node.textContent).toContain("drawer suggests");
    expect(node.textContent).toContain("align the price box with the flow");
    expect(node.textContent).toContain('E-1 "Hello" (the operator\'s): moved by c+2r0');
    expect(node.querySelector(".cv2-card-reason").textContent).toBe("changes your marks");
  });

  test("reason words", () => {
    expect(reasonWords("human_made")).toBe("changes your marks");
    expect(reasonWords("peer", "beta")).toBe("edits beta's work");
    expect(reasonWords("foreign_lane", "beta")).toBe("in beta's lane");
    expect(reasonWords("foreign_lane")).toBe("in another agent's lane");
    expect(reasonWords("frozen")).toBe("in a frozen area");
  });

  test("writable: Accept, Reject, Ask and Zoom to; Accept sends", () => {
    const { node, onAccept, onZoom } = mount({});
    for (const text of ["Accept", "Reject", "Ask", "Zoom to"]) expect(button(node, text), text).not.toBeNull();
    act(() => button(node, "Accept").click());
    expect(onAccept).toHaveBeenCalledTimes(1);
    act(() => button(node, "Zoom to").click());
    expect(onZoom).toHaveBeenCalledTimes(1);
  });

  test("read-only: the card without its buttons (Zoom to stays)", () => {
    const { node } = mount({ writable: false });
    expect(button(node, "Accept")).toBeNull();
    expect(button(node, "Reject")).toBeNull();
    expect(button(node, "Ask")).toBeNull();
    expect(button(node, "Zoom to")).not.toBeNull();
  });

  test("outdated: Accept is disabled with a reason; the warning and the base note show", () => {
    const { node } = mount({ id: "P-4", proposal: P4, owner: "drawer" });
    const accept = button(node, "Accept");
    expect(accept.disabled).toBe(true);
    expect(accept.getAttribute("title")).toMatch(/outdated/);
    expect(node.querySelector(".cv2-card-warn")).not.toBeNull();
    expect(node.querySelector("[data-base-note]").textContent).toBe("made against v10; you changed E-2 since");
    expect(node.querySelector(".cv2-card-reason").textContent).toBe("edits drawer's work");
  });

  test("Reject asks for an optional note, then sends it", () => {
    const { node, onReject } = mount({});
    act(() => button(node, "Reject").click());
    const input = node.querySelector(".cv2-card-input input");
    expect(input).not.toBeNull();
    act(() => typeInto(input, "keep it red"));
    act(() => node.querySelector(".cv2-card-input button").click());
    expect(onReject).toHaveBeenCalledWith("keep it red");
  });

  test("Ask is prefilled with the proposer", () => {
    const { node, onAsk } = mount({});
    act(() => button(node, "Ask").click());
    const input = node.querySelector(".cv2-card-input input");
    expect(input.value).toBe("@drawer ");
    act(() => typeInto(input, "@drawer why this far?"));
    act(() => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    expect(onAsk).toHaveBeenCalledWith("@drawer why this far?");
  });

  test("a hostile summary stays text", () => {
    const { node } = mount({ proposal: { ...P3, intent: "<script>window.__x=1</script>", summary: ['<img src=x onerror="window.__x=1">'] } });
    expect(node.querySelector("script")).toBeNull();
    expect(node.querySelector("img")).toBeNull();
    expect(window.__x).toBeUndefined();
  });
});

describe("placement", () => {
  const vp = { w: 1200, h: 800 };
  test("right of the ghost when it fits, else left, else below or above; kept on screen", () => {
    expect(cardPosition({ x: 100, y: 100, w: 200, h: 80 }, vp)).toMatchObject({ side: "right", left: 312, top: 100 });
    expect(cardPosition({ x: 900, y: 100, w: 200, h: 80 }, vp)).toMatchObject({ side: "left", left: 900 - 12 - CARD_W });
    expect(cardPosition({ x: 100, y: 100, w: 1000, h: 80 }, vp)).toMatchObject({ side: "below", top: 192 });
    expect(cardPosition({ x: 100, y: 600, w: 1000, h: 190 }, vp).side).toBe("above");
    const low = cardPosition({ x: 100, y: 780, w: 100, h: 10 }, vp);
    expect(low.top + 240).toBeLessThanOrEqual(vp.h - 8);
  });
});

// @vitest-environment jsdom
// index.js: the first-load half of the charts (canvas-v2-phase3-4.md 2.10, WW-2, WW-5). The slot
// shows its fallback until slot.jsx has loaded, then slot.jsx draws with the same props; the live
// view is empty until live.jsx has loaded.
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeAll, describe, expect, test, vi } from "vitest";

let releaseSlot;
const slotGate = new Promise((resolve) => (releaseSlot = resolve));
vi.mock("./slot.jsx", async () => {
  await slotGate;
  return { default: (props) => <g data-lazy-slot={props.entryId} /> };
});
vi.mock("./live.jsx", () => ({ default: (props) => <div data-lazy-live={props.entryId} /> }));

const { QA, chartSlot } = await import("./index.js");

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

const settle = () =>
  act(async () => {
    for (let i = 0; i < 10; i += 1) await new Promise((r) => setTimeout(r, 0));
  });

describe("chartSlot", () => {
  test("the slot definition (WW-2) and the QA hook (WW-5)", () => {
    expect(chartSlot.mode).toBe("svg");
    expect(typeof chartSlot.Component).toBe("function");
    expect(typeof chartSlot.Live).toBe("function");
    expect(QA.name).toBe("charts");
    expect(Array.isArray(QA.read())).toBe(true);
  });

  test("the fallback shows until slot.jsx arrives, then the slot draws", async () => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    document.body.appendChild(node);
    const root = createRoot(node);
    const { Component } = chartSlot;
    act(() => root.render(<Component entryId="E-1" fallback={<g data-fallback="1" />} />));
    await settle();
    expect(node.querySelector("[data-fallback]")).not.toBe(null);
    expect(node.querySelector("[data-lazy-slot]")).toBe(null);
    releaseSlot();
    await settle();
    expect(node.querySelector("[data-fallback]")).toBe(null);
    expect(node.querySelector('[data-lazy-slot="E-1"]')).not.toBe(null);
    act(() => root.unmount());
    node.remove();
  });

  test("the live view mounts live.jsx", async () => {
    const node = document.createElement("div");
    document.body.appendChild(node);
    const root = createRoot(node);
    const { Live } = chartSlot;
    act(() => root.render(<Live entryId="E-2" />));
    await settle();
    expect(node.querySelector('[data-lazy-live="E-2"]')).not.toBe(null);
    act(() => root.unmount());
    node.remove();
  });
});

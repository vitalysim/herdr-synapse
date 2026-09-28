// @vitest-environment jsdom
// live.jsx: the entered chart (canvas-v2-phase3-4.md 2.10, WW-2, WW-3). A live SVG ECharts in the
// HTML layer over the slot, disposed on exit; GL charts delegate to glCharts.js; Esc calls onExit;
// the board's pointer gestures stop at it.
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, test, vi } from "vitest";
import { optionFixtures } from "./__fixtures__/fixtures.js";

const fx = Object.fromEntries(optionFixtures().map((f) => [f.name, f]));
const glDispose = vi.fn();
vi.mock("./gl.js", () => ({ mountGL: vi.fn(async () => ({ dispose: glDispose })), renderGL: vi.fn() }));
vi.mock("./docs.js", () => ({ loadDoc: vi.fn(async () => fx["local/bar"].doc), docNameOf: (prim, el) => prim?.ref?.doc || el?.doc_asset || null }));

const { mountGL } = await import("./gl.js");
const { default: ChartLive, mountLiveChart } = await import("./live.jsx");
const { fallbackPalette } = await import("../theme/palette.js");

beforeAll(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  // The lazy chunks (render.js, ECharts) transform slowly under a full parallel run: load them once
  // here, so the tests' own waits measure the component, not the first import.
  await import("./render.js");
  await import("./echartsCore.js");
}, 60000);

let mounted = [];
function mount(props, onParentPointerDown = undefined) {
  const node = document.createElement("div");
  document.body.appendChild(node);
  const root = createRoot(node);
  act(() => root.render(<div onPointerDown={onParentPointerDown}><ChartLive {...props} /></div>));
  mounted.push({ root, node });
  return { node, unmount: () => act(() => root.unmount()) };
}
const settle = () => act(async () => {
  for (let i = 0; i < 8; i += 1) await new Promise((r) => setTimeout(r, 0));
});
beforeEach(() => {
  // jsdom has no canvas: zrender then estimates text widths.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  vi.mocked(mountGL).mockClear();
  glDispose.mockClear();
});
afterEach(() => {
  for (const { root, node } of mounted) {
    try {
      act(() => root.unmount());
    } catch {
      // already unmounted
    }
    node.remove();
  }
  mounted = [];
});

const PRIM = { k: "slot", slot: "chart", x: 40, y: 60, w: 400, h: 260, ref: { doc: "0123456789abcdef0123456789abcdef.json" } };

describe("ChartLive", () => {
  test("mounts a live SVG chart in world coordinates, on the chart paper", async () => {
    const f = fx["local/bar"];
    const { node } = mount({ prim: PRIM, entryId: "E-51", version: 12, element: f.element, team: "t", theme: "light", palette: fallbackPalette("light") });
    await vi.waitFor(() => expect(node.querySelector(".sv2-chart-live svg")).not.toBe(null), { timeout: 3000 });
    const div = node.querySelector(".sv2-chart-live");
    expect(div.style.left).toBe("40px");
    expect(div.style.top).toBe("60px");
    expect(div.style.width).toBe("400px");
    expect(div.style.background).not.toBe("");
    expect(div.querySelector("svg")).not.toBe(null);
    // no HTML text inside the chart: labels and (richText) tooltips are SVG
    expect([...div.querySelectorAll("div")].every((d) => !d.childNodes.length || [...d.childNodes].every((c) => c.nodeType === 1))).toBe(true);
  });

  test("disposes the chart on exit", async () => {
    const f = fx["local/bar"];
    const host = document.createElement("div");
    document.body.appendChild(host);
    const handle = await mountLiveChart(host, { element: f.element, prim: PRIM, team: "t", theme: "dark", palette: fallbackPalette("dark"), w: 400, h: 260 });
    expect(handle.chart.getOption().animation).toBe(false);
    expect(handle.chart.getOption().tooltip[0].renderMode).toBe("richText");
    expect(host.querySelector("svg")).not.toBe(null);
    handle.dispose();
    expect(handle.chart.isDisposed()).toBe(true);
    host.remove();
  });

  test("a GL chart mounts through glCharts and is disposed on unmount", async () => {
    const element = { id: "E-9", engine: "echarts", chart: { type: "bar3d", gl: true }, option: { grid3D: {} }, updated_seq: 3 };
    const { unmount } = mount({ prim: PRIM, entryId: "E-9", version: 3, element, team: "t", theme: "dark", palette: fallbackPalette("dark") });
    await settle();
    expect(mountGL).toHaveBeenCalledTimes(1);
    const [div, option, doc, opts] = vi.mocked(mountGL).mock.calls[0];
    expect(div.tagName).toBe("DIV");
    expect(option).toBe(element.option);
    expect(doc).toEqual(fx["local/bar"].doc);
    expect(opts).toMatchObject({ w: 400, h: 260, theme: "dark" });
    unmount();
    expect(glDispose).toHaveBeenCalledTimes(1);
  });

  test("Esc leaves; pointer-downs stay inside", async () => {
    const onExit = vi.fn();
    const outer = vi.fn();
    const f = fx["local/bar"];
    const { node } = mount({ prim: PRIM, entryId: "E-51", element: f.element, team: "t", theme: "light", palette: fallbackPalette("light"), onExit }, outer);
    await settle();
    const div = node.querySelector(".sv2-chart-live");
    act(() => div.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(onExit).toHaveBeenCalledTimes(1);
    act(() => div.dispatchEvent(new Event("pointerdown", { bubbles: true })));
    expect(outer).not.toHaveBeenCalled();
  });
});

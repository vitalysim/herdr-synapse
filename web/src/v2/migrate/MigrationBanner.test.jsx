// @vitest-environment jsdom
// The canvas v2 migration notice (canvas-v2-phase6.md 2.4): its words from the server's counts, Fix sizes and
// Dismiss each send one `migrate` op, Details says what changes, Not now hides it for this visit.
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";
import MigrationBanner, { MIGRATE_INTENT, detailLines, noticeText, resultText } from "./MigrationBanner.jsx";

const INFO = { pending: true, state: null, pre_022: 16, refit: 16, overflow: 14, sketch: 3, legacy_colour: 24, unknown_hex: 1, vega_lite: 1, ids_total: 17 };

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
  act(() => root.render(<MigrationBanner {...props} />));
  mounted.push({ root, node });
  return node;
}

const click = async (node, action) => {
  await act(async () => {
    node.querySelector(`[data-action="${action}"]`).click();
  });
};

describe("migration notice", () => {
  test("says what v2 draws differently, in the spec's words", () => {
    expect(noticeText(INFO)).toBe(
      "This board was drawn before canvas v2. 14 labels need resizing and 3 marks in the old sketch style are now drawn clean.",
    );
    expect(noticeText({ refit: 1, overflow: 0, sketch: 0 })).toBe("This board was drawn before canvas v2. 1 label is sized for the old fonts.");
    expect(noticeText({ refit: 0, overflow: 1, sketch: 1 })).toBe(
      "This board was drawn before canvas v2. 1 label needs resizing and 1 mark in the old sketch style is now drawn clean.",
    );
    // 0.21 drew every mark sketchy by default, so the count is usually the whole board: never call it hand-drawn.
    expect(`${noticeText(INFO)} ${detailLines(INFO).join(" ")}`).not.toMatch(/hand-drawn/);
    const lines = detailLines(INFO).join("\n");
    expect(lines).toMatch(/16 labels from before canvas v2; Fix sizes sizes 16 of them again/);
    expect(lines).toMatch(/1 Vega-Lite chart is still drawn/);
    expect(lines).toMatch(/hand font \(the classic canvas still draws them\)/);
    expect(lines).toMatch(/Nothing stored is lost/);
  });

  test("Fix sizes sends one migrate op (apply) and says what it did", async () => {
    const send = vi.fn(async () => ({ applied: [{ index: 0, op: "migrate", ids: ["E-1"], migrate: { action: "apply", refitted: 16, grew: 9, moved: 2, clean: 3, batch: "B-5" } }] }));
    const toast = vi.fn();
    const node = mount({ info: INFO, send, toast });
    expect(node.querySelector("[data-migration]").textContent).toContain("14 labels need resizing");
    await click(node, "apply");
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toEqual([{ op: "migrate", action: "apply", intent: MIGRATE_INTENT.apply }]);
    expect(toast).toHaveBeenCalledWith("Migrated: 16 refitted (9 grew, 2 moved to fit), 3 drawn clean; undo B-5 to take it back.", "ok");
  });

  test("Dismiss sends migrate dismiss; Details opens the list; Not now hides it", async () => {
    const send = vi.fn(async () => ({ applied: [{ index: 0, op: "migrate", ids: [], migrate: { action: "dismiss" } }] }));
    const node = mount({ info: INFO, send });
    expect(node.querySelector(".cv2-migrate-details")).toBe(null);
    await click(node, "details");
    expect(node.querySelectorAll(".cv2-migrate-details li").length).toBe(detailLines(INFO).length);
    await click(node, "dismiss");
    expect(send.mock.calls[0][0]).toEqual([{ op: "migrate", action: "dismiss", intent: MIGRATE_INTENT.dismiss }]);
    await click(node, "later");
    expect(node.querySelector("[data-migration]")).toBe(null);
  });

  test("result words", () => {
    expect(resultText(null)).toBe(null);
    expect(resultText({ migrate: { action: "dismiss" } })).toMatch(/dismissed/);
  });
});

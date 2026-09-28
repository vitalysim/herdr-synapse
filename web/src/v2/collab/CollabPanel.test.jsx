// @vitest-environment jsdom
// The collaboration panel (canvas-v2-phase5.md 12.4): the proposals list, "Revert all by <name>"
// with its three kinds of `since`, the checkpoint flow and the two settings; read-only pages get
// the lists without the actions.
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";
import CollabPanel, { agentRows, openProposals } from "./CollabPanel.jsx";

const NOW = Date.parse("2026-09-28T10:30:00Z");
const SCENE = {
  version: 20,
  authors: { human: { kind: "human", index: -1 }, drawer: { kind: "member", index: 0, agent: "claude" }, peer: { kind: "member", index: 1, agent: "codex" } },
  batches: {
    "B-1": { author: "drawer", first_seq: 3, last_seq: 4, at: "2026-09-28T09:00:00Z", undone: false },
    "B-2": { author: "drawer", first_seq: 10, last_seq: 12, at: "2026-09-28T10:25:00Z", undone: false },
  },
  proposals: [
    { id: "P-3", author: "drawer", status: "open", seq: 12, op: "move", intent: "align", summary: ["E-1 moved"] },
    { id: "P-2", author: "peer", status: "rejected", seq: 9 },
    { id: "P-4", author: "peer", status: "open", seq: 13, op: "edit", summary: ["E-2 text"] },
  ],
  checkpoints: [
    { id: "V-1", label: "start", version: 2, by: "human", auto: false },
    { id: "V-2", label: "before B-2 (restore)", version: 11, by: "human", auto: true },
  ],
  freezes: [{ id: "X-5", region: [0, 0, 100, 100], ids: null, label: "final layout" }],
  settings: { collab: { human_edits: "propose", frozen: "refuse" } },
  claims: [],
  homes: {},
};
const PRESENCE = { members: [{ name: "peer", status: "waiting", intent: "needs the numbers", opacity: 1, at: NOW }], operators: [] };
const DL_PROPOSALS = new Map([
  ["P-3", { author: "drawer", outdated: false, summary: ["E-1 moved by c+2r0"] }],
  ["P-4", { author: "peer", outdated: true, summary: ["E-2 text"] }],
]);

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

function mount(props = {}) {
  const node = document.createElement("div");
  document.body.appendChild(node);
  const root = createRoot(node);
  const actions = {
    openProposal: vi.fn(),
    revert: vi.fn(),
    saveCheckpoint: vi.fn(),
    restore: vi.fn(),
    removeCheckpoint: vi.fn(),
    setSettings: vi.fn(),
    thaw: vi.fn(),
    focusFreeze: vi.fn(),
  };
  act(() => root.render(React.createElement(CollabPanel, { scene: SCENE, presence: PRESENCE, dlProposals: DL_PROPOSALS, writable: true, theme: "light", actions, now: NOW, ...props })));
  mounted.push({ root, node });
  return { node, actions };
}

const section = (node, name) => node.querySelector(`[data-section="${name}"]`);
const button = (node, text) => [...node.querySelectorAll("button")].find((b) => b.textContent.trim().startsWith(text)) || null;
const typeInto = (input, value) => {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
};

describe("lists", () => {
  test("open proposals oldest first, with what the list says now", () => {
    expect(openProposals(SCENE, DL_PROPOSALS).map((p) => [p.id, p.outdated, p.summary[0]])).toEqual([
      ["P-3", false, "E-1 moved by c+2r0"],
      ["P-4", true, "E-2 text"],
    ]);
    const { node, actions } = mount();
    const rows = section(node, "proposals").querySelectorAll("[data-proposal]");
    expect([...rows].map((r) => r.getAttribute("data-proposal"))).toEqual(["P-3", "P-4"]);
    expect(rows[1].textContent).toContain("outdated");
    act(() => rows[0].querySelector("button").click());
    expect(actions.openProposal).toHaveBeenCalledWith("P-3");
  });

  test("agents: members from the scene and presence, with status and intent", () => {
    expect(agentRows(SCENE, PRESENCE).map((r) => [r.name, r.here ? r.here.status : null])).toEqual([
      ["drawer", null],
      ["peer", "waiting"],
    ]);
    const { node } = mount();
    expect(section(node, "agents").textContent).toContain("needs the numbers");
  });
});

describe("revert all by <name>", () => {
  test("the three kinds of since, each sending undo {author, since}", () => {
    const { node, actions } = mount();
    const drawer = node.querySelector('[data-agent="drawer"]');
    act(() => button(drawer, "revert").click());
    const choices = [...drawer.querySelectorAll("[data-choice]")].map((b) => b.getAttribute("data-choice"));
    expect(choices).toEqual(["recent", "since:V-2", "since:V-1", "all"]);
    act(() => drawer.querySelector('[data-choice="recent"]').click());
    expect(actions.revert).toHaveBeenLastCalledWith("drawer", 9, "in the last 10 minutes");
    act(() => button(drawer, "revert").click());
    act(() => drawer.querySelector('[data-choice="since:V-1"]').click());
    expect(actions.revert).toHaveBeenLastCalledWith("drawer", 2, "since checkpoint V-1");
    act(() => button(drawer, "revert").click());
    act(() => drawer.querySelector('[data-choice="all"]').click());
    expect(actions.revert).toHaveBeenLastCalledWith("drawer", null, "everything");
  });

  test("a choice with nothing to revert is disabled", () => {
    const { node } = mount();
    const peer = node.querySelector('[data-agent="peer"]');
    act(() => button(peer, "revert").click());
    expect(peer.querySelector('[data-choice="all"]').disabled).toBe(true);
    expect(peer.querySelector('[data-choice="recent"]')).toBeNull();
  });
});

describe("checkpoints", () => {
  test("save with a label, restore and remove", () => {
    const { node, actions } = mount();
    const box = section(node, "checkpoints");
    const save = button(box, "Save checkpoint");
    expect(save.disabled).toBe(true);
    act(() => typeInto(box.querySelector("input"), "before pricing rework"));
    act(() => button(box, "Save checkpoint").click());
    expect(actions.saveCheckpoint).toHaveBeenCalledWith("before pricing rework");
    expect(box.querySelector("input").value).toBe("");
    const newest = box.querySelector('[data-checkpoint="V-2"]');
    expect(newest.textContent).toContain("auto");
    act(() => button(newest, "restore").click());
    expect(actions.restore).toHaveBeenCalledWith("V-2");
    act(() => button(box.querySelector('[data-checkpoint="V-1"]'), "remove").click());
    expect(actions.removeCheckpoint).toHaveBeenCalledWith("V-1");
  });
});

describe("settings", () => {
  test("two segmented controls showing the scene's values; a pick sends settings", () => {
    const { node, actions } = mount();
    const human = node.querySelector('[data-setting="human_edits"]');
    const frozen = node.querySelector('[data-setting="frozen"]');
    expect(human.querySelector('[aria-checked="true"]').getAttribute("data-value")).toBe("propose");
    expect(frozen.querySelector('[aria-checked="true"]').getAttribute("data-value")).toBe("refuse");
    act(() => human.querySelector('[data-value="live"]').click());
    expect(actions.setSettings).toHaveBeenCalledWith({ human_edits: "live" });
    act(() => frozen.querySelector('[data-value="refuse"]').click());
    expect(actions.setSettings).toHaveBeenCalledTimes(1); // already the value
  });

  test("defaults when the scene has none", () => {
    const { node } = mount({ scene: { ...SCENE, settings: {} } });
    expect(node.querySelector('[data-setting="frozen"] [aria-checked="true"]').getAttribute("data-value")).toBe("propose");
  });
});

describe("read-only", () => {
  test("lists, and no action", () => {
    const { node } = mount({ writable: false });
    expect(button(node, "revert")).toBeNull();
    expect(button(node, "Save checkpoint")).toBeNull();
    expect(button(node, "restore")).toBeNull();
    expect(button(node, "thaw")).toBeNull();
    for (const b of node.querySelectorAll(".cv2-segmented button")) expect(b.disabled).toBe(true);
    expect(section(node, "proposals").querySelectorAll("[data-proposal]")).toHaveLength(2);
  });
});

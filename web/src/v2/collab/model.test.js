// The collaboration layer's pure readers (canvas-v2-phase5.md 12): proposal and freeze entries,
// the review queue, a freeze's rim, the freeze gesture, the Revert notices, result lines, the QA
// counts, and the scene store's fold of the new change targets (I-7).
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { createSceneStore, foldEvents } from "../../sceneStore.js";
import {
  beginFreezeRegion,
  cameraFor,
  freezeAt,
  freezesOf,
  isOverlayId,
  proposalsOf,
  resultLines,
  reviewBox,
  reviewQueue,
  screenBox,
  touchedNotices,
  withoutOverlays,
} from "./model.js";
import { restoreSummary, undoSummary } from "./ops.js";
import { collabCounts } from "./qa.js";

const DL = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "__fixtures__", "display.json"), "utf8"));
// SERVER's display fixture (I-3), once it exists: the same readers must hold on it.
const SHARED = path.resolve(import.meta.dirname, "../../../../tests/fixtures/collab/display.json");

describe("entries", () => {
  test("proposals and freezes by id; overlays are never targets", () => {
    const p = proposalsOf(DL);
    expect([...p.keys()]).toEqual(["P-3", "P-4"]);
    expect(p.get("P-4").outdated).toBe(true);
    expect(p.get("P-3").bbox).toEqual([-6, -6, 206, 86]);
    expect(freezesOf(DL).map((e) => e.id)).toEqual(["X-5", "X-6"]);
    expect(isOverlayId("P-3") && isOverlayId("X-5") && isOverlayId("K-2")).toBe(true);
    expect(withoutOverlays(["E-1", "P-3", "C-2", "X-6"])).toEqual(["E-1", "C-2"]);
  });

  test("reviewing shows the ghost and its targets as they stand", () => {
    // P-3's ghost [-6,-6,206,86] and its target E-1 [0,0,160,80]; P-4's ghost and E-2.
    expect(reviewBox(DL, "P-3")).toEqual([-6, -6, 206, 86]);
    expect(reviewBox(DL, "P-4")).toEqual([294, 0, 466, 206]);
    expect(reviewBox(DL, "P-9")).toBeNull();
  });

  test("the review queue is oldest first", () => {
    expect(reviewQueue(DL)).toEqual(["P-3", "P-4"]);
    expect(reviewQueue(DL, { proposals: [{ id: "P-3", seq: 40 }, { id: "P-4", seq: 30 }] })).toEqual(["P-4", "P-3"]);
  });

  test.runIf(fs.existsSync(SHARED))("SERVER's display fixture reads the same way", () => {
    const doc = JSON.parse(fs.readFileSync(SHARED, "utf8"));
    const dl = doc.display || doc;
    for (const [id, p] of proposalsOf(dl)) {
      expect(id).toMatch(/^P-\d+$/);
      expect(typeof p.author).toBe("string");
      expect(Array.isArray(p.summary)).toBe(true);
      expect(typeof p.outdated).toBe("boolean");
    }
    for (const f of freezesOf(dl)) expect(f.bbox).toHaveLength(4);
  });
});

describe("geometry", () => {
  test("screen boxes and the camera that shows a box", () => {
    expect(screenBox([10, 20, 110, 70], { x: 0, y: 10, scale: 2 })).toEqual({ x: 20, y: 20, w: 200, h: 100 });
    const cam = cameraFor([0, 0, 200, 100], { w: 1000, h: 800 });
    expect(cam.scale).toBe(1.25);
    expect(cam.x + 1000 / (2 * cam.scale)).toBe(100);
  });

  test("a freeze opens from its rim or its label, not from its inside", () => {
    // X-6 [600,300,760,380] sits inside X-5 [580,280,1000,400]; X-6 is later, so on top.
    expect(freezeAt(DL, [600, 340], 1)).toBe("X-6");
    expect(freezeAt(DL, [1000, 340], 1)).toBe("X-5");
    expect(freezeAt(DL, [990, 275], 1)).toBe("X-5"); // the label band above the top-right (QA phase 5 L9)
    expect(freezeAt(DL, [620, 262], 1)).toBeNull(); // above the top-left: no label there any more
    expect(freezeAt(DL, [900, 350], 1)).toBeNull();
    expect(freezeAt(DL, [0, 0], 1)).toBeNull();
    // The rim is 6 screen px at any zoom.
    expect(freezeAt(DL, [1000 + 5 / 0.5, 340], 0.5)).toBe("X-5");
    expect(freezeAt(DL, [1000 + 5 / 2, 340], 2)).toBe("X-5");
    expect(freezeAt(DL, [1000 + 8 / 2, 340], 2)).toBeNull();
    // On an element only the label counts.
    expect(freezeAt(DL, [600, 340], 1, { rim: false })).toBeNull();
    expect(freezeAt(DL, [990, 275], 1, { rim: false })).toBe("X-5");
  });

  test("the freeze gesture: a drag becomes freeze {region}; a click only explains", () => {
    const ev = (x, y) => ({ world: [x, y], screen: [x, y] });
    const g = beginFreezeRegion(ev(10, 10));
    expect(g.update(ev(11, 11))).toBeNull();
    expect(g.update(ev(200, 120)).ghost.length).toBeGreaterThan(0);
    expect(g.finish(ev(200.5, 120.5))).toEqual({ ops: [{ op: "freeze", region: [10, 10, 201, 121] }] });
    const click = beginFreezeRegion(ev(10, 10));
    expect(click.finish(ev(10, 10)).toast).toMatch(/drag/);
  });
});

describe("notices", () => {
  const elements = { "E-4": { id: "E-4", text: "Pricing" }, "E-7": { id: "E-7", text: "" } };
  const elementOf = (id) => elements[id] || null;

  test("one Revert notice per batch that touched the operator's marks, from a member", () => {
    const seen = new Set();
    const events = [
      { seq: 20, batch: "B-40", author: { name: "alpha", kind: "member" }, touched_human: ["E-4"] },
      { seq: 21, batch: "B-40", author: { name: "alpha", kind: "member" }, touched_human: ["E-4"] },
      { seq: 22, batch: "B-41", author: { name: "human", kind: "human" }, touched_human: ["E-4"] },
      { seq: 23, batch: "B-42", author: { name: "beta", kind: "member" } },
      { seq: 24, batch: "B-43", author: { name: "beta", kind: "member" }, touched_human: ["E-4", "E-7"] },
    ];
    expect(touchedNotices(events, { elementOf, seen })).toEqual([
      { batch: "B-40", author: "alpha", ids: ["E-4"], text: "alpha changed your 'Pricing'" },
      { batch: "B-43", author: "beta", ids: ["E-4", "E-7"], text: "beta changed 2 of your marks (E-4, E-7)" },
    ]);
    // Never twice for one batch.
    expect(touchedNotices(events, { elementOf, seen })).toEqual([]);
    expect(touchedNotices([{ batch: "B-50", author: { name: "gamma", kind: "member" }, touched_human: ["E-7"] }], { elementOf })[0].text).toBe("gamma changed your E-7");
  });
});

describe("result lines", () => {
  const fns = { undoSummary, restoreSummary };
  test("proposals made, undo summaries, restore counts", () => {
    expect(resultLines({ applied: [], proposed: [{ index: 0, op: "move", proposal: "P-3", reason: "human_made", targets: ["E-4"] }] }, fns)).toEqual([
      { text: "#1 move became proposal P-3: it waits for review", tone: "info" },
    ]);
    expect(resultLines({ applied: [{ index: 0, op: "undo", undo: { batches: ["B-1"], restored: 3, of: 7, skipped: [{ id: "E-12", by: "human", seq: 415 }] } }] }, fns)).toEqual([
      { text: "3 of 7 reverted; E-12 edited by you later", tone: "warn" },
    ]);
    expect(resultLines({ applied: [{ index: 0, op: "restore", restore: { added: 1, changed: 0, deleted: 2 } }] }, fns)[0].text).toBe("restored: 1 put back, 0 changed back, 2 removed");
    expect(resultLines({ applied: [{ index: 0, op: "move" }] }, fns)).toEqual([]);
  });
});

// SERVER's apply-result fixtures (I-1), once they exist: the toasts the page makes of them.
const RESULTS = path.resolve(import.meta.dirname, "../../../../tests/fixtures/collab/results");
describe.runIf(fs.existsSync(RESULTS))("SERVER's result fixtures (I-1)", () => {
  const load = (name) => {
    const doc = JSON.parse(fs.readFileSync(path.join(RESULTS, `${name}.json`), "utf8"));
    return doc.result || doc;
  };
  const fns = { undoSummary, restoreSummary };
  test("proposed, undo and restore become toast lines", () => {
    const proposed = load("proposed");
    expect(resultLines(proposed, fns)[0].text).toMatch(/became proposal P-\d+/);
    const undo = load("undo");
    const line = resultLines(undo, fns)[0];
    const u = undo.applied.find((a) => a.undo).undo;
    expect(line.text.startsWith(`${u.restored} of ${u.of} reverted`)).toBe(true);
    expect(line.tone).toBe(u.skipped.length ? "warn" : "ok");
    expect(resultLines(load("restore"), fns)[0].text).toMatch(/^restored: /);
  });
  test("every refusal carries a code and a message the page can show", () => {
    for (const name of ["element_busy", "frozen", "in_proposal", "operator_only", "proposal_limit", "proposal_outdated", "stale_base_refused"]) {
      const r = load(name);
      const refused = r.refused || [];
      expect(refused.length, name).toBeGreaterThan(0);
      expect(typeof refused[0].code).toBe("string");
      expect(typeof refused[0].message).toBe("string");
    }
    expect(load("stale_base_warning").warnings.some((w) => w.code === "stale_base")).toBe(true);
  });
});

describe("QA counts (I-8)", () => {
  function fakeRoot(ids) {
    return { querySelectorAll: () => ids.map((id) => ({ getAttribute: () => id })) };
  }
  function fakeLayer(counts) {
    return { querySelectorAll: (sel) => ({ length: counts[sel] || 0 }) };
  }
  test("counts what is drawn, read against the list", () => {
    const counts = collabCounts({
      root: fakeRoot(["E-1", "E-2", "E-7", "E-8", "X-5", "P-3", "P-4"]),
      halos: fakeLayer({ ".cv2-halo": 2, ".cv2-operator-view": 1, ".cv2-operator-cursor": 1 }),
      dl: DL,
      reviewOpen: true,
      queue: 2,
    });
    expect(counts).toEqual({ halos: 2, operator_marks: 2, proposals: 2, outdated: 1, frozen: 2, freezes: 1, review_open: true, queue: 2 });
  });
});

describe("the scene store folds the new targets (I-7)", () => {
  test("proposal, freeze, checkpoint and setting changes land in their keys; undo marks batches", () => {
    const store = createSceneStore();
    store.replace({ version: 10, elements: [], batches: { "B-1": { author: "alpha", first_seq: 2, last_seq: 3, undone: false }, "B-2": { author: "alpha", first_seq: 4, last_seq: 4, undone: false } } });
    expect(store.get().proposals).toEqual([]);
    store.applyEvents([
      { seq: 11, op: "move", batch: "B-3", author: { name: "alpha" }, proposal: "P-1", changes: [{ target: "proposal", action: "add", id: "P-1", value: { id: "P-1", status: "open" } }] },
      { seq: 12, op: "freeze", batch: "B-4", author: { name: "human" }, changes: [{ target: "freeze", action: "add", id: "X-2", value: { id: "X-2", region: [0, 0, 10, 10] } }] },
      { seq: 13, op: "checkpoint", batch: "B-5", author: { name: "human" }, changes: [{ target: "checkpoint", action: "add", id: "V-1", value: { id: "V-1", version: 12 } }] },
      { seq: 14, op: "settings", batch: "B-6", author: { name: "human" }, changes: [{ target: "setting", action: "update", id: "collab", value: { human_edits: "live", frozen: "propose" } }] },
      { seq: 15, op: "undo", batch: "B-7", author: { name: "human" }, undoes_all: ["B-1", "B-2"], changes: [] },
      { seq: 16, op: "accept", batch: "B-8", author: { name: "human" }, accepts: "P-1", changes: [{ target: "proposal", action: "update", id: "P-1", value: { id: "P-1", status: "accepted" } }] },
    ]);
    const scene = store.get();
    expect(scene.proposals).toEqual([{ id: "P-1", status: "accepted" }]);
    expect(scene.freezes.map((f) => f.id)).toEqual(["X-2"]);
    expect(scene.checkpoints.map((c) => c.id)).toEqual(["V-1"]);
    expect(scene.settings.collab.human_edits).toBe("live");
    expect(scene.batches["B-1"].undone && scene.batches["B-2"].undone).toBe(true);
    const thawed = foldEvents(scene, [{ seq: 17, changes: [{ target: "freeze", action: "delete", id: "X-2" }] }]);
    expect(thawed.freezes).toEqual([]);
  });

  test("decided proposals are capped at 20; open ones are kept", () => {
    const proposals = Array.from({ length: 30 }, (_, i) => ({ id: `P-${i + 1}`, status: i === 0 ? "open" : "rejected" }));
    const scene = foldEvents({ ...createSceneStore().get(), proposals }, [{ seq: 1, changes: [{ target: "proposal", action: "add", id: "P-31", value: { id: "P-31", status: "open" } }] }]);
    expect(scene.proposals.filter((p) => p.status === "open").map((p) => p.id)).toEqual(["P-1", "P-31"]);
    expect(scene.proposals.filter((p) => p.status !== "open")).toHaveLength(20);
  });

  test("an older scene without the new keys still folds", () => {
    const store = createSceneStore();
    store.replace({ version: 1, elements: [] });
    store.applyEvents([{ seq: 2, changes: [{ target: "mystery", action: "add", id: "Z-1", value: {} }] }]);
    expect(store.get().version).toBe(2);
  });
});

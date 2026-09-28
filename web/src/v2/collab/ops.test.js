// The collaboration op builders (canvas-v2-phase5.md 11.2 I-2): every op's exact shape, and null
// for anything malformed, so the page never sends what the server would only refuse.
import { describe, expect, test } from "vitest";
import {
  buildAccept,
  buildCheckpoint,
  buildCheckpointRemove,
  buildFreezeIds,
  buildFreezeRegion,
  buildReject,
  buildRestore,
  buildSettings,
  buildThaw,
  buildThawIds,
  buildUndoAuthor,
  buildUndoBatch,
  buildWithdraw,
  oneLine,
  restoreSummary,
  undoSummary,
} from "./ops.js";
import { revertChoices } from "./revert.js";

describe("I-2 builders", () => {
  test("accept and reject {id, note?}", () => {
    expect(buildAccept("P-3")).toEqual({ op: "accept", id: "P-3" });
    expect(buildAccept("P-3", "  looks right\n")).toEqual({ op: "accept", id: "P-3", note: "looks right" });
    expect(buildReject("P-4", "keep it\nred")).toEqual({ op: "reject", id: "P-4", note: "keep it red" });
    expect(buildReject("P-4", "   ")).toEqual({ op: "reject", id: "P-4" });
    expect(buildAccept("E-3")).toBeNull();
    expect(buildReject("P-0")).toBeNull();
  });

  test("withdraw {id}", () => {
    expect(buildWithdraw("P-12")).toEqual({ op: "withdraw", id: "P-12" });
    expect(buildWithdraw("X-1")).toBeNull();
  });

  test("freeze {ids, label?}: element ids only, deduplicated", () => {
    expect(buildFreezeIds(["E-1", "P-3", "E-1", "C-2", "X-5"], "final layout")).toEqual({ op: "freeze", ids: ["E-1", "C-2"], label: "final layout" });
    expect(buildFreezeIds(["E-4"])).toEqual({ op: "freeze", ids: ["E-4"] });
    expect(buildFreezeIds(["P-3"])).toBeNull();
    expect(buildFreezeIds([])).toBeNull();
  });

  test("freeze {region, label?}: normalised and rounded outwards", () => {
    expect(buildFreezeRegion([200.4, 120.6, 10.2, 10.9])).toEqual({ op: "freeze", region: [10, 10, 201, 121] });
    expect(buildFreezeRegion([0, 0, 100, 50], "hold")).toEqual({ op: "freeze", region: [0, 0, 100, 50], label: "hold" });
    expect(buildFreezeRegion([5, 5, 5, 40])).toBeNull();
    expect(buildFreezeRegion([0, 0, NaN, 1])).toBeNull();
  });

  test("thaw {id} and thaw {ids}", () => {
    expect(buildThaw("X-6")).toEqual({ op: "thaw", id: "X-6" });
    expect(buildThaw("E-6")).toBeNull();
    expect(buildThawIds(["E-12", "E-13", "E-12"])).toEqual({ op: "thaw", ids: ["E-12", "E-13"] });
    expect(buildThawIds(["X-6"])).toBeNull();
  });

  test("settings {human_edits?, frozen?}: known values only", () => {
    expect(buildSettings({ human_edits: "live" })).toEqual({ op: "settings", human_edits: "live" });
    expect(buildSettings({ frozen: "refuse" })).toEqual({ op: "settings", frozen: "refuse" });
    expect(buildSettings({ human_edits: "propose", frozen: "propose" })).toEqual({ op: "settings", human_edits: "propose", frozen: "propose" });
    expect(buildSettings({ human_edits: "always" })).toBeNull();
    expect(buildSettings({})).toBeNull();
  });

  test("checkpoint {label} and checkpoint {remove}; restore {id}", () => {
    expect(buildCheckpoint("before pricing rework")).toEqual({ op: "checkpoint", label: "before pricing rework" });
    expect(buildCheckpoint("  ")).toBeNull();
    expect(buildCheckpoint("x".repeat(300)).label).toHaveLength(120);
    expect(buildCheckpointRemove("V-3")).toEqual({ op: "checkpoint", remove: "V-3" });
    expect(buildCheckpointRemove("P-3")).toBeNull();
    expect(buildRestore("V-3")).toEqual({ op: "restore", id: "V-3" });
    expect(buildRestore("V-x")).toBeNull();
  });

  test("undo {batch, force?} and undo {author, since?, force?}", () => {
    expect(buildUndoBatch("B-40")).toEqual({ op: "undo", batch: "B-40" });
    expect(buildUndoBatch("B-40", { force: true })).toEqual({ op: "undo", batch: "B-40", force: true });
    expect(buildUndoBatch("E-40")).toBeNull();
    expect(buildUndoAuthor("drawer", 405)).toEqual({ op: "undo", author: "drawer", since: 405 });
    expect(buildUndoAuthor("drawer")).toEqual({ op: "undo", author: "drawer" });
    expect(buildUndoAuthor("human", 0, { force: true })).toEqual({ op: "undo", author: "human", since: 0, force: true });
    expect(buildUndoAuthor("drawer", -1)).toBeNull();
    expect(buildUndoAuthor("drawer", 1.5)).toBeNull();
    expect(buildUndoAuthor("../etc")).toBeNull();
  });

  test("oneLine keeps one line of at most the cap", () => {
    expect(oneLine(" a\r\n b\tc ")).toBe("a b c");
    expect(oneLine("é".repeat(200), 5)).toBe("ééééé");
  });
});

describe("revert choices (12.4)", () => {
  const now = Date.parse("2026-09-28T10:30:00Z");
  const scene = {
    batches: {
      "B-1": { author: "drawer", first_seq: 3, last_seq: 4, at: "2026-09-28T09:00:00Z", undone: false },
      "B-2": { author: "drawer", first_seq: 10, last_seq: 12, at: "2026-09-28T10:22:00Z", undone: false },
      "B-3": { author: "peer", first_seq: 13, last_seq: 13, at: "2026-09-28T10:25:00Z", undone: false },
      "B-4": { author: "drawer", first_seq: 14, last_seq: 15, at: "2026-09-28T10:28:00Z", undone: false },
      "B-5": { author: "drawer", first_seq: 16, last_seq: 16, at: "2026-09-28T10:29:00Z", undone: true },
    },
    checkpoints: [{ id: "V-2", label: "before", version: 11, auto: false }],
  };

  test("recent, since each checkpoint, everything", () => {
    const choices = revertChoices(scene, "drawer", now);
    expect(choices.map((c) => [c.key, c.since, c.count])).toEqual([
      ["recent", 9, 2],
      ["since:V-2", 11, 1],
      ["all", null, 3],
    ]);
  });

  test("no recent choice when the author drew nothing in the last 10 minutes", () => {
    expect(revertChoices(scene, "drawer", now + 3600 * 1000).map((c) => c.key)).toEqual(["since:V-2", "all"]);
  });
});

describe("result summaries", () => {
  test("undo: restored of, and the first skip", () => {
    expect(undoSummary({ batches: ["B-40"], restored: 7, of: 7, skipped: [] })).toBe("7 of 7 reverted");
    expect(undoSummary({ batches: ["B-40", "B-37"], restored: 3, of: 7, skipped: [{ id: "E-12", by: "human", seq: 415 }, { id: "E-13", by: "peer", seq: 416 }] })).toBe(
      "3 of 7 reverted; E-12 edited by you later (and 1 more)",
    );
    expect(undoSummary({ restored: 1, of: 2, skipped: [{ id: "E-2", by: "peer", seq: 9 }] })).toBe("1 of 2 reverted; E-2 edited by peer later");
    // An agent's undo leaves what a freeze holds (QA phase 5 H1).
    expect(undoSummary({ restored: 0, of: 1, skipped: [{ id: "E-10", reason: "frozen", freeze: "X-3" }] })).toBe("0 of 1 reverted; E-10 is frozen");
    expect(undoSummary(null)).toBe("");
  });

  test("restore counts", () => {
    expect(restoreSummary({ added: 2, changed: 1, deleted: 4 })).toBe("restored: 2 put back, 1 changed back, 4 removed");
  });
});

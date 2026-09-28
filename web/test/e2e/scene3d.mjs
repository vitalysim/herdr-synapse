// The 3D page scenarios (canvas-v2-phase3-4.md 8.4, S1 to S9; S10 is run.mjs's CSP check over every
// scenario). Each runs on a tools/canvas_rig.py scene: `scene3d` and `charts3d` are the Python side's
// (tests/fixtures/canvas_scenes/), `viz8` is this folder's (scenes/viz8.json, eight live visuals).
// Scenes and charts are found by the display list's generic fields (kind, the slot primitive),
// never by ids or positions.
//
// What the page reports comes from window.__synapseV2.scene3d() (web/src/v2/scene3d/qa.js):
// {contexts, renderer, scenes: [{id, visible, rendered, renders, tris, modelTris, expectTris,
// entered, stills}], budget}, window.__synapseV2.scene3dProbe() (the share of each drawn scene's
// pixels that differ from its paper) and window.__synapseV2.charts() for the GL charts.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { MOD, sleep } from "./cdp.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const center = (box) => [box[0] + box[2] / 2, box[1] + box[3] / 2];

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

function slotOf(entry) {
  const walk = (items) => {
    for (const p of items || []) {
      if (p && p.k === "slot") return p;
      if (p && p.k === "group") {
        const inner = walk(p.items);
        if (inner) return inner;
      }
    }
    return null;
  };
  return walk(entry.items);
}

const slotBox = (p) => [p.x, p.y, p.w, p.h];

async function sceneEntries(b) {
  const dl = await b.dl();
  const scenes = (dl.entries || []).filter((e) => slotOf(e) && slotOf(e).slot === "scene3d");
  assert(scenes.length > 0, "no scene3d entries in the display list");
  return scenes;
}

const qa = (b) => b.page.eval("window.__synapseV2.scene3d()");
const rowOf = (report, id) => report.scenes.find((s) => s.id === id) || null;

// Waits until every scene on screen is drawn; returns the report.
async function scenesDrawn(b, ids, what = "every scene to draw") {
  const list = JSON.stringify(ids);
  await b.page.waitFor(
    `(() => { const r = window.__synapseV2.scene3d && window.__synapseV2.scene3d(); if (!r) return false;
      return ${list}.every((id) => r.scenes.some((s) => s.id === id && s.rendered)); })()`,
    { timeoutMs: 30000, what },
  );
  return qa(b);
}

// S1: one WebGL context for every scene; each visible scene's rectangle has ink.
async function s1OneContext(b) {
  const scenes = await sceneEntries(b);
  const ids = scenes.map((e) => e.id);
  const report = await scenesDrawn(b, ids);
  assert(report.contexts === 1, `expected 1 WebGL context, scene3d() says ${report.contexts} (${JSON.stringify(report.budget)})`);
  const canvases = await b.page.eval(`document.querySelectorAll("canvas.sv2-gl").length`);
  assert(canvases === 1, `expected one shared canvas, found ${canvases}`);
  const ink = await b.page.eval("window.__synapseV2.scene3dProbe()");
  const blank = ids.filter((id) => !(ink[id] > 0.01));
  assert(!blank.length, `scenes with no pixels of their own: ${JSON.stringify(blank)} (${JSON.stringify(ink)})`);
  return `${ids.length} scenes on 1 context; ink ${ids.map((id) => `${id} ${Math.round(ink[id] * 100)}%`).join(", ")}`;
}

// S2: stills iso, front and top are posted once per scene version (writable).
async function s2Stills(b) {
  const scenes = await sceneEntries(b);
  await scenesDrawn(b, scenes.map((e) => e.id));
  const want = scenes.length * 3;
  const stillPosts = () => b.since(0, { method: "POST" }).filter((r) => r.done && /\/stills\//.test(new URL(r.url).pathname));
  for (let until = Date.now() + 30000; stillPosts().length < want && Date.now() < until; ) await sleep(200);
  await sleep(1500); // a second POST of the same still would arrive by now
  const stills = stillPosts();
  const seen = new Map();
  for (const r of stills) {
    const url = new URL(r.url);
    const id = decodeURIComponent(url.pathname.split("/stills/")[1]);
    const key = `${id}@${url.searchParams.get("v")}@${url.searchParams.get("view")}`;
    seen.set(key, (seen.get(key) || 0) + 1);
  }
  for (const e of scenes) {
    for (const view of ["iso", "front", "top"]) {
      const n = seen.get(`${e.id}@${e.v}@${view}`) || 0;
      assert(n === 1, `${e.id} v${e.v} ${view}: ${n} still POSTs (all: ${JSON.stringify([...seen])})`);
    }
  }
  const failed = stills.filter((r) => r.status !== 200 && r.status !== 204);
  assert(!failed.length, `still POSTs refused: ${failed.map((r) => `${r.url} ${r.status}`).join(", ")}`);
  return `${stills.length} stills (${scenes.length} scenes × iso, front, top), each once`;
}

// S3: a scene panned off screen stops rendering (the board culls its slot, or the renderer skips
// its rectangle); back on screen it renders again.
async function s3Offscreen(b) {
  const scenes = await sceneEntries(b);
  await scenesDrawn(b, scenes.map((e) => e.id));
  const target = scenes[0];
  const bbox = await b.page.eval("window.__synapseV2.dl().bbox");
  const framesOf = async () => ((await qa(b)).renderer || { frames: null }).frames;
  // Look far to the right of everything.
  await b.page.eval(`window.__synapseV2.zoom(1, [${bbox[2] + 5000}, ${bbox[3] + 5000}]), true`);
  await sleep(700);
  const before = rowOf(await qa(b), target.id);
  const frames = await framesOf();
  await sleep(1000);
  const after = rowOf(await qa(b), target.id);
  // Culled: the slot unmounted, so nothing of it can render. Still mounted: its count is flat.
  assert(!before === !after, `${target.id} came and went while nothing moved`);
  if (after) {
    assert(after.renders === before.renders, `${target.id} rendered off screen: ${before.renders} -> ${after.renders}`);
    assert(!after.visible, `${target.id} still reports visible off screen`);
  }
  const idle = await framesOf();
  assert(idle === frames, `the renderer kept drawing while nothing moved (${frames} -> ${idle} frames)`);
  await b.fit();
  await b.page.waitFor(`(() => { const r = window.__synapseV2.scene3d().scenes.find((s) => s.id === ${JSON.stringify(target.id)}); return !!r && r.visible && r.rendered && r.renders > ${after ? after.renders : 0}; })()`, { timeoutMs: 20000, what: "the scene to render again on screen" });
  const back = rowOf(await qa(b), target.id);
  const held = after ? `${after.renders} renders held off screen for 1 s` : "culled off screen (unmounted, 0 renders)";
  return `${target.id}: ${held}, ${back.renders} after it came back; idle frames flat (${frames})`;
}

// S3b: a camera change with no pointer event (keyboard zoom, a programmatic zoom) redraws the
// shared canvas: every drawn scene's last frame is at its placeholder's current rectangle, not
// left painted at the old camera over other cards (QA phase34 H1).
async function s3bKeyZoom(b) {
  const scenes = await sceneEntries(b);
  const ids = scenes.map((e) => e.id);
  await b.fit();
  await scenesDrawn(b, ids);
  await sleep(800); // the loop goes idle
  const framesOf = async () => ((await qa(b)).renderer || { frames: null }).frames;
  const stale = async () =>
    b.page.eval(`(() => { const r = window.__synapseV2.scene3d(); const out = [];
      for (const s of r.scenes) {
        if (!s.rendered || !s.rect) continue;
        const el = document.querySelector('[data-slot="scene3d"][data-id="' + CSS.escape(s.id) + '"]');
        if (!el) continue;
        const d = el.getBoundingClientRect();
        const want = [d.left, d.top, d.width, d.height].map((v) => Math.round(v));
        if (want.some((v, i) => Math.abs(v - s.rect[i]) > 1)) out.push(s.id + " drawn at " + s.rect.join(",") + " but placed at " + want.join(","));
      }
      return out; })()`);
  const idle = await framesOf();
  const mod = process.platform === "darwin" ? MOD.meta : MOD.ctrl;
  await b.page.key("-", { modifiers: mod });
  await b.page.key("-", { modifiers: mod });
  await sleep(800);
  const afterKey = await framesOf();
  assert(afterKey > idle, `keyboard zoom drew no frame (${idle} -> ${afterKey})`);
  const keyStale = await stale();
  assert(!keyStale.length, `stale 3D frames after keyboard zoom: ${keyStale.join("; ")}`);
  const bbox = await b.page.eval("window.__synapseV2.dl().bbox");
  await b.page.eval(`window.__synapseV2.zoom(1.1, [${(bbox[0] + bbox[2]) / 2}, ${(bbox[1] + bbox[3]) / 2}]), true`);
  await sleep(800);
  const afterHook = await framesOf();
  assert(afterHook > afterKey, `a programmatic zoom drew no frame (${afterKey} -> ${afterHook})`);
  const hookStale = await stale();
  assert(!hookStale.length, `stale 3D frames after a programmatic zoom: ${hookStale.join("; ")}`);
  await b.fit();
  return `frames ${idle} -> ${afterKey} (Cmd - twice) -> ${afterHook} (zoom hook); every drawn scene at its placeholder`;
}

// S4: enter, orbit (no POST), Save view (patch set camera with if_version), Esc leaves.
async function s4EnterOrbitSave(b) {
  const scenes = await sceneEntries(b);
  await scenesDrawn(b, scenes.map((e) => e.id));
  const entry = scenes[0];
  const slot = slotOf(entry);
  await b.page.key("Escape");
  await b.zoomToShow([slot.x, slot.y, slot.x + slot.w, slot.y + slot.h]);
  const at = await b.toScreen(center(slotBox(slot)));
  await b.page.dblclick(at);
  await b.page.waitFor(`window.__synapseV2.scene3d().scenes.some((s) => s.id === ${JSON.stringify(entry.id)} && s.entered)`, { what: "the scene to be entered" });
  const rendersBefore = rowOf(await qa(b), entry.id).renders;
  const mark = b.mark();
  await b.page.drag(at, [at[0] + 120, at[1] + 30], { steps: 10 });
  await sleep(400);
  const opsAfterOrbit = b.since(mark, { method: "POST", suffix: "/ops" });
  assert(!opsAfterOrbit.length, `orbiting sent ${opsAfterOrbit.length} op POST(s)`);
  const rendersAfter = rowOf(await qa(b), entry.id).renders;
  assert(rendersAfter > rendersBefore, `the orbit drew nothing new (${rendersBefore} -> ${rendersAfter} renders)`);
  const still = await b.page.eval(`window.__synapseV2.scene3d().scenes.find((s) => s.id === ${JSON.stringify(entry.id)}).entered`);
  assert(still, "the orbit drag left the scene");
  const button = await b.page.eval(`(() => { const n = document.querySelector('.sv2-scene3d[data-id=${JSON.stringify(entry.id)}] .sv2-save-view'); if (!n) return null; const r = n.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`);
  assert(button, "no Save view button on the entered scene");
  const saveMark = b.mark();
  await b.page.click(button);
  const { ops, result } = await b.opsPost(saveMark);
  const op = ops[0];
  assert(ops.length === 1 && op.op === "patch" && op.id === entry.id && op.set && op.set.camera && op.set.camera.preset === "orbit", `expected patch {set: {camera}}, sent ${JSON.stringify(ops)}`);
  assert(Number.isFinite(op.if_version), "Save view sent no if_version");
  assert(Number.isFinite(op.set.camera.az) && Number.isFinite(op.set.camera.el), `camera without angles: ${JSON.stringify(op.set.camera)}`);
  assert(!(result.refused || []).length, `Save view refused: ${JSON.stringify(result.refused)}`);
  await b.page.key("Escape");
  await b.page.waitFor(`!window.__synapseV2.scene3d().scenes.some((s) => s.entered)`, { what: "Esc to leave the scene" });
  return `entered ${entry.id}, orbit drew ${rendersAfter - rendersBefore} frame(s) with no POST, saved camera ${JSON.stringify(op.set.camera)} (if_version ${op.if_version}), Esc left`;
}

// S5: a textured glTF model loads; its triangles are the server's count; no CSP violation.
async function s5Model(b) {
  const scenes = await sceneEntries(b);
  const drawn = await scenesDrawn(b, scenes.map((e) => e.id));
  assert(drawn.scenes.some((s) => s.expectTris > 0), "no scene in this rig has a glTF model (the scene3d scene's glTF example needs artifacts/ seeded by the rig)");
  await b.page.waitFor(`window.__synapseV2.scene3d().scenes.some((s) => s.expectTris > 0 && !s.loading && (s.models > 0 || s.failed))`, { timeoutMs: 30000, what: "a model to load" });
  const rows = (await qa(b)).scenes.filter((s) => s.expectTris > 0);
  assert(rows.length, "no scene with a glTF model");
  for (const r of rows) {
    assert(!r.failed, `${r.id}: the model failed to load: ${JSON.stringify(r.failed)}`);
    assert(r.models > 0 && r.modelTris === r.expectTris, `${r.id}: ${r.modelTris} triangles loaded, the server counted ${r.expectTris}`);
  }
  const csp = await b.page.eval("window.__cspViolations || []");
  assert(!csp.length, `CSP violations: ${csp.join("; ")}`);
  return rows.map((r) => `${r.id}: ${r.models} model(s), ${r.modelTris} triangles as solved`).join("; ");
}

// S6: bar3d, scatter3d and surface render static (a PNG in the slot); entering all three in turn
// never holds more than 2 live GL contexts; no CSP violation (the claygl patch, in a browser).
async function s6GLCharts(b) {
  const dl = await b.dl();
  const charts = (dl.entries || []).filter((e) => e.kind === "chart" && slotOf(e) && slotOf(e).gl);
  assert(charts.length >= 3, `expected the three GL charts, found ${charts.length}`);
  const ids = JSON.stringify(charts.map((e) => e.id));
  await b.page.waitFor(`(() => { const rows = window.__synapseV2.charts ? window.__synapseV2.charts() : []; return ${ids}.every((id) => rows.some((r) => r.id === id && (r.rendered || r.failed))); })()`, { timeoutMs: 60000, what: "the GL charts to render" });
  const rows = await b.page.eval("window.__synapseV2.charts()");
  const failed = rows.filter((r) => charts.some((e) => e.id === r.id) && (!r.rendered || r.failed));
  assert(!failed.length, `GL charts not drawn: ${JSON.stringify(failed)}`);
  const pngs = await b.page.eval(`${ids}.map((id) => !![...document.querySelectorAll('svg.sv2-svg g[data-id="' + id + '"] image')].find((n) => /^data:image\\/png/.test(n.getAttribute("href") || "")))`);
  assert(pngs.every(Boolean), `a GL chart shows no PNG picture: ${JSON.stringify(pngs)}`);
  let most = 0;
  for (const e of charts) {
    const slot = slotOf(e);
    await b.page.key("Escape");
    await b.zoomToShow([slot.x, slot.y, slot.x + slot.w, slot.y + slot.h]);
    await b.page.dblclick(await b.toScreen(center(slotBox(slot))));
    await sleep(2500);
    const live = (await qa(b)).budget["echarts-gl"].live;
    most = Math.max(most, live);
    assert(live <= 2, `${live} live GL chart contexts after entering ${e.id}`);
  }
  await b.page.key("Escape");
  const csp = await b.page.eval("window.__cspViolations || []");
  assert(!csp.length, `CSP violations: ${csp.join("; ")}`);
  return `${charts.length} GL charts drawn as PNGs; at most ${most} live GL context(s) while entering each; no CSP violation`;
}

// S7: with 8 live visuals, at most 6 frames are live; the others show their still; selecting one
// brings it back.
async function s7VizBudget(b) {
  await b.page.waitFor(`document.querySelectorAll(".sv2-viz").length >= 8`, { timeoutMs: 20000, what: "8 viz slots" });
  await sleep(500);
  const counts = await b.page.eval(`[document.querySelectorAll(".sv2-viz").length, document.querySelectorAll(".sv2-viz[data-live='1'] iframe").length, document.querySelectorAll(".sv2-viz[data-live='0']").length]`);
  assert(counts[1] <= 6, `${counts[1]} live viz iframes (limit 6)`);
  assert(counts[2] >= counts[0] - 6, `${counts[2]} paused of ${counts[0]}`);
  const paused = await b.page.eval(`(() => { const n = document.querySelector(".sv2-viz[data-live='0']"); return n ? n.getAttribute("data-id") : null; })()`);
  assert(paused, "no paused viz frame");
  const entry = await b.entry((e) => e.id === paused);
  await b.page.key("Escape");
  await b.page.click(await b.toScreen(center(entry.hit.box)));
  await b.page.waitFor(`!!document.querySelector('.sv2-viz[data-id=${JSON.stringify(paused)}][data-live="1"] iframe')`, { what: `${paused} to come back live` });
  const live = await b.page.eval(`document.querySelectorAll(".sv2-viz[data-live='1'] iframe").length`);
  assert(live <= 6, `${live} live viz iframes after bringing one back`);
  return `${counts[0]} viz: ${counts[1]} live, ${counts[2]} paused; selecting ${paused} brought it back (${live} live)`;
}

// S8: losing the WebGL context shows the stills; restoring it draws every scene again, once.
async function s8ContextLoss(b) {
  const scenes = await sceneEntries(b);
  await scenesDrawn(b, scenes.map((e) => e.id));
  await b.page.eval(`(() => { const c = document.querySelector("canvas.sv2-gl"); const gl = c.getContext("webgl2") || c.getContext("webgl"); window.__loseCtx = gl.getExtension("WEBGL_lose_context"); window.__loseCtx.loseContext(); return true; })()`);
  await b.page.waitFor(`(() => { const r = window.__synapseV2.scene3d(); return r.renderer.lost && r.scenes.every((s) => !s.rendered); })()`, { what: "the scenes to fall back to their stills" });
  const underlay = await b.page.eval(`${JSON.stringify(scenes.map((e) => e.id))}.every((id) => !!document.querySelector('svg.sv2-svg g[data-id="' + id + '"] g[data-slot="scene3d"] > *'))`);
  assert(underlay, "a scene shows nothing beneath its canvas while the context is lost");
  await b.page.eval("window.__loseCtx.restoreContext(), true");
  await b.page.waitFor(`(() => { const r = window.__synapseV2.scene3d(); return !r.renderer.lost && r.renderer.restores === 1 && r.scenes.filter((s) => s.visible).every((s) => s.rendered); })()`, { timeoutMs: 20000, what: "the scenes to draw again after the restore" });
  return `lost: ${scenes.length} scenes showed their stills; restored once and drew again`;
}

// S9: a read-only page posts nothing and has no Save view button.
async function s9ReadOnly(b) {
  const scenes = await sceneEntries(b);
  await scenesDrawn(b, scenes.map((e) => e.id));
  const entry = scenes[0];
  const slot = slotOf(entry);
  await b.zoomToShow([slot.x, slot.y, slot.x + slot.w, slot.y + slot.h]);
  await b.page.dblclick(await b.toScreen(center(slotBox(slot))));
  await sleep(1500);
  const button = await b.page.eval(`!!document.querySelector(".sv2-save-view")`);
  assert(!button, "a read-only page shows Save view");
  await b.page.key("Escape");
  await sleep(1500);
  const posts = b.since(0, { method: "POST" });
  assert(!posts.length, `a read-only page POSTed: ${posts.map((r) => new URL(r.url).pathname).join(", ")}`);
  return `no POST and no Save view across ${scenes.length} scenes`;
}

export const SCENE3D_SCENARIOS = [
  { rig: "scene3d", writable: true, list: [["S1", s1OneContext], ["S2", s2Stills], ["S3", s3Offscreen], ["S3b", s3bKeyZoom], ["S4", s4EnterOrbitSave], ["S5", s5Model]] },
  { rig: "charts3d", writable: true, list: [["S6", s6GLCharts]] },
  { rig: path.join(HERE, "scenes", "viz8.json"), label: "viz8", writable: true, list: [["S7", s7VizBudget]] },
  { rig: "scene3d", writable: true, list: [["S8", s8ContextLoss]] },
  { rig: "scene3d", writable: false, list: [["S9", s9ReadOnly]] },
];

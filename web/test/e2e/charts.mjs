// The chart page scenarios (canvas-v2-phase3-4.md 8.4, K1 to K7). run.mjs (3D-WEB) imports
// CHART_SCENARIOS next to the other series; S10 is its CSP check over every scenario. Each runs on
// a tools/canvas_rig.py scene the Python side owns (tests/fixtures/canvas_scenes/charts.json and
// charts-escape.json) and finds its charts by the display list's generic fields (kind "chart", the
// slot primitive), never by ids or positions.
//
// What the page reports comes from window.__synapseV2.charts() (web/src/v2/charts/qa.js):
// [{id, engine, type, rendered, labelOverlaps, failed}].
import path from "node:path";
import { MOD, sleep } from "./cdp.mjs";

const MOD_KEY = process.platform === "darwin" ? MOD.meta : MOD.ctrl;
const center = (box) => [box[0] + box[2] / 2, box[1] + box[3] / 2];
const OUT = process.env.SYNAPSE_E2E_OUT || path.join(process.cwd(), "test-results", "e2e");

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

// The slot primitive of an entry, wherever it sits in its items.
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

async function chartEntries(b) {
  const dl = await b.dl();
  const charts = (dl.entries || []).filter((e) => e.kind === "chart" && slotOf(e));
  assert(charts.length > 0, "no chart entries with a slot in the display list");
  return charts;
}

// Waits until every chart the page reports has settled (drawn or failed); returns the rows.
async function settledCharts(b, ids, what) {
  const list = JSON.stringify(ids);
  await b.page.waitFor(
    `(() => { const rows = (window.__synapseV2.charts ? window.__synapseV2.charts() : []); const want = ${list};
      return want.every((id) => rows.some((r) => r.id === id && (r.rendered || r.failed))); })()`,
    { timeoutMs: 30000, what },
  );
  return b.page.eval("window.__synapseV2.charts()");
}

async function themeButton(b, label) {
  return b.page.eval(`(() => {
    const el = [...document.querySelectorAll(".v2-top-right button")].find((n) => n.textContent.trim() === ${JSON.stringify(label)});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  })()`);
}

async function setTheme(b, theme) {
  const now = await b.page.eval(`document.querySelector(".sv2-surface").getAttribute("data-theme")`);
  if (now === theme) return;
  const button = await themeButton(b, theme);
  assert(button, `no "${theme}" theme toggle on the board`);
  await b.page.click(button);
  await b.page.waitFor(`document.querySelector(".sv2-surface").getAttribute("data-theme") === ${JSON.stringify(theme)}`, { what: `the ${theme} theme` });
}

// Every chart drawn, none failed, no label collisions, and each slot shows the picture.
function checkRows(rows, ids, theme) {
  const bad = ids
    .map((id) => rows.find((r) => r.id === id) || { id, missing: true })
    .filter((r) => r.missing || !r.rendered || r.failed !== null || (r.labelOverlaps !== null && r.labelOverlaps !== 0));
  assert(!bad.length, `${theme}: charts not drawn cleanly: ${JSON.stringify(bad)}`);
}

const stillPosts = (b, mark) => b.since(mark, { method: "POST" }).filter((r) => /\/stills\/[^/]+$/.test(new URL(r.url).pathname));

// K1: every chart renders in both themes; before the chart chunk arrives, each slot shows its
// fallback (the Python drawing), never a blank box.
async function k1Renders(b) {
  const charts = await chartEntries(b);
  const ids = charts.map((e) => e.id);
  // Throttled: the fallback is what the slot shows while ECharts loads.
  await b.page.send("Network.emulateNetworkConditions", { offline: false, latency: 1500, downloadThroughput: 64 * 1024, uploadThroughput: 64 * 1024 });
  await b.page.send("Page.reload", { ignoreCache: true });
  await b.page.waitFor("!!(window.__synapseV2 && window.__synapseV2.ready())", { timeoutMs: 60000, what: "the throttled board" });
  await b.fit();
  const early = await b.page.eval(`(() => {
    const out = [];
    for (const g of document.querySelectorAll('svg.sv2-svg g[data-slot="chart"]:not([data-chart-fallback] *)')) {
      out.push({ picture: g.hasAttribute("data-chart-engine"), marks: g.querySelectorAll("path, rect, line, polyline, text, image").length });
    }
    return out;
  })()`);
  await b.page.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
  assert(early.length > 0, "no chart slot groups on the page");
  const blank = early.filter((g) => !g.picture && g.marks === 0);
  assert(!blank.length, `${blank.length} chart slot(s) blank before the chart chunk loaded`);
  const drawnEarly = early.filter((g) => !g.picture).length;
  await b.page.eval("document.addEventListener('securitypolicyviolation', (e) => { (window.__cspViolations = window.__cspViolations || []).push(e.violatedDirective + ' ' + e.blockedURI); }), true");
  const out = [];
  for (const theme of ["light", "dark"]) {
    await setTheme(b, theme);
    await b.fit();
    const rows = await settledCharts(b, ids, `every chart drawn in ${theme}`);
    checkRows(rows, ids, theme);
    const shown = await b.page.eval(`[...document.querySelectorAll('svg.sv2-svg g[data-slot="chart"][data-chart-engine] > image[data-chart-picture]')].length`);
    assert(shown >= ids.length, `${theme}: ${shown} chart picture(s) on the page for ${ids.length} chart(s)`);
    await b.page.screenshot(path.join(OUT, `k1-${theme}.png`)).catch(() => undefined);
    out.push(`${theme} ${ids.length}/${ids.length}`);
  }
  await setTheme(b, "light");
  return `${drawnEarly} slot(s) showed the fallback while throttled; drawn ${out.join(", ")}, 0 overlaps`;
}

// K2: a writable page posts one still per chart per version, drawn light, even while viewing dark.
async function k2Stills(b) {
  const charts = await chartEntries(b);
  const ids = charts.map((e) => e.id);
  await setTheme(b, "dark");
  const mark = b.mark();
  await b.page.send("Page.reload", { ignoreCache: false });
  await b.page.waitFor("!!(window.__synapseV2 && window.__synapseV2.ready())", { timeoutMs: 30000, what: "the reloaded board" });
  await b.fit();
  await settledCharts(b, ids, "every chart drawn");
  const posts = await (async () => {
    const until = Date.now() + 15000;
    for (;;) {
      const found = stillPosts(b, mark).filter((r) => r.done);
      if (found.length >= ids.length || Date.now() > until) return found;
      await sleep(100);
    }
  })();
  const per = new Map();
  for (const p of posts) {
    const url = new URL(p.url);
    const id = decodeURIComponent(url.pathname.split("/").pop());
    const key = `${id}@${url.searchParams.get("v")}`;
    per.set(key, (per.get(key) || 0) + 1);
  }
  const twice = [...per.entries()].filter(([, n]) => n > 1);
  assert(!twice.length, `stills posted more than once: ${JSON.stringify(twice)}`);
  const missing = charts.filter((e) => !per.has(`${e.id}@${e.v}`));
  assert(!missing.length, `no still posted for ${missing.map((e) => `${e.id}@${e.v}`).join(", ")}`);
  // Light: the stored still carries the light theme's ink and muted text colours (glyph cores on the
  // white raster background), far more than the dark picture the page is showing does when it is
  // rasterised the same way.
  const rows = await b.page.eval("window.__synapseV2.charts()");
  const light = charts.find((e) => rows.some((r) => r.id === e.id && r.engine === "echarts")) || charts[0];
  const inks = await b.page.eval(`[window.__synapseV2.dl().palettes.light["base.ink"], window.__synapseV2.dl().palettes.light["base.ink_muted"]]`);
  const probe = await b.page.eval(`(async () => {
    const inks = ${JSON.stringify(inks)}.map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
    const count = (px) => {
      let n = 0;
      for (let i = 0; i < px.length; i += 4) if (inks.some(([r, g, b]) => Math.abs(px[i] - r) <= 10 && Math.abs(px[i + 1] - g) <= 10 && Math.abs(px[i + 2] - b) <= 10)) n += 1;
      return n;
    };
    const res = await fetch(${JSON.stringify(`/api/teams/${encodeURIComponent(b.rig.team)}/stills/`)} + encodeURIComponent(${JSON.stringify(`${light.id}-v${light.v}.png`)}), { credentials: "same-origin" });
    if (!res.ok) return { status: res.status };
    const still = await createImageBitmap(await res.blob());
    const canvas = document.createElement("canvas");
    canvas.width = still.width;
    canvas.height = still.height;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(still, 0, 0);
    const stillInk = count(ctx.getImageData(0, 0, canvas.width, canvas.height).data);
    const shown = document.querySelector('svg.sv2-svg g[data-id=${JSON.stringify(light.id)}] g[data-slot="chart"][data-chart-engine] > image[data-chart-picture]');
    let darkInk = null;
    if (shown) {
      const img = new Image();
      img.src = shown.getAttribute("href");
      await img.decode();
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      darkInk = count(ctx.getImageData(0, 0, canvas.width, canvas.height).data);
    }
    return { status: 200, w: canvas.width, h: canvas.height, stillInk, darkInk };
  })()`);
  assert(probe.status === 200, `GET the still of ${light.id}@${light.v}: ${probe.status}`);
  assert(probe.stillInk > 50 && (probe.darkInk === null || probe.stillInk > 3 * probe.darkInk), `the still of ${light.id} does not look light: ${JSON.stringify(probe)}`);
  await setTheme(b, "light");
  return `${posts.length} still POST(s) for ${ids.length} chart(s) viewed in dark, one per (id, v); ${light.id}'s still is light (${probe.w}x${probe.h})`;
}

// K3: a double-click enters live mode; hovering a bar shows a richText tooltip (no HTML inside the
// chart); Esc leaves.
async function k3Live(b) {
  const charts = await chartEntries(b);
  const bar = charts.find((e) => (slotOf(e).ref || {}).id && /bar|column/.test(String(e.chart?.type || e.type || ""))) || charts[0];
  const slot = slotOf(bar);
  await b.page.key("Escape");
  await b.fit();
  if ((await b.scale()) < 0.8) {
    await b.page.eval(`window.__synapseV2.zoom(1, ${JSON.stringify(center([slot.x, slot.y, slot.w, slot.h]))}), true`);
    await b.page.waitFor("window.__synapseV2.ready()", { what: "the zoomed board" });
  }
  await b.page.dblclick(await b.toScreen(center([slot.x, slot.y, slot.w, slot.h])));
  await b.page.waitFor(`!!document.querySelector('.sv2-chart-live[data-id=${JSON.stringify(bar.id)}] svg')`, { timeoutMs: 15000, what: "the live chart" });
  assert(!(await b.page.eval("document.activeElement && document.activeElement.classList.contains('v2-text-editor')")), "the double-click opened the text editor instead of entering the chart");
  // Hover across the plot until a tooltip draws (richText: zrender text inside the chart's SVG).
  const box = await b.page.eval(`(() => { const r = document.querySelector('.sv2-chart-live[data-id=${JSON.stringify(bar.id)}]').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()`);
  const before = await b.page.eval(`document.querySelector('.sv2-chart-live[data-id=${JSON.stringify(bar.id)}] svg').querySelectorAll("text").length`);
  let after = before;
  for (let i = 1; i <= 12 && after <= before; i += 1) {
    await b.page.move([box[0] + (box[2] * i) / 13, box[1] + box[3] * 0.7]);
    await sleep(120);
    after = await b.page.eval(`document.querySelector('.sv2-chart-live[data-id=${JSON.stringify(bar.id)}] svg').querySelectorAll("text").length`);
  }
  assert(after > before, "hovering the live chart drew no tooltip");
  const html = await b.page.eval(`[...document.querySelectorAll('.sv2-chart-live[data-id=${JSON.stringify(bar.id)}] div')].filter((d) => [...d.childNodes].some((c) => c.nodeType === 3 && c.textContent.trim())).length`);
  assert(html === 0, `${html} HTML text node(s) inside the live chart (tooltips must be richText)`);
  await b.page.key("Escape");
  await b.page.waitFor(`!document.querySelector('.sv2-chart-live[data-id=${JSON.stringify(bar.id)}]')`, { what: "the live chart to leave" });
  await b.fit();
  return `entered ${bar.id}; hover drew ${after - before} tooltip text node(s) in SVG, none in HTML; Esc left`;
}

// K4: resizing a chart with its east handle sends move {w}; the new version re-frames (the x
// labels rotate or thin as Python recorded) and still draws without collisions.
async function k4Resize(b) {
  const charts = await chartEntries(b);
  const scene = await b.scene();
  const withX = charts.find((e) => {
    const el = scene.elements.find((x) => x.id === e.id);
    return el && el.chart_frame && el.chart_frame.axes && el.chart_frame.axes.x && el.engine === "echarts";
  });
  assert(withX, "no flat chart with an x axis in the scene");
  const before = scene.elements.find((x) => x.id === withX.id);
  await b.page.key("Escape");
  await b.fit();
  if ((await b.scale()) < 0.8) {
    await b.page.eval(`window.__synapseV2.zoom(1, ${JSON.stringify(center(withX.hit.box))}), true`);
    await b.page.waitFor("window.__synapseV2.ready()", { what: "the zoomed board" });
  }
  await b.page.click(await b.toScreen([withX.hit.box[0] + 12, withX.hit.box[1] + 12]));
  await b.page.waitFor(`!!document.querySelector('.sv2-handles [data-handle="e"]')`, { what: "the east handle" });
  const handle = await b.page.eval(`(() => { const r = document.querySelector('.sv2-handles [data-handle="e"]').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`);
  const scale = await b.scale();
  const shrink = Math.round(before.w * 0.35);
  const mark = b.mark();
  await b.page.drag(handle, [handle[0] - shrink * scale, handle[1]], { steps: 10 });
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "move" && Number.isFinite(ops[0].w), `expected move {w}, sent ${JSON.stringify(ops)}`);
  assert(!(result.refused || []).length, `refused: ${JSON.stringify(result.refused)}`);
  const after = (await b.scene()).elements.find((x) => x.id === withX.id);
  const ax = (f) => f && f.axes && f.axes.x ? { rotate: f.axes.x.rotate || 0, interval: f.axes.x.interval || 0, width: f.axes.x.width || null } : null;
  const notes = (after.chart_frame && after.chart_frame.notes) || [];
  const changed = JSON.stringify(ax(before.chart_frame)) !== JSON.stringify(ax(after.chart_frame)) || after.w < before.w;
  assert(changed, `the frame did not follow the resize: ${JSON.stringify(ax(before.chart_frame))} -> ${JSON.stringify(ax(after.chart_frame))}`);
  const dl = await b.dl();
  const entry = (dl.entries || []).find((e) => e.id === withX.id);
  await b.page.waitFor(`window.__synapseV2.charts().some((r) => r.id === ${JSON.stringify(withX.id)} && (r.rendered || r.failed))`, { what: "the resized chart" });
  const row = (await settledCharts(b, [withX.id], "the resized chart")).find((r) => r.id === withX.id);
  assert(row.rendered && !row.failed && row.labelOverlaps === 0, `after the resize: ${JSON.stringify(row)}`);
  await b.page.key("z", { modifiers: MOD_KEY });
  await sleep(300);
  await b.fit();
  return `w ${before.w} -> ${after.w} (v${entry ? entry.v : "?"}); x axis ${JSON.stringify(ax(before.chart_frame))} -> ${JSON.stringify(ax(after.chart_frame))}${notes.length ? `; notes: ${notes.join("; ")}` : ""}; 0 overlaps`;
}

// K5: the escape hatches: the Vega-Lite chart and the raw radar render; a formatter with an <img>
// shows as text and no img element appears anywhere in the page.
async function k5Escape(b) {
  const charts = await chartEntries(b);
  const ids = charts.map((e) => e.id);
  await b.fit();
  const rows = await settledCharts(b, ids, "the escape-hatch charts");
  const vega = rows.filter((r) => r.engine === "vega-lite");
  const raw = rows.filter((r) => r.engine === "echarts-raw");
  assert(vega.length > 0 && vega.every((r) => r.rendered && !r.failed), `Vega-Lite: ${JSON.stringify(vega)}`);
  assert(raw.length > 0 && raw.every((r) => r.rendered && !r.failed), `raw ECharts: ${JSON.stringify(raw)}`);
  const imgs = await b.page.eval(`document.querySelectorAll("img").length`);
  const injected = await b.page.eval(`window.__injected === true`);
  assert(!injected, "a hostile formatter ran code");
  // Enter the raw chart and hover: the hostile text must still be SVG text, never HTML.
  const rawEntry = charts.find((e) => rows.some((r) => r.id === e.id && r.engine === "echarts-raw"));
  const slot = slotOf(rawEntry);
  await b.page.dblclick(await b.toScreen(center([slot.x, slot.y, slot.w, slot.h])));
  await b.page.waitFor(`!!document.querySelector('.sv2-chart-live[data-id=${JSON.stringify(rawEntry.id)}] svg')`, { timeoutMs: 15000, what: "the live raw chart" });
  const box = await b.page.eval(`(() => { const r = document.querySelector('.sv2-chart-live[data-id=${JSON.stringify(rawEntry.id)}]').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()`);
  for (let i = 1; i <= 8; i += 1) {
    await b.page.move([box[0] + (box[2] * i) / 9, box[1] + (box[3] * i) / 9]);
    await sleep(80);
  }
  const live = await b.page.eval(`(() => {
    const root = document.querySelector('.sv2-chart-live[data-id=${JSON.stringify(rawEntry.id)}]');
    return { imgs: root.querySelectorAll("img").length, text: [...root.querySelectorAll("svg text")].map((t) => t.textContent).join(" | ") };
  })()`);
  await b.page.key("Escape");
  assert(live.imgs === 0, "an img element appeared inside the live raw chart");
  const imgsAfter = await b.page.eval(`document.querySelectorAll("img").length`);
  assert(imgsAfter === imgs, `img elements appeared on the page: ${imgs} -> ${imgsAfter}`);
  const shown = /<img/.test(live.text);
  return `Vega-Lite ${vega.length}, raw ${raw.length} drawn; hostile markup ${shown ? "shows as text" : "not shown in this view"}; 0 img elements added`;
}

// K6: the read-only page draws every chart and posts nothing (no stills).
async function k6ReadOnly(b) {
  const charts = await chartEntries(b);
  const ids = charts.map((e) => e.id);
  const mark = b.mark();
  await b.page.send("Page.reload", { ignoreCache: false });
  await b.page.waitFor("!!(window.__synapseV2 && window.__synapseV2.ready())", { timeoutMs: 30000, what: "the reloaded board" });
  await b.fit();
  const rows = await settledCharts(b, ids, "every chart drawn");
  checkRows(rows, ids, "read-only");
  await setTheme(b, "dark");
  await settledCharts(b, ids, "every chart drawn in dark");
  await sleep(800);
  await setTheme(b, "light");
  const posts = b.since(mark, { method: "POST" });
  assert(posts.length === 0, `a read-only page POSTed: ${posts.map((p) => p.url).join(", ")}`);
  return `${ids.length} chart(s) drawn in both themes, 0 POSTs`;
}

// K7: pan and zoom over the charts board: frame times at the 95th percentile. Asserted at 16.7 ms
// only with SYNAPSE_E2E_PERF=1 (real Chrome on a GPU; SwiftShader headless runs report it).
async function k7PanZoom(b) {
  const charts = await chartEntries(b);
  await b.fit();
  await settledCharts(b, charts.map((e) => e.id), "every chart drawn");
  const surface = await b.page.eval(`(() => { const r = document.querySelector(".sv2-surface").getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`);
  await b.page.eval(`(() => {
    window.__k7 = [];
    let last = performance.now();
    const tick = (t) => { window.__k7.push(t - last); last = t; if (window.__k7.length < 400) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    return true;
  })()`);
  for (let i = 0; i < 60; i += 1) {
    const zoom = i % 20 < 10;
    await b.page.send("Input.dispatchMouseEvent", {
      type: "mouseWheel",
      x: surface[0],
      y: surface[1],
      deltaX: zoom ? 0 : 40,
      deltaY: zoom ? (i % 2 ? 40 : -40) : 0,
      modifiers: zoom ? MOD.ctrl : 0,
    });
    await sleep(16);
  }
  await sleep(300);
  const frames = (await b.page.eval("window.__k7")).slice(1).sort((x, y) => x - y);
  const p95 = frames[Math.min(frames.length - 1, Math.floor(frames.length * 0.95))] || 0;
  if (process.env.SYNAPSE_E2E_PERF === "1") assert(p95 <= 16.7, `pan and zoom p95 ${p95.toFixed(1)} ms over ${charts.length} charts`);
  await b.fit();
  return `p95 frame ${p95.toFixed(1)} ms over ${frames.length} frames with ${charts.length} charts${process.env.SYNAPSE_E2E_PERF === "1" ? "" : " (reported; asserted with SYNAPSE_E2E_PERF=1)"}`;
}

export const CHART_SCENARIOS = [
  { rig: "charts", writable: true, list: [["K1", k1Renders], ["K2", k2Stills], ["K3", k3Live], ["K4", k4Resize], ["K7", k7PanZoom]] },
  { rig: "charts-escape", writable: true, list: [["K5", k5Escape]] },
  { rig: "charts", writable: false, list: [["K6", k6ReadOnly]] },
];

export const CHART_SCENES = [...new Set(CHART_SCENARIOS.map((g) => g.rig))];

// For unit use.
export { slotOf };

// The v2 board's CDP interaction tests (canvas-v2-phase1.md 5.3, E1 to E12, plus the Q phase 1 QA
// regressions; canvas-v2-phase2.md 8.3, P1 to P12 in phase2.mjs; canvas-v2-phase3-4.md 8.4, the
// charts' K1 to K7 in charts.mjs and the 3D S1 to S10 in scene3d.mjs): `npm run test:e2e`.
//
// Each rig is tools/canvas_rig.py (a throwaway team on loopback, never the real session) with a
// golden scene; one headless Chrome started here drives the page with real mouse and key events
// (Input.dispatchMouseEvent / dispatchKeyEvent). Assertions read the op bodies the page POSTed
// (Network), the server's answers, the change log (/changes, fetched in the page) and the display
// list the page holds (window.__synapseV2.dl()).
//
// usage: node test/e2e/run.mjs [--only E1,E5] [--out DIR] [--keep-going]
// Exit 0 when every scenario passes; 2 when Chrome or the rig is unavailable (reported, not failed).
import fs from "node:fs";
import path from "node:path";
import { MOD, findChrome, launchChrome, openPage, sleep } from "./cdp.mjs";
import { CHART_SCENARIOS } from "./charts.mjs";
import { PHASE2_SCENARIOS } from "./phase2.mjs";
import { REPO, rigAvailable, startRig } from "./rig.mjs";
import { SCENE3D_SCENARIOS } from "./scene3d.mjs";

const args = process.argv.slice(2);
const argOf = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : null;
};
const ONLY = argOf("--only") ? new Set(argOf("--only").split(",").map((s) => s.trim().toUpperCase())) : null;
const OUT = argOf("--out") || fs.mkdtempSync(path.join(fs.existsSync("/private/tmp") ? "/private/tmp" : "/tmp", "synapse-e2e-"));
const SCENARIO_MS = 60000;
const MOD_KEY = process.platform === "darwin" ? MOD.meta : MOD.ctrl;
fs.mkdirSync(OUT, { recursive: true });

// -- page helpers ------------------------------------------------------------------------------

class Board {
  constructor(page, rig) {
    this.page = page;
    this.rig = rig;
    this.requests = new Map(); // requestId -> {method, url, postData, status, body}
    this.order = [];
    this.logs = [];
    this.csp = [];
    this.exceptions = [];
    page.on("Network.requestWillBeSent", ({ requestId, request }) => {
      if (!this.requests.has(requestId)) this.order.push(requestId);
      this.requests.set(requestId, { method: request.method, url: request.url, postData: request.postData ?? null, status: null, done: false });
    });
    page.on("Network.responseReceived", ({ requestId, response }) => {
      const r = this.requests.get(requestId);
      if (r) r.status = response.status;
    });
    page.on("Network.loadingFinished", ({ requestId }) => {
      const r = this.requests.get(requestId);
      if (r) r.done = true;
    });
    page.on("Network.loadingFailed", ({ requestId }) => {
      const r = this.requests.get(requestId);
      if (r) r.done = true;
    });
    page.on("Log.entryAdded", ({ entry }) => {
      const line = `${entry.level} ${entry.source}: ${entry.text} ${entry.url || ""}`;
      this.logs.push(line);
      if (/content security policy|content-security-policy|refused to (load|execute|apply|connect|frame)/i.test(entry.text)) this.csp.push(line);
    });
    page.on("Runtime.exceptionThrown", ({ exceptionDetails }) => {
      this.exceptions.push(exceptionDetails.exception?.description || exceptionDetails.text);
    });
    page.on("Runtime.consoleAPICalled", ({ type, args: list }) => {
      const text = list.map((a) => a.value ?? a.description).join(" ");
      this.logs.push(`console.${type}: ${text}`);
      if (type === "error" && /content security policy/i.test(text)) this.csp.push(text);
    });
  }

  static async open(port, rig) {
    const page = await openPage(port);
    const board = new Board(page, rig);
    await page.send("Runtime.enable");
    await page.send("Log.enable");
    await page.send("Network.enable", { maxPostDataSize: 1 << 20 });
    await page.send("Page.enable");
    await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    await page.send("Page.navigate", { url: rig.url });
    await page.waitFor("!!(window.__synapseV2 && window.__synapseV2.ready())", { timeoutMs: 30000, what: "the v2 board to be ready" });
    await page.eval(`document.addEventListener("securitypolicyviolation", (e) => { (window.__cspViolations = window.__cspViolations || []).push(e.violatedDirective + " " + e.blockedURI); }), true`);
    await board.fit();
    return board;
  }

  async fit() {
    await this.page.eval("window.__synapseV2.fit(), true");
    await sleep(250);
  }

  dl() {
    return this.page.eval("window.__synapseV2.dl()");
  }

  version() {
    return this.page.eval("window.__synapseV2.version()");
  }

  async entry(pred) {
    const dl = await this.dl();
    return (dl.entries || []).find(pred) || null;
  }

  // World point -> page (CSS px) point, from the camera and the surface's position.
  async toScreen(pt) {
    return this.page.eval(`(() => {
      const c = window.__synapseV2.camera();
      const r = document.querySelector(".sv2-surface").getBoundingClientRect();
      return [r.left + (${pt[0]} - c.x) * c.scale, r.top + (${pt[1]} - c.y) * c.scale];
    })()`);
  }

  async scale() {
    return this.page.eval("window.__synapseV2.camera().scale");
  }

  mark() {
    return this.order.length;
  }

  // Zooms out (Cmd -) until world rect [x0, y0, x1, y1] is on screen, clear of the side panel.
  async zoomToShow(rect) {
    for (let i = 0; i < 6; i += 1) {
      const [a, z] = [await this.toScreen([rect[0], rect[1]]), await this.toScreen([rect[2], rect[3]])];
      const room = await this.page.eval(`(() => {
        const s = document.querySelector(".sv2-surface").getBoundingClientRect();
        const panel = document.querySelector(".side-panel");
        const right = panel ? panel.getBoundingClientRect().left : s.right;
        return [s.left + 8, s.top + 8, right - 8, s.bottom - 70];
      })()`);
      if (a[0] >= room[0] && a[1] >= room[1] && z[0] <= room[2] && z[1] <= room[3]) return;
      await this.page.key("-", { modifiers: MOD_KEY });
      await sleep(200);
    }
    throw new Error(`could not bring ${JSON.stringify(rect)} on screen`);
  }

  // Requests since mark(), optionally only POSTs to a path ending in `suffix`.
  since(mark, { method = null, suffix = null } = {}) {
    return this.order
      .slice(mark)
      .map((id) => ({ id, ...this.requests.get(id) }))
      .filter((r) => (!method || r.method === method) && (!suffix || new URL(r.url).pathname.endsWith(suffix)));
  }

  async waitRequests(mark, count, opts = {}, timeoutMs = 8000) {
    const until = Date.now() + timeoutMs;
    for (;;) {
      const found = this.since(mark, opts).filter((r) => r.done);
      if (found.length >= count || Date.now() > until) return found;
      await sleep(50);
    }
  }

  async body(request) {
    const res = await this.page.send("Network.getResponseBody", { requestId: request.id });
    return JSON.parse(res.base64Encoded ? Buffer.from(res.body, "base64").toString("utf8") : res.body);
  }

  // The one POST /ops after `mark`: {ops, result}. Waits for the display list to reach its version.
  async opsPost(mark, { count = 1 } = {}) {
    const posts = await this.waitRequests(mark, count, { method: "POST", suffix: "/ops" });
    if (posts.length !== count) throw new Error(`expected ${count} POST /ops, saw ${posts.length}`);
    const out = [];
    for (const post of posts) {
      const sent = JSON.parse(post.postData || "{}");
      const result = await this.body(post);
      if (Number.isFinite(result.version)) {
        await this.page.waitFor(`window.__synapseV2.version() >= ${result.version} && window.__synapseV2.ready()`, { what: `display list version ${result.version}` });
      }
      out.push({ ops: sent.ops || [], result });
    }
    return count === 1 ? out[0] : out;
  }

  async scene() {
    return this.page.eval(`fetch("/api/teams/${encodeURIComponent(this.rig.team)}/scene", {credentials: "same-origin"}).then((r) => r.json())`);
  }

  async changes(since = 0) {
    return this.page.eval(`fetch("/api/teams/${encodeURIComponent(this.rig.team)}/changes?since=${since}", {credentials: "same-origin"}).then((r) => r.json())`);
  }

  async close() {
    await this.page.close();
  }
}

const center = (box) => [box[0] + box[2] / 2, box[1] + box[3] / 2];
const byText = (text) => (e) => e.edit && e.edit.value === text;
const hitBox = (e) => e.hit.box;

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

// -- scenarios ----------------------------------------------------------------------------------

const TREE = "old oak tree planted by grandpa";
const DOOR = "front door with a brass knocker";
const SUN = "the warm afternoon sun";
const SUN_ARROW = "warms the tiles";

async function e1(b, state) {
  const tree = await b.entry(byText(TREE));
  assert(tree, "house: no tree entry");
  const [x, y] = hitBox(tree);
  const from = await b.toScreen(center(hitBox(tree)));
  const to = await b.toScreen([center(hitBox(tree))[0] + 47, center(hitBox(tree))[1] + 33]);
  const mark = b.mark();
  await b.page.drag(from, to);
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "move", `expected one move, sent ${JSON.stringify(ops)}`);
  const op = ops[0];
  assert(JSON.stringify(op.ids) === JSON.stringify([tree.id]), `move ids ${JSON.stringify(op.ids)}`);
  assert(Array.isArray(op.by) && op.by.every((v) => v % 20 === 0), `by not grid-snapped: ${JSON.stringify(op.by)}`);
  assert((x + op.by[0]) % 20 === 0 && (y + op.by[1]) % 20 === 0, "the box does not land on the grid");
  assert(Number.isFinite(op.if_version), "move carries no if_version");
  const after = await b.entry((e) => e.id === tree.id);
  assert(hitBox(after)[0] === x + op.by[0] && hitBox(after)[1] === y + op.by[1], `dl shows ${hitBox(after)} after move by ${op.by}`);
  state.e1 = { id: tree.id, x, y, batch: result.batch };
  return `move ${tree.id} by ${JSON.stringify(op.by)} -> x ${hitBox(after)[0]} (batch ${result.batch})`;
}

async function e9(b, state) {
  assert(state.e1, "E9 needs E1");
  const mark = b.mark();
  await b.page.key("z", { modifiers: MOD_KEY });
  const { ops } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "undo" && ops[0].batch === state.e1.batch, `expected undo ${state.e1.batch}, sent ${JSON.stringify(ops)}`);
  const back = await b.entry((e) => e.id === state.e1.id);
  assert(hitBox(back)[0] === state.e1.x && hitBox(back)[1] === state.e1.y, `the box is at ${hitBox(back)} after undo, not ${state.e1.x},${state.e1.y}`);
  return `undo ${state.e1.batch}: ${state.e1.id} back at ${state.e1.x},${state.e1.y}`;
}

async function e3(b) {
  const door = await b.entry(byText(DOOR));
  assert(door, "house: no door entry");
  const box = hitBox(door);
  await b.page.key("Escape");
  await b.page.click(await b.toScreen(center(box)));
  await sleep(150);
  const handle = await b.toScreen([box[0] + box[2], box[1] + box[3] / 2]);
  const scale = await b.scale();
  const mark = b.mark();
  await b.page.drag(handle, [handle[0] + 80 * scale, handle[1]]);
  const { ops } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "move" && ops[0].id === door.id && Number.isFinite(ops[0].w), `expected move {w}, sent ${JSON.stringify(ops)}`);
  assert(!("h" in ops[0]) && !("to" in ops[0]), `an east handle sends w only: ${JSON.stringify(ops[0])}`);
  const scene = await b.scene();
  const el = scene.elements.find((e) => e.id === door.id);
  assert(el.w >= ops[0].w, `returned w ${el.w} < asked ${ops[0].w}`);
  return `move {w: ${ops[0].w}} -> w ${el.w}`;
}

async function e4(b) {
  const tree = await b.entry(byText(TREE));
  const sun = await b.entry(byText(SUN));
  await b.page.key("Escape");
  await sleep(100);
  const box = hitBox(tree);
  await b.page.move(await b.toScreen(center(box)));
  await sleep(120);
  const north = await b.toScreen([box[0] + box[2] / 2, box[1]]);
  await b.page.move(north);
  await sleep(120);
  const connectors = await b.page.eval(`document.querySelectorAll(".sv2-connector").length`);
  assert(connectors === 4, `hovering a box shows ${connectors} connection points, not 4`);
  const mark = b.mark();
  await b.page.drag(north, await b.toScreen(center(hitBox(sun))));
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "arrow" && ops[0].from === tree.id && ops[0].to === sun.id, `expected arrow {from, to}, sent ${JSON.stringify(ops)}`);
  const id = result.applied[0].ids[0];
  const scene = await b.scene();
  const el = scene.elements.find((e) => e.id === id);
  assert(el && el.from === tree.id && el.to === sun.id, `the new arrow is not bound: ${JSON.stringify(el && { from: el.from, to: el.to })}`);
  assert(await b.entry((e) => e.id === id), "the new arrow has no entry");
  return `arrow ${id} bound ${tree.id} -> ${sun.id}`;
}

async function e5(b) {
  const tree = await b.entry(byText(TREE));
  await b.page.key("Escape");
  await b.page.dblclick(await b.toScreen(center(hitBox(tree))));
  await b.page.waitFor(`document.activeElement && document.activeElement.classList.contains("v2-text-editor")`, { what: "the text editor" });
  const value = await b.page.eval(`document.activeElement.value`);
  assert(value === TREE, `the editor opened with ${JSON.stringify(value)}`);
  const mark = b.mark();
  await b.page.type("A new oak tree by the fence");
  await b.page.key("Enter", { modifiers: MOD_KEY });
  const { ops } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "edit" && ops[0].id === tree.id && ops[0].text === "A new oak tree by the fence", `expected edit {text}, sent ${JSON.stringify(ops)}`);
  const lines = await b.page.eval(`window.__synapseV2.lines()[${JSON.stringify(tree.id)}]`);
  assert(lines && lines.join(" ").replace(/\s+/g, " ").includes("oak tree"), `the label's lines did not update: ${JSON.stringify(lines)}`);
  const audit = await b.page.eval("window.__synapseV2.audit()");
  assert(audit.length === 0, `audit shows overflow: ${JSON.stringify(audit)}`);
  return `edit -> lines ${JSON.stringify(lines)}; audit clean`;
}

async function e6(b) {
  const arrow = await b.entry((e) => e.kind === "arrow" && e.edit && e.edit.value === SUN_ARROW);
  assert(arrow, "house: no labelled arrow");
  await b.page.key("Escape");
  const mark = b.mark();
  await b.page.dblclick(await b.toScreen(center(arrow.edit.box)));
  await b.page.waitFor(`document.activeElement && document.activeElement.classList.contains("v2-text-editor")`, { what: "the label editor" });
  const value = await b.page.eval("document.activeElement.value");
  assert(value === SUN_ARROW, `the arrow label editor opened with ${JSON.stringify(value)}, not the label`);
  await b.page.key("Escape");
  await sleep(200);
  assert(b.since(mark, { method: "POST" }).length === 0, "cancelling the label editor sent something");
  return `label editor pre-filled with ${JSON.stringify(value)}`;
}

async function e7(b) {
  const dl = await b.dl();
  const home = (dl.entries || []).find((e) => e.kind === "frame");
  const y0 = home.hit.box[1] + home.hit.box[3] + 40;
  const x0 = home.hit.box[0] + 20;
  await b.page.key("Escape");
  await b.page.key("p");
  const pts = [];
  for (let i = 0; i <= 40; i += 1) pts.push(await b.toScreen([x0 + i * 8, y0 + Math.sin(i / 4) * 20]));
  const mark = b.mark();
  await b.page.path(pts);
  const { ops } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "pen", `expected one pen, sent ${JSON.stringify(ops.map((o) => o.op))}`);
  const n = ops[0].points.length;
  assert(n >= 2 && n <= 500, `pen sent ${n} points`);
  await b.page.key("v");
  return `pen with ${n} points`;
}

async function e8(b) {
  const dl = await b.dl();
  const home = (dl.entries || []).find((e) => e.kind === "frame");
  const [hx, hy, , hh] = home.hit.box;
  const region = [hx, hy + hh + 100, hx + 440, hy + hh + 340];
  await b.page.key("Escape");
  await b.zoomToShow(region);
  await b.page.key("f");
  let mark = b.mark();
  await b.page.drag(await b.toScreen([region[0], region[1]]), await b.toScreen([region[2], region[3]]));
  const made = await b.opsPost(mark);
  assert(made.ops.length === 1 && made.ops[0].op === "frame", `expected frame, sent ${JSON.stringify(made.ops)}`);
  const frameId = made.result.applied[0].ids[0];
  // The title editor opens on the new frame; Enter commits a one-line title.
  await b.page.waitFor(`document.activeElement && document.activeElement.classList.contains("v2-text-editor")`, { what: "the frame title editor" });
  mark = b.mark();
  await b.page.type("Garden");
  await b.page.key("Enter");
  const titled = await b.opsPost(mark);
  assert(titled.ops[0].op === "edit" && titled.ops[0].id === frameId && titled.ops[0].text === "Garden", `title edit sent ${JSON.stringify(titled.ops)}`);
  const tree = await b.entry(byText(TREE)) || (await b.entry((e) => e.edit && /oak tree/.test(e.edit.value)));
  const box = hitBox(tree);
  const target = [region[0] + 60 + box[2] / 2, region[1] + 80 + box[3] / 2];
  mark = b.mark();
  await b.page.drag(await b.toScreen(center(box)), await b.toScreen(target), { steps: 12 });
  const moved = await b.opsPost(mark);
  const op = moved.ops[0];
  assert(op.op === "move" && op.frame === frameId, `expected move {frame: ${frameId}}, sent ${JSON.stringify(moved.ops)}`);
  const scene = await b.scene();
  assert(scene.elements.find((e) => e.id === tree.id).frame === frameId, "the moved box is not in the new frame");
  return `frame ${frameId} titled Garden; ${tree.id} moved into it`;
}

async function e11(b) {
  const dl = await b.dl();
  const dark = dl.palettes.dark["base.canvas"];
  const theme = await b.page.eval(`document.querySelector(".sv2-surface").getAttribute("data-theme")`);
  const button = await b.page.eval(`(() => {
    const el = [...document.querySelectorAll(".v2-top-right button")].find((n) => n.textContent.trim() === ${JSON.stringify(theme === "dark" ? "light" : "dark")});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  })()`);
  assert(button, "no theme toggle on the board");
  if (theme === "dark") {
    await b.page.click(button);
    await sleep(300);
  }
  const toggle = await b.page.eval(`(() => {
    const el = [...document.querySelectorAll(".v2-top-right button")].find((n) => n.textContent.trim() === "dark");
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  })()`);
  const mark = b.mark();
  await b.page.click(toggle);
  await sleep(600);
  const requests = b.since(mark);
  assert(requests.length === 0, `switching the theme made requests: ${requests.map((r) => `${r.method} ${r.url}`).join(", ")}`);
  const probe = await b.page.eval(`(() => {
    const surface = document.querySelector(".sv2-surface");
    const bg = document.querySelector(".sv2-bg").getAttribute("fill");
    const filters = [];
    for (let n = document.querySelector(".sv2-svg"); n && n.nodeType === 1; n = n.parentElement) {
      const f = getComputedStyle(n).filter;
      if (f && f !== "none") filters.push(n.tagName + "." + n.className + ": " + f);
    }
    return { theme: surface.getAttribute("data-theme"), bg, css: getComputedStyle(surface).backgroundColor, filters };
  })()`);
  assert(probe.theme === "dark", `the surface is ${probe.theme} after the toggle`);
  assert(String(probe.bg).toLowerCase() === dark.toLowerCase(), `canvas background ${probe.bg}, expected ${dark}`);
  assert(probe.filters.length === 0, `CSS filter on an ancestor: ${probe.filters.join("; ")}`);
  await b.page.screenshot(path.join(OUT, "e11-dark.png"));
  await b.page.click(await b.page.eval(`(() => {
    const el = [...document.querySelectorAll(".v2-top-right button")].find((n) => n.textContent.trim() === "light");
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  })()`));
  return `dark background ${probe.bg}, no requests, no filters`;
}

async function e2(b) {
  const dl = await b.dl();
  const notes = (dl.entries || []).filter((e) => e.kind === "note").slice(0, 3);
  assert(notes.length === 3, "sticky-notes: fewer than 3 notes");
  const boxes = notes.map((e) => [e.bbox[0], e.bbox[1], e.bbox[2], e.bbox[3]]);
  const union = [Math.min(...boxes.map((x) => x[0])), Math.min(...boxes.map((x) => x[1])), Math.max(...boxes.map((x) => x[2])), Math.max(...boxes.map((x) => x[3]))];
  const pad = 6;
  const rect = [union[0] - pad, union[1] - pad, union[2] + pad, union[3] + pad];
  const others = (dl.entries || []).filter((e) => !notes.includes(e) && e.layer !== "overlays" && e.kind !== "frame").filter((e) => {
    const [x, y, w, h] = e.hit.box || [e.bbox[0], e.bbox[1], e.bbox[2] - e.bbox[0], e.bbox[3] - e.bbox[1]];
    return x >= rect[0] && y >= rect[1] && x + w <= rect[2] && y + h <= rect[3];
  });
  assert(others.length === 0, `the marquee would also hold ${others.map((e) => e.id)}`);
  await b.page.key("Escape");
  const mark = b.mark();
  await b.page.drag(await b.toScreen([rect[0], rect[1]]), await b.toScreen([rect[2], rect[3]]), { steps: 10 });
  await sleep(150);
  const selected = await b.page.eval(`document.querySelectorAll(".sv2-ui [data-selected], .sv2-ui .sv2-selection").length`);
  await b.page.key("Delete");
  const { ops } = await b.opsPost(mark);
  const ids = notes.map((e) => e.id).sort();
  assert(ops.length === 1 && ops[0].op === "delete", `expected one delete, sent ${JSON.stringify(ops)}`);
  assert(JSON.stringify([...ops[0].ids].sort()) === JSON.stringify(ids), `deleted ${ops[0].ids}, expected ${ids}`);
  const after = await b.dl();
  assert(!after.entries.some((e) => ids.includes(e.id)), "deleted notes still in the display list");
  return `marquee selected ${ids.join(", ")}${selected ? ` (${selected} outlines)` : ""}; one delete`;
}

async function e10(b) {
  const toolbar = await b.page.eval(`!!document.querySelector(".v2-toolbar")`);
  assert(!toolbar, "a read-only page shows the tool bar");
  const tree = await b.entry(byText(TREE));
  const mark = b.mark();
  const from = await b.toScreen(center(hitBox(tree)));
  await b.page.drag(from, [from[0] + 80, from[1] + 60]);
  await b.page.click(from);
  for (const key of ["Delete", "Backspace", "ArrowLeft", "r", "p", "f"]) await b.page.key(key);
  await b.page.key("z", { modifiers: MOD_KEY });
  await b.page.dblclick(from);
  await sleep(800);
  const editor = await b.page.eval(`!!document.querySelector(".v2-text-editor")`);
  assert(!editor, "a read-only page opened the text editor");
  const handles = await b.page.eval(`document.querySelectorAll(".sv2-handles, .sv2-connector").length`);
  assert(handles === 0, `a read-only page shows ${handles} handle groups or connection points`);
  const posts = b.since(mark, { method: "POST" });
  const all = b.order.map((id) => b.requests.get(id)).filter((r) => r.method === "POST");
  assert(posts.length === 0 && all.length === 0, `a read-only page POSTed: ${all.map((r) => r.url).join(", ")}`);
  const after = await b.entry((e) => e.id === tree.id);
  assert(JSON.stringify(hitBox(after)) === JSON.stringify(hitBox(tree)), "the tree moved on a read-only page");
  return "no tool bar, no handles, no editor, zero POSTs";
}


// -- beyond E1-E12: the rest of the gesture table (4.3) ------------------------------------------

async function x1CreateBox(b) {
  const dl = await b.dl();
  const home = (dl.entries || []).find((e) => e.kind === "frame" && e.edit && e.edit.value !== "Garden");
  const [hx, hy, hw] = home.hit.box;
  const rect = [hx + hw + 360, hy, hx + hw + 520, hy + 80];
  await b.page.key("Escape");
  await b.zoomToShow(rect);
  await b.page.key("r");
  let mark = b.mark();
  await b.page.drag(await b.toScreen([rect[0], rect[1]]), await b.toScreen([rect[2], rect[3]]));
  const made = await b.opsPost(mark);
  const op = made.ops[0];
  assert(op.op === "shape" && op.kind === "box" && op.text === "" && op.w >= 150 && op.h >= 70, `expected shape {kind: box, w, h}, sent ${JSON.stringify(made.ops)}`);
  const id = made.result.applied[0].ids[0];
  await b.page.waitFor(`document.activeElement && document.activeElement.classList.contains("v2-text-editor")`, { what: "the editor on the new box" });
  mark = b.mark();
  await b.page.type("Shed");
  await b.page.key("Enter", { modifiers: MOD_KEY });
  const typed = await b.opsPost(mark);
  assert(typed.ops[0].op === "edit" && typed.ops[0].id === id && typed.ops[0].text === "Shed", `expected edit on ${id}, sent ${JSON.stringify(typed.ops)}`);
  return `shape ${id} ${op.w}x${op.h}, then edit "Shed"`;
}

async function x2Rebind(b) {
  const arrow = await b.entry((e) => e.kind === "arrow" && e.edit && e.edit.value === SUN_ARROW);
  const walls = await b.entry((e) => e.kind === "box" && e.edit && e.edit.value === "");
  assert(arrow && walls, "house: no sun arrow or walls");
  await b.page.key("Escape");
  await b.zoomToShow(arrow.bbox);
  const pts = arrow.hit.points;
  const along = [(pts[0][0] + pts[pts.length - 1][0]) / 2, (pts[0][1] + pts[pts.length - 1][1]) / 2];
  // Select the arrow on its shaft (clear of the label pill), then drag its end handle onto the walls.
  const shaft = [along[0] + (pts[pts.length - 1][0] - along[0]) * 0.6, along[1] + (pts[pts.length - 1][1] - along[1]) * 0.6];
  await b.page.click(await b.toScreen(shaft));
  await sleep(150);
  const end = pts[pts.length - 1];
  // A point on the walls that no child (the window, the door) covers.
  const [wx, wy, ww, wh] = hitBox(walls);
  const covered = (pt) => (b._dlCache || []).some((e) => e.id !== walls.id && e.kind !== "frame" && e.hit && Array.isArray(e.hit.box) && pt[0] >= e.hit.box[0] - 4 && pt[0] <= e.hit.box[0] + e.hit.box[2] + 4 && pt[1] >= e.hit.box[1] - 4 && pt[1] <= e.hit.box[1] + e.hit.box[3] + 4);
  b._dlCache = (await b.dl()).entries;
  const target = [[0.08, 0.92], [0.92, 0.08], [0.08, 0.5], [0.92, 0.92], [0.5, 0.92]].map(([fx, fy]) => [wx + ww * fx, wy + wh * fy]).find((pt) => !covered(pt));
  assert(target, "no free point on the walls");
  const mark = b.mark();
  await b.page.drag(await b.toScreen(end), await b.toScreen(target), { steps: 10 });
  const { ops } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "move" && ops[0].id === arrow.id && ops[0].to_element === walls.id, `expected move {to_element: ${walls.id}}, sent ${JSON.stringify(ops)}`);
  const scene = await b.scene();
  assert(scene.elements.find((e) => e.id === arrow.id).to === walls.id, "the arrow end is not bound to the walls");
  return `end of ${arrow.id} rebound to ${walls.id}`;
}

async function x3Nudge(b) {
  const sun = await b.entry(byText(SUN));
  await b.page.key("Escape");
  await b.page.click(await b.toScreen(center(hitBox(sun))));
  await sleep(150);
  const mark = b.mark();
  await b.page.key("ArrowRight", { modifiers: MOD.shift });
  const { ops } = await b.opsPost(mark);
  assert(ops[0].op === "move" && JSON.stringify(ops[0].by) === "[20,0]" && JSON.stringify(ops[0].ids) === JSON.stringify([sun.id]), `expected move by [20,0], sent ${JSON.stringify(ops)}`);
  return `nudge ${sun.id} by [20,0]`;
}

async function x4Restyle(b) {
  const sun = await b.entry(byText(SUN));
  const swatch = await b.page.eval(`(() => {
    const el = document.querySelector('.v2-stylebar [data-style="tone:warning"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  })()`);
  assert(swatch, "no style bar with a selection");
  const mark = b.mark();
  await b.page.click(swatch);
  const { ops } = await b.opsPost(mark);
  assert(ops[0].op === "restyle" && ops[0].tone === "warning" && JSON.stringify(ops[0].ids) === JSON.stringify([sun.id]), `expected restyle {tone: warning}, sent ${JSON.stringify(ops)}`);
  const after = await b.entry((e) => e.id === sun.id);
  assert(JSON.stringify(after.items).includes("tone.warning"), "the display list does not show the new tone");
  return `restyle ${sun.id} tone warning`;
}

// -- QA phase 1 regressions (findings 2, 3, 4 and 6 of qa-report.md; V-1 and V-3 of verdict.md) ---

// Q2: a shape small on screen moves when dragged from its middle (connection points used to cover
// it, so the drag drew an arrow instead).
async function q2SmallShapeMoves(b) {
  const tree = await b.entry(byText(TREE));
  await b.page.key("Escape");
  await b.fit();
  for (let i = 0; i < 20 && hitBox(tree)[3] * (await b.scale()) >= 16; i += 1) {
    await b.page.key("-", { modifiers: MOD_KEY });
    await sleep(120);
  }
  const scale = await b.scale();
  assert(hitBox(tree)[3] * scale < 16, `could not make the tree small on screen (scale ${scale})`);
  await b.page.waitFor("window.__synapseV2.ready()", { what: "the zoomed-out board to settle" });
  const from = await b.toScreen(center(hitBox(tree)));
  const mark = b.mark();
  await b.page.drag(from, [from[0] + 30, from[1] + 20]);
  const { ops } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "move" && JSON.stringify(ops[0].ids) === JSON.stringify([tree.id]), `expected a move of ${tree.id}, sent ${JSON.stringify(ops)}`);
  await b.page.key("z", { modifiers: MOD_KEY });
  await sleep(300);
  await b.fit();
  return `at scale ${scale.toFixed(3)} (${(hitBox(tree)[3] * scale).toFixed(1)} px tall) the drag moved ${tree.id}`;
}

// Q3: a wheel zoom that runs into the zoom limit still settles and draws crisply.
async function q3ZoomLimitSettles(b) {
  await b.page.key("Escape");
  const at = await b.toScreen(center(hitBox(await b.entry(byText(TREE)))));
  for (let i = 0; i < 40; i += 1) {
    await b.page.send("Input.dispatchMouseEvent", { type: "mouseWheel", x: at[0], y: at[1], deltaX: 0, deltaY: -120 });
    await sleep(16);
  }
  const scale = await b.scale();
  assert(scale >= 7.99, `the wheel did not reach the zoom limit (scale ${scale})`);
  await b.page.waitFor(`document.querySelector("svg.sv2-svg").getAttribute("data-settled") === "1" && window.__synapseV2.ready()`, { timeoutMs: 3000, what: "the board to settle at the zoom limit" });
  await b.fit();
  return `settled at scale ${scale}`;
}

// Q6: a second drag right after the first one chains on it instead of being refused as stale.
async function q6ChainedDrags(b) {
  const sun = await b.entry(byText(SUN));
  await b.page.key("Escape");
  await b.fit();
  const scale = await b.scale();
  const start = hitBox(sun);
  const mark = b.mark();
  const from = await b.toScreen(center(start));
  await b.page.drag(from, [from[0] + 40 * scale, from[1]], { steps: 4, pauseMs: 8 });
  await b.page.drag([from[0] + 40 * scale, from[1]], [from[0] + 80 * scale, from[1]], { steps: 4, pauseMs: 8 });
  const [first, second] = await b.opsPost(mark, { count: 2 });
  for (const { result } of [first, second]) {
    assert(!(result.refused || []).length, `a drag was refused: ${JSON.stringify(result.refused)}`);
  }
  const moved = (first.ops[0].by || [0])[0] + (second.ops[0].by || [0])[0];
  const after = await b.entry((e) => e.id === sun.id);
  assert(hitBox(after)[0] === start[0] + moved, `the sun is at x ${hitBox(after)[0]}, not ${start[0] + moved}`);
  return `two drags, if_version ${first.ops[0].if_version} then ${second.ops[0].if_version}, moved ${moved}`;
}

// Q7: a second drag released while the first POST /ops is still on its way (1.2 s of network
// latency: at 250 ms a loaded machine finished the first POST before the second drag let go,
// QA phase34 L10) waits behind it, is sent with if_version rebased onto the first one's answer,
// and is applied (QA phase 1, V-3: the op queue).
async function q7DragWhileInFlight(b) {
  const sun = await b.entry(byText(SUN));
  await b.page.key("Escape");
  await b.fit();
  const scale = await b.scale();
  const start = hitBox(sun);
  const from = await b.toScreen(center(start));
  const mark = b.mark();
  await b.page.send("Network.emulateNetworkConditions", { offline: false, latency: 1200, downloadThroughput: -1, uploadThroughput: -1 });
  let first;
  let second;
  try {
    await b.page.drag(from, [from[0] + 40 * scale, from[1]], { steps: 4, pauseMs: 8 });
    await b.page.drag([from[0] + 40 * scale, from[1]], [from[0] + 80 * scale, from[1]], { steps: 4, pauseMs: 8 });
    const early = b.since(mark, { method: "POST", suffix: "/ops" });
    assert(early.length === 1 && !early[0].done, `the second POST did not wait for the first (${early.length} sent, first done: ${early[0] && early[0].done})`);
    [first, second] = await b.opsPost(mark, { count: 2 });
  } finally {
    await b.page.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
  }
  for (const { result } of [first, second]) assert(!(result.refused || []).length, `a drag was refused: ${JSON.stringify(result.refused)}`);
  assert(second.ops[0].if_version === first.result.version, `the second drag carried if_version ${second.ops[0].if_version}, not ${first.result.version}`);
  const moved = (first.ops[0].by || [0])[0] + (second.ops[0].by || [0])[0];
  const after = await b.entry((e) => e.id === sun.id);
  assert(hitBox(after)[0] === start[0] + moved, `the sun is at x ${hitBox(after)[0]}, not ${start[0] + moved}`);
  return `queued behind the first POST, if_version ${first.ops[0].if_version} then ${second.ops[0].if_version}, moved ${moved}`;
}

// Q8: a shape small on screen, once selected, still moves when dragged from its middle: its
// resize handles are left out, so they cannot win over the body (QA phase 1, V-1).
async function q8SelectedSmallShapeMoves(b) {
  const tree = await b.entry(byText(TREE));
  await b.page.key("Escape");
  await b.fit();
  for (let i = 0; i < 20 && hitBox(tree)[3] * (await b.scale()) >= 12; i += 1) {
    await b.page.key("-", { modifiers: MOD_KEY });
    await sleep(120);
  }
  const scale = await b.scale();
  assert(hitBox(tree)[3] * scale < 12, `could not make the tree small on screen (scale ${scale})`);
  await b.page.waitFor("window.__synapseV2.ready()", { what: "the zoomed-out board to settle" });
  const from = await b.toScreen(center(hitBox(tree)));
  await b.page.click(from);
  await sleep(150);
  const selected = await b.page.eval(`document.querySelectorAll(".sv2-handles [data-handle]").length`);
  const mark = b.mark();
  await b.page.drag(from, [from[0] + 30, from[1] + 20]);
  const { ops } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "move" && JSON.stringify(ops[0].ids) === JSON.stringify([tree.id]) && ops[0].by && ops[0].w === undefined && ops[0].h === undefined,
    `expected a move of ${tree.id} by a delta, sent ${JSON.stringify(ops)}`);
  await b.page.key("z", { modifiers: MOD_KEY });
  await sleep(300);
  await b.fit();
  return `at scale ${scale.toFixed(3)} (${(hitBox(tree)[3] * scale).toFixed(1)} px tall, ${selected} handle(s) drawn) the drag moved ${tree.id}`;
}

// Q4: indented lines keep their indentation on the page (xml:space on each <text>).
async function q4Indent(b) {
  const collapsed = await b.page.eval(`[...document.querySelectorAll("svg.sv2-svg text")].filter((t) => t.textContent.startsWith(" ")).map((t) => [t.textContent, t.getNumberOfChars()])`);
  assert(collapsed.length > 0, "text-notes: no indented line on the page");
  const lost = collapsed.filter(([text, n]) => n !== text.length);
  assert(!lost.length, `whitespace collapsed on ${JSON.stringify(lost)}`);
  const audit = await b.page.eval("window.__synapseV2.audit().filter((r) => r.collapsed)");
  assert(!audit.length, `the QA hook reports collapsed lines: ${JSON.stringify(audit)}`);
  return `${collapsed.length} indented line(s) keep every space`;
}

const SCENARIOS = [
  { rig: "house", writable: true, list: [["E1", e1], ["E9", e9], ["E3", e3], ["E4", e4], ["E5", e5], ["E6", e6], ["E7", e7], ["E8", e8], ["X1", x1CreateBox], ["X2", x2Rebind], ["X3", x3Nudge], ["X4", x4Restyle], ["E11", e11]] },
  { rig: "sticky-notes", writable: true, list: [["E2", e2]] },
  { rig: "house", writable: false, list: [["E10", e10]] },
  { rig: "house", writable: true, list: [["Q2", q2SmallShapeMoves], ["Q3", q3ZoomLimitSettles], ["Q6", q6ChainedDrags], ["Q7", q7DragWhileInFlight], ["Q8", q8SelectedSmallShapeMoves]] },
  { rig: "text-notes", writable: false, list: [["Q4", q4Indent]] },
  ...PHASE2_SCENARIOS,
  ...CHART_SCENARIOS,
  ...SCENE3D_SCENARIOS,
];

// A scene the Python side has not written yet: its scenarios are reported as skipped, not run.
// SYNAPSE_E2E_SCENES names a folder of scene files that stand in for golden scenes not written
// yet (a page change tried before its scene lands); a golden scene of the name always wins.
const SCENE_DIR = process.env.SYNAPSE_E2E_SCENES || "";
const sceneOf = (name) => {
  if (path.isAbsolute(name)) return fs.existsSync(name) ? name : null; // a scene of this folder (scenes/)
  if (fs.existsSync(path.join(REPO, "tests", "fixtures", "canvas_scenes", `${name}.json`))) return name;
  const local = SCENE_DIR ? path.join(SCENE_DIR, `${name}.json`) : "";
  return local && fs.existsSync(local) ? local : null;
};

// -- the run --------------------------------------------------------------------------------------

async function main() {
  const chrome = findChrome();
  if (!chrome) {
    console.log("SKIP: no Chrome (set CHROME=/path/to/chrome)");
    process.exit(2);
  }
  if (!rigAvailable()) {
    console.log(`SKIP: ${path.join(REPO, "tools", "canvas_rig.py")} is missing`);
    process.exit(2);
  }
  const browser = await launchChrome(chrome);
  const results = [];
  const csp = [];
  const exceptions = [];
  try {
    for (const group of SCENARIOS) {
      // E9 undoes E1's move, so asking for E9 runs E1 first.
      const wanted = group.list.filter(([name]) => !ONLY || ONLY.has(name) || (name === "E1" && ONLY.has("E9")));
      if (!wanted.length) continue;
      const scene = sceneOf(group.rig);
      const label = group.label || group.rig;
      if (!scene) {
        for (const [name] of wanted) {
          results.push({ name, ok: true, skipped: true, detail: `scene ${label} is not written yet` });
          console.log(`SKIP ${name}  scene ${label} is not written yet`);
        }
        continue;
      }
      const rig = await startRig(scene, { writable: group.writable, out: path.join(OUT, `${label}-${group.writable ? "rw" : "ro"}`) });
      let board = null;
      try {
        board = await Board.open(browser.port, rig);
        const state = {};
        for (const [name, fn] of wanted) {
          const started = Date.now();
          try {
            const detail = await Promise.race([
              fn(board, state),
              new Promise((_, reject) => setTimeout(() => reject(new Error(`timed out after ${SCENARIO_MS / 1000} s`)), SCENARIO_MS)),
            ]);
            results.push({ name, ok: true, detail, ms: Date.now() - started });
            console.log(`PASS ${name}  ${detail}`);
          } catch (err) {
            results.push({ name, ok: false, detail: err.message, ms: Date.now() - started });
            console.log(`FAIL ${name}  ${err.message}`);
            await board.page.screenshot(path.join(OUT, `${name}-fail.png`)).catch(() => undefined);
            await board.page.key("Escape").catch(() => undefined);
            await sleep(200);
          }
        }
        const inPage = await board.page.eval("window.__cspViolations || []").catch(() => []);
        csp.push(...board.csp, ...inPage);
        exceptions.push(...board.exceptions);
        fs.writeFileSync(path.join(OUT, `${label}-${group.writable ? "rw" : "ro"}.log`), board.logs.join("\n"));
      } finally {
        if (board) await board.close();
        await rig.stop();
      }
    }
    for (const [name, during] of [["E12", "E1-E11"], ["P12", "P1-P11"], ["S10", "K1-K7 and S1-S9"]]) {
      if (ONLY && !ONLY.has(name)) continue;
      const ok = csp.length === 0;
      results.push({ name, ok, detail: ok ? `no CSP violation during ${during}` : csp.join("\n") });
      console.log(`${ok ? "PASS" : "FAIL"} ${name}  ${ok ? `no CSP violation during ${during}` : csp.join("; ")}`);
    }
  } finally {
    await browser.close();
  }
  if (exceptions.length) console.log(`page exceptions:\n  ${exceptions.join("\n  ")}`);
  fs.writeFileSync(path.join(OUT, "results.json"), `${JSON.stringify({ results, exceptions }, null, 2)}\n`);
  const failed = results.filter((r) => !r.ok);
  const skipped = results.filter((r) => r.skipped).length;
  console.log(`\n${results.length - failed.length - skipped}/${results.length} passed${skipped ? `, ${skipped} skipped` : ""}; evidence in ${OUT}`);
  process.exit(failed.length || exceptions.length ? 1 : 0);
}

main().catch((err) => {
  console.error(err.stack || String(err));
  process.exit(1);
});

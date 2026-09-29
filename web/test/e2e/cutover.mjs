// The canvas v2 phase 6 cut-over scenarios (canvas-v2-phase6.md 1.6): U1 to U4, and U5 (no CSP violation).
//
//   U1  a rig URL with no engine opens canvas v2, and the classic canvas (CanvasTab) is never downloaded;
//   U2  the "Classic canvas" chip loads CanvasTab and Excalidraw; its banner says only what v1 really loses and
//       claims no theme, both measured on the page (checkV1Banner); a reload keeps v1 (the 0.22 key); the
//       "Canvas v2" chip goes back;
//   U3  ?engine=v1 opens the classic canvas directly;
//   U4  a board 0.21.2 drew (the rig's v021-house), opened writable: the migration banner shows; "Fix sizes"
//       posts one migrate batch; the banner goes; the board has no label_overflow (the rig's check); undo of
//       that batch brings the banner back.
//
// These run on raw pages (openRaw): the harness's Board.open waits for the v2 board, and U2 and U3 are about
// not having it. Every page counts CSP reports and exceptions like Board does.
import { openPage, sleep } from "./cdp.mjs";

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

class RawPage {
  constructor(page, rig) {
    this.page = page;
    this.rig = rig;
    this.logs = [];
    this.csp = [];
    this.exceptions = [];
    this.posts = [];
    page.on("Network.requestWillBeSent", ({ request }) => {
      if (request.method === "POST") this.posts.push({ url: request.url, body: request.postData ?? null });
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
      if (/content security policy|refused to/i.test(text)) this.csp.push(text);
    });
  }

  async goto(url) {
    await this.page.send("Page.navigate", { url });
    await this.settled();
  }

  async reload() {
    await this.page.send("Page.reload", { ignoreCache: false });
    await this.settled();
  }

  // The app has mounted (its top bar is there) and the violation hook is in place.
  async settled() {
    await this.page.waitFor(`document.readyState === "complete" && !!document.querySelector(".topbar .engine-toggle")`, { timeoutMs: 30000, what: "the page's top bar" });
    await this.page.eval(`window.__cspHooked || (window.__cspHooked = true, document.addEventListener("securitypolicyviolation", (e) => { (window.__cspViolations = window.__cspViolations || []).push(e.violatedDirective + " " + e.blockedURI); }, true)), true`);
  }

  resources(pattern) {
    return this.page.eval(`performance.getEntriesByType("resource").map((e) => e.name).filter((n) => ${pattern}.test(n))`);
  }

  engine() {
    return this.page.eval(`(() => { const b = document.querySelector("[data-engine]"); return b ? b.getAttribute("data-engine") : (document.querySelector(".excalidraw") ? "v1" : null); })()`);
  }

  chip() {
    return this.page.eval(`(() => { const c = document.querySelector(".topbar .engine-toggle"); return c ? c.textContent : null; })()`);
  }

  async clickSel(selector) {
    const at = await this.page.eval(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return null;
      el.scrollIntoView({ block: "center", inline: "nearest" });
      const r = el.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2];
    })()`);
    assert(at, `nothing on the page matches ${selector}`);
    await sleep(60);
    await this.page.click(at);
    await sleep(150);
  }

  async close() {
    await this.page.close();
  }
}

export async function openRaw(port, rig) {
  const page = await openPage(port);
  const raw = new RawPage(page, rig);
  await page.send("Runtime.enable");
  await page.send("Log.enable");
  await page.send("Network.enable", { maxPostDataSize: 1 << 20 });
  await page.send("Page.enable");
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await raw.goto(rig.url);
  return raw;
}

const CANVAS_TAB = "/CanvasTab-[^/]*\\.js$/";

async function u1DefaultIsV2(p) {
  await p.page.waitFor(`!!document.querySelector('[data-engine="v2"]') && !!(window.__synapseV2 && window.__synapseV2.ready())`, { timeoutMs: 30000, what: "the v2 board" });
  assert(new URL(p.rig.url).searchParams.get("engine") === null, `the rig URL names an engine: ${p.rig.url}`);
  const loaded = await p.resources(CANVAS_TAB);
  assert(loaded.length === 0, `the default page downloaded the classic canvas: ${loaded.join(", ")}`);
  const excalidraw = await p.resources("/excalidraw/i");
  assert(excalidraw.length === 0, `the default page loaded Excalidraw files: ${excalidraw.join(", ")}`);
  assert((await p.chip()) === "Classic canvas", `the chip reads ${await p.chip()}`);
  return "no engine in the URL: [data-engine=v2], no CanvasTab chunk, chip reads Classic canvas";
}

// The v1 banner, held to what the classic canvas really does (QA phase 6, 3.1). It once said "it is always light"
// and this gate asserted that string, so the falsehood passed. Both halves are now measured on the live page: the
// theme claim by flipping the theme and watching Excalidraw's own class follow, the losses against the kind registry
// the page draws with (dist/kinds.json, from src/canvas/kinds/names.js). Adding a v1 builder, or making the classic
// canvas stop following the theme, fails here instead of quietly dating the wording.
async function checkV1Banner(p, banner) {
  assert(/A block keeps its title but loses its body, badges and counters, and a table or a 3D scene is a captioned placeholder\./.test(banner),
    `the v1 banner must say what v1 really loses: ${banner}`);
  assert(!/always (light|dark)|only (light|dark)|(light|dark) only/i.test(banner), `the v1 banner claims a fixed theme: ${banner}`);
  const seen = [];
  for (let i = 0; i < 2; i += 1) {
    await p.clickSel(".topbar .theme-toggle");
    await sleep(400);
    seen.push(await p.page.eval(`(() => {
      const ex = document.querySelector(".excalidraw");
      return [document.documentElement.getAttribute("data-theme"), ex ? ex.classList.contains("theme--dark") : null];
    })()`));
  }
  for (const [theme, exDark] of seen) {
    assert(exDark === (theme === "dark"), `the classic canvas did not follow the ${theme} theme: ${JSON.stringify(seen)}`);
  }
  assert(seen[0][0] !== seen[1][0], `the theme toggle changed nothing: ${JSON.stringify(seen)}`);
  const kinds = await p.page.eval(`fetch("/kinds.json", {credentials: "same-origin"}).then((r) => r.json()).then((j) => j.kinds)`);
  assert(Array.isArray(kinds) && kinds.length, `no kinds.json: ${JSON.stringify(kinds)}`);
  for (const lost of ["badge", "callout", "card", "heading", "icon", "scene3d", "sticky", "table"]) {
    assert(!kinds.includes(lost), `the classic canvas now draws ${lost}, so the v1 banner is out of date: ${kinds.join(",")}`);
  }
  assert(kinds.includes("chart"), `the classic canvas no longer draws charts, so the v1 banner is out of date: ${kinds.join(",")}`);
  return `v1 banner true: the classic canvas followed both themes (${seen.map(([t]) => t).join(" -> ")}), no builder for ${"badge,callout,card,heading,icon,scene3d,sticky,table"}, chart drawn`;
}

async function u2ChipToV1AndBack(p) {
  await p.clickSel(".topbar .engine-toggle");
  await p.page.waitFor(`!!document.querySelector(".excalidraw")`, { timeoutMs: 30000, what: "the classic canvas (.excalidraw)" });
  const loaded = await p.resources(CANVAS_TAB);
  assert(loaded.length >= 1, "the chip did not download CanvasTab");
  const stored = await p.page.eval(`localStorage.getItem("synapse-engine-v022")`);
  assert(stored === "v1", `the choice is stored as ${stored}`);
  assert((await p.chip()) === "Canvas v2", `the chip on v1 reads ${await p.chip()}`);
  const banner = await p.page.eval(`[...document.querySelectorAll(".banner")].map((b) => b.textContent).join(" | ")`);
  assert(/Classic canvas \(v1\): frozen in 0\.22, for comparison\./.test(banner), `no v1 banner: ${banner}`);
  const bannerNote = await checkV1Banner(p, banner);
  await p.reload();
  await p.page.waitFor(`!!document.querySelector(".excalidraw")`, { timeoutMs: 30000, what: "v1 after a reload" });
  assert(!(await p.page.eval(`!!document.querySelector('[data-engine="v2"]')`)), "a reload went back to v2");
  await p.clickSel(".topbar .engine-toggle");
  await p.page.waitFor(`!!document.querySelector('[data-engine="v2"]') && !document.querySelector(".excalidraw")`, { timeoutMs: 30000, what: "back to v2" });
  const back = await p.page.eval(`localStorage.getItem("synapse-engine-v022")`);
  assert(back === "v2", `going back stored ${back}`);
  return `chip -> CanvasTab + .excalidraw; ${bannerNote}; reload keeps v1 (synapse-engine-v022); Canvas v2 chip -> v2`;
}

async function u3QueryOpensV1(p) {
  const origin = new URL(p.rig.url).origin;
  // The page is signed in (the cookie): the query alone decides, whatever this browser stored (v2 by now).
  await p.goto(`${origin}/?engine=v1#team=${encodeURIComponent(p.rig.team)}`);
  await p.page.waitFor(`!!document.querySelector(".excalidraw")`, { timeoutMs: 30000, what: "the classic canvas from ?engine=v1" });
  assert((await p.chip()) === "Canvas v2", "the chip on ?engine=v1");
  await p.goto(`${origin}/#team=${encodeURIComponent(p.rig.team)}`);
  await p.page.waitFor(`!!document.querySelector('[data-engine="v2"]')`, { timeoutMs: 30000, what: "v2 without the query" });
  return "?engine=v1 opens .excalidraw directly; without it the page is v2 again";
}

async function u4MigrationBanner(p) {
  await p.page.waitFor(`!!document.querySelector('[data-engine="v2"]') && !!(window.__synapseV2 && window.__synapseV2.ready())`, { timeoutMs: 30000, what: "the v2 board" });
  await p.page.waitFor(`!!document.querySelector('[data-migration="pending"]')`, { timeoutMs: 20000, what: "the migration banner" });
  const text = await p.page.eval(`document.querySelector('[data-migration="pending"]').textContent`);
  // 0.21 drew every mark in its sketch style by default, so the banner names the style, never a hand.
  assert(/This board was drawn before canvas v2\. \d+ labels need resizing and \d+ marks in the old sketch style are now drawn clean\./.test(text),
    `banner: ${text}`);
  assert(!/hand-drawn/.test(text), `banner: ${text}`);
  const chunk = await p.resources("/MigrationBanner-[^/]*\\.js$/");
  assert(chunk.length === 1, "the banner is its own lazy chunk");
  const before = await p.rig.control({ as: "lead", check: {} });
  assert((before.check.problems || []).some((x) => x.code === "label_overflow"), "the 0.21 board should overflow before the fix");
  const mark = p.posts.length;
  await p.clickSel('[data-migration="pending"] [data-action="apply"]');
  await p.page.waitFor(`!document.querySelector('[data-migration="pending"]')`, { timeoutMs: 20000, what: "the banner to go after Fix sizes" });
  const posts = p.posts.slice(mark).filter((x) => new URL(x.url).pathname.endsWith("/ops"));
  assert(posts.length === 1, `expected one POST /ops, saw ${posts.length}`);
  const sent = JSON.parse(posts[0].body || "{}");
  assert(sent.ops && sent.ops.length === 1 && sent.ops[0].op === "migrate" && sent.ops[0].action === "apply", `sent ${posts[0].body}`);
  const after = await p.rig.control({ as: "lead", check: {} });
  const overflow = (after.check.problems || []).filter((x) => x.code === "label_overflow");
  assert(overflow.length === 0, `label_overflow after the fix: ${JSON.stringify(overflow).slice(0, 300)}`);
  const scene = await p.page.eval(`fetch("/api/teams/${encodeURIComponent(p.rig.team)}/scene", {credentials: "same-origin"}).then((r) => r.json())`);
  assert(scene.settings && scene.settings.migration && scene.settings.migration.action === "apply", "the answer is stored");
  const applied = Object.entries(scene.batches || {}).sort((a, b) => b[1].first_seq - a[1].first_seq)[0][0];
  const undone = await p.rig.control({ as: "lead", ops: [{ op: "undo", batch: applied, intent: "take the migration back" }] });
  assert(undone.result && !(undone.result.refused || []).length, `undo refused: ${JSON.stringify(undone).slice(0, 300)}`);
  await p.page.waitFor(`!!document.querySelector('[data-migration="pending"]')`, { timeoutMs: 20000, what: "the banner back after the undo" });
  return `banner shown; Fix sizes -> one migrate POST, banner gone, label_overflow 0 (was ${before.check.problems.filter((x) => x.code === "label_overflow").length}); undo ${applied} -> banner back`;
}

export const CUTOVER_SCENARIOS = [
  { rig: "house", writable: true, engine: null, raw: true, list: [["U1", u1DefaultIsV2], ["U2", u2ChipToV1AndBack], ["U3", u3QueryOpensV1]] },
  { rig: "v021-house", writable: true, engine: null, raw: true, list: [["U4", u4MigrationBanner]] },
];

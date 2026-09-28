// The canvas v2 phase 5 page scenarios (canvas-v2-phase5.md 14.3, C1 to C12): collaboration with
// the operator as the lead. Each runs on the `collab` rig (tools/canvas_rig.py, the SERVER's
// tests/fixtures/canvas_scenes/collab.json), where the page is the operator in person and the
// rig's stdin protocol (I-9, rig.mjs control) acts as the agents: `drawer` and `peer` (members),
// `deputy` (a member with an operator grant) and `lead` (the trusted human over the CLI path).
//
// Scenarios find what they need by the display list's generic fields (author, kind, frozen,
// pending, the `proposal` object) and the scene, never by fixed ids or positions. Each scenario
// leaves the board as it found it where it can (proposals decided, freezes thawed, settings back),
// so the series runs in any order and three times in a row.
import { MOD, sleep } from "./cdp.mjs";

const MOD_KEY = process.platform === "darwin" ? MOD.meta : MOD.ctrl;
const center = (box) => [box[0] + box[2] / 2, box[1] + box[3] / 2];
const bcenter = (b) => [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
const hitBox = (e) => e.hit.box;

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

const inside = (box, rect) => rect && box[0] >= rect[0] && box[1] >= rect[1] && box[0] + box[2] <= rect[2] && box[1] + box[3] <= rect[3];
const overlaps = (a, b) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];
const bboxOfBox = (box) => [box[0], box[1], box[0] + box[2], box[1] + box[3]];

// A plain shape entry (a box-like mark with a rect hit), not in a block, not frozen or pending,
// clear of every freeze and proposal overlay (claims and comment pins may sit on it).
function plainShapes(dl, author) {
  const overlays = (dl.entries || []).filter((e) => (e.kind === "proposal" || e.kind === "freeze") && Array.isArray(e.bbox));
  return (dl.entries || []).filter(
    (e) =>
      e.author === author &&
      e.layer !== "overlays" &&
      /^E-/.test(e.id) &&
      e.hit &&
      e.hit.shape === "rect" &&
      e.handles === "box" &&
      !e.block &&
      !e.frame &&
      !e.frozen &&
      !(e.pending && e.pending.length) &&
      !e.locked &&
      !overlays.some((o) => overlaps(o.bbox, bboxOfBox(hitBox(e)))),
  );
}

// The rig's authors by key ("drawer" is the scene's member; each is a member's full name on the
// board), read once per rig from each one's look.
async function nameOf(b, key) {
  b.names = b.names || {};
  if (!b.names[key]) {
    const answer = await b.rig.control({ as: key, look: { advance: false } });
    b.names[key] = (answer && answer.look && answer.look.reader) || key;
  }
  return b.names[key];
}

// A plain shape of the operator's. The collab scene's own are all frozen, in proposals or in the
// frozen frame, so the first scenario to need one draws it (as the lead, in free space below the
// board) and the rest reuse it.
async function operatorBox(b) {
  const dl = await b.dl();
  const mine = b.opBox ? plainShapes(dl, "human").find((e) => e.id === b.opBox) : null;
  if (mine) return mine;
  const found = plainShapes(dl, "human").find((e) => !e.frame);
  if (found) return found;
  const [x0, , , y1] = dl.bbox;
  const at = [Math.round((x0 + 40) / 20) * 20, Math.round((y1 + 120) / 20) * 20];
  const made = await as(b, "lead", [{ op: "shape", kind: "box", text: "Pricing", at, w: 200, h: 80 }]);
  const id = (((made.applied || [])[0] || {}).ids || [])[0];
  assert(id, `the lead could not draw a box: ${JSON.stringify(made.refused)}`);
  b.opBox = id;
  await b.fit();
  return (await b.dl()).entries.find((e) => e.id === id);
}

async function drawerBox(b) {
  const dl = await b.dl();
  const found = plainShapes(dl, await nameOf(b, "drawer"))[0];
  assert(found, "collab: no plain drawer shape clear of overlays");
  return found;
}

// The page's list reaches the version an agent's op left the board at.
async function caughtUp(b, result) {
  if (result && Number.isFinite(result.version)) {
    await b.page.waitFor(`window.__synapseV2.version() >= ${result.version} && window.__synapseV2.ready()`, { what: `display list version ${result.version}` });
  }
}

// Every agent op carries an intent (the server refuses one without).
async function as(b, name, ops, extra = {}) {
  const answer = await b.rig.control({ as: name, ops: ops.map((op) => (op.intent ? op : { ...op, intent: "e2e" })), ...extra });
  const result = answer && answer.result;
  assert(result, `the rig gave no result for ${name}: ${JSON.stringify(answer).slice(0, 300)}`);
  // What a scenario proposed is decided before the next one starts, even when it failed midway.
  b.proposed = (b.proposed || []).concat((result.proposed || []).map((p) => p.proposal));
  await caughtUp(b, result);
  return result;
}

async function look(b, name, args = {}) {
  const answer = await b.rig.control({ as: name, look: args });
  assert(answer && answer.look, `the rig gave no look for ${name}: ${JSON.stringify(answer).slice(0, 300)}`);
  return answer.look;
}

async function settings(b, patch) {
  const answer = await b.rig.control({ settings: patch });
  if (answer && answer.result) await caughtUp(b, answer.result);
  return answer;
}

async function collab(b) {
  return b.page.eval("window.__synapseV2.collab()");
}

// A real click on the element a selector finds (scrolled into view first).
async function clickSel(b, selector, { what = selector } = {}) {
  const at = await b.page.eval(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    el.scrollIntoView({ block: "center", inline: "nearest" });
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  })()`);
  assert(at, `nothing on the page matches ${what}`);
  await sleep(60);
  await b.page.click(at);
  await sleep(120);
}

async function exists(b, selector) {
  return b.page.eval(`!!document.querySelector(${JSON.stringify(selector)})`);
}

async function toasts(b) {
  return b.page.eval(`[...document.querySelectorAll(".toast span, .cv2-notice span")].map((n) => n.textContent)`);
}

async function waitToast(b, pattern, what) {
  const re = pattern instanceof RegExp ? pattern : new RegExp(pattern);
  const seen = new Set();
  for (let i = 0; i < 60; i += 1) {
    const list = await toasts(b);
    for (const t of list) seen.add(t);
    const hit = list.find((t) => re.test(t));
    if (hit) return hit;
    await sleep(100);
  }
  throw new Error(`no toast matching ${re} (${what}); toasts seen: ${JSON.stringify([...seen])}`);
}

async function openProposalCard(b, id) {
  await b.page.waitFor(`!!document.querySelector('[data-proposal="${id}"] .cv2-proposal-open')`, { what: `${id} in the panel` });
  await clickSel(b, `[data-proposal="${id}"] .cv2-proposal-open`, { what: `${id}'s row in the panel` });
  await b.page.waitFor(`!!document.querySelector('.cv2-review[data-proposal="${id}"]')`, { what: `${id}'s review card` });
}

function proposalOf(result, what) {
  const p = (result.proposed || [])[0];
  assert(p && /^P-\d+$/.test(p.proposal), `${what}: expected a proposal, got ${JSON.stringify({ applied: result.applied, refused: result.refused, proposed: result.proposed })}`);
  return p;
}

async function waitEntry(b, id, present = true) {
  await b.page.waitFor(`${present ? "" : "!"}window.__synapseV2.dl().entries.some((e) => e.id === ${JSON.stringify(id)}) && window.__synapseV2.ready()`, { what: `${id} ${present ? "drawn" : "gone"}` });
}

async function reset(b) {
  await b.page.key("Escape");
  await b.page.key("Escape");
  await b.page.key("Escape");
  const open = new Set(((await b.scene()).proposals || []).filter((p) => p.status === "open").map((p) => p.id));
  const left = (b.proposed || []).filter((id) => open.has(id));
  b.proposed = [];
  if (left.length) await as(b, "lead", left.map((id) => ({ op: "reject", id, note: "e2e cleanup" })));
  b.proposed = [];
  await b.fit();
}

// -- the scenarios ---------------------------------------------------------------------------------

// C1: peer focuses a region; its halo appears within 1.5 s and fades out after its TTL (10 s).
async function c1Halo(b) {
  await reset(b);
  const dl = await b.dl();
  const [x0, y0, x1, y1] = dl.bbox;
  const region = [Math.round(x0 + (x1 - x0) * 0.1), Math.round(y0 + (y1 - y0) * 0.1), Math.round(x0 + (x1 - x0) * 0.4), Math.round(y0 + (y1 - y0) * 0.4)];
  const peer = await nameOf(b, "peer");
  const halo = `.cv2-halo[data-name="${peer}"]`;
  const pillOf = `[...document.querySelectorAll(".cv2-halo-text")].map((n) => n.textContent).find((t) => t.startsWith(${JSON.stringify(`${peer} ·`)}))`;
  const ok = await b.rig.control({ as: "peer", focus: { region, intent: "checking the numbers", status: "reading", ttl_s: 10 } });
  // From the moment the rig says the focus is written.
  const t0 = Date.now();
  assert(ok && ok.ok, `focus: ${JSON.stringify(ok)}`);
  await b.page.waitFor(`!!document.querySelector('${halo}') && (${pillOf} || "").includes("checking the numbers")`, { timeoutMs: 1500, what: "peer's halo within 1.5 s" });
  const shown = Date.now() - t0;
  const pill = await b.page.eval(pillOf);
  assert(pill && pill.includes("reading") && pill.includes("checking the numbers"), `peer's pill reads ${JSON.stringify(pill)}`);
  const counts = await collab(b);
  assert(counts.halos >= 1, `collab() counts ${counts.halos} halos`);
  await b.page.waitFor(`!document.querySelector('${halo}')`, { timeoutMs: 16000, what: "peer's halo to fade after its TTL" });
  const gone = Date.now() - t0;
  assert(gone >= 8000, `the halo went after ${gone} ms, before its 10 s TTL`);
  return `halo after ${shown} ms, gone after ${(gone / 1000).toFixed(1)} s`;
}

// C2: the page pans, then selects an element; drawer's look shows the operator's view and
// selection within 1.5 s.
async function c2OperatorContext(b) {
  await reset(b);
  const box = await operatorBox(b);
  await b.page.eval(`window.__synapseV2.zoom(1, ${JSON.stringify(center(hitBox(box)))}), true`);
  await b.page.waitFor("window.__synapseV2.ready()", { what: "the panned board" });
  await b.page.click(await b.toScreen(center(hitBox(box))));
  const t0 = Date.now();
  let seen = null;
  while (Date.now() - t0 < 1500) {
    const l = await look(b, "drawer");
    const op = l.presence && l.presence.operator;
    if (op && Array.isArray(op.selection) && op.selection.includes(box.id)) {
      seen = op;
      break;
    }
    await sleep(100);
  }
  assert(seen, `drawer's look does not show the operator selecting ${box.id} within 1.5 s`);
  const cam = await b.page.eval("window.__synapseV2.camera()");
  assert(Array.isArray(seen.viewport) && Math.abs(seen.viewport[0] - cam.x) < 2 && Math.abs(seen.viewport[1] - cam.y) < 2, `the operator's viewport ${JSON.stringify(seen.viewport)} is not the camera's ${JSON.stringify(cam)}`);
  await reset(b);
  return `operator seen selecting ${box.id} after ${Date.now() - t0} ms, viewport ${JSON.stringify(seen.viewport.map(Math.round))}`;
}

// C3: drawer moves the operator's box: a ghost, Review (n) counts it, Accept moves the box, and
// drawer's look lists it as accepted.
async function c3Accept(b) {
  await reset(b);
  const box = await operatorBox(b);
  const before = await collab(b);
  const result = await as(b, "drawer", [{ op: "move", ids: [box.id], by: [40, 0], intent: "line it up with the flow" }]);
  const p = proposalOf(result, "drawer moving the operator's box");
  assert(!(result.applied || []).some((a) => (a.ids || []).includes(box.id)), "the move applied live");
  await waitEntry(b, p.proposal);
  const counts = await collab(b);
  assert(counts.proposals === before.proposals + 1, `collab() draws ${counts.proposals} proposals, not ${before.proposals + 1}`);
  const review = await b.page.eval(`document.querySelector(".v2-review").textContent`);
  assert(review === `Review (${counts.proposals})`, `the tool bar says ${JSON.stringify(review)}`);
  await openProposalCard(b, p.proposal);
  const card = await b.page.eval(`document.querySelector(".cv2-review").textContent`);
  assert(card.includes(`${await nameOf(b, "drawer")} suggests`) && card.includes("line it up with the flow") && card.includes("changes your marks"), `the card reads ${JSON.stringify(card)}`);
  const mark = b.mark();
  await clickSel(b, ".cv2-review .cv2-accept", { what: "Accept" });
  const { ops, result: accepted } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "accept" && ops[0].id === p.proposal, `expected accept ${p.proposal}, sent ${JSON.stringify(ops)}`);
  assert(!(accepted.refused || []).length, `accept refused: ${JSON.stringify(accepted.refused)}`);
  await waitEntry(b, p.proposal, false);
  const after = await b.entry((e) => e.id === box.id);
  assert(hitBox(after)[0] === hitBox(box)[0] + 40, `the box is at x ${hitBox(after)[0]}, not ${hitBox(box)[0] + 40}`);
  const l = await look(b, "drawer");
  const decided = JSON.stringify(l.decided || []);
  assert(decided.includes(p.proposal) && decided.includes("accepted"), `drawer's look: decided ${decided}`);
  // Put it back (the operator's own undo of the accept batch).
  await as(b, "lead", [{ op: "undo", batch: accepted.batch }]);
  return `${p.proposal} drawn, accepted: ${box.id} moved 40; drawer reads it under decided`;
}

// C4: reject with a note: the ghost goes, the box stays, drawer reads the note.
async function c4Reject(b) {
  await reset(b);
  const box = await operatorBox(b);
  const result = await as(b, "drawer", [{ op: "move", ids: [box.id], by: [0, 40], intent: "tidy" }]);
  const p = proposalOf(result, "drawer moving the operator's box");
  await waitEntry(b, p.proposal);
  await openProposalCard(b, p.proposal);
  await clickSel(b, ".cv2-review .cv2-reject", { what: "Reject" });
  await b.page.waitFor(`!!(document.activeElement && document.activeElement.closest(".cv2-card-input"))`, { what: "the reject note" });
  const mark = b.mark();
  await b.page.type("keep it where it is");
  await b.page.key("Enter");
  const { ops } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "reject" && ops[0].id === p.proposal && ops[0].note === "keep it where it is", `expected reject with a note, sent ${JSON.stringify(ops)}`);
  await waitEntry(b, p.proposal, false);
  const after = await b.entry((e) => e.id === box.id);
  assert(JSON.stringify(hitBox(after)) === JSON.stringify(hitBox(box)), `the box moved: ${hitBox(after)}`);
  const l = await look(b, "drawer");
  const decided = JSON.stringify(l.decided || []);
  assert(decided.includes(p.proposal) && decided.includes("keep it where it is"), `drawer's look: decided ${decided}`);
  return `${p.proposal} rejected with a note; the box stayed`;
}

// C5: drawer proposes, then the page edits the target: the card says outdated, Accept is disabled.
async function c5Outdated(b) {
  await reset(b);
  const box = await operatorBox(b);
  const [, , , h] = hitBox(box);
  const result = await as(b, "drawer", [{ op: "move", ids: [box.id], by: [0, Math.ceil((h + 60) / 20) * 20], intent: "make room" }]);
  const p = proposalOf(result, "drawer moving the operator's box");
  await waitEntry(b, p.proposal);
  await b.page.click(await b.toScreen(center(hitBox(box))));
  await sleep(150);
  const sel = await b.page.eval("window.__synapseV2.selection()");
  assert(JSON.stringify(sel) === JSON.stringify([box.id]), `selected ${JSON.stringify(sel)}, not ${box.id}`);
  const mark = b.mark();
  await b.page.key("ArrowRight");
  const { result: nudged } = await b.opsPost(mark);
  assert(!(nudged.refused || []).length, `the nudge was refused: ${JSON.stringify(nudged.refused)}`);
  await b.page.waitFor(`(window.__synapseV2.dl().entries.find((e) => e.id === ${JSON.stringify(p.proposal)}) || {}).proposal?.outdated === true`, { what: `${p.proposal} outdated in the list` });
  await openProposalCard(b, p.proposal);
  const state = await b.page.eval(`(() => { const c = document.querySelector(".cv2-review"); return { warn: !!c.querySelector(".cv2-card-warn"), disabled: c.querySelector(".cv2-accept").disabled }; })()`);
  assert(state.warn && state.disabled, `the card: ${JSON.stringify(state)}`);
  const counts = await collab(b);
  assert(counts.outdated >= 1, `collab() counts ${counts.outdated} outdated`);
  // Clean up: reject it, and undo the nudge.
  await as(b, "lead", [{ op: "reject", id: p.proposal }, { op: "undo", batch: nudged.batch }]);
  return `${p.proposal} outdated after the page's nudge; Accept disabled`;
}

// C6: the page freezes a selection; drawer's edit becomes a proposal; after the setting changes to
// refuse, drawer is refused `frozen`, and so is deputy.
async function c6Freeze(b) {
  await reset(b);
  const box = await drawerBox(b);
  await b.page.click(await b.toScreen(center(hitBox(box))));
  await sleep(150);
  let mark = b.mark();
  await clickSel(b, '.v2-toolbar [data-tool="freeze"]', { what: "the Freeze button" });
  const { ops: fops, result: froze } = await b.opsPost(mark);
  assert(fops.length === 1 && fops[0].op === "freeze" && JSON.stringify(fops[0].ids) === JSON.stringify([box.id]), `expected freeze {ids: [${box.id}]}, sent ${JSON.stringify(fops)}`);
  const xid = ((froze.applied || [])[0].ids || []).find((i) => /^X-/.test(i));
  assert(xid, `the freeze made no X- id: ${JSON.stringify(froze.applied)}`);
  await b.page.waitFor(`window.__synapseV2.dl().entries.some((e) => e.id === ${JSON.stringify(box.id)} && e.frozen)`, { what: `${box.id} frozen in the list` });
  const proposed = await as(b, "drawer", [{ op: "move", ids: [box.id], by: [20, 0], intent: "nudge" }]);
  const p = proposalOf(proposed, "drawer editing its frozen box");
  assert(p.reason === "frozen", `reason ${p.reason}, not frozen`);
  mark = b.mark();
  await clickSel(b, '[data-setting="frozen"] [data-value="refuse"]', { what: "Frozen areas: Refuse" });
  const { ops: sops } = await b.opsPost(mark);
  assert(sops.length === 1 && sops[0].op === "settings" && sops[0].frozen === "refuse", `expected settings {frozen: refuse}, sent ${JSON.stringify(sops)}`);
  const refused = await as(b, "drawer", [{ op: "move", ids: [box.id], by: [20, 0] }]);
  assert((refused.refused || [])[0] && refused.refused[0].code === "frozen", `drawer: ${JSON.stringify(refused.refused || refused.applied)}`);
  const deputy = await as(b, "deputy", [{ op: "move", ids: [box.id], by: [20, 0] }]);
  assert((deputy.refused || [])[0] && deputy.refused[0].code === "frozen", `deputy: ${JSON.stringify(deputy.refused || deputy.applied)}`);
  // Thaw from the freeze card? The card opens from the freeze's rim; the panel's row is simpler.
  mark = b.mark();
  await clickSel(b, `[data-freeze="${xid}"] button.small`, { what: `Thaw ${xid}` });
  const { ops: tops } = await b.opsPost(mark);
  assert(tops[0].op === "thaw" && tops[0].id === xid, `expected thaw ${xid}, sent ${JSON.stringify(tops)}`);
  await as(b, "lead", [{ op: "reject", id: p.proposal }]);
  await settings(b, { frozen: "propose" });
  return `${xid} froze ${box.id}: drawer proposed (${p.proposal}); after refuse, drawer and deputy refused frozen; thawed`;
}

// C7: drawer makes 3 batches, the page edits one element, then "Revert all by drawer": the toast
// shows the skip and the page's edit stays.
async function c7Revert(b) {
  await reset(b);
  const dl = await b.dl();
  const [, y0, x1] = dl.bbox;
  // Revert since a checkpoint taken now, so only this scenario's batches are in play.
  const cp = await as(b, "lead", [{ op: "checkpoint", label: "before C7" }]);
  const vid = (((cp.applied || [])[0] || {}).ids || []).find((i) => /^V-/.test(i)) || ((await b.scene()).checkpoints || []).find((c) => c.label === "before C7")?.id;
  assert(vid, `no checkpoint for C7: ${JSON.stringify(cp.applied)}`);
  const made = [];
  for (let i = 0; i < 3; i += 1) {
    const r = await as(b, "drawer", [{ op: "shape", kind: "box", at: [Math.round(x1 + 200 + i * 200), Math.round(y0)], w: 160, h: 80, text: `revert me ${i + 1}`, intent: "scratch" }]);
    const id = ((r.applied || [])[0] || {}).ids?.[0];
    assert(id, `drawer's box ${i + 1}: ${JSON.stringify(r.refused || r.proposed)}`);
    made.push(id);
  }
  const target = await b.entry((e) => e.id === made[1]);
  await b.zoomToShow(bboxOfBox(hitBox(target)));
  await b.page.click(await b.toScreen(center(hitBox(target))));
  await sleep(150);
  let mark = b.mark();
  await b.page.key("ArrowDown");
  const { result: nudged } = await b.opsPost(mark);
  assert(!(nudged.refused || []).length, `the page's edit was refused: ${JSON.stringify(nudged.refused)}`);
  await b.page.key("Escape");
  const drawer = await nameOf(b, "drawer");
  await clickSel(b, `[data-agent="${drawer}"] button.small`, { what: "drawer's revert menu" });
  mark = b.mark();
  await clickSel(b, `[data-agent="${drawer}"] [data-choice="since:${vid}"]`, { what: `revert: since checkpoint ${vid}` });
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "undo" && ops[0].author === drawer && Number.isInteger(ops[0].since), `expected undo {author, since}, sent ${JSON.stringify(ops)}`);
  const undo = ((result.applied || [])[0] || {}).undo;
  assert(undo && (undo.skipped || []).some((s) => s.id === made[1]), `the undo did not skip ${made[1]}: ${JSON.stringify(undo || result)}`);
  const toast = await waitToast(b, `edited by you later`, "the revert's skip");
  await waitEntry(b, made[0], false);
  const kept = await b.entry((e) => e.id === made[1]);
  assert(kept && hitBox(kept)[1] === hitBox(target)[1] + 1, `the page's edit of ${made[1]} did not survive: ${kept && hitBox(kept)}`);
  assert(undo.restored === undo.of - undo.skipped.length, `reverted ${undo.restored} of ${undo.of} with ${undo.skipped.length} skipped`);
  await waitEntry(b, made[2], false);
  // Clean up the one left behind, and the checkpoint.
  await as(b, "lead", [{ op: "delete", ids: [made[1]] }, { op: "checkpoint", remove: vid }]);
  return `reverted ${undo.restored} of ${undo.of}; toast "${toast}"`;
}

// C8: save a checkpoint, drawer draws, restore: the drawing goes, comments stay, Cmd-Z brings the
// drawing back.
async function c8Checkpoint(b) {
  await reset(b);
  const label = `before the drawer draws ${Date.now() % 100000}`;
  await clickSel(b, '[data-section="checkpoints"] input', { what: "the checkpoint label" });
  await b.page.type(label);
  let mark = b.mark();
  await clickSel(b, ".cv2-save-checkpoint", { what: "Save checkpoint" });
  const { ops, result } = await b.opsPost(mark);
  assert(ops[0].op === "checkpoint" && ops[0].label === label, `expected checkpoint {label}, sent ${JSON.stringify(ops)}`);
  const vid = ((result.applied || [])[0] || {}).ids?.find((i) => /^V-/.test(i)) || ((await b.scene()).checkpoints || []).find((c) => c.label === label)?.id;
  assert(vid, `no checkpoint id: ${JSON.stringify(result.applied)}`);
  const commentsBefore = (await b.dl()).entries.filter((e) => e.kind === "comment").length;
  const dl = await b.dl();
  const drawn = await as(b, "drawer", [{ op: "shape", kind: "box", at: [Math.round(dl.bbox[0]), Math.round(dl.bbox[3] + 200)], w: 160, h: 80, text: "after the checkpoint" }]);
  const nid = ((drawn.applied || [])[0] || {}).ids?.[0];
  assert(nid, `drawer's box: ${JSON.stringify(drawn.refused || drawn.proposed)}`);
  await b.page.waitFor(`!!document.querySelector('[data-checkpoint="${vid}"]')`, { what: `${vid} in the panel` });
  await clickSel(b, `[data-checkpoint="${vid}"] .cv2-row-actions button`, { what: `restore ${vid}` });
  await b.page.waitFor(`!!document.querySelector(".cv2-dialog")`, { what: "the restore confirmation" });
  const text = await b.page.eval(`document.querySelector(".cv2-dialog").textContent`);
  assert(text.includes(`Restore ${vid}?`) && text.includes("checkpoint of now is saved first"), `the dialog reads ${JSON.stringify(text)}`);
  mark = b.mark();
  await clickSel(b, ".cv2-dialog .cv2-accept", { what: "Restore" });
  const { ops: rops, result: restored } = await b.opsPost(mark);
  assert(rops[0].op === "restore" && rops[0].id === vid, `expected restore ${vid}, sent ${JSON.stringify(rops)}`);
  assert(!(restored.refused || []).length, `restore refused: ${JSON.stringify(restored.refused)}`);
  await waitEntry(b, nid, false);
  const commentsAfter = (await b.dl()).entries.filter((e) => e.kind === "comment").length;
  assert(commentsAfter === commentsBefore, `comments ${commentsBefore} -> ${commentsAfter}`);
  mark = b.mark();
  await b.page.key("z", { modifiers: MOD_KEY });
  const { ops: uops } = await b.opsPost(mark);
  assert(uops[0].op === "undo" && uops[0].batch === restored.batch, `Cmd-Z sent ${JSON.stringify(uops)}, not undo ${restored.batch}`);
  await waitEntry(b, nid, true);
  await as(b, "lead", [{ op: "delete", ids: [nid] }, { op: "checkpoint", remove: vid }]);
  return `${vid} restored (${nid} gone, ${commentsAfter} comments kept); Cmd-Z brought ${nid} back`;
}

// C9: a comment on a box follows it when the page drags the box.
async function c9CommentFollows(b) {
  await reset(b);
  const scene = await b.scene();
  const dl = await b.dl();
  const plain = new Set(plainShapes(dl, "human").concat(plainShapes(dl, await nameOf(b, "drawer"))).map((e) => e.id));
  let comment = (scene.elements || []).find((el) => el.type === "comment" && typeof el.on === "string" && plain.has(el.on) && !el.resolved);
  if (!comment) {
    // The scene's comments sit on a proposal's target and on a deleted box: put one on a plain box.
    const box = await operatorBox(b);
    const made = await as(b, "lead", [{ op: "comment", at: box.id, text: "does this follow the box?" }]);
    const id = (((made.applied || [])[0] || {}).ids || [])[0];
    comment = ((await b.scene()).elements || []).find((el) => el.id === id);
    assert(comment && comment.on === box.id, `the lead's comment is not on ${box.id}: ${JSON.stringify(comment)}`);
  }
  const now = await b.dl();
  const box = now.entries.find((e) => e.id === comment.on);
  const pinBefore = now.entries.find((e) => e.id === comment.id);
  assert(pinBefore, `${comment.id} has no entry`);
  await b.zoomToShow(bboxOfBox(hitBox(box)));
  const scale = await b.scale();
  const from = await b.toScreen([hitBox(box)[0] + hitBox(box)[2] * 0.3, hitBox(box)[1] + hitBox(box)[3] * 0.6]);
  const mark = b.mark();
  await b.page.drag(from, [from[0] + 80 * scale, from[1] + 40 * scale]);
  const { ops, result } = await b.opsPost(mark);
  assert(ops[0].op === "move" && JSON.stringify(ops[0].ids) === JSON.stringify([box.id]), `expected a move of ${box.id}, sent ${JSON.stringify(ops)}`);
  const [dx, dy] = ops[0].by;
  const pinAfter = await b.entry((e) => e.id === comment.id);
  const moved = [pinAfter.bbox[0] - pinBefore.bbox[0], pinAfter.bbox[1] - pinBefore.bbox[1]];
  assert(Math.abs(moved[0] - dx) <= 1 && Math.abs(moved[1] - dy) <= 1, `the pin moved ${JSON.stringify(moved)}, the box ${JSON.stringify([dx, dy])}`);
  await as(b, "lead", [{ op: "undo", batch: result.batch }]);
  return `${comment.id} followed ${box.id} by ${JSON.stringify(moved)}`;
}

// C10: drawer changes its box while the page is dragging it: the drag applies, with a stale_base
// warning shown as an info toast.
async function c10StaleBase(b) {
  await reset(b);
  const box = await drawerBox(b);
  await b.zoomToShow(bboxOfBox(hitBox(box)));
  const scale = await b.scale();
  const from = await b.toScreen(center(hitBox(box)));
  await b.page.mouse("mouseMoved", from, { buttons: 0 });
  await b.page.mouse("mousePressed", from, { buttons: 1 });
  for (let i = 1; i <= 4; i += 1) {
    await b.page.mouse("mouseMoved", [from[0] + i * 8, from[1]], { buttons: 1 });
    await sleep(16);
  }
  const vBefore = (await b.entry((e) => e.id === box.id)).v;
  const changed = await as(b, "drawer", [{ op: "restyle", ids: [box.id], tone: "warning" }]);
  assert((changed.applied || []).length, `drawer's restyle: ${JSON.stringify(changed.refused || changed.proposed)}`);
  await b.page.waitFor(`window.__synapseV2.dl().entries.find((e) => e.id === ${JSON.stringify(box.id)}).v > ${vBefore}`, { what: `${box.id}'s new version on the page` });
  const mark = b.mark();
  for (let i = 5; i <= 8; i += 1) {
    await b.page.mouse("mouseMoved", [from[0] + i * 10 * scale, from[1]], { buttons: 1 });
    await sleep(16);
  }
  await b.page.mouse("mouseReleased", [from[0] + 80 * scale, from[1]], { buttons: 0 });
  const posts = await b.waitRequests(mark, 1, { method: "POST", suffix: "/ops" });
  assert(posts.length === 1, `the drag sent ${posts.length} POST /ops`);
  const body = JSON.parse(posts[0].postData || "{}");
  const result = await b.body(posts[0]);
  assert(Number.isInteger(body.base) && body.base < changed.version, `the drag's base ${body.base} is not before drawer's change (v${changed.version})`);
  assert((result.applied || []).length === 1, `the drag did not apply: ${JSON.stringify(result.refused)}`);
  const warning = (result.warnings || []).find((w) => w.code === "stale_base");
  assert(warning, `no stale_base warning: ${JSON.stringify(result.warnings)}`);
  await waitToast(b, warning.message.slice(0, 40).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "the stale_base warning");
  await caughtUp(b, result);
  await as(b, "lead", [{ op: "undo", batch: result.batch }]);
  return `base ${body.base} < v${changed.version}: applied with "${warning.message}"`;
}

// C11 (read-only page): no action buttons; POST /presence is 403; halos are still drawn.
async function c11ReadOnly(b) {
  await reset(b);
  const dl = await b.dl();
  const p = (dl.entries || []).find((e) => e.kind === "proposal");
  assert(p, "collab: no open proposal to open read-only");
  await b.page.click(await b.toScreen(center(hitBox(p))));
  await b.page.waitFor(`!!document.querySelector(".cv2-review")`, { what: "the review card" });
  const buttons = await b.page.eval(`[...document.querySelectorAll(".cv2-review button")].map((n) => n.textContent.trim())`);
  assert(!buttons.some((t) => ["Accept", "Reject", "Ask"].includes(t)), `a read-only card offers ${JSON.stringify(buttons)}`);
  assert(!(await exists(b, ".v2-toolbar")), "a read-only page shows the tool bar");
  assert(!(await exists(b, ".cv2-save-checkpoint")) && !(await exists(b, '[data-agent] button.small')), "a read-only panel offers actions");
  const status = await b.page.eval(`(async () => {
    const s = await fetch("/api/session", { credentials: "same-origin" }).then((r) => r.json()).catch(() => ({}));
    const r = await fetch("/api/teams/${encodeURIComponent(b.rig.team)}/presence", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-Synapse-CSRF": s.csrf || "" }, body: JSON.stringify({ page: "0123456789abcdef", selection: [] }) });
    return r.status;
  })()`);
  assert(status === 403, `POST /presence answered ${status} on a read-only page`);
  const posted = b.since(0, { method: "POST", suffix: "/presence" }).length;
  assert(posted === 1, `the read-only page posted presence ${posted - 1} time(s) itself`);
  const [x0, y0, x1, y1] = dl.bbox;
  const peer = await nameOf(b, "peer");
  await b.rig.control({ as: "peer", focus: { region: [x0, y0, (x0 + x1) / 2, (y0 + y1) / 2].map(Math.round), intent: "reading along", status: "reading", ttl_s: 30 } });
  await b.page.waitFor(`[...document.querySelectorAll(".cv2-halo-text")].some((n) => n.textContent.startsWith(${JSON.stringify(`${peer} ·`)}) && n.textContent.includes("reading along"))`, { timeoutMs: 1500, what: "peer's halo on the read-only page" });
  await b.page.key("Escape");
  return `card without actions; POST /presence ${status}; halo drawn`;
}

// C12: human_edits live: drawer's edit of the operator's box applies at once; the notice's Revert
// undoes it.
async function c12LiveRevert(b) {
  await reset(b);
  const box = await operatorBox(b);
  await settings(b, { human_edits: "live" });
  try {
    const result = await as(b, "drawer", [{ op: "move", ids: [box.id], by: [60, 0], intent: "make space" }]);
    assert((result.applied || []).some((a) => (a.ids || []).includes(box.id)) && !(result.proposed || []).length, `under live the move did not apply: ${JSON.stringify(result.proposed || result.refused)}`);
    await b.page.waitFor(`!!document.querySelector('.cv2-notice[data-notice="touched"]')`, { what: "the Revert notice" });
    const text = await b.page.eval(`document.querySelector('.cv2-notice[data-notice="touched"] span').textContent`);
    assert(text.startsWith(`${await nameOf(b, "drawer")} changed your`), `the notice reads ${JSON.stringify(text)}`);
    const mark = b.mark();
    await clickSel(b, '.cv2-notice[data-notice="touched"] .cv2-notice-action', { what: "Revert" });
    const { ops } = await b.opsPost(mark);
    assert(ops[0].op === "undo" && ops[0].batch === result.batch, `Revert sent ${JSON.stringify(ops)}, not undo ${result.batch}`);
    const back = await b.entry((e) => e.id === box.id);
    assert(hitBox(back)[0] === hitBox(box)[0], `the box is at x ${hitBox(back)[0]} after Revert, not ${hitBox(box)[0]}`);
    return `applied live with a notice "${text}"; Revert undid ${result.batch}`;
  } finally {
    await settings(b, { human_edits: "propose" });
  }
}

export const COLLAB_SCENARIOS = [
  {
    rig: "collab",
    writable: true,
    list: [
      ["C1", c1Halo],
      ["C2", c2OperatorContext],
      ["C3", c3Accept],
      ["C4", c4Reject],
      ["C5", c5Outdated],
      ["C6", c6Freeze],
      ["C7", c7Revert],
      ["C8", c8Checkpoint],
      ["C9", c9CommentFollows],
      ["C10", c10StaleBase],
      ["C12", c12LiveRevert],
    ],
  },
  { rig: "collab", writable: false, list: [["C11", c11ReadOnly]] },
];

export { plainShapes, inside, bcenter };

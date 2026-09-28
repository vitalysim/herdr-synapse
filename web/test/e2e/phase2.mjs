// The canvas v2 phase 2 page scenarios (canvas-v2-phase2.md 8.3, P1 to P11; P12 is run.mjs's CSP
// check over every scenario). Each runs on a tools/canvas_rig.py scene whose owner is the Python
// side (tests/fixtures/canvas_scenes/<scene>.json), and finds what it needs by the display list's
// generic fields (kind, block, part, parts, container, pin, tip), never by ids or positions.
import fs from "node:fs";
import path from "node:path";
import { MOD, sleep } from "./cdp.mjs";
import { REPO } from "./rig.mjs";

const tokens = JSON.parse(fs.readFileSync(path.join(REPO, "assets", "canvas", "tokens.json"), "utf8"));

const MOD_KEY = process.platform === "darwin" ? MOD.meta : MOD.ctrl;
const center = (box) => [box[0] + box[2] / 2, box[1] + box[3] / 2];
const hitBox = (e) => e.hit.box;

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

const editorOpen = `document.activeElement && document.activeElement.classList.contains("v2-text-editor")`;
const byId = (dl, id) => (dl.entries || []).find((e) => e.id === id) || null;
const membersOf = (dl, root) => (dl.entries || []).filter((e) => e.block === root);
const isNode = (e) => e.kind !== "arrow" && !(e.hit && e.hit.shape === "frame") && e.layer !== "overlays";

async function selectAllText(b) {
  await b.page.key("a", { modifiers: MOD_KEY });
}

// P1: double-click a table cell, type, Cmd-Enter: edit {id, part}; the cell's text is updated.
async function p1TableCell(b) {
  const dl = await b.dl();
  const table = (dl.entries || []).find((e) => e.kind === "table" && Array.isArray(e.parts));
  assert(table, "table: no table entry with parts");
  const cell = table.parts.find((p) => p.part === "r1.owner" && p.edit) || table.parts.find((p) => /^r\w*\./.test(p.part) && p.edit);
  assert(cell, `table: no editable body cell in ${JSON.stringify(table.parts.map((p) => p.part))}`);
  await b.page.key("Escape");
  await b.fit();
  if ((await b.scale()) < 0.5) {
    await b.page.eval(`window.__synapseV2.zoom(1, ${JSON.stringify(center(cell.hit.box))}), true`);
    await b.page.waitFor("window.__synapseV2.ready()", { what: "the zoomed board" });
  }
  await b.page.dblclick(await b.toScreen(center(cell.hit.box)));
  await b.page.waitFor(editorOpen, { what: "the cell editor" });
  const opened = await b.page.eval(`[document.activeElement.value, document.activeElement.getAttribute("data-part")]`);
  assert(opened[0] === cell.edit.value && opened[1] === cell.part, `the editor opened on ${JSON.stringify(opened)}, not ${cell.part} "${cell.edit.value}"`);
  const typed = "docs team";
  const mark = b.mark();
  await selectAllText(b);
  await b.page.type(typed);
  await b.page.key("Enter", { modifiers: MOD_KEY });
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "edit" && ops[0].id === table.id && ops[0].part === cell.part && ops[0].text === typed, `expected edit {id, part}, sent ${JSON.stringify(ops)}`);
  assert(!(result.refused || []).length, `refused: ${JSON.stringify(result.refused)}`);
  const after = byId(await b.dl(), table.id);
  const now = after.parts.find((p) => p.part === cell.part);
  assert(now && now.edit && now.edit.value === typed, `the cell reads ${JSON.stringify(now && now.edit && now.edit.value)}`);
  const lines = await b.page.eval(`window.__synapseV2.lines()[${JSON.stringify(table.id)}]`);
  assert((lines || []).some((l) => l.includes("docs team")), `the table's lines do not show the new text: ${JSON.stringify(lines)}`);
  const audit = (await b.page.eval("window.__synapseV2.audit()")).filter((r) => r.id === table.id);
  assert(audit.length === 0, `audit shows overflow: ${JSON.stringify(audit)}`);
  await b.fit();
  return `edit ${table.id} part ${cell.part} -> ${JSON.stringify(typed)}; audit clean`;
}

// The kanban's columns in order, and each one's cards in order.
function board(dl) {
  const root = (dl.entries || []).find((e) => e.kind === "kanban");
  assert(root, "kanban: no kanban entry");
  const order = (root.container && root.container.order) || [];
  const columns = order.map((id) => byId(dl, id)).filter((e) => e && e.container);
  assert(columns.length >= 2, `kanban: ${columns.length} column(s)`);
  const cards = (col) => (col.container.order || []).map((id) => byId(dl, id)).filter(Boolean);
  return { root, columns, cards };
}

// P2: drag a card from the first column to the top of the last: move {frame: <that column>}; it
// shows there at index 0, and the first column re-stacks.
async function p2KanbanDrag(b, state) {
  await b.page.key("Escape");
  await b.fit();
  const dl = await b.dl();
  const { root, columns, cards } = board(dl);
  const from = columns[0];
  const to = columns[columns.length - 1];
  const moving = cards(from)[0];
  assert(moving, "kanban: the first column has no card");
  const rest = cards(from).slice(1);
  const target = cards(to)[0] ? hitBox(cards(to)[0]) : hitBox(to);
  const [tx, ty] = [target[0] + target[2] / 2, target[1] + Math.min(12, target[3] / 4)];
  const mark = b.mark();
  await b.page.drag(await b.toScreen(center(hitBox(moving))), await b.toScreen([tx, ty]), { steps: 14 });
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "move" && ops[0].frame === to.id, `expected move {frame: ${to.id}}, sent ${JSON.stringify(ops)}`);
  assert(!(result.refused || []).length, `refused: ${JSON.stringify(result.refused)}`);
  const after = await b.dl();
  const moved = byId(after, moving.id);
  const toAfter = byId(after, to.id);
  assert(moved.frame === to.id, `the card's frame is ${moved.frame}, not ${to.id}`);
  assert((toAfter.container.order || [])[0] === moving.id, `${to.id} order is ${JSON.stringify(toAfter.container.order)}`);
  const fromAfter = byId(after, from.id);
  assert(!(fromAfter.container.order || []).includes(moving.id), `${from.id} still lists the card`);
  if (rest.length) {
    const first = byId(after, rest[0].id);
    assert(Math.abs(hitBox(first)[1] - hitBox(moving)[1]) < 1, `${from.id} did not re-stack: ${rest[0].id} at y ${hitBox(first)[1]}, the gap was at ${hitBox(moving)[1]}`);
  }
  state.kanban = { root: root.id, card: moving.id };
  return `${moving.id} moved into ${to.id} at index 0 (batch ${result.batch}); ${from.id} re-stacked`;
}

// P10: Esc on a selected kanban card selects the kanban.
async function p10SelectParent(b) {
  await b.page.key("Escape");
  await b.page.key("Escape");
  const dl = await b.dl();
  const { root, columns, cards } = board(dl);
  const card = columns.map(cards).flat()[0];
  assert(card, "kanban: no card");
  await b.page.click(await b.toScreen(center(hitBox(card))));
  await sleep(120);
  let sel = await b.page.eval("window.__synapseV2.selection()");
  assert(JSON.stringify(sel) === JSON.stringify([card.id]), `the click selected ${JSON.stringify(sel)}`);
  await b.page.key("Escape");
  await sleep(80);
  sel = await b.page.eval("window.__synapseV2.selection()");
  assert(JSON.stringify(sel) === JSON.stringify([card.block]), `Esc selected ${JSON.stringify(sel)}, not the block ${card.block}`);
  await b.page.key("Escape");
  await sleep(80);
  sel = await b.page.eval("window.__synapseV2.selection()");
  assert(sel.length === 0, `the second Esc left ${JSON.stringify(sel)} selected`);
  return `card ${card.id} -> ${card.block} (${root.id}) -> nothing`;
}

// P3: drag a graph node: one move; it is pinned by the operator, the other nodes stay, its edges re-route.
async function p3GraphDrag(b, state) {
  await b.page.key("Escape");
  await b.fit();
  const dl = await b.dl();
  const root = (dl.entries || []).find((e) => e.kind === "graph");
  assert(root, "graph-groups: no graph entry");
  const members = membersOf(dl, root.id);
  const nodes = members.filter(isNode);
  const node = nodes.find((n) => !n.pin && !n.locked);
  assert(node, "graph-groups: no unpinned node");
  const scene = await b.scene();
  const edges = scene.elements.filter((el) => el.type === "arrow" && (el.from === node.id || el.to === node.id)).map((el) => el.id);
  const before = Object.fromEntries(members.map((e) => [e.id, e]));
  const from = await b.toScreen(center(hitBox(node)));
  const scale = await b.scale();
  const mark = b.mark();
  await b.page.drag(from, [from[0] + 80 * scale, from[1] + 60 * scale], { steps: 10 });
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "move" && JSON.stringify(ops[0].ids) === JSON.stringify([node.id]), `expected one move of ${node.id}, sent ${JSON.stringify(ops)}`);
  assert(!(result.refused || []).length, `refused: ${JSON.stringify(result.refused)}`);
  const after = await b.dl();
  const moved = byId(after, node.id);
  assert(moved.pin === "human", `the node's pin is ${JSON.stringify(moved.pin)}, not human`);
  const shifted = nodes.filter((n) => n.id !== node.id).filter((n) => JSON.stringify(hitBox(byId(after, n.id))) !== JSON.stringify(hitBox(n))).map((n) => n.id);
  assert(!shifted.length, `other nodes moved: ${shifted.join(", ")} after ${JSON.stringify(ops)} (block ${JSON.stringify((result.applied[0] || {}).block || null)})`);
  const rerouted = edges.filter((id) => byId(after, id) && JSON.stringify(byId(after, id).hit) !== JSON.stringify(before[id] && before[id].hit));
  assert(!edges.length || rerouted.length === edges.length, `edges not re-routed: ${edges.filter((id) => !rerouted.includes(id)).join(", ")}`);
  state.graph = { root: root.id, node: node.id, box: hitBox(node) };
  return `${node.id} pinned by the operator; ${nodes.length - 1} other nodes still; ${rerouted.length}/${edges.length} edges re-routed`;
}

// P4: Unpin in the style bar after P3: unpin; the graph lays out again; the pin is gone.
async function p4Unpin(b, state) {
  assert(state.graph, "P4 needs P3");
  const node = byId(await b.dl(), state.graph.node);
  const sel = await b.page.eval("window.__synapseV2.selection()");
  if (JSON.stringify(sel) !== JSON.stringify([node.id])) {
    await b.page.key("Escape");
    await b.page.click(await b.toScreen(center(hitBox(node))));
    await sleep(120);
  }
  await b.page.waitFor(`!!document.querySelector('[data-style="unpin"]')`, { what: "the Unpin button" });
  const mark = b.mark();
  await b.page.eval(`document.querySelector('[data-style="unpin"]').click(), true`);
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "unpin" && JSON.stringify(ops[0].ids) === JSON.stringify([node.id]), `expected unpin ${node.id}, sent ${JSON.stringify(ops)}`);
  assert(!(result.refused || []).length, `refused: ${JSON.stringify(result.refused)}`);
  const after = byId(await b.dl(), node.id);
  assert(!after.pin, `still pinned: ${after.pin}`);
  // The block lays out again (incrementally, seeded from where things are, so the node may stay).
  const entry = (result.applied || [])[0] || {};
  const moved = JSON.stringify(hitBox(after)) !== JSON.stringify(hitBox(node));
  return `unpin ${node.id}: pin gone; ${moved ? `laid out again at ${hitBox(after).slice(0, 2)}` : "kept its place"}${entry.block ? ` (block ${entry.block.kind}, moved ${JSON.stringify(entry.block.moved || [])})` : ""}`;
}

// P9: an element with detail carries it as an SVG <title>.
async function p9Tip(b) {
  const dl = await b.dl();
  const withTip = (dl.entries || []).find((e) => typeof e.tip === "string" && e.tip);
  assert(withTip, "no entry with a tip on this board");
  await b.page.move(await b.toScreen(center(hitBox(withTip))));
  await sleep(100);
  const titles = await b.page.eval(`[...document.querySelectorAll('svg.sv2-svg g[data-id=${JSON.stringify(withTip.id)}] > title')].map((t) => t.textContent)`);
  assert(titles.length === 1 && titles[0] === withTip.tip, `<title> ${JSON.stringify(titles)}, tip ${JSON.stringify(withTip.tip)}`);
  return `${withTip.id} <title> ${JSON.stringify(withTip.tip.slice(0, 40))}`;
}

// P5: C then a click: card {at}; the editor opens on its title part, and a commit sends edit {part}.
async function p5CardTool(b) {
  await b.page.key("Escape");
  await b.fit();
  const dl = await b.dl();
  const spot = [dl.bbox[0], dl.bbox[3] + 80];
  await b.zoomToShow([spot[0], spot[1], spot[0] + 340, spot[1] + 160]);
  await b.page.key("c");
  const mark = b.mark();
  await b.page.click(await b.toScreen(spot));
  const made = await b.opsPost(mark);
  const op = made.ops[0];
  assert(made.ops.length === 1 && op.op === "card" && Array.isArray(op.at), `expected card {at}, sent ${JSON.stringify(made.ops)}`);
  const id = made.result.applied[0].ids[0];
  await b.page.waitFor(editorOpen, { what: "the card's title editor" });
  const part = await b.page.eval(`document.activeElement.getAttribute("data-part")`);
  assert(part === "title", `the editor opened on part ${JSON.stringify(part)}, not title`);
  const mark2 = b.mark();
  await b.page.type("Ship the beta");
  await b.page.key("Enter", { modifiers: MOD_KEY });
  const titled = await b.opsPost(mark2);
  assert(titled.ops[0].op === "edit" && titled.ops[0].id === id && titled.ops[0].part === "title" && titled.ops[0].text === "Ship the beta", `title edit sent ${JSON.stringify(titled.ops)}`);
  return `card ${id} at ${op.at}; title edited through its part`;
}

// P6: S then a drag over two cards: section {region}; both become its children.
async function p6SectionTool(b) {
  await b.page.key("Escape");
  await b.fit();
  const dl = await b.dl();
  const cards = (dl.entries || []).filter((e) => e.kind === "card" && !e.frame && !e.block).slice(0, 2);
  assert(cards.length === 2, `blocks: ${cards.length} loose card(s)`);
  const boxes = cards.map(hitBox);
  const region = [Math.min(...boxes.map((x) => x[0])) - 40, Math.min(...boxes.map((x) => x[1])) - 80, Math.max(...boxes.map((x) => x[0] + x[2])) + 40, Math.max(...boxes.map((x) => x[1] + x[3])) + 40];
  const covered = (dl.entries || []).filter((e) => e.layer !== "overlays" && !e.frame && e.id !== cards[0].id && e.id !== cards[1].id).filter((e) => {
    const [x, y, w, h] = hitBox(e);
    return x >= region[0] && y >= region[1] && x + w <= region[2] && y + h <= region[3];
  });
  await b.zoomToShow(region);
  await b.page.key("s");
  const mark = b.mark();
  await b.page.drag(await b.toScreen([region[0], region[1]]), await b.toScreen([region[2], region[3]]), { steps: 10 });
  const made = await b.opsPost(mark);
  const op = made.ops[0];
  assert(made.ops.length === 1 && op.op === "section" && Array.isArray(op.region), `expected section {region}, sent ${JSON.stringify(made.ops)}`);
  const id = made.result.applied[0].ids[0];
  // The title editor opens on the new section; Escape leaves its title as it is.
  await b.page.waitFor(editorOpen, { what: "the section's title editor" }).catch(() => undefined);
  await b.page.key("Escape");
  const scene = await b.scene();
  const children = cards.map((c) => scene.elements.find((el) => el.id === c.id)).map((el) => el && el.frame);
  assert(children.every((f) => f === id), `the cards' frames are ${JSON.stringify(children)}, not ${id}${covered.length ? ` (also covered: ${covered.map((e) => e.id)})` : ""}`);
  return `section ${id} over ${cards.map((c) => c.id).join(", ")}`;
}

// Whether an SVG path is an orthogonal route: only M, L and Q commands, every L axis-aligned, and
// every Q a rounded elbow (its control point in line with where it starts and where it ends, at
// most `elbow` units away: tokens.json radius.elbow).
const ELBOW = Number(tokens.radius && tokens.radius.elbow) || 8;
function orthogonal(d, elbow = ELBOW) {
  const tokens = String(d).match(/[A-Za-z]|-?\d*\.?\d+(?:e-?\d+)?/g) || [];
  let cmd = null;
  let at = null;
  let ok = true;
  const letters = new Set();
  for (let i = 0; i < tokens.length; ) {
    if (/[A-Za-z]/.test(tokens[i])) {
      cmd = tokens[i];
      letters.add(cmd);
      i += 1;
      continue;
    }
    const n = cmd === "Q" ? 4 : 2;
    const nums = tokens.slice(i, i + n).map(Number);
    i += n;
    const end = [nums[n - 2], nums[n - 1]];
    const aligned = (p, q) => Math.abs(p[0] - q[0]) <= 0.5 || Math.abs(p[1] - q[1]) <= 0.5;
    if (cmd === "L" && at && !aligned(at, end)) ok = false;
    const corner = [nums[0], nums[1]];
    if (cmd === "Q" && at && !(aligned(at, corner) && aligned(corner, end))) ok = false;
    if (cmd === "Q" && at && Math.max(Math.hypot(corner[0] - at[0], corner[1] - at[1]), Math.hypot(end[0] - corner[0], end[1] - corner[1])) > elbow + 0.5) ok = false;
    at = end;
  }
  return ok && [...letters].every((c) => c === "M" || c === "L" || c === "Q");
}

// P7: style bar Elbow on an arrow: restyle {route: "orthogonal"}; its path is axis-aligned.
async function p7Elbow(b) {
  await b.page.key("Escape");
  await b.fit();
  const dl = await b.dl();
  const scene = await b.scene();
  const routeOf = (id) => ((scene.elements.find((el) => el.id === id) || {}).style || {}).route || "straight";
  const arrow = (dl.entries || []).find((e) => e.kind === "arrow" && !e.locked && e.hit && Array.isArray(e.hit.points) && e.hit.points.length >= 2 && routeOf(e.id) !== "orthogonal"
    && !orthogonal((e.items.find((p) => p.k === "arrow") || {}).d || ""));
  assert(arrow, "routing: no arrow that is not already orthogonal");
  // A point on its line: the middle of its longest hit segment (a curve's hit is its sampled points).
  const pts = arrow.hit.points;
  let best = [pts[0], pts[1]];
  for (let i = 1; i < pts.length; i += 1) if (Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) > Math.hypot(best[1][0] - best[0][0], best[1][1] - best[0][1])) best = [pts[i - 1], pts[i]];
  const mid = [(best[0][0] + best[1][0]) / 2, (best[0][1] + best[1][1]) / 2];
  await b.page.click(await b.toScreen(mid));
  await sleep(150);
  const sel = await b.page.eval("window.__synapseV2.selection()");
  assert(JSON.stringify(sel) === JSON.stringify([arrow.id]), `the click selected ${JSON.stringify(sel)}, not ${arrow.id}`);
  await b.page.waitFor(`!!document.querySelector('[data-style="route:orthogonal"]')`, { what: "the route choice" });
  const mark = b.mark();
  await b.page.eval(`document.querySelector('[data-style="route:orthogonal"]').click(), true`);
  const { ops, result } = await b.opsPost(mark);
  assert(ops.length === 1 && ops[0].op === "restyle" && ops[0].route === "orthogonal" && ops[0].ids.includes(arrow.id), `expected restyle {route}, sent ${JSON.stringify(ops)}`);
  assert(!(result.refused || []).length, `refused: ${JSON.stringify(result.refused)}`);
  const after = byId(await b.dl(), arrow.id);
  const d = (after.items.find((p) => p.k === "arrow") || {}).d || "";
  assert(orthogonal(d), `the arrow's path is not orthogonal: ${d}`);
  return `${arrow.id} routed orthogonal: ${d.slice(0, 60)}`;
}

// P8: zoom to 0.3, then 0.1: card bodies go and titles stay; at 0.1 only top-level titles remain.
async function p8SemanticZoom(b) {
  await b.page.key("Escape");
  await b.fit();
  const dl = await b.dl();
  const cards = (dl.entries || []).filter((e) => e.kind === "card" && Array.isArray(e.parts) && e.parts.some((p) => p.part === "body" && p.edit && p.edit.value));
  assert(cards.length, "composed: no card with a body");
  const card = cards[0];
  const body = card.parts.find((p) => p.part === "body").edit.value.split("\n")[0].replace(/^[-*]\s+/, "").slice(0, 12);
  const title = (card.parts.find((p) => p.part === "title") || {}).edit;
  const zoomTo = async (scale) => {
    await b.page.eval(`window.__synapseV2.zoom(${scale}, ${JSON.stringify(center(hitBox(card)))}), true`);
    await b.page.waitFor(`window.__synapseV2.ready() && Math.abs(window.__synapseV2.camera().scale - ${scale}) < 1e-6`, { what: `the board settled at ${scale}` });
    return b.page.eval("window.__synapseV2.lines()");
  };
  const full = await zoomTo(1);
  assert((full[card.id] || []).some((l) => l.includes(body)), `at 1 the card body is not drawn: ${JSON.stringify(full[card.id])}`);
  const mid = await zoomTo(0.3);
  assert(!(mid[card.id] || []).some((l) => l.includes(body)), `at 0.3 the card body still draws: ${JSON.stringify(mid[card.id])}`);
  if (title && title.value) assert((mid[card.id] || []).some((l) => title.value.startsWith(l.replace(/…$/, "")) || l.includes(title.value.slice(0, 8))), `at 0.3 the card title is gone: ${JSON.stringify(mid[card.id])}`);
  const far = await zoomTo(0.1);
  // Only top-level containers (and level-1 headings, which follow the title rule) name themselves.
  const topLevel = new Set((dl.entries || []).filter((e) => !e.frame && ((e.hit && e.hit.shape === "frame") || e.kind === "heading")).map((e) => e.id));
  const stray = Object.entries(far).filter(([id, lines]) => lines.length && !topLevel.has(id));
  assert(!stray.length, `at 0.1 nested entries still draw text: ${JSON.stringify(stray.slice(0, 4))}`);
  const containers = new Set((dl.entries || []).filter((e) => !e.frame && e.hit && e.hit.shape === "frame").map((e) => e.id));
  const named = Object.entries(far).filter(([id, lines]) => lines.length && containers.has(id)).length;
  assert(named > 0, "at 0.1 no top-level container shows its title");
  await b.fit();
  return `body hidden at 0.3, title kept; at 0.1 ${named} top-level title(s) and nothing nested`;
}

// P11: the read-only page on the kanban: no tools, no pin buttons, and nothing is ever POSTed.
async function p11ReadOnly(b) {
  const mark = b.mark();
  const dl = await b.dl();
  const { columns, cards } = board(dl);
  const card = columns.map(cards).flat()[0];
  assert(!(await b.page.eval(`!!document.querySelector(".v2-toolbar")`)), "a read-only page shows the tool bar");
  await b.page.click(await b.toScreen(center(hitBox(card))));
  await sleep(120);
  assert(!(await b.page.eval(`!!document.querySelector('[data-style="pin"], [data-style="unpin"], .v2-stylebar')`)), "a read-only page shows the style bar or pin buttons");
  await b.page.drag(await b.toScreen(center(hitBox(card))), await b.toScreen(center(hitBox(columns[columns.length - 1]))), { steps: 8 });
  const part = (card.parts || []).find((p) => p.edit);
  if (part) await b.page.dblclick(await b.toScreen(center(part.hit.box)));
  for (const k of ["c", "s", "n", "Enter", "Delete", "Escape", "Escape"]) await b.page.key(k);
  await sleep(400);
  assert(!(await b.page.eval(editorOpen)), "an editor opened on a read-only page");
  const posts = b.since(mark, { method: "POST" });
  assert(posts.length === 0, `a read-only page POSTed: ${posts.map((p) => p.url).join(", ")}`);
  return "no tool bar, no style bar, 0 POSTs";
}

export const PHASE2_SCENARIOS = [
  { rig: "table", writable: true, list: [["P1", p1TableCell]] },
  { rig: "kanban", writable: true, list: [["P2", p2KanbanDrag], ["P10", p10SelectParent]] },
  { rig: "graph-groups", writable: true, list: [["P3", p3GraphDrag], ["P4", p4Unpin], ["P9", p9Tip]] },
  { rig: "blocks", writable: true, list: [["P5", p5CardTool], ["P6", p6SectionTool]] },
  { rig: "routing", writable: true, list: [["P7", p7Elbow]] },
  { rig: "composed", writable: false, list: [["P8", p8SemanticZoom]] },
  { rig: "kanban", writable: false, list: [["P11", p11ReadOnly]] },
];

// Which rigs exist yet (the Python side writes the scenes): a missing one is reported, not failed,
// until the scene lands; a run with every scene present fails on any scenario.
export const PHASE2_SCENES = [...new Set(PHASE2_SCENARIOS.map((g) => g.rig))];

// For the unit tests of the path check.
export { orthogonal };

// The v2 canvas tab (?engine=v2): the team's display list drawn by our own renderer, with the
// interaction layer turning gestures and keys into the existing ops (canvas-v2-phase1.md 4).
//
// Data flow (4.5): the display list comes from GET /display, in full on mount and after a
// reset, then as deltas whenever the scene store (fed by the page's one event stream) is ahead
// of it. Every change is an op POSTed to /ops with if_version; the only local state drawn is the
// preview of a gesture or of an op in flight, which clears when the list reaches the op's
// version. A read-only page syncs and selects but never sends anything.
//
// Takes the same props as CanvasTab (App renders one or the other), plus onFallback(reason),
// which sends this page back to the classic canvas when it cannot draw what the server serves.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getJSON, postBytes, postJSON, teamPath } from "../api.js";
import AuthorChip from "../canvas/AuthorChip.jsx";
import SidePanel from "../canvas/SidePanel.jsx";
import {
  PAGE_FAMILIES,
  Surface,
  applyDelta,
  camera as cameraMath,
  createIndex,
  installQAHook,
  isSupported,
  queryRect,
  resolvePaint,
  toSVGString,
  verifyText,
} from "./render/index.js";
import {
  NO_REDO,
  ONE_SHOT,
  READ_ONLY_TOOLS,
  StyleBar,
  TextEditor,
  Toolbar,
  ViewControls,
  answerExport,
  beginGesture,
  combinePreviews,
  commandOf,
  createDisplayClient,
  createModel,
  createOpQueue,
  createPendingPreviews,
  createUndoStack,
  editTargetOf,
  escapeSelection,
  ops as opsLib,
  panModeOf,
  withoutPartText,
} from "./interact/index.js";
import "./interact/board.css";

const MEASURE_MAX_LINES = 200;
const FONT_METRICS_SHA = typeof __SYNAPSE_FONT_METRICS_SHA__ === "string" ? __SYNAPSE_FONT_METRICS_SHA__ : "";
const SIZE_NAMES = { 16: "s", 20: "m", 28: "l", 36: "xl" };
const NEW_TEXT = { font: "sans", weight: 500, size: 20, lh: 25, align: "start", wrap: "auto", value: "" };
export const UNSUPPORTED = "this page is older than the server's display list; showing the classic canvas";
export const NO_DISPLAY = "this server does not serve a display list yet; showing the classic canvas";

const who = (name) => (name === "human" ? "the operator" : name);

// The side panel's footprint on the right (styles.css .side-panel: 300 wide, 12 from the edge) plus
// a gap. Fitting leaves it out while the panel is open, so nothing fitted sits under the panel
// (QA R-7); a viewport too narrow to spare it fits the whole width.
export const PANEL_INSET_PX = 324;

export function fitViewport(viewport, panelOpen) {
  const w = Number(viewport && viewport.w) || 0;
  const h = Number(viewport && viewport.h) || 0;
  return panelOpen && w >= 2 * PANEL_INSET_PX ? { w: w - PANEL_INSET_PX, h } : { w, h };
}

function refusalText(refused) {
  const first = refused[0];
  const more = refused.length > 1 ? ` (and ${refused.length - 1} more)` : "";
  // The server's stale message ends "look again", which is addressed to agents.
  if (first.code === "canvas_stale") {
    const id = (first.details && first.details.id) || "it";
    return `${id} changed on the board before your change arrived, so it was not applied; try again${more}`;
  }
  return `${first.code}: ${first.message}${more}`;
}

function paintOf(dl, theme, ref) {
  if (!ref || !dl) return null;
  // A reference this list's palette lacks is simply absent here (the renderer would log it).
  const palette = dl.palettes && dl.palettes[theme];
  if (typeof ref === "string" && !ref.startsWith("#") && !(palette && ref in palette)) return null;
  const value = resolvePaint(ref, theme, dl);
  return typeof value === "string" ? value : null;
}

function unionBox(entries) {
  const boxes = entries.map((e) => e && e.bbox).filter((b) => Array.isArray(b) && b.length === 4);
  if (!boxes.length) return null;
  return [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
}

// The canonical elements by id, rebuilt only when the store hands out a new scene.
function useElements(store) {
  const cache = useRef({ scene: null, byId: new Map() });
  return useCallback(
    (id) => {
      const scene = store.get();
      if (cache.current.scene !== scene) {
        cache.current = { scene, byId: new Map((scene.elements || []).map((el) => [el.id, el])) };
      }
      return cache.current.byId.get(id) || null;
    },
    [store],
  );
}

export default function Board({ team, teamRow, writable, store, bus, visible, toast, theme = "light", onTheme, onFallback }) {
  const [dl, setDl] = useState(null);
  const [cam, setCam] = useState(() => cameraMath.create());
  const [viewport, setViewport] = useState({ w: 0, h: 0 });
  const [selection, setSelectionState] = useState([]);
  const [hover, setHover] = useState(null);
  const [hoverPart, setHoverPart] = useState(null);
  const [hoverAt, setHoverAt] = useState(null);
  const [tool, setToolState] = useState("select");
  const [spaceHeld, setSpaceHeld] = useState(false);
  const [livePreview, setLivePreview] = useState(null);
  const [pendingPreviews, setPendingPreviews] = useState([]);
  const [editor, setEditor] = useState(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const [showChips, setShowChips] = useState(false);
  const [hidden, setHidden] = useState(() => new Set());
  const [failure, setFailure] = useState(null);

  const elementOf = useElements(store);
  const dlRef = useRef(null);
  const camRef = useRef(cam);
  const viewportRef = useRef(viewport);
  const panelOpenRef = useRef(panelOpen);
  const selectionRef = useRef(selection);
  const toolRef = useRef(tool);
  const editorRef = useRef(editor);
  const gesture = useRef(null);
  const cancelledAt = useRef(0);
  const rootRef = useRef(null);
  const fitted = useRef(false);
  const measured = useRef(new Set());
  const stills = useRef(new Set());
  const awaitEdit = useRef(null);
  const awaitSelect = useRef(null);
  // The block an Esc selected from one of its members: the next Esc clears (W-g).
  const parented = useRef(null);
  const undo = useMemo(() => createUndoStack(), []);
  const pending = useMemo(() => createPendingPreviews({ onChange: setPendingPreviews }), []);
  dlRef.current = dl;
  camRef.current = cam;
  viewportRef.current = viewport;
  panelOpenRef.current = panelOpen;
  selectionRef.current = selection;
  toolRef.current = tool;
  editorRef.current = editor;

  const index = useMemo(() => createIndex(dl), [dl]);
  // What this page's own applied ops left each element at, until the list catches up (ops.versionOf).
  const ownVersions = useRef(new Map());
  const indexRef = useRef(index);
  indexRef.current = index;
  // The newest version the page knows of an element: its own op's, or the newest list's. A gesture
  // keeps the model it started with, and the list may catch up while it runs (the own version is
  // then dropped), so the list it started with is not enough (QA phase 1, V-3).
  const ownVersionOf = useCallback((id) => opsLib.newestVersion(ownVersions.current, indexRef.current, id), []);
  const model = useMemo(() => createModel(dl, elementOf, { own: ownVersionOf }), [dl, elementOf, ownVersionOf]);
  const modelRef = useRef(model);
  modelRef.current = model;

  const setSelection = useCallback((ids) => setSelectionState(Array.isArray(ids) ? [...new Set(ids)] : []), []);
  const setTool = useCallback(
    (next) => {
      if (!writable && !READ_ONLY_TOOLS.has(next)) return;
      setToolState(next);
    },
    [writable],
  );

  // -- the display list ------------------------------------------------------------------------

  const client = useMemo(
    () =>
      createDisplayClient({
        team,
        fetchJSON: getJSON,
        applyDelta,
        isSupported,
        onError: (err) => {
          if (err instanceof ApiError && (err.status === 404 || err.status === 405) && !dlRef.current) {
            if (onFallback) onFallback(NO_DISPLAY);
            return;
          }
          setFailure(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
        },
      }),
    [team], // eslint-disable-line react-hooks/exhaustive-deps
  );

  useEffect(() => {
    const off = client.subscribe((next) => {
      if (next && !isSupported(next)) {
        if (onFallback) onFallback(UNSUPPORTED);
        return;
      }
      setFailure(null);
      setDl(next);
    });
    client.load();
    return () => {
      off();
      client.close();
    };
  }, [client]); // eslint-disable-line react-hooks/exhaustive-deps

  // The store (the page's event stream) says when the list is behind. A scene event is the
  // stream's first snapshot or a reset: the whole list again, unless the list already shows that
  // very version (the first snapshot usually does) or the first load is still on its way (the
  // list that arrives catches up below).
  useEffect(() => {
    const offList = client.subscribe((next) => {
      if (next && Number.isFinite(next.version) && store.get().version > next.version) client.sync();
    });
    const offStore = store.subscribe((scene, detail) => {
      if (detail && detail.kind === "scene") {
        if (client.current() && scene.version !== client.version()) client.load();
      } else if (scene.version > client.version()) client.sync();
    });
    return () => {
      offList();
      offStore();
    };
  }, [store, client]);

  // What the list now shows: pending previews it caught up with, the first fit, the element a
  // create op made (selected, and its editor opened), and a selection without vanished ids.
  useEffect(() => {
    if (!dl) return;
    pending.reached(dl.version || 0);
    if (!fitted.current && viewport.w > 0 && Array.isArray(dl.bbox)) {
      fitted.current = true;
      setCam(cameraMath.fit(dl.bbox, fitViewport(viewport, panelOpen)));
    }
    const live = new Set(dl.entries.map((e) => e.id));
    // Own versions the list has caught up with (or whose element is gone) are no longer needed.
    if (ownVersions.current.size) {
      const shown = new Map(dl.entries.map((e) => [e.id, e.v]));
      for (const [id, version] of ownVersions.current) {
        if (!live.has(id) || shown.get(id) >= version) ownVersions.current.delete(id);
      }
    }
    setSelectionState((prev) => (prev.every((id) => live.has(id)) ? prev : prev.filter((id) => live.has(id))));
    if (awaitSelect.current && live.has(awaitSelect.current)) {
      setSelectionState([awaitSelect.current]);
      awaitSelect.current = null;
    }
    const editing = editorRef.current;
    if (editing && editing.id && !live.has(editing.id) && !editing.gone) {
      // Warn while the text can still be copied out of the editor; the commit says it again.
      setEditor({ ...editing, gone: true });
      toast(`${editing.id} was deleted while you were editing it; copy your text before you close the editor`, "warn");
    }
    const want = awaitEdit.current;
    if (want && live.has(want)) {
      awaitEdit.current = null;
      const entry = dl.entries.find((e) => e.id === want);
      const target = entry && !entry.locked && writable ? editTargetOf(entry, null, { created: true }) : null;
      if (target) setEditor({ id: want, part: target.part, spec: target.spec, created: true });
    }
  }, [dl, viewport.w > 0]); // eslint-disable-line react-hooks/exhaustive-deps

  // A lost update must not leave a preview: pending ones time out even when nothing arrives.
  useEffect(() => {
    if (!pendingPreviews.length) return undefined;
    const timer = setInterval(() => pending.reached(dlRef.current ? dlRef.current.version || 0 : 0), 1000);
    return () => clearInterval(timer);
  }, [pendingPreviews.length, pending]);

  // -- sending ops -------------------------------------------------------------------------------

  const opQueue = useMemo(() => createOpQueue({ post: (ops) => postJSON(teamPath(team, "ops"), { ops, atomic: false }) }), [team]);

  const sendOps = useCallback(
    async (list, { preview = null, editCreated = false, isUndo = false } = {}) => {
      const opsToSend = (list || []).filter(Boolean);
      if (!writable || !opsToSend.length) return null;
      const key = pending.add(preview);
      let result;
      try {
        // Behind any POST still in flight, with if_version rebased over what those left (V-3).
        result = await opQueue.send(opsToSend);
      } catch (err) {
        pending.drop(key);
        toast(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err), "error");
        return null;
      }
      const refused = result.refused || [];
      const applied = result.applied || [];
      if (result.batch && !isUndo) undo.push(result.batch);
      for (const [id, version] of Object.entries(opsLib.appliedVersions(result))) {
        if (!(ownVersions.current.get(id) > version)) ownVersions.current.set(id, version);
      }
      if (!applied.length) pending.drop(key);
      else pending.settle(key, result.version, dlRef.current ? dlRef.current.version || 0 : 0);
      if (refused.length) toast(refusalText(refused), "warn");
      for (const warning of (result.warnings || []).slice(0, 1)) toast(warning.message, "info");
      const created = applied[0] && applied[0].ids && applied[0].ids[0];
      if (created && editCreated) {
        awaitEdit.current = created;
        awaitSelect.current = created;
      }
      if (applied.length) client.sync();
      return result;
    },
    [writable, opQueue, toast, undo, pending, client],
  );

  // -- the editor ------------------------------------------------------------------------------

  const openEditor = useCallback(
    (id, part = null) => {
      const entry = modelRef.current.entry(id);
      const target = writable ? editTargetOf(entry, part) : null;
      if (!target) return false;
      if (entry.locked) {
        toast(`${id} is in a locked region; unlock it first`, "info");
        return false;
      }
      setEditor({ id, part: target.part, spec: target.spec, created: false });
      return true;
    },
    [writable, toast],
  );

  const openNewText = useCallback(
    (point) => {
      if (!writable || !point) return;
      setEditor({ id: null, at: [Math.round(point[0]), Math.round(point[1])], spec: { ...NEW_TEXT, box: [point[0], point[1], NEW_TEXT.size, NEW_TEXT.lh] } });
    },
    [writable],
  );

  const commitEditor = useCallback(
    (text) => {
      const current = editorRef.current;
      setEditor(null);
      if (!current) return;
      if (current.id) {
        if (!modelRef.current.entry(current.id)) {
          // Someone deleted it while it was being edited (QA 1 finding 11): say so, with the text.
          const typed = String(text ?? "");
          const shown = typed.length > 80 ? `${typed.slice(0, 80)}…` : typed;
          toast(`${current.id} was deleted while you were editing it, so your text was not saved${shown.trim() ? `: “${shown}”` : ""}`, "warn");
          return;
        }
        const op = current.part ? opsLib.buildEditPart(modelRef.current, current.id, current.part, text) : opsLib.buildEdit(modelRef.current, current.id, text);
        if (op) sendOps([op]);
      } else {
        const op = opsLib.buildText(text, current.at);
        if (op) sendOps([op], { editCreated: false }).then((result) => {
          const id = result && result.applied && result.applied[0] && result.applied[0].ids && result.applied[0].ids[0];
          if (id) awaitSelect.current = id;
        });
      }
    },
    [sendOps, toast],
  );

  const editorPreview = useMemo(() => {
    if (!editor || !editor.id || !dl) return null;
    const entry = dl.entries.find((e) => e.id === editor.id);
    if (!entry) return null;
    // Hide the entry and redraw everything but its text, so the typing replaces only the label (for
    // a part, only the text inside the part's box).
    if (editor.part) return { hide: [editor.id], ghost: withoutPartText(entry, editor.spec.box) };
    return { hide: [editor.id], ghost: (entry.items || []).filter((p) => p && p.k !== "text") };
  }, [editor, dl]);

  // -- pointer -----------------------------------------------------------------------------------

  const applyOutcome = useCallback(
    (outcome, usedTool) => {
      if (!outcome) return;
      if (outcome.select) setSelection(outcome.select);
      if (outcome.toast) toast(outcome.toast, "info");
      if (outcome.editNewText) openNewText(outcome.editNewText);
      if (outcome.ops && outcome.ops.length) sendOps(outcome.ops, { preview: outcome.preview, editCreated: outcome.editCreated });
      if (ONE_SHOT.has(usedTool)) setToolState("select");
    },
    [setSelection, toast, openNewText, sendOps],
  );

  const onPointer = useCallback(
    (ev) => {
      if (!ev) return;
      switch (ev.type) {
        case "hover":
          setHover(ev.hit || null);
          setHoverAt(ev.hit ? ev.screen : null);
          setHoverPart((prev) => {
            const next = writable && ev.hit && ev.part && ev.part.edit ? { id: ev.hit, part: ev.part.part } : null;
            return prev && next && prev.id === next.id && prev.part === next.part ? prev : next;
          });
          return;
        case "leave":
          setHover(null);
          setHoverAt(null);
          setHoverPart(null);
          return;
        case "down": {
          if (editorRef.current) return; // the textarea's blur commits it first
          const usedTool = toolRef.current;
          const g = beginGesture(usedTool, ev, {
            model: modelRef.current,
            selection: selectionRef.current,
            writable,
            query: (rect) => queryRect(indexRef.current, rect, { mode: "contain" }),
            setSelection,
          });
          gesture.current = g ? { g, tool: usedTool } : null;
          setHoverAt(null);
          return;
        }
        case "move": {
          const current = gesture.current;
          if (!current) return;
          const preview = current.g.update(ev);
          if (preview !== null) setLivePreview(preview);
          return;
        }
        case "up": {
          const current = gesture.current;
          gesture.current = null;
          setLivePreview(null);
          if (current) applyOutcome(current.g.finish(ev), current.tool);
          return;
        }
        case "cancel": {
          const current = gesture.current;
          gesture.current = null;
          setLivePreview(null);
          if (current) current.g.cancel();
          cancelledAt.current = Date.now();
          return;
        }
        case "dblclick": {
          if (!writable || editorRef.current) return;
          const entry = ev.hit ? modelRef.current.entry(ev.hit) : null;
          // A part with an editor (a table cell, a card's title or body) edits just that part.
          if (entry && ev.part && ev.part.edit && openEditor(entry.id, ev.part.part)) return;
          if (entry) openEditor(entry.id);
          else openNewText(ev.world);
          return;
        }
        default:
      }
    },
    [writable, setSelection, applyOutcome, openEditor, openNewText],
  );

  // -- keyboard ----------------------------------------------------------------------------------

  const fit = useCallback(() => {
    const current = dlRef.current;
    if (current && Array.isArray(current.bbox)) setCam(cameraMath.fit(current.bbox, fitViewport(viewportRef.current, panelOpenRef.current)));
  }, []);

  const zoomBy = useCallback((factor) => {
    const vp = viewportRef.current;
    setCam((c) => cameraMath.zoomAt(c, factor, [vp.w / 2, vp.h / 2]));
  }, []);

  const runUndo = useCallback(async () => {
    const batch = undo.peek((b) => Boolean(store.get().batches?.[b]?.undone));
    if (!batch) {
      toast("nothing of yours to undo on this page", "info");
      return;
    }
    undo.markUndone(batch);
    const result = await sendOps([opsLib.buildUndo(batch)], { isUndo: true });
    if (!result || !(result.applied || []).length) undo.restore(batch);
  }, [undo, store, sendOps, toast]);

  useEffect(() => {
    if (!visible) return undefined;
    const onKey = (event) => {
      if (editorRef.current) return;
      const cmd = commandOf(event, { writable });
      if (!cmd) return;
      const sel = selectionRef.current;
      const m = modelRef.current;
      switch (cmd.command) {
        case "pan_start":
          event.preventDefault();
          if (!event.repeat) setSpaceHeld(true);
          return;
        case "pan_end":
          setSpaceHeld(false);
          return;
        case "tool":
          setTool(cmd.tool);
          return;
        case "cancel":
          if (gesture.current) {
            gesture.current.g.cancel();
            gesture.current = null;
            setLivePreview(null);
          } else if (Date.now() - cancelledAt.current > 100) {
            // (an Esc that Surface already used to cancel a drag keeps the selection)
            // One block member selected: Esc selects its block first, then clears (W-g).
            const next = escapeSelection(sel, (id) => m.entry(id), parented.current);
            parented.current = next.parented;
            setSelection(next.select);
            if (!next.parented) setToolState("select");
          }
          return;
        case "select_all":
          event.preventDefault();
          setSelection((dlRef.current ? dlRef.current.entries : []).filter((e) => e.layer !== "overlays" && /^(E|C)-/.test(e.id)).map((e) => e.id));
          return;
        case "fit":
          event.preventDefault();
          fit();
          return;
        case "zoom":
          event.preventDefault();
          zoomBy(cmd.factor);
          return;
        case "edit":
          if (sel.length === 1 && openEditor(sel[0])) event.preventDefault();
          return;
        case "delete": {
          const op = opsLib.buildDelete(m, sel);
          if (!op) return;
          event.preventDefault();
          setSelection([]);
          sendOps([op], { preview: { hide: op.ids } });
          return;
        }
        case "nudge": {
          const op = opsLib.buildNudge(m, sel, cmd.key, cmd.far);
          if (!op) return;
          event.preventDefault();
          sendOps([op], { preview: { move: { ids: opsLib.movingIds(m, sel), by: op.by } } });
          return;
        }
        case "undo":
          event.preventDefault();
          runUndo();
          return;
        case "redo":
          event.preventDefault();
          toast(NO_REDO, "info");
          return;
        default:
      }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("keyup", onKey);
    const release = () => setSpaceHeld(false);
    window.addEventListener("blur", release);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("keyup", onKey);
      window.removeEventListener("blur", release);
    };
  }, [visible, writable, setTool, setSelection, fit, zoomBy, openEditor, sendOps, runUndo, toast]);

  // -- renderer callbacks --------------------------------------------------------------------------

  const urls = useMemo(
    () => ({
      asset: (name) => teamPath(team, `assets/${encodeURIComponent(name)}`),
      still: (name) => teamPath(team, `stills/${encodeURIComponent(name)}`),
      viz: (id) => `/viz/${encodeURIComponent(team)}/${encodeURIComponent(id)}`,
      artifact: (rel) => `${teamPath(team, "artifact")}?path=${encodeURIComponent(rel)}`,
    }),
    [team],
  );

  const onStill = useCallback(
    (id, version, blob) => {
      if (!writable || !blob) return;
      const key = `${id}:${version}`;
      if (stills.current.has(key)) return;
      stills.current.add(key);
      postBytes(`${teamPath(team, `stills/${encodeURIComponent(id)}`)}?v=${version}`, blob, "image/png").catch(() => stills.current.delete(key));
    },
    [team, writable],
  );

  // The grow-only browser check: lines the browser draws wider than Python measured go to
  // POST /measure, once per version, from writable pages only (a 429 is simply skipped).
  const onRendered = useCallback(
    ({ version, root }) => {
      if (root) rootRef.current = root;
      const current = dlRef.current;
      if (!writable || !root || !current || measured.current.has(version)) return;
      measured.current.add(version);
      Promise.resolve(verifyText(root, current))
        .then((lines) => {
          if (!Array.isArray(lines) || !lines.length) return null;
          return postJSON(teamPath(team, "measure"), { version, metrics: FONT_METRICS_SHA, lines: lines.slice(0, MEASURE_MAX_LINES) });
        })
        .then((result) => {
          if (result && result.refit && result.refit.length) client.sync();
        })
        .catch(() => undefined);
    },
    [team, writable, client],
  );

  useEffect(
    () =>
      installQAHook({
        getDL: () => dlRef.current,
        getCamera: () => camRef.current,
        setCamera: (next) => setCam(next),
        getRoot: () => rootRef.current,
        getViewport: () => fitViewport(viewportRef.current, panelOpenRef.current),
        getSelection: () => selectionRef.current,
      }),
    [],
  );

  useEffect(() => {
    return bus.subscribe("export_request", (data) => {
      if (!writable || !data || data.team !== team) return;
      answerExport(data, dlRef.current, {
        toSVGString,
        urls,
        families: PAGE_FAMILIES,
        scene: () => store.get(),
        post: (requestId, blob) => postBytes(teamPath(team, `exports/${encodeURIComponent(requestId)}`), blob, "image/png"),
      }).catch(() => undefined);
    });
  }, [bus, team, writable, urls, store]);

  // -- side panel actions (the same contract as the classic canvas) ---------------------------------

  const selectedElements = useCallback(() => selection.map((id) => elementOf(id)).filter(Boolean), [selection, elementOf]);

  const actions = {
    send: async (to, note) => {
      const ids = selectedElements().filter((el) => el.type !== "comment").map((el) => el.id);
      if (!ids.length) return toast("select what to send first", "info");
      try {
        await postJSON(teamPath(team, "send"), { to, elements: ids, note: note || null });
        toast(`sent ${ids.length} element${ids.length === 1 ? "" : "s"} to ${to}`, "ok");
      } catch (err) {
        toast(`${err.code || "error"}: ${err.message}`, "error");
      }
      return undefined;
    },
    copyText: async () => {
      const ids = selection.filter((id) => /^(E|C)-/.test(id));
      if (!ids.length) return;
      try {
        const { text } = await getJSON(`${teamPath(team, "text")}?ids=${ids.map(encodeURIComponent).join(",")}`);
        await navigator.clipboard.writeText(text);
        toast("copied the text form", "ok");
      } catch (err) {
        toast(`could not copy: ${err.message || err}`, "error");
      }
    },
    lock: async (label) => {
      const box = unionBox(selection.map((id) => model.entry(id)));
      if (!box) return toast("select the area to lock first", "info");
      const pad = 20;
      await sendOps([{ op: "lock", region: [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad].map(Math.round), label: label || "hands off" }]);
      return undefined;
    },
    unlock: (id) => sendOps([{ op: "unlock", id }]),
    undo: (batch) => sendOps([opsLib.buildUndo(batch)], { isUndo: true }),
    resolve: (id) => sendOps([{ op: "resolve", id }]),
    comment: async (text, replyTo) => {
      if (!text.trim()) return;
      let at = null;
      if (replyTo) {
        const parent = elementOf(replyTo);
        at = parent ? parent.on || parent.point || [parent.x, parent.y] : null;
      } else {
        const target = selectedElements().find((el) => el.type !== "comment");
        if (target) at = target.id;
      }
      if (!at) {
        const [x0, y0, x1, y1] = cameraMath.viewRect(camRef.current, viewportRef.current);
        at = [Math.round((x0 + x1) / 2), Math.round((y0 + y1) / 2)];
      }
      const op = { op: "comment", at, text };
      if (replyTo) op.reply_to = replyTo;
      await sendOps([op]);
    },
    toggleAuthor: (name) =>
      setHidden((prev) => {
        const next = new Set(prev);
        if (next.has(name)) next.delete(name);
        else next.add(name);
        return next;
      }),
    focus: (id) => {
      const entry = model.entry(id);
      if (!entry || !Array.isArray(entry.bbox)) return;
      const vp = viewportRef.current;
      const c = camRef.current;
      const cx = (entry.bbox[0] + entry.bbox[2]) / 2;
      const cy = (entry.bbox[1] + entry.bbox[3]) / 2;
      setCam({ ...c, x: cx - vp.w / (2 * c.scale), y: cy - vp.h / (2 * c.scale) });
      setSelection([id]);
    },
  };

  // -- drawing -----------------------------------------------------------------------------------------

  const hiddenIds = useMemo(() => {
    if (!hidden.size || !dl) return null;
    const ids = dl.entries.filter((e) => hidden.has(e.author)).map((e) => e.id);
    return ids.length ? { hide: ids } : null;
  }, [hidden, dl]);

  const preview = useMemo(
    () => combinePreviews([hiddenIds, ...pendingPreviews, livePreview, editorPreview]),
    [hiddenIds, pendingPreviews, livePreview, editorPreview],
  );

  const styleCurrent = useMemo(() => {
    const el = selection.length ? elementOf(selection[0]) : null;
    const style = (el && el.style) || {};
    return { tone: style.tone, variant: style.variant, size: SIZE_NAMES[style.size] || style.size, dash: style.dash, font: style.font };
  }, [selection, elementOf, dl]); // eslint-disable-line react-hooks/exhaustive-deps

  const styled = selection.filter((id) => /^E-/.test(id) && model.entry(id) && !model.entry(id).locked);
  const arrows = styled.filter((id) => opsLib.kindOf(model, id) === "arrow");
  const arrowRoute = arrows.length ? (elementOf(arrows[0]) && elementOf(arrows[0]).style && elementOf(arrows[0]).style.route) || "straight" : null;
  const hoverEl = hover && hoverAt && !gesture.current ? elementOf(hover) : null;
  const editorColor = editor ? paintOf(dl, theme, editor.spec.fill) : null;

  return (
    <div className={`canvas-tab v2-board${tool !== "select" && tool !== "hand" ? " v2-drawing" : ""}`} data-engine="v2" data-tool={tool}>
      <div className="canvas-host">
        <Surface
          dl={dl}
          theme={theme}
          camera={cam}
          onCamera={setCam}
          onViewport={setViewport}
          selection={selection}
          hover={hover}
          hoverPart={hoverPart}
          preview={preview}
          panMode={panModeOf(tool, spaceHeld)}
          showChips={showChips}
          writable={writable}
          vizOn={Boolean(teamRow && teamRow.viz)}
          team={team}
          urls={urls}
          elementOf={elementOf}
          onPointer={onPointer}
          onStill={onStill}
          onRendered={onRendered}
        >
          {editor ? (
            <TextEditor
              key={editor.id ? `${editor.id}:${editor.part || ""}` : `new:${editor.at}`}
              spec={editor.spec}
              part={editor.part || null}
              camera={cam}
              color={editorColor}
              onCommit={commitEditor}
              onCancel={() => setEditor(null)}
            />
          ) : null}
        </Surface>
      </div>
      <Toolbar tool={tool} onTool={setTool} writable={writable} />
      {writable ? (
        <StyleBar
          count={styled.length}
          current={styleCurrent}
          swatch={(tone) => paintOf(dl, theme, `tone.${tone}.stroke`) || paintOf(dl, theme, `tone.${tone}.solid`) || "#888888"}
          onPick={(patch) => sendOps([opsLib.buildRestyle(model, styled, patch)])}
          route={arrowRoute}
          onRoute={(route) => sendOps([opsLib.buildRestyleRoute(model, arrows, route)])}
          pins={opsLib.pinChoices(model, styled)}
          onPin={() => sendOps([opsLib.buildPin(model, styled)])}
          onUnpin={() => sendOps([opsLib.buildUnpin(model, styled)])}
        />
      ) : null}
      <div className="canvas-top-right v2-top-right">
        <button type="button" className={showChips ? "chip on" : "chip"} title="Show who made each selected mark" onClick={() => setShowChips((v) => !v)}>
          show authors
        </button>
        <button type="button" className={panelOpen ? "chip on" : "chip"} onClick={() => setPanelOpen((v) => !v)}>
          panel
        </button>
        {onTheme ? (
          <button type="button" className="chip" title="Switch the theme" onClick={() => onTheme(theme === "dark" ? "light" : "dark")}>
            {theme === "dark" ? "light" : "dark"}
          </button>
        ) : null}
      </div>
      <ViewControls scale={cam.scale} onZoom={zoomBy} onFit={fit} />
      {!dl && !failure ? <div className="v2-loading">Loading the board…</div> : null}
      {failure ? <div className="v2-failure">The board could not load: {failure}</div> : null}
      {hoverEl ? (
        <div className="hover-card" style={{ left: hoverAt[0] + 14, top: hoverAt[1] + 14 }}>
          <div className="hover-title">
            <AuthorChip scene={store.get()} name={hoverEl.author} theme={theme} />
            {hoverEl.id} {(model.entry(hoverEl.id) && model.entry(hoverEl.id).kind) || hoverEl.type} · {who(hoverEl.author)}
          </div>
          {hoverEl.intent ? <div className="hover-intent">{hoverEl.intent}</div> : null}
          {hoverEl.type === "comment" ? <div className="hover-intent">“{hoverEl.text}”</div> : null}
        </div>
      ) : null}
      {panelOpen ? (
        <SidePanel
          team={team}
          teamRow={teamRow}
          writable={writable}
          store={store}
          selection={selection}
          hidden={hidden}
          theme={theme}
          actions={actions}
          onClose={() => setPanelOpen(false)}
        />
      ) : null}
    </div>
  );
}

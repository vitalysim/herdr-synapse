// The Canvas tab: Excalidraw showing the team's canonical scene through the adapter.
//
// Agents' operations stream in (pen strokes animate along their points, each author's
// labelled cursor moves to what it changed); the human's edits go back as operations
// 400 ms after the pointer lifts, each with if_version. The server is the only writer:
// refused operations revert to the canonical scene with the reason in a toast.
//
// The first build waits for the bundled fonts (src/fonts.css), so Excalidraw measures labels in
// the Inter the server measured them in, not in a fallback that would clip them.
import React, { useCallback, useEffect, useRef, useState } from "react";
import { CaptureUpdateAction, Excalidraw, MainMenu, exportToCanvas, getCommonBounds, restoreElements } from "@excalidraw/excalidraw";
// Excalidraw's own styles load with the classic canvas, never on the default (v2) page (canvas-v2-phase6.md 1.2).
import "@excalidraw/excalidraw/index.css";
import { ApiError, dataURLToBlob, getJSON, postBytes, postJSON, teamPath } from "../api.js";
import VizFrame from "../viz/VizFrame.jsx";
import { CANVAS, INK, SANS, TOKENS, authorChip } from "../theme/tokens.js";
import { build, diff, elementAt, exIdOf, fitAudit, fontStringOf, snapshotOf, unionBounds } from "./adapter.js";
import AuthorChip from "./AuthorChip.jsx";
import { FONT_TO_EX } from "./elements.js";
import { rasterize, renderFile, svgToDataURL } from "./renderers.js";
import SidePanel from "./SidePanel.jsx";

const SYNC_DEBOUNCE_MS = 400;
const PEN_ANIMATION_MS = 600;
const CURSOR_LINGER_MS = 8000;
const OVERRIDE_TTL_MS = 10000;
const RESYNC_MS = 1500;
const MARK_FONT = `600 12px ${SANS}`;
const FONT_WAIT_MS = 4000;
const EX = TOKENS.phase0_excalidraw;
// Frames keep Excalidraw's name label (what a human grabs to select or rename one); the zone
// behind each frame (kinds/frame.js) replaces its grey outline.
const FRAME_RENDERING = { ...EX.frameRendering, name: true };
// What a human draws with: the design's Phase 0 defaults (clean lines, Inter, ink on nothing).
const HUMAN_DEFAULTS = {
  viewBackgroundColor: CANVAS,
  frameRendering: FRAME_RENDERING,
  currentItemFontFamily: FONT_TO_EX.normal,
  currentItemFontSize: 20,
  currentItemRoughness: EX.roughness,
  currentItemStrokeColor: INK,
  currentItemBackgroundColor: "transparent",
  currentItemFillStyle: EX.fillStyle,
  currentItemStrokeStyle: "solid",
  currentItemStrokeWidth: 2,
  currentItemRoundness: "round",
};

// Resolves once the canvas fonts have loaded (or after FONT_WAIT_MS, so a missing font never
// keeps the canvas empty): family 2 ("Helvetica", Inter here) and the chrome's Synapse Sans.
let fontsReady = null;
function whenFontsReady() {
  if (!fontsReady) {
    const fonts = typeof document !== "undefined" ? document.fonts : null;
    const loaded = fonts
      ? Promise.all([fonts.load(fontStringOf(20, FONT_TO_EX.normal)), fonts.load(`400 16px "Synapse Sans"`)]).then(() => fonts.ready)
      : Promise.resolve();
    fontsReady = Promise.race([loaded.catch(() => undefined), new Promise((resolve) => setTimeout(resolve, FONT_WAIT_MS))]);
  }
  return fontsReady;
}

function refusalText(refused) {
  const first = refused[0];
  const more = refused.length > 1 ? ` (and ${refused.length - 1} more)` : "";
  return `${first.code}: ${first.message}${more}`;
}

function authorName(name) {
  return name === "human" ? "the operator" : name;
}

export default function CanvasTab({ team, teamRow, writable, store, bus, visible, toast, theme = "light", onTheme }) {
  const apiRef = useRef(null);
  const snapshots = useRef(new Map());
  const overrides = useRef(new Map());
  const sentCreates = useRef(new Set());
  const clientToCanon = useRef(new Map());
  const uploads = useRef(new Map());
  const ownBatches = useRef(new Set());
  const touched = useRef(new Set());
  const dropped = useRef(new Set());
  const addedFiles = useRef(new Set());
  const stillsSent = useRef(new Set());
  const animations = useRef(new Map());
  const collaborators = useRef(new Map());
  const pointerDown = useRef(false);
  const pendingRebuild = useRef(false);
  const syncTimer = useRef(null);
  const syncing = useRef(false);
  const fitted = useRef(false);
  const resyncTimer = useRef(null);
  const animFrame = useRef(null);
  const hoverFrame = useRef(null);
  const lastClient = useRef({ x: 0, y: 0 });
  const selectionKey = useRef("");
  const fontsLoaded = useRef(false);
  const exTheme = useRef(null);
  const [authors, setAuthors] = useState(false);
  const [hidden, setHidden] = useState(() => new Set());
  const [selection, setSelection] = useState([]);
  const [tooltip, setTooltip] = useState(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const optionsRef = useRef({ authors, hidden, vizOn: Boolean(teamRow && teamRow.viz) });
  optionsRef.current = { authors, hidden, vizOn: Boolean(teamRow && teamRow.viz) };

  // -- building the Excalidraw scene -------------------------------------------------

  const isInteracting = useCallback(() => {
    const api = apiRef.current;
    if (!api) return false;
    const state = api.getAppState();
    return Boolean(
      pointerDown.current || state.editingTextElement || state.newElement || state.resizingElement || state.multiElement ||
        state.editingLinearElement || state.isResizing || state.isRotating,
    );
  }, []);

  const animatedPoints = useCallback((now = performance.now()) => {
    const out = new Map();
    for (const [id, anim] of animations.current) {
      const t = Math.min(1, (now - anim.start) / PEN_ANIMATION_MS);
      if (t >= 1) {
        animations.current.delete(id);
        continue;
      }
      out.set(id, anim.points.slice(0, Math.max(2, Math.ceil(anim.points.length * t))));
    }
    return out;
  }, []);

  const postStill = useCallback(
    async (element, url) => {
      if (!writable) return;
      const key = `${element.id}:${element.updated_seq || 0}`;
      if (stillsSent.current.has(key)) return;
      stillsSent.current.add(key);
      try {
        const png = await rasterize(url, element.w, element.h);
        await postBytes(`${teamPath(team, `stills/${element.id}`)}?v=${element.updated_seq || 0}`, png, "image/png");
      } catch {
        stillsSent.current.delete(key);
      }
    },
    [team, writable],
  );

  const runFileJobs = useCallback(
    (jobs) => {
      const api = apiRef.current;
      for (const { fileId, element } of jobs) {
        if (addedFiles.current.has(fileId)) continue;
        addedFiles.current.add(fileId);
        renderFile(team, element, fileId)
          .then((file) => {
            if (apiRef.current !== api || !api) return;
            api.addFiles([{ id: fileId, mimeType: file.mimeType, dataURL: file.dataURL, created: Date.now() }]);
            if (file.svg && !file.failed && (element.type === "mermaid" || element.type === "chart")) postStill(element, svgToDataURL(file.svg));
          })
          .catch(() => addedFiles.current.delete(fileId));
      }
    },
    [team, postStill],
  );

  const pushCollaborators = useCallback(() => {
    const api = apiRef.current;
    if (!api) return;
    const map = new Map();
    for (const [name, value] of collaborators.current) map.set(name, value.collaborator);
    api.updateScene({ collaborators: map, captureUpdate: CaptureUpdateAction.NEVER });
  }, []);

  const rebuild = useCallback(() => {
    const api = apiRef.current;
    if (!api) return;
    if (isInteracting() || !fontsLoaded.current) {
      pendingRebuild.current = true;
      return;
    }
    pendingRebuild.current = false;
    const scene = store.get();
    const { authors: showAuthors, hidden: hiddenAuthors, vizOn } = optionsRef.current;
    const { elements: built, fileJobs } = build(scene, { team, hiddenAuthors, authors: showAuthors, vizOn, animated: animatedPoints() });
    const current = api.getSceneElementsIncludingDeleted();
    const currentById = new Map(current.map((el) => [el.id, el]));
    const now = Date.now();
    for (const [id, at] of overrides.current) if (now - at > OVERRIDE_TTL_MS) overrides.current.delete(id);
    const final = built.map((el) => (overrides.current.has(el.id) && currentById.has(el.id) ? currentById.get(el.id) : el));
    const builtIds = new Set(final.map((el) => el.id));
    const keep = current.filter(
      (el) =>
        !builtIds.has(el.id) &&
        !el.isDeleted &&
        !el.customData?.synapse &&
        !dropped.current.has(el.id) &&
        !(el.containerId && (builtIds.has(el.containerId) || dropped.current.has(el.containerId))),
    );
    api.updateScene({ elements: [...final, ...keep], captureUpdate: CaptureUpdateAction.NEVER });
    snapshots.current = new Map(final.map((el) => [el.id, snapshotOf(el)]));
    runFileJobs(fileJobs);
    if (!fitted.current && scene.elements.length) {
      fitted.current = true;
      api.scrollToContent(undefined, { fitToContent: true, animate: false });
    }
  }, [store, team, isInteracting, animatedPoints, runFileJobs]);

  // Only the animating pens change per frame; the rest of the scene stays as built.
  const animate = useCallback(() => {
    animFrame.current = null;
    const api = apiRef.current;
    if (!api || !animations.current.size) return;
    if (!isInteracting()) {
      const scene = store.get();
      const running = new Set(animations.current.keys());
      const points = animatedPoints();
      const parts = scene.elements.filter((el) => running.has(el.id));
      const { elements: replacements } = build({ ...scene, elements: parts, claims: [], locks: [] }, {
        team, hiddenAuthors: optionsRef.current.hidden, authors: false, vizOn: optionsRef.current.vizOn, animated: points,
      });
      const current = api.getSceneElementsIncludingDeleted();
      const currentById = new Map(current.map((el) => [el.id, el]));
      const byId = new Map(replacements.map((el) => [el.id, { ...el, frameId: currentById.get(el.id)?.frameId ?? el.frameId }]));
      api.updateScene({ elements: current.map((el) => byId.get(el.id) || el), captureUpdate: CaptureUpdateAction.NEVER });
      for (const el of byId.values()) snapshots.current.set(el.id, snapshotOf(el));
      for (const id of running) {
        const anim = animations.current.get(id);
        const pts = points.get(id) || (anim ? anim.points : null);
        const source = scene.elements.find((el) => el.id === id);
        const last = pts && pts[pts.length - 1];
        if (source && last) moveCursor(source.author, last[0], last[1], [exIdOf(source)]);
      }
      pushCollaborators();
    }
    if (animations.current.size) animFrame.current = requestAnimationFrame(animate);
  }, [store, team, isInteracting, animatedPoints, pushCollaborators]); // eslint-disable-line react-hooks/exhaustive-deps

  function moveCursor(author, x, y, exIds) {
    if (!author) return;
    const color = authorChip(store.get(), author).bg;
    const selected = {};
    for (const id of exIds || []) selected[id] = true;
    collaborators.current.set(author, {
      at: Date.now(),
      collaborator: {
        id: author,
        socketId: author,
        username: authorName(author),
        color: { background: color, stroke: color },
        pointer: { x, y, tool: "pointer" },
        button: "up",
        selectedElementIds: selected,
      },
    });
  }

  // -- the scene store and the stream ------------------------------------------------

  const scheduleResync = useCallback(() => {
    clearTimeout(resyncTimer.current);
    resyncTimer.current = setTimeout(async () => {
      try {
        const scene = await getJSON(teamPath(team, "scene"));
        if (scene.version >= store.get().version) store.replace(scene, { kind: "scene", quiet: true });
      } catch {
        // the stream will catch up
      }
    }, RESYNC_MS);
  }, [store, team]);

  useEffect(() => {
    return store.subscribe((scene, detail) => {
      if (detail.kind === "ops") {
        let animateNow = false;
        for (const event of detail.events) {
          const own = ownBatches.current.has(event.batch);
          const author = event.author && event.author.name;
          const touchedIds = [];
          let focus = null;
          for (const change of event.changes || []) {
            if (change.target !== "element") continue;
            const value = change.value;
            const exId = value ? exIdOf(value) : null;
            if (value && value.client_id) {
              sentCreates.current.delete(value.client_id);
              clientToCanon.current.set(value.client_id, value.id);
            }
            if (exId) {
              overrides.current.delete(exId);
              overrides.current.delete(`${exId}~t`);
              touchedIds.push(exId);
            }
            if (!own && value) {
              focus = [value.x + (value.w || 0) / 2, value.y + (value.h || 0) / 2];
              if (value.type === "pen" && change.action === "add" && Array.isArray(value.points) && value.points.length > 2) {
                animations.current.set(value.id, { points: value.points, start: performance.now(), author, element: value });
                animateNow = true;
              }
            }
          }
          if (!own && focus && author) moveCursor(author, focus[0], focus[1], touchedIds);
        }
        if (detail.events.some((event) => event.op === "undo" || event.op === "clear")) scheduleResync();
        rebuild();
        pushCollaborators();
        if (animateNow && !animFrame.current) animFrame.current = requestAnimationFrame(animate);
        return;
      }
      if (detail.kind === "reset") fitted.current = false;
      rebuild();
    });
  }, [store, rebuild, animate, pushCollaborators, scheduleResync]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const timer = setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const [name, value] of collaborators.current) {
        if (now - value.at > CURSOR_LINGER_MS) {
          collaborators.current.delete(name);
          changed = true;
        }
      }
      if (changed) pushCollaborators();
    }, 2000);
    return () => clearInterval(timer);
  }, [pushCollaborators]);

  useEffect(() => {
    rebuild();
  }, [authors, hidden, teamRow && teamRow.viz, rebuild]); // eslint-disable-line react-hooks/exhaustive-deps

  // A build measures text with the fonts loaded at the time; one made before the fonts arrived
  // sized every text by a narrower fallback and clipped it. The first build waits for them, and
  // any font that loads later (Excalifont, Cascadia on legacy text) builds again.
  useEffect(() => {
    const fonts = typeof document !== "undefined" ? document.fonts : null;
    let cancelled = false;
    const again = () => {
      if (!cancelled) rebuild();
    };
    whenFontsReady().then(() => {
      fontsLoaded.current = true;
      again();
    });
    if (!fonts) return undefined;
    fonts.addEventListener("loadingdone", again);
    return () => {
      cancelled = true;
      fonts.removeEventListener("loadingdone", again);
    };
  }, [rebuild]);

  // QA hooks (canvas-v2-qa.md): the page's line breaks and overflow per fitted element, and
  // measureText in the canvas's own font strings for the text corpus calibration.
  useEffect(() => {
    window.__synapseFitAudit = (options = {}) => {
      const api = apiRef.current;
      return api ? fitAudit(store.get(), api.getSceneElements(), options) : [];
    };
    window.__synapseMeasure = async (strings, font = "normal", size = 20) => {
      const family = FONT_TO_EX[font] || FONT_TO_EX.normal;
      const fontString = fontStringOf(size, family);
      if (document.fonts) await document.fonts.load(fontString, "A");
      const context = document.createElement("canvas").getContext("2d");
      context.font = fontString;
      return { font: fontString, size, widths: (strings || []).map((text) => context.measureText(String(text)).width) };
    };
    return () => {
      delete window.__synapseFitAudit;
      delete window.__synapseMeasure;
    };
  }, [store]);

  useEffect(() => {
    if (visible && apiRef.current) apiRef.current.refresh();
  }, [visible]);

  // -- the human's edits -> operations ------------------------------------------------

  const sendOps = useCallback(
    async (ops, entries = null) => {
      try {
        const result = await postJSON(teamPath(team, "ops"), { ops, atomic: false });
        if (result.batch) ownBatches.current.add(result.batch);
        for (const [key, canonical] of Object.entries(result.aliases || {})) clientToCanon.current.set(key, canonical);
        const refused = result.refused || [];
        for (const refusal of refused) {
          const entry = entries && entries[refusal.index];
          if (!entry) continue;
          if (entry.create) {
            sentCreates.current.delete(entry.create);
            for (const id of entry.exIds) dropped.current.add(id);
          } else {
            for (const id of entry.exIds) overrides.current.delete(id);
          }
        }
        if (refused.length) toast(refusalText(refused), "warn");
        for (const warning of (result.warnings || []).slice(0, 1)) toast(warning.message, "info");
        return result;
      } catch (err) {
        for (const entry of entries || []) {
          if (entry.create) {
            sentCreates.current.delete(entry.create);
            for (const id of entry.exIds) dropped.current.add(id);
          } else {
            for (const id of entry.exIds) overrides.current.delete(id);
          }
        }
        toast(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err), "error");
        return null;
      }
    },
    [team, toast],
  );

  const upload = useCallback(
    async (element, file) => {
      if (!file || !file.dataURL) return null;
      let blob = await dataURLToBlob(file.dataURL);
      if (blob.type !== "image/png" && blob.type !== "image/jpeg") blob = await rasterize(file.dataURL, element.width, element.height, 2048);
      const stored = await postBytes(teamPath(team, "uploads"), blob, blob.type);
      return stored.asset;
    },
    [team],
  );

  const sync = useCallback(async () => {
    const api = apiRef.current;
    if (!api || !writable) return;
    if (syncing.current || isInteracting()) {
      clearTimeout(syncTimer.current);
      syncTimer.current = setTimeout(() => sync(), SYNC_DEBOUNCE_MS);
      return;
    }
    syncing.current = true;
    try {
      let elements = api.getSceneElementsIncludingDeleted();
      const files = api.getFiles();
      for (const el of elements) {
        if (el.type !== "image" || el.isDeleted || el.customData?.synapse || sentCreates.current.has(el.id) || uploads.current.has(el.fileId)) continue;
        try {
          const asset = await upload(el, files[el.fileId]);
          if (asset) uploads.current.set(el.fileId, asset);
        } catch (err) {
          dropped.current.add(el.id);
          toast(err instanceof ApiError ? `${err.code}: ${err.message}` : "the image could not be uploaded", "error");
        }
      }
      elements = api.getSceneElementsIncludingDeleted();
      const result = diff({
        elements, scene: store.get(), snapshots: snapshots.current, touched: touched.current, clientToCanon: clientToCanon.current,
        sentCreates: sentCreates.current, uploads: uploads.current,
      });
      touched.current = new Set();
      if (result.unsupported.length) {
        for (const id of result.unsupported) dropped.current.add(id);
        toast("embeds and generated frames are not part of the team canvas; they were removed", "info");
      }
      const entries = [...result.creates, ...result.updates];
      if (entries.length) {
        const now = Date.now();
        for (const entry of result.creates) sentCreates.current.add(entry.create);
        for (const entry of result.updates) for (const id of entry.exIds) overrides.current.set(id, now);
        await sendOps(entries.map((entry) => entry.op), entries);
      }
      if (entries.length || result.unsupported.length) rebuild();
    } finally {
      syncing.current = false;
    }
  }, [writable, isInteracting, upload, store, sendOps, rebuild, toast]);

  const scheduleSync = useCallback(() => {
    clearTimeout(syncTimer.current);
    syncTimer.current = setTimeout(() => sync(), SYNC_DEBOUNCE_MS);
  }, [sync]);

  const onChange = useCallback(
    (elements, appState) => {
      const selected = Object.keys(appState.selectedElementIds || {}).filter((id) => appState.selectedElementIds[id]);
      if (writable) {
        for (const id of selected) touched.current.add(id);
        if (appState.editingTextElement) {
          touched.current.add(appState.editingTextElement.id);
          if (appState.editingTextElement.containerId) touched.current.add(appState.editingTextElement.containerId);
        }
        scheduleSync();
      }
      const api = apiRef.current;
      const byId = new Map((api ? api.getSceneElements() : elements).map((el) => [el.id, el]));
      const ids = [];
      for (const id of selected) {
        const synapse = byId.get(id)?.customData?.synapse;
        if (synapse && synapse.id && (!synapse.derived || synapse.derived === "comment") && !ids.includes(synapse.id)) ids.push(synapse.id);
      }
      const key = ids.join(",");
      if (key !== selectionKey.current) {
        selectionKey.current = key;
        setSelection(ids);
      }
      // Excalidraw's own theme toggle: follow it in the chrome. Compared with the theme it last
      // reported, not the prop, so a prop change is never bounced back while Excalidraw catches up.
      if (appState.theme && appState.theme !== exTheme.current) {
        const first = exTheme.current === null;
        exTheme.current = appState.theme;
        if (!first && onTheme) onTheme(appState.theme);
      }
    },
    [writable, scheduleSync, onTheme],
  );

  const onPointerUpdate = useCallback(
    (payload) => {
      const wasDown = pointerDown.current;
      pointerDown.current = payload.button === "down";
      if (wasDown && !pointerDown.current) {
        if (pendingRebuild.current) setTimeout(() => rebuild(), 0);
        if (writable) scheduleSync();
      }
      if (hoverFrame.current) return;
      const { x, y } = payload.pointer;
      hoverFrame.current = requestAnimationFrame(() => {
        hoverFrame.current = null;
        const el = pointerDown.current ? null : elementAt(store.get(), x, y, optionsRef.current.hidden);
        setTooltip((prev) => {
          if (!el) return prev ? null : prev;
          if (prev && prev.id === el.id && Math.abs(prev.x - lastClient.current.x) < 40 && Math.abs(prev.y - lastClient.current.y) < 40) return prev;
          return { id: el.id, x: lastClient.current.x, y: lastClient.current.y, element: el };
        });
      });
    },
    [store, rebuild, scheduleSync, writable],
  );

  // -- exact exports for `canvas look --exact` ----------------------------------------------

  const exportRegion = useCallback(
    async (request) => {
      const api = apiRef.current;
      if (!api || !writable || !Array.isArray(request.region)) return;
      const [x0, y0, x1, y1] = request.region;
      const width = Math.max(1, x1 - x0);
      const height = Math.max(1, y1 - y0);
      // Everything overlapping the region (frames keep their children), plus an invisible
      // rectangle spanning it so the export covers the whole region; then crop to it.
      const all = api.getSceneElements().filter((el) => el.customData?.synapse?.derived !== "chip" && el.type !== "embeddable");
      const byId = new Map(all.map((el) => [el.id, el]));
      const overlaps = (el) => {
        const [ex0, ey0, ex1, ey1] = getCommonBounds([el]);
        return ex0 <= x1 && ex1 >= x0 && ey0 <= y1 && ey1 >= y0;
      };
      const chosen = all.filter((el) => overlaps(el) || (el.containerId && byId.has(el.containerId) && overlaps(byId.get(el.containerId))));
      const pin = restoreElements([{ type: "rectangle", id: "__synapse_region__", x: x0, y: y0, width, height, strokeColor: "transparent",
        backgroundColor: "transparent", strokeWidth: 1, roughness: 0 }], null)[0];
      const elements = [pin, ...chosen];
      const [minX, minY] = getCommonBounds(elements);
      const scale = Math.min(2, 1024 / Math.max(width, height));
      const full = await exportToCanvas({
        elements,
        appState: { ...api.getAppState(), exportBackground: true, viewBackgroundColor: CANVAS, exportWithDarkMode: false,
          frameRendering: { ...FRAME_RENDERING, name: false } },
        files: api.getFiles(),
        exportPadding: 0,
        getDimensions: (w, h) => ({ width: Math.max(1, Math.round(w * scale)), height: Math.max(1, Math.round(h * scale)), scale }),
      });
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(width * scale));
      canvas.height = Math.max(1, Math.round(height * scale));
      const crop = canvas.getContext("2d");
      crop.fillStyle = CANVAS;
      crop.fillRect(0, 0, canvas.width, canvas.height);
      crop.drawImage(full, Math.round((x0 - minX) * scale), Math.round((y0 - minY) * scale), canvas.width, canvas.height, 0, 0, canvas.width, canvas.height);
      const context = crop;
      const toPx = (x, y) => [(x - x0) * scale, (y - y0) * scale];
      if (request.grid) {
        context.fillStyle = "rgba(73,80,87,0.55)";
        context.font = "10px sans-serif";
        for (let gx = Math.ceil(x0 / 100) * 100; gx <= x1; gx += 100) {
          for (let gy = Math.ceil(y0 / 100) * 100; gy <= y1; gy += 100) {
            const [px, py] = toPx(gx, gy);
            context.fillRect(px - 1.5, py - 1.5, 3, 3);
            if (gx % 200 === 0 && gy % 200 === 0) context.fillText(`c${gx / 20}r${gy / 20}`, px + 3, py - 3);
          }
        }
      }
      if (request.marks) {
        context.font = MARK_FONT;
        for (const el of store.get().elements) {
          if (el.x > x1 || el.y > y1 || el.x + (el.w || 1) < x0 || el.y + (el.h || 1) < y0) continue;
          const [px, py] = toPx(Math.max(el.x, x0), Math.max(el.y, y0));
          const width = context.measureText(el.id).width + 8;
          context.fillStyle = INK;
          context.fillRect(px, py, width, 16);
          context.fillStyle = CANVAS;
          context.fillText(el.id, px + 4, py + 12);
        }
      }
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
      if (blob) await postBytes(teamPath(team, `exports/${encodeURIComponent(request.request_id)}`), blob, "image/png");
    },
    [team, writable, store],
  );

  useEffect(() => {
    return bus.subscribe("export_request", (data) => {
      if (data && data.team === team) exportRegion(data).catch(() => undefined);
    });
  }, [bus, team, exportRegion]);

  // -- live visuals ------------------------------------------------------------------------

  const onVizStill = useCallback(
    async (element, url) => {
      postStill(element, url);
    },
    [postStill],
  );

  const renderEmbeddable = useCallback(
    (el) => {
      const id = el.customData?.synapse?.id;
      const canonical = id ? store.get().elements.find((item) => item.id === id) : null;
      if (!canonical || canonical.type !== "viz") return null;
      return <VizFrame team={team} element={canonical} onStill={onVizStill} />;
    },
    [store, team, onVizStill],
  );

  // -- side panel actions -------------------------------------------------------------

  const selectedElements = useCallback(() => {
    const scene = store.get();
    return selection.map((id) => scene.elements.find((el) => el.id === id)).filter(Boolean);
  }, [store, selection]);

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
      const box = unionBounds(selectedElements());
      if (!box) return toast("select the area to lock first", "info");
      const pad = 20;
      await sendOps([{ op: "lock", region: [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad], label: label || "hands off" }]);
    },
    unlock: (id) => sendOps([{ op: "unlock", id }]),
    undo: (batch) => sendOps([{ op: "undo", batch }]),
    resolve: (id) => sendOps([{ op: "resolve", id }]),
    comment: async (text, replyTo) => {
      if (!text.trim()) return;
      const scene = store.get();
      let at = null;
      if (replyTo) {
        const parent = scene.elements.find((el) => el.id === replyTo);
        at = parent ? parent.on || parent.point || [parent.x, parent.y] : null;
      } else {
        const target = selectedElements().find((el) => el.type !== "comment");
        if (target) at = target.id;
      }
      if (!at) {
        const api = apiRef.current;
        const state = api.getAppState();
        at = [Math.round(-state.scrollX + state.width / 2 / state.zoom.value), Math.round(-state.scrollY + state.height / 2 / state.zoom.value)];
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
      const api = apiRef.current;
      const scene = store.get();
      const el = scene.elements.find((item) => item.id === id);
      if (!api || !el) return;
      const target = api.getSceneElements().find((item) => item.id === exIdOf(el));
      if (target) api.scrollToContent(target, { fitToViewport: false, animate: true });
    },
  };

  const tooltipElement = tooltip && tooltip.element;

  return (
    <div
      className="canvas-tab"
      onPointerMove={(event) => {
        lastClient.current = { x: event.clientX, y: event.clientY };
      }}
      onPointerLeave={() => setTooltip(null)}
    >
      <div className="canvas-host">
        <Excalidraw
          excalidrawAPI={(api) => {
            apiRef.current = api;
            setTimeout(() => rebuild(), 0);
          }}
          initialData={{ elements: [], appState: HUMAN_DEFAULTS }}
          theme={theme}
          onChange={onChange}
          onPointerUpdate={onPointerUpdate}
          viewModeEnabled={!writable}
          validateEmbeddable={(link) => typeof link === "string" && link.startsWith("/viz/")}
          renderEmbeddable={renderEmbeddable}
          onLinkOpen={(_element, event) => event.preventDefault()}
          aiEnabled={false}
          name={`${team} canvas`}
          UIOptions={{
            canvasActions: { loadScene: false, saveToActiveFile: false, export: false, clearCanvas: false, saveAsImage: true, toggleTheme: true },
            tools: { image: writable },
          }}
          renderTopRightUI={() => (
            <div className="canvas-top-right">
              <button type="button" className={authors ? "chip on" : "chip"} title="Show who made each mark as a small badge" onClick={() => setAuthors((v) => !v)}>
                show authors
              </button>
              <button type="button" className={panelOpen ? "chip on" : "chip"} onClick={() => setPanelOpen((v) => !v)}>
                panel
              </button>
            </div>
          )}
        >
          <MainMenu>
            <MainMenu.DefaultItems.SaveAsImage />
            <MainMenu.DefaultItems.SearchMenu />
            <MainMenu.DefaultItems.ToggleTheme />
            <MainMenu.DefaultItems.ChangeCanvasBackground />
            <MainMenu.DefaultItems.Help />
          </MainMenu>
        </Excalidraw>
      </div>
      {tooltipElement ? (
        <div className="hover-card" style={{ left: tooltip.x + 14, top: tooltip.y + 14 }}>
          <div className="hover-title">
            <AuthorChip scene={store.get()} name={tooltipElement.author} theme={theme} />
            {tooltipElement.id} {tooltipElement.type} · {authorName(tooltipElement.author)}
          </div>
          {tooltipElement.intent ? <div className="hover-intent">{tooltipElement.intent}</div> : null}
          {tooltipElement.type === "comment" ? <div className="hover-intent">“{tooltipElement.text}”</div> : null}
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

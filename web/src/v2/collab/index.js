// The collaboration layer of the v2 board (canvas-v2-phase5.md 12): the one hook Board calls.
//
// useCollab(...) -> {presence, proposals, freezes, settings, checkpoints, review, ctx, overlays,
// onDown, onEscape, onCursor, freezeSelection, page}
//   presence    what the server says everyone is doing (parsePresence), refreshed as TTLs fade
//   proposals   id -> the display list's proposal object, for the open ones
//   review      {open: id | null, queue: ids | null, at}
//   overlays    React nodes Board places: screen (the halo layer, for Surface's screenOverlay),
//               cards (Surface children), panel (under the side panel), chrome (the queue bar,
//               notices and the confirm dialog)
//   onDown(ev)  a Surface "down": true when it opened a proposal or a freeze (Board then starts
//               no gesture)
//
// Nothing here decides anything: every button sends an op through Board's sendOps, and the
// server answers. Presence is posted only from writable pages (the operator in person).
import React, { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { getJSON, postJSON, teamPath } from "../../api.js";
import { authorChip } from "../../theme/tokens.js";
import { camera as cameraMath } from "../render/index.js";
import {
  beginFreezeRegion,
  beneath,
  cameraFor,
  freezeAt,
  freezesOf,
  isFreezeEntry,
  isProposalId,
  proposalsOf,
  resultLines,
  reviewBox,
  reviewQueue,
  screenBox,
  touchedNotices,
  withoutOverlays,
} from "./model.js";
import * as collabOps from "./ops.js";
import { HEARTBEAT_MS, createPresencePoster, newPageId, parsePresence } from "./presence.js";
import { installCollabQA } from "./qa.js";

export { beginFreezeRegion, beneath, resultLines, withoutOverlays, collabOps };
export { isOverlayId } from "./model.js";

// The chrome (ui.jsx) is its own chunk: the board draws before it arrives.
const loadUI = () => import("./ui.jsx");
const lazyPart = (name) => React.lazy(() => loadUI().then((m) => ({ default: m[name] })));
const CollabPanel = lazyPart("CollabPanel");
const ReviewCard = lazyPart("ReviewCard");
const FreezeCard = lazyPart("FreezeCard");
const ConfirmDialog = lazyPart("ConfirmDialog");
const QueueBar = lazyPart("QueueBar");
const Notices = lazyPart("Notices");
// The halo layer too: nothing to draw until someone is present.
const Halos = lazyPart("Halos");

const TICK_MS = 1000;
const NOTICE_LIMIT = 4;

function useScene(store) {
  return useSyncExternalStore(
    (fn) => store.subscribe(fn),
    () => store.get(),
  );
}

const intersects = (a, b) => Array.isArray(a) && Array.isArray(b) && a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];

export function useCollab({
  team,
  writable,
  store,
  dl,
  bus,
  toast,
  sendOps,
  camera,
  setCamera,
  viewport,
  fitView = null,
  selection,
  setSelection,
  editingId = null,
  theme = "light",
  visible = true,
  rootRef = null,
  elementOf = () => null,
}) {
  const scene = useScene(store);
  const page = useMemo(() => newPageId(), []);
  const [presenceDoc, setPresenceDoc] = useState(null);
  const [now, setNow] = useState(() => Date.now());
  const [reviewId, setReviewId] = useState(null);
  const [queueAt, setQueueAt] = useState(null); // null: the queue is closed
  const [freezeId, setFreezeId] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [notices, setNotices] = useState([]);
  const [busy, setBusy] = useState(false);
  const halosRef = useRef(null);
  const noticed = useRef(new Set());
  const noticeId = useRef(0);

  const live = useRef({});
  // The part of the view the side panel leaves clear: cards and zooms stay out from under it (QA phase 5 L10).
  const clear = fitView || viewport;
  live.current = { dl, camera, viewport, clear, selection, reviewId, queueAt, scene };

  // -- presence: reading ------------------------------------------------------------------------

  useEffect(() => {
    if (!team) return undefined;
    let gone = false;
    getJSON(teamPath(team, "presence"))
      .then((doc) => {
        if (!gone && doc) setPresenceDoc((prev) => prev || doc);
      })
      .catch(() => undefined);
    const off = bus.subscribe("presence", (data) => {
      if (data && (!data.team || data.team === team)) setPresenceDoc(data);
    });
    return () => {
      gone = true;
      off();
    };
  }, [team, bus]);

  const presence = useMemo(() => parsePresence(presenceDoc, { now, page }), [presenceDoc, now, page]);
  const anyFresh = presence.members.length + presence.operators.length > 0;
  useEffect(() => {
    setNow(Date.now());
    if (!anyFresh) return undefined;
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [anyFresh, presenceDoc]);

  // -- presence: posting (writable pages only) -----------------------------------------------------

  const poster = useMemo(() => {
    if (!writable || !team) return null;
    return createPresencePoster({ page, post: (body, { keepalive } = {}) => postJSON(teamPath(team, "presence"), body, { keepalive }) });
  }, [writable, team, page]);
  useEffect(() => () => poster && poster.stop(), [poster]);

  useEffect(() => {
    if (!poster || !viewport || !(viewport.w > 0) || !camera) return;
    poster.update({ viewport: cameraMath.viewRect(camera, viewport) }, "debounce");
  }, [poster, camera, viewport]);
  const selectionKey = (selection || []).join(",");
  useEffect(() => {
    if (poster) poster.update({ selection: withoutOverlays(selection) }, "now");
  }, [poster, selectionKey]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (poster) poster.update({ editing: editingId || null }, "now");
  }, [poster, editingId]);
  useEffect(() => {
    if (!poster) return undefined;
    const beat = setInterval(() => {
      if (typeof document === "undefined" || document.visibilityState === "visible") poster.heartbeat();
    }, HEARTBEAT_MS);
    const onVisibility = () => (document.visibilityState === "hidden" ? poster.away() : poster.back());
    const onHide = () => poster.away();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onHide);
    return () => {
      clearInterval(beat);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onHide);
    };
  }, [poster]);
  useEffect(() => {
    if (poster && !visible) poster.away();
    else if (poster && visible) poster.back();
  }, [poster, visible]);

  const onCursor = useCallback(
    (world) => {
      if (poster && Array.isArray(world)) poster.update({ cursor: [world[0], world[1]] }, "throttle");
    },
    [poster],
  );

  // -- proposals, freezes, the queue ------------------------------------------------------------------

  const proposals = useMemo(() => proposalsOf(dl), [dl]);
  const freezes = useMemo(() => freezesOf(dl), [dl]);
  const queue = useMemo(() => (queueAt === null ? null : reviewQueue(dl, scene)), [queueAt, dl, scene]);

  // A proposal that left the list (accepted, rejected, withdrawn, superseded) closes its card; a
  // freeze thawed closes its card.
  useEffect(() => {
    if (reviewId && !proposals.has(reviewId)) setReviewId(null);
    if (freezeId && !freezes.some((f) => f.id === freezeId)) setFreezeId(null);
  }, [proposals, freezes]); // eslint-disable-line react-hooks/exhaustive-deps

  const zoomToBox = useCallback(
    (bbox) => {
      const next = cameraFor(bbox, live.current.clear);
      if (next) setCamera(next);
    },
    [setCamera],
  );

  const openReview = useCallback(
    (id, { zoom = false } = {}) => {
      if (!isProposalId(id)) return;
      setFreezeId(null);
      setReviewId(id);
      setSelection([id]);
      const box = zoom ? reviewBox(live.current.dl, id) : null;
      if (!box) return;
      const vr = cameraMath.viewRect(live.current.camera, live.current.viewport);
      if (!intersects(box, vr) || zoom === "always") zoomToBox(box);
    },
    [setSelection, zoomToBox],
  );

  const step = useCallback(
    (delta) => {
      const ids = reviewQueue(live.current.dl, live.current.scene);
      if (!ids.length) return;
      const current = live.current.reviewId ? ids.indexOf(live.current.reviewId) : -1;
      const at = current < 0 ? (delta > 0 ? 0 : ids.length - 1) : (current + delta + ids.length) % ids.length;
      setQueueAt(at);
      openReview(ids[at], { zoom: "always" });
    },
    [openReview],
  );

  const toggleQueue = useCallback(() => {
    if (live.current.queueAt !== null) {
      setQueueAt(null);
      return;
    }
    const ids = reviewQueue(live.current.dl, live.current.scene);
    setQueueAt(0);
    if (ids.length) openReview(ids[0], { zoom: "always" });
  }, [openReview]);

  // Keep the queue's position on the open card as proposals come and go.
  useEffect(() => {
    if (queueAt === null || !queue) return;
    const at = reviewId ? queue.indexOf(reviewId) : -1;
    if (at >= 0 && at !== queueAt) setQueueAt(at);
  }, [queue, reviewId]); // eslint-disable-line react-hooks/exhaustive-deps

  // -- sending --------------------------------------------------------------------------------------

  const run = useCallback(
    async (op, opts) => {
      if (!op) return null;
      setBusy(true);
      try {
        return await sendOps([op], opts);
      } finally {
        setBusy(false);
      }
    },
    [sendOps],
  );

  const nextAfterDecision = useCallback(
    (id) => {
      if (live.current.queueAt === null) {
        setReviewId(null);
        return;
      }
      const ids = reviewQueue(live.current.dl, live.current.scene).filter((x) => x !== id);
      if (!ids.length) {
        setReviewId(null);
        return;
      }
      const at = Math.min(live.current.queueAt, ids.length - 1);
      setQueueAt(at);
      openReview(ids[at], { zoom: "always" });
    },
    [openReview],
  );

  // What the lazily loaded chrome builds its actions from (actions.js): stable across renders,
  // everything that changes is read through `live`.
  const ctx = useMemo(
    () => ({ run, live, openReview, nextAfterDecision, zoomToBox, toast, elementOf, setConfirm, setFreezeId, setReviewId }),
    [run, openReview, nextAfterDecision, zoomToBox, toast, elementOf],
  );

  // Freeze with a selection: freeze {ids}. Returns false when there is nothing selected to freeze
  // (Board then arms the region tool).
  const freezeSelection = useCallback(() => {
    const op = collabOps.buildFreezeIds(withoutOverlays(live.current.selection));
    if (!op) return false;
    run(op);
    return true;
  }, [run]);

  // -- notices: an agent's live change to the operator's marks (12.5) ----------------------------------

  useEffect(() => {
    return store.subscribe((_scene, detail) => {
      if (!writable || !detail || detail.kind !== "ops") return;
      const found = touchedNotices(detail.events, { elementOf, seen: noticed.current });
      if (!found.length) return;
      setNotices((list) =>
        list
          .concat(
            found.map((n) => {
              noticeId.current += 1;
              return { id: noticeId.current, kind: "touched", text: n.text, tone: "info", batch: n.batch };
            }),
          )
          .slice(-NOTICE_LIMIT),
      );
    });
  }, [store, writable, elementOf]);

  // -- pointer --------------------------------------------------------------------------------------------

  // A Surface "down" (the select or hand tool): a proposal's ghost opens its review card, a
  // freeze's outline or label its card, unless a handle, a connection point or an element beneath
  // takes the press (a frozen element stays the operator's to edit). `hitBelow(ev, skip)` is the
  // renderer's hit test without the collaboration overlays. True when a card took the press.
  const onDown = useCallback(
    (ev, tool, hitBelow = () => null) => {
      if (!ev || ev.button !== 0) return false;
      const current = live.current.dl;
      // A drawing tool draws over a ghost or an outline as over anything.
      if (tool !== "select" && tool !== "hand") return false;
      const onHandle = Boolean(ev.handle || ev.connector);
      if (!onHandle && ev.hit && isProposalId(ev.hit)) {
        openReview(ev.hit);
        return true;
      }
      const hitEntry = ev.hit && current ? current.entries.find((e) => e.id === ev.hit) : null;
      const onFreeze = hitEntry && isFreezeEntry(hitEntry);
      const below = onFreeze ? beneath(ev, current, hitBelow).hit : ev.hit;
      if (!onHandle && !below && tool === "select") {
        const fid = onFreeze ? hitEntry.id : freezeAt(current, ev.world, ev.scale);
        if (fid) {
          setReviewId(null);
          setFreezeId(fid);
          return true;
        }
      }
      // A press anywhere else closes a card (the queue bar stays).
      if (live.current.reviewId) setReviewId(null);
      setFreezeId(null);
      return false;
    },
    [openReview],
  );

  // Esc: a card closes first. Returns true when it used the key.
  const onEscape = useCallback(() => {
    if (confirm) {
      setConfirm(null);
      return true;
    }
    if (live.current.reviewId || freezeId) {
      setReviewId(null);
      setFreezeId(null);
      return true;
    }
    if (live.current.queueAt !== null) {
      setQueueAt(null);
      return true;
    }
    return false;
  }, [confirm, freezeId]);

  // -- QA -----------------------------------------------------------------------------------------------------

  useEffect(
    () =>
      installCollabQA(() => ({
        root: rootRef ? rootRef.current : null,
        halos: halosRef.current,
        dl: live.current.dl,
        reviewOpen: Boolean(live.current.reviewId),
        queue: live.current.queueAt === null ? 0 : reviewQueue(live.current.dl, live.current.scene).length,
      })),
    [], // eslint-disable-line react-hooks/exhaustive-deps
  );

  // -- drawing -----------------------------------------------------------------------------------------------

  // One map per list, so a pan (which redraws the halos) never scans the entries.
  const boxes = useMemo(() => new Map(((dl && dl.entries) || []).map((e) => [e.id, e.bbox])), [dl]);
  const bboxOf = useCallback((id) => boxes.get(id) || null, [boxes]);
  const chipOf = useCallback((name) => authorChip(store.get(), name, theme), [store, theme]);
  const neutral = (dl && dl.palettes && dl.palettes[theme] && dl.palettes[theme]["tone.neutral.stroke"]) || "#8b8d98";

  const h = React.createElement;
  const screen = anyFresh ? h(React.Suspense, { fallback: null }, h(Halos, { presence, camera, viewport, bboxOf, chipOf, theme, neutral, layerRef: halosRef })) : null;

  const reviewProposal = reviewId ? proposals.get(reviewId) : null;
  const ownerOf = (p) => {
    if (!p) return null;
    if (p.reason === "peer") {
      const el = elementOf((p.targets || [])[0]);
      return el ? el.author : null;
    }
    if (p.reason === "foreign_lane") {
      const claim = (scene.claims || []).find((c) => c && c.author !== p.author && intersects(c.region, p.bbox));
      if (claim) return claim.author;
      const home = Object.entries(scene.homes || {}).find(([name, box]) => name !== p.author && intersects(box, p.bbox));
      return home ? home[0] : null;
    }
    return null;
  };
  const openFreeze = freezeId ? freezes.find((f) => f.id === freezeId) : null;
  const freezeRecord = openFreeze
    ? { id: openFreeze.id, ...((scene.freezes || []).find((f) => f.id === openFreeze.id) || {}), ...(openFreeze.freeze || {}) }
    : null;
  // Each lazy part in its own Suspense: nothing shows until the chrome's chunk is in.
  const lazy = (key, type, props) => h(React.Suspense, { key, fallback: null }, h(type, props));
  const cards = h(
    React.Fragment,
    null,
    reviewProposal
      ? lazy("review", ReviewCard, {
          id: reviewId,
          proposal: reviewProposal,
          box: screenBox(reviewProposal.bbox, camera),
          viewport: clear,
          writable,
          chip: chipOf(reviewProposal.author),
          owner: ownerOf(reviewProposal),
          busy,
          ctx,
          onClose: () => setReviewId(null),
        })
      : null,
    freezeRecord
      ? lazy("freeze", FreezeCard, {
          freeze: freezeRecord,
          box: screenBox(openFreeze.bbox, camera),
          viewport: clear,
          writable,
          ctx,
          onClose: () => setFreezeId(null),
        })
      : null,
  );

  const panel = useMemo(
    () => lazy("panel", CollabPanel, { scene, presence, dlProposals: proposals, writable, theme, ctx, now }),
    [scene, presence, proposals, writable, theme, ctx, now], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const chrome = h(
    React.Fragment,
    null,
    queue ? lazy("queue", QueueBar, { queue, at: queueAt || 0, onPrev: () => step(-1), onNext: () => step(1), onClose: () => setQueueAt(null) }) : null,
    notices.length ? lazy("notices", Notices, { notices, ctx, onDismiss: (id) => setNotices((list) => list.filter((n) => n.id !== id)) }) : null,
    confirm
      ? lazy("confirm", ConfirmDialog, {
          open: true,
          title: confirm.title,
          text: confirm.text,
          confirm: confirm.confirm,
          onCancel: () => setConfirm(null),
          onConfirm: () => {
            const job = confirm;
            setConfirm(null);
            if (job) job.run();
          },
        })
      : null,
  );

  return {
    presence,
    proposals,
    freezes,
    settings: { ...collabOps.DEFAULT_SETTINGS, ...((scene.settings && scene.settings.collab) || {}) },
    checkpoints: scene.checkpoints || [],
    review: { open: reviewId, queue, at: queueAt, count: proposals.size, toggle: toggleQueue, step },
    ctx,
    overlays: { screen, cards, panel, chrome },
    onDown,
    onEscape,
    onCursor,
    freezeSelection,
    page,
  };
}

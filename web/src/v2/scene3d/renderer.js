// The one shared WebGLRenderer (canvas-v2-phase3-4.md 3.7, D13). Every scene3d slot is a placeholder
// div in the Surface's HTML layer; one canvas, in the Surface's GL host (between the SVG world and the
// HTML layer, pointer-events none), draws every visible placeholder's scene into its rectangle with
// a viewport and a scissor (the three.js "multiple elements" pattern). Panning and zooming move the
// placeholders; the renderer follows them.
//
// Rules:
// - On demand: a frame is drawn when a scene is added, changed or entered, the theme changes, or a
//   placeholder moved. While any moves, one requestAnimationFrame loop runs; it stops after 500 ms
//   with nothing moving and is re-armed by wheel, a pointer move with a button down, resize (of the
//   window or of the GL host), every camera change of the Surface (its "sv2-camera" event on the
//   host: keyboard zoom, fit and QA hooks move the placeholders with no pointer event), a
//   display-list change and the orbit controls.
// - Per frame: clear the canvas, then for each visible placeholder in DOM order set the viewport and
//   scissor to its rectangle, clear it to the card's paper and render. Under 48 px on its shorter
//   side, past 16 scenes, or past 4 megapixels, a scene is not drawn: the SVG beneath shows its
//   still or its drawing (LOD). Past 1,000,000 triangles a frame, models draw their proxy box.
// - Off screen nothing renders; document.hidden stops the loop.
// - Context loss: every scene shows its still or drawing (the SVG beneath); on restore the scenes are
//   rebuilt once. A second loss within 60 s leaves the stills showing and logs once.
import { Vector2, WebGLRenderer } from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { glBudget } from "./budget.js";
import { buildScene, assetsOf, readSolved } from "./scene.js";
import { cameraSpec, fitAspect, frame, makeCamera, orbitOf, STILL_VIEWS } from "./cameras.js";
import { loadGLB } from "./loaders/gltf.js";
import { labelSizePx, sizeLabels } from "./labels.js";
import { captureStill, createStillQueue } from "./stills.js";
import { MAX_TRIANGLES, moved, pixelRatio, planFrame } from "./viewport.js";
import { setActive } from "./qa.js";

const IDLE_MS = 500;
const MIN_LABEL_PX = 5;
const RELOSS_MS = 60000;
const DISPOSE_AFTER_MS = 5000;
const renderers = new WeakMap(); // host element -> SceneRenderer
let current = null; // the page's live renderer (one Surface per page)

const nowMs = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
const raf = (fn) => (typeof requestAnimationFrame === "function" ? requestAnimationFrame(fn) : setTimeout(() => fn(nowMs()), 16));
const caf = (id) => (typeof cancelAnimationFrame === "function" ? cancelAnimationFrame(id) : clearTimeout(id));

function domOrder(a, b) {
  if (a === b || !a.compareDocumentPosition) return 0;
  return a.compareDocumentPosition(b) & 4 ? -1 : 1; // Node.DOCUMENT_POSITION_FOLLOWING
}

class SceneRenderer {
  constructor(host) {
    this.host = host;
    this.regs = new Map(); // key -> registration
    this.builds = new Map(); // "id@v@theme" -> built scene (the few most recent)
    this.stills = createStillQueue();
    this.frames = 0;
    this.lastInfo = { calls: 0, triangles: 0 };
    this.loop = 0;
    this.idleSince = 0;
    this.dirty = true;
    this.lost = false;
    this.failed = false;
    this.losses = [];
    this.lease = glBudget.request("shared", "scene3d");
    this.canvas = document.createElement("canvas");
    this.canvas.className = "sv2-gl";
    this.canvas.setAttribute("data-gl", "scene3d");
    Object.assign(this.canvas.style, { position: "absolute", left: "0", top: "0", width: "100%", height: "100%", pointerEvents: "none" });
    host.appendChild(this.canvas);
    this.gl = new WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: true, powerPreference: "low-power" });
    this.gl.autoClear = false;
    this.gl.setPixelRatio(pixelRatio(window.devicePixelRatio));
    this.gl.setClearColor(0x000000, 0);
    this.onLost = (e) => {
      e.preventDefault();
      this.lost = true;
      this.losses = this.losses.filter((t) => nowMs() - t < RELOSS_MS).concat([nowMs()]);
      if (this.losses.length > 1 && !this.failed) {
        this.failed = true;
        console.warn("[synapse v2] the 3D renderer lost its WebGL context twice within a minute; scenes show their stills");
      }
      for (const reg of this.regs.values()) reg.drawn = false;
      this.notify();
    };
    this.onRestored = () => {
      this.lost = false;
      if (this.failed) return;
      for (const b of this.builds.values()) b.dispose();
      this.builds.clear();
      this.restores = (this.restores || 0) + 1;
      this.invalidate();
    };
    this.canvas.addEventListener("webglcontextlost", this.onLost, false);
    this.canvas.addEventListener("webglcontextrestored", this.onRestored, false);
    this.arm = () => this.start();
    this.onPointerMove = (e) => {
      if (e.buttons) this.start();
    };
    this.onDown = () => this.stills.pause(true);
    this.onUp = () => {
      this.stills.pause(false);
      this.start();
    };
    this.onVisibility = () => {
      if (!document.hidden) this.invalidate();
    };
    window.addEventListener("wheel", this.arm, { passive: true, capture: true });
    window.addEventListener("pointermove", this.onPointerMove, { passive: true, capture: true });
    window.addEventListener("pointerdown", this.onDown, { passive: true, capture: true });
    window.addEventListener("pointerup", this.onUp, { passive: true, capture: true });
    window.addEventListener("pointercancel", this.onUp, { passive: true, capture: true });
    window.addEventListener("resize", this.arm);
    host.addEventListener("sv2-camera", this.arm);
    if (typeof ResizeObserver === "function") {
      this.hostObserver = new ResizeObserver(this.arm);
      this.hostObserver.observe(host);
    }
    document.addEventListener("visibilitychange", this.onVisibility);
  }

  // -- registrations --------------------------------------------------------------------------------

  register(key, props) {
    clearTimeout(this.disposeTimer);
    const reg = { key, ...props, renders: 0, drawn: false, rect: null, state: "pending", listeners: new Set(), orbit: null, assets: new Map(), loading: false, reason: null };
    this.regs.set(key, reg);
    this.prepare(reg);
    this.invalidate();
    return reg;
  }

  update(key, props) {
    const reg = this.regs.get(key);
    if (!reg) return;
    const changed = props.version !== reg.version || props.theme !== reg.theme || props.palette !== reg.palette || props.element !== reg.element;
    Object.assign(reg, props);
    if (changed) this.prepare(reg);
    this.invalidate();
  }

  unregister(key) {
    const reg = this.regs.get(key);
    if (!reg) return;
    this.exit(key);
    this.regs.delete(key);
    this.stills.drop(`${reg.id}@`);
    this.invalidate();
    if (!this.regs.size) this.disposeTimer = setTimeout(() => this.destroy(), DISPOSE_AFTER_MS);
  }

  subscribe(key, fn) {
    const reg = this.regs.get(key);
    if (!reg) return () => {};
    reg.listeners.add(fn);
    return () => reg.listeners.delete(fn);
  }

  notify() {
    for (const reg of this.regs.values()) for (const fn of reg.listeners) fn({ drawn: reg.drawn, reason: reg.reason, entered: !!reg.orbit });
  }

  // Loads the GLB assets a scene names, then (re)builds it.
  prepare(reg) {
    const names = assetsOf(reg.element).filter((n) => !reg.assets.has(n));
    const solved = readSolved(reg.element);
    if (!names.length || !reg.urls?.asset) return;
    reg.loading = true;
    Promise.allSettled(
      names.map((name) => {
        const entry = [...solved.objects.values()].find((o) => o.asset === name);
        return loadGLB(name, reg.urls.asset, { expectTris: entry ? entry.tris : null }).then((model) => reg.assets.set(name, model));
      }),
    ).then((results) => {
      reg.loading = false;
      reg.loadFailed = results.filter((r) => r.status === "rejected").map((r) => String(r.reason?.message || r.reason));
      if (reg.loadFailed.length) console.warn(`[synapse v2] ${reg.id}: ${reg.loadFailed.join("; ")}`);
      for (const k of [...this.builds.keys()]) if (k.startsWith(`${reg.id}@`)) this.dropBuild(k);
      this.invalidate();
    });
  }

  dropBuild(k) {
    const b = this.builds.get(k);
    if (b) b.dispose();
    this.builds.delete(k);
  }

  built(reg, palette, theme) {
    const k = `${reg.id}@${reg.version}@${theme}@${reg.assets.size}`;
    if (!this.builds.has(k)) {
      for (const old of [...this.builds.keys()]) if (old.startsWith(`${reg.id}@`) && old.split("@")[2] === theme) this.dropBuild(old);
      this.builds.set(k, buildScene(reg.element, reg.assets, palette));
    }
    return this.builds.get(k);
  }

  // -- entering: orbit controls on this scene's own camera --------------------------------------------

  /** Orbit controls on `key`'s own camera, listening on `surface` (the placeholder's orbit layer). */
  enter(key, surface = null) {
    const reg = this.regs.get(key);
    if (!reg || reg.orbit) return;
    const b = this.built(reg, reg.palette, reg.theme);
    const base = cameraSpec(b.spec, { preset: "orbit", projection: "persp", zoom: 1 });
    const start = cameraSpec(b.spec.preset === "orbit" ? b.spec : { ...base, az: b.spec.az, el: b.spec.el }, { projection: "persp" });
    const f = frame(b.framed, start, reg.rect ? reg.rect.width / Math.max(1, reg.rect.height) : 1, b.centreOf, b.fitPoints);
    const f1 = frame(b.framed, { ...start, zoom: 1 }, 1, b.centreOf, b.fitPoints);
    const camera = makeCamera(f);
    const controls = new OrbitControls(camera, surface || reg.el);
    controls.target.set(...f.target);
    controls.enableDamping = false;
    controls.update();
    const onChange = () => this.invalidate();
    controls.addEventListener("change", onChange);
    reg.orbit = { camera, controls, target: f.target, baseDist: Math.hypot(f1.eye[0] - f1.target[0], f1.eye[1] - f1.target[1], f1.eye[2] - f1.target[2]), onChange };
    this.invalidate();
    this.notify();
  }

  exit(key) {
    const reg = this.regs.get(key);
    if (!reg || !reg.orbit) return;
    reg.orbit.controls.removeEventListener("change", reg.orbit.onChange);
    reg.orbit.controls.dispose();
    reg.orbit = null;
    this.invalidate();
    this.notify();
  }

  /** The entered camera as {preset: "orbit", az, el, zoom} ("Save view"), or null. */
  view(key) {
    const reg = this.regs.get(key);
    if (!reg || !reg.orbit) return null;
    const target = reg.orbit.controls.target.toArray();
    return { preset: "orbit", ...orbitOf(reg.orbit.camera, target, reg.orbit.baseDist) };
  }

  // -- the loop --------------------------------------------------------------------------------------

  invalidate() {
    this.dirty = true;
    this.start();
  }

  start() {
    this.idleSince = nowMs();
    if (this.loop || this.destroyed) return;
    this.loop = raf((t) => this.tick(t));
  }

  tick() {
    this.loop = 0;
    if (this.destroyed || (typeof document !== "undefined" && document.hidden)) return;
    const movedNow = this.measure();
    if (movedNow || this.dirty) {
      this.idleSince = nowMs();
      this.draw();
    }
    // One queued still a frame, drawn off screen (stills.js), after the frame that queued it.
    if (this.stills.size > 0 && !this.stills.paused && !this.lost && !this.failed) this.takeStill();
    if (nowMs() - this.idleSince < IDLE_MS || this.stills.size) this.loop = raf((t) => this.tick(t));
  }

  // Reads every placeholder's rectangle; true when one moved.
  measure() {
    let any = false;
    for (const reg of this.regs.values()) {
      const r = reg.el && reg.el.isConnected ? reg.el.getBoundingClientRect() : null;
      const rect = r ? { left: r.left, top: r.top, width: r.width, height: r.height } : null;
      if (moved(rect, reg.rect)) any = true;
      reg.rect = rect;
    }
    const h = this.host.getBoundingClientRect();
    const hostRect = { left: h.left, top: h.top, width: h.width, height: h.height };
    if (moved(hostRect, this.hostRect)) any = true;
    this.hostRect = hostRect;
    return any;
  }

  draw() {
    this.dirty = false;
    if (this.lost || this.failed || !this.hostRect) return;
    const gl = this.gl;
    const pr = pixelRatio(window.devicePixelRatio);
    if (gl.getPixelRatio() !== pr) gl.setPixelRatio(pr);
    const size = gl.getSize(new Vector2());
    if (Math.round(size.x) !== Math.round(this.hostRect.width) || Math.round(size.y) !== Math.round(this.hostRect.height)) {
      gl.setSize(Math.max(1, Math.round(this.hostRect.width)), Math.max(1, Math.round(this.hostRect.height)), false);
    }
    const regs = [...this.regs.values()].filter((r) => r.rect).sort((a, b) => domOrder(a.el, b.el));
    const plan = planFrame(regs.map((r) => ({ key: r.key, rect: r.rect })), this.hostRect, pr);
    gl.setScissorTest(false);
    gl.setClearColor(0x000000, 0);
    gl.clear(true, true, true);
    gl.setScissorTest(true);
    let tris = 0;
    let calls = 0;
    let triangles = 0;
    let changed = false;
    plan.forEach((step, i) => {
      const reg = regs[i];
      const wasDrawn = reg.drawn;
      reg.reason = step.reason;
      reg.visible = step.reason !== "off";
      if (!step.draw) {
        reg.drawn = false;
        if (wasDrawn) changed = true;
        return;
      }
      const b = this.built(reg, reg.palette, reg.theme);
      const overBudget = tris + b.tris > MAX_TRIANGLES;
      b.proxyModels(overBudget);
      reg.proxied = overBudget && b.models.length > 0;
      tris += overBudget ? b.tris - b.models.reduce((n, m) => n + (m.tris || 0), 0) : b.tris;
      const camera = reg.orbit ? reg.orbit.camera : b.camera;
      const [vx, vy, vw, vh] = step.vp.viewport;
      const aspect = vw / Math.max(1, vh);
      if (reg.orbit) {
        camera.aspect = aspect;
        camera.updateProjectionMatrix();
      } else b.fit(aspect);
      // Labels are 12 units of the board (they zoom with it, like every other label); under 5 px on
      // screen they are hidden.
      const labelPx = labelSizePx() * (vh / Math.max(1, reg.box ? reg.box[1] : vh));
      for (const sprite of b.labels) sprite.visible = labelPx >= MIN_LABEL_PX;
      sizeLabels(b.scene, camera, vh, labelPx);
      gl.setViewport(vx, vy, vw, vh);
      gl.setScissor(...step.vp.scissor);
      gl.setClearColor(reg.paper || "#ffffff", 1);
      gl.clear(true, true, true);
      gl.render(b.scene, camera);
      if (this.probing) reg.ink = this.inkOf(step.vp.scissor, pr, reg.paper || "#ffffff");
      calls += gl.info.render.calls;
      triangles += gl.info.render.triangles;
      reg.renders += 1;
      reg.tris = b.tris;
      if (!wasDrawn) changed = true;
      reg.drawn = true;
      this.queueStills(reg);
    });
    this.frames += 1;
    this.lastInfo = { calls, triangles };
    if (this.probing) {
      const done = this.probing;
      this.probing = null;
      done();
    }
    if (changed) this.notify();
  }

  // -- stills ----------------------------------------------------------------------------------------

  queueStills(reg) {
    if (!reg.onStill || !reg.writable) return;
    for (const view of STILL_VIEWS) {
      const key = `${reg.id}@${reg.version}@${view}`;
      this.stills.add(key, { id: reg.id, version: reg.version, view, key: reg.key });
    }
  }

  takeStill() {
    const next = this.stills.next();
    if (!next) return;
    const { job } = next;
    const reg = this.regs.get(job.key);
    if (!reg || reg.version !== job.version || !reg.onStill) return;
    try {
      const b = this.built(reg, reg.stillPalette || reg.palette, "light");
      const spec = cameraSpec({ preset: job.view });
      const f = frame(b.framed, spec, reg.box[0] / Math.max(1, reg.box[1]), b.centreOf, b.fitPoints);
      const camera = makeCamera(f);
      b.proxyModels(false);
      for (const sprite of b.labels) sprite.visible = true;
      captureStill(this.gl, b.scene, camera, reg.box, reg.stillPaper || "#ffffff", {
        fitCamera: (cam, aspect) => fitAspect(cam, f, aspect),
        sizeLabels,
      })
        .then((blob) => {
          (reg.stillsPosted || (reg.stillsPosted = [])).push(job.view);
          reg.onStill(job.id, job.version, blob, job.view);
        })
        .catch(() => this.stills.forget(next.key));
    } catch (err) {
      this.stills.forget(next.key);
      console.warn(`[synapse v2] ${job.id}: the ${job.view} still failed: ${err.message}`);
    }
  }

  // -- QA ----------------------------------------------------------------------------------------------

  // The share of a drawn rectangle's pixels (sampled on a grid) that differ from the paper: the
  // e2e runs' proof that a scene put something on screen. Only while a probe is asked for.
  inkOf(scissor, pr, paper) {
    const gl = this.gl.getContext();
    const [x, y, w, h] = scissor.map((v) => Math.round(v * pr));
    if (w <= 0 || h <= 0) return 0;
    const pixels = new Uint8Array(w * h * 4);
    gl.readPixels(x, y, w, h, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(paper) || ["", "ff", "ff", "ff"];
    const ref = [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
    let ink = 0;
    let n = 0;
    const step = Math.max(1, Math.floor(Math.sqrt((w * h) / 4000)));
    for (let row = 0; row < h; row += step) {
      for (let col = 0; col < w; col += step) {
        const i = (row * w + col) * 4;
        n += 1;
        if (Math.abs(pixels[i] - ref[0]) + Math.abs(pixels[i + 1] - ref[1]) + Math.abs(pixels[i + 2] - ref[2]) > 24) ink += 1;
      }
    }
    return n ? Math.round((ink / n) * 1000) / 1000 : 0;
  }

  /** Measures every drawn scene's ink on the next frame; resolves to {id: share}. */
  probe() {
    return new Promise((resolve) => {
      const prev = this.probing;
      this.probing = () => {
        if (prev) prev();
        const out = {};
        for (const r of this.regs.values()) if (r.drawn) out[r.id] = r.ink ?? 0;
        resolve(out);
      };
      this.invalidate();
    });
  }

  qa() {
    return {
      renderer: { calls: this.lastInfo.calls, triangles: this.lastInfo.triangles, frames: this.frames, lost: this.lost, failed: this.failed, restores: this.restores || 0 },
      scenes: [...this.regs.values()].map((r) => ({
        id: r.id,
        version: r.version,
        visible: !!r.visible,
        rendered: !!r.drawn,
        renders: r.renders,
        reason: r.reason,
        tris: r.tris ?? null,
        models: r.assets.size,
        modelTris: [...r.assets.values()].reduce((n, m) => n + (m.userData?.tris || 0), 0),
        expectTris: [...readSolved(r.element).objects.values()].reduce((n, o) => n + (o.tris || 0), 0),
        loading: !!r.loading,
        failed: r.loadFailed && r.loadFailed.length ? r.loadFailed : null,
        proxied: !!r.proxied,
        entered: !!r.orbit,
        rect: r.rect ? [r.rect.left, r.rect.top, r.rect.width, r.rect.height].map((v) => Math.round(v)) : null,
        stills: [...(r.stillsPosted || [])],
      })),
    };
  }

  // -- teardown --------------------------------------------------------------------------------------

  destroy() {
    if (this.destroyed || this.regs.size) return;
    this.destroyed = true;
    if (this.loop) caf(this.loop);
    for (const b of this.builds.values()) b.dispose();
    this.builds.clear();
    window.removeEventListener("wheel", this.arm, { capture: true });
    window.removeEventListener("pointermove", this.onPointerMove, { capture: true });
    window.removeEventListener("pointerdown", this.onDown, { capture: true });
    window.removeEventListener("pointerup", this.onUp, { capture: true });
    window.removeEventListener("pointercancel", this.onUp, { capture: true });
    window.removeEventListener("resize", this.arm);
    this.host.removeEventListener("sv2-camera", this.arm);
    if (this.hostObserver) this.hostObserver.disconnect();
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.canvas.removeEventListener("webglcontextlost", this.onLost);
    this.canvas.removeEventListener("webglcontextrestored", this.onRestored);
    this.gl.dispose();
    this.canvas.remove();
    glBudget.release(this.lease);
    renderers.delete(this.host);
    if (current === this) current = null;
  }
}

/** The shared renderer for a GL host element (made on first use). */
export function rendererFor(host) {
  let r = renderers.get(host);
  if (!r || r.destroyed) {
    r = new SceneRenderer(host);
    renderers.set(host, r);
  }
  current = r;
  setActive(() => (current && !current.destroyed ? current.qa() : null), () => (current && !current.destroyed ? current.probe() : Promise.resolve({})));
  return r;
}

/** The page's renderer, if one exists (QA, tests). */
export function currentRenderer() {
  return current && !current.destroyed ? current : null;
}

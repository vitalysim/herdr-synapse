// A small DevTools-protocol client for the v2 board's interaction tests (canvas-v2-phase1.md 5.3):
// a headless Chrome this runner starts itself (fresh profile under /private/tmp, a free port read
// back from DevToolsActivePort) and Node's built-in WebSocket. Only the Chrome started here is
// ever killed.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const CHROME_PATHS = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
];

export function findChrome() {
  if (process.env.CHROME && fs.existsSync(process.env.CHROME)) return process.env.CHROME;
  return CHROME_PATHS.find((p) => fs.existsSync(p)) || null;
}

function tmpRoot() {
  return fs.existsSync("/private/tmp") ? "/private/tmp" : os.tmpdir();
}

// Starts a headless Chrome; resolves to {browserWs, port, close()}.
export async function launchChrome(binary, { width = 1440, height = 900 } = {}) {
  const profile = fs.mkdtempSync(path.join(tmpRoot(), "synapse-e2e-chrome-"));
  const child = spawn(
    binary,
    [
      "--headless=new",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      `--window-size=${width},${height}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--disable-background-networking",
      "--disable-component-update",
      "--hide-scrollbars",
      "--mute-audio",
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr = (stderr + chunk).slice(-4000);
  });
  const portFile = path.join(profile, "DevToolsActivePort");
  let port = null;
  for (let i = 0; i < 150 && port === null; i += 1) {
    if (child.exitCode !== null) throw new Error(`Chrome exited early: ${stderr}`);
    if (fs.existsSync(portFile)) {
      const first = fs.readFileSync(portFile, "utf8").split("\n")[0].trim();
      if (/^\d+$/.test(first)) port = Number(first);
    }
    if (port === null) await sleep(100);
  }
  if (port === null) {
    child.kill("SIGKILL");
    throw new Error(`Chrome gave no debugging port: ${stderr}`);
  }
  const close = async () => {
    if (child.exitCode === null) {
      child.kill("SIGTERM");
      for (let i = 0; i < 30 && child.exitCode === null; i += 1) await sleep(100);
      if (child.exitCode === null) child.kill("SIGKILL");
    }
    fs.rmSync(profile, { recursive: true, force: true });
  };
  return { port, close };
}

// One page target: send(method, params), events by method, and the helpers the scenarios use.
// Other page targets (Chrome may open one of its own, such as chrome://settings/help) are closed:
// a page that is not in front is "hidden", its animation frames stop, and rAF-aligned input
// (every mouse move) then never gets dispatched.
export async function openPage(port) {
  const created = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
  for (const t of await (await fetch(`http://127.0.0.1:${port}/json`)).json()) {
    if (t.type === "page" && t.id !== created.id) await fetch(`http://127.0.0.1:${port}/json/close/${t.id}`).catch(() => undefined);
  }
  const ws = new WebSocket(created.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  let seq = 0;
  const pending = new Map();
  const handlers = new Map();
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.message} (${msg.error.code})`));
      else resolve(msg.result);
      return;
    }
    for (const fn of handlers.get(msg.method) || []) fn(msg.params);
  });
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      seq += 1;
      pending.set(seq, { resolve, reject });
      ws.send(JSON.stringify({ id: seq, method, params }));
    });
  const on = (method, fn) => {
    if (!handlers.has(method)) handlers.set(method, []);
    handlers.get(method).push(fn);
  };

  const front = () => send("Page.bringToFront").catch(() => undefined);
  const page = {
    send,
    front,
    on,
    targetId: created.id,
    async close() {
      try {
        await fetch(`http://127.0.0.1:${port}/json/close/${created.id}`);
      } catch {
        // the browser is going away anyway
      }
      ws.close();
    },
    // Evaluates an expression (awaiting promises) and returns its JSON value; throws on an exception.
    async eval(expression) {
      const res = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (res.exceptionDetails) {
        const detail = res.exceptionDetails.exception?.description || res.exceptionDetails.text;
        throw new Error(`page threw: ${detail}\n  in: ${expression.slice(0, 160)}`);
      }
      return res.result.value;
    },
    async waitFor(expression, { timeoutMs = 15000, what = expression } = {}) {
      const until = Date.now() + timeoutMs;
      let last;
      while (Date.now() < until) {
        try {
          last = await page.eval(expression);
          if (last) return last;
        } catch (err) {
          last = err.message;
        }
        await sleep(100);
      }
      throw new Error(`timed out waiting for ${what} (last: ${JSON.stringify(last)})`);
    },
    // -- input -------------------------------------------------------------------------------
    async mouse(type, [x, y], { button = "left", buttons, clickCount = 1, modifiers = 0 } = {}) {
      const held = buttons ?? (type === "mouseReleased" || type === "mouseMoved" ? 0 : 1);
      await send("Input.dispatchMouseEvent", { type, x, y, button: type === "mouseMoved" && !held ? "none" : button, buttons: held, clickCount, modifiers });
    },
    async move(pt, opts = {}) {
      await page.mouse("mouseMoved", pt, opts);
    },
    // A press, `steps` moves and a release, as a hand would drag.
    async drag(from, to, { steps = 8, modifiers = 0, pauseMs = 16 } = {}) {
      await front();
      await page.mouse("mouseMoved", from, { modifiers });
      await page.mouse("mousePressed", from, { modifiers });
      for (let i = 1; i <= steps; i += 1) {
        const t = i / steps;
        await page.mouse("mouseMoved", [from[0] + (to[0] - from[0]) * t, from[1] + (to[1] - from[1]) * t], { buttons: 1, modifiers });
        if (pauseMs) await sleep(pauseMs);
      }
      await page.mouse("mouseReleased", to, { modifiers });
    },
    async path(points, { modifiers = 0, pauseMs = 8 } = {}) {
      await front();
      await page.mouse("mouseMoved", points[0], { modifiers });
      await page.mouse("mousePressed", points[0], { modifiers });
      for (const pt of points.slice(1)) {
        await page.mouse("mouseMoved", pt, { buttons: 1, modifiers });
        if (pauseMs) await sleep(pauseMs);
      }
      await page.mouse("mouseReleased", points[points.length - 1], { modifiers });
    },
    async click(pt, { modifiers = 0 } = {}) {
      await front();
      await page.mouse("mouseMoved", pt, { modifiers });
      await page.mouse("mousePressed", pt, { modifiers, clickCount: 1 });
      await page.mouse("mouseReleased", pt, { modifiers, clickCount: 1 });
    },
    async dblclick(pt) {
      await front();
      await page.mouse("mouseMoved", pt);
      await page.mouse("mousePressed", pt, { clickCount: 1 });
      await page.mouse("mouseReleased", pt, { clickCount: 1 });
      await page.mouse("mousePressed", pt, { clickCount: 2 });
      await page.mouse("mouseReleased", pt, { clickCount: 2 });
    },
    async key(key, { modifiers = 0 } = {}) {
      const spec = KEYS[key] || { key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key, keyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0 };
      const text = key.length === 1 && !(modifiers & (MOD.meta | MOD.ctrl)) ? key : undefined;
      const base = { key: spec.key, code: spec.code, windowsVirtualKeyCode: spec.keyCode, modifiers };
      await send("Input.dispatchKeyEvent", { type: text ? "keyDown" : "rawKeyDown", ...base, text, unmodifiedText: text });
      await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
    },
    async type(text) {
      await send("Input.insertText", { text });
    },
    async screenshot(file) {
      const shot = await send("Page.captureScreenshot", { format: "png" });
      fs.writeFileSync(file, Buffer.from(shot.data, "base64"));
      return file;
    },
  };
  return page;
}

export const MOD = { alt: 1, ctrl: 2, meta: 4, shift: 8 };

const KEYS = {
  Enter: { key: "Enter", code: "Enter", keyCode: 13 },
  Escape: { key: "Escape", code: "Escape", keyCode: 27 },
  Delete: { key: "Delete", code: "Delete", keyCode: 46 },
  Backspace: { key: "Backspace", code: "Backspace", keyCode: 8 },
  ArrowLeft: { key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 },
  ArrowUp: { key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
  ArrowRight: { key: "ArrowRight", code: "ArrowRight", keyCode: 39 },
  ArrowDown: { key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
  " ": { key: " ", code: "Space", keyCode: 32 },
  "-": { key: "-", code: "Minus", keyCode: 189 },
  "=": { key: "=", code: "Equal", keyCode: 187 },
  0: { key: "0", code: "Digit0", keyCode: 48 },
};

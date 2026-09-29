// Starts tools/canvas_rig.py (a throwaway team and page server on loopback, never the real
// session) and reads its one JSON line {url, team, port}. stop() closes its stdin, which ends it.
//
// control(command) speaks the rig's stdin protocol (canvas-v2-phase5.md 11.2 I-9): one JSON line
// in ({"as": "drawer" | "peer" | "deputy" | "lead", "ops": [...], "base"?}, {"as", "focus": {...}},
// {"as", "look": {...}}, {"settings": {...}}), one JSON line out, in order. It lets a scenario act
// as the other agents on the board while the page is the operator.
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO = path.resolve(HERE, "..", "..", "..");
const RIG = path.join(REPO, "tools", "canvas_rig.py");

const DIST_SHIM = [
  "import sys",
  "from pathlib import Path",
  "sys.path.insert(0, 'tools')",
  "import canvas_rig",
  "from herdr_team import whiteboard_server as W",
  "dist = Path(sys.argv[1])",
  "W.web_dist = lambda: dist",
  "raise SystemExit(canvas_rig.main(sys.argv[2:]))",
].join("\n");

export function rigAvailable() {
  return fs.existsSync(RIG);
}

// `engine`: "v2" (the default here) or "v1" puts ?engine= in the page URL; null leaves it out, so the page
// opens its own default (canvas v2 since phase 6) or what this browser stored.
export async function startRig(scene, { writable = true, seconds = 180, out, engine = "v2", python = process.env.PYTHON || "python3" } = {}) {
  const args = [RIG, scene, "--seconds", String(seconds)];
  if (engine) args.push("--engine", engine);
  if (writable) args.push("--writable");
  if (out) args.push("--out", out);
  // SYNAPSE_E2E_DIST serves a page build other than web/dist (a build under test that is not
  // committed yet): the rig runs with whiteboard_server.web_dist pointed at it.
  const dist = process.env.SYNAPSE_E2E_DIST;
  const argv = dist
    ? ["-c", DIST_SHIM, dist, ...args.slice(1)]
    : args;
  const child = spawn(python, argv, { cwd: REPO, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  let served = false;
  // Answers to control() commands, in order: one waiter per command sent.
  const waiters = [];
  const answers = [];
  child.stderr.on("data", (chunk) => {
    stderr = (stderr + chunk).slice(-8000);
  });
  const info = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`canvas_rig.py ${scene}: no URL within 60 s\n${stderr}`)), 60000);
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      const lines = stdout.split("\n");
      stdout = lines.pop();
      for (const line of lines) {
        const text = line.trim();
        if (!text.startsWith("{")) continue;
        let doc;
        try {
          doc = JSON.parse(text);
        } catch {
          continue;
        }
        if (!served && doc && doc.url) {
          served = true;
          clearTimeout(timer);
          resolve(doc);
          continue;
        }
        if (!served) continue;
        const waiter = waiters.shift();
        if (waiter) waiter(doc);
        else answers.push(doc);
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`canvas_rig.py ${scene} exited (${code}) before serving\n${stderr}`));
    });
  });
  const url = new URL(info.url);
  if (engine && url.searchParams.get("engine") !== engine) url.searchParams.set("engine", engine);
  const stop = async () => {
    if (child.exitCode !== null) return;
    child.stdin.end();
    for (let i = 0; i < 50 && child.exitCode === null; i += 1) await new Promise((r) => setTimeout(r, 100));
    if (child.exitCode === null) child.kill("SIGTERM");
  };
  const control = (command, { timeoutMs = 20000 } = {}) =>
    new Promise((resolve, reject) => {
      if (child.exitCode !== null) {
        reject(new Error(`canvas_rig.py has exited\n${stderr}`));
        return;
      }
      const timer = setTimeout(() => reject(new Error(`canvas_rig.py: no answer to ${JSON.stringify(command).slice(0, 200)} within ${timeoutMs / 1000} s\n${stderr.slice(-2000)}`)), timeoutMs);
      const done = (doc) => {
        clearTimeout(timer);
        if (doc && doc.error) reject(new Error(`canvas_rig.py: ${JSON.stringify(doc.error)}`));
        else resolve(doc);
      };
      if (answers.length) done(answers.shift());
      else waiters.push(done);
      child.stdin.write(`${JSON.stringify(command)}\n`);
    });
  return { ...info, url: url.toString(), origin: url.origin, stop, control, stderr: () => stderr };
}

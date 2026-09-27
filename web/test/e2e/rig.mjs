// Starts tools/canvas_rig.py (a throwaway team and page server on loopback, never the real
// session) and reads its one JSON line {url, team, port}. stop() closes its stdin, which ends it.
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

export async function startRig(scene, { writable = true, seconds = 180, out, python = process.env.PYTHON || "python3" } = {}) {
  const args = [RIG, scene, "--engine", "v2", "--seconds", String(seconds)];
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
  child.stderr.on("data", (chunk) => {
    stderr = (stderr + chunk).slice(-8000);
  });
  const info = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`canvas_rig.py ${scene}: no URL within 60 s\n${stderr}`)), 60000);
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      for (const line of stdout.split("\n")) {
        const text = line.trim();
        if (!text.startsWith("{")) continue;
        try {
          const doc = JSON.parse(text);
          if (doc && doc.url) {
            clearTimeout(timer);
            resolve(doc);
            return;
          }
        } catch {
          // a partial line; wait for the rest
        }
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`canvas_rig.py ${scene} exited (${code}) before serving\n${stderr}`));
    });
  });
  const url = new URL(info.url);
  if (url.searchParams.get("engine") !== "v2") url.searchParams.set("engine", "v2");
  const stop = async () => {
    if (child.exitCode !== null) return;
    child.stdin.end();
    for (let i = 0; i < 50 && child.exitCode === null; i += 1) await new Promise((r) => setTimeout(r, 100));
    if (child.exitCode === null) child.kill("SIGTERM");
  };
  return { ...info, url: url.toString(), origin: url.origin, stop, stderr: () => stderr };
}

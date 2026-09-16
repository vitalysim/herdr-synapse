// herdr-synapse pi extension v1
// Synapse observability only. Herdr's own extension remains the state authority.
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";

const cli = __SYNAPSE_CLI__;

export default function (pi: any) {
  let ctx: any;
  let active = false;
  let instance = randomUUID();
  let compact: any = null;
  let timer: any;
  let child: any;
  let lastSent = 0;

  function publish() {
    if (!active || child || Date.now() - lastSent < 2000) return;
    const session = ctx?.sessionManager?.getSessionFile?.();
    const model = ctx?.model;
    if (!session || !model) return;
    const usage = ctx.getContextUsage?.();
    const data = {
      version: 1, mode: "tui", instance, session, at: Date.now() / 1000,
      provider: model.provider, model: model.id,
      effort: ctx.thinkingLevel ?? pi.getThinkingLevel?.() ?? null,
      used: usage?.tokens ?? null, window: usage?.contextWindow ?? model.contextWindow ?? null,
      compact,
    };
    lastSent = Date.now();
    // Payload never contains editor contents, prompts, API keys or transcript text.
    const proc = spawn(cli, ["pi-report"], { stdio: ["pipe", "ignore", "ignore"] });
    child = proc;
    const deadline = setTimeout(() => proc.kill(), 4000);
    deadline.unref();
    const finished = () => {
      clearTimeout(deadline);
      if (child === proc) child = undefined;
    };
    proc.on("error", finished);
    proc.on("close", finished);
    proc.stdin.on("error", () => {});
    proc.stdin.end(JSON.stringify(data));
  }

  pi.on("session_start", (_event: any, current: any) => {
    if (current.mode !== "tui" || process.env.HERDR_ENV !== "1" || !process.env.HERDR_PANE_ID) return;
    ctx = current;
    active = true;
    instance = randomUUID();
    compact = null;
    lastSent = 0;
    clearInterval(timer);
    timer = setInterval(publish, 5000);
    timer.unref();
    publish(); // A racing native session report is retried by the heartbeat.
  });
  for (const name of ["agent_start", "agent_settled", "model_select", "thinking_level_select", "session_tree"]) {
    pi.on(name, (_event: any, current: any) => { ctx = current; publish(); });
  }
  for (const name of ["session_compact", "session_compact_failed"]) {
    pi.on(name, (event: any, current: any) => {
      ctx = current;
      compact = { id: event.compactionEntry?.id ?? randomUUID(), at: Date.now() / 1000,
                  status: name === "session_compact" ? "success" : "failed" };
      publish(); // Kept on subsequent heartbeats even if an in-flight report coalesces this event.
    });
  }
  pi.on("session_shutdown", () => {
    active = false;
    clearInterval(timer);
    if (child) child.kill();
  });
}

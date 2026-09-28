// One Server-Sent Events stream per open page (GET /api/stream). The browser reconnects
// with Last-Event-ID by itself; after three failures in a row the page polls /changes
// every 5 s instead and retries the stream now and then.
import { getJSON, teamPath } from "./api.js";

const EVENTS = ["hello", "scene", "ops", "state", "views", "activity", "export_request", "presence", "bye"];
const POLL_MS = 5000;
const RETRY_STREAM_EVERY = 6;

export function openStream({ team, onEvent, onStatus }) {
  let source = null;
  let failures = 0;
  let lastId = null;
  let closed = false;
  let pollTimer = null;
  let polls = 0;

  const status = (value) => onStatus && onStatus(value);

  function url() {
    const query = new URLSearchParams();
    if (team) query.set("team", team);
    if (lastId !== null) query.set("since", String(lastId));
    return `/api/stream?${query}`;
  }

  function deliver(name, raw, id) {
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      return;
    }
    if (id !== null && id !== undefined && id !== "" && Number.isFinite(Number(id))) lastId = Number(id);
    if ((name === "ops" || name === "scene") && data && Number.isFinite(Number(data.version))) {
      lastId = Math.max(lastId === null ? 0 : lastId, Number(name === "scene" ? data.scene?.version ?? data.version : data.version));
    }
    onEvent(name, data);
  }

  function connect() {
    if (closed) return;
    source = new EventSource(url());
    for (const name of EVENTS) {
      source.addEventListener(name, (event) => {
        failures = 0;
        if (name === "bye") {
          closed = true;
          source.close();
          stopPolling();
        }
        deliver(name, event.data, event.lastEventId);
      });
    }
    source.onopen = () => {
      failures = 0;
      status("live");
    };
    source.onerror = () => {
      if (closed) return;
      failures += 1;
      status("reconnecting");
      if (failures >= 3) {
        source.close();
        source = null;
        startPolling();
      }
    };
  }

  async function pollOnce() {
    polls += 1;
    if (polls % RETRY_STREAM_EVERY === 0) {
      stopPolling();
      failures = 0;
      connect();
      return;
    }
    if (!team) return;
    try {
      const result = await getJSON(`${teamPath(team, "changes")}?since=${lastId === null ? 0 : lastId}`);
      status("polling");
      if (result.reset) {
        const scene = await getJSON(teamPath(team, "scene"));
        deliver("scene", JSON.stringify({ team, scene }), scene.version);
      } else if (result.events && result.events.length) {
        const last = result.complete ? result.version : result.events[result.events.length - 1].seq;
        deliver("ops", JSON.stringify({ team, version: last, events: result.events }), last);
      }
    } catch (err) {
      status("offline");
      if (err && (err.status === 401 || err.status === 403)) {
        closed = true;
        stopPolling();
        onEvent("bye", { reason: err.code === "whiteboard_off" ? "disabled" : "stopped" });
      }
    }
  }

  function startPolling() {
    if (pollTimer || closed) return;
    status("polling");
    pollTimer = setInterval(pollOnce, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  connect();
  return {
    close() {
      closed = true;
      stopPolling();
      if (source) source.close();
    },
  };
}

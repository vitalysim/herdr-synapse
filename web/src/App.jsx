// The whiteboard page: a team selector, four tabs (Canvas, Team, Activity, Diagrams) and one
// event stream per open page. Everything comes from the local server; nothing is fetched
// from anywhere else.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJSON, setCsrf } from "./api.js";
import { createBus } from "./bus.js";
import CanvasTab from "./canvas/CanvasTab.jsx";
import { createSceneStore } from "./sceneStore.js";
import { openStream } from "./stream.js";
import ActivityTab from "./views/ActivityTab.jsx";
import DiagramsTab from "./views/DiagramsTab.jsx";
import TeamTab from "./views/TeamTab.jsx";

const TABS = [
  ["canvas", "Canvas"],
  ["team", "Team"],
  ["activity", "Activity"],
  ["diagrams", "Diagrams"],
];

const BYE = {
  disabled: "The whiteboard was disabled by the operator. Nothing drawn is lost; it comes back when it is enabled again.",
  team_off: "The whiteboard is off for this team.",
  stopped: "The page server stopped. Open the whiteboard again from Herdr (prefix+a).",
  idle: "The page server stopped after being idle. Open the whiteboard again from Herdr (prefix+a).",
};

function readHash() {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  return { team: params.get("team"), tab: params.get("tab") };
}

function writeHash(team, tab) {
  const params = new URLSearchParams();
  if (team) params.set("team", team);
  if (tab) params.set("tab", tab);
  const next = `#${params}`;
  if (window.location.hash !== next) window.history.replaceState(null, "", next);
}

function Toasts({ toasts, dismiss }) {
  return (
    <div className="toasts" role="status">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast ${toast.tone}`}>
          <span>{toast.text}</span>
          <button type="button" onClick={() => dismiss(toast.id)} aria-label="Dismiss">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState(null);
  const [failure, setFailure] = useState(null);
  const [teams, setTeams] = useState([]);
  const [team, setTeam] = useState(() => readHash().team);
  const [tab, setTab] = useState(() => readHash().tab || "canvas");
  const [streamStatus, setStreamStatus] = useState("connecting");
  const [bye, setBye] = useState(null);
  const [toasts, setToasts] = useState([]);
  const toastId = useRef(0);
  const store = useMemo(() => createSceneStore(), []);
  const bus = useMemo(() => createBus(), []);

  const toast = useCallback((text, tone = "info") => {
    toastId.current += 1;
    const id = toastId.current;
    setToasts((list) => [...list.slice(-4), { id, text: String(text).slice(0, 400), tone }]);
    setTimeout(() => setToasts((list) => list.filter((item) => item.id !== id)), tone === "error" ? 9000 : 5000);
  }, []);

  const refreshTeams = useCallback(async () => {
    try {
      const data = await getJSON("/api/teams");
      setTeams(data.teams || []);
    } catch {
      // the stream reports what matters
    }
  }, []);

  useEffect(() => {
    getJSON("/api/session")
      .then((data) => {
        setCsrf(data.csrf);
        setSession(data);
        setTeams(data.teams || []);
      })
      .catch((err) => setFailure(err));
  }, []);

  useEffect(() => {
    if (!teams.length) return;
    if (!team || !teams.some((row) => row.name === team)) setTeam(teams[0].name);
  }, [teams, team]);

  useEffect(() => writeHash(team, tab), [team, tab]);

  useEffect(() => {
    if (!session) return undefined;
    store.reset();
    setBye(null);
    const current = team && teams.some((row) => row.name === team) ? team : null;
    const stream = openStream({
      team: current,
      onStatus: setStreamStatus,
      onEvent: (name, data) => {
        if (name === "scene") store.replace(data.scene);
        else if (name === "ops") store.applyEvents(data.events);
        else if (name === "state") refreshTeams();
        else if (name === "bye") setBye(data.reason || "stopped");
        else bus.emit(name, data);
      },
    });
    return () => stream.close();
  }, [session, team, teams.length > 0]); // eslint-disable-line react-hooks/exhaustive-deps

  if (failure) {
    return (
      <div className="gate">
        <h1>Synapse whiteboard</h1>
        {failure.status === 401 ? (
          <p>This page needs a fresh link: press prefix+a in Herdr, or run <code>herdr-synapse whiteboard open</code>.</p>
        ) : (
          <p>
            The page server answered {failure.code}: {failure.message}
          </p>
        )}
      </div>
    );
  }
  if (!session) return <div className="gate">Connecting…</div>;

  const teamRow = teams.find((row) => row.name === team) || null;
  const layerOn = Boolean(session.switches && session.switches.session && session.switches.session.enabled);

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Synapse whiteboard</span>
        <select className="team-select" value={teamRow ? teamRow.name : ""} onChange={(event) => setTeam(event.target.value)} aria-label="Team">
          {!teams.length ? <option value="">no team is on</option> : null}
          {teams.map((row) => (
            <option key={row.name} value={row.name}>
              {row.name}
            </option>
          ))}
        </select>
        <nav className="tabs">
          {TABS.map(([id, title]) => (
            <button key={id} type="button" className={tab === id ? "tab active" : "tab"} onClick={() => setTab(id)}>
              {title}
            </button>
          ))}
        </nav>
        <span className="spacer" />
        {teamRow && !teamRow.viz ? <span className="pill">live visuals off</span> : null}
        {!session.writable ? <span className="pill warn">read-only</span> : null}
        <span className={`status ${streamStatus}`} title={`stream: ${streamStatus}`}>
          {streamStatus}
        </span>
        <span className="muted small">v{session.server_version}</span>
      </header>
      {!session.writable ? (
        <div className="banner">
          Read-only: this page was not opened by the operator in person, so drawing and sending are off. Open it from Herdr with prefix+a to edit.
        </div>
      ) : null}
      {!layerOn ? <div className="banner warn">{BYE.disabled}</div> : null}
      {bye ? <div className="banner warn">{BYE[bye] || BYE.stopped}</div> : null}
      <main className="content">
        <div className={tab === "canvas" ? "pane" : "pane hidden"}>
          {teamRow ? (
            <CanvasTab
              key={teamRow.name}
              team={teamRow.name}
              teamRow={teamRow}
              writable={Boolean(session.writable) && !bye}
              store={store}
              bus={bus}
              visible={tab === "canvas"}
              toast={toast}
            />
          ) : (
            <div className="empty">No team has the whiteboard on. The operator turns a team on with: herdr-synapse --team T whiteboard team on</div>
          )}
        </div>
        {tab === "team" ? (
          <div className="pane scroll">
            <TeamTab team={teamRow ? teamRow.name : null} bus={bus} visible />
          </div>
        ) : null}
        {tab === "activity" ? (
          <div className="pane scroll">
            <ActivityTab team={teamRow ? teamRow.name : null} bus={bus} visible />
          </div>
        ) : null}
        {tab === "diagrams" ? (
          <div className="pane scroll">
            <DiagramsTab team={teamRow ? teamRow.name : null} teamRow={teamRow} store={store} />
          </div>
        ) : null}
      </main>
      <Toasts toasts={toasts} dismiss={(id) => setToasts((list) => list.filter((item) => item.id !== id))} />
    </div>
  );
}

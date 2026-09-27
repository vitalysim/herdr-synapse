// The whiteboard page: a team selector, four tabs (Canvas, Team, Activity, Diagrams) and one
// event stream per open page. Everything comes from the local server; nothing is fetched
// from anywhere else.
//
// The Canvas tab runs one of two engines (web/src/v2/interact/engine.js): v1, the Excalidraw
// canvas (the default), or v2, the display-list board, with ?engine=v2 or the top bar toggle.
// Both load lazily, so a v2 page never downloads Excalidraw and a v1 page never the board.
import React, { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJSON, setCsrf } from "./api.js";
import { createBus } from "./bus.js";
import { createSceneStore } from "./sceneStore.js";
import { openStream } from "./stream.js";
import { applyTheme } from "./theme/tokens.js";
import ActivityTab from "./views/ActivityTab.jsx";
import DiagramsTab from "./views/DiagramsTab.jsx";
import TeamTab from "./views/TeamTab.jsx";
import { engineFromQuery, engineOf, setEngine, withEngine } from "./v2/interact/engine.js";

const CanvasTab = lazy(() => import("./canvas/CanvasTab.jsx"));
const Board = lazy(() => import("./v2/Board.jsx"));

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

// Light or dark: the viewer's own choice when they made one (kept in this browser), else the
// system's. Excalidraw's menu toggle and the top bar button both set the choice.
const THEME_KEY = "synapse-theme";
const darkQuery = () => (typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null);

function storedTheme() {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null;
  }
}

function useTheme() {
  const [chosen, setChosen] = useState(storedTheme);
  const [system, setSystem] = useState(() => (darkQuery()?.matches ? "dark" : "light"));
  useEffect(() => {
    const query = darkQuery();
    if (!query) return undefined;
    const follow = () => setSystem(query.matches ? "dark" : "light");
    query.addEventListener("change", follow);
    return () => query.removeEventListener("change", follow);
  }, []);
  const theme = chosen || system;
  useEffect(() => applyTheme(theme), [theme]);
  const choose = useCallback((next) => {
    setChosen((prev) => {
      if (prev === next || (!prev && next === system)) return prev;
      try {
        window.localStorage.setItem(THEME_KEY, next);
      } catch {
        // kept for this page only
      }
      return next;
    });
  }, [system]);
  return [theme, choose];
}

function browserStorage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

// The canvas engine: the URL's, else the stored choice, else v1. A v2 page whose server or
// display list it cannot draw falls back to v1 for this session only (Board's onFallback).
function useEngine() {
  const [engine, setEngineState] = useState(() => engineOf(window.location, browserStorage()));
  const [fallback, setFallback] = useState(null);
  const choose = useCallback((next) => {
    setEngine(next, browserStorage());
    if (engineFromQuery(window.location)) window.history.replaceState(null, "", withEngine(window.location.href, next));
    setFallback(null);
    setEngineState(next);
  }, []);
  return [fallback ? "v1" : engine, choose, setFallback, fallback];
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
  const [theme, setTheme] = useTheme();
  const [engine, chooseEngine, fallBack, fallbackReason] = useEngine();
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
        <button
          type="button"
          className={engine === "v2" ? "chip on engine-toggle" : "chip engine-toggle"}
          title={engine === "v2" ? "Back to the classic canvas" : "Try the new canvas (v2 preview)"}
          aria-pressed={engine === "v2"}
          onClick={() => chooseEngine(engine === "v2" ? "v1" : "v2")}
        >
          v2 preview
        </button>
        <span className="muted small">v{session.server_version}</span>
        <button
          type="button"
          className="theme-toggle"
          title={theme === "dark" ? "Light theme" : "Dark theme"}
          aria-label={theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>
      </header>
      {!session.writable ? (
        <div className="banner">
          Read-only: this page was not opened by the operator in person, so drawing and sending are off. Open it from Herdr with prefix+a to edit.
        </div>
      ) : null}
      {!layerOn ? <div className="banner warn">{BYE.disabled}</div> : null}
      {bye ? <div className="banner warn">{BYE[bye] || BYE.stopped}</div> : null}
      {fallbackReason ? <div className="banner warn">{fallbackReason}</div> : null}
      <main className="content">
        <div className={tab === "canvas" ? "pane" : "pane hidden"}>
          {teamRow ? (
            <Suspense fallback={<div className="empty">Loading the canvas…</div>}>
              {engine === "v2" ? (
                <Board
                  key={`v2:${teamRow.name}`}
                  team={teamRow.name}
                  teamRow={teamRow}
                  writable={Boolean(session.writable) && !bye}
                  store={store}
                  bus={bus}
                  visible={tab === "canvas"}
                  toast={toast}
                  theme={theme}
                  onTheme={setTheme}
                  onFallback={fallBack}
                />
              ) : (
                <CanvasTab
                  key={teamRow.name}
                  team={teamRow.name}
                  teamRow={teamRow}
                  writable={Boolean(session.writable) && !bye}
                  store={store}
                  bus={bus}
                  visible={tab === "canvas"}
                  toast={toast}
                  theme={theme}
                  onTheme={setTheme}
                />
              )}
            </Suspense>
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

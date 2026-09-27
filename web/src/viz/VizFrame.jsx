// One live visual: the agent's HTML in <iframe sandbox="allow-scripts"> loaded from
// /viz/<team>/<id> (never allow-same-origin, never srcdoc, which would inherit the page's
// CSP). Data reaches it only by postMessage. A frame that stops beating for 2 s while on
// screen is replaced by its placeholder with "Run again".
import React, { useCallback, useEffect, useRef, useState } from "react";
import { loadArtifactData } from "../canvas/renderers.js";
import { registerFrame } from "./manager.js";

const WATCHDOG_MS = 2000;
const STARTUP_GRACE_MS = 6000;

export default function VizFrame({ team, element, onStill }) {
  const frameRef = useRef(null);
  const wrapRef = useRef(null);
  const lastBeat = useRef(Date.now());
  const startedAt = useRef(Date.now());
  const handle = useRef(null);
  const captured = useRef(false);
  const [state, setState] = useState("running");
  const [run, setRun] = useState(0);
  const [error, setError] = useState(null);
  const elementRef = useRef(element);
  elementRef.current = element;
  const onStillRef = useRef(onStill);
  onStillRef.current = onStill;
  const seq = element.updated_seq || 0;
  const elementId = element.id;
  const src = `/viz/${encodeURIComponent(team)}/${encodeURIComponent(element.id)}?v=${seq}`;

  const post = useCallback((message) => {
    const target = frameRef.current && frameRef.current.contentWindow;
    if (target) target.postMessage(message, "*");
  }, []);

  useEffect(() => {
    if (state !== "running") return undefined;
    startedAt.current = Date.now();
    lastBeat.current = Date.now();
    captured.current = false;
    handle.current = registerFrame(`${team}/${elementId}/${run}`, {
      running: () => true,
      setPaused: (paused) => post({ type: paused ? "synapse:pause" : "synapse:resume" }),
    });
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) handle.current && handle.current.setVisible(entry.isIntersecting);
    });
    if (wrapRef.current) observer.observe(wrapRef.current);
    const onMessage = async (event) => {
      if (!frameRef.current || event.source !== frameRef.current.contentWindow) return;
      const message = event.data;
      if (!message || typeof message.type !== "string") return;
      if (message.type === "synapse:beat") {
        lastBeat.current = Date.now();
      } else if (message.type === "synapse:ready") {
        lastBeat.current = Date.now();
        const current = elementRef.current;
        let data = current.data ?? null;
        if (data === null && current.data_path) {
          try {
            data = await loadArtifactData(team, current.data_path);
          } catch (err) {
            setError(`data: ${err.message || err}`);
          }
        }
        post({ type: "synapse:data", data });
        handle.current && handle.current.refresh();
        setTimeout(() => post({ type: "synapse:capture" }), 1500);
      } else if (message.type === "synapse:still") {
        if (!captured.current && typeof message.png === "string" && message.png.startsWith("data:image/")) {
          captured.current = true;
          if (onStillRef.current) onStillRef.current(elementRef.current, message.png);
        }
      } else if (message.type === "synapse:error") {
        setError(String(message.message || "error").slice(0, 200));
      }
    };
    window.addEventListener("message", onMessage);
    const watchdog = setInterval(() => {
      const box = wrapRef.current && wrapRef.current.getBoundingClientRect();
      const onScreen = box && box.width > 0 && box.bottom > 0 && box.right > 0 && box.top < window.innerHeight && box.left < window.innerWidth;
      const grace = Date.now() - startedAt.current < STARTUP_GRACE_MS;
      if (onScreen && !grace && Date.now() - lastBeat.current > WATCHDOG_MS) setState("stopped");
    }, 500);
    return () => {
      window.removeEventListener("message", onMessage);
      clearInterval(watchdog);
      observer.disconnect();
      if (handle.current) handle.current.unregister();
      handle.current = null;
    };
  }, [state, run, team, elementId, seq, post]);

  if (state === "stopped") {
    return (
      <div className="viz-stopped" ref={wrapRef}>
        <div className="viz-title">{element.text || "live visual"}</div>
        <div className="viz-note">stopped: not responding</div>
        <button type="button" onClick={() => { setError(null); setState("running"); setRun((n) => n + 1); }}>
          Run again
        </button>
      </div>
    );
  }
  return (
    <div className="viz-wrap" ref={wrapRef} onPointerEnter={() => handle.current && handle.current.focus()}>
      <iframe
        key={`${src}#${run}`}
        ref={frameRef}
        className="viz-frame"
        title={element.text || "live visual"}
        src={src}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        loading="lazy"
      />
      {error ? <div className="viz-error" title={error}>{error}</div> : null}
    </div>
  );
}

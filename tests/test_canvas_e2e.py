"""The whole visual layer end to end (0.21): the switch, a member drawing, looking, a mention, the page server, a human edit, off.

One scenario through the real doors, in-process and hermetic: the CLI as
the operator and as members (``cli.main`` with a ``FakeApi``), the canvas
core, the MCP server over in-memory streams, and the page server started
by ``whiteboard open`` itself, whose ``SPAWN`` hook runs the real
``whiteboard-serve`` command on a thread instead of a detached process. It
listens on a random ``127.0.0.1`` port and is driven with raw HTTP requests.
``resvg`` is faked through ``canvas_render.RUN``; no browser, harness,
network or Herdr server is involved. The pieces are pinned in depth by
``test_canvas*.py``, ``test_whiteboard_server.py``, ``test_whiteboard_cmd.py``
and ``test_watch.py``; this file checks that they fit together.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from support import FAKE_AGENTS, TempState
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon
from test_whiteboard_server import SSE, raw_request

from herdr_team import canvas, canvas_render, cli, cmd_whiteboard, features, models, store
from herdr_team import whiteboard_server as W

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
WORKER_PANE = "w2:p2"
REVIEWER_PANE = "w2:p1"

#: The member's batch: every drawing layer the task names, plus a claim and a legend.
BATCH = {"ops": [
    {"op": "claim", "region": "c10r4:c40r22", "label": "mapping churn drivers", "intent": "tell others where I work"},
    {"op": "frame", "id": "drivers", "title": "Churn drivers", "at": "c10r4", "w": 600, "h": 360, "intent": "group the drivers"},
    {"op": "shape", "id": "price", "kind": "note", "text": "Price rise in March", "inside": "drivers", "intent": "the biggest driver"},
    {"op": "shape", "id": "onboard", "kind": "note", "text": "Slow onboarding", "right_of": "price", "gap": 60, "intent": "second driver"},
    {"op": "arrow", "id": "worsens", "from": "price", "to": "onboard", "label": "worsens", "intent": "price makes onboarding churn worse"},
    {"op": "pen", "id": "circle", "points": ["c11r5", "c19r4", "c22r9", "c15r12", "c10r9", "c11r5"], "color": "red", "closed": True,
     "intent": "circle the main driver"},
    {"op": "svg", "id": "funnel", "title": "Funnel", "svg": "<svg viewBox='0 0 100 60'><path d='M0 0H100L70 60H30Z' fill='#a5d8ff'/></svg>",
     "right_of": "drivers", "sketchy": True, "intent": "show the funnel shape"},
    {"op": "viz", "id": "orbit", "title": "Churn over time", "libs": ["d3"], "data": [{"month": 1, "rate": 3}, {"month": 2, "rate": 5}],
     "html": "<svg id=s width=460 height=320></svg><script>synapse.onData(d => d3.select('#s').selectAll('circle').data(d)"
             ".join('circle').attr('cx', (r, i) => 20 + i * 30).attr('cy', r => 300 - r.rate * 5).attr('r', 6))</script>",
     "below": "drivers", "intent": "animate the churn trend"},
    {"op": "legend", "symbol": "red circle", "meaning": "the main driver", "intent": "record the convention"},
    {"op": "comment", "at": "price", "text": "@alpha-reviewer 38% of churn in the cohort; can you check it?", "mentions": ["human"],
     "intent": "ask for a check"},
]}


def fake_png(width: int = 10, height: int = 10) -> bytes:
    """PNG bytes as far as the canvas reads them (magic and the IHDR size)."""
    return PNG_MAGIC + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 16


class CanvasEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api(list(FAKE_AGENTS))
        # resvg: found, and it "renders" by writing a PNG; the SVG it was given is kept for the checks.
        self.rendered: List[str] = []
        self.patch(canvas_render, "find_resvg", lambda env=None: "/fake/bin/resvg")
        self.patch(canvas_render, "RUN", self._fake_resvg)
        # The page server's process is a thread running the real ``whiteboard-serve`` command.
        self.server_threads: List[threading.Thread] = []
        self.server_log: List[str] = []
        self.patch(W, "SPAWN", self._spawn_in_process)
        self.patch(W, "LOG", self.server_log.append)
        self.patch(W, "POLL_S", 0.05)
        self.browser: List[str] = []
        self.patch(cmd_whiteboard, "OPEN_BROWSER", lambda url: self.browser.append(url) or True)
        self.addCleanup(self._stop_leftover_server)

    # -- rig ---------------------------------------------------------------------------

    def patch(self, target: Any, name: str, value: Any) -> None:
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_resvg(self, argv: List[str], timeout: float):
        self.assertEqual(argv[0], "/fake/bin/resvg")
        self.rendered.append(Path(argv[-2]).read_text(encoding="utf-8"))
        Path(argv[-1]).write_bytes(fake_png())
        return 0, ""

    def _spawn_in_process(self, argv: List[str], env: Dict[str, str], log_path: Path) -> int:
        """``whiteboard open`` spawns ``<plugin>/bin/herdr-synapse --socket S whiteboard-serve``; run exactly that through the CLI."""
        self.assertTrue(argv[0].endswith(os.path.join("bin", "herdr-synapse")), argv)
        self.assertEqual(argv[1:], ["--socket", os.fspath(self.ts.layout.socket), "whiteboard-serve"])
        self.assertEqual(env.get(W.ENV_WRITABLE), "1", "started by the operator in person: a writable page")
        self.assertEqual(json.loads(env[W.ENV_STARTED_BY])["name"], "human")
        self.serve_out = (io.StringIO(), io.StringIO())
        thread = threading.Thread(target=cli.main, args=(argv[1:],),
                                  kwargs={"env": dict(env), "stdout": self.serve_out[0], "stderr": self.serve_out[1], "api": self.api},
                                  name="whiteboard-serve", daemon=True)
        thread.start()
        self.server_threads.append(thread)
        return os.getpid()

    def _stop_leftover_server(self) -> None:
        W.stop(self.ts.layout, "stopped")
        for thread in self.server_threads:
            thread.join(timeout=10)

    def cli(self, argv: List[str], pane: Optional[str] = None):
        overrides = {"HERDR_PANE_ID": pane} if pane else {}
        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **overrides), self.api))

    def ok(self, argv: List[str], pane: Optional[str] = None) -> Dict[str, Any]:
        code, payload, err = self.cli(argv, pane)
        self.assertEqual(code, 0, (argv, err))
        return payload

    def refused(self, argv: List[str], pane: Optional[str] = None) -> Dict[str, Any]:
        code, _payload, err = self.cli(argv, pane)
        self.assertNotEqual(code, 0, argv)
        return err

    def board(self, event: Optional[str] = None) -> List[Dict[str, Any]]:
        records = store.BoardStore(self.ts.team).read()
        return [r for r in records if event is None or r.get("event") == event]

    def element(self, eid: str) -> Dict[str, Any]:
        return next(e for e in canvas.load_scene(self.ts.team)["elements"] if e["id"] == eid)

    # -- the page, over raw HTTP ---------------------------------------------------------

    def get(self, path: str, cookie: Optional[str] = None, host: Optional[str] = None):
        return raw_request(self.port, "GET", path, {"Cookie": cookie} if cookie else {}, host=host)

    def post(self, path: str, body: Any, cookie: str, csrf: Optional[str], origin: Optional[str] = "local"):
        headers = {"Cookie": cookie, "Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = "http://127.0.0.1:{}".format(self.port) if origin == "local" else origin
        if csrf is not None:
            headers[W.CSRF_HEADER] = csrf
        return raw_request(self.port, "POST", path, headers, json.dumps(body).encode("utf-8"))

    # -- the scenario ------------------------------------------------------------------

    def test_switch_draw_look_mention_page_edit_and_off(self):
        layout, team = self.ts.layout, self.ts.team

        # 1. Off by default: the canvas refuses and nothing is written; members get no MCP flags.
        err = self.refused(["canvas", "look"], pane=WORKER_PANE)
        self.assertEqual((err["code"], err["scope"]), ("whiteboard_off", "session"))
        self.assertIsNone(features.mcp_spec(layout, team))
        self.assertIn("whiteboard: off", self.ok(["me"], pane=WORKER_PANE)["whiteboard"]["line"])

        # 2. The operator turns the layer on; a member cannot.
        self.assertEqual(self.refused(["whiteboard", "enable"], pane=WORKER_PANE)["code"], "author_mismatch")
        enabled = self.ok(["whiteboard", "enable"])
        self.assertTrue(enabled["changed"])
        # A canvas is per team: enabling turns none on and tells nobody, until the operator picks this team.
        self.assertEqual((enabled["notices"], self.board("whiteboard_state")), ({}, []))
        self.assertEqual(self.refused(["canvas", "look"], pane=WORKER_PANE)["scope"], "team")
        self.assertTrue(self.ok(["whiteboard", "team", "on"])["switch"]["on"])
        [state] = self.board("whiteboard_state")
        self.assertIn("the whiteboard is on for team alpha", state["text"])
        me = self.ok(["me"], pane=WORKER_PANE)["whiteboard"]
        self.assertEqual((me["on"], me["viz"]), (True, True), "the canvas and live visuals are on by default once the layer is")
        self.assertIn("herdr-synapse skill get --reference canvas", me["line"])
        self.assertIn("## One example per layer", self.ok(["skill", "get", "--reference", "canvas"], pane=WORKER_PANE)["text"])
        spec = features.mcp_spec(layout, team)
        self.assertEqual(spec["args"][-2:], ["canvas", "mcp"])
        self.assertEqual(models.launch_args("claude", None, None, mcp=spec)[-2:], ["--mcp-config", spec["config_file"]])

        # The notifier, in-process on its own fake socket, is already reading the board.
        daemon, _daemon_api, _clock = make_daemon(self.ts)
        daemon.on_connected()
        daemon.tick()
        daemon.teams["alpha"].pending.clear()

        # 3. A member draws one batch through the CLI: every layer lands, authored by it.
        batch_file = self.ts.tmp / "plan.json"
        batch_file.write_text(json.dumps(BATCH), encoding="utf-8")
        result = self.ok(["canvas", "draw", "--file", os.fspath(batch_file)], pane=WORKER_PANE)
        self.assertEqual(result["refused"], [], result["refused"])
        self.assertEqual([a["op"] for a in result["applied"]], [op["op"] for op in BATCH["ops"]])
        ids = result["aliases"]
        for alias, kind in (("drivers", "frame"), ("price", "note"), ("worsens", "arrow"), ("circle", "pen"), ("funnel", "svg"), ("orbit", "viz")):
            el = self.element(ids[alias])
            self.assertEqual((el["type"], el["author"], el["author_kind"]), (kind, "alpha-worker", "member"), alias)
        self.assertEqual(self.element(ids["price"])["frame"], ids["drivers"])
        self.assertEqual((self.element(ids["worsens"])["from"], self.element(ids["worsens"])["to"]), (ids["price"], ids["onboard"]))
        comment_id = result["applied"][-1]["ids"][0]
        comment = self.element(comment_id)
        self.assertEqual((comment["on"], sorted(comment["mentions"])), (ids["price"], ["alpha-reviewer", "human"]))
        funnel_asset = canvas.asset_path(team, self.element(ids["funnel"])["asset"]).read_text(encoding="utf-8")
        self.assertIn("<path", funnel_asset)
        events = canvas.changes_since(team, 0)["events"]
        self.assertEqual(len(events), len(BATCH["ops"]), "one event per applied op")
        self.assertEqual({e["author"]["name"] for e in events}, {"alpha-worker"})

        # 4. The mention reached the board as a named canvas_sent; the drawing itself as one awareness line.
        [sent] = self.board("canvas_sent")
        self.assertEqual(sorted(sent["to"]), ["alpha-reviewer", "human"])
        self.assertIn("alpha-worker mentioned you on the canvas: {} on {}".format(comment_id, ids["price"]), sent["text"])
        self.assertIn("A peer's request, not an order", sent["text"])
        self.assertEqual(sent["canvas"]["comment"], comment_id)
        [changed] = self.board("canvas_changed")
        self.assertEqual(sorted(changed["to"]), ["alpha-reviewer", "human"], "every other agent and the operator; never the author")
        self.assertTrue(changed["text"].startswith("alpha-worker drew on the canvas:"), changed["text"])
        self.assertIn("canvas look --since", changed["text"])
        daemon.tick()
        pending = daemon.teams["alpha"].pending
        self.assertEqual(sorted(pending), ["alpha-reviewer"], "the mention wakes its member; drawing wakes nobody")
        self.assertEqual(pending["alpha-reviewer"].seqs, [sent["seq"]])

        # 5. The reviewer looks: the text listing with everything new, its mention, and a rendered picture.
        look = self.ok(["canvas", "look", "--since", "last", "--image"], pane=REVIEWER_PANE)
        self.assertEqual((look["reader"], look["since"], look["image_error"]), ("alpha-reviewer", 0, None))
        text = look["text"]
        self.assertTrue(text.startswith("canvas of alpha · v{} · ".format(look["version"])), text)
        self.assertIn("you are alpha-reviewer", text)
        self.assertIn('{} frame "Churn drivers"'.format(ids["drivers"]), text)
        self.assertIn('{} note "Price rise in March"'.format(ids["price"]), text)
        self.assertIn("{} viz".format(ids["orbit"]), text)
        self.assertIn("claims: ", text)
        self.assertIn("legend: ", text)
        self.assertIn("changes since v0 ({}):".format(len(BATCH["ops"])), text)
        self.assertEqual([c["id"] for c in look["comments_for_you"]], [comment_id])
        self.assertTrue(Path(look["image"]).read_bytes().startswith(PNG_MAGIC))
        self.assertTrue(look["image"].startswith(os.fspath(features.whiteboard_dir(team) / "renders")))
        svg = self.rendered[-1]
        for alias in ("drivers", "price", "funnel", "orbit"):
            self.assertIn(">{}<".format(ids[alias]), svg, "the render marks {} with its id".format(alias))
        again = self.ok(["canvas", "look", "--since", "last"], pane=REVIEWER_PANE)
        self.assertEqual(again["changes"], [], "the cursor moved: nothing new since the last look")

        # 6. The same canvas over MCP, as the member Synapse would have started (the stdio server, in-memory streams).
        requests = "\n".join(json.dumps(m) for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "canvas_look", "arguments": {"around": ids["price"]}}},
        )) + "\n"
        with mock.patch.object(sys, "stdin", io.StringIO(requests)):
            code, out, err = run_cli(spec["args"], env_no_daemon(self.ts, HERDR_PANE_ID=WORKER_PANE), self.api)
        self.assertEqual(code, 0, err)
        replies = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertFalse(replies[1]["result"]["isError"], replies[1])
        self.assertEqual(replies[1]["result"]["structuredContent"]["reader"], "alpha-worker")

        # 7. The operator opens the page: ``whiteboard open`` starts the real server on loopback and mints a ticket.
        opened = self.ok(["whiteboard", "open", "--no-browser"])
        self.assertEqual((opened["started"], opened["writable"], opened["browser"]), (True, True, "skipped"))
        self.assertEqual(self.browser, [])
        self.port = int(opened["port"])
        prefix = "http://127.0.0.1:{}".format(self.port)
        self.assertTrue(opened["url"].startswith(prefix + "/?ticket="), opened["url"])
        status = self.ok(["whiteboard", "status"])["server"]
        self.assertEqual((status["port"], status["alive"], status["writable"]), (self.port, True, True))

        # 8. Who may use it: no cookie, a forged Host, the ticket once, then the cookie.
        self.assertEqual(self.get("/api/session").json()["code"], "not_signed_in")
        self.assertEqual(self.get("/api/session").status, 401)
        ticket_path = opened["url"][len(prefix):]
        rebinding = raw_request(self.port, "GET", ticket_path, host="attacker.example:{}".format(self.port))
        self.assertEqual((rebinding.status, rebinding.json()["code"]), (403, "bad_host"))
        signed_in = self.get(ticket_path)
        self.assertEqual((signed_in.status, signed_in.header("Location")), (303, "/"), "the ticket leaves the address bar at once")
        cookie_header = signed_in.cookies[0]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie_header)
        cookie = cookie_header.split(";", 1)[0]
        self.assertTrue(cookie.startswith(W.COOKIE_NAME + "="))
        self.assertEqual(self.get(ticket_path).status, 401, "a ticket opens the page once")
        session = self.get("/api/session", cookie).json()
        self.assertTrue(session["writable"])
        csrf = session["csrf"]
        forged = self.get("/api/session", cookie, host="localhost.attacker.example:{}".format(self.port))
        self.assertEqual((forged.status, forged.json()["code"]), (403, "bad_host"), "a valid cookie does not excuse a foreign Host")
        page = self.get("/", cookie)
        self.assertEqual((page.status, page.header("Content-Security-Policy")), (200, W.PAGE_CSP))
        self.assertIn(b"<script type=\"module\"", page.body, "the built page from web/dist")

        # 9. The scene, the views and the sealed viz frame, from the member's drawing.
        scene = self.get("/api/teams/alpha/scene", cookie).json()
        self.assertEqual(scene["version"], look["version"])
        by_id = {el["id"]: el for el in scene["elements"]}
        self.assertEqual(by_id[ids["drivers"]]["text"], "Churn drivers")
        self.assertEqual(by_id[comment_id]["type"], "comment")
        views = self.get("/api/teams/alpha/views", cookie).json()
        self.assertEqual(views["team"], "alpha")
        self.assertEqual(views["canvas"]["version"], scene["version"])
        self.assertEqual(views["canvas"]["comments_open"], 1)
        self.assertEqual({m["name"] for m in views["topology"]["members"]} >= {"alpha-worker", "alpha-reviewer"}, True)
        self.assertIn("canvas_sent", [row.get("event") for row in views["timeline"]])
        frame = self.get("/viz/alpha/" + ids["orbit"], cookie)
        self.assertEqual(frame.status, 200)
        self.assertTrue(frame.header("Content-Security-Policy").startswith("sandbox allow-scripts; default-src 'none'"))
        self.assertIn(b"d3.min.js", frame.body)

        # 10. The page's own stream says who it is and what is there.
        stream = SSE(self.port, "/api/stream?team=alpha", {"Cookie": cookie})
        self.addCleanup(stream.close)
        _eid, hello = stream.until("hello")
        self.assertEqual((hello["team"], hello["writable"], hello["switch"]["on"]), ("alpha", True, True))
        _eid, streamed = stream.until("scene")
        self.assertEqual(streamed["scene"]["version"], scene["version"])

        # 11. A human edit: writes need the page's Origin and CSRF token, then land as the operator's.
        edit = {"ops": [
            {"op": "shape", "kind": "box", "text": "Pricing page", "at": [900, 40], "w": 160, "h": 80, "color": "#1e1e1e", "id": "hBox1",
             "client_id": "Box1"},
            {"op": "move", "id": ids["onboard"], "by": [0, 40], "if_version": by_id[ids["onboard"]]["updated_seq"]},
        ], "atomic": False}
        self.assertEqual(self.post("/api/teams/alpha/ops", edit, cookie, None).json()["code"], "bad_csrf")
        self.assertEqual(self.post("/api/teams/alpha/ops", edit, cookie, csrf, origin="http://127.0.0.1:1").json()["code"], "bad_origin")
        self.assertEqual(self.post("/api/teams/alpha/ops", edit, cookie, csrf, origin=None).json()["code"], "bad_origin")
        self.assertEqual(canvas.current_version(team), scene["version"], "a refused request applies nothing")
        applied = self.post("/api/teams/alpha/ops", edit, cookie, csrf)
        self.assertEqual(applied.status, 200, applied.body[:300])
        applied = applied.json()
        self.assertEqual(applied["refused"], [])
        box_id = applied["aliases"]["Box1"]
        box = self.element(box_id)
        self.assertEqual((box["author"], box["author_kind"], box["client_id"], box["intent"]), ("human", "human", "Box1", "the operator's edit"))
        self.assertEqual(self.element(ids["onboard"])["y"], by_id[ids["onboard"]]["y"] + 40, "the operator may move an agent's element")
        _eid, ops = stream.until("ops")
        self.assertEqual({e["author"]["via"] for e in ops["events"]}, {"page"})
        stream.close()
        operator_line = [r for r in self.board("canvas_changed") if r["canvas"]["author"] == "human"]
        self.assertEqual(len(operator_line), 1)
        self.assertTrue(operator_line[0]["text"].startswith("the operator drew on the canvas:"), operator_line[0]["text"])
        self.assertEqual(sorted(operator_line[0]["to"]), ["alpha-reviewer", "alpha-worker"])
        seen = self.ok(["canvas", "look", "--since", "last"], pane=REVIEWER_PANE)
        self.assertEqual({c["author"] for c in seen["changes"]}, {"human"})
        self.assertIn("the operator added {} box".format(box_id), seen["text"])
        self.assertIn('{} box "Pricing page"'.format(box_id), seen["text"])

        # 12. The operator stops the page, then turns the layer off: every door refuses again, nothing is lost.
        self.assertEqual(self.ok(["whiteboard", "stop"]), {"stopped": True})
        for thread in self.server_threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "whiteboard-serve returned")
        self.assertFalse(W.state_path(self.ts.session).exists(), "whiteboard.json is removed on exit")
        with self.assertRaises(OSError):
            raw_request(self.port, "GET", "/api/session")
        disabled = self.ok(["whiteboard", "disable"])
        self.assertTrue(disabled["changed"])
        self.assertEqual(self.refused(["canvas", "look"], pane=REVIEWER_PANE)["code"], "whiteboard_off")
        self.assertIsNone(features.mcp_spec(layout, team))
        self.assertEqual(self.ok(["me"], pane=WORKER_PANE)["whiteboard"]["line"], "whiteboard: off")
        self.assertIsNotNone(canvas.load_scene(team)["elements"], "off deletes nothing")
        self.assertEqual(len(canvas.load_scene(team)["elements"]), len(scene["elements"]) + 1)
        self.assertEqual(self.refused(["whiteboard", "open", "--no-browser"])["code"], "whiteboard_off")


if __name__ == "__main__":
    unittest.main()

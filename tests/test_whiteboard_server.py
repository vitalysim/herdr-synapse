"""The whiteboard page server (``herdr_team.whiteboard_server``, 0.21): who may use it, what it serves, its stream, its lifecycle, and the built page.

Every test runs a real ``ThreadingHTTPServer`` on a random loopback port
with a temp state root. The canvas, the generated views and the activity
cards belong to other modules; they are faked here through their module
attributes so these tests pin the server's contract alone (contract
``.local/prd/canvas-contracts.md`` section 13). No browser, no network, no
Herdr server, no harness binary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from herdr_team import activity, canvas, features, store, views
from herdr_team import whiteboard_server as W
from herdr_team.errors import HerdrTeamError
from herdr_team.identity import Author

from support import PLUGIN_ROOT, TempState, whiteboard_on


def operator() -> Author:
    return Author("human", "human", "console", True)


def unverified_human() -> Author:
    return Author("human", "human", "cli-unverified", False, reason="not the foreground job")


def member() -> Author:
    return Author("alpha-worker", "claude", "cli", True, team="alpha")


SECRET = b"TOP-SECRET-OUTSIDE-DIST"


def make_static(root: Path) -> Path:
    """A small stand-in for ``web/dist`` plus a secret file beside it that must never be served."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "fonts" / "Excalifont").mkdir(parents=True)
    (dist / "viz-lib").mkdir()
    (dist / "index.html").write_text('<!doctype html><title>page</title><script type="module" src="/assets/index-abc123.js"></script>', encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log('page')", encoding="utf-8")
    (dist / "fonts" / "Excalifont" / "Excalifont-Regular-0123abcd.woff2").write_bytes(b"wOF2font")
    (dist / "viz-lib" / "synapse-viz.js").write_text("window.synapse={}", encoding="utf-8")
    (dist / "viz-lib" / "d3.min.js").write_text("var d3={}", encoding="utf-8")
    (dist / ".hidden").write_text("hidden", encoding="utf-8")
    (root / "secret.txt").write_bytes(SECRET)
    return dist


class FakeCanvas:
    """The canvas functions the server calls, recording every call."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, tuple, dict]] = []
        self.events: List[Dict[str, Any]] = []
        self.exports: List[Dict[str, Any]] = []
        self.apply_error: Optional[BaseException] = None
        self.scene = {"v": 1, "team": "alpha", "version": 0, "elements": [], "claims": [], "locks": [], "legend": [], "homes": {},
                      "authors": {"alpha-worker": {"color": "#1971c2", "index": 0, "kind": "member", "agent": "claude"}}, "batches": {},
                      "counters": {}}
        self.asset_dir: Optional[Path] = None

    def record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> List[str]:
        return [name for name, _args, _kwargs in self.calls]

    def version(self) -> int:
        return self.events[-1]["seq"] if self.events else 0

    def add_event(self, team: Any, op: str = "shape") -> Dict[str, Any]:
        seq = self.version() + 1
        event = {"v": 1, "seq": seq, "ts": store.now_iso(), "batch": "B-{}".format(seq), "author": {"name": "alpha-worker", "kind": "member"},
                 "op": op, "index": 0, "intent": "draw", "changes": [{"target": "element", "action": "add", "id": "E-{}".format(seq),
                                                                      "value": {"id": "E-{}".format(seq), "type": "box"}}]}
        self.events.append(event)
        path = features.whiteboard_dir(team) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        return event

    # -- the canvas surface --------------------------------------------------------

    def load_scene(self, team: Any) -> Dict[str, Any]:
        self.record("load_scene", team.name)
        return dict(self.scene, version=self.version())

    def current_version(self, team: Any) -> int:
        return self.version()

    def changes_since(self, team: Any, version: int, limit: int = 500) -> Dict[str, Any]:
        self.record("changes_since", team.name, version)
        current = self.version()
        if version > current:
            return {"version": current, "since": version, "events": [], "complete": True, "reset": True}
        events = [e for e in self.events if e["seq"] > version][:limit]
        return {"version": current, "since": version, "events": events, "complete": len(events) < limit or events[-1]["seq"] == current,
                "reset": False}

    def parse_batch(self, raw: Any) -> Tuple[List[Dict[str, Any]], bool]:
        self.record("parse_batch", raw)
        if isinstance(raw, dict):
            return list(raw.get("ops") or []), bool(raw.get("atomic", False))
        return list(raw), False

    def page_author(self, writable: bool) -> Any:
        return canvas.CanvasAuthor("human", "human", "page", True, operator=bool(writable))

    def apply_ops(self, layout: Any, team: Any, ops: Any, author: Any, atomic: bool = False, doc: Any = None, now: Any = None) -> Dict[str, Any]:
        self.record("apply_ops", team.name, ops, author, atomic)
        if self.apply_error is not None:
            raise self.apply_error
        return {"team": team.name, "version": 7, "batch": "B-3", "atomic": atomic, "applied": [{"index": 0, "op": "shape", "ids": ["E-1"]}],
                "refused": [{"index": 1, "op": "pen", "code": "canvas_locked", "message": "locked", "details": {"lock": "X-1"}}],
                "aliases": {"k1": "E-1"}, "warnings": [], "notices": {}}

    def send_to_member(self, layout: Any, team: Any, ids: Any, to: str, note: Any, author: Any) -> Dict[str, Any]:
        self.record("send_to_member", team.name, list(ids), to, note, author)
        return {"seq": 12, "image": "/tmp/send.png"}

    def store_asset(self, team: Any, data: bytes, kind: str) -> Dict[str, Any]:
        self.record("store_asset", team.name, data, kind)
        return {"asset": "0123456789abcdef0123456789abcdef.png", "mime": "image/png", "px_w": 2, "px_h": 3}

    def store_still(self, team: Any, element_id: str, version: int, png: bytes, **kw: Any) -> Path:
        self.record("store_still", team.name, element_id, version, png, **kw)
        return Path("/tmp/still.png")

    def complete_export(self, team: Any, request_id: str, png: bytes) -> Path:
        self.record("complete_export", team.name, request_id, png)
        return Path("/tmp/export.png")

    def pending_exports(self, team: Any, max_age_s: float = 30.0) -> List[Dict[str, Any]]:
        return list(self.exports)

    def asset_path(self, team: Any, name: str) -> Path:
        self.record("asset_path", team.name, name)
        assert self.asset_dir is not None
        path = self.asset_dir / name
        if not path.exists():
            raise HerdrTeamError("element_unknown", "no such asset")
        return path

    def artifact_file(self, layout: Any, team: Any, rel: str, doc: Any = None) -> Path:
        self.record("artifact_file", team.name, rel)
        if ".." in rel:
            raise HerdrTeamError("path_refused", "outside the artifacts dir")
        assert self.asset_dir is not None
        return self.asset_dir / rel

    def viz_source(self, team: Any, element_id: str) -> Dict[str, Any]:
        self.record("viz_source", team.name, element_id)
        return {"html": "<script>draw()</script>", "libs": ["d3", "three"], "title": "Churn", "version": 4, "data": None, "data_path": None}

    def text_form(self, scene: Any, ids: Any, reader: Any = None) -> str:
        self.record("text_form", list(ids), reader)
        return "E-1 box \"x\""

    def patches(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "load_scene", "current_version", "changes_since", "parse_batch", "page_author", "apply_ops", "send_to_member", "store_asset",
            "store_still", "complete_export", "pending_exports", "asset_path", "artifact_file", "viz_source", "text_form")}


class Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes, cookies: List[str]) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.cookies = cookies

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())


def raw_request(port: int, method: str, path: str, headers: Optional[Dict[str, str]] = None, body: Optional[bytes] = None,
                host: Optional[str] = None) -> Response:
    """One HTTP/1.1 request on a fresh socket, so forged Host headers and odd paths go out exactly as written."""
    lines = ["{} {} HTTP/1.1".format(method, path), "Host: {}".format(host if host is not None else "127.0.0.1:{}".format(port)), "Connection: close"]
    for key, value in (headers or {}).items():
        lines.append("{}: {}".format(key, value))
    if body is not None:
        lines.append("Content-Length: {}".format(len(body)))
    data = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b"")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, _, rest = raw.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split()[1])
    parsed: Dict[str, str] = {}
    cookies: List[str] = []
    for line in head_lines[1:]:
        key, _, value = line.partition(":")
        if key.lower() == "set-cookie":
            cookies.append(value.strip())
        parsed[key.strip().lower()] = value.strip()
    length = parsed.get("content-length")
    return Response(status, parsed, rest[: int(length)] if length is not None else rest, cookies)


class ServerCase(unittest.TestCase):
    """A running server with a signed-in page session; the canvas, views and activity faked."""

    writable_server = True
    layer_on = True

    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout
        if self.layer_on:
            whiteboard_on(self.ts.session, self.ts.team, via="console")
        self.static_root = Path(tempfile.mkdtemp(prefix="wb-static-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.static_root, ignore_errors=True))
        self.dist = make_static(self.static_root)
        self.fake = FakeCanvas()
        self.fake.asset_dir = self.static_root / "assets-store"
        self.fake.asset_dir.mkdir()
        for name, value in self.fake.patches().items():
            patcher = mock.patch.object(canvas, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.views_calls: List[str] = []
        self.cards: List[Dict[str, Any]] = [{"key": "term_w1", "name": "alpha-worker", "state": "working"}]
        views_patch = mock.patch.object(views, "team_views", side_effect=lambda layout, name, api=None, now=None: self._views(name))
        cards_patch = mock.patch.object(activity, "cards", side_effect=lambda layout, api=None, team=None, now=None: list(self.cards))
        for patcher in (views_patch, cards_patch):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = W.make_server(self.layout, self.ts.env, port=0, static_dir=self.dist, writable=self.writable_server, api=None)
        self.port = self.server.port
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop_server, thread)

    def _views(self, name: str) -> Dict[str, Any]:
        self.views_calls.append(name)
        return {"team": name, "work": {"nodes": [], "edges": []}, "facts": {"subjects": [], "disputes": []}, "topology": {}, "timeline": [],
                "lanes": {}, "canvas": None}

    def _stop_server(self, thread: threading.Thread) -> None:
        self.server.request_stop("stopped")
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=5)

    # -- requests ----------------------------------------------------------------------

    def request(self, method: str, path: str, headers: Optional[Dict[str, str]] = None, body: Optional[bytes] = None,
                host: Optional[str] = None) -> Response:
        return raw_request(self.port, method, path, headers, body, host)

    def sign_in(self, writable: bool = True) -> Tuple[str, str]:
        url = W.mint_ticket(self.layout, writable, "human", port=self.port)
        response = self.request("GET", url[len("http://127.0.0.1:{}".format(self.port)):])
        self.assertEqual(response.status, 303)
        cookie = response.cookies[0].split(";", 1)[0]
        session = self.request("GET", "/api/session", {"Cookie": cookie}).json()
        return cookie, session["csrf"]

    def authed(self, method: str, path: str, cookie: str, csrf: Optional[str] = None, body: Optional[bytes] = None,
               content_type: str = "application/json", extra: Optional[Dict[str, str]] = None) -> Response:
        headers = {"Cookie": cookie}
        if method == "POST":
            headers["Origin"] = "http://127.0.0.1:{}".format(self.port)
            headers["Content-Type"] = content_type
            if csrf is not None:
                headers[W.CSRF_HEADER] = csrf
            if body is None:
                body = b""
        headers.update(extra or {})
        return self.request(method, path, headers, body)

    def post_json(self, path: str, obj: Any, cookie: str, csrf: str) -> Response:
        return self.authed("POST", path, cookie, csrf, json.dumps(obj).encode("utf-8"))


# ---------------------------------------------------------------------------------------
# tickets and cookies


class TicketTests(ServerCase):
    def test_a_ticket_becomes_an_httponly_strict_cookie_and_leaves_the_address_bar(self):
        url = W.mint_ticket(self.layout, True, "human", port=self.port)
        self.assertTrue(url.startswith("http://127.0.0.1:{}/?ticket=".format(self.port)))
        ticket = url.split("ticket=", 1)[1]
        self.assertGreaterEqual(len(ticket), 43, "32 random bytes, urlsafe base64")
        stored = W.tickets_dir(self.ts.session) / (hashlib.sha256(ticket.encode()).hexdigest() + ".json")
        self.assertTrue(stored.is_file(), "only the ticket's hash names the file")
        self.assertNotIn(ticket, stored.read_text())
        self.assertEqual(stat.S_IMODE(os.stat(stored).st_mode), 0o600)
        response = self.request("GET", "/?ticket=" + ticket)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/", "the redirect drops the ticket from the address bar and history")
        cookie = response.cookies[0]
        self.assertTrue(cookie.startswith(W.COOKIE_NAME + "="))
        flags = [part.strip() for part in cookie.split(";")[1:]]
        self.assertIn("HttpOnly", flags)
        self.assertIn("SameSite=Strict", flags)
        self.assertIn("Path=/", flags)
        self.assertNotIn(ticket, cookie, "the cookie is a new secret, not the ticket")
        self.assertFalse(stored.exists(), "a used ticket is deleted")

    def test_a_ticket_opens_the_page_once(self):
        url = W.mint_ticket(self.layout, True, "human", port=self.port)
        path = url.split(str(self.port), 1)[1]
        self.assertEqual(self.request("GET", path).status, 303)
        again = self.request("GET", path)
        self.assertEqual(again.status, 401)
        self.assertIn(b"already used or has expired", again.body)
        self.assertEqual(again.cookies, [])

    def test_an_expired_ticket_is_refused(self):
        url = W.mint_ticket(self.layout, True, "human", port=self.port)
        ticket = url.split("ticket=", 1)[1]
        path = W.tickets_dir(self.ts.session) / (hashlib.sha256(ticket.encode()).hexdigest() + ".json")
        doc = store.read_json(path)
        doc["expires_at"] = "2020-01-01T00:00:00.000Z"
        store.write_json(path, doc)
        self.assertEqual(self.request("GET", "/?ticket=" + ticket).status, 401)
        self.assertFalse(path.exists())

    def test_minting_prunes_expired_tickets_and_needs_a_port(self):
        directory = W.tickets_dir(self.ts.session)
        directory.mkdir(parents=True, exist_ok=True)
        store.write_json(directory / ("0" * 64 + ".json"), {"expires_at": "2020-01-01T00:00:00.000Z", "writable": True, "by": "human"})
        W.mint_ticket(self.layout, True, "human", port=self.port)
        self.assertFalse((directory / ("0" * 64 + ".json")).exists())
        with self.assertRaises(HerdrTeamError) as ctx:
            W.mint_ticket(self.layout, True, "human")
        self.assertEqual(ctx.exception.code, "whiteboard_not_running")

    def test_without_a_cookie_nothing_but_the_sign_in_hint_is_served(self):
        api = self.request("GET", "/api/session")
        self.assertEqual(api.status, 401)
        self.assertEqual(api.json()["code"], "not_signed_in")
        page = self.request("GET", "/")
        self.assertEqual(page.status, 401)
        self.assertIn(b"prefix+a", page.body)
        self.assertIn(b"herdr-synapse whiteboard open", page.body)
        self.assertEqual(page.header("Content-Security-Policy"), W.PAGE_CSP)
        self.assertEqual(self.request("GET", "/assets/index-abc123.js").status, 401)
        forged = self.request("GET", "/api/session", {"Cookie": W.COOKIE_NAME + "=" + "A" * 43})
        self.assertEqual(forged.status, 401)
        self.assertEqual(self.request("GET", "/?ticket=nope").status, 401)

    def test_the_session_shape(self):
        cookie, csrf = self.sign_in()
        body = self.request("GET", "/api/session", {"Cookie": cookie}).json()
        self.assertEqual(set(body), {"server_version", "writable", "csrf", "switches", "teams", "limits"})
        self.assertTrue(body["writable"])
        self.assertEqual(body["csrf"], csrf)
        self.assertTrue(body["switches"]["session"]["enabled"])
        self.assertEqual([row["name"] for row in body["teams"]], ["alpha"])
        row = body["teams"][0]
        self.assertEqual(set(row), {"name", "on", "viz", "version", "members"})
        self.assertEqual(set(row["members"][0]), {"name", "role", "kind", "status", "manager", "color"})
        self.assertEqual([m["name"] for m in row["members"]], ["alpha-reviewer", "alpha-worker"], "the human row is not a member")
        self.assertEqual({m["name"]: m["color"] for m in row["members"]}, {"alpha-reviewer": None, "alpha-worker": "#1971c2"})


# ---------------------------------------------------------------------------------------
# Host, Origin, CSRF


class RequestCheckTests(ServerCase):
    def test_a_foreign_host_is_refused(self):
        cookie, _csrf = self.sign_in()
        for host in ("evil.example:{}".format(self.port), "127.0.0.1:{}".format(self.port + 1), "127.0.0.1", "[::1]:{}".format(self.port), ""):
            response = self.request("GET", "/api/session", {"Cookie": cookie}, host=host)
            self.assertEqual(response.status, 403, host)
            self.assertEqual(response.json()["code"], "bad_host")
        self.assertEqual(self.request("GET", "/api/session", {"Cookie": cookie}, host="localhost:{}".format(self.port)).status, 200)

    def test_a_foreign_origin_or_fetch_site_is_refused(self):
        cookie, _csrf = self.sign_in()
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "http://127.0.0.1:{}".format(self.port + 1)}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            response = self.request("GET", "/api/session", dict(headers, Cookie=cookie))
            self.assertEqual(response.status, 403, headers)
            self.assertEqual(response.json()["code"], "bad_origin")
        ok = self.request("GET", "/api/session", {"Cookie": cookie, "Origin": "http://localhost:{}".format(self.port), "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(ok.status, 200)

    def test_writes_need_the_origin_and_the_csrf_token(self):
        cookie, csrf = self.sign_in()
        body = json.dumps({"ops": [{"op": "shape"}]}).encode()
        no_origin = self.request("POST", "/api/teams/alpha/ops", {"Cookie": cookie, W.CSRF_HEADER: csrf, "Content-Type": "application/json"}, body)
        self.assertEqual((no_origin.status, no_origin.json()["code"]), (403, "bad_origin"))
        no_token = self.authed("POST", "/api/teams/alpha/ops", cookie, None, body)
        self.assertEqual((no_token.status, no_token.json()["code"]), (403, "bad_csrf"))
        wrong = self.authed("POST", "/api/teams/alpha/ops", cookie, "x" * 43, body)
        self.assertEqual((wrong.status, wrong.json()["code"]), (403, "bad_csrf"))
        self.assertNotIn("apply_ops", self.fake.names())
        self.assertEqual(self.authed("POST", "/api/teams/alpha/ops", cookie, csrf, body).status, 200)

    def test_no_cors_headers_outside_viz_lib(self):
        cookie, _csrf = self.sign_in()
        for path in ("/api/session", "/", "/api/teams/alpha/scene"):
            response = self.request("GET", path, {"Cookie": cookie, "Origin": "http://127.0.0.1:{}".format(self.port)})
            self.assertIsNone(response.header("Access-Control-Allow-Origin"), path)

    def test_only_get_and_post(self):
        cookie, _csrf = self.sign_in()
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            response = self.request(method, "/api/session", {"Cookie": cookie})
            self.assertEqual(response.status, 405, method)


# ---------------------------------------------------------------------------------------
# headers per route


class HeaderTests(ServerCase):
    def test_the_page_gets_the_strict_csp_and_no_framing(self):
        cookie, _csrf = self.sign_in()
        response = self.request("GET", "/", {"Cookie": cookie})
        self.assertEqual(response.status, 200)
        csp = response.header("Content-Security-Policy")
        self.assertEqual(csp, W.PAGE_CSP)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self' 'wasm-unsafe-eval';", csp)
        self.assertNotIn("'unsafe-eval'", csp.replace("'wasm-unsafe-eval'", ""))
        self.assertNotIn("http", csp, "no remote origin anywhere in the policy")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(response.header("X-Frame-Options"), "DENY")
        self.assertEqual(response.header("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.header("Referrer-Policy"), "no-referrer")
        self.assertEqual(response.header("Cross-Origin-Opener-Policy"), "same-origin")
        self.assertEqual(response.header("Cache-Control"), "no-store")
        self.assertEqual(response.header("Content-Type"), "text/html; charset=utf-8")

    def test_hashed_static_files_are_immutable(self):
        cookie, _csrf = self.sign_in()
        script = self.request("GET", "/assets/index-abc123.js", {"Cookie": cookie})
        self.assertEqual(script.status, 200)
        self.assertEqual(script.header("Content-Type"), "text/javascript; charset=utf-8")
        self.assertIn("immutable", script.header("Cache-Control"))
        font = self.request("GET", "/fonts/Excalifont/Excalifont-Regular-0123abcd.woff2", {"Cookie": cookie})
        self.assertEqual((font.status, font.header("Content-Type")), (200, "font/woff2"))

    def test_api_answers_cannot_render(self):
        cookie, _csrf = self.sign_in()
        response = self.request("GET", "/api/session", {"Cookie": cookie})
        self.assertEqual(response.header("Content-Type"), "application/json")
        self.assertEqual(response.header("Content-Security-Policy"), W.API_CSP)
        self.assertEqual(response.header("Cache-Control"), "no-store")

    def test_assets_are_sandboxed_and_html_assets_are_never_served(self):
        cookie, _csrf = self.sign_in()
        name = "0123456789abcdef0123456789abcdef.svg"
        (self.fake.asset_dir / name).write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        (self.fake.asset_dir / "0123456789abcdef0123456789abcdef.html").write_text("<script>x()</script>", encoding="utf-8")
        response = self.request("GET", "/api/teams/alpha/assets/" + name, {"Cookie": cookie})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("Content-Type"), "image/svg+xml")
        self.assertEqual(response.header("Content-Security-Policy"), W.ASSET_CSP)
        self.assertTrue(response.header("Content-Security-Policy").startswith("sandbox;"))
        html = self.request("GET", "/api/teams/alpha/assets/0123456789abcdef0123456789abcdef.html", {"Cookie": cookie})
        self.assertEqual(html.status, 404)
        missing = self.request("GET", "/api/teams/alpha/assets/ffffffffffffffffffffffffffffffff.png", {"Cookie": cookie})
        self.assertEqual((missing.status, missing.json()["code"]), (400, "element_unknown"))
        spec = self.fake.asset_dir / "0123456789abcdef0123456789abcdef.vl.json"
        spec.write_text("{}", encoding="utf-8")
        self.assertEqual(self.request("GET", "/api/teams/alpha/assets/" + spec.name, {"Cookie": cookie}).header("Content-Type"), "application/json")

    def test_viz_frames_get_their_own_sandbox_csp(self):
        cookie, _csrf = self.sign_in()
        response = self.request("GET", "/viz/alpha/E-4", {"Cookie": cookie})
        self.assertEqual(response.status, 200)
        csp = response.header("Content-Security-Policy")
        self.assertEqual(csp, W.VIZ_CSP.format(port=self.port))
        self.assertTrue(csp.startswith("sandbox allow-scripts;"))
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("script-src 'unsafe-inline' http://127.0.0.1:{}/viz-lib/;".format(self.port), csp)
        self.assertIn("frame-ancestors http://127.0.0.1:{0} http://localhost:{0}".format(self.port), csp)
        self.assertIsNone(response.header("X-Frame-Options"), "the page frames it")
        text = response.body.decode("utf-8")
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertLess(text.index("synapse-viz.js"), text.index("d3.min.js"))
        self.assertLess(text.index("d3.min.js"), text.index("<script>draw()</script>"), "the agent's HTML comes last")
        self.assertIn('"three":"http://127.0.0.1:{}/viz-lib/three.module.min.js"'.format(self.port), text)
        self.assertNotIn("p5.min.js", text)
        self.assertIn(("viz_source", ("alpha", "E-4"), {}), self.fake.calls)

    def test_viz_is_refused_when_the_team_turned_live_visuals_off(self):
        cookie, _csrf = self.sign_in()
        features.set_team(self.ts.team, viz=False)
        response = self.request("GET", "/viz/alpha/E-4", {"Cookie": cookie})
        self.assertEqual((response.status, response.json()["code"]), (403, "viz_off"))
        self.assertNotIn("viz_source", self.fake.names())
        self.assertEqual(self.request("GET", "/viz/alpha/nope", {"Cookie": cookie}).status, 403)

    def test_viz_lib_is_public_with_cors_and_nothing_else_is(self):
        response = self.request("GET", "/viz-lib/synapse-viz.js", {"Origin": "null"})
        self.assertEqual(response.status, 200, "an opaque-origin frame loads it with no cookie")
        self.assertEqual(response.header("Access-Control-Allow-Origin"), "*")
        self.assertEqual(response.header("Cross-Origin-Resource-Policy"), "cross-origin")
        self.assertEqual(response.header("Content-Type"), "text/javascript; charset=utf-8")
        self.assertEqual(response.body, b"window.synapse={}")
        for path in ("/viz-lib/../index.html", "/viz-lib/%2e%2e%2fsecret.txt", "/viz-lib/missing.js", "/viz-lib/", "/viz-lib/.hidden"):
            refused = self.request("GET", path)
            self.assertIn(refused.status, (401, 404), path)
            self.assertNotIn(SECRET, refused.body)
        self.assertEqual(self.request("GET", "/viz-lib/d3.min.js", host="evil.example:{}".format(self.port)).status, 403)

    def test_favicon_needs_no_cookie(self):
        self.assertEqual(self.request("GET", "/favicon.ico").status, 204)


# ---------------------------------------------------------------------------------------
# static files


class StaticTests(ServerCase):
    def test_path_traversal_is_refused(self):
        cookie, _csrf = self.sign_in()
        os.symlink(self.static_root / "secret.txt", self.dist / "assets" / "link.txt")
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt", "/assets/%2e%2e/%2e%2e/secret.txt",
                     "/assets/link.txt", "/.hidden", "/assets/%00x", "/assets\\..\\secret.txt", "//etc/passwd", "/assets/"):
            response = self.request("GET", path, {"Cookie": cookie})
            self.assertEqual(response.status, 404, path)
            self.assertNotIn(SECRET, response.body, path)

    def test_safe_static_path(self):
        self.assertEqual(W.safe_static_path("/"), "index.html")
        self.assertEqual(W.safe_static_path("/assets/a-1.js"), "assets/a-1.js")
        self.assertEqual(W.safe_static_path("/fonts/Excalifont/x.woff2"), "fonts/Excalifont/x.woff2")
        for bad in ("/../x", "/a/../b", "/%2e%2e/x", "/.env", "/a//b", "/a%00", "/a\\b", "relative", "/a/%ff"):
            self.assertIsNone(W.safe_static_path(bad), bad)

    def test_an_unbuilt_page_says_so(self):
        cookie, _csrf = self.sign_in()
        (self.dist / "index.html").unlink()
        response = self.request("GET", "/", {"Cookie": cookie})
        self.assertEqual((response.status, response.json()["code"]), (503, "page_not_built"))

    def test_unknown_files_are_not_found(self):
        cookie, _csrf = self.sign_in()
        self.assertEqual(self.request("GET", "/nope.js", {"Cookie": cookie}).status, 404)
        self.assertEqual(self.request("GET", "/api/nope", {"Cookie": cookie}).json()["code"], "not_found")


# ---------------------------------------------------------------------------------------
# read-only pages


class ReadOnlyServerTests(ServerCase):
    writable_server = False

    def test_a_server_not_started_by_the_operator_in_person_is_read_only(self):
        cookie, csrf = self.sign_in(writable=True)
        session = self.request("GET", "/api/session", {"Cookie": cookie}).json()
        self.assertFalse(session["writable"], "a writable ticket cannot lift a read-only server")
        for path, body in (("/api/teams/alpha/ops", b'{"ops": []}'), ("/api/teams/alpha/send", b"{}"), ("/api/teams/alpha/exports/r1", b"png"),
                           ("/api/teams/alpha/stills/E-1?v=1", b"png"), ("/api/teams/alpha/uploads", b"png")):
            response = self.authed("POST", path, cookie, csrf, body)
            self.assertEqual((response.status, response.json()["code"]), (403, "read_only"), path)
        self.assertEqual(self.fake.names().count("apply_ops"), 0)
        self.assertEqual(self.request("GET", "/api/teams/alpha/scene", {"Cookie": cookie}).status, 200, "reads still work")


class ReadOnlyTicketTests(ServerCase):
    def test_a_ticket_minted_for_an_unverified_human_is_read_only(self):
        cookie, csrf = self.sign_in(writable=False)
        self.assertFalse(self.request("GET", "/api/session", {"Cookie": cookie}).json()["writable"])
        response = self.post_json("/api/teams/alpha/ops", {"ops": [{"op": "shape"}]}, cookie, csrf)
        self.assertEqual((response.status, response.json()["code"]), (403, "read_only"))


# ---------------------------------------------------------------------------------------
# the JSON API


class ApiTests(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.cookie, self.csrf = self.sign_in()

    def get(self, path: str) -> Response:
        return self.request("GET", path, {"Cookie": self.cookie})

    def test_teams_lists_only_teams_with_the_canvas_on(self):
        self.assertEqual([row["name"] for row in self.get("/api/teams").json()["teams"]], ["alpha"])
        features.set_team(self.ts.team, enabled=False)
        self.assertEqual(self.get("/api/teams").json()["teams"], [])

    def test_scene_and_changes(self):
        self.fake.add_event(self.ts.team)
        self.fake.add_event(self.ts.team)
        scene = self.get("/api/teams/alpha/scene")
        self.assertEqual(scene.status, 200)
        self.assertEqual(scene.json()["version"], 2)
        changes = self.get("/api/teams/alpha/changes?since=1").json()
        self.assertEqual([e["seq"] for e in changes["events"]], [2])
        self.assertEqual(set(changes), {"version", "since", "events", "complete", "reset"})
        self.assertEqual(self.get("/api/teams/alpha/changes?since=-1").status, 400)
        self.assertEqual(self.get("/api/teams/alpha/changes?since=x").json()["code"], "usage")

    def test_team_routes_check_the_switches_and_the_team(self):
        features.set_team(self.ts.team, enabled=False)
        off = self.get("/api/teams/alpha/scene")
        self.assertEqual((off.status, off.json()["code"], off.json()["scope"]), (403, "whiteboard_off", "team"))
        features.set_team(self.ts.team, enabled=True)
        features.set_layer(self.ts.session, False, "human", "cli")
        layer = self.get("/api/teams/alpha/scene")
        self.assertEqual((layer.status, layer.json()["scope"]), (403, "session"))
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        for path in ("/api/teams/nobody/scene", "/api/teams/Bad..Name/scene", "/api/teams/%2e%2e/scene"):
            response = self.get(path)
            self.assertEqual((response.status, response.json()["code"]), (404, "team_not_found"), path)
            self.assertNotIn(str(self.ts.tmp), response.body.decode("utf-8"), "no state path reaches the page (phase 1 QA #14)")

    def test_ops_apply_as_the_human_on_the_page(self):
        response = self.post_json("/api/teams/alpha/ops", {"ops": [{"op": "shape", "kind": "box", "at": "c1r1"}], "atomic": True}, self.cookie, self.csrf)
        self.assertEqual(response.status, 200, "refusals inside the result are still a 200")
        body = response.json()
        self.assertEqual(body["refused"][0]["code"], "canvas_locked")
        name, args, _kwargs = [call for call in self.fake.calls if call[0] == "apply_ops"][0]
        team, ops, author, atomic = args
        self.assertEqual((team, ops, atomic), ("alpha", [{"op": "shape", "kind": "box", "at": "c1r1"}], True))
        self.assertEqual((author.name, author.kind, author.via, author.operator), ("human", "human", "page", True))

    def test_batch_level_refusals_map_to_http_codes(self):
        cases = [("canvas_rate", 429), ("canvas_stale", 409), ("op_invalid", 400), ("canvas_limit", 413), ("whiteboard_off", 403),
                 ("canvas_busy", 503), ("canvas_refused", 409)]
        for code, status in cases:
            self.fake.apply_error = HerdrTeamError(code, "refused: " + code, details={"retry_after_s": 3})
            response = self.post_json("/api/teams/alpha/ops", {"ops": [{"op": "shape"}]}, self.cookie, self.csrf)
            self.assertEqual(response.status, status, code)
            self.assertEqual(response.json()["code"], code)
            self.assertEqual(response.json()["retry_after_s"], 3, "details travel with the error")

    def test_an_unexpected_failure_is_a_500_without_a_traceback(self):
        self.fake.apply_error = RuntimeError("secret internals /Users/x")
        response = self.post_json("/api/teams/alpha/ops", {"ops": [{"op": "shape"}]}, self.cookie, self.csrf)
        self.assertEqual((response.status, response.json()["code"]), (500, "internal"))
        self.assertNotIn(b"Traceback", response.body)
        self.assertNotIn(b"secret internals", response.body)
        self.fake.apply_error = HerdrTeamError("some_new_code", "internal detail")
        self.assertEqual(self.post_json("/api/teams/alpha/ops", {"ops": []}, self.cookie, self.csrf).json()["code"], "internal")

    def test_bodies_are_bounded_and_must_be_json(self):
        with mock.patch.object(W, "MAX_BODY_BYTES", 64):
            big = self.authed("POST", "/api/teams/alpha/ops", self.cookie, self.csrf, b"{" + b" " * 100 + b"}")
        self.assertEqual((big.status, big.json()["code"]), (413, "body_too_large"))
        bad = self.authed("POST", "/api/teams/alpha/ops", self.cookie, self.csrf, b"{nope")
        self.assertEqual((bad.status, bad.json()["code"]), (400, "usage"))

    def test_send_to_member(self):
        good = self.post_json("/api/teams/alpha/send", {"to": "alpha-worker", "elements": ["E-1", "E-2"], "note": "merge these"}, self.cookie, self.csrf)
        self.assertEqual(good.json(), {"seq": 12, "image": "/tmp/send.png"})
        call = [c for c in self.fake.calls if c[0] == "send_to_member"][0]
        self.assertEqual(call[1][:4], ("alpha", ["E-1", "E-2"], "alpha-worker", "merge these"))
        for body in ({"to": "", "elements": ["E-1"]}, {"to": "x", "elements": []}, {"to": "x", "elements": ["bad"]}, {"to": "x", "elements": ["E-1"], "note": 3},
                     ["E-1"]):
            response = self.post_json("/api/teams/alpha/send", body, self.cookie, self.csrf)
            self.assertEqual((response.status, response.json()["code"]), (400, "usage"), body)

    def test_uploads_stills_and_exports(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        upload = self.authed("POST", "/api/teams/alpha/uploads", self.cookie, self.csrf, png, content_type="image/png")
        self.assertEqual(upload.json(), {"asset": "0123456789abcdef0123456789abcdef.png", "mime": "image/png", "px_w": 2, "px_h": 3})
        self.assertIn(("store_asset", ("alpha", png, "png"), {}), self.fake.calls)
        wrong = self.authed("POST", "/api/teams/alpha/uploads", self.cookie, self.csrf, b"text", content_type="text/plain")
        self.assertEqual(wrong.status, 415)
        still = self.authed("POST", "/api/teams/alpha/stills/E-4?v=9", self.cookie, self.csrf, png, content_type="image/png")
        self.assertEqual(still.json(), {"ok": True})
        self.assertIn(("store_still", ("alpha", "E-4", 9, png), {}), self.fake.calls)
        # A still of one view (canvas v2 phases 3 and 4, 1.3): the view travels as a query field.
        viewed = self.authed("POST", "/api/teams/alpha/stills/E-4?v=9&view=iso", self.cookie, self.csrf, png, content_type="image/png")
        self.assertEqual(viewed.json(), {"ok": True})
        self.assertIn(("store_still", ("alpha", "E-4", 9, png), {"view": "iso"}), self.fake.calls)
        for path in ("/api/teams/alpha/stills/E-4", "/api/teams/alpha/stills/K-4?v=1", "/api/teams/alpha/stills/E-4?v=-2",
                     "/api/teams/alpha/stills/E-4?v=1&view=ISO", "/api/teams/alpha/stills/E-4?v=1&view=../x", "/api/teams/alpha/stills/E-4?v=1&view=iso1"):
            self.assertEqual(self.authed("POST", path, self.cookie, self.csrf, png, content_type="image/png").status, 400, path)
        export = self.authed("POST", "/api/teams/alpha/exports/req-1", self.cookie, self.csrf, png, content_type="image/png")
        self.assertEqual(export.json(), {"ok": True})
        self.assertIn(("complete_export", ("alpha", "req-1", png), {}), self.fake.calls)
        self.assertEqual(self.authed("POST", "/api/teams/alpha/exports/..%2fx", self.cookie, self.csrf, png).status, 400)

    def test_artifact_data_views_text_and_activity(self):
        (self.fake.asset_dir / "churn.csv").write_text("month,rate\n1,0.1\n", encoding="utf-8")
        data = self.get("/api/teams/alpha/artifact?path=churn.csv")
        self.assertEqual((data.status, data.header("Content-Type")), (200, "text/csv; charset=utf-8"))
        self.assertEqual(data.header("Content-Security-Policy"), W.ASSET_CSP)
        self.assertEqual(data.body, b"month,rate\n1,0.1\n")
        refused = self.get("/api/teams/alpha/artifact?path=../x.csv")
        self.assertEqual((refused.status, refused.json()["code"]), (403, "path_refused"))
        self.assertEqual(self.get("/api/teams/alpha/artifact").status, 400)
        self.assertEqual(self.get("/api/teams/alpha/views").json()["team"], "alpha")
        self.assertEqual(self.views_calls, ["alpha"])
        self.assertEqual(self.get("/api/teams/alpha/text?ids=E-1,C-2").json(), {"text": 'E-1 box "x"'})
        self.assertIn(("text_form", (["E-1", "C-2"], "human"), {}), self.fake.calls)
        self.assertEqual(self.get("/api/teams/alpha/text?ids=bad").status, 400)
        self.assertEqual(self.get("/api/activity?team=alpha").json(), {"cards": self.cards})
        features.set_layer(self.ts.session, False, "human", "cli")
        self.assertEqual(self.get("/api/activity").json()["code"], "whiteboard_off")

    def test_wrong_methods_on_api_routes(self):
        self.assertEqual(self.get("/api/teams/alpha/ops").status, 405)
        self.assertEqual(self.authed("POST", "/api/teams/alpha/scene", self.cookie, self.csrf, b"{}").status, 405)


# ---------------------------------------------------------------------------------------
# the event stream


class SSE:
    """A raw SSE client: reads frames off the socket so timing and framing are exact."""

    def __init__(self, port: int, path: str, headers: Dict[str, str]) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        lines = ["GET {} HTTP/1.1".format(path), "Host: 127.0.0.1:{}".format(port)] + ["{}: {}".format(k, v) for k, v in headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        self.file = self.sock.makefile("rb")
        self.status = int(self.file.readline().split()[1])
        self.headers: Dict[str, str] = {}
        while True:
            line = self.file.readline().decode("latin-1").strip()
            if not line:
                break
            key, _, value = line.partition(":")
            self.headers[key.strip().lower()] = value.strip()
        self.raw: List[str] = []

    def next(self) -> Tuple[Optional[str], Optional[str], Any]:
        """The next frame ``(event, id, data)``; comment lines are kept in ``raw`` and skipped."""
        event = event_id = None
        data = None
        while True:
            line = self.file.readline()
            if not line:
                raise EOFError("stream closed")
            text = line.decode("utf-8").rstrip("\n")
            self.raw.append(text)
            if text == "":
                if event is not None or data is not None:
                    return event, event_id, data
                continue
            if text.startswith(":"):
                continue
            key, _, value = text.partition(": ")
            if key == "event":
                event = value
            elif key == "id":
                event_id = value
            elif key == "data":
                data = json.loads(value)

    def until(self, name: str, limit: int = 50) -> Tuple[Optional[str], Any]:
        for _ in range(limit):
            event, event_id, data = self.next()
            if event == name:
                return event_id, data
        raise AssertionError("no {} frame".format(name))

    def close(self) -> None:
        try:
            self.file.close()
            self.sock.close()
        except OSError:
            pass


class StreamTests(ServerCase):
    def setUp(self) -> None:
        for name, value in (("POLL_S", 0.02), ("VIEWS_MIN_S", 0.05), ("ACTIVITY_MIN_S", 3600.0), ("KEEPALIVE_S", 3600.0)):
            patcher = mock.patch.object(W, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
        self.cookie, self.csrf = self.sign_in()

    def open(self, path: str, **headers: str) -> SSE:
        stream = SSE(self.port, path, dict(headers, Cookie=self.cookie))
        self.addCleanup(stream.close)
        return stream

    def test_connect_without_since_gets_hello_then_the_scene(self):
        self.fake.add_event(self.ts.team)
        stream = self.open("/api/stream?team=alpha")
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(stream.headers["cache-control"], "no-store")
        event, event_id, hello = stream.next()
        self.assertEqual(event, "hello")
        self.assertIsNone(event_id, "hello must not move Last-Event-ID")
        self.assertEqual(set(hello), {"team", "version", "writable", "switch", "server_version"})
        self.assertEqual((hello["team"], hello["version"], hello["writable"]), ("alpha", 1, True))
        self.assertTrue(hello["switch"]["on"])
        event, event_id, scene = stream.next()
        self.assertEqual((event, event_id), ("scene", "1"))
        self.assertEqual(scene["team"], "alpha")
        self.assertEqual(scene["scene"]["version"], 1)

    def test_since_and_new_events_arrive_as_ops_frames(self):
        for _ in range(3):
            self.fake.add_event(self.ts.team)
        stream = self.open("/api/stream?team=alpha&since=1")
        stream.until("hello")
        event_id, ops = stream.until("ops")
        self.assertEqual(event_id, "3")
        self.assertEqual((ops["team"], ops["version"], [e["seq"] for e in ops["events"]]), ("alpha", 3, [2, 3]))
        self.fake.add_event(self.ts.team)
        event_id, ops = stream.until("ops")
        self.assertEqual((event_id, [e["seq"] for e in ops["events"]]), ("4", [4]))
        self.assertIn("id: 4", stream.raw)

    def test_last_event_id_overrides_since(self):
        for _ in range(3):
            self.fake.add_event(self.ts.team)
        stream = self.open("/api/stream?team=alpha&since=0", **{"Last-Event-ID": "2"})
        _event_id, ops = stream.until("ops")
        self.assertEqual([e["seq"] for e in ops["events"]], [3])

    def test_a_since_ahead_of_the_log_resets_to_the_scene(self):
        self.fake.add_event(self.ts.team)
        stream = self.open("/api/stream?team=alpha&since=40")
        event_id, scene = stream.until("scene")
        self.assertEqual((event_id, scene["scene"]["version"]), ("1", 1))

    def test_stopping_the_server_says_bye(self):
        stream = self.open("/api/stream?team=alpha")
        stream.until("scene")
        self.server.request_stop("stopped")
        _event_id, bye = stream.until("bye")
        self.assertEqual(bye, {"reason": "stopped"})
        with self.assertRaises(EOFError):
            stream.next()

    def test_switching_the_layer_off_says_state_then_bye(self):
        stream = self.open("/api/stream?team=alpha")
        stream.until("scene")
        features.set_layer(self.ts.session, False, "human", "cli")
        _event_id, state = stream.until("state")
        self.assertFalse(state["switches"]["session"]["enabled"])
        _event_id, bye = stream.until("bye")
        self.assertEqual(bye, {"reason": "disabled"})

    def test_switching_the_team_off_ends_its_stream(self):
        stream = self.open("/api/stream?team=alpha")
        stream.until("scene")
        features.set_team(self.ts.team, enabled=False)
        _event_id, bye = stream.until("bye")
        self.assertEqual(bye, {"reason": "team_off"})

    def test_team_data_changes_push_views(self):
        stream = self.open("/api/stream?team=alpha")
        stream.until("scene")
        with open(self.ts.team.root / "work.jsonl", "a", encoding="utf-8") as handle:
            handle.write("{}\n")
        _event_id, pushed = stream.until("views")
        self.assertEqual(pushed["team"], "alpha")
        self.assertEqual(pushed["views"]["team"], "alpha")

    def test_export_requests_reach_writable_pages(self):
        self.fake.exports.append({"id": "req-9", "region": [0, 0, 100, 50], "marks": True, "grid": False, "by": "alpha-worker", "at": "…"})
        stream = self.open("/api/stream?team=alpha")
        _event_id, request = stream.until("export_request")
        self.assertEqual(request, {"team": "alpha", "request_id": "req-9", "region": [0, 0, 100, 50], "marks": True, "grid": False})

    def test_activity_is_pushed_only_when_it_changed(self):
        with mock.patch.object(W, "ACTIVITY_MIN_S", 0.05):
            stream = self.open("/api/stream")
            _event_id, hello = stream.until("hello")
            self.assertIsNone(hello["team"])
            _event_id, first = stream.until("activity")
            self.assertEqual(first, {"cards": self.cards})
            self.cards = [{"key": "term_w1", "name": "alpha-worker", "state": "idle"}]
            _event_id, second = stream.until("activity")
            self.assertEqual(second["cards"][0]["state"], "idle")

    def test_streams_are_limited_and_need_the_team_on(self):
        features.set_team(self.ts.team, enabled=False)
        refused = self.request("GET", "/api/stream?team=alpha", {"Cookie": self.cookie})
        self.assertEqual((refused.status, refused.json()["code"]), (403, "whiteboard_off"))
        features.set_team(self.ts.team, enabled=True)
        with mock.patch.object(W, "MAX_STREAMS", 1):
            first = self.open("/api/stream?team=alpha")
            first.until("hello")
            second = self.request("GET", "/api/stream?team=alpha", {"Cookie": self.cookie})
            self.assertEqual((second.status, second.json()["code"]), (503, "too_many_streams"))

    def test_a_closed_page_frees_its_stream(self):
        stream = self.open("/api/stream?team=alpha")
        stream.until("scene")
        self.assertEqual(self.server.active_streams(), 1)
        stream.close()
        deadline = time.monotonic() + 3
        while self.server.active_streams() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.server.active_streams(), 0)

    def test_sse_frame(self):
        self.assertEqual(W.sse_frame("ops", {"a": "b\nc"}, 7), b'event: ops\nid: 7\ndata: {"a":"b\\nc"}\n\n')
        self.assertEqual(W.sse_frame("bye", {"reason": "idle"}), b'event: bye\ndata: {"reason":"idle"}\n\n')


# ---------------------------------------------------------------------------------------
# the real canvas behind the page (builder A's module), with the page adapter's op shapes


class RealCanvasTests(unittest.TestCase):
    """The operations ``web/src/canvas/adapter.js`` sends, through the HTTP API into the real ``canvas.apply_ops``."""

    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout
        whiteboard_on(self.ts.session, self.ts.team, via="console")
        canvas.load_scene(self.ts.team)
        for patcher in (mock.patch.object(views, "team_views", return_value={}), mock.patch.object(activity, "cards", return_value=[]),
                        mock.patch.object(W, "POLL_S", 0.02)):
            patcher.start()
            self.addCleanup(patcher.stop)
        static_root = Path(tempfile.mkdtemp(prefix="wb-static-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(static_root, ignore_errors=True))
        self.server = W.make_server(self.layout, self.ts.env, static_dir=make_static(static_root), writable=True)
        self.port = self.server.port
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.request_stop("stopped"), self.server.shutdown(), self.server.server_close(), thread.join(5)))
        url = W.mint_ticket(self.layout, True, "human", port=self.port)
        self.cookie = raw_request(self.port, "GET", url.split(str(self.port), 1)[1]).cookies[0].split(";", 1)[0]
        self.csrf = raw_request(self.port, "GET", "/api/session", {"Cookie": self.cookie}).json()["csrf"]

    def post(self, ops: List[Dict[str, Any]]) -> Dict[str, Any]:
        response = raw_request(self.port, "POST", "/api/teams/alpha/ops", {
            "Cookie": self.cookie, "Origin": "http://127.0.0.1:{}".format(self.port), W.CSRF_HEADER: self.csrf, "Content-Type": "application/json",
        }, json.dumps({"ops": ops, "atomic": False}).encode("utf-8"))
        self.assertEqual(response.status, 200, response.body[:400])
        return response.json()

    def scene(self) -> Dict[str, Any]:
        return raw_request(self.port, "GET", "/api/teams/alpha/scene", {"Cookie": self.cookie}).json()

    def test_the_adapters_create_ops_are_accepted(self):
        style = {"color": "#1e1e1e", "width": 2, "dash": "solid", "opacity": 100}
        result = self.post([
            dict(style, op="shape", kind="box", text="", at=[474, 768], w=167, h=89, fill="none", rough=1, id="hVUaYiE85qpjxLXBmGHAAy",
                 client_id="VUaYiE85qpjxLXBmGHAAy"),
            dict(style, op="shape", kind="note", text="Price rise", at=[40, 40], w=160, h=100, fill="#ffec99", rough=1, font="hand", size=20,
                 id="hN1", client_id="N1"),
            {"op": "shape", "kind": "text", "text": "hello", "at": [10, 300], "color": "#1e1e1e", "opacity": 100, "font": "normal", "size": 28,
             "id": "hT1", "client_id": "T1"},
            dict(style, op="arrow", head="arrow", tail="none", curve=False, **{"from": "hVUaYiE85qpjxLXBmGHAAy"}, to="900,900", label="then",
                 id="hA1", client_id="A1"),
            dict(style, op="arrow", head="triangle", tail="dot", curve=True, points=[[0, 600], [100, 650], [200, 600]], id="hA2", client_id="A2"),
            {"op": "pen", "points": [[0, 0, 0.5], [10, 10, 0.6], [20, 5, 0.4]], "closed": False, "style": "smooth", "color": "#e03131", "width": 4,
             "opacity": 100, "id": "hP1"},
            {"op": "pen", "points": [[300, 0], [310, 10], [320, 5], [300, 0]], "closed": True, "style": "straight", "color": "#1e1e1e",
             "fill": "#a5d8ff", "width": 2, "opacity": 100, "id": "hL1"},
            {"op": "frame", "title": "Frame", "region": [1000, 1000, 1400, 1300], "id": "hF1"},
        ])
        self.assertEqual(result["refused"], [], "every op the adapter creates is valid")
        self.assertEqual(len(result["applied"]), 8)
        box = result["aliases"]["VUaYiE85qpjxLXBmGHAAy"]
        self.assertEqual(result["aliases"]["hVUaYiE85qpjxLXBmGHAAy"], box)
        elements = {el["id"]: el for el in self.scene()["elements"]}
        self.assertEqual(elements[box]["client_id"], "VUaYiE85qpjxLXBmGHAAy")
        self.assertEqual((elements[box]["author"], elements[box]["author_kind"]), ("human", "human"))
        arrow = elements[result["aliases"]["A1"]]
        self.assertEqual((arrow["from"], arrow["text"]), (box, "then"))

    def test_the_adapters_edit_ops_carry_if_version(self):
        created = self.post([{"op": "shape", "kind": "box", "text": "a", "at": [0, 0], "w": 160, "h": 80, "id": "hB", "client_id": "B"},
                             {"op": "shape", "kind": "box", "text": "b", "at": [400, 0], "w": 160, "h": 80, "id": "hC", "client_id": "C"}])
        first, second = created["aliases"]["B"], created["aliases"]["C"]
        seq = {el["id"]: el["updated_seq"] for el in self.scene()["elements"]}
        result = self.post([
            {"op": "move", "id": first, "to": [20, 20], "w": 200, "h": 90, "if_version": seq[first]},
            {"op": "restyle", "id": first, "color": "#e03131", "fill": "#ffc9c9", "width": 4, "dash": "dashed", "opacity": 80, "rough": 0},
            {"op": "edit", "id": first, "text": "renamed"},
            {"op": "move", "id": second, "frame": None, "if_version": seq[second]},
            {"op": "delete", "id": second, "if_version": seq[second]},
        ])
        codes = [r["code"] for r in result["refused"]]
        self.assertEqual(codes, ["canvas_stale"], "the second op on an element in one batch has moved its version on")
        stale = self.post([{"op": "move", "id": first, "to": [0, 0], "if_version": seq[first]}])
        self.assertEqual(stale["refused"][0]["code"], "canvas_stale")
        el = [e for e in self.scene()["elements"] if e["id"] == first][0]
        self.assertEqual((el["x"], el["y"], el["w"], el["text"], el["style"]["stroke"]), (20, 20, 200, "renamed", "#e03131"))

    def test_the_human_tools(self):
        created = self.post([{"op": "shape", "kind": "box", "text": "x", "at": [0, 0], "w": 160, "h": 80, "id": "hX", "client_id": "X"}])
        box = created["aliases"]["X"]
        result = self.post([
            {"op": "comment", "at": box, "text": "look at this @alpha-worker"},
            {"op": "lock", "region": [-20, -20, 200, 120], "label": "hands off"},
        ])
        self.assertEqual(result["refused"], [])
        scene = self.scene()
        comment = [el for el in scene["elements"] if el["type"] == "comment"][0]
        lock = scene["locks"][0]
        follow = self.post([{"op": "resolve", "id": comment["id"]}, {"op": "unlock", "id": lock["id"]}, {"op": "undo", "batch": created["batch"]}])
        self.assertEqual(follow["refused"], [])
        self.assertEqual(self.scene()["locks"], [])

    def png(self, width: int = 2, height: int = 3) -> bytes:
        import struct
        import zlib

        raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
        chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    def upload(self, path: str, body: bytes, content_type: str = "image/png") -> Response:
        return raw_request(self.port, "POST", path, {"Cookie": self.cookie, "Origin": "http://127.0.0.1:{}".format(self.port),
                                                     W.CSRF_HEADER: self.csrf, "Content-Type": content_type}, body)

    def test_viz_uploads_stills_exports_and_text_end_to_end(self):
        viz = self.post([{"op": "viz", "html": "<script>draw(synapse)</script>", "libs": ["d3", "three"], "title": "Orbit", "data": {"n": 3}, "at": [500, 500],
                          "intent": "show it moving"}])
        self.assertEqual(viz["refused"], [])
        viz_id = viz["applied"][0]["ids"][0]
        frame = raw_request(self.port, "GET", "/viz/alpha/" + viz_id, {"Cookie": self.cookie})
        self.assertEqual(frame.status, 200)
        text = frame.body.decode("utf-8")
        self.assertTrue(text.rstrip().endswith("<script>draw(synapse)</script>"))
        self.assertIn("d3.min.js", text)
        self.assertIn("importmap", text)
        uploaded = self.upload("/api/teams/alpha/uploads", self.png())
        self.assertEqual(uploaded.status, 200, uploaded.body)
        asset = uploaded.json()["asset"]
        self.assertEqual((uploaded.json()["px_w"], uploaded.json()["px_h"]), (2, 3))
        image = self.post([{"op": "image", "asset": asset, "at": [0, 0], "w": 20, "h": 30, "id": "hImg", "client_id": "Img"}])
        self.assertEqual(image["refused"], [])
        served = raw_request(self.port, "GET", "/api/teams/alpha/assets/" + asset, {"Cookie": self.cookie})
        self.assertEqual((served.status, served.header("Content-Type"), served.body), (200, "image/png", self.png()))
        self.assertEqual(self.upload("/api/teams/alpha/uploads", b"GIF89a....", "image/png").status, 413, "bytes, not the header, decide")
        seq = [el for el in self.scene()["elements"] if el["id"] == viz_id][0]["updated_seq"]
        still = self.upload("/api/teams/alpha/stills/{}?v={}".format(viz_id, seq), self.png())
        self.assertEqual(still.json(), {"ok": True})
        self.assertIsNotNone(canvas.still_path(self.ts.team, viz_id, seq))
        shown = raw_request(self.port, "GET", "/api/teams/alpha/stills/{}-v{}.png".format(viz_id, seq), {"Cookie": self.cookie})
        self.assertEqual((shown.status, shown.header("Content-Type"), shown.body), (200, "image/png", self.png()), "the page shows a still by its name")
        self.assertEqual(shown.header("Content-Security-Policy"), W.ASSET_CSP)
        for name in ("{}-v{}.png".format(viz_id, seq + 1), "../x.png", "{}-v{}-ISO.png".format(viz_id, seq)):
            self.assertEqual(raw_request(self.port, "GET", "/api/teams/alpha/stills/" + name, {"Cookie": self.cookie}).status, 404, name)
        request_id = canvas.request_export(self.ts.team, [0, 0, 100, 100], True, False, "alpha-worker")
        stream = SSE(self.port, "/api/stream?team=alpha", {"Cookie": self.cookie})
        self.addCleanup(stream.close)
        _event_id, pending = stream.until("export_request")
        self.assertEqual(pending["request_id"], request_id)
        self.assertEqual(self.upload("/api/teams/alpha/exports/" + request_id, self.png()).json(), {"ok": True})
        listing = raw_request(self.port, "GET", "/api/teams/alpha/text?ids=" + viz_id, {"Cookie": self.cookie}).json()["text"]
        self.assertIn(viz_id, listing)

    def test_the_page_sees_its_edit_on_the_stream(self):
        stream = SSE(self.port, "/api/stream?team=alpha&since=0", {"Cookie": self.cookie})
        self.addCleanup(stream.close)
        stream.until("hello")
        self.post([{"op": "shape", "kind": "box", "text": "live", "at": [0, 0], "w": 160, "h": 80, "id": "hL", "client_id": "L"}])
        _event_id, ops = stream.until("ops")
        event = ops["events"][0]
        self.assertEqual((event["author"]["name"], event["author"]["via"]), ("human", "page"))
        added = [c["value"] for c in event["changes"] if c["target"] == "element" and c["action"] == "add"]
        self.assertEqual([value["client_id"] for value in added], ["L"], "the page matches the echo to its own element by client_id")


# ---------------------------------------------------------------------------------------
# lifecycle: status, idle, serve, start, stop


class LifecycleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout
        self.log: List[str] = []
        for name, value in (("_process_start_time", lambda pid: "T0"), ("LOOP_S", 0.02), ("START_WAIT_S", 3.0), ("DRAIN_S", 0.5),
                            ("LOG", self.log.append)):
            patcher = mock.patch.object(W, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.static_root = Path(tempfile.mkdtemp(prefix="wb-static-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.static_root, ignore_errors=True))
        dist_patch = mock.patch.object(W, "web_dist", return_value=make_static(self.static_root))
        dist_patch.start()
        self.addCleanup(dist_patch.stop)

    def enable(self) -> None:
        whiteboard_on(self.ts.session, self.ts.team, via="console")


class StatusAndIdleTests(LifecycleCase):
    def test_status_of_nothing_a_stale_record_and_a_live_one(self):
        self.assertIsNone(W.status(self.layout))
        path = W.state_path(self.ts.session)
        store.write_json(path, {"v": 1, "pid": 999999, "port": 5, "start_time": "T0"})
        with mock.patch.object(W, "_pid_alive", return_value=False):
            self.assertIsNone(W.status(self.layout))
        self.assertFalse(path.exists(), "a stale record is removed")
        store.write_json(path, {"v": 1, "pid": 4242, "port": 5, "start_time": "T-other"})
        with mock.patch.object(W, "_pid_alive", return_value=True):
            self.assertIsNone(W.status(self.layout), "a reused pid with another start time is not ours")
        store.write_json(path, {"v": 1, "pid": os.getpid(), "port": 8123, "start_time": "T0", "writable": True, "beat_at": store.now_iso()})
        live = W.status(self.layout)
        self.assertTrue(live["alive"])
        self.assertEqual(live["url"], "http://127.0.0.1:8123/")
        self.assertNotIn("ticket", live["url"])
        self.assertLess(live["beat_age_s"], 5)

    def test_idle_logic(self):
        self.enable()
        server = W.make_server(self.layout, self.ts.env, static_dir=self.static_root / "dist")
        self.addCleanup(server.server_close)
        start = time.monotonic()
        server._last_activity = start
        self.assertIsNone(server.exit_reason(60.0, now=start + 59))
        self.assertEqual(server.exit_reason(60.0, now=start + 61), "idle")
        self.assertTrue(server.open_stream())
        self.assertEqual(server.idle_seconds(now=start + 10_000), 0.0, "an open page keeps the server up")
        self.assertIsNone(server.exit_reason(60.0, now=start + 10_000))
        server.close_stream()
        self.assertLess(server.idle_seconds(), 1.0, "closing a stream counts as activity")
        features.set_layer(self.ts.session, False, "human", "cli")
        self.assertEqual(server.exit_reason(60.0), "disabled")
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        server.pending_signal = "stopped"
        self.assertEqual(server.exit_reason(60.0), "stopped")
        self.assertTrue(server.stop_event.is_set())

    def test_page_sessions_are_capped(self):
        self.enable()
        server = W.make_server(self.layout, self.ts.env, static_dir=self.static_root / "dist")
        self.addCleanup(server.server_close)
        with mock.patch.object(W, "MAX_SESSIONS", 3):
            tokens = [server.new_session(True, "human")[0] for _ in range(5)]
        self.assertIsNone(server.session_for(tokens[0]), "the oldest session is dropped")
        self.assertIsNone(server.session_for(tokens[1]))
        self.assertTrue(server.session_for(tokens[4])["writable"])

    def test_the_server_binds_loopback_only(self):
        self.enable()
        server = W.make_server(self.layout, self.ts.env, static_dir=self.static_root / "dist")
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertGreater(server.port, 0)


class ServeTests(LifecycleCase):
    def run_serve(self, env: Dict[str, str], idle: float = 30.0) -> Tuple[threading.Thread, List[int]]:
        result: List[int] = []
        thread = threading.Thread(target=lambda: result.append(W.serve(self.layout, env, idle_timeout_s=idle, api=object())), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return thread, result

    def wait_for_state(self) -> Dict[str, Any]:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rec = W.status(self.layout)
            if rec is not None:
                return rec
            time.sleep(0.02)
        self.fail("whiteboard.json never appeared")

    def test_serve_refuses_while_the_layer_is_off(self):
        self.assertEqual(W.serve(self.layout, self.ts.env, api=object()), 1)
        self.assertIsNone(W.status(self.layout))
        self.assertIn("layer is off", self.log[-1])

    def test_serve_records_itself_and_exits_when_idle(self):
        self.enable()
        env = dict(self.ts.env, **{W.ENV_WRITABLE: "1", W.ENV_STARTED_BY: json.dumps({"name": "human", "via": "console", "verified": True})})
        thread, result = self.run_serve(env, idle=0.4)
        rec = self.wait_for_state()
        self.assertEqual(rec["v"], 1)
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["start_time"], "T0")
        self.assertTrue(rec["writable"])
        self.assertEqual(rec["started_by"], {"name": "human", "via": "console", "verified": True})
        self.assertIn("beat_at", rec)
        self.assertIn("version", rec)
        self.assertEqual(stat.S_IMODE(os.stat(W.state_path(self.ts.session)).st_mode), 0o600)
        thread.join(5)
        self.assertEqual(result, [0])
        self.assertFalse(W.state_path(self.ts.session).exists(), "the record goes with the server")

    def test_serve_is_read_only_unless_told_otherwise_and_exits_when_disabled(self):
        self.enable()
        thread, result = self.run_serve(dict(self.ts.env))
        rec = self.wait_for_state()
        self.assertFalse(rec["writable"])
        self.assertEqual(rec["started_by"]["verified"], False)
        features.set_layer(self.ts.session, False, "human", "cli")
        thread.join(5)
        self.assertEqual(result, [0])
        self.assertIsNone(W.status(self.layout))

    def test_a_second_serve_refuses_while_one_runs(self):
        self.enable()
        store.write_json(W.state_path(self.ts.session), {"v": 1, "pid": 4242, "port": 9, "start_time": "T0"})
        with mock.patch.object(W, "_pid_alive", return_value=True):
            self.assertEqual(W.serve(self.layout, self.ts.env, api=object()), 1)


class StartStopTests(LifecycleCase):
    def fake_spawn(self) -> List[Tuple[List[str], Dict[str, str], Path]]:
        calls: List[Tuple[List[str], Dict[str, str], Path]] = []

        def spawn(argv: List[str], env: Dict[str, str], log_path: Path) -> int:
            calls.append((argv, env, log_path))
            thread = threading.Thread(target=W.serve, args=(self.layout, env), kwargs={"idle_timeout_s": 30.0, "api": object()}, daemon=True)
            thread.start()
            self.addCleanup(lambda: (W.stop(self.layout), thread.join(5)))
            return os.getpid()

        patcher = mock.patch.object(W, "SPAWN", spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_start_refuses_members_and_a_disabled_layer(self):
        calls = self.fake_spawn()
        with self.assertRaises(HerdrTeamError) as ctx:
            W.start(self.layout, self.ts.env, operator())
        self.assertEqual(ctx.exception.code, "whiteboard_off")
        self.enable()
        with self.assertRaises(HerdrTeamError) as ctx:
            W.start(self.layout, self.ts.env, member())
        self.assertEqual(ctx.exception.code, "author_mismatch")
        self.assertEqual(calls, [])

    def test_start_spawns_once_and_mints_a_ticket_per_open(self):
        self.enable()
        calls = self.fake_spawn()
        first = W.start(self.layout, self.ts.env, operator())
        self.assertEqual(set(first), {"url", "port", "pid", "writable", "started"})
        self.assertTrue(first["started"])
        self.assertTrue(first["writable"])
        self.assertEqual(first["pid"], os.getpid())
        argv, env, log = calls[0]
        self.assertEqual(argv[1:], ["--socket", os.fspath(self.layout.socket), "whiteboard-serve"])
        self.assertTrue(argv[0].endswith(os.path.join("bin", "herdr-synapse")))
        self.assertEqual(env[W.ENV_WRITABLE], "1")
        self.assertEqual(json.loads(env[W.ENV_STARTED_BY])["name"], "human")
        self.assertEqual(env["HERDR_TEAM_STATE_DIR"], os.fspath(self.layout.state_root.path))
        self.assertEqual(log, W.log_path(self.ts.session))
        second = W.start(self.layout, self.ts.env, operator())
        self.assertFalse(second["started"])
        self.assertEqual(second["port"], first["port"])
        self.assertNotEqual(second["url"], first["url"], "every open gets its own ticket")
        self.assertEqual(len(calls), 1)
        response = raw_request(first["port"], "GET", first["url"].split(str(first["port"]), 1)[1])
        self.assertEqual(response.status, 303)
        self.assertTrue(W.stop(self.layout))
        deadline = time.monotonic() + 3
        while W.status(self.layout) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(W.status(self.layout))

    def test_an_unverified_human_gets_a_read_only_page_and_the_operator_upgrades_it(self):
        self.enable()
        calls = self.fake_spawn()
        opened = W.start(self.layout, self.ts.env, unverified_human())
        self.assertFalse(opened["writable"])
        self.assertEqual(calls[0][1][W.ENV_WRITABLE], "0")
        upgraded = W.start(self.layout, self.ts.env, operator())
        self.assertTrue(upgraded["writable"])
        self.assertTrue(upgraded["started"], "a read-only server is replaced when the operator opens the page")
        self.assertEqual(len(calls), 2)

    def test_start_opens_the_browser_only_when_asked(self):
        self.enable()
        self.fake_spawn()
        opened: List[str] = []
        with mock.patch.object(W, "OPEN_BROWSER", side_effect=lambda url: opened.append(url)):
            quiet = W.start(self.layout, self.ts.env, operator())
            loud = W.start(self.layout, self.ts.env, operator(), open_browser=True)
        self.assertEqual(opened, [loud["url"]])
        self.assertNotIn(quiet["url"], opened)

    def test_a_server_that_never_comes_up_is_reported(self):
        self.enable()
        with mock.patch.object(W, "SPAWN", return_value=4242), mock.patch.object(W, "START_WAIT_S", 0.2):
            with self.assertRaises(HerdrTeamError) as ctx:
                W.start(self.layout, self.ts.env, operator())
        self.assertEqual(ctx.exception.code, "whiteboard_start_failed")
        self.assertIn("whiteboard.log", ctx.exception.message)

    def test_stop_signals_only_the_recorded_process_after_checking_its_start_time(self):
        path = W.state_path(self.ts.session)
        self.assertFalse(W.stop(self.layout), "nothing to stop")
        store.write_json(path, {"v": 1, "pid": 4242, "port": 9, "start_time": "T-reused"})
        kills: List[Tuple[int, int]] = []
        with mock.patch.object(W, "_pid_alive", return_value=True), mock.patch.object(W, "KILL", side_effect=lambda pid, sig: kills.append((pid, sig))):
            self.assertFalse(W.stop(self.layout), "a pid whose start time changed is someone else's")
            self.assertEqual(kills, [])
            self.assertFalse(path.exists())
            store.write_json(path, {"v": 1, "pid": 4242, "port": 9, "start_time": "T0"})
            with mock.patch.object(W, "STOP_WAIT_S", 0.1):
                self.assertTrue(W.stop(self.layout, "disabled"))
        self.assertEqual(kills, [(4242, signal.SIGTERM)])
        self.assertFalse(path.exists())

    def test_stop_passes_its_reason_to_the_server(self):
        path = W.state_path(self.ts.session)
        store.write_json(path, {"v": 1, "pid": 4242, "port": 9, "start_time": "T0"})
        seen: List[Optional[str]] = []

        def kill(pid: int, sig: int) -> None:
            seen.append(store.read_json(path).get("stop_reason"))

        with mock.patch.object(W, "_pid_alive", return_value=True), mock.patch.object(W, "KILL", side_effect=kill), \
                mock.patch.object(W, "STOP_WAIT_S", 0.05):
            W.stop(self.layout, "disabled")
        self.assertEqual(seen, ["disabled"])


# ---------------------------------------------------------------------------------------
# pure helpers


class HelperTests(unittest.TestCase):
    def test_viz_document(self):
        doc = W.viz_document({"html": "<canvas></canvas><script>go()</script>", "libs": ["p5", "evil", "d3"]}, 4321)
        self.assertTrue(doc.startswith('<!doctype html><meta charset="utf-8">'))
        base = "http://127.0.0.1:4321/viz-lib/"
        self.assertIn('<script src="{}synapse-viz.js"></script>'.format(base), doc)
        self.assertLess(doc.index("synapse-viz.js"), doc.index("d3.min.js"))
        self.assertLess(doc.index("d3.min.js"), doc.index("p5.min.js"))
        self.assertLess(doc.index("p5.min.js"), doc.index("<script>go()</script>"))
        self.assertNotIn("evil", doc)
        self.assertNotIn("importmap", doc, "no import map without three")
        with_three = W.viz_document({"html": "", "libs": ["three"]}, 1)
        self.assertIn('<script type="importmap">{"imports":{"three":"http://127.0.0.1:1/viz-lib/three.module.min.js"}}</script>', with_three)

    def test_http_status_mapping(self):
        self.assertEqual(W.http_status_for("whiteboard_off"), 403)
        self.assertEqual(W.http_status_for("team_not_found"), 404)
        self.assertEqual(W.http_status_for("canvas_rate"), 429)
        self.assertEqual(W.http_status_for("member_not_found"), 404)  # "Send to…" an unknown member
        self.assertEqual(W.http_status_for("brand_new_code"), 500)

    def test_mime_types(self):
        self.assertEqual(W.mime_type("a.vl.json"), "application/json")
        self.assertEqual(W.mime_type("x.woff2"), "font/woff2")
        self.assertEqual(W.mime_type("x.unknown"), "application/octet-stream")


# ---------------------------------------------------------------------------------------
# the built page


WEB = PLUGIN_ROOT / "web"
DIST = WEB / "dist"


def license_file_name(key: str) -> str:
    """``web/scripts/postbuild.mjs``'s naming: no leading ``@``, ``/`` -> ``__``, the version kept for duplicates."""
    return key.lstrip("@").replace("/", "__") + ".txt"


class BuiltPageTests(unittest.TestCase):
    def setUp(self) -> None:
        if not (DIST / "MANIFEST.json").is_file():
            self.fail("web/dist is not built: run npm ci && npm run build in web/")
        self.manifest = json.loads((DIST / "MANIFEST.json").read_text(encoding="utf-8"))

    def test_the_web_dist_is_the_manifest(self):
        listed = self.manifest["files"]
        on_disk = set()
        for root, _dirs, files in os.walk(DIST):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), DIST).replace(os.sep, "/")
                if rel not in ("MANIFEST.json", "manifest.sha256"):
                    on_disk.add(rel)
        self.assertEqual(set(listed), on_disk, "every file in web/dist is listed, and nothing listed is missing")
        for rel, digest in listed.items():
            self.assertEqual(hashlib.sha256((DIST / rel).read_bytes()).hexdigest(), digest, rel)
        lines = (DIST / "manifest.sha256").read_text(encoding="utf-8").splitlines()
        self.assertEqual({line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}, listed)

    def test_every_package_has_its_licence(self):
        packages = self.manifest["packages"]
        for required in ("@excalidraw/excalidraw", "react", "react-dom", "mermaid", "vega", "vega-lite", "vega-interpreter", "d3", "three", "p5"):
            self.assertIn(required, packages)
        for key in packages:
            path = DIST / "licenses" / license_file_name(key)
            self.assertTrue(path.is_file(), key)
            self.assertGreater(path.stat().st_size, 40, key)
        for name in ("THIRD_PARTY.txt", "fonts.txt"):
            self.assertTrue((DIST / "licenses" / name).is_file(), name)
        self.assertIn("SIL OPEN FONT LICENSE", (DIST / "licenses" / "fonts.txt").read_text(encoding="utf-8"))

    def test_the_page_loads_nothing_remote_and_no_inline_script(self):
        html = (DIST / "index.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S)
        self.assertTrue(scripts)
        for attrs, body in scripts:
            self.assertIn("src=", attrs, "the CSP forbids inline script")
            self.assertEqual(body.strip(), "")
        self.assertIsNone(re.search(r"(src|href)=[\"']?(https?:)?//", html), "no remote or protocol-relative loads")

    def test_viz_libraries_and_fonts_are_local(self):
        for name in list(W.VIZ_LIB_FILES.values()) + [W.VIZ_RUNTIME]:
            self.assertTrue((DIST / "viz-lib" / name).is_file(), name)
        self.assertTrue((DIST / "fonts" / "Excalifont").is_dir())
        self.assertFalse((DIST / "fonts" / "Xiaolai").exists(), "the 12 MB CJK fallback is left out")
        runtime = (DIST / "viz-lib" / W.VIZ_RUNTIME).read_text(encoding="utf-8")
        for message in ("synapse:ready", "synapse:beat", "synapse:still", "synapse:data", "synapse:pause", "synapse:resume", "synapse:capture"):
            self.assertIn(message, runtime)
        self.assertIn("event.source !== parentWindow", runtime, "the frame only listens to its parent")

    def test_versions_are_pinned_with_a_lock_file(self):
        package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies"):
            for name, version in package[section].items():
                self.assertRegex(version, r"^\d+\.\d+\.\d+$", "{} is pinned exactly".format(name))
        self.assertTrue(package["dependencies"]["@excalidraw/excalidraw"].startswith("0.18."))
        self.assertTrue((WEB / "package-lock.json").is_file())
        self.assertIn("node_modules/", (WEB / ".gitignore").read_text(encoding="utf-8").split())
        self.assertEqual(self.manifest["packages"]["@excalidraw/excalidraw"], package["dependencies"]["@excalidraw/excalidraw"])

    def test_the_server_serves_the_real_build(self):
        with TempState() as ts:
            whiteboard_on(ts.session, ts.team, via="console")
            server = W.make_server(ts.layout, ts.env, static_dir=DIST)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                url = W.mint_ticket(ts.layout, True, "human", port=server.port)
                cookie = raw_request(server.port, "GET", url.split(str(server.port), 1)[1]).cookies[0].split(";", 1)[0]
                page = raw_request(server.port, "GET", "/", {"Cookie": cookie})
                self.assertEqual(page.status, 200)
                for ref in re.findall(r'(?:src|href)="(/[^"]+)"', page.body.decode("utf-8")):
                    self.assertEqual(raw_request(server.port, "GET", ref, {"Cookie": cookie}).status, 200, ref)
                lib = raw_request(server.port, "GET", "/viz-lib/three.module.min.js")
                self.assertEqual((lib.status, lib.header("Content-Type")), (200, "text/javascript; charset=utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()

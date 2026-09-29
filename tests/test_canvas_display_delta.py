"""The display list over HTTP (canvas v2 phase 1, 3.2 to 3.4): deltas, ``GET /display``, ``POST /measure`` and the engine
redirect (T-S1 to T-S6 of ``.local/prd/canvas-v2-phase1.md`` 5.1)."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import TempState, whiteboard_on
from test_canvas import OPERATOR, WORKER, CanvasRig
from test_whiteboard_server import make_static, raw_request

from herdr_team import activity
from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team import canvas_text as X
from herdr_team import views
from herdr_team import whiteboard_server as W


def applied(old, delta):
    """``old`` with a delta applied the way the page applies it (1.1)."""
    if delta.get("full"):
        return delta
    entries = {e["id"]: e for e in old["entries"] if e["id"] not in delta["removes"]}
    entries.update({e["id"]: e for e in delta["upserts"]})
    return dict(old, version=delta["version"], bbox=delta["bbox"], entries=sorted(entries.values(), key=D.order_key))


class Deltas(CanvasRig):
    def setUp(self):
        super().setUp()
        C._DISPLAY_CACHE.clear()
        self.addCleanup(C._DISPLAY_CACHE.clear)
        self.ids = [self.ok({"op": "shape", "kind": "box", "text": "Box {}".format(i), "at": [i * 240, 0], "intent": "t"})["ids"][0]
                    for i in range(4)]

    def test_a_delta_is_the_full_list_with_the_same_entries_replaced(self):
        before = C.display(self.team)
        a, b, c, _d = self.ids
        self.apply([{"op": "move", "id": a, "by": [0, 200], "intent": "t"}, {"op": "edit", "id": b, "text": "Box two, renamed longer", "intent": "t"},
                    {"op": "delete", "id": c, "intent": "t"}, {"op": "shape", "kind": "note", "text": "new", "at": [0, 600], "intent": "t"}])
        delta = C.display_delta(self.team, before["version"])
        self.assertFalse(delta["full"])
        # The four boxes, one per batch, share one automatic claim (QA phase 5 L8), so the note's new claim releases none.
        self.assertEqual(delta["removes"], [c])
        self.assertIn(a, [e["id"] for e in delta["upserts"]])
        fresh = D.display_list(C.load_scene(self.team), stills=set())
        patched = applied(before, delta)
        self.assertEqual(D.dumps(patched["entries"]), D.dumps(fresh["entries"]))
        self.assertEqual(patched["bbox"], fresh["bbox"])
        self.assertEqual(D.dumps(C.display(self.team)), D.dumps(fresh), "the cached list was patched forward, not rebuilt wrong")

    def test_a_claim_beside_another_stops_its_label_and_a_delta_redraws_both(self):
        # QA phase 5 L8: a claim's label is a fixed screen size from its top-left, so zoomed out it ran over the label of
        # the claim beside it. It now stops where that claim begins; a claim that comes or goes redraws the others.
        self.ok({"op": "claim", "region": [0, 400, 400, 700], "label": "the pricing table and all of its footnotes", "intent": "t"})
        self.ok({"op": "claim", "region": [0, 3000, 100, 3100], "label": "far away"}, OPERATOR)  # a new author sends the whole list
        before = C.display(self.team)
        clip_of = lambda doc, cid: next(i.get("clip") for e in doc["entries"] if e["id"] == cid for i in e["items"] if i["k"] == "group")
        mine = next(e["id"] for e in before["entries"] if e["kind"] == "claim" and e["bbox"] == [0, 400, 400, 700])
        self.assertIsNone(clip_of(before, mine), "alone, its label runs on")
        other = self.ok({"op": "claim", "region": [420, 380, 800, 800], "label": "the funnel", "intent": "t"}, OPERATOR)["ids"][0]
        delta = C.display_delta(self.team, before["version"])
        self.assertFalse(delta["full"])
        self.assertIn(mine, [e["id"] for e in delta["upserts"]])
        patched = applied(before, delta)
        self.assertEqual(clip_of(patched, mine), [0, 400, 420 - D.CLAIM_LABEL_GAP, 300])
        self.assertIsNone(clip_of(patched, other), "nothing beside it on the right")
        self.assertEqual(D.dumps(patched["entries"]), D.dumps(D.display_list(C.load_scene(self.team), stills=set())["entries"]))
        # A claim below it, not beside it, stops nothing; one that goes lets the label run on again.
        version = C.display(self.team)["version"]
        self.ok({"op": "release", "id": other}, OPERATOR)
        patched = applied(patched, C.display_delta(self.team, version))
        self.assertIsNone(clip_of(patched, mine))
        self.ok({"op": "claim", "region": [420, 900, 800, 1000], "label": "below", "intent": "t"}, OPERATOR)
        self.assertIsNone(clip_of(C.display(self.team), mine))

    def test_a_claims_label_clears_its_own_dashed_rectangle(self):
        """QA phase 6, 3.6 saw a claim's label struck through by the claim's own dashes on the page. The list both
        renderers draw keeps them apart, and this is where that clearance lives: the label hangs from the top-left
        corner in screen pixels, so the rectangle's 2 px stroke ends 1 px below the corner while the first line's ink
        starts lower. Both distances are screen pixels, so the gap is the same at every zoom. Since V2 the border is
        drawn ``CLAIM_BORDER_OUT`` units outside the region as well, so its stroke lands on nothing the region holds;
        the label still hangs from the region's own corner, which is where the authority begins."""
        self.ok({"op": "claim", "region": [0, 400, 400, 700], "label": "the pricing table", "intent": "t"})
        entry = next(e for e in C.display(self.team)["entries"] if e["kind"] == "claim" and e["bbox"] == [0, 400, 400, 700])
        rect = next(i for i in entry["items"] if i["k"] == "rect")
        group = next(i for i in entry["items"] if i["k"] == "group")
        self.assertEqual((rect["dash"], group["screen"]), ([8, 6], [0, 400]))
        self.assertEqual([rect["x"], rect["y"], rect["w"], rect["h"]],
                         [-D.CLAIM_BORDER_OUT, 400 - D.CLAIM_BORDER_OUT, 400 + 2 * D.CLAIM_BORDER_OUT, 300 + 2 * D.CLAIM_BORDER_OUT],
                         "the dashed border is drawn outside the region it holds (V2)")
        text = group["items"][0]
        face = X.face("normal", text["weight"])
        ink_top = text["lines"][0]["y"] - face.ascender / float(face.units_per_em) * text["size"]
        self.assertGreaterEqual(ink_top, rect["sw_px"] / 2.0 + 1.0,
                                "the label's first line runs into the dashes along the claim's top edge")
        # The other edges are world units, so how far the label reaches into the region depends on the zoom: a
        # region drawn shorter than one line (about 17 px) would meet its bottom edge, which the page clips sideways
        # (CLAIM_LABEL_GAP) and nobody has seen vertically.

    def test_no_change_is_an_empty_delta(self):
        version = C.display(self.team)["version"]
        delta = C.display_delta(self.team, version)
        self.assertEqual((delta["full"], delta["upserts"], delta["removes"]), (False, [], []))

    def test_a_reset_a_lock_an_author_or_too_many_ids_send_the_whole_list(self):
        version = C.display(self.team)["version"]
        self.assertTrue(C.display_delta(self.team, version + 5)["full"], "since ahead of the canvas")
        self.apply([{"op": "lock", "region": [3000, 3000, 3100, 3100], "label": "mine"}], OPERATOR)
        self.assertTrue(C.display_delta(self.team, version)["full"], "a lock changes locked on other entries")
        version = C.display(self.team)["version"]
        with mock.patch.object(C, "MAX_DELTA_IDS", 1):
            self.apply([{"op": "move", "ids": self.ids[:3], "by": [0, 10], "intent": "t"}])
            self.assertTrue(C.display_delta(self.team, version)["full"])
        version = C.display(self.team)["version"]
        C.clear(self.layout, self.team, "human")
        found = C.display_delta(self.team, version)
        self.assertTrue(found["full"])
        self.assertEqual(found["entries"], [])

    def test_the_full_list_is_cached_by_version_and_corrections(self):
        first = C.display(self.team)
        self.assertIs(C.display(self.team), first)
        C.store.write_json(C._file(self.team, C.MEASURE_FILE), {"v": 1, "metrics": C.metrics_sha(), "em": {"sans-500|Box 1": 5.0}})
        self.assertIsNot(C.display(self.team), first, "new corrections are a new list")


class RouteCase(unittest.TestCase):
    """A running page server over the real canvas, and a signed-in writable page."""

    writable = True

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout
        whiteboard_on(self.ts.session, self.ts.team, via="console")
        C._DISPLAY_CACHE.clear()
        for patcher in (mock.patch.object(views, "team_views", return_value={}), mock.patch.object(activity, "cards", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        static_root = Path(tempfile.mkdtemp(prefix="wb-static-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(static_root, ignore_errors=True))
        self.server = W.make_server(self.layout, self.ts.env, static_dir=make_static(static_root), writable=True)
        self.port = self.server.port
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.request_stop("stopped"), self.server.shutdown(), self.server.server_close(), thread.join(5)))
        self.cookie, self.csrf = self.sign_in(self.writable)
        result = C.apply_ops(self.layout, self.ts.team, [{"op": "shape", "kind": "box", "text": "Checkout API", "at": [0, 0], "intent": "t"},
                                                        {"op": "shape", "kind": "box", "text": "Orders", "at": [400, 0], "intent": "t"}], WORKER)
        self.box = result["applied"][0]["ids"][0]

    def sign_in(self, writable):
        url = W.mint_ticket(self.layout, writable, "human", port=self.port)
        response = raw_request(self.port, "GET", url.split(str(self.port), 1)[1])
        cookie = response.cookies[0].split(";", 1)[0]
        return cookie, raw_request(self.port, "GET", "/api/session", {"Cookie": cookie}).json()["csrf"]

    def get(self, path, cookie=None):
        return raw_request(self.port, "GET", path, {"Cookie": cookie or self.cookie})

    def post(self, path, obj, cookie=None, csrf=None):
        return raw_request(self.port, "POST", path, {"Cookie": cookie or self.cookie, "Origin": "http://127.0.0.1:{}".format(self.port),
                                                     W.CSRF_HEADER: csrf or self.csrf, "Content-Type": "application/json"},
                           json.dumps(obj).encode("utf-8"))


class DisplayRoute(RouteCase):
    def test_the_page_reads_the_list_and_its_deltas(self):
        response = self.get("/api/teams/alpha/display")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("cache-control"), "no-store")
        doc = response.json()
        self.assertEqual((doc["dl"], len(doc["entries"])), (1, 3))  # the box, the pin, and the member's automatic claim (phase 5)
        C.apply_ops(self.layout, self.ts.team, [{"op": "move", "id": self.box, "by": [0, 40], "intent": "t"}], WORKER)
        delta = self.get("/api/teams/alpha/display?since={}".format(doc["version"])).json()
        self.assertEqual((delta["full"], [e["id"] for e in delta["upserts"]]), (False, [self.box]))
        self.assertEqual(self.get("/api/teams/alpha/display?since=0").json()["version"], delta["version"])
        self.assertEqual(self.get("/api/teams/alpha/display?since=-1").status, 400)

    def test_it_needs_a_session_and_the_right_host_and_a_read_only_page_may_read(self):
        self.assertEqual(raw_request(self.port, "GET", "/api/teams/alpha/display").status, 401)
        self.assertEqual(raw_request(self.port, "GET", "/api/teams/alpha/display", {"Cookie": self.cookie}, host="evil.example").status, 403)
        cookie, _csrf = self.sign_in(False)
        self.assertEqual(self.get("/api/teams/alpha/display", cookie).status, 200)
        self.assertEqual(raw_request(self.port, "POST", "/api/teams/alpha/display", {"Cookie": self.cookie}).status, 403, "no Origin")


class MeasureRoute(RouteCase):
    def line(self, **extra):
        entry = next(e for e in C.display(self.ts.team)["entries"] if e["id"] == self.box)
        text = next(p for p in entry["items"] if p["k"] == "text")
        found = {"id": self.box, "font": text["font"], "weight": text["weight"], "size": text["size"], "t": text["lines"][0]["t"],
                 "w": text["lines"][0]["w"]}
        found.update(extra)
        return found

    def report(self, lines, **extra):
        body = {"version": C.current_version(self.ts.team), "metrics": C.metrics_sha(), "lines": lines}
        body.update(extra)
        with mock.patch.object(W, "MEASURE_INTERVAL_S", 0.0):
            return self.post("/api/teams/alpha/measure", body)

    def test_a_wider_line_is_kept_and_its_box_refitted(self):
        before = next(e for e in C.load_scene(self.ts.team)["elements"] if e["id"] == self.box)
        wide = self.line()
        wide["w"] = wide["w"] * 1.8
        response = self.report([wide])
        self.assertEqual(response.status, 200, response.body)
        result = response.json()
        self.assertEqual((result["accepted"], result["ignored"], result["refit"]), (1, [], [self.box]))
        self.assertTrue(result["batch"])
        after = next(e for e in C.load_scene(self.ts.team)["elements"] if e["id"] == self.box)
        self.assertGreater(after["w"], before["w"], "a box hugs its label: it grows to the width the page measured")
        drawn = next(p for p in C.display(self.ts.team)["entries"] if p["id"] == self.box)["items"][-1]
        self.assertEqual([line["t"] for line in drawn["lines"]], ["Checkout API"])
        self.assertGreaterEqual(drawn["lines"][0]["w"] + 0.01, wide["w"], "the display list carries the corrected width (to 2 decimals)")
        self.assertLessEqual(drawn["lines"][0]["w"], drawn["box"][2] + 0.5, "and the line fits its room again")
        table, _token = C.corrections(self.ts.team)
        self.assertEqual(list(table), [X.correction_key("sans", 500, "Checkout API")])
        events = [json.loads(line) for line in C._file(self.ts.team, C.EVENTS_FILE).read_text().splitlines()]
        self.assertEqual((events[-1]["op"], events[-1]["intent"], events[-1]["author"]["name"]), ("refit", C.MEASURE_INTENT, "human"))

    def test_every_reason_a_line_is_ignored(self):
        good = self.line()
        lines = [dict(good, t="not on this box"), dict(good), dict(good, w=good["w"] * 9), "junk", dict(good, id="E-999")]
        result = self.report(lines).json()
        self.assertEqual(result["accepted"], 0)
        self.assertEqual([(i["i"], i["why"]) for i in result["ignored"]],
                         [(0, "not_a_line"), (1, "not_wider"), (2, "implausible"), (3, "not_a_line"), (4, "stale")])
        stale = self.report([dict(good, w=good["w"] * 2)], metrics="0" * 64).json()
        self.assertEqual(stale["ignored"], [{"i": 0, "why": "stale"}], "measured against other fonts")
        old = self.report([dict(good, w=good["w"] * 2)], version=0).json()
        self.assertEqual(old["ignored"], [{"i": 0, "why": "stale"}], "the box changed after the page measured it")

    def test_repeated_reports_never_compound_past_the_ratio_of_the_metrics_width(self):
        # Phase 1 QA finding 5: each report 3.9x the width the list then carried took a line from 72 to 4300 units.
        def metrics_width(line):
            with C._ctext.corrected(None):
                return X.measure(line["t"], line["font"], line["size"], line["weight"]).width

        results = []
        for _ in range(6):
            current = self.line()
            results.append(self.report([dict(current, w=current["w"] * 3.9)]).json())
        self.assertEqual(results[0]["accepted"], 1, results[0])
        self.assertIn({"i": 0, "why": "implausible"}, [i for r in results[1:] for i in r["ignored"]], results)
        # The box may rewrap its label as it grows, so a later report can be about a new line; none compounds.
        table, _token = C.corrections(self.ts.team)
        for stored_key, em in table.items():
            text = stored_key.split("|", 1)[1]
            with C._ctext.corrected(None):
                metrics_em = X.measure(text, "sans", 1, 500).width
            self.assertLessEqual(em, C.MEASURE_MAX_RATIO * metrics_em + 1e-5, stored_key)
        drawn = self.line()
        self.assertLessEqual(drawn["w"], C.MEASURE_MAX_RATIO * metrics_width(drawn) + 0.01, "never more than the ratio times the metrics' width")

    def test_a_read_only_page_cannot_report_and_a_second_report_waits(self):
        cookie, csrf = self.sign_in(False)
        self.assertEqual(self.post("/api/teams/alpha/measure", {"version": 1, "metrics": "", "lines": []}, cookie, csrf).status, 403)
        first = self.post("/api/teams/alpha/measure", {"version": 1, "metrics": "", "lines": []})
        self.assertEqual(first.status, 200)
        second = self.post("/api/teams/alpha/measure", {"version": 1, "metrics": "", "lines": []})
        self.assertEqual(second.status, 429)
        self.assertGreaterEqual(second.json()["retry_after"], 1)
        self.assertEqual(self.post("/api/teams/alpha/measure", {"lines": [{}] * (C.MAX_MEASURE_LINES + 1), "version": 1}).status, 429)

    def test_the_table_is_capped_and_other_metrics_empty_it(self):
        C.store.write_json(C._file(self.ts.team, C.MEASURE_FILE), {"v": 1, "metrics": C.metrics_sha(), "em": {"sans-500|a": 9.0, "sans-500|b": 9.0}})
        with mock.patch.object(C, "MAX_CORRECTIONS", 2):
            good = self.line()
            self.assertEqual(self.report([dict(good, w=good["w"] * 1.5)]).json()["accepted"], 1)
        table, _token = C.corrections(self.ts.team)
        self.assertEqual(list(table), ["sans-500|b", X.correction_key("sans", 500, "Checkout API")], "the oldest went first")
        C.store.write_json(C._file(self.ts.team, C.MEASURE_FILE), {"v": 1, "metrics": "f" * 64, "em": {"sans-500|a": 9.0}})
        self.assertEqual(C.corrections(self.ts.team), ({}, ""), "measured against other metrics: no corrections")


class RebindingAnArrowEnd(CanvasRig):
    def test_letting_go_of_an_end_keeps_the_points_it_is_given(self):
        # T-S5: the page's "drop an arrow's end on empty canvas" is move {to_element: null, points}.
        a = self.ok({"op": "shape", "kind": "box", "text": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "kind": "box", "text": "B", "at": [400, 0], "intent": "t"})["ids"][0]
        arrow = self.ok({"op": "arrow", "from": a, "to": b, "intent": "t"})["ids"][0]
        start = self.el(arrow)["points"][0]
        self.ok({"op": "move", "id": arrow, "to_element": None, "points": [start, [300, 300]], "intent": "t"})
        moved = self.el(arrow)
        self.assertEqual((moved["from"], moved["to"]), (a, None))
        self.assertEqual(moved["points"][-1], [300, 300])
        self.ok({"op": "move", "id": arrow, "to_element": b, "intent": "t"})
        self.assertEqual(self.el(arrow)["to"], b)


class EngineRedirect(RouteCase):
    def test_the_ticket_redirect_keeps_the_engine_and_nothing_else(self):
        for query, location in (("&engine=v2", "/?engine=v2"), ("&engine=v1", "/?engine=v1"), ("&engine=v3", "/"), ("&x=1", "/"),
                                ("&engine=v2%26x%3D1", "/")):
            with self.subTest(query=query):
                url = W.mint_ticket(self.layout, True, "human", port=self.port) + query
                response = raw_request(self.port, "GET", url.split(str(self.port), 1)[1])
                self.assertEqual((response.status, response.header("location")), (303, location))


if __name__ == "__main__":
    unittest.main()

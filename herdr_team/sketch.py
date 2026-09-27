"""Synapse Sketch helper for agents' scripts: build canvas operations in Python (0.21).

Contract ``.local/prd/canvas-contracts.md`` section 10.1. This file is
standalone on purpose: standard library only and no ``herdr_team`` imports,
so an agent can import it from the directory ``herdr-synapse canvas helper``
prints, or copy it next to its own script::

    from sketch import Sketch
    s = Sketch(default_intent="map the churn drivers")
    s.claim("c10r4:c40r22", "mapping churn drivers")
    f = s.frame("Churn drivers", at="c10r4", w=600, h=360, id="drivers")
    price = s.shape("note", "Price rise in March", inside=f, id="price")
    onboard = s.shape("note", "Slow onboarding", right_of=price, gap=60)
    s.arrow(price, onboard, label="worsens")
    s.pen(s.circle_points(300, 170, 90, n=24), closed=True, color="red", intent="circle the main driver")
    s.print()          # {"ops": [...]} on stdout: python3 plan.py | herdr-synapse canvas draw --file -
    result = s.send()  # or run herdr-synapse canvas draw --file - --json and get its parsed result

Style keywords pass through to the op: colour by meaning with ``tone=``
(``neutral``, ``info``, ``success``, ``warning``, ``danger``, ``accent``,
``idea``, ``decision``) and ``variant=`` (``soft``, ``solid``, ``outline``),
or ``color=``/``fill=`` for a named or hex colour; ``w=``/``h=`` on a shape
are its minimum size, since shapes grow to fit their label (0.22)::

    s.shape("box", "Payments API", tone="info", w=160, h=80)
    s.restyle(price, tone="danger")

Every method appends one operation and returns what later calls can refer to:
the op's alias for anything it creates (the ``id`` you gave, or a generated
one), the target id for edits. Generated aliases carry a per-run tag
(``s3f9.1``, ``s3f9.2`` ...) so running the same script twice does not
collide with the aliases of your first run. The server does the validation;
this helper only builds JSON.
"""
from __future__ import annotations

import json
import math
import random
import string
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

GRID = 20
Point = Union[str, Sequence[float]]


class Sketch:
    """Collects operations; every method appends one op and returns its alias."""

    def __init__(self, default_intent: Optional[str] = None, prefix: Optional[str] = None) -> None:
        self.default_intent = default_intent
        self.ops: List[Dict[str, Any]] = []
        self.atomic = False
        tag = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
        self.prefix = prefix if prefix is not None else "s{}.".format(tag)
        self._count = 0

    # --------------------------------------------------------------- places

    @staticmethod
    def cell(col: int, row: int) -> str:
        """The grid point ``c<col>r<row>`` (its top-left corner; one cell is 20 units)."""
        return "c{}r{}".format(int(col), int(row))

    @staticmethod
    def point(x: float, y: float) -> List[float]:
        return [round(float(x), 2), round(float(y), 2)]

    @staticmethod
    def region(a: Point, b: Point) -> str:
        """A region from two corners: cells (``"c10r4"``) or points (``[x, y]``)."""
        def text(corner: Point) -> str:
            if isinstance(corner, str):
                return corner
            return "c{}r{}".format(int(math.floor(float(corner[0]) / GRID)), int(math.floor(float(corner[1]) / GRID)))
        if isinstance(a, str) and isinstance(b, str):
            return "{}:{}".format(a, b)
        if not isinstance(a, str) and not isinstance(b, str):
            return "{},{},{},{}".format(float(a[0]), float(a[1]), float(b[0]), float(b[1]))
        return "{}:{}".format(text(a), text(b))

    @staticmethod
    def circle_points(cx: float, cy: float, r: float, n: int = 24) -> List[List[float]]:
        """``n`` points around a circle, closed (the last point repeats the first)."""
        n = max(3, int(n))
        pts = [[round(cx + r * math.cos(2 * math.pi * k / n), 2), round(cy + r * math.sin(2 * math.pi * k / n), 2)] for k in range(n)]
        return pts + [list(pts[0])]

    @staticmethod
    def polyline(fn: Callable[[float], float], x0: float, x1: float, n: int = 50) -> List[List[float]]:
        """Sample ``y = fn(x)`` at ``n`` points from ``x0`` to ``x1`` (y grows downward on the canvas)."""
        n = max(2, int(n))
        return [[round(x0 + (x1 - x0) * k / (n - 1), 2), round(float(fn(x0 + (x1 - x0) * k / (n - 1))), 2)] for k in range(n)]

    @staticmethod
    def spiral_points(cx: float, cy: float, r0: float, r1: float, turns: float = 3.0, n: int = 120) -> List[List[float]]:
        """An Archimedean spiral from radius ``r0`` to ``r1``."""
        n = max(2, int(n))
        out = []
        for k in range(n):
            t = k / (n - 1)
            angle = 2 * math.pi * turns * t
            radius = r0 + (r1 - r0) * t
            out.append([round(cx + radius * math.cos(angle), 2), round(cy + radius * math.sin(angle), 2)])
        return out

    # ---------------------------------------------------------------- core

    def _alias(self, given: Optional[str]) -> str:
        if given:
            return given
        self._count += 1
        return "{}{}".format(self.prefix, self._count)

    def add(self, op: Dict[str, Any], derived_intent: Optional[str] = None) -> Dict[str, Any]:
        """Append a raw operation (fills ``intent`` from ``default_intent`` or the op's own text)."""
        op = {key: value for key, value in op.items() if value is not None}
        if not op.get("intent"):
            intent = self.default_intent or derived_intent
            if intent:
                op["intent"] = " ".join(str(intent).split())[:200]
        self.ops.append(op)
        return op

    def _create(self, op: Dict[str, Any], id: Optional[str], derived_intent: Optional[str], kw: Dict[str, Any]) -> str:
        alias = self._alias(id)
        op = dict(op, id=alias)
        op.update(kw)
        self.add(op, derived_intent)
        return alias

    # ----------------------------------------------------------- drawings

    def shape(self, kind: str, text: str = "", id: Optional[str] = None, **kw: Any) -> str:
        """``kind`` box, ellipse, diamond, note or text; placement: at, right_of, left_of, below, above, inside (+ gap);
        ``tone=``/``variant=`` colour it by meaning; ``w=``/``h=`` are its minimum (it grows to fit ``text``)."""
        return self._create({"op": "shape", "kind": kind, "text": text}, id, text, kw)

    def frame(self, title: str = "", id: Optional[str] = None, **kw: Any) -> str:
        """A frame: at + w + h, or children=[...], or region=..."""
        return self._create({"op": "frame", "title": title}, id, title, kw)

    def arrow(self, frm: Point, to: Point, label: Optional[str] = None, id: Optional[str] = None, **kw: Any) -> str:
        """An arrow between two elements (ids or aliases) or points."""
        return self._create({"op": "arrow", "from": frm, "to": to, "label": label}, id, label, kw)

    def pen(self, points: Sequence[Point], closed: bool = False, id: Optional[str] = None, **kw: Any) -> str:
        """A freehand stroke through cells, ``"x,y"``, ``[x, y]`` or ``[x, y, pressure]`` (at most 500 points)."""
        return self._create({"op": "pen", "points": [p if isinstance(p, str) else list(p) for p in points], "closed": bool(closed)}, id, None, kw)

    def path(self, d: str, id: Optional[str] = None, **kw: Any) -> str:
        return self._create({"op": "path", "d": d}, id, None, kw)

    def svg(self, markup: str, id: Optional[str] = None, **kw: Any) -> str:
        return self._create({"op": "svg", "svg": markup}, id, kw.get("title"), kw)

    def graph(self, nodes: Sequence[Any], edges: Sequence[Any] = (), id: Optional[str] = None, **kw: Any) -> str:
        """Nodes as ``{"id", "text"}`` (or bare ids), edges as ``{"from", "to", "label"}`` (or pairs); laid out by the server."""
        node_list = [n if isinstance(n, dict) else {"id": str(n)} for n in nodes]
        edge_list = [e if isinstance(e, dict) else {"from": e[0], "to": e[1], "label": e[2] if len(e) > 2 else None} for e in edges]
        edge_list = [{k: v for k, v in e.items() if v is not None} for e in edge_list]
        return self._create({"op": "graph", "nodes": node_list, "edges": edge_list}, id, kw.get("title"), kw)

    def mermaid(self, source: str, id: Optional[str] = None, **kw: Any) -> str:
        return self._create({"op": "mermaid", "source": source}, id, kw.get("title"), kw)

    def chart(self, spec: Dict[str, Any], id: Optional[str] = None, **kw: Any) -> str:
        """A Vega-Lite spec; ``data="churn.csv"`` names a file under the team's artifacts/ (no URLs in the spec)."""
        return self._create({"op": "chart", "spec": spec}, id, kw.get("title"), kw)

    def viz(self, html: str, title: str, id: Optional[str] = None, **kw: Any) -> str:
        """A live visual: HTML and JavaScript run sealed on the page; ``libs=["d3"]``; ``data=`` inline JSON."""
        return self._create({"op": "viz", "html": html, "title": title}, id, title, kw)

    def image(self, path: str, id: Optional[str] = None, **kw: Any) -> str:
        return self._create({"op": "image", "path": path}, id, None, kw)

    # ------------------------------------------------------ working together

    def comment(self, at: Point, text: str, mentions: Optional[Sequence[str]] = None, **kw: Any) -> Point:
        self.add(dict({"op": "comment", "at": at, "text": text, "mentions": list(mentions) if mentions else None}, **kw), text)
        return at

    def claim(self, region: Any, label: str, **kw: Any) -> Any:
        self.add(dict({"op": "claim", "region": region, "label": label}, **kw), label)
        return region

    def release(self, id: Optional[str] = None, **kw: Any) -> Optional[str]:
        self.add(dict({"op": "release", "id": id}, **kw), "release {}".format(id) if id else "release my claims")
        return id

    def legend(self, symbol: str, meaning: str, **kw: Any) -> str:
        self.add(dict({"op": "legend", "symbol": symbol, "meaning": meaning}, **kw), "{} = {}".format(symbol, meaning))
        return symbol

    def portrait(self, steps: Sequence[Any], current: Optional[int] = None, title: Optional[str] = None, **kw: Any) -> None:
        self.add(dict({"op": "portrait", "steps": list(steps), "current": current, "title": title}, **kw), "my current plan")

    # ------------------------------------------------------------- editing

    def _edit(self, op: str, id: Union[str, Sequence[str]], fields: Dict[str, Any]) -> Union[str, Sequence[str]]:
        target = {"id": id} if isinstance(id, str) else {"ids": list(id)}
        self.add(dict({"op": op}, **target, **fields), "{} {}".format(op, id if isinstance(id, str) else ", ".join(id)))
        return id

    def move(self, id: Union[str, Sequence[str]], **kw: Any) -> Union[str, Sequence[str]]:
        """``to=`` a point, ``by=[dx, dy]``, a relative placement, and/or ``w=``/``h=`` (a labelled shape's new minimum)."""
        return self._edit("move", id, kw)

    def restyle(self, id: Union[str, Sequence[str]], **kw: Any) -> Union[str, Sequence[str]]:
        """``tone=``, ``variant=``, ``color=``, ``fill=``, ``size=``, ``font=`` ... (a new size or font refits the label)."""
        return self._edit("restyle", id, kw)

    def edit(self, id: str, text: str, **kw: Any) -> str:
        self._edit("edit", id, dict(kw, text=text))
        return id

    def delete(self, id: Union[str, Sequence[str]], **kw: Any) -> Union[str, Sequence[str]]:
        return self._edit("delete", id, kw)

    # -------------------------------------------------------------- output

    def batch(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {"ops": list(self.ops)}
        if self.atomic:
            body["atomic"] = True
        return body

    def to_json(self) -> str:
        """The batch as ``{"ops": [...]}`` JSON."""
        return json.dumps(self.batch(), ensure_ascii=False)

    def print(self) -> None:
        sys.stdout.write(self.to_json() + "\n")
        sys.stdout.flush()

    def send(self, cli: str = "herdr-synapse", timeout: float = 60.0) -> Dict[str, Any]:
        """Run ``<cli> canvas draw --file - --json`` with this batch; its parsed result (or its error object)."""
        done = subprocess.run([cli, "canvas", "draw", "--file", "-", "--json"], input=self.to_json(), capture_output=True,
                              text=True, timeout=timeout, check=False)
        for stream in (done.stdout, done.stderr):
            for line in stream.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except ValueError:
                        continue
        return {"code": "no_output", "message": (done.stderr or done.stdout).strip()[:500], "exit": done.returncode}


if __name__ == "__main__":
    sys.stdout.write(__doc__ or "")

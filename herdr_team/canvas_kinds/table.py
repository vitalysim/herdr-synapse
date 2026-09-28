"""Tables (canvas v2 phase 2, 4.4): columns and rows held inline in one element, every cell addressable.

A table is one element (``type: "table"``) whose root holds its ``columns`` and
``rows`` (phase 2, D1): a 20x10 table costs one element, not two hundred. Each
column is as wide as its widest single-line cell (120 to 320) unless its
``width`` says otherwise; cells wrap at their column and keep ``max_lines`` (1
to 4, default 2), and a row is as tall as its tallest cell. Cells are parts
(``h.<column>`` for a header, ``<row>.<column>`` for a cell): the page edits
one with ``edit {id, part, text}``, which is a ``patch``.

Everything is drawn from the element itself (``geometry``), so the page, the
agent's picture and ``check`` agree on every cell.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote
from herdr_team.canvas_kinds.sdk import Block, Collection

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 137

_TOKENS = canvas_theme.section("table", {}) or {}
WIDTHS = dict(_TOKENS.get("widths") or {"s": 120, "m": 200, "l": 320})
MIN_W, MAX_W = float(_TOKENS.get("min_w", 120)), float(_TOKENS.get("max_w", 320))
ROW_MIN = float(_TOKENS.get("row_min", 40))
BAND = float(_TOKENS.get("band", 40))
SIZES = dict(_TOKENS.get("size") or {"s": 14, "m": 16})
PAD_X, PAD_Y = 12.0, 8.0
TITLE_SIZE, TITLE_WEIGHT = 16.0, 600
HEADER_WEIGHT, CELL_WEIGHT = 600, 400
ALIGNS = ("start", "center", "end")
MAX_ROWS, MAX_COLUMNS, MAX_CELLS, MAX_CELL_CHARS = 100, 16, 2000, 200
DEFAULT_LINES = int(_TOKENS.get("max_lines", 2))


# --------------------------------------------------------------------------
# items


def column_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """``"Risk"`` or ``{key, title, align, width}``; ``key`` defaults to ``c<n>``."""
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} is a title or {{key, title, align, width}}".format(field))
    for key in raw:
        if key not in ("key", "id", "title", "align", "width", "_gen"):
            raise ctx.invalid("{}.{}".format(field, key), "a column takes key, title, align, width")
    out: Dict[str, Any] = {"title": ctx.text(raw, "title", limit="label", one_line=True, label=field + ".title")}
    key = raw.get("key", raw.get("id"))
    if key is not None:
        if not isinstance(key, str) or not key.replace("_", "").replace("-", "").isalnum() or len(key) > 32:
            raise ctx.invalid(field + ".key", "a column key is 1 to 32 letters, digits, _ or -")
        out["id"] = key
    if raw.get("align") is not None:
        align = ctx.choice(raw, "align", ALIGNS, "start", label=field + ".align")
        if align != "start":
            out["align"] = align
    width = raw.get("width")
    if width is not None:
        if isinstance(width, str) and width in WIDTHS:
            out["width"] = width
        elif isinstance(width, (int, float)) and not isinstance(width, bool) and 80 <= width <= 600:
            out["width"] = int(width)
        else:
            raise ctx.invalid(field + ".width", "width is s, m, l or 80 to 600")
    if raw.get("_gen"):
        out["_gen"] = True
    return out


def row_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """``["a", "b"]`` or ``{id, cells: [..] | {key: text}, tone}``; ``id`` defaults to ``r<n>``."""
    if isinstance(raw, list):
        raw = {"cells": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} is a list of cells or {{id, cells, tone}}".format(field))
    for key in raw:
        if key not in ("id", "cells", "tone", "_gen"):
            raise ctx.invalid("{}.{}".format(field, key), "a row takes id, cells, tone")
    cells = raw.get("cells", [])
    out: Dict[str, Any] = {}
    if isinstance(cells, list):
        out["cells"] = [_cell(ctx, value, "{}.cells[{}]".format(field, index)) for index, value in enumerate(cells)]
    elif isinstance(cells, dict):
        out["cells"] = {str(key): _cell(ctx, value, "{}.cells.{}".format(field, key)) for key, value in cells.items()}
    else:
        raise ctx.invalid(field + ".cells", "cells is a list, or {column key: text}")
    if raw.get("id") is not None:
        if not isinstance(raw["id"], str) or not raw["id"].replace("_", "").replace("-", "").isalnum() or len(raw["id"]) > 32:
            raise ctx.invalid(field + ".id", "a row id is 1 to 32 letters, digits, _ or -")
        out["id"] = raw["id"]
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=field + ".tone")
    if raw.get("_gen"):
        out["_gen"] = True
    return out


def _cell(ctx: Any, value: Any, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = "{:g}".format(value)
    text = ctx.text({"v": value}, "v", limit="label", one_line=True, label=field)
    if len(text) > MAX_CELL_CHARS:
        raise ctx.too_big(field, "MAX_CELL_CHARS", MAX_CELL_CHARS, "a cell holds at most {} characters".format(MAX_CELL_CHARS))
    return text


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Settings with their defaults, and every row's cells as ``{column key: text}`` over the columns."""
    spec["title"] = ctx.text(spec, "title", limit="label", one_line=True)
    spec["header"] = ctx.boolean(spec, "header", True)
    spec["zebra"] = ctx.boolean(spec, "zebra", False)
    spec["max_lines"] = int(ctx.number(spec, "max_lines", 1, 4, DEFAULT_LINES))
    spec["size"] = ctx.choice(spec, "size", tuple(SIZES), "m")
    columns = spec.get("columns") or []
    rows = spec.get("rows") or []
    if not columns:
        raise ctx.invalid("columns", "a table needs columns: [\"Risk\", {\"title\": \"Owner\", \"key\": \"owner\"}]")
    if len(columns) > MAX_COLUMNS:
        raise ctx.too_big("columns", "MAX_COLUMNS", MAX_COLUMNS, "{} columns; the limit is {}".format(len(columns), MAX_COLUMNS))
    if len(rows) > MAX_ROWS:
        raise ctx.too_big("rows", "MAX_ROWS", MAX_ROWS, "{} rows; the limit is {}".format(len(rows), MAX_ROWS))
    if len(rows) * len(columns) > MAX_CELLS:
        raise ctx.too_big("rows", "MAX_CELLS", MAX_CELLS, "{} cells; the limit is {}".format(len(rows) * len(columns), MAX_CELLS))
    # Column keys first (generated ones included), so a row's cells can be read by key.
    taken = {c["id"] for c in columns if c.get("id") and not c.get("_gen")}
    counter = 0
    for column in columns:
        if column.get("id") and not column.get("_gen"):
            continue
        counter += 1
        while "c{}".format(counter) in taken:
            counter += 1
        column["id"] = "c{}".format(counter)
        column["_gen"] = True
        taken.add(column["id"])
    keys = [c["id"] for c in columns]
    for index, row in enumerate(rows):
        cells = row.get("cells", [])
        field = "rows[{}].cells".format(index)
        if isinstance(cells, list):
            if len(cells) > len(keys):
                raise ctx.invalid(field, "row {} has {} cells for {} columns".format(index + 1, len(cells), len(keys)))
            row["cells"] = {key: (cells[i] if i < len(cells) else "") for i, key in enumerate(keys)}
        else:
            unknown = [key for key in cells if key not in keys]
            if unknown:
                raise ctx.invalid("{}.{}".format(field, unknown[0]), "{} is not a column key; the keys are {}".format(unknown[0], ", ".join(keys)))
            row["cells"] = {key: cells.get(key, "") for key in keys}
    return spec


# --------------------------------------------------------------------------
# geometry (a pure function of the element)


def _columns(el: Element) -> List[Dict[str, Any]]:
    return [c for c in el.get("columns") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)]


def _rows(el: Element) -> List[Dict[str, Any]]:
    return [r for r in el.get("rows") or [] if isinstance(r, dict) and isinstance(r.get("id"), str)]


def _settings(el: Element) -> Dict[str, Any]:
    return el.get("settings") if isinstance(el.get("settings"), dict) else {}


def geometry(el: Element) -> Dict[str, Any]:
    """Column widths, row heights and every cell's lines, from the element's own columns, rows and settings."""
    settings = _settings(el)
    size = float(SIZES.get(settings.get("size"), SIZES["m"]))
    lines_max = settings.get("max_lines") if isinstance(settings.get("max_lines"), int) else DEFAULT_LINES
    columns, rows = _columns(el), _rows(el)
    header = settings.get("header", True) is not False
    widths: List[float] = []
    for column in columns:
        width = column.get("width")
        if isinstance(width, str) and width in WIDTHS:
            widths.append(float(WIDTHS[width]))
        elif isinstance(width, (int, float)) and not isinstance(width, bool):
            widths.append(float(width))
        else:
            widest = TX.width(str(column.get("title") or ""), size, HEADER_WEIGHT) if header else 0.0
            for row in rows:
                cells = row.get("cells") if isinstance(row.get("cells"), dict) else {}
                widest = max(widest, TX.width(str(cells.get(column["id"]) or ""), size, CELL_WEIGHT))
            widths.append(float(min(MAX_W, max(MIN_W, TX.snap(widest + 2 * PAD_X, 0.0, 4.0)))))
    lh = canvas_text.line_height(size)
    cells: Dict[str, TX.Laid] = {}
    heights: List[float] = []
    truncated: List[str] = []
    grid_rows: List[Tuple[str, Optional[Dict[str, Any]]]] = ([("h", None)] if header else []) + [(r["id"], r) for r in rows]
    for rid, row in grid_rows:
        tallest = 1
        for column, width in zip(columns, widths):
            text = str(column.get("title") or "") if row is None else str((row.get("cells") or {}).get(column["id"]) or "")
            laid = TX.lay(text, width - 2 * PAD_X, size, HEADER_WEIGHT if row is None else CELL_WEIGHT, max_lines=lines_max, bullets=False)
            part = "{}.{}".format(rid, column["id"])
            cells[part] = laid
            if laid.truncated:
                truncated.append(part)
            tallest = max(tallest, len(laid.lines()))
        heights.append(max(ROW_MIN, tallest * lh + 2 * PAD_Y))
    band = BAND if str(el.get("text") or "").strip() else 0.0
    return {"size": size, "widths": widths, "heights": heights, "rows": grid_rows, "cells": cells, "band": band, "truncated": truncated,
            "w": max(sum(widths), MIN_W), "h": band + sum(heights) if heights else band + ROW_MIN}


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The surface, the title band, the header and toned rows, hairlines between cells, and every cell's lines."""
    x0, y0, x1, y1 = D.box_of(el)
    geo = geometry(el)
    settings = _settings(el)
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": 8, "fill": "base.surface",
                                    "stroke": "tone.neutral.zone_stroke", "sw": 1}]
    title = str(el.get("text") or "")
    if geo["band"]:
        line = D.text_prim([canvas_text_fit(title, (x1 - x0) - 2 * PAD_X)], x0 + PAD_X, y0 + (BAND - canvas_text.line_height(TITLE_SIZE)) / 2.0,
                           TITLE_SIZE, {}, D.INK, "start", (x0 + PAD_X, y0, max(0.0, (x1 - x0) - 2 * PAD_X), BAND), TITLE_WEIGHT)
        line["lod"] = list(D.LOD_LABEL)
        items.append(line)
    top = y0 + geo["band"]
    fills: List[Dict[str, Any]] = []
    hairlines: List[Dict[str, Any]] = []
    texts: List[Dict[str, Any]] = []
    y = top
    data_index = 0
    for (rid, row), height in zip(geo["rows"], geo["heights"]):
        if row is None:
            fills.append({"k": "rect", "x": x0 + 1, "y": y, "w": (x1 - x0) - 2, "h": height, "fill": "tone.neutral.zone"})
        else:
            tone = row.get("tone") if row.get("tone") in canvas_theme.TONES else None
            if tone and tone != "neutral":
                fills.append({"k": "rect", "x": x0 + 1, "y": y, "w": (x1 - x0) - 2, "h": height, "fill": "tone.{}.fill".format(tone)})
            elif settings.get("zebra") and data_index % 2 == 1:
                fills.append({"k": "rect", "x": x0 + 1, "y": y, "w": (x1 - x0) - 2, "h": height, "fill": "tone.neutral.zone", "op": 0.5})
            data_index += 1
        if y > top or geo["band"]:
            hairlines.append({"k": "line", "points": [[x0, y], [x1, y]], "stroke": "base.grid", "sw": 1})
        x = x0
        for column, width in zip(_columns(el), geo["widths"]):
            laid = geo["cells"]["{}.{}".format(rid, column["id"])]
            align = column.get("align") if column.get("align") in ALIGNS else "start"
            room = width - 2 * PAD_X
            top_text = y + (height - laid.height) / 2.0
            if align == "start":
                texts += TX.emit_lines(laid, x + PAD_X, top_text, room, D.INK, lod="label" if row is None else "body")
            else:
                texts += _aligned(laid, x + PAD_X, top_text, room, align, row is None)
            x += width
        y += height
    x = x0
    for width in geo["widths"][:-1]:
        x += width
        hairlines.append({"k": "line", "points": [[x, top], [x, y1]], "stroke": "base.grid", "sw": 1})
    return items + fills + hairlines + texts


def canvas_text_fit(text: str, room: float) -> str:
    from herdr_team import canvas_geometry

    return canvas_geometry.fit_line(text, max(0.0, room), TITLE_SIZE, TITLE_WEIGHT)


def _aligned(laid: TX.Laid, x: float, top: float, room: float, align: str, header: bool) -> List[Dict[str, Any]]:
    lines = laid.lines()
    if not lines:
        return []
    anchor = "middle" if align == "center" else "end"
    at = x + room / 2.0 if align == "center" else x + room
    prim = D.text_prim(lines, at, top, laid.size, {}, D.INK, anchor, (x, top, room, laid.height), laid.weight)
    if header:
        prim["lod"] = list(D.LOD_LABEL)
        return [prim]
    return D.body(prim)


def _cell_boxes(el: Element) -> List[Tuple[str, List[float], Dict[str, Any]]]:
    x0, y0, _x1, _y1 = D.box_of(el)
    geo = geometry(el)
    out = []
    y = y0 + geo["band"]
    for (rid, row), height in zip(geo["rows"], geo["heights"]):
        x = x0
        for column, width in zip(_columns(el), geo["widths"]):
            value = str(column.get("title") or "") if row is None else str((row.get("cells") or {}).get(column["id"]) or "")
            out.append(("{}.{}".format(rid, column["id"]), [x, y, width, height], {"value": value, "header": row is None, "size": geo["size"],
                                                                                  "align": column.get("align")}))
            x += width
        y += height
    return out


def parts(el: Element) -> List[Dict[str, Any]]:
    """Every cell: ``h.<column>`` for a header cell, ``<row>.<column>`` for a cell (6.2)."""
    out = []
    for part, box, info in _cell_boxes(el):
        inner = [box[0] + PAD_X, box[1] + PAD_Y, max(1.0, box[2] - 2 * PAD_X), max(1.0, box[3] - 2 * PAD_Y)]
        out.append({"part": part, "hit": {"shape": "rect", "box": box},
                    "edit": TX.edit("text", info["value"], inner, info["size"], HEADER_WEIGHT if info["header"] else CELL_WEIGHT, D.INK, part=part,
                                    align="center" if info.get("align") == "center" else "start"),
                    "lod": list(D.LOD_LABEL if info["header"] else D.LOD_BODY)})
    return out


def part_edit(root: Element, part: str, text: str) -> Dict[str, Any]:
    """A cell's edit as a patch: a header cell renames its column, any other cell updates its row."""
    row, _dot, column = str(part).partition(".")
    if row == "h":
        return {"update": {"columns": [{"id": column, "title": text}]}}
    return {"update": {"rows": [{"id": row, "cells": {column: text}}]}}


def text_edit(el: Element) -> Optional[Dict[str, Any]]:
    """The title in its band (a table without a title edits its first cell through its parts)."""
    if not str(el.get("text") or "").strip():
        return None
    x0, y0, x1, _y1 = D.box_of(el)
    lh = canvas_text.line_height(TITLE_SIZE)
    return TX.edit("text", str(el.get("text") or ""), [x0 + PAD_X, y0 + (BAND - lh) / 2.0, max(1.0, (x1 - x0) - 2 * PAD_X), lh], TITLE_SIZE,
                   TITLE_WEIGHT, D.INK, wrap="line")


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


# --------------------------------------------------------------------------
# build and readback


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    columns = [{k: v for k, v in c.items() if k != "_gen"} for c in spec["columns"]]
    rows = [{k: v for k, v in r.items() if k != "_gen"} for r in spec.get("rows") or []]
    settings = {"header": spec["header"], "zebra": spec["zebra"], "max_lines": spec["max_lines"], "size": spec["size"]}
    probe = {"type": "table", "text": spec.get("title") or "", "columns": columns, "rows": rows, "settings": settings}
    geo = geometry(probe)
    truncated = bool(geo["truncated"])
    fit = {"policy": "clamp", "min": [int(geo["w"]), int(geo["h"])], "truncated": truncated, "parts": geo["truncated"][:50]}
    style = None if bctx.root is not None else bctx.ctx.default_style("table")
    bctx.root_fields(text=spec.get("title") or "", style=style, settings=settings, w=geo["w"], h=geo["h"], columns=columns, rows=rows, fit=fit)


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    """The op: columns as titles and rows as arrays, objects only when they carry more."""
    from herdr_team.canvas_blocks import generated_ids

    out: Dict[str, Any] = {"op": "table"}
    if root.get("alias"):
        out["id"] = root["alias"]
    if root.get("text"):
        out["title"] = root["text"]
    columns = _columns(root)
    gen_cols = generated_ids("c", [c["id"] for c in columns])
    out["columns"] = []
    for column, generated in zip(columns, gen_cols):
        extra = {k: column[k] for k in ("align", "width") if column.get(k) is not None}
        if generated and not extra:
            out["columns"].append(column.get("title") or "")
        else:
            item = {"title": column.get("title") or ""}
            if not generated:
                item["key"] = column["id"]
            item.update(extra)
            out["columns"].append(item)
    rows = _rows(root)
    gen_rows = generated_ids("r", [r["id"] for r in rows])
    out["rows"] = []
    for row, generated in zip(rows, gen_rows):
        cells = [str((row.get("cells") or {}).get(c["id"]) or "") for c in columns]
        while cells and cells[-1] == "":
            cells.pop()
        if generated and not row.get("tone"):
            out["rows"].append(cells)
        else:
            item: Dict[str, Any] = {}
            if not generated:
                item["id"] = row["id"]
            item["cells"] = cells
            if row.get("tone"):
                item["tone"] = row["tone"]
            out["rows"].append(item)
    settings = _settings(root)
    for key, default in (("header", True), ("zebra", False), ("max_lines", DEFAULT_LINES), ("size", "m")):
        if settings.get(key, default) != default:
            out[key] = settings[key]
    return out


def readback(el: Element, full: bool) -> str:
    return "{} table{}{} {}x{} [{},{} {}x{}]".format(el.get("id"), " " + str(el["alias"]) if el.get("alias") else "",
                                                   " " + quote(el.get("text"), 0 if full else 60) if el.get("text") else "",
                                                   len(_rows(el)), len(_columns(el)), el.get("x"), el.get("y"), el.get("w"), el.get("h"))


def clamped(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``label_truncated`` naming the clamped cells, with a fix that keeps one more line."""
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    if not fit.get("truncated"):
        return []
    lines = _settings(el).get("max_lines") if isinstance(_settings(el).get("max_lines"), int) else DEFAULT_LINES
    parts_cut = [str(p) for p in fit.get("parts") or []]
    fix = {"op": "patch", "id": el.get("alias") or el.get("id"), "set": {"max_lines": lines + 1}} if lines < 4 else None
    return [{"code": "label_truncated", "ids": [str(el.get("id"))], "parts": parts_cut,
             "message": "{} clamps {} cell{} to {} line{} ({}); raise max_lines or widen the column".format(
                 el.get("id"), len(parts_cut), "" if len(parts_cut) == 1 else "s", lines, "" if lines == 1 else "s", ", ".join(parts_cut[:5])),
             "fix": fix}]


def create(ctx: Any, op: Dict[str, Any]) -> None:
    ctx.block("table", op)


OPS = (
    OpSpec(name="table", fields=("title", "columns", "rows", "header", "zebra", "max_lines", "size", "id", "client_id"), create=create, place=True,
           order=43, doc="a table: columns and rows held in one element, every cell editable",
           mcp="table {title, columns [title | {key, title, align, width s|m|l}], rows [[cells] | {id, cells, tone}], header, zebra, max_lines 1-4}"),
)

KINDS = (
    Kind(name="table", role="composite", ops=("table",), solid=True, cell=True, connectable=True, handles="none", edit_limit="label",
         block=Block(collections=(Collection(name="columns", item=column_item, prefix="c", maximum=MAX_COLUMNS, label="title"),
                                  Collection(name="rows", item=row_item, prefix="r", maximum=MAX_ROWS, refs=(("cells", "columns", "key"),))),
                     settings=("header", "zebra", "max_lines", "size"), parts="inline", positional=False, normalize=normalize, build=build,
                     spec=spec, part_edit=part_edit),
         emit=emit, hit=hit, text_edit=text_edit, parts=parts, readback=readback, checks=(clamped,),
         doc="columns and rows held in one element, every cell addressable"),
)

"""``recall``: one search over everything a team knows (0.19).

The board, the facts, the work items with their settlement summaries (a
work item's thread brief), and the text files in the team's ``artifacts/``
folder are indexed into one SQLite FTS5 table under the team's state dir
(``<team>/index/recall.sqlite3``). The index is a cache: the JSONL files and
the folder stay the source of truth, and deleting the index only costs a
rebuild on the next query. It is brought up to date lazily, by the query that
needs it, under its own lock.

Ranking follows graphiti's hybrid search without its paid parts: several
cheap rankings are fused with reciprocal rank fusion (``score = sum 1/(60 +
rank)``):

* keyword relevance, SQLite's ``bm25``;
* recency;
* standing: a current fact backed by several members outranks a retired one
  or a passing remark;
* with ``--about``, closeness to the subject.

``--as-of`` answers "what did the team believe then": facts believed at that
moment, and posts and work created by then. Everything returned is already
readable by any member, so recall adds no new access; snippets from files are
still passed through the secret patterns the board refuses.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from herdr_team import facts as _facts
from herdr_team import store
from herdr_team import work as _work
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import TeamPaths, ensure_dir

INDEX_VERSION = 1
RRF_K = 60
POST_KINDS = ("note", "request", "handoff", "done", "blocked", "question", "answer", "direct")
#: System records worth finding: announcements of what the team learned or decided.
POST_EVENTS = ("knowledge_finding", "charter_updated", "knowledge_updated", "fact_resolved", "manager_changed", "link_established")
TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".html", ".htm", ".xml", ".rst", ".org",
                 ".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".sh", ".sql", ".c", ".h", ".cpp", ".swift", ".kt", ".toml", ".ini", ".log")
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 2000
CHUNK_CHARS = 1200
DEFAULT_LIMIT = 10
KINDS = ("post", "fact", "work", "file")
_TERM_RE = re.compile(r"\w+", re.UNICODE)


def index_path(team: TeamPaths) -> Path:
    return team.root / "index" / "recall.sqlite3"


def _lock(team: TeamPaths) -> store.FileLock:
    return store.FileLock(team.root / "index" / "recall.lock", timeout=10.0, code="recall_locked")


def _connect(path: Path) -> sqlite3.Connection:
    ensure_dir(path.parent)
    if not path.exists():
        fd = store.secure_open(path, os.O_RDWR | os.O_CREAT)
        os.close(fd)
    con = sqlite3.connect(os.fspath(path), timeout=10.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
            body, about, key UNINDEXED, kind UNINDEXED, ref UNINDEXED, author UNINDEXED,
            ts UNINDEXED, weight UNINDEXED, info UNINDEXED, tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
        """
    )
    row = con.execute("SELECT v FROM meta WHERE k='version'").fetchone()
    if row is None or row[0] != str(INDEX_VERSION):
        con.executescript("DELETE FROM docs; DELETE FROM files; DELETE FROM meta;")
        con.execute("INSERT OR REPLACE INTO meta VALUES('version', ?)", (str(INDEX_VERSION),))
        con.commit()
    return con


def _meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, value))


def _sig(*paths: Path) -> str:
    out = []
    for path in paths:
        try:
            st = os.stat(path)
            out.append("{}:{}:{}".format(path.name, st.st_size, int(st.st_mtime_ns)))
        except OSError:
            out.append("{}:-".format(path.name))
    return "|".join(out)


def _ts(value: Any) -> float:
    epoch = _facts.epoch(value) if isinstance(value, str) else None
    return float(epoch) if epoch is not None else 0.0


def _insert(con: sqlite3.Connection, key: str, kind: str, ref: str, author: str, ts: float, body: str, about: str = "", weight: float = 1.0, info: Optional[Dict[str, Any]] = None) -> None:
    con.execute("INSERT INTO docs(body, about, key, kind, ref, author, ts, weight, info) VALUES(?,?,?,?,?,?,?,?,?)",
                (body, about, key, kind, ref, author, ts, weight, json.dumps(info or {}, ensure_ascii=False)))


# --------------------------------------------------------------------------
# indexing


def _index_board(con: sqlite3.Connection, team: TeamPaths) -> int:
    board = store.BoardStore(team)
    archive = ",".join(sorted(board._archive_filenames()))
    seq_doc = store.read_json(team.board_seq, default={})
    first = str(seq_doc.get("active_first_seq") if isinstance(seq_doc, dict) else "")
    watermark = int(_meta(con, "board_watermark", "0") or 0)
    # A rotation, a wipe or a purge moved or deleted what was indexed: start over.
    # Rare, and the only way to be sure a purged post is not still findable.
    if (_meta(con, "archive") != archive or _meta(con, "active_first") != first) and watermark:
        con.execute("DELETE FROM docs WHERE kind='post'")
        watermark = 0
    if board.max_seq() < watermark:
        con.execute("DELETE FROM docs WHERE kind='post'")
        watermark = 0
    records = board.read(since_seq=watermark, include_archive=watermark == 0, include_retracted=True)
    added = 0
    top = watermark
    for record in records:
        seq = record.get("seq")
        if not isinstance(seq, int):
            continue
        top = max(top, seq)
        retracts = record.get("retracts")
        if isinstance(retracts, int):
            con.execute("DELETE FROM docs WHERE key=?", ("post:{}".format(retracts),))
            continue
        kind = record.get("kind")
        if kind == "system" and record.get("event") not in POST_EVENTS:
            continue
        if kind != "system" and kind not in POST_KINDS:
            continue
        text = str(record.get("text") or "")
        if not text.strip():
            continue
        meta = record.get("work") if isinstance(record.get("work"), dict) else {}
        _insert(con, "post:{}".format(seq), "post", "#{}".format(seq), str(record.get("from") or "?"), _ts(record.get("ts")), text,
                about=str(meta.get("id") or ""), weight=1.2 if kind in ("done", "answer") else 1.0,
                info={"kind": kind, "to": record.get("to"), "event": record.get("event"), "work": meta.get("id")})
        added += 1
    _set_meta(con, "board_watermark", str(top))
    _set_meta(con, "archive", archive)
    _set_meta(con, "active_first", first)
    return added


def _index_facts(con: sqlite3.Connection, team: TeamPaths) -> int:
    signature = _sig(_facts.facts_jsonl(team), team.knowledge_jsonl)
    if _meta(con, "facts_sig") == signature:
        return 0
    con.execute("DELETE FROM docs WHERE kind='fact'")
    state = _facts.load(team)
    for fact in state.ordered():
        status = fact.status(state.disputes)
        weight = (2.0 + 0.5 * (len(fact.members) - 1) + 0.25 * len(fact.sources)) if fact.current else 0.5
        if status == "disputed":
            weight = 1.0
        sources = " ".join(str(s.get("url") or s.get("path") or "") for s in fact.sources)
        _insert(con, "fact:" + fact.id, "fact", fact.id, fact.author, _ts(fact.recorded_at), "{} {}".format(fact.label(), sources).strip(),
                about="{} {}".format(fact.about or "", fact.attribute or "").strip(), weight=weight,
                info={"status": status, "confidence": fact.confidence(), "recorded_at": fact.recorded_at, "retired_at": fact.retired_at,
                      "valid_from": fact.valid_from, "valid_to": fact.valid_to, "about": fact.about, "attribute": fact.attribute})
    _set_meta(con, "facts_sig", signature)
    return len(state.facts)


def _index_work(con: sqlite3.Connection, team: TeamPaths) -> int:
    signature = _sig(_work.work_jsonl(team))
    if _meta(con, "work_sig") == signature:
        return 0
    con.execute("DELETE FROM docs WHERE kind='work'")
    items = _work.load(team)
    for item in _work.sorted_items(items):
        parts = [item.title] + [item.brief[k] for k in _work.BRIEF_FIELDS if item.brief.get(k)]
        for attempt in item.attempts:
            if attempt.summary:
                parts.append(attempt.summary)
            parts.extend(attempt.deliverables + attempt.evidence)
        parts.extend(str(r.get("note") or "") for r in item.reviews)
        _insert(con, "work:" + item.id, "work", item.id, item.owner or item.requester, _ts(item.created_at), "\n".join(p for p in parts if p),
                about=item.id, weight=1.5 if item.status == _work.STATUS_DONE else 1.2,
                info={"status": item.status, "title": item.title, "owner": item.owner, "created_at": item.created_at})
    _set_meta(con, "work_sig", signature)
    return len(items)


def _drop_file(con: sqlite3.Connection, rel: str) -> None:
    """Remove one file's passages by exact, case-sensitive prefix. A pattern match would treat
    ``_`` as a wildcard and ignore ASCII case, so notes_v1.md would take notes-v1.md with it."""
    prefix = rel + ":"
    con.execute("DELETE FROM docs WHERE kind='file' AND substr(ref, 1, ?) = ?", (len(prefix), prefix))


def _chunks(text: str) -> Iterable[Tuple[int, str]]:
    """Passages of about ``CHUNK_CHARS``, each with the line it starts on."""
    lines = text.splitlines()
    buf: List[str] = []
    start = 1
    size = 0
    for number, line in enumerate(lines, 1):
        if not buf:
            start = number
        buf.append(line)
        size += len(line) + 1
        if size >= CHUNK_CHARS:
            yield start, "\n".join(buf)
            buf, size = [], 0
    if buf:
        yield start, "\n".join(buf)


def _index_files(con: sqlite3.Connection, root: Optional[Path]) -> int:
    known = {row[0]: (row[1], row[2]) for row in con.execute("SELECT path, mtime, size FROM files")}
    seen: Dict[str, Tuple[float, int]] = {}
    # A member who replaces artifacts/ with a link must not get the files it points at
    # indexed and served to the whole team; os.walk follows a symlinked starting point.
    if root is not None and root.is_dir() and not root.is_symlink():
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if len(seen) >= MAX_FILES:
                    break
                if name.startswith(".") or not name.lower().endswith(TEXT_SUFFIXES):
                    continue
                path = Path(dirpath) / name
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if not os.path.isfile(path) or os.path.islink(path) or st.st_size > MAX_FILE_BYTES:
                    continue
                seen[os.fspath(path.relative_to(root))] = (st.st_mtime, st.st_size)
    changed = 0
    for rel in set(known) - set(seen):
        _drop_file(con, rel)
        con.execute("DELETE FROM files WHERE path=?", (rel,))
        changed += 1
    for rel, (mtime, size) in seen.items():
        if known.get(rel) == (mtime, size):
            continue
        _drop_file(con, rel)
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace") if root is not None else ""
        except OSError:
            continue
        for line, passage in _chunks(text):
            _insert(con, "file:{}:{}".format(rel, line), "file", "{}:{}".format(rel, line), "", float(mtime), passage, about=rel, weight=1.0, info={"path": rel, "line": line})
        con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)", (rel, mtime, size))
        changed += 1
    return changed


def refresh(team: TeamPaths, artifacts: Optional[Path]) -> Dict[str, int]:
    """Bring the index up to date; returns how much each source changed."""
    with _lock(team):
        con = _connect(index_path(team))
        try:
            with con:
                stats = {"posts": _index_board(con, team), "facts": _index_facts(con, team), "work": _index_work(con, team), "files": _index_files(con, artifacts)}
        finally:
            con.close()
    return stats


# --------------------------------------------------------------------------
# querying


def fts_query(query: str, any_term: bool = False) -> str:
    """User text -> an FTS5 expression that cannot be a syntax error: quoted phrases and terms."""
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]*"', " ", query)
    parts = ['"{}"'.format(" ".join(_TERM_RE.findall(p))) for p in phrases if _TERM_RE.findall(p)]
    parts += ['"{}"'.format(term) for term in _TERM_RE.findall(rest)]
    return (" OR " if any_term else " ").join(parts)


def _rank(values: Sequence[Tuple[str, float]], reverse: bool) -> Dict[str, int]:
    """Competition ranking: equal values share a rank, so a tie never decides anything."""
    ordered = sorted(values, key=lambda kv: kv[1], reverse=reverse)
    out: Dict[str, int] = {}
    previous = None
    rank = 0
    for index, (key, value) in enumerate(ordered):
        if index == 0 or value != previous:
            rank = index
        out[key] = rank
        previous = value
    return out


def _believed(info: Dict[str, Any], kind: str, ts: float, when: float) -> bool:
    if kind == "fact":
        fact = _facts.Fact(id="x", statement="", author="", recorded_at=info.get("recorded_at") or "",
                           retired_at=info.get("retired_at"), valid_from=info.get("valid_from"), valid_to=info.get("valid_to"))
        return fact.believed_at(when)
    if kind == "file":
        return True
    return ts <= when


def search(team: TeamPaths, artifacts: Optional[Path], query: str, kinds: Optional[Sequence[str]] = None, about: Optional[str] = None,
           as_of: Optional[str] = None, limit: int = DEFAULT_LIMIT, refresh_index: bool = True) -> Dict[str, Any]:
    if not _TERM_RE.search(query or ""):
        raise HerdrTeamError("usage", "recall needs words to look for", EXIT_REFUSED)
    stats = refresh(team, artifacts) if refresh_index else {}
    con = _connect(index_path(team))
    try:
        rows = []
        for any_term in (False, True):
            expression = fts_query(query, any_term)
            rows = con.execute(
                "SELECT key, kind, ref, author, ts, weight, info, bm25(docs), snippet(docs, 0, '»', '«', '…', 24), about FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT 400",
                (expression,)).fetchall()
            if rows or any_term:
                break
    finally:
        con.close()
    when = _facts.epoch(as_of) if as_of else None
    candidates = []
    for key, kind, ref, author, ts, weight, info_json, bm25, snippet, about_col in rows:
        if kinds and kind not in kinds:
            continue
        try:
            info = json.loads(info_json or "{}")
        except ValueError:
            info = {}
        if when is not None and not _believed(info, kind, float(ts or 0), when):
            continue
        closeness = 0.0
        if about:
            needle = _facts.normalize(about)
            if needle and (needle in _facts.normalize(about_col) or needle in _facts.normalize(info.get("about"))):
                closeness = 2.0
            elif needle and needle in _facts.normalize(snippet):
                closeness = 1.0
        candidates.append({"key": key, "kind": kind, "ref": ref, "author": author, "ts": float(ts or 0), "weight": float(weight or 1),
                           "bm25": float(bm25), "snippet": _redact(snippet), "info": info, "closeness": closeness})
    rankings = [
        _rank([(c["key"], c["bm25"]) for c in candidates], reverse=False),
        _rank([(c["key"], c["ts"]) for c in candidates], reverse=True),
        _rank([(c["key"], c["weight"]) for c in candidates], reverse=True),
    ]
    for c in candidates:
        c["score"] = sum(1.0 / (RRF_K + r[c["key"]] + 1) for r in rankings)
        # asked about a subject: what is about it comes first, what mentions it next
        c["score"] = round(c["score"] * (1.0 + 0.5 * c["closeness"]), 6)
    candidates.sort(key=lambda c: (-c["score"], c["bm25"]))
    hits = candidates[:max(1, int(limit))]
    for hit in hits:
        hit["when"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(hit["ts"])) if hit["ts"] else None
    return {"query": query, "hits": hits, "indexed": stats, "as_of": as_of, "about": about}


def _redact(text: str) -> str:
    from herdr_team.cmd_board import SECRET_PATTERNS

    for kind, pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted:{}]".format(kind), text)
    return text

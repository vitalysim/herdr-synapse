"""Board storage tests (plan M1: BD-01 to BD-05, BD-08, BD-09, tailer, ENOSPC)."""

import errno
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from herdr_team import store
from herdr_team.errors import HerdrTeamError
from support import PLUGIN_ROOT, TempState


def _note(text="hi", sender="alpha-worker", to=None, **extra):
    rec = {"from": sender, "kind": "note", "text": text, "to": to or ["all"]}
    rec.update(extra)
    return rec


def _seqs(records):
    return [r["seq"] for r in records]


def _hold_team_lock(lock_path, hold_s, ready):
    lock = store.FileLock(lock_path, timeout=1.0)
    with lock:
        ready.set()
        time.sleep(hold_s)


SUBPROCESS_POSTER = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from herdr_team import paths, store
team = paths.TeamPaths(Path(sys.argv[2]), "alpha")
board = store.BoardStore(team)
tag = sys.argv[3]
for i in range(int(sys.argv[4])):
    board.append({"from": "alpha-reviewer", "kind": "note", "text": "%s-%d" % (tag, i), "to": ["all"]})
print("done")
"""


class RecordGrammarTests(unittest.TestCase):
    def test_normalize_fills_every_key_in_order(self):
        rec = store.normalize_record(_note("x", reply_to=3))
        self.assertEqual(tuple(rec.keys()), store.RECORD_KEYS)
        self.assertEqual(rec["v"], 1)
        self.assertIsNone(rec["seq"])
        self.assertEqual(rec["reply_to"], 3)
        self.assertEqual(rec["refs"], [])
        self.assertEqual(rec["origin"], {})
        self.assertFalse(rec["urgent"])

    def test_normalize_keeps_extra_keys_after_the_grammar(self):
        rec = store.normalize_record(_note("x", custom="y"))
        self.assertEqual(list(rec.keys())[-1], "custom")

    def test_invalid_records_refused(self):
        cases = [
            ({"kind": "note", "text": "x", "to": ["all"]}, "record_invalid"),
            ({"from": "a", "kind": "note", "text": "x", "to": []}, "record_invalid"),
            ({"from": "a", "kind": "note", "text": "x", "to": ["ok", ""]}, "record_invalid"),
            (_note(kind="bogus"), "record_invalid"),
            ({"from": "system", "kind": "system", "text": "x", "to": ["all"]}, "record_invalid"),
            ({"from": "system", "kind": "note", "text": "x", "to": ["all"]}, "record_invalid"),
            ({"from": "a", "kind": "system", "event": "nudged", "text": "x", "to": ["all"]}, "record_invalid"),
            (_note(event="nudged"), "record_invalid"),
            (_note(kind="retract"), "record_invalid"),
            (_note(reply_to="7"), "record_invalid"),
            ("not a dict", "record_invalid"),
            (_note("bad \udcff surrogate"), "invalid_utf8"),
            (_note(origin={"via": "bad \udcff"}), "invalid_utf8"),
        ]
        for record, code in cases:
            with self.assertRaises(HerdrTeamError, msg=repr(record)) as ctx:
                store.normalize_record(record)
            self.assertEqual(ctx.exception.code, code, repr(record))
            self.assertEqual(ctx.exception.exit_code, 1)

    def test_bytes_values_are_decoded_strictly(self):
        rec = store.normalize_record(_note(b"caf\xc3\xa9"))
        self.assertEqual(rec["text"], "café")
        with self.assertRaises(HerdrTeamError) as ctx:
            store.normalize_record(_note(b"caf\xe9"))
        self.assertEqual(ctx.exception.code, "invalid_utf8")

    def test_encode_record_never_contains_a_raw_newline(self):
        rec = store.normalize_record(_note("line1\nline2\u2028three"))
        rec["seq"], rec["ts"] = 1, "t"
        line = store.encode_record(rec)
        self.assertEqual(line.count(b"\n"), 1)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(json.loads(line)["text"], "line1\nline2\u2028three")

    def test_parse_lines_skips_fragment_and_corrupt(self):
        good = store.encode_record(dict(store.normalize_record(_note("a")), seq=1, ts="t"))
        records, stats = store.parse_lines(good + b"{not json}\n" + b'{"v":1,"seq":"x"}\n' + b'{"v":1,"seq":2,"from":"a","kind":"note","to":["all"],"text":"b"}\n' + b'{"v":1,"seq":3')
        self.assertEqual(_seqs(records), [1, 2])
        self.assertEqual(stats, {"corrupt": 2, "fragment": 1})

    def test_safe_basename(self):
        self.assertEqual(store.safe_basename("diff.md"), "diff.md")
        self.assertEqual(store.safe_basename("/a/b/report-v2_final.txt"), "report-v2_final.txt")
        rewritten = store.safe_basename("my report (1).txt")
        self.assertRegex(rewritten, r"^my_report__1_-[0-9a-f]{8}\.txt$")
        self.assertRegex(store.safe_basename(".env"), r"^env-[0-9a-f]{8}$")
        self.assertRegex(store.safe_basename("é.txt"), r"^_-[0-9a-f]{8}\.txt$")
        long_name = "x" * 200 + ".log"
        self.assertLessEqual(len(store.safe_basename(long_name)), store.MAX_BASENAME_CHARS)
        self.assertEqual(store.safe_basename(""), "file")


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.board = store.BoardStore(self.ts.team)

    def test_append_assigns_seq_ts_and_writes_board_seq(self):
        self.assertEqual(self.board.append(_note("one")), 1)
        self.assertEqual(self.board.append(_note("two")), 2)
        lines = self.ts.team.board_jsonl.read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        recs = [json.loads(l) for l in lines[:-1]]
        self.assertEqual([r["seq"] for r in recs], [1, 2])
        self.assertRegex(recs[0]["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(tuple(recs[0].keys()), store.RECORD_KEYS)
        self.assertEqual(json.loads(self.ts.team.board_seq.read_text()), {"next": 3, "active_first_seq": 1})
        self.assertEqual(stat.S_IMODE(self.ts.team.board_jsonl.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.ts.team.board_seq.stat().st_mode), 0o600)

    def test_validation_happens_before_the_lock_and_writes_nothing(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            self.board.append(_note("bad \udcff"))
        self.assertEqual(ctx.exception.code, "invalid_utf8")
        self.assertFalse(self.ts.team.board_jsonl.exists())
        self.assertFalse(self.ts.team.board_seq.exists())

    def test_bd02_torn_tail_healed_exactly_one_line_skipped(self):
        for i in range(3):
            self.board.append(_note("n%d" % i))
        data = self.ts.team.board_jsonl.read_bytes()
        cut = data.rfind(b"\n", 0, len(data) - 1)  # start of the last record
        torn = data[: cut + 1 + (len(data) - cut) // 2]  # keep half of the last record
        self.ts.team.board_jsonl.write_bytes(torn)
        records, stats = self.board.read_detailed()
        self.assertEqual(_seqs(records), [1, 2])
        self.assertEqual(stats["fragment"], 1)
        self.assertEqual(stats["corrupt"], 0)
        # a writer heals with a newline; the torn line becomes exactly one corrupt line
        self.assertEqual(self.board.append(_note("after")), 4)
        raw = self.ts.team.board_jsonl.read_bytes()
        self.assertEqual(raw.count(b"\n"), 4)
        records, stats = self.board.read_detailed()
        self.assertEqual(_seqs(records), [1, 2, 4])
        self.assertEqual(stats["corrupt"], 1)
        self.assertEqual(stats["fragment"], 0)

    def test_bd09_board_seq_garbage_recomputed(self):
        for i in range(3):
            self.board.append(_note("n%d" % i))
        self.ts.team.board_seq.write_text("garbage{{")
        self.assertEqual(self.board.next_seq_unlocked(), 4)
        self.assertEqual(self.board.append(_note("x")), 4)
        self.assertEqual(json.loads(self.ts.team.board_seq.read_text())["next"], 5)
        # a stale, too-small hint loses to the tail
        self.ts.team.board_seq.write_text('{"next":2,"active_first_seq":1}\n')
        self.assertEqual(self.board.append(_note("y")), 5)
        # a larger hint wins: gaps are legal
        self.ts.team.board_seq.write_text('{"next":50,"active_first_seq":1}\n')
        self.assertEqual(self.board.append(_note("z")), 50)
        # missing file entirely
        os.unlink(self.ts.team.board_seq)
        self.assertEqual(self.board.append(_note("w")), 51)
        self.assertEqual(_seqs(self.board.read()), [1, 2, 3, 4, 5, 50, 51])

    def test_tail_scan_reads_only_the_last_window(self):
        big = "x" * 1500
        for i in range(60):
            self.board.append(_note(big))
        self.assertGreater(self.ts.team.board_jsonl.stat().st_size, store.TAIL_SCAN_BYTES)
        os.unlink(self.ts.team.board_seq)
        self.assertEqual(self.board.next_seq_unlocked(), 61)
        self.assertEqual(self.board.tail_state()["active_first_seq"], 1)

    def test_enospc_truncates_back_and_leaves_a_gap(self):
        self.board.append(_note("ok"))
        before = self.ts.team.board_jsonl.read_bytes()

        def fail(fd, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(store, "_os_write", fail):
            with self.assertRaises(HerdrTeamError) as ctx:
                self.board.append(_note("lost"))
        self.assertEqual(ctx.exception.code, "board_write_failed")
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(ctx.exception.details["errno"], errno.ENOSPC)
        self.assertEqual(self.ts.team.board_jsonl.read_bytes(), before)
        # board.seq moved on: the failed seq is a gap, never reused
        self.assertEqual(self.board.append(_note("next")), 3)
        self.assertEqual(_seqs(self.board.read()), [1, 3])

    def test_short_write_truncates_back(self):
        self.board.append(_note("ok"))
        before = self.ts.team.board_jsonl.read_bytes()
        real_write = os.write

        def short(fd, data):
            return real_write(fd, data[: len(data) // 2])

        with mock.patch.object(store, "_os_write", short):
            with self.assertRaises(HerdrTeamError) as ctx:
                self.board.append(_note("lost"))
        self.assertEqual(ctx.exception.code, "board_write_failed")
        self.assertEqual(self.ts.team.board_jsonl.read_bytes(), before)

    def test_lock_timeout_says_post_not_written(self):
        ready = multiprocessing.Event()
        child = multiprocessing.Process(target=_hold_team_lock, args=(os.fspath(self.ts.team.team_lock), 1.5, ready))
        child.start()
        try:
            self.assertTrue(ready.wait(5.0))
            board = store.BoardStore(self.ts.team, lock_timeout=0.2)
            with self.assertRaises(HerdrTeamError) as ctx:
                board.append(_note("blocked"))
            self.assertEqual(ctx.exception.code, "board_locked")
            self.assertEqual(ctx.exception.exit_code, 5)
            self.assertTrue(ctx.exception.message.startswith("post NOT written"))
            self.assertEqual(ctx.exception.to_json()["lock"], os.fspath(self.ts.team.team_lock))
            self.assertFalse(self.ts.team.board_jsonl.exists())
        finally:
            child.join(5.0)

    def test_symlinked_board_refused(self):
        real = self.ts.tmp / "elsewhere.jsonl"
        real.write_text("")
        os.symlink(real, self.ts.team.board_jsonl)
        with self.assertRaises(HerdrTeamError) as ctx:
            self.board.append(_note("x"))
        self.assertEqual(ctx.exception.code, "path_symlink")


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_bd01_fifty_threads_and_eight_subprocesses(self):
        board = store.BoardStore(self.ts.team)
        errors = []
        results = []
        barrier = threading.Barrier(50)

        def post(i):
            try:
                barrier.wait(10)
                results.append(board.append(_note("t%d" % i)))
            except Exception as err:  # noqa: BLE001 - collected for the assertion
                errors.append(err)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(50)]
        procs = []
        per_proc = 3
        for p in range(8):
            procs.append(subprocess.Popen(
                [sys.executable, "-c", SUBPROCESS_POSTER, os.fspath(PLUGIN_ROOT), os.fspath(self.ts.team.root), "p%d" % p, str(per_proc)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        for proc in procs:
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
            self.assertEqual(out.strip(), b"done")
        self.assertEqual(errors, [])
        total = 50 + 8 * per_proc
        self.assertEqual(len(results), 50)
        lines = self.ts.team.board_jsonl.read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        parsed = [json.loads(l) for l in lines[:-1]]
        self.assertEqual(sorted(r["seq"] for r in parsed), list(range(1, total + 1)))
        self.assertEqual([r["seq"] for r in parsed], sorted(r["seq"] for r in parsed))
        self.assertEqual(len(set(r["text"] for r in parsed)), total)
        records, stats = board.read_detailed()
        self.assertEqual(_seqs(records), list(range(1, total + 1)))
        self.assertEqual(stats["corrupt"], 0)
        self.assertEqual(stats["duplicates"], 0)
        self.assertEqual(json.loads(self.ts.team.board_seq.read_text()), {"next": total + 1, "active_first_seq": 1})

    def test_bd05_concurrent_cursor_writers(self):
        board = store.BoardStore(self.ts.team)
        for i in range(40):
            board.append(_note("n%d" % i))
        cursors = store.Cursors(self.ts.team, board)
        errors = []
        barrier = threading.Barrier(30)

        def advance(i):
            try:
                barrier.wait(10)
                cursors.advance("alpha-worker", i + 1, "term_w1", "cli")
                cursors.advance("human@lab", (i % 7) + 1, None, "cli")
            except Exception as err:  # noqa: BLE001
                errors.append(err)

        threads = [threading.Thread(target=advance, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(cursors.get("alpha-worker")["seq"], 30)
        self.assertEqual(cursors.get("human@lab")["seq"], 7)
        doc = json.loads(self.ts.team.cursor("alpha-worker").read_text())
        self.assertEqual(set(doc), {"v", "seq", "terminal_id", "surfaced_by", "updated"})
        self.assertTrue(self.ts.team.human_cursor("lab").exists())


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.board = store.BoardStore(self.ts.team)
        self.board.append(_note("to all"))  # 1
        self.board.append(_note("to reviewer", to=["alpha-reviewer"], kind="request"))  # 2
        self.board.append(_note("reply", sender="alpha-reviewer", to=["alpha-worker"], kind="answer", reply_to=2))  # 3
        self.board.append(_note("human note", sender="human", to=["all"]))  # 4
        self.board.append({"from": "alpha-worker", "kind": "retract", "retracts": 1, "text": "retracted #1", "to": ["all"]})  # 5
        self.board.append(_note("edited", supersedes=2, to=["alpha-reviewer"], kind="request"))  # 6

    def test_filters(self):
        self.assertEqual(_seqs(self.board.read()), [1, 2, 3, 4, 5, 6])
        self.assertEqual(_seqs(self.board.read(since_seq=4)), [5, 6])
        self.assertEqual(_seqs(self.board.read(to=["alpha-reviewer", "all"])), [1, 2, 4, 5, 6])
        self.assertEqual(_seqs(self.board.read(from_name="alpha-reviewer")), [3])
        self.assertEqual(_seqs(self.board.read(kind="request")), [2, 6])
        self.assertEqual(_seqs(self.board.read(thread=2)), [2, 3, 6])
        self.assertEqual(_seqs(self.board.read(last=2)), [5, 6])
        self.assertEqual(_seqs(self.board.read(limit=2)), [1, 2])
        self.assertEqual(_seqs(self.board.read(last=3, limit=2)), [4, 5])
        self.assertEqual(_seqs(self.board.read(include_retracted=False)), [2, 3, 4, 6])
        self.assertEqual(self.board.get(3)["text"], "reply")
        self.assertIsNone(self.board.get(99))

    def test_read_never_takes_the_lock_and_tolerates_a_missing_file(self):
        with store.team_lock(self.ts.team, timeout=0.1):
            self.assertEqual(len(self.board.read()), 6)
        os.unlink(self.ts.team.board_jsonl)
        self.assertEqual(self.board.read(), [])
        self.assertEqual(self.board.tail_state()["inode"], 0)

    def test_corrupt_lines_and_duplicates_counted_separately(self):
        line = store.encode_record(dict(store.normalize_record(_note("dup")), seq=3, ts="t"))
        with open(self.ts.team.board_jsonl, "ab") as fh:
            fh.write(b"\x00\x01 not json\n" + line + b'{"v":2,"seq":9,"from":"a","kind":"note","to":["all"],"text":""}\n')
        records, stats = self.board.read_detailed()
        self.assertEqual(_seqs(records), [1, 2, 3, 4, 5, 6])
        self.assertEqual(records[2]["text"], "reply")  # first occurrence wins
        self.assertEqual(stats["corrupt"], 2)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(self.board.last_stats["corrupt"], 2)

    def test_retraction_and_supersedes_helpers(self):
        records = self.board.read()
        self.assertEqual(store.retracted_map(records), {1: 5})
        self.assertEqual(store.supersedes_map(records), {2: 6})
        chain = records + [dict(store.normalize_record(_note("again", supersedes=6)), seq=7, ts="t")]
        self.assertEqual(store.supersedes_map(chain), {2: 7, 6: 7})
        author = {"from": "alpha-reviewer", "from_kind": "codex", "origin": {"via": "cli", "verified": True}}
        retract = store.make_retract_record(records[1], author)
        self.assertEqual(self.board.append(retract), 7)
        got = self.board.get(7)
        self.assertEqual((got["kind"], got["retracts"], got["from_kind"], got["to"]), ("retract", 2, "codex", ["alpha-reviewer"]))
        edit = store.make_supersede_record(records[1], author, "new text")
        self.assertEqual(self.board.append(edit), 8)
        self.assertEqual(self.board.get(8)["supersedes"], 2)
        self.assertEqual(self.board.get(8)["kind"], "request")


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_rotation_moves_the_file_and_continues_seq(self):
        board = store.BoardStore(self.ts.team, rotate_bytes=1200)
        seqs = [board.append(_note("x" * 300)) for _ in range(6)]
        self.assertEqual(seqs, sorted(seqs))
        every = board.read(include_archive=True)
        max_seq = every[-1]["seq"]
        self.assertEqual(_seqs(every), list(range(1, max_seq + 1)))
        rotated = [r for r in every if r["kind"] == "system"]
        self.assertGreaterEqual(len(rotated), 1)
        self.assertTrue(all(r["event"] == "rotated" and r["from"] == "system" for r in rotated))
        self.assertEqual(sorted(seqs + [r["seq"] for r in rotated]), _seqs(every))
        names = sorted(os.listdir(self.ts.team.archive_dir))
        self.assertIn("index.json", names)
        first_segment = self.ts.team.archive_segment(1, rotated[0]["seq"] - 1)
        self.assertIn(first_segment.name, names)
        active_first = rotated[-1]["seq"]  # every rotation opens the new file with its note
        self.assertEqual(json.loads(self.ts.team.board_seq.read_text())["active_first_seq"], active_first)
        self.assertEqual(_seqs(board.read()), list(range(active_first, max_seq + 1)))
        self.assertEqual(_seqs(board.read(last=max_seq - 1)), list(range(2, max_seq + 1)))
        self.assertEqual(_seqs(board.read(since_seq=2)), list(range(3, max_seq + 1)))
        self.assertEqual(_seqs(board.read(thread=2)), [2])
        self.assertEqual(board.get(2)["seq"], 2)
        self.assertEqual(board.tail_state()["max_seq"], max_seq)
        self.assertEqual(board.tail_state()["active_first_seq"], active_first)
        # the archive max survives the active file vanishing
        os.unlink(self.ts.team.board_jsonl)
        os.unlink(self.ts.team.board_seq)
        self.assertEqual(board.next_seq_unlocked(), rotated[-1]["seq"])

    def test_bd04_index_rebuilt_from_filenames(self):
        board = store.BoardStore(self.ts.team, rotate_bytes=800)
        for _ in range(8):
            board.append(_note("y" * 300))
        expected = sorted(n for n in os.listdir(self.ts.team.archive_dir) if n != "index.json")
        self.assertGreaterEqual(len(expected), 2)
        os.unlink(self.ts.team.archive_index)
        segments = board.archive_segments()
        self.assertEqual([s["file"] for s in segments], sorted(expected, key=lambda n: int(n.split(".")[1].split("-")[0])))
        self.assertTrue(self.ts.team.archive_index.exists())
        doc = json.loads(self.ts.team.archive_index.read_text())
        self.assertEqual(doc["v"], 1)
        self.assertEqual(doc["max_seq"], max(s["last_seq"] for s in segments))
        # inconsistent index (a segment missing, a bogus one listed) is rebuilt on read
        bogus = {"v": 1, "segments": [{"file": "board.900-999.jsonl", "first_seq": 900, "last_seq": 999, "bytes": 0}], "max_seq": 999}
        self.ts.team.archive_index.write_text(json.dumps(bogus))
        self.assertEqual(board.next_seq_unlocked(), doc["max_seq"] + 1 + len(board.read()))
        self.assertEqual([s["file"] for s in board.archive_segments()], [s["file"] for s in segments])
        # garbage index is rebuilt too
        self.ts.team.archive_index.write_text("nope")
        self.assertEqual(len(board.archive_segments()), len(segments))
        self.assertEqual(json.loads(self.ts.team.archive_index.read_text())["max_seq"], doc["max_seq"])

    def test_rotate_if_needed_is_a_noop_below_the_threshold(self):
        board = store.BoardStore(self.ts.team)
        board.append(_note("small"))
        self.assertIsNone(board.rotate_if_needed())
        board.rotate_bytes = 1
        target = board.rotate_if_needed()
        self.assertEqual(target, self.ts.team.archive_segment(1, 1))
        self.assertEqual(_seqs(board.read()), [2])


class TailerTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_tail_new_records_and_persist_state(self):
        board = store.BoardStore(self.ts.team)
        tailer = store.BoardTailer(self.ts.team)
        self.addCleanup(tailer.close)
        self.assertEqual(tailer.poll(), [])
        board.append(_note("a"))
        board.append(_note("b"))
        self.assertEqual(_seqs(tailer.poll()), [1, 2])
        self.assertEqual(tailer.poll(), [])
        state = json.loads(self.ts.team.notifier_state.read_text())
        self.assertEqual(state["watermark_seq"], 2)
        self.assertEqual(state["offset"], self.ts.team.board_jsonl.stat().st_size)
        self.assertEqual(state["inode"], os.stat(self.ts.team.board_jsonl).st_ino)
        # a partial line is not yielded until its newline lands
        with open(self.ts.team.board_jsonl, "ab") as fh:
            fh.write(b'{"v":1,"seq":3,"from":"x","kind":"note","to":["all"]')
        self.assertEqual(tailer.poll(), [])
        with open(self.ts.team.board_jsonl, "ab") as fh:
            fh.write(b',"text":"c"}\n')
        self.assertEqual(_seqs(tailer.poll()), [3])
        # a second instance resumes from the persisted state
        board.append(_note("d"))
        tailer.close()
        again = store.BoardTailer(self.ts.team)
        self.addCleanup(again.close)
        self.assertEqual(again.watermark_seq, 3)
        self.assertEqual(_seqs(again.poll()), [4])

    def test_start_seq_used_without_state(self):
        board = store.BoardStore(self.ts.team)
        for i in range(5):
            board.append(_note("n%d" % i))
        tailer = store.BoardTailer(self.ts.team, start_seq=3, persist=False)
        self.addCleanup(tailer.close)
        self.assertEqual(_seqs(tailer.poll()), [4, 5])
        self.assertFalse(self.ts.team.notifier_state.exists())

    def test_bd03_rotation_under_an_active_tailer_sees_every_seq(self):
        board = store.BoardStore(self.ts.team, rotate_bytes=700)
        tailer = store.BoardTailer(self.ts.team)
        self.addCleanup(tailer.close)
        seen = []
        appended = []
        for i in range(40):
            appended.append(board.append(_note("r%02d" % i + "z" * 200)))
            if i % 3 == 2:
                seen.extend(_seqs(tailer.poll()))
        seen.extend(_seqs(tailer.poll()))
        self.assertGreater(len(os.listdir(self.ts.team.archive_dir)), 3)
        self.assertEqual(seen, list(range(1, board.max_seq() + 1)))
        self.assertTrue(set(appended).issubset(seen))
        self.assertEqual(tailer.resets, 0)

    def test_tailer_far_behind_reads_archives_across_the_gap(self):
        board = store.BoardStore(self.ts.team, rotate_bytes=700)
        tailer = store.BoardTailer(self.ts.team)
        self.addCleanup(tailer.close)
        board.append(_note("first"))
        self.assertEqual(_seqs(tailer.poll()), [1])
        for i in range(30):  # several rotations happen while the tailer sleeps
            board.append(_note("g%02d" % i + "q" * 200))
        self.assertEqual(board.get(1)["seq"], 1)  # archived, still reachable
        got = _seqs(tailer.poll())
        self.assertEqual(got, list(range(2, board.max_seq() + 1)))
        self.assertEqual(tailer.resets, 0)

    def test_enoent_between_rotation_and_create_is_tolerated(self):
        board = store.BoardStore(self.ts.team)
        tailer = store.BoardTailer(self.ts.team)
        self.addCleanup(tailer.close)
        board.append(_note("a"))
        self.assertEqual(_seqs(tailer.poll()), [1])
        # simulate a rotation by hand: rename away, records still pending in the old inode
        board.append(_note("b"))
        os.makedirs(self.ts.team.archive_dir, exist_ok=True)
        os.rename(self.ts.team.board_jsonl, self.ts.team.archive_segment(1, 2))
        board.rebuild_archive_index()
        drained = tailer.poll()  # drains the old fd, file is gone
        self.assertEqual(_seqs(drained), [2])
        self.assertEqual(tailer.poll(), [])
        self.assertIsNone(tailer.fd)
        self.assertEqual(tailer.warnings, [])
        tailer.ENOENT_LOG_S = 0.0
        tailer.poll()
        self.assertTrue(any("missing" in w for w in tailer.warnings))
        board.append(_note("c"))
        self.assertEqual(_seqs(tailer.poll()), [3])

    def test_reset_detection_returns_a_system_record(self):
        board = store.BoardStore(self.ts.team)
        tailer = store.BoardTailer(self.ts.team)
        self.addCleanup(tailer.close)
        for i in range(3):
            board.append(_note("n%d" % i))
        self.assertEqual(_seqs(tailer.poll()), [1, 2, 3])
        # someone deletes the board and its seq file; the next post restarts at 1
        os.unlink(self.ts.team.board_jsonl)
        os.unlink(self.ts.team.board_seq)
        self.assertEqual(board.append(_note("fresh")), 1)
        out = tailer.poll()
        self.assertEqual(len(out), 2)
        reset, fresh = out
        self.assertTrue(reset["synthetic"])
        self.assertIsNone(reset["seq"])
        self.assertEqual((reset["from"], reset["kind"], reset["event"]), ("system", "system", "reset_detected"))
        self.assertEqual(fresh["seq"], 1)
        self.assertEqual(tailer.resets, 1)
        self.assertEqual(tailer.watermark_seq, 1)
        board.append(_note("more"))
        self.assertEqual(_seqs(tailer.poll()), [2])

    def test_truncation_of_the_same_inode_is_a_reset(self):
        board = store.BoardStore(self.ts.team)
        tailer = store.BoardTailer(self.ts.team, persist=False)
        self.addCleanup(tailer.close)
        for i in range(3):
            board.append(_note("n%d" % i))
        self.assertEqual(_seqs(tailer.poll()), [1, 2, 3])
        with open(self.ts.team.board_jsonl, "r+b") as fh:
            fh.truncate(0)
        os.unlink(self.ts.team.board_seq)
        self.assertEqual(board.append(_note("again")), 1)
        out = tailer.poll()
        self.assertEqual([r.get("event") for r in out], ["reset_detected", None])
        self.assertEqual(out[1]["seq"], 1)

    def test_truncation_while_the_tailer_was_down_is_a_reset(self):
        board = store.BoardStore(self.ts.team)
        tailer = store.BoardTailer(self.ts.team)
        for i in range(3):
            board.append(_note("n%d" % i))
        self.assertEqual(_seqs(tailer.poll()), [1, 2, 3])
        tailer.close()
        # same inode, truncated and restarted at seq 1 while no tailer was running
        with open(self.ts.team.board_jsonl, "r+b") as fh:
            fh.truncate(0)
        os.unlink(self.ts.team.board_seq)
        self.assertEqual(board.append(_note("again")), 1)
        again = store.BoardTailer(self.ts.team)
        self.addCleanup(again.close)
        self.assertEqual(again.watermark_seq, 3)
        out = again.poll()
        self.assertEqual([r.get("event") for r in out], ["reset_detected", None])
        self.assertEqual(out[1]["seq"], 1)
        self.assertEqual(again.resets, 1)
        self.assertEqual(again.watermark_seq, 1)
        # a restart whose persisted offset still fits the file is a plain resume, not a reset
        board.append(_note("two"))
        again.close()
        third = store.BoardTailer(self.ts.team)
        self.addCleanup(third.close)
        self.assertEqual(_seqs(third.poll()), [2])
        self.assertEqual(third.resets, 0)

    def test_archived_seqs_are_not_a_reset(self):
        board = store.BoardStore(self.ts.team)
        tailer = store.BoardTailer(self.ts.team, persist=False)
        self.addCleanup(tailer.close)
        for i in range(3):
            board.append(_note("n%d" % i))
        self.assertEqual(_seqs(tailer.poll()), [1, 2, 3])
        # a copy of archived content reappears as the active file (no new seqs): skip, no reset
        os.rename(self.ts.team.board_jsonl, self.ts.team.archive_segment(1, 3))
        board.rebuild_archive_index()
        self.assertEqual(tailer.poll(), [])
        import shutil

        shutil.copy(self.ts.team.archive_segment(1, 3), self.ts.team.board_jsonl)
        self.assertEqual(tailer.poll(), [])
        self.assertEqual(tailer.resets, 0)

    def test_corrupt_lines_are_skipped_and_warned(self):
        board = store.BoardStore(self.ts.team)
        tailer = store.BoardTailer(self.ts.team, persist=False)
        self.addCleanup(tailer.close)
        board.append(_note("a"))
        with open(self.ts.team.board_jsonl, "ab") as fh:
            fh.write(b"garbage\n")
        board.append(_note("b"))
        self.assertEqual(_seqs(tailer.poll()), [1, 2])
        self.assertTrue(any("corrupt" in w for w in tailer.warnings))


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.board = store.BoardStore(self.ts.team)
        for i in range(10):
            self.board.append(_note("n%d" % i))
        self.cursors = store.Cursors(self.ts.team, self.board)

    def test_bd08_cursor_stops_at_the_last_printed_seq(self):
        printed = _seqs(self.board.read(limit=7))
        self.assertEqual(printed[-1], 7)
        doc = self.cursors.advance("alpha-worker", printed[-1], "term_w1", "cli")
        self.assertTrue(doc["advanced"])
        self.assertEqual(self.cursors.get("alpha-worker")["seq"], 7)
        self.assertEqual(_seqs(self.board.read(since_seq=self.cursors.get("alpha-worker")["seq"])), [8, 9, 10])
        stored = json.loads(self.ts.team.cursor("alpha-worker").read_text())
        self.assertEqual(stored["v"], 1)
        self.assertEqual(stored["surfaced_by"], "cli")
        self.assertEqual(stored["terminal_id"], "term_w1")
        self.assertRegex(stored["updated"], r"Z$")

    def test_cursor_never_moves_backwards(self):
        self.cursors.advance("alpha-worker", 8, "term_w1", "cli")
        doc = self.cursors.advance("alpha-worker", 5, "term_w1", "hook")
        self.assertFalse(doc["advanced"])
        self.assertEqual(self.cursors.get("alpha-worker")["seq"], 8)
        self.assertEqual(self.cursors.get("alpha-worker")["surfaced_by"], "cli")

    def test_missing_corrupt_and_beyond_max(self):
        missing = self.cursors.get("alpha-reviewer")
        self.assertEqual(missing["seq"], 0)
        self.assertIn("missing", missing["warning"])
        self.ts.team.cursor("alpha-reviewer").write_text("{corrupt")
        corrupt = self.cursors.get("alpha-reviewer")
        self.assertEqual(corrupt["seq"], 0)
        self.assertIn("corrupt", corrupt["warning"])
        self.ts.team.cursor("alpha-reviewer").write_text('{"v":1,"seq":"x"}')
        self.assertEqual(self.cursors.get("alpha-reviewer")["seq"], 0)
        self.ts.team.cursor("alpha-reviewer").write_text('{"v":1,"seq":500,"terminal_id":"t","surfaced_by":"cli","updated":"u"}')
        clamped = self.cursors.get("alpha-reviewer")
        self.assertEqual(clamped["seq"], 10)
        self.assertIn("clamped", clamped["warning"])
        doc = self.cursors.advance("alpha-worker", 999, "term_w1", "cli")
        self.assertEqual(doc["seq"], 10)
        self.assertEqual(json.loads(self.ts.team.cursor("alpha-worker").read_text())["seq"], 10)

    def test_human_cursor_and_all(self):
        self.cursors.advance("human@vitaly", 4, None, "cli")
        self.cursors.advance("alpha-worker", 2, "term_w1", "hook")
        self.assertTrue(self.ts.team.human_cursor("vitaly").exists())
        every = self.cursors.all()
        self.assertEqual(sorted(every), ["alpha-worker", "human@vitaly"])
        self.assertEqual(every["human@vitaly"]["seq"], 4)
        with self.assertRaises(HerdrTeamError) as ctx:
            self.cursors.get("../escape")
        self.assertEqual(ctx.exception.code, "name_invalid")


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.board = store.BoardStore(self.ts.team)

    def test_stage_and_commit_under_the_lock(self):
        src = self.ts.tmp / "my diff (1).md"
        src.write_bytes(b"# diff\n" * 100)
        staged = self.board.stage_attachment(src)
        self.assertTrue(staged.tmp_path.name.startswith(".tmp-%d-" % os.getpid()))
        self.assertEqual(staged.tmp_path.parent, self.ts.team.payloads_dir)
        self.assertEqual(staged.size, 700)
        self.assertEqual(stat.S_IMODE(staged.tmp_path.stat().st_mode), 0o600)
        self.assertRegex(staged.basename, r"^my_diff__1_-[0-9a-f]{8}\.md$")
        seq = self.board.append(_note("see diff"), attachments=[staged])
        rec = self.board.get(seq)
        self.assertEqual(rec["refs"], ["payloads/1-" + staged.basename])
        self.assertEqual(staged.committed, rec["refs"][0])
        self.assertFalse(staged.tmp_path.exists())
        self.assertEqual((self.ts.team.payloads_dir / ("1-" + staged.basename)).read_bytes(), b"# diff\n" * 100)

    def test_duplicate_basenames_get_a_suffix(self):
        a = self.ts.tmp / "a" / "notes.txt"
        b = self.ts.tmp / "b" / "notes.txt"
        a.parent.mkdir()
        b.parent.mkdir()
        a.write_text("A")
        b.write_text("B")
        s1, s2 = self.board.stage_attachment(a), self.board.stage_attachment(b)
        seq = self.board.append(_note("two"), attachments=[s1, s2])
        self.assertEqual(self.board.get(seq)["refs"], ["payloads/1-notes.txt", "payloads/1-notes-2.txt"])
        self.assertEqual(sorted(os.listdir(self.ts.team.payloads_dir)), ["1-notes-2.txt", "1-notes.txt"])

    def test_spill_text_lands_in_payloads(self):
        seq = self.board.append(_note("long post spilled"), spill_text="x" * 5000)
        body = self.ts.team.payloads_dir / "1-body.md"
        self.assertEqual(body.read_text(), "x" * 5000)
        self.assertEqual(self.board.get(seq)["refs"], ["payloads/1-body.md"])
        self.assertEqual(stat.S_IMODE(body.stat().st_mode), 0o600)

    def test_refusals(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            self.board.stage_attachment(self.ts.tmp / "missing")
        self.assertEqual(ctx.exception.code, "ref_invalid")
        with self.assertRaises(HerdrTeamError) as ctx:
            self.board.stage_attachment(self.ts.tmp)
        self.assertEqual(ctx.exception.code, "ref_invalid")
        real = self.ts.tmp / "real.txt"
        real.write_text("x")
        link = self.ts.tmp / "link.txt"
        os.symlink(real, link)
        with self.assertRaises(HerdrTeamError) as ctx:
            self.board.stage_attachment(link)
        self.assertEqual(ctx.exception.code, "path_symlink")
        big = self.ts.tmp / "big.bin"
        big.write_bytes(b"\0" * 100)
        with mock.patch.object(store, "MAX_ATTACHMENT_BYTES", 99):
            with self.assertRaises(HerdrTeamError) as ctx:
                self.board.stage_attachment(big)
        self.assertEqual(ctx.exception.code, "attachment_too_large")
        self.assertEqual([n for n in os.listdir(self.ts.team.payloads_dir) if n.startswith(".tmp-")], [])

    def test_failed_write_removes_committed_payloads(self):
        src = self.ts.tmp / "x.txt"
        src.write_text("x")
        staged = self.board.stage_attachment(src)

        def fail(fd, data):
            raise OSError(errno.ENOSPC, "full")

        with mock.patch.object(store, "_os_write", fail):
            with self.assertRaises(HerdrTeamError):
                self.board.append(_note("nope"), attachments=[staged], spill_text="body")
        self.assertEqual(os.listdir(self.ts.team.payloads_dir), [])

    def test_cleanup_stale_staging(self):
        src = self.ts.tmp / "x.txt"
        src.write_text("x")
        staged = self.board.stage_attachment(src)
        self.assertEqual(self.board.cleanup_stale_staging(max_age_s=3600), [])
        old = time.time() - 7200
        os.utime(staged.tmp_path, (old, old))
        self.assertEqual(self.board.cleanup_stale_staging(max_age_s=3600), [staged.tmp_path])
        self.board.discard_staged([staged])


if __name__ == "__main__":
    unittest.main()

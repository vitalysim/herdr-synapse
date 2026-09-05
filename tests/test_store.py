import json
import multiprocessing
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from herdr_team import store
from herdr_team.errors import HerdrTeamError, LockTimeout
from support import PLUGIN_ROOT, TempState


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_atomic_write_mode_and_content(self):
        target = self.ts.team.root / "x.json"
        store.atomic_write(target, b"hello")
        self.assertEqual(target.read_bytes(), b"hello")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        leftovers = [p for p in self.ts.team.root.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_atomic_write_creates_parent(self):
        target = self.ts.team.root / "deep" / "x"
        store.atomic_write(target, b"1")
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_write_and_read_json(self):
        target = self.ts.session.who_json
        store.write_json(target, {"v": 1, "x": "é"})
        self.assertEqual(store.read_json(target), {"v": 1, "x": "é"})
        self.assertTrue(target.read_bytes().endswith(b"\n"))
        self.assertEqual(store.read_json(self.ts.session.root / "missing.json", default={}), {})
        target.write_text("{not json")
        self.assertIsNone(store.read_json(target))

    def test_symlink_refused(self):
        real = self.ts.tmp / "real.json"
        real.write_text("{}")
        link = self.ts.team.root / "link.json"
        os.symlink(real, link)
        with self.assertRaises(HerdrTeamError) as ctx:
            store.atomic_write(link, b"x")
        self.assertEqual(ctx.exception.code, "path_symlink")
        with self.assertRaises(HerdrTeamError):
            store.read_bytes(link)
        with self.assertRaises(HerdrTeamError):
            store.secure_open(link, os.O_RDONLY)

    def test_append_line_heals_missing_newline(self):
        path = self.ts.team.board_jsonl
        store.append_line(path, b'{"seq":1}')
        with open(path, "ab") as fh:
            fh.write(b'{"seq":2}')  # torn tail without newline
        store.append_line(path, b'{"seq":3}\n')
        self.assertEqual(path.read_bytes(), b'{"seq":1}\n{"seq":2}\n{"seq":3}\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


def _hold_lock_in_child(lock_path, hold_s, ready):
    lock = store.FileLock(lock_path, timeout=1.0)
    with lock:
        ready.set()
        time.sleep(hold_s)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_team_lock_paths_and_reentry(self):
        lock = store.team_lock(self.ts.team)
        self.assertEqual(lock.path, self.ts.team.team_lock)
        self.assertEqual(store.team_lock(self.ts.team.root).path, self.ts.team.team_lock)
        self.assertEqual(store.daemon_lock(self.ts.session).path, self.ts.session.daemon_lock)
        self.assertEqual(store.daemon_lock(self.ts.session.root).path, self.ts.session.daemon_lock)
        self.assertEqual(store.claude_settings_lock("/h/.claude/settings.json").path, Path("/h/.claude/settings.json.herdr-team.lock"))
        with lock:
            self.assertTrue(lock.held)
            self.assertIs(lock.acquire(), lock)
        self.assertFalse(lock.held)
        self.assertEqual(stat.S_IMODE(self.ts.team.team_lock.stat().st_mode), 0o600)

    def test_team_lock_refuses_a_missing_team_dir(self):
        """A dissolved team's dir must not come back as a lock-only ``teams/<team>/`` (live RS-10 finding)."""
        gone = self.ts.team.root.parent / "gone"
        self.assertFalse(gone.exists())
        with self.assertRaises(HerdrTeamError) as ctx:
            store.team_lock(gone)
        self.assertEqual(ctx.exception.code, "team_not_found")
        self.assertFalse(gone.exists())
        archived = self.ts.team.root.parent / "archived-copy"
        os.rename(self.ts.team.root, archived)
        try:
            with self.assertRaises(HerdrTeamError):
                store.team_lock(self.ts.team)
            self.assertFalse(self.ts.team.root.exists())
        finally:
            os.rename(archived, self.ts.team.root)

    def test_lock_excludes_other_process_and_times_out(self):
        ready = multiprocessing.Event()
        child = multiprocessing.Process(target=_hold_lock_in_child, args=(os.fspath(self.ts.team.team_lock), 1.0, ready))
        child.start()
        try:
            self.assertTrue(ready.wait(5.0))
            lock = store.team_lock(self.ts.team, timeout=0.15)
            started = time.monotonic()
            with self.assertRaises(LockTimeout) as ctx:
                lock.acquire()
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.14)
            self.assertLess(elapsed, 0.9)
            self.assertEqual(ctx.exception.code, "board_locked")
            self.assertEqual(ctx.exception.exit_code, 5)
            self.assertEqual(ctx.exception.details["lock"], os.fspath(self.ts.team.team_lock))
            self.assertFalse(lock.held)
            self.assertFalse(store.daemon_lock(self.ts.session).try_acquire() is False)
        finally:
            child.join(5.0)
        # released by the child's exit: acquire promptly now
        with store.team_lock(self.ts.team, timeout=2.0):
            pass

    def test_daemon_lock_single_try_and_release_on_death(self):
        ready = multiprocessing.Event()
        child = multiprocessing.Process(target=_hold_lock_in_child, args=(os.fspath(self.ts.session.daemon_lock), 30.0, ready))
        child.start()
        try:
            self.assertTrue(ready.wait(5.0))
            lock = store.daemon_lock(self.ts.session)
            self.assertFalse(lock.try_acquire())
            self.assertEqual(lock.code, "daemon_lock_held")
        finally:
            child.terminate()
            child.join(5.0)
        self.assertTrue(store.daemon_lock(self.ts.session).try_acquire())

    def test_lock_survives_detach_sequence(self):
        """BD-10: a lock taken after fd cleanup stays held; one taken before closerange is lost."""
        script = r"""
import os, sys, time, fcntl
sys.path.insert(0, sys.argv[1])
from herdr_team import store
lock = store.FileLock(sys.argv[2], timeout=0.5)
# the detach order: close inherited fds first, then lock
os.closerange(3, 64)
lock.acquire()
print("held", flush=True)
time.sleep(float(sys.argv[3]))
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", script, os.fspath(PLUGIN_ROOT), os.fspath(self.ts.session.daemon_lock), "2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        )
        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), b"held")
            self.assertFalse(store.daemon_lock(self.ts.session).try_acquire())
        finally:
            proc.kill()
            proc.wait(5)
        self.assertTrue(store.daemon_lock(self.ts.session).try_acquire())

    def test_lock_symlink_refused(self):
        real = self.ts.tmp / "real.lock"
        real.write_text("")
        os.symlink(real, self.ts.team.team_lock)
        with self.assertRaises(HerdrTeamError) as ctx:
            store.team_lock(self.ts.team).acquire()
        self.assertEqual(ctx.exception.code, "path_symlink")


class RosterStoreTests(unittest.TestCase):
    """``RosterStore`` is the one team.json persistence path (roster, cmd_board, daemon, hooks share it)."""

    def test_load_missing_is_team_not_found(self):
        with TempState(write_team=False) as ts:
            with self.assertRaises(HerdrTeamError) as ctx:
                store.RosterStore(ts.team).load()
            self.assertEqual(ctx.exception.code, "team_not_found")

    def test_save_and_update_bump_revision_under_lock(self):
        with TempState() as ts:
            rs = store.RosterStore(ts.team)
            doc = rs.load()
            before = int(doc["revision"])
            saved = rs.save(dict(doc), expected_revision=before)
            self.assertEqual(saved["revision"], before + 1)

            def mutate(d):
                d["naming"] = "plain"

            updated = rs.update(mutate)
            self.assertEqual(updated["revision"], before + 2)
            self.assertEqual(rs.load()["naming"], "plain")
            self.assertEqual(oct(ts.team.team_json.stat().st_mode & 0o777), oct(0o600))

    def test_save_refuses_a_moved_revision(self):
        with TempState() as ts:
            rs = store.RosterStore(ts.team)
            doc = rs.load()
            rs.save(dict(doc))
            with self.assertRaises(HerdrTeamError) as ctx:
                rs.save(dict(doc), expected_revision=int(doc["revision"]))
            self.assertEqual(ctx.exception.code, "roster_conflict")

    def test_no_stub_raises_notimplemented(self):
        with TempState() as ts:
            for obj in (store.BoardStore(ts.team), store.Cursors(ts.team), store.RosterStore(ts.team)):
                for name in dir(type(obj)):
                    fn = getattr(type(obj), name)
                    if callable(fn) and not name.startswith("_"):
                        code = getattr(fn, "__code__", None)
                        self.assertNotIn("NotImplementedError", code.co_names if code else (), "{}.{}".format(type(obj).__name__, name))


if __name__ == "__main__":
    unittest.main()

"""daemon.py: process model (BD-10, PK-05), reconnects, ledger replay, env hygiene (S-07), toasts, jobs, delivery."""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from herdr_team import daemon as D
from herdr_team import gate, ledger, paths, roster, store
from herdr_team.api import HerdrApi, IDENTITY_ENV_VARS
from herdr_team.cli import main as cli_main
from herdr_team.errors import HerdrTeamError
from support import FAKE_AGENTS, FakeApi, FakeError, FakeHerdrServer, TempState, fake_agent, fake_read, identity_tokens, wait_until

PLUGIN_ROOT = paths.plugin_root()

DETACH_SCRIPT = """
import json, os, sys
sys.path.insert(0, {root!r})
from herdr_team import daemon, paths
env = dict(os.environ)
layout = paths.resolve_layout(env)
result = daemon.detach_and_run(layout, env, replace={replace})
print(json.dumps(result))
"""

HOLDER_SCRIPT = """
import os, sys, time, json
sys.path.insert(0, {root!r})
from herdr_team import daemon, paths, store
env = dict(os.environ)
layout = paths.resolve_layout(env)
paths.ensure_session_dirs(layout.session)
lock = store.daemon_lock(layout.session)
lock.acquire()
info = daemon.DaemonInfo(os.getpid(), daemon.process_start_time(os.getpid()) or "", daemon.now_iso(), os.fspath(layout.socket), None, "0.0.9", "0.8.2", 20)
daemon.write_daemon_info(layout.session, info)
print("held", flush=True)
time.sleep(60)
"""


def identity_env(ts, **extra):
    env = ts.env_with(HERDR_PANE_ID="wA:p6", HERDR_WORKSPACE_ID="wA", HERDR_TAB_ID="wA:t1", HERDR_SESSION="ignored", HERDR_TEAM_LOCK_WAIT_S="1")
    env.update(extra)
    return env


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def trust_kinds(ts, *kinds):
    """Mark kinds ``trusted`` in kinds.json (the owner override; plan 8.3 verifies through the ledger)."""
    doc = store.read_json(ts.session.kinds_json, default=None)
    if not isinstance(doc, dict):
        doc = {}
    for kind in kinds or ("claude", "codex"):
        entry = doc.get(kind) if isinstance(doc.get(kind), dict) else {}
        entry["trusted"] = True
        doc[kind] = entry
    store.write_json(ts.session.kinds_json, doc)


def make_daemon(ts, api=None, env=None, clock=None, trust=True):
    env = dict(env if env is not None else ts.env)
    if trust:
        trust_kinds(ts)
    else:
        store.write_json(ts.session.kinds_json, {})
    api = api if api is not None else FakeApi(ts.socket_path)
    clock = clock or FakeClock()
    d = D.Daemon(ts.layout, env, api, clock=clock, sleep=clock.sleep)
    d.landed_fast_ms = 0.0
    d.logged = []
    d.log_sinks.append(d.logged.append)
    d.log_stderr = False
    return d, api, clock


def post(ts, to, text="please review", author="alpha-worker", kind="request", urgent=False):
    return store.BoardStore(ts.team).append({
        "from": author, "from_kind": "claude", "from_pane": "w2:p2", "from_terminal": "term_w1", "from_gen": 1,
        "origin": {"via": "cli", "verified": True}, "to": [to] if isinstance(to, str) else list(to),
        "kind": kind, "text": text, "urgent": urgent,
    })


# --------------------------------------------------------------------------
# process model


class DetachTests(unittest.TestCase):
    """BD-10 and PK-05: the double fork keeps the lock, fds point where the plan says, takeover works."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.server = FakeHerdrServer(self.ts.socket_path).start()
        self.addCleanup(self.server.stop)
        self.addCleanup(self.stop_daemon)
        self.env = identity_env(self.ts)

    def stop_daemon(self):
        D.stop_daemon(self.ts.session, timeout_s=5.0)

    def detach(self, replace=False):
        script = DETACH_SCRIPT.format(root=os.fspath(PLUGIN_ROOT), replace=replace)
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, timeout=60)
        elapsed = time.monotonic() - t0
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return json.loads(proc.stdout.decode().strip().splitlines()[-1]), elapsed

    def test_detach_holds_lock_redirects_stdio_and_scrubs_env(self):
        result, _elapsed = self.detach()
        self.assertEqual(result["status"], "started", result)
        self.assertLess(result["intermediate_exit_ms"], 1000.0)
        self.assertFalse(result["replaced"])
        info = D.read_daemon_info(self.ts.session)
        self.assertIsNotNone(info)
        self.assertEqual(info.pid, result["daemon"]["pid"])
        self.assertTrue(info.start_time)
        self.assertEqual(info.start_time, D.process_start_time(info.pid))
        self.assertEqual(info.socket, os.path.realpath(self.ts.socket_path))
        self.assertEqual(info.socket_inode, os.stat(self.ts.socket_path).st_ino)
        self.assertEqual((info.herdr_version, info.protocol), ("0.8.2", 20))
        self.assertTrue(D.daemon_alive(self.ts.session))
        # the grandchild holds daemon.lock: a non-blocking try from here fails
        lock = store.daemon_lock(self.ts.session)
        self.assertFalse(lock.try_acquire())
        # the grandchild lives in the intermediate child's new session, not in ours
        self.assertNotEqual(info.pid, os.getpid())
        self.assertNotEqual(os.getsid(info.pid), os.getsid(0))
        self.assertNotEqual(os.getpgid(info.pid), os.getpgid(0))
        # fds 0-2 and env hygiene are reported by the daemon itself into daemon.log
        self.assertTrue(wait_until(lambda: "fd0=/dev/null fd1=daemon.log fd2=daemon.log" in self.ts.session.daemon_log.read_text(errors="replace"), 5.0))
        log = self.ts.session.daemon_log.read_text(errors="replace")
        self.assertIn("identity env unset: True", log)
        # ``identity_env`` sets HERDR_SESSION and HERDR_CLIENT_SOCKET_PATH is inherited when the test
        # itself runs inside Herdr; the grandchild must report both gone (S-07 live finding).
        self.assertIn("session env unset: True", log)
        self.assertTrue(info.identity_env_unset)
        # the daemon talked to the fake server as itself: never --current, never a pane id
        self.assertTrue(wait_until(lambda: len(self.server.requests_for("agent.list")) >= 1, 5.0))
        for request in list(self.server.requests):
            self.assertNotIn("current", json.dumps(request.get("params") or {}))
        self.assertTrue(self.server.requests_for("events.subscribe"))
        # SIGTERM through stop_daemon releases the lock
        self.assertTrue(D.stop_daemon(self.ts.session, timeout_s=5.0))
        self.assertFalse(D.daemon_alive(self.ts.session))
        self.assertTrue(wait_until(lambda: lock.try_acquire(), 3.0))
        lock.release()
        self.assertIn("stopping: signal 15", self.ts.session.daemon_log.read_text(errors="replace"))

    def test_second_start_is_already_running_and_replace_takes_over(self):
        first, _ = self.detach()
        self.assertEqual(first["status"], "started")
        pid1 = first["daemon"]["pid"]
        second, elapsed = self.detach()
        self.assertEqual(second["status"], "already_running", second)
        self.assertEqual(second["daemon"]["pid"], pid1)
        self.assertLess(elapsed, 10.0)
        self.assertTrue(D.daemon_alive(self.ts.session))
        third, _ = self.detach(replace=True)
        self.assertEqual(third["status"], "started", third)
        self.assertTrue(third["replaced"])
        pid2 = third["daemon"]["pid"]
        self.assertNotEqual(pid1, pid2)
        self.assertTrue(wait_until(lambda: not D.pid_alive(pid1), 5.0))
        self.assertEqual(D.read_daemon_info(self.ts.session).pid, pid2)
        self.assertTrue(D.daemon_alive(self.ts.session))

    def test_replace_takes_over_a_fake_lock_holder(self):
        holder = subprocess.Popen([sys.executable, "-c", HOLDER_SCRIPT.format(root=os.fspath(PLUGIN_ROOT))], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), b"held")
        self.assertTrue(D.daemon_alive(self.ts.session))
        plain, _ = self.detach()
        self.assertEqual(plain["status"], "already_running", plain)
        self.assertEqual(plain["daemon"]["pid"], holder.pid)
        result, _ = self.detach(replace=True)
        self.assertEqual(result["status"], "started", result)
        self.assertTrue(result["replaced"])
        self.assertEqual(holder.wait(timeout=5), -signal.SIGTERM)
        self.assertNotEqual(D.read_daemon_info(self.ts.session).pid, holder.pid)

    def test_cli_start_status_stop(self):
        def run_cli(argv):
            out, err = io.StringIO(), io.StringIO()
            code = cli_main(argv + ["--json"], env=self.env, stdout=out, stderr=err)
            return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

        # ``daemon start`` forks the test process; do it through a subprocess instead and read status here.
        started, _ = self.detach()
        self.assertEqual(started["status"], "started")
        code, payload, err = run_cli(["daemon", "start"])
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["already_running"])
        self.assertFalse(payload["started"])
        self.assertEqual(payload["daemon"]["pid"], started["daemon"]["pid"])
        self.assertEqual(payload["session_dir"], os.fspath(self.ts.session.root))
        self.assertEqual(os.path.realpath(payload["pointer"]), os.path.realpath(paths.pointer_file(self.ts.config_dir)))
        self.assertEqual(os.path.realpath(paths.read_pointer(paths.canonicalize(self.ts.config_dir))), os.path.realpath(self.ts.state_root))
        code, payload, err = run_cli(["daemon", "status"])
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["alive"])
        self.assertEqual(payload["pid"], started["daemon"]["pid"])
        self.assertEqual(payload["teams"], ["alpha"])
        self.assertEqual(payload["ledger"]["wrong_target"], 0)
        self.assertEqual(payload["socket_source"], "env:HERDR_SOCKET_PATH")
        self.assertIsNotNone(payload["beat_age_s"])
        code, payload, err = run_cli(["daemon", "stop"])
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["stopped"])
        self.assertEqual(payload["pid"], started["daemon"]["pid"])
        self.assertFalse(D.daemon_alive(self.ts.session))
        code, payload, err = run_cli(["daemon", "status"])
        self.assertFalse(payload["alive"])
        code, payload, err = run_cli(["daemon", "stop"])
        self.assertEqual((code, payload["stopped"], payload["pid"]), (0, False, None))
        code, payload, err = run_cli(["notifier", "stats"])
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["wrong_target"], 0)
        self.assertIn("alpha", payload["teams"])

    def test_version_mismatch_refuses_without_allow_version(self):
        self.server.set_response("ping", {"type": "pong", "version": "0.9.1", "protocol": 21})
        result, _ = self.detach()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["error"]["code"], "herdr_version_mismatch")
        self.assertFalse(D.daemon_alive(self.ts.session))
        self.assertTrue(store.daemon_lock(self.ts.session).try_acquire())


class StartGateTests(unittest.TestCase):
    def test_socket_not_allowed_skips_with_exit_0(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        allowed = paths.allowed_sockets_file(ts.config_dir)
        allowed.parent.mkdir(parents=True, exist_ok=True)
        allowed.write_text("/somewhere/else/herdr.sock\n")
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(["daemon", "start", "--json"], env=ts.env, stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["skipped"], "socket_not_allowed")
        self.assertFalse(ts.session.daemon_json.exists())

    def test_ensure_daemon_reports_failure_without_a_server(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        # No server: the grandchild starts anyway (it retries for 60 s) so ensure_daemon must still see it alive.
        script = DETACH_SCRIPT.format(root=os.fspath(PLUGIN_ROOT), replace=False)
        env = identity_env(ts)
        try:
            proc = subprocess.run([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
            result = json.loads(proc.stdout.decode().strip().splitlines()[-1])
            self.assertEqual(result["status"], "started", result)
            self.assertTrue(D.daemon_alive(ts.session))
            self.assertIn("server not reachable at start", ts.session.daemon_log.read_text(errors="replace"))
        finally:
            D.stop_daemon(ts.session, timeout_s=5.0)


# --------------------------------------------------------------------------
# reconnect and server identity


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_backoff_sequence_then_give_up_releases_lock(self):
        api = FakeApi(self.ts.socket_path)
        api.unreachable = True
        d, api, clock = make_daemon(self.ts, api=api)
        lock = store.daemon_lock(self.ts.session).acquire()
        d.lock = lock
        code = d.run()
        self.assertEqual(code, 3)
        self.assertEqual(d.stop_reason, "server unreachable")
        # 1, 2, 4, 8, 16, 30 s ... until 60 s of failures (interruptible sleeps in 100 ms slices)
        self.assertTrue(all(0.0 < s <= 0.1 + 1e-9 for s in clock.sleeps))
        self.assertGreaterEqual(sum(clock.sleeps), 60.0)
        self.assertLess(sum(clock.sleeps), 60.0 + 30.0 + 1.0)
        self.assertTrue(store.daemon_lock(self.ts.session).try_acquire())
        pings = [m for m, _ in api.calls if m == "ping"]
        self.assertGreaterEqual(len(pings), 5)
        delays = [line.split("retrying in ")[1].rstrip("s") for line in d.logged if "retrying in" in line]
        self.assertEqual(delays[:6], ["1", "2", "4", "8", "16", "30"])
        self.assertTrue(all(x == "30" for x in delays[5:]))
        self.assertTrue(any("unreachable for 60s" in line for line in d.logged))

    def test_removed_session_dir_stops_the_retry_loop_without_recreating_it(self):
        """A daemon whose ``<session>/`` vanished (teardown, gc, a finished test) exits instead of rewriting daemon.json there."""
        api = FakeApi(self.ts.socket_path)
        api.unreachable = True
        d, api, clock = make_daemon(self.ts, api=api)
        d.lock = store.daemon_lock(self.ts.session).acquire()
        shutil.rmtree(self.ts.session.root)
        code = d.run()
        self.assertEqual(code, 3)
        self.assertEqual(d.stop_reason, "session dir removed")
        self.assertFalse(self.ts.session.root.exists())
        self.assertFalse(self.ts.session.daemon_json.exists())
        self.assertEqual(len([m for m, _ in api.calls if m == "ping"]), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertTrue(any("session state dir removed" in line for line in d.logged), d.logged)

    def test_heartbeat_stops_when_the_session_dir_is_gone(self):
        d, api, clock = make_daemon(self.ts)
        d.heartbeat()
        self.assertTrue(self.ts.session.daemon_json.exists())
        shutil.rmtree(self.ts.session.root)
        d.heartbeat()
        self.assertTrue(d.stop_requested)
        self.assertEqual(d.stop_reason, "session dir removed")
        self.assertFalse(self.ts.session.root.exists())

    def test_reconnects_after_stream_eof_and_detects_server_replacement(self):
        socket_file = self.ts.socket_path
        socket_file.write_text("a")
        first_inode = os.stat(socket_file).st_ino

        class EofApi(FakeApi):
            rounds = 0

            def subscribe(self, subscriptions, tick_timeout=None, connect_timeout=None):
                EofApi.rounds += 1
                self.calls.append(("events.subscribe", {"subscriptions": list(subscriptions)}))
                if EofApi.rounds == 2:
                    # the server was replaced while we were subscribed: same path, new inode.
                    # Rename instead of unlink: Linux hands a freshly freed inode number straight back.
                    socket_file.rename(socket_file.with_name(socket_file.name + ".old"))
                    socket_file.write_text("b")
                yield {"event": "pane_updated", "data": {"pane": {"terminal_id": "term_r1", "agent_status": "working"}}}
                return

        d, api, clock = make_daemon(self.ts, api=EofApi(self.ts.socket_path))
        d.backoff = (0.0,)

        def stop_after(line):
            if d.counters["reconnects"] >= 3:
                d.request_stop("test")

        d.log_sinks.append(stop_after)
        d.run()
        self.assertGreaterEqual(d.counters["reconnects"], 3)
        self.assertEqual(d.socket_inode, os.stat(socket_file).st_ino)
        self.assertNotEqual(d.socket_inode, first_inode)
        self.assertTrue(any("server replaced" in line for line in d.logged), d.logged)
        self.assertTrue(any(line == "subscription ended; reconnecting" for line in d.logged), d.logged)
        # every reconnect is a cold start: agent.list and ping each time, caches rebuilt
        self.assertGreaterEqual(len([m for m, _ in api.calls if m == "agent.list"]), 3)
        self.assertGreaterEqual(len([m for m, _ in api.calls if m == "ping"]), 3)
        self.assertIn("term_r1", d.agents)

    def test_live_server_eof_then_loss(self):
        server = FakeHerdrServer(self.ts.socket_path)
        server.script_events([{"event": "pane_focused", "data": {"pane_id": "w2:p1"}}], interval_s=0.01, hold_open=False)
        server.start()
        api = HerdrApi(self.ts.socket_path, env=self.ts.env)
        d = D.Daemon(self.ts.layout, dict(self.ts.env), api)
        d.logged = []
        d.log_sinks.append(d.logged.append)
        d.log_stderr = False
        d.backoff = (0.05, 0.1)
        d.give_up_s = 1.0
        d.tick_s = 0.05
        thread = threading.Thread(target=lambda: setattr(d, "code", d.run()), daemon=True)
        thread.start()
        try:
            self.assertTrue(wait_until(lambda: d.counters["reconnects"] >= 2, 10.0), d.logged)
            self.assertGreaterEqual(server.connections, 3)
        finally:
            server.stop()
        thread.join(timeout=15.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(d.code, 3)
        self.assertEqual(d.stop_reason, "server unreachable")
        self.assertTrue(any("ping failed (server_not_running)" in line for line in d.logged), d.logged)

    def test_manifest_version_change_needs_two_identical_reads(self):
        d, api, clock = make_daemon(self.ts)
        d.manifest_version = "0.1.0"
        versions = iter(["0.1.0", "0.2.0", None, "0.2.0", "0.2.0"])
        original = D.read_manifest_version
        D.read_manifest_version = lambda root=None: next(versions)
        self.addCleanup(setattr, D, "read_manifest_version", original)
        d.watch_version(clock() * 1000)
        self.assertFalse(d.stop_requested)
        clock.advance(10)
        d.watch_version(clock() * 1000)  # first different read
        self.assertFalse(d.stop_requested)
        clock.advance(10)
        d.watch_version(clock() * 1000)  # unparseable: ignored
        self.assertFalse(d.stop_requested)
        clock.advance(10)
        d.watch_version(clock() * 1000)  # second identical read
        self.assertTrue(d.stop_requested)
        self.assertEqual(d.stop_reason, "manifest version changed")

    def test_registry_poll_exits_when_plugin_missing_or_disabled(self):
        d, api, clock = make_daemon(self.ts)
        api.set_response("plugin.list", {"type": "plugin_list", "plugins": [{"plugin_id": "herdr-team", "enabled": False}]})
        api.set_response("agent.view.clear", {"type": "ok"})
        d.scan_teams(force=True)
        d.poll_agents(force=True)
        d.poll_registry(clock() * 1000)  # first call only arms the interval
        self.assertFalse(d.stop_requested)
        clock.advance(D.REGISTRY_POLL_S / 2)
        d.poll_registry(clock() * 1000)  # inside the interval: no call, no exit
        self.assertFalse(d.stop_requested)
        self.assertFalse([m for m, _ in api.calls if m == "plugin.list"])
        clock.advance(D.REGISTRY_POLL_S / 2 + 0.5)
        d.poll_registry(clock() * 1000)
        self.assertTrue(d.stop_requested)
        self.assertEqual(d.stop_reason, "plugin disabled")
        cleared = [p for m, p in api.calls if m == "pane.report_metadata" and p["tokens"].get("team") is None]
        self.assertTrue(cleared)
        self.assertIn(("agent.view.clear", {"source": "plugin:herdr-team"}), api.calls)

    def test_registry_poll_interval_meets_the_disable_budget(self):
        # PK-08: ``plugin disable`` must clear tokens and the view and stop the daemon within 15 s.
        # Live on 2026-09-05 the 60 s interval took 54 s; the first poll fires one interval after start,
        # so the interval itself has to sit under the budget with room for the teardown calls.
        self.assertLessEqual(D.REGISTRY_POLL_S, 10.0)
        d, _api, _clock = make_daemon(self.ts)
        self.assertEqual(d.registry_poll_s, D.REGISTRY_POLL_S)


# --------------------------------------------------------------------------
# environment hygiene


class EnvHygieneTests(unittest.TestCase):
    def test_daemon_env_lacks_identity_vars(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        env = identity_env(ts)
        d = D.Daemon(ts.layout, env, FakeApi(ts.socket_path))
        for name in IDENTITY_ENV_VARS:
            self.assertNotIn(name, d.env)
        self.assertIn("HERDR_SOCKET_PATH", d.env)
        info = d.info()
        self.assertIn("identity_env_unset", info.to_json())

    def test_detach_scrubs_the_child_environment(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        # The scrubbed env handed to the grandchild pins the socket and drops the session name.
        from herdr_team.api import scrub_env

        scrubbed = scrub_env(identity_env(ts))
        for name in IDENTITY_ENV_VARS:
            self.assertNotIn(name, scrubbed)

    def test_dropped_env_names_cover_session_and_client_socket(self):
        # S-07 live on 2026-09-05: ``HERDR_SESSION`` survived into the daemon because the grandchild
        # overlaid the scrubbed copy on ``os.environ`` without removing the names the copy had dropped.
        for name in IDENTITY_ENV_VARS + ("HERDR_SESSION", "HERDR_CLIENT_SOCKET_PATH"):
            self.assertIn(name, D.DAEMON_DROPPED_ENV_VARS)
        self.assertNotIn("HERDR_SOCKET_PATH", D.DAEMON_DROPPED_ENV_VARS)


# --------------------------------------------------------------------------
# ledger replay and the hard rule


class LedgerReplayTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_open_intent_counts_as_sent_after_restart(self):
        seq = post(self.ts, "alpha-reviewer")
        led = ledger.Ledger(self.ts.team)
        led.record_intent(ledger.Attempt("old-1", "alpha-reviewer", "codex", [seq], False, False, False, True, 0.0, 0.0, 1))
        d, api, clock = make_daemon(self.ts)
        d.on_connected()
        self.assertIn("alpha-reviewer", d.teams["alpha"].open_intents)
        for _ in range(20):
            clock.advance(5)
            d.tick()
        self.assertNotIn("agent.prompt", [m for m, _ in api.calls])
        pending = d.teams["alpha"].pending["alpha-reviewer"]
        self.assertIsNotNone(pending.landed_ms)
        self.assertEqual(led.counts()["open_intents"], 1)
        self.assertTrue(any("counts as sent" in line for line in d.logged))

    def test_post_tailed_before_a_restart_is_nudged_by_the_next_daemon(self):
        """M5 ND-12: the old daemon tailed the post (watermark saved) and died inside the stable window; the post must not be lost."""
        d1, api1, clock1 = make_daemon(self.ts)
        d1.on_connected()
        seq = post(self.ts, "alpha-reviewer")
        d1.tick()  # ingested and held (done_hold 60 s); notifier/state.json now resumes after seq
        self.assertIn("alpha-reviewer", d1.teams["alpha"].pending)
        self.assertEqual([m for m, _ in api1.calls if m == "agent.prompt"], [])
        del d1
        d2, api2, clock2 = make_daemon(self.ts)
        d2.on_connected()
        d2.tick()
        self.assertTrue(any("rebuilt pending for alpha-reviewer: [{}] (never nudged)".format(seq) in line for line in d2.logged), d2.logged)
        for _ in range(16):
            clock2.advance(5)
            d2.tick()
        prompts = [p for m, p in api2.calls if m == "agent.prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertIn("(seq {})".format(seq), prompts[0]["text"])
        self.assertEqual(d2.teams["alpha"].ledger.counts()["intents"], 1)

    def test_landed_unread_post_keeps_its_landing_across_a_restart(self):
        """A nudged-but-unread post is not re-nudged right after a restart; the re-nudge schedule applies."""
        doc = store.read_json(self.ts.team.team_json)
        doc["config"] = {"gate": {"done_hold_ms": 0}}
        store.write_json(self.ts.team.team_json, doc)
        d1, api1, clock1 = make_daemon(self.ts)
        api1.set_response("agent.explain", STRONG_IDLE)
        d1.on_connected()
        seq = post(self.ts, "alpha-reviewer")
        for _ in range(6):
            clock1.advance(2.5)
            d1.tick()
        self.assertEqual(len([m for m, _ in api1.calls if m == "agent.prompt"]), 1)
        attempt_id = d1.teams["alpha"].pending["alpha-reviewer"].attempt_id
        del d1
        d2, api2, clock2 = make_daemon(self.ts)
        api2.set_response("agent.explain", STRONG_IDLE)
        d2.on_connected()
        for _ in range(12):
            clock2.advance(5)
            d2.tick()
        self.assertEqual([m for m, _ in api2.calls if m == "agent.prompt"], [], d2.logged)
        pending = d2.teams["alpha"].pending["alpha-reviewer"]
        self.assertIsNotNone(pending.landed_ms)
        self.assertEqual(pending.attempt_id, attempt_id)
        self.assertEqual(pending.seqs, [seq])
        self.assertTrue(any("rebuilt pending for alpha-reviewer" in line and "unread" in line for line in d2.logged))

    def test_wrong_target_is_refused_and_counted(self):
        d, api, clock = make_daemon(self.ts)
        d.on_connected()
        team = d.teams["alpha"]
        # the cached row for the reviewer suddenly names the owner's terminal
        d.agents["term_r1"] = fake_agent("wA:p6", "term_owner", "claude", None)
        result = d.deliver("alpha-reviewer", ["[herdr-team probe 1]"], [])
        self.assertEqual(result, "wrong_occupant")
        self.assertNotIn("agent.prompt", [m for m, _ in api.calls])
        self.assertEqual(d.counters["wrong_target"], 1)
        self.assertEqual(team.ledger.counts()["wrong_target"], 1)
        with self.assertRaises(HerdrTeamError):
            d.deliver("nobody", ["x"], [])


# --------------------------------------------------------------------------
# notification queue


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.scan_teams(force=True)
        self.answers = []
        self.api.set_response("notification.show", self.answer)

    def answer(self, params):
        reason = self.answers.pop(0) if self.answers else "shown"
        if isinstance(reason, Exception):
            raise reason
        return {"type": "notification_show", "shown": reason == "shown", "reason": reason}

    def shows(self):
        return [p for m, p in self.api.calls if m == "notification.show"]

    def attention(self):
        raw = store.read_bytes(self.ts.team.human_attention) or b""
        return [json.loads(l) for l in raw.split(b"\n") if l.strip()]

    def test_busy_and_rate_limited_retry_after_1_1s(self):
        self.answers = ["busy", "rate_limited", "shown"]
        self.d.enqueue_toast("alpha", [3], "title", "body")
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 1)
        self.assertEqual(self.d.notifications[0].reason, "busy")
        self.clock.advance(1.05)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 1)  # global one-per-second and the 1.1 s retry hold it
        self.clock.advance(0.1)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 2)
        self.clock.advance(1.2)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 3)
        self.assertEqual(self.d.notifications, [])
        self.assertEqual(self.attention()[-1]["reason"], "shown")
        self.assertTrue(self.attention()[-1]["shown"])
        self.assertEqual(self.d.counters["toasts"], 1)

    def test_busy_gives_up_after_30s_and_mirrors(self):
        self.answers = ["busy"] * 40
        self.d.enqueue_toast("alpha", [3], "title", "body")
        for _ in range(40):
            self.d.process_notifications()
            self.clock.advance(1.2)
        self.assertEqual(self.d.notifications, [])
        entry = self.attention()[-1]
        self.assertEqual((entry["reason"], entry["shown"]), ("busy", False))
        self.assertLess(len(self.shows()), 30)
        records = store.BoardStore(self.ts.team).read()
        self.assertTrue(any(r.get("event") == "toast" for r in records))

    def test_disabled_stops_everything(self):
        self.answers = ["disabled"]
        self.d.enqueue_toast("alpha", [1], "one", "b")
        self.d.enqueue_toast("alpha", [], "roster", "b", kind="roster")
        self.d.process_notifications()
        self.assertTrue(self.d.toasts_disabled)
        self.assertEqual(self.d.notifications, [])
        self.assertEqual(len(self.shows()), 1)
        self.d.enqueue_toast("alpha", [2], "two", "b")
        self.clock.advance(5)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 1)
        self.assertEqual([e["reason"] for e in self.attention()], ["disabled", "disabled", "disabled"])
        self.assertEqual(self.d.build_who()["toasts"], "disabled")

    def test_disabled_latch_lifts_when_the_config_turns_toasts_back_on(self):
        # HP-14 (2026-09-05): after one `disabled` verdict the daemon never called notification.show
        # again, so `delivery = "terminal"` plus `herdr server reload-config` still mirrored `disabled`.
        self.answers = ["disabled", "shown"]
        self.d.enqueue_toast("alpha", [1], "one", "b")
        self.d.process_notifications()
        self.assertTrue(self.d.toasts_disabled)
        self.assertEqual(len(self.shows()), 1)
        (self.ts.config_dir / "config.toml").write_text('[ui.toast]\ndelivery = "terminal"\n', encoding="utf-8")
        self.d.enqueue_toast("alpha", [2], "two", "b")
        self.clock.advance(2)
        self.d.process_notifications()
        self.assertFalse(self.d.toasts_disabled)
        self.assertEqual(len(self.shows()), 2)
        self.assertEqual([e["reason"] for e in self.attention()], ["disabled", "shown"])
        self.assertTrue(any("toast delivery changed to 'terminal'" in line for line in self.d.logged), self.d.logged)

    def test_disabled_latch_is_rechecked_after_60s(self):
        self.answers = ["disabled", "disabled", "shown"]
        self.d.enqueue_toast("alpha", [1], "one", "b")
        self.d.process_notifications()
        self.d.enqueue_toast("alpha", [2], "two", "b")
        self.clock.advance(30)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 1)  # still latched inside the window
        self.d.enqueue_toast("alpha", [3], "three", "b")
        self.clock.advance(31)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 2)  # one probe after 60 s; still disabled -> latched again
        self.assertTrue(self.d.toasts_disabled)
        self.d.enqueue_toast("alpha", [4], "four", "b")
        self.clock.advance(61)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 3)
        self.assertFalse(self.d.toasts_disabled)
        self.assertEqual([e["reason"] for e in self.attention()], ["disabled", "disabled", "disabled", "shown"])

    def test_no_foreground_client_retries_every_15s_and_on_pane_focused(self):
        self.answers = ["no_foreground_client", "no_foreground_client", "shown"]
        self.d.enqueue_toast("alpha", [7], "title", "body")
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 1)
        self.clock.advance(14)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 1)
        self.clock.advance(1.5)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 2)
        self.d.handle_event({"event": "pane_focused", "data": {"pane_id": "w2:p1"}})
        self.clock.advance(1.1)
        self.d.process_notifications()
        self.assertEqual(len(self.shows()), 3)
        self.assertEqual(self.d.notifications, [])

    def test_posts_to_human_are_coalesced_with_seq_first(self):
        for i in range(3):
            post(self.ts, "human", "hello {}".format(i), kind="request" if i == 0 else "note")
        self.d.on_connected()
        self.d.tail_boards()
        self.d.process_notifications()
        shows = self.shows()
        self.assertEqual(len(shows), 1)
        self.assertRegex(shows[0]["title"], r"^3 new posts for you #\d+-#\d+$")
        self.assertRegex(shows[0]["body"], r"^#\d+ ")
        self.assertEqual(shows[0]["sound"], "request")
        self.assertLessEqual(len(shows[0]["title"]), 80)

    def test_single_post_title_budget(self):
        post(self.ts, "human", "x" * 300, kind="done")
        self.d.on_connected()
        self.d.tail_boards()
        self.d.process_notifications()
        show = self.shows()[0]
        self.assertLessEqual(len(show["title"]), 80)
        self.assertTrue(show["title"].startswith("#"))
        self.assertEqual(show["sound"], "done")

    def test_transport_error_is_retried_then_mirrored(self):
        self.answers = [FakeError("server_not_running", "down")] * 5
        self.d.enqueue_toast("alpha", [1], "t", "b")
        self.d.process_notifications()
        self.assertEqual(self.d.notifications[0].reason, "server_not_running")
        self.clock.advance(31)
        self.d.process_notifications()
        self.assertEqual(self.d.notifications, [])
        self.assertEqual(self.attention()[-1]["reason"], "server_not_running")


# --------------------------------------------------------------------------
# jobs


class JobTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()
        self.api.set_response("agent.focus", {"type": "ok"})

    def job(self, kind, member="alpha-reviewer", **extra):
        obj = {"v": 1, "kind": kind, "member": member, "force": False, "requested_by": {"name": "human"}, "requested_at": "now"}
        obj.update(extra)
        path = self.ts.team.jobs_dir / "{}-{}.json".format(int(time.time() * 1000), kind)
        store.write_json(path, obj)
        return path

    def consume(self):
        self.clock.advance(1)
        self.d.consume_jobs(self.clock() * 1000)

    def test_brief_job_queues_a_briefing_and_writes_the_file(self):
        path = self.job("brief")
        self.consume()
        self.assertFalse(path.exists())
        pending = self.d.teams["alpha"].pending["alpha-reviewer"]
        self.assertEqual(pending.kind, "brief")
        self.assertEqual(len(pending.lines), 1)
        self.assertTrue(pending.lines[0].startswith('[herdr-team briefing] You are "alpha-reviewer" (reviewer) in team "alpha": Find and fix the bug'))
        self.assertTrue("alpha-worker (worker)" in pending.lines[0] or "1 teammate, run herdr-team who" in pending.lines[0], pending.lines[0])
        self.assertLessEqual(len(pending.lines[0]), 400)
        self.assertEqual(self.ts.team.briefing("alpha-reviewer").read_text().strip(), pending.lines[0])
        self.assertEqual(self.d.counters["jobs"], 1)

    def test_posts_during_a_briefing_are_nudged_after_the_ack(self):
        # HP-10 (2026-09-05): a post to a member whose briefing was pending was dropped by _add_pending
        # ("the nudge follows on the next read") and never nudged: the ack cleared the pending and
        # nothing re-queued the post, so the second role holder never heard of #18.
        post(self.ts, "all", text="filler so the briefing has a board seq to ack")
        self.job("brief")
        self.consume()
        team = self.d.teams["alpha"]
        self.assertEqual(team.pending["alpha-reviewer"].kind, "brief")
        # the briefing lands (stable window only) ...
        for _ in range(4):
            self.clock.advance(1)
            self.d.tick()
        pending = team.pending["alpha-reviewer"]
        self.assertEqual(len([p for m, p in self.api.calls if m == "agent.prompt"]), 1, self.d.logged)
        self.assertIsNotNone(pending.landed_ms)
        brief_seq = team.rt("alpha-reviewer").brief_seq
        # ... a post arrives before the ack: kept on the briefing, not dropped
        seq = post(self.ts, "alpha-reviewer")
        self.clock.advance(1)
        self.d.tick()
        self.assertIs(team.pending["alpha-reviewer"], pending)
        self.assertEqual((pending.kind, pending.seqs, pending.deferred_seqs, pending.deferred_authors), ("brief", [], [seq], {"alpha-worker"}))
        # the member acks the briefing (cursor at the briefing's seq, below the new post)
        store.Cursors(self.ts.team).advance("alpha-reviewer", brief_seq, "term_r1", "cli")
        self.clock.advance(1)
        self.d.tick()
        nudge = team.pending["alpha-reviewer"]
        self.assertEqual((nudge.kind, nudge.seqs, nudge.authors, nudge.attempts), ("nudge", [seq], {"alpha-worker"}, 0))
        self.assertTrue(any("deferred during the briefing" in line for line in self.d.logged), self.d.logged)

    def test_deferred_post_already_read_or_retracted_is_not_requeued(self):
        self.job("brief")
        self.consume()
        team = self.d.teams["alpha"]
        first = post(self.ts, "alpha-reviewer", text="read with the briefing")
        second = post(self.ts, "alpha-reviewer", text="retracted meanwhile")
        self.clock.advance(1)
        self.d.tick()
        self.assertEqual(team.pending["alpha-reviewer"].deferred_seqs, [first, second])
        store.BoardStore(self.ts.team).append({"from": "alpha-worker", "from_kind": "claude", "from_pane": "w2:p2", "from_terminal": "term_w1", "from_gen": 1,
                                                "origin": {"via": "cli", "verified": True}, "to": ["alpha-reviewer"], "kind": "retract", "retracts": second, "text": ""})
        for _ in range(4):
            self.clock.advance(1)
            self.d.tick()
        self.assertEqual(team.pending["alpha-reviewer"].deferred_seqs, [first])
        brief_seq = team.rt("alpha-reviewer").brief_seq
        store.Cursors(self.ts.team).advance("alpha-reviewer", brief_seq, "term_r1", "cli")  # the ack covered every post
        self.clock.advance(1)
        self.d.tick()
        self.assertNotIn("alpha-reviewer", team.pending)

    def test_brief_job_carries_the_role_brief_as_a_second_line(self):
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0]["brief"] = "Review every patch for regressions."
        store.write_json(self.ts.team.team_json, doc)
        self.d.scan_teams(force=True)
        self.job("brief")
        self.consume()
        lines = self.d.teams["alpha"].pending["alpha-reviewer"].lines
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("[herdr-team briefing] Your brief: Review every patch for regressions"))

    def test_nudge_job_with_nothing_unread_is_dropped(self):
        self.job("nudge")
        self.consume()
        self.assertNotIn("alpha-reviewer", self.d.teams["alpha"].pending)
        self.assertTrue(any("nudge job dropped" in line for line in self.d.logged))

    def test_nudge_job_with_unread_posts_and_force(self):
        seq = post(self.ts, "alpha-reviewer")
        self.job("nudge", force=True)
        self.consume()
        pending = self.d.teams["alpha"].pending["alpha-reviewer"]
        self.assertEqual(pending.seqs, [seq])
        self.assertTrue(pending.force)

    def test_focus_job_calls_agent_focus(self):
        self.job("focus")
        self.consume()
        self.assertIn(("agent.focus", {"target": "w2:p1"}), self.api.calls)

    def test_mute_job_writes_mute_json(self):
        self.job("mute", until="2099-01-01T00:00:00Z")
        self.consume()
        self.assertEqual(store.read_json(self.ts.team.mute_json), {"alpha-reviewer": "2099-01-01T00:00:00Z"})
        self.job("mute", member="*", until=None)
        self.consume()
        self.assertEqual(store.read_json(self.ts.team.mute_json), {"alpha-reviewer": "2099-01-01T00:00:00Z", "*": None})
        self.job("mute", unmute=True)
        self.consume()
        self.assertEqual(store.read_json(self.ts.team.mute_json), {"*": None})

    def test_unknown_member_and_garbage_files_are_skipped(self):
        self.job("brief", member="ghost")
        (self.ts.team.jobs_dir / "junk.json").write_text("{not json")
        (self.ts.team.jobs_dir / ".hidden.json").write_text("{}")
        self.consume()
        self.assertEqual(self.d.teams["alpha"].pending, {})
        self.assertFalse((self.ts.team.jobs_dir / "junk.json").exists())
        self.assertTrue((self.ts.team.jobs_dir / ".hidden.json").exists())

    def test_probe_job_lands_and_records_kinds_json(self):
        self.job("probe", nonce=4242, agent_kind="codex")
        self.consume()
        pending = self.d.teams["alpha"].pending["alpha-reviewer"]
        self.assertEqual(pending.kind, "probe")
        self.assertEqual(pending.lines, ["[herdr-team probe 4242]"])
        for _ in range(3):
            self.clock.advance(5)
            self.d.tick()
        prompts = [p for m, p in self.api.calls if m == "agent.prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["text"], "[herdr-team probe 4242]")
        kinds = store.read_json(self.ts.session.kinds_json)
        self.assertEqual(kinds["codex"]["probe"]["nonce"], 4242)
        self.assertTrue(kinds["codex"]["probe"]["ok"])
        self.assertNotIn("alpha-reviewer", self.d.teams["alpha"].pending)


# --------------------------------------------------------------------------
# board tail, gate, delivery


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()

    def prompts(self):
        return [p for m, p in self.api.calls if m == "agent.prompt"]

    def run_ticks(self, count, step=5):
        for _ in range(count):
            self.clock.advance(step)
            self.d.tick()

    def test_directed_post_is_nudged_once_after_the_windows(self):
        seq = post(self.ts, "alpha-reviewer")
        self.d.tick()
        pending = self.d.teams["alpha"].pending["alpha-reviewer"]
        self.assertEqual(pending.seqs, [seq])
        self.assertEqual(pending.authors, {"alpha-worker"})
        # done_hold (60 s) keeps the nudge back even though the agent is idle and stable
        self.run_ticks(6)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(pending.hold, gate.HOLD_DONE_HOLD)
        self.run_ticks(8)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["target"], "w2:p1")
        self.assertEqual(prompts[0]["wait"], {"until": ["working", "blocked"], "timeout_ms": 8000})
        self.assertRegex(prompts[0]["text"], r"^\[herdr-team nudge\] 1 new board post for alpha-reviewer \(seq {}\)\. Run: herdr-team board --new \[n\d+\]$".format(seq))
        self.assertLessEqual(len(prompts[0]["text"]), 120)
        counts = self.d.teams["alpha"].ledger.counts()
        self.assertEqual((counts["intents"], counts["landed_working"], counts["wrong_target"]), (1, 1, 0))
        records = store.BoardStore(self.ts.team).read()
        nudged = [r for r in records if r.get("event") == "nudged"]
        self.assertEqual(len(nudged), 1)
        self.assertEqual(nudged[0]["to"], ["alpha-reviewer"])
        self.assertEqual(nudged[0]["seqs"], [seq])
        self.assertIsNotNone(pending.landed_ms)
        # the prompt was a whole-daemon single call: no second nudge within the interval
        self.run_ticks(3)
        self.assertEqual(len(self.prompts()), 1)
        who = store.read_json(self.ts.session.who_json)
        member = [m for m in who["teams"]["alpha"]["members"] if m["name"] == "alpha-reviewer"][0]
        self.assertEqual(member["pending_nudges"], 1)
        self.assertEqual(who["counters"]["wrong_target"], 0)

    def test_read_before_nudge_drops_the_pending_work(self):
        seq = post(self.ts, "alpha-reviewer")
        self.d.tick()
        store.Cursors(self.ts.team).advance("alpha-reviewer", seq, "term_r1", "cli")
        self.run_ticks(14)
        self.assertEqual(self.prompts(), [])
        self.assertNotIn("alpha-reviewer", self.d.teams["alpha"].pending)

    def test_cursor_advance_after_landing_records_the_outcome(self):
        seq = post(self.ts, "alpha-reviewer")
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(len(self.prompts()), 1)
        store.Cursors(self.ts.team).advance("alpha-reviewer", seq, "term_r1", "cli")
        self.run_ticks(2)
        counts = self.d.teams["alpha"].ledger.counts()
        self.assertEqual((counts["outcomes"], counts["cursor_advanced"]), (1, 1))
        self.assertNotIn("alpha-reviewer", self.d.teams["alpha"].pending)

    def test_working_member_is_never_prompted(self):
        post(self.ts, "alpha-worker", author="alpha-reviewer")  # the worker is ``working`` in FAKE_AGENTS
        self.d.tick()
        self.run_ticks(20)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.d.teams["alpha"].pending["alpha-worker"].hold, gate.HOLD_NOT_IDLE)

    def test_human_broadcast_nudges_every_member(self):
        seq = post(self.ts, "all", author="human")
        self.d.tick()
        pending = self.d.teams["alpha"].pending
        self.assertEqual(sorted(pending), ["alpha-reviewer", "alpha-worker"])
        self.assertEqual((pending["alpha-reviewer"].seqs, pending["alpha-reviewer"].urgent), ([seq], False))
        # a cold start rebuilds the same work from the board
        d2, _api2, _clock2 = make_daemon(self.ts)
        d2.on_connected()
        team2 = d2.teams["alpha"]
        team2.pending.clear()
        team2.watermark = seq
        d2._rebuild_pending(team2)
        self.assertEqual(sorted(team2.pending), ["alpha-reviewer", "alpha-worker"])

    def test_agent_broadcast_nudges_nobody_unless_urgent(self):
        post(self.ts, "all")
        self.d.tick()
        self.assertEqual(self.d.teams["alpha"].pending, {})
        post(self.ts, "all", urgent=True, author="alpha-reviewer")
        self.d.tick()
        self.assertEqual(sorted(self.d.teams["alpha"].pending), ["alpha-worker"])
        self.assertTrue(self.d.teams["alpha"].pending["alpha-worker"].urgent)

    def test_author_is_never_nudged_for_own_post(self):
        post(self.ts, "alpha-worker", author="alpha-worker")
        self.d.tick()
        self.assertEqual(self.d.teams["alpha"].pending, {})

    def test_retract_cancels_a_pending_nudge(self):
        seq = post(self.ts, "alpha-reviewer")
        self.d.tick()
        self.assertIn("alpha-reviewer", self.d.teams["alpha"].pending)
        store.BoardStore(self.ts.team).append({"from": "alpha-worker", "to": ["alpha-reviewer"], "kind": "retract", "retracts": seq, "text": "retracted", "origin": {"via": "cli", "verified": True}})
        self.run_ticks(14)
        self.assertEqual(self.prompts(), [])
        self.assertNotIn("alpha-reviewer", self.d.teams["alpha"].pending)
        events = [r.get("event") for r in store.BoardStore(self.ts.team).read()]
        self.assertIn("retracted", events)

    def test_landed_in_turn_backs_off_and_retries(self):
        seq = post(self.ts, "alpha-reviewer")
        self.api.set_response("agent.prompt", lambda params: {"type": "agent_prompted", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", status="working", state_change_seq=1)})
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(len(self.prompts()), 1)
        counts = self.d.teams["alpha"].ledger.counts()
        self.assertEqual(counts["landed_in_turn"], 1)
        pending = self.d.teams["alpha"].pending["alpha-reviewer"]
        self.assertIsNone(pending.landed_ms)
        self.assertEqual(pending.transient_failures, 1)
        self.assertEqual(pending.seqs, [seq])

    def test_wrong_occupant_response_is_classified(self):
        post(self.ts, "alpha-reviewer")
        self.api.set_response("agent.prompt", lambda params: {"type": "agent_prompted", "agent": fake_agent("w2:p1", "term_other", "codex", "alpha-reviewer", status="working", state_change_seq=9)})
        self.d.tick()
        self.run_ticks(14)
        counts = self.d.teams["alpha"].ledger.counts()
        self.assertGreaterEqual(counts["wrong_occupant"], 1)
        self.assertEqual(counts["landed_working"], 0)
        pending = self.d.teams["alpha"].pending["alpha-reviewer"]
        self.assertGreaterEqual(pending.transient_failures, 1)
        self.assertIsNone(pending.landed_ms)

    def test_hung_prompt_marks_pane_stuck(self):
        post(self.ts, "alpha-reviewer")
        self.api.set_error("agent.prompt", "herdr_timeout", "timed out")
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(self.d.teams["alpha"].ledger.counts()["hung"], 1)
        rt = self.d.teams["alpha"].rt("alpha-reviewer")
        self.assertIsNotNone(rt.pane_stuck_until_ms)
        self.run_ticks(2)
        self.assertEqual(len(self.prompts()), 1)

    def test_dry_nudge_never_calls_agent_prompt(self):
        d, api, clock = make_daemon(self.ts, env=self.ts.env_with(HERDR_TEAM_DRY_NUDGE="1"))
        d.on_connected()
        post(self.ts, "alpha-reviewer")
        d.tick()
        for _ in range(14):
            clock.advance(5)
            d.tick()
        self.assertNotIn("agent.prompt", [m for m, _ in api.calls])
        self.assertEqual(d.teams["alpha"].ledger.counts()["dry"], 1)
        self.assertTrue(any("DRY nudge" in line for line in d.logged))

    def test_agent_get_recheck_is_logged_before_the_send(self):
        """M5 ND-01: daemon.log shows the fresh ``agent.get`` seq re-check immediately before the (dry) send."""
        d, api, clock = make_daemon(self.ts, env=self.ts.env_with(HERDR_TEAM_DRY_NUDGE="1"))
        d.on_connected()
        post(self.ts, "alpha-reviewer")
        d.tick()
        for _ in range(14):
            clock.advance(5)
            d.tick()
        rechecks = [i for i, line in enumerate(d.logged) if "alpha-reviewer agent.get re-check: state_change_seq" in line]
        sends = [i for i, line in enumerate(d.logged) if "DRY nudge to alpha-reviewer" in line]
        self.assertEqual(len(sends), 1, d.logged)
        self.assertTrue(rechecks, d.logged)
        self.assertLess(rechecks[-1], sends[0])
        self.assertNotIn("agent.get re-check", "\n".join(d.logged[rechecks[-1] + 1:sends[0]]).replace(d.logged[rechecks[-1]], ""))
        self.assertIn("(idle)", d.logged[rechecks[-1]])
        self.assertIn("agent.get", [m for m, _ in api.calls])

    def test_muted_member_is_held(self):
        store.write_json(self.ts.team.mute_json, {"alpha-reviewer": "2099-01-01T00:00:00Z"})
        post(self.ts, "alpha-reviewer")
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_MUTED)

    def test_unverified_kind_is_held_and_toasted_once(self):
        d, api, clock = make_daemon(self.ts, trust=False)
        d.on_connected()
        post(self.ts, "alpha-reviewer")
        d.tick()
        for _ in range(14):
            clock.advance(5)
            d.tick()
        self.assertNotIn("agent.prompt", [m for m, _ in api.calls])
        self.assertEqual(d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_KIND_UNVERIFIED)
        shows = [p for m, p in api.calls if m == "notification.show"]
        self.assertEqual(len(shows), 1)
        self.assertIn("unverified", shows[0]["body"])

    def test_dialog_on_screen_holds(self):
        post(self.ts, "alpha-reviewer")
        self.api.set_response("agent.read", fake_read("w2:p1", "› \n\nAllow command? y/n  esc to cancel\n", "detection"))
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_DIALOG)

    def test_detection_text_reads_the_pane_read_result_shape(self):
        """``agent.read`` answers ``{"type":"pane_read","read":{"text":...}}``; the daemon must read ``read.text``."""
        self.api.set_response("agent.read", fake_read("w2:p1", "› draft in the box\n", "detection"))
        self.assertEqual(self.d._detection_text("w2:p1"), "› draft in the box\n")
        self.assertEqual([p["source"] for m, p in self.api.calls if m == "agent.read"], ["detection"])
        # gate 9 sees the draft through the same read: the nudge is held as draft_present
        post(self.ts, "alpha-reviewer")
        self.d.tick()
        self.run_ticks(14)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_DRAFT_PRESENT)

    def test_stalled_prompt_reads_the_prompt_line_from_the_pane_read_shape(self):
        """``agent_prompt_stalled`` with the text still on the prompt line is ``not_submitted``, else a fast turn."""
        post(self.ts, "alpha-reviewer")
        self.api.set_error("agent.prompt", "agent_prompt_stalled", "no transition")
        sent = []

        def read(params):
            text = sent[0][:20] if sent else ""
            return fake_read("w2:p1", "› {}\n".format(text), str(params.get("source") or "detection"))

        original = self.d.api.request

        def request(method, params=None, timeout=None):
            if method == "agent.prompt":
                sent.append(str((params or {}).get("text")))
            return original(method, params, timeout)

        self.d.api.request = request
        self.api.set_response("agent.read", read)
        self.d.tick()
        self.run_ticks(14)
        counts = self.d.teams["alpha"].ledger.counts()
        self.assertEqual(counts["not_submitted"], 1, counts)
        self.assertTrue(any("text still on the prompt line" in line for line in self.d.logged), self.d.logged[-5:])

    def test_stability_resets_after_a_sample_gap(self):
        post(self.ts, "alpha-reviewer")
        self.d.tick()
        self.run_ticks(1, step=15)
        self.assertTrue(any("sample gap" in line for line in self.d.logged))
        self.assertEqual(self.prompts(), [])
        self.run_ticks(2)
        self.assertEqual(self.d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_DONE_HOLD)


class HeartbeatAndWhoTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)

    def stamps(self):
        return [p for m, p in self.api.calls if m == "pane.report_metadata"]

    def test_heartbeat_restamps_identity_without_ttl_and_task_with_ttl(self):
        post(self.ts, "human", "review the diff for the login change please", author="alpha-reviewer", kind="request")
        self.d.on_connected()
        self.d.tick()
        roster = [p for p in self.stamps() if p["source"] == "herdr-team:roster"]
        task = [p for p in self.stamps() if p["source"] == "herdr-team:task"]
        self.assertTrue(roster)
        self.assertNotIn("ttl_ms", roster[0])
        self.assertEqual(roster[0]["tokens"], identity_tokens("alpha", "reviewer", 1))
        self.assertEqual(task[0]["ttl_ms"], 120000)
        self.assertTrue(task[0]["tokens"]["team_task"].startswith("→ "))
        self.assertLessEqual(len(task[0]["tokens"]["team_task"]), 80)
        before = len(self.stamps())
        self.clock.advance(10)
        self.d.tick()
        self.assertEqual(len(self.stamps()), before)  # not yet 30 s
        self.clock.advance(21)
        self.d.tick()
        self.assertGreater(len(self.stamps()), before)
        info = D.read_daemon_info(self.ts.session)
        self.assertEqual(info.pid, os.getpid())

    def test_heartbeat_stamps_team_task_from_the_task_file(self):
        """RS-01/RS-04 regression: ``herdr-team task`` writes ``tasks/<name>.json``; the heartbeat must read it."""
        post(self.ts, "human", "review the diff for the login change please", author="alpha-reviewer", kind="request")
        tasks_dir = self.ts.team.root / "tasks"
        tasks_dir.mkdir(mode=0o700, exist_ok=True)
        store.write_json(tasks_dir / "alpha-reviewer.json", {"v": 1, "member": "alpha-reviewer", "text": "rig smoke", "headline": "rig smoke", "set_at": D.now_iso()})
        self.d.on_connected()
        self.d.tick()
        task = [p for p in self.stamps() if p["source"] == "herdr-team:task" and p["pane_id"] == "w2:p1"]
        self.assertTrue(task, self.stamps())
        self.assertEqual(task[0]["tokens"], {"team_task": "rig smoke"})  # the task beats the last post headline
        self.assertEqual(task[0]["ttl_ms"], 120000)
        # an unsafe member name or a missing file is no task
        self.assertIsNone(D.read_task_file(self.ts.team, "../etc"))
        self.assertIsNone(D.read_task_file(self.ts.team, "nobody"))
        # a stale task (older than 30 min) falls back to the last post headline
        store.write_json(tasks_dir / "alpha-reviewer.json", {"v": 1, "member": "alpha-reviewer", "text": "old task", "set_at": "2020-01-01T00:00:00.000Z"})
        self.assertTrue(D.task_headline({"name": "alpha-reviewer"}, {"kind": "request", "text": "review the diff"}, time.time(), task=D.read_task_file(self.ts.team, "alpha-reviewer")).startswith("→ "))

    def test_who_json_is_coalesced_to_once_per_second(self):
        self.d.on_connected()
        self.d.tick()
        who_json = self.ts.session.who_json
        sentinel = 1_000_000_000  # 1970-01-01T00:00:01; a rewrite cannot keep it (kernel mtime ticks are too coarse to compare two writes)
        os.utime(who_json, ns=(sentinel, sentinel))
        self.d.who_dirty = True
        self.d.tick()
        self.assertEqual(who_json.stat().st_mtime_ns, sentinel)  # coalesced: not rewritten within the second
        self.clock.advance(1.1)
        self.d.who_dirty = True
        self.d.tick()
        self.assertNotEqual(who_json.stat().st_mtime_ns, sentinel)  # rewritten
        who = store.read_json(self.ts.session.who_json)
        self.assertEqual(who["v"], 1)
        self.assertEqual(who["default_team"], "alpha")
        self.assertEqual(who["charters"]["alpha"]["seq"], 1)
        names = sorted(m["name"] for m in who["teams"]["alpha"]["members"])
        self.assertEqual(names, ["alpha-reviewer", "alpha-worker", "human"])
        reviewer = [m for m in who["teams"]["alpha"]["members"] if m["name"] == "alpha-reviewer"][0]
        self.assertEqual(reviewer["agent_status"], "idle")
        self.assertTrue(reviewer["charter_stale"])

    def test_reconcile_marks_missing_after_grace_and_rebinds(self):
        self.api.set_response("agent.list", {"type": "agent_list", "agents": [dict(FAKE_AGENTS[1]), dict(FAKE_AGENTS[2])]})
        self.d.on_connected()
        self.assertEqual(self.d.teams["alpha"].member("alpha-reviewer")["status"], "active")  # grace window
        self.clock.advance(31)
        self.d.reconcile_due = True
        self.d.tick()
        self.assertEqual(self.d.teams["alpha"].member("alpha-reviewer")["status"], "missing")
        gone = [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "member_gone"]
        self.assertEqual(len(gone), 1)
        # the agent comes back on the same pane with a new terminal id and no name: rebound, renamed
        self.api.set_response("agent.list", {"type": "agent_list", "agents": [fake_agent("w2:p1", "term_r2", "codex", None), dict(FAKE_AGENTS[1]), dict(FAKE_AGENTS[2])]})
        self.api.set_response("agent.rename", {"type": "ok"})
        self.api.set_response("pane.rename", {"type": "ok"})
        self.d.handle_event({"event": "pane_agent_detected", "data": {"pane_id": "w2:p1"}})
        self.clock.advance(1)
        self.d.tick()
        member = self.d.teams["alpha"].member("alpha-reviewer")
        self.assertEqual((member["status"], member["terminal_id"], member["generation"]), ("active", "term_r2", 2))
        self.assertIn(("agent.rename", {"target": "w2:p1", "name": "alpha-reviewer"}), self.api.calls)
        self.assertEqual(store.read_json(self.ts.session.pane_record("term_r2"))["gen"], 2)
        restarted = [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "member_restarted"]
        self.assertEqual(len(restarted), 1)

    def test_pane_closed_event_marks_member_missing(self):
        self.d.on_connected()
        self.d.handle_event({"event": "pane_exited", "data": {"pane_id": "w2:p2"}})
        self.assertEqual(self.d.teams["alpha"].member("alpha-worker")["status"], "missing")
        cleared = [p for m, p in self.api.calls if m == "pane.report_metadata" and p["pane_id"] == "w2:p2" and p["tokens"].get("team") is None]
        self.assertTrue(cleared)

    def test_pane_closed_event_records_a_gone_console_as_closed(self):
        """UI-05: the event carries a pane id (stale after a move); the tick checks the console terminal in pane.list."""
        from support import fake_pane

        self.d.on_connected()
        store.write_json(self.ts.session.console_json, {"pane_id": "w3:p2", "terminal_id": "term_console", "pid": os.getpid(), "open": True, "default_team": "alpha"})
        # a move kept the terminal: still open
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w4:p1", "term_console", None, "Team console"), fake_pane("w1:p1", "term_shell")]})
        self.d.handle_event({"event": "pane_closed", "data": {"pane_id": "w1:p7"}})
        self.assertTrue(self.d.console_check_due)
        self.d.tick()
        self.assertFalse(self.d.console_check_due)
        self.assertTrue(store.read_json(self.ts.session.console_json)["open"])
        # plugin pane close: the terminal is gone while the server answers
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w1:p1", "term_shell")]})
        self.d.handle_event({"event": "pane_closed", "data": {"pane_id": "w4:p1"}})
        self.d.tick()
        doc = store.read_json(self.ts.session.console_json)
        self.assertEqual((doc["open"], doc["pid"]), (False, None))
        self.assertTrue(any("console pane closed" in line for line in self.d.logged))

    def test_rebind_to_another_pane_clears_the_label_left_on_the_old_shell(self):
        """RT-02: a member rebound by name in a new pane must not leave ``team:alpha/worker`` on its old pane."""
        from support import fake_pane

        self.api.set_response("agent.list", {"type": "agent_list", "agents": [dict(FAKE_AGENTS[0]), dict(FAKE_AGENTS[2])]})
        self.d.on_connected()
        self.clock.advance(31)
        self.d.reconcile_due = True
        self.d.tick()
        self.assertEqual(self.d.teams["alpha"].member("alpha-worker")["status"], "missing")
        # alpha-worker comes back under its exact name in a fresh pane; its old pane w2:p2 is a labelled shell now
        self.api.set_response("agent.list", {"type": "agent_list", "agents": [fake_agent("w5:p1", "term_w9", "claude", "alpha-worker"), dict(FAKE_AGENTS[0]), dict(FAKE_AGENTS[2])]})
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w2:p2", "term_shell2", None, "team:alpha/worker"), fake_pane("w5:p1", "term_w9", "claude", None)]})
        self.api.set_response("agent.rename", {"type": "ok"})
        self.api.set_response("pane.rename", {"type": "ok"})
        self.d.handle_event({"event": "pane_agent_detected", "data": {"pane_id": "w5:p1"}})
        self.clock.advance(1)
        self.d.tick()
        member = self.d.teams["alpha"].member("alpha-worker")
        self.assertEqual((member["status"], member["pane_id"], member["terminal_id"]), ("active", "w5:p1", "term_w9"))
        renames = [p for m, p in self.api.calls if m == "pane.rename"]
        self.assertIn({"pane_id": "w5:p1", "label": "team:alpha/worker"}, renames)
        self.assertIn({"pane_id": "w2:p2", "label": None}, renames)

    def test_connect_schedules_a_deferred_console_pass_that_closes_the_dead_shell(self):
        """RT-05: the startup hook's pass saw an empty foreground; the daemon retries 3 s after connect until known."""
        from support import fake_pane

        store.write_json(self.ts.session.console_json, {"pane_id": "w3:p6", "terminal_id": "term_old", "pid": 999999, "open": True, "default_team": "alpha"})
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w3:p6", "term_shell6", None, "Team console"), fake_pane("w1:p1", "term_shell")]})
        foreground = {"procs": []}
        self.api.set_response("pane.process_info", lambda params: {"type": "pane_process_info", "process_info": {"pane_id": params["pane_id"], "shell_pid": 4, "foreground_processes": list(foreground["procs"])}})
        self.api.set_response("plugin.pane.open", {"type": "plugin_pane_opened", "plugin_pane": {"pane": fake_pane("w3:p7", "term_new", None, "Team console")}})
        self.api.set_response("pane.close", {"type": "ok"})
        self.d.on_connected()
        self.assertIsNotNone(self.d.console_reconcile_at_ms)
        self.d.tick()
        self.assertEqual([m for m, _ in self.api.calls if m in ("plugin.pane.open", "pane.close")], [])  # not before the delay
        self.clock.advance(3.1)
        self.d.tick()
        opened = [p for m, p in self.api.calls if m == "plugin.pane.open"]
        self.assertEqual((len(opened), opened[0]["entrypoint"]), (1, "console"))  # open:true with a dead pid: reopened once
        self.assertEqual([m for m, _ in self.api.calls if m == "pane.close"], [])  # foreground unknown: unresolved, retry scheduled
        self.assertIsNotNone(self.d.console_reconcile_at_ms)
        # the reopened console writes its record; the old shell now shows a plain zsh, but the launch grace still holds
        store.write_json(self.ts.session.console_json, dict(store.read_json(self.ts.session.console_json), pane_id="w3:p7", terminal_id="term_new", pid=os.getpid(), open=True))
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w3:p6", "term_shell6", None, "Team console"), fake_pane("w3:p7", "term_new", None, "Team console"), fake_pane("w1:p1", "term_shell")]})
        foreground["procs"] = [{"pid": 4, "name": "zsh"}]
        console = store.read_json(self.ts.session.console_json)
        console["launched_at"] = "2020-01-01T00:00:00.000Z"  # grace over
        store.write_json(self.ts.session.console_json, console)
        self.clock.advance(3.1)
        self.d.tick()
        self.assertEqual([p for m, p in self.api.calls if m == "pane.close"], [{"pane_id": "w3:p6"}])
        self.assertEqual(len([m for m, _ in self.api.calls if m == "plugin.pane.open"]), 1)  # the live console is never duplicated
        self.assertIsNone(self.d.console_reconcile_at_ms)  # resolved: no more passes
        self.assertTrue(any("closed dead console shell w3:p6" in line for line in self.d.logged))

    def test_deferred_console_pass_is_bounded(self):
        from support import fake_pane

        store.write_json(self.ts.session.console_json, {"pane_id": "w3:p6", "terminal_id": "term_old", "pid": os.getpid(), "open": True})
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w3:p6", "term_old", None, "Team console"), fake_pane("w3:p9", "term_zombie", None, "Team console")]})
        self.api.set_response("pane.process_info", {"type": "pane_process_info", "process_info": {"pane_id": "w3:p9", "shell_pid": 4, "foreground_processes": []}})
        self.d.on_connected()
        for _ in range(D.CONSOLE_RECONCILE_ATTEMPTS + 3):
            self.clock.advance(D.CONSOLE_RECONCILE_DELAY_S + 0.1)
            self.d.tick()
        self.assertIsNone(self.d.console_reconcile_at_ms)
        self.assertEqual(len([m for m, _ in self.api.calls if m == "pane.process_info"]), D.CONSOLE_RECONCILE_ATTEMPTS)
        self.assertEqual([m for m, _ in self.api.calls if m in ("pane.close", "plugin.pane.open")], [])

    def test_board_tail_survives_rotation_and_reset(self):
        self.d.on_connected()
        team = self.d.teams["alpha"]
        board = store.BoardStore(self.ts.team, rotate_bytes=600)
        seqs = [board.append({"from": "alpha-worker", "to": ["alpha-reviewer"], "kind": "note", "text": "n{}".format(i) * 20, "origin": {"via": "cli", "verified": True}}) for i in range(6)]
        self.d.tail_boards()
        self.assertGreaterEqual(team.watermark, seqs[-1])  # a ``rotated`` system record may follow
        self.assertEqual(team.pending["alpha-reviewer"].seqs, seqs)
        self.assertTrue(os.listdir(self.ts.team.archive_dir))
        state = store.read_json(self.ts.team.notifier_state)
        self.assertEqual(state["watermark_seq"], team.watermark)
        # a truncated board with an old seq is a reset
        os.unlink(self.ts.team.board_jsonl)
        for name in os.listdir(self.ts.team.archive_dir):
            os.unlink(self.ts.team.archive_dir / name)
        store.write_json(self.ts.team.board_seq, {"next": 2, "active_first_seq": 1})
        store.append_line(self.ts.team.board_jsonl, json.dumps({"v": 1, "seq": 1, "ts": "x", "from": "alpha-worker", "to": ["alpha-reviewer"], "kind": "note", "text": "again", "refs": [], "origin": {}}).encode(), fsync=False)
        self.d.tail_boards()
        self.assertTrue(any("reset detected" in line for line in self.d.logged), self.d.logged)
        self.assertEqual(team.watermark, 1)


# --------------------------------------------------------------------------
# G6: gate config from team.json and the gate 11 follow-up exception


def _board_max(ts):
    return int(store.read_json(ts.team.board_seq, default={"next": 1}).get("next", 1)) - 1


def ticks(d, clock, count, step=5):
    for _ in range(count):
        clock.advance(step)
        d.tick()


def prompts_of(api):
    return [p for m, p in api.calls if m == "agent.prompt"]


STRONG_IDLE = {"type": "agent_explain", "explain": {"state": "idle", "matched_rule": {"id": "codex.idle", "region": "after_last_prompt_marker"}}}


class GateConfigAndFollowUpTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def write_gate(self, overrides):
        doc = store.read_json(self.ts.team.team_json)
        config = dict(doc.get("config") or {})
        if overrides is None:
            config.pop("gate", None)
        else:
            config["gate"] = overrides
        doc["config"] = config
        store.write_json(self.ts.team.team_json, doc)

    def daemon(self, gate_overrides=None):
        if gate_overrides is not None:
            self.write_gate(gate_overrides)
        d, api, clock = make_daemon(self.ts)
        api.set_response("agent.explain", STRONG_IDLE)
        d.on_connected()
        return d, api, clock

    def test_done_hold_zero_in_team_json_removes_the_sixty_second_hold(self):
        # control: the default gate holds a stable idle target for 60 s after it went idle
        d, api, clock = self.daemon()
        self.assertIs(d.teams["alpha"].gate_config, gate.DEFAULT_CONFIG)
        post(self.ts, "alpha-reviewer")
        d.tick()
        ticks(d, clock, 2)  # 10 s
        self.assertEqual(prompts_of(api), [])
        self.assertEqual(d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_DONE_HOLD)
        # the same sequence with config.gate.done_hold_ms 0 lands right after the 2 s stable window
        ts2 = TempState()
        self.addCleanup(ts2.cleanup)
        self.ts = ts2
        d, api, clock = self.daemon({"done_hold_ms": 0})
        team = d.teams["alpha"]
        self.assertEqual((team.gate_config.done_hold_ms, team.gate_config.min_interval_ms), (0, gate.MIN_INTERVAL_MS))
        self.assertTrue(any("gate config" in line and "done_hold_ms" in line for line in d.logged), d.logged)
        post(self.ts, "alpha-reviewer")
        d.tick()
        ticks(d, clock, 2)  # 10 s
        self.assertEqual(len(prompts_of(api)), 1)
        self.assertFalse(any("held: done_hold" in line for line in d.logged), d.logged)

    def test_gate_config_change_is_picked_up_by_the_roster_scan(self):
        d, api, clock = self.daemon()
        team = d.teams["alpha"]
        self.write_gate({"min_interval_ms": 5000, "nudge_focused": "never"})
        ticks(d, clock, 1, step=2.5)  # scan_teams reloads every 2 s
        self.assertEqual(team.gate_config.min_interval_ms, 5000)
        self.write_gate(None)
        ticks(d, clock, 1, step=2.5)
        self.assertIs(team.gate_config, gate.DEFAULT_CONFIG)

    def test_bad_gate_config_falls_back_to_the_default_and_logs_once(self):
        d, api, clock = self.daemon({"done_hold_ms": 0, "bogus_key": 1})
        team = d.teams["alpha"]
        self.assertIs(team.gate_config, gate.DEFAULT_CONFIG)
        bad = [line for line in d.logged if "config.gate ignored" in line]
        self.assertEqual(len(bad), 1, d.logged)
        self.assertIn("bogus_key", bad[0])
        ticks(d, clock, 3, step=2.5)
        self.assertEqual(len([line for line in d.logged if "config.gate ignored" in line]), 1)
        for overrides in ({"done_hold_ms": "soon"}, {"pair_budget": True}, {"nudge_focused": 3}, {"post_ttl_ms": -1}, ["done_hold_ms"]):
            self.write_gate(overrides)
            ticks(d, clock, 1, step=2.5)
            self.assertIs(team.gate_config, gate.DEFAULT_CONFIG, overrides)
        self.assertEqual(D.gate_config_from_roster({"config": {"gate": {"done_hold_ms": 0}}})[0].done_hold_ms, 0)
        self.assertEqual(D.gate_config_from_roster({"config": {"gate": {"done_hold_ms": 0}}})[1], {"done_hold_ms": 0})
        self.assertEqual(D.gate_config_from_roster({})[0], gate.DEFAULT_CONFIG)
        self.assertIsNotNone(D.gate_config_from_roster({"config": {"gate": {"x": 1}}})[2])

    def test_same_second_burst_becomes_one_nudge_covering_the_range(self):
        """Plan 12 / M5 ND-03: five posts 200 ms apart to a stable idle member yield one nudge for the whole range.

        Before ``burst_window_ms`` the first post passed the gate on its own tick and the nudge
        said "1 new board post" while four more were already on the board.
        """
        d, api, clock = self.daemon({"done_hold_ms": 0})
        ticks(d, clock, 3, step=2.5)  # stable idle, interval clear
        seqs = []
        for i in range(5):
            seqs.append(post(self.ts, "alpha-reviewer", text="burst {}".format(i)))
            ticks(d, clock, 1, step=0.2)
        prompts = [p for m, p in api.calls if m == "agent.prompt"]
        self.assertEqual(prompts, [], "a nudge went out inside the burst window")
        ticks(d, clock, 2, step=0.6)
        prompts = [p for m, p in api.calls if m == "agent.prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertRegex(prompts[0]["text"], r"^\[herdr-team nudge\] 5 new board posts for alpha-reviewer \(seq {}-{}\)\.".format(seqs[0], seqs[-1]))
        self.assertEqual(gate.GateConfig.from_mapping({"burst_window_ms": 0}).burst_window_ms, 0)

    def test_pair_budget_hold_toasts_once_per_hour(self):
        """Plan 8.2 gate 11 / M5 ND-08: the ping-pong budget hold raises one toast, coalesced per (member, reason) per hour."""
        d, api, clock = self.daemon({"done_hold_ms": 0, "pair_budget": 2, "min_interval_ms": 0})
        ticks(d, clock, 3, step=2.5)
        now = d.now_ms()
        d.pair_exchanges[("alpha-reviewer", "alpha-worker")] = [now - 1000.0, now - 500.0]
        post(self.ts, "alpha-reviewer")  # author alpha-worker
        ticks(d, clock, 4, step=1.5)
        pending = d.teams["alpha"].pending["alpha-reviewer"]
        self.assertEqual(pending.hold, gate.HOLD_PAIR_BUDGET)
        self.assertEqual([m for m, _ in api.calls if m == "agent.prompt"], [])
        def shown():
            return [p for m, p in api.calls if m == "notification.show" and "ping-pong paused" in str(p.get("title"))]

        toasts = shown()
        self.assertEqual(len(toasts), 1, [p for m, p in api.calls if m == "notification.show"])
        self.assertIn("alpha-worker", toasts[0]["body"])
        # a second post inside the hour re-holds without another toast
        post(self.ts, "alpha-reviewer", text="again")
        ticks(d, clock, 4, step=1.5)
        self.assertEqual(len(shown()), 1)
        # once the 10 min pair window expires the held posts go out
        clock.advance(601)
        ticks(d, clock, 8, step=1.5)  # the gap voided the stable window; it rebuilds over these polls
        self.assertEqual(len([m for m, _ in api.calls if m == "agent.prompt"]), 1, d.logged[-12:])
        # a fresh hold inside the same hour is coalesced: still one toast
        d.pair_exchanges[("alpha-reviewer", "alpha-worker")] = [d.now_ms() - 1000.0, d.now_ms() - 500.0]
        post(self.ts, "alpha-reviewer", text="third")
        ticks(d, clock, 4, step=1.5)
        self.assertEqual(d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_PAIR_BUDGET)
        self.assertEqual(len(shown()), 1)
        # an hour after the first toast the next fresh hold toasts again
        rt = d.teams["alpha"].rt("alpha-reviewer")
        rt.hold_toast_ms[gate.HOLD_PAIR_BUDGET] -= 3601 * 1000.0
        clock.advance(601)
        ticks(d, clock, 8, step=1.5)
        self.assertEqual(len([m for m, _ in api.calls if m == "agent.prompt"]), 2)
        store.Cursors(self.ts.team).advance("alpha-reviewer", _board_max(self.ts), "term_r1", "cli")  # read: the landed pending clears
        ticks(d, clock, 2, step=1.5)
        self.assertNotIn("alpha-reviewer", d.teams["alpha"].pending)
        d.pair_exchanges[("alpha-reviewer", "alpha-worker")] = [d.now_ms() - 1000.0, d.now_ms() - 500.0]
        post(self.ts, "alpha-reviewer", text="fourth")
        ticks(d, clock, 4, step=1.5)
        self.assertEqual(len(shown()), 2)

    def test_follow_up_nudge_is_typed_before_the_interval_and_only_once(self):
        """Plan 8.2 gate 11: one immediate follow-up when the cursor advanced past the nudged seq but posts remain."""
        d, api, clock = self.daemon({"done_hold_ms": 0})
        team = d.teams["alpha"]
        rt = team.rt("alpha-reviewer")
        first = post(self.ts, "alpha-reviewer", "one")
        d.tick()
        ticks(d, clock, 5, step=1)  # the cheap phase has no explain yet: weak idle, 4 s stable window
        self.assertEqual(len(prompts_of(api)), 1)
        pending = team.pending["alpha-reviewer"]
        landed_at = pending.landed_ms / 1000.0
        self.assertEqual((pending.gate_cursor, rt.last_nudge_cursor_seq, pending.follow_up_used), (0, 0, False))
        second = post(self.ts, "alpha-reviewer", "two")
        d.tick()  # ingest: posts arrived after the landing
        self.assertTrue(pending.follow_up_due)
        # the cursor does not move: the interval holds the second nudge
        ticks(d, clock, 5, step=1)
        self.assertEqual(len(prompts_of(api)), 1)
        self.assertEqual(pending.hold, gate.HOLD_INTERVAL)
        # the member reads #first (cursor advances) while #second remains: the follow-up goes out inside 20 s
        store.Cursors(self.ts.team).advance("alpha-reviewer", first, "term_r1", "cli")
        ticks(d, clock, 3, step=1)
        self.assertEqual(len(prompts_of(api)), 2, d.logged[-8:])
        self.assertLess(clock() - landed_at, 20.0)
        self.assertIn("(seq {})".format(second), prompts_of(api)[1]["text"])
        self.assertEqual((pending.follow_up_used, rt.last_nudge_cursor_seq, pending.renudges, pending.attempts), (True, first, 0, 2))
        self.assertTrue(any("follow-up nudge inside the interval" in line for line in d.logged), d.logged[-8:])
        # another post and another read inside the interval: no second follow-up
        post(self.ts, "alpha-reviewer", "three")
        d.tick()
        self.assertFalse(pending.follow_up_due)
        store.Cursors(self.ts.team).advance("alpha-reviewer", second, "term_r1", "cli")
        ticks(d, clock, 8, step=1)
        self.assertEqual(len(prompts_of(api)), 2)
        # the gate itself refuses a spent follow-up: same snapshot, no schedule bypass
        snapshot = d._snapshot(team, team.member("alpha-reviewer"), d.agents["term_r1"], rt, d.now_ms(), pending)
        self.assertEqual((snapshot.last_nudge_cursor_seq, snapshot.follow_up_used), (first, True))
        decision = D.gate_evaluate(snapshot, d._pending_work(pending, second), d.now_ms(), None, 0, config=team.gate_config)
        self.assertEqual(decision.hold, gate.HOLD_INTERVAL)


# --------------------------------------------------------------------------
# G9: one matcher for the daemon and the roster (plan 4.2)


class RehydrateFixtureTests(unittest.TestCase):
    """The daemon's reconcile and ``roster.rehydrate_match`` bind the same members to the same agents."""

    FIXTURES = ("cold_restart.json", "in_session_move.json")

    @staticmethod
    def fixture(name):
        return json.loads((Path(__file__).resolve().parent / "fixtures" / "roster" / name).read_text(encoding="utf-8"))

    def run_fixture(self, name):
        fixture = self.fixture(name)
        ts = TempState(members=fixture["members"])
        self.addCleanup(ts.cleanup)
        d, api, clock = make_daemon(ts)
        api.set_response("agent.list", {"type": "agent_list", "agents": [dict(a) for a in fixture["agent_list"]]})
        api.set_response("pane.list", {"type": "pane_list", "panes": [dict(p) for p in fixture["pane_list"]]})
        api.set_response("agent.rename", {"type": "ok"})
        api.set_response("pane.rename", {"type": "ok"})
        d.on_connected()
        expected = roster.rehydrate_match([roster.Member.from_json(m) for m in fixture["members"]], fixture["agent_list"], fixture["pane_list"])
        members = {m["name"]: m for m in store.read_json(ts.team.team_json)["members"]}
        bound = {b.member: members[b.member]["terminal_id"] for b in expected.bindings if b.kind_matches}
        self.assertEqual(bound, {b.member: b.agent["terminal_id"] for b in expected.bindings if b.kind_matches})
        for binding in expected.bindings:
            member = members[binding.member]
            if binding.kind_matches:
                self.assertEqual((member["status"], member["pane_id"]), ("active", binding.agent["pane_id"]), binding)
                if binding.how != roster.MATCH_TERMINAL:
                    self.assertTrue(any("{} rebound by {} to {}".format(binding.member, binding.how, binding.agent["terminal_id"]) in line for line in d.logged), d.logged)
            else:
                self.assertEqual(member["status"], "kind_changed", binding)
        for entry in expected.kind_changed:
            self.assertEqual(members[entry["member"]]["status"], "kind_changed")
            self.assertEqual(members[entry["member"]]["terminal_id"], entry["agent"]["terminal_id"])
        waiting = expected.missing + [u["member"] for u in expected.unbound]
        for name in waiting:
            self.assertEqual(members[name]["status"], "active", "{} marked during the grace window".format(name))
        for entry in expected.unbound:
            self.assertTrue(any("{} matches only by fingerprint ({}".format(entry["member"], entry["candidate"]["terminal_id"]) in line for line in d.logged), d.logged)
            self.assertNotEqual(members[entry["member"]]["terminal_id"], entry["candidate"]["terminal_id"], "fingerprint auto-bound")
        self.assertEqual(len([m for m, _ in api.calls if m == "pane.list"]), 1 if fixture["pane_list"] or waiting or any(b.how != roster.MATCH_TERMINAL for b in expected.bindings) else 0)
        # after the grace window the leftovers are missing; bindings are unchanged; the candidate is logged once
        clock.advance(31)
        d.reconcile_due = True
        d.tick()
        members = {m["name"]: m for m in store.read_json(ts.team.team_json)["members"]}
        for name in waiting:
            self.assertEqual(members[name]["status"], "missing", name)
        self.assertEqual({b.member: members[b.member]["terminal_id"] for b in expected.bindings if b.kind_matches}, bound)
        for entry in expected.unbound:
            self.assertEqual(len([line for line in d.logged if "{} matches only by fingerprint".format(entry["member"]) in line]), 1)
        self.assertEqual(members["human"]["status"], "active")
        for m in fixture["members"]:
            if m.get("status") == "left":
                self.assertEqual(members[m["name"]]["status"], "left")
        return d, expected, members

    def test_cold_restart_fixture_binds_like_the_roster_matcher(self):
        d, expected, members = self.run_fixture("cold_restart.json")
        self.assertEqual(sorted(b.member for b in expected.bindings), ["alpha-reviewer", "alpha-scout", "alpha-worker"])
        self.assertIn({"target": "w2:p4", "name": "alpha-reviewer"}, [p for m, p in d.api.calls if m == "agent.rename"])

    def test_in_session_move_fixture_binds_like_the_roster_matcher(self):
        d, expected, members = self.run_fixture("in_session_move.json")
        self.assertEqual(members["alpha-reviewer"]["pane_id"], "w5:p1")
        self.assertEqual(members["alpha-swapped"]["status"], "kind_changed")
        self.assertNotIn("agent.rename", [m for m, _ in d.api.calls])

    def test_rows_claimed_by_another_team_never_bind(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        other = ts.session.team("beta")
        paths.ensure_team_dirs(other)
        store.write_json(other.team_json, {"schema": 1, "team": "beta", "created_at": "x", "socket": os.fspath(ts.socket_path), "state_dir": os.fspath(ts.state_root), "naming": "prefixed", "revision": 1, "charter": None, "members": [
            {"name": "beta-reviewer", "role": "reviewer", "kind": "codex", "terminal_id": "term_r2", "pane_id": "w2:p1", "status": "active"},
        ]})
        ts.members[0].update({"terminal_id": "term_old", "status": "missing"})
        ts.write_team_json()
        d, api, clock = make_daemon(ts)
        api.set_response("agent.list", {"type": "agent_list", "agents": [fake_agent("w2:p1", "term_r2", "codex", None), dict(FAKE_AGENTS[1])]})
        d.on_connected()
        alpha = {m["name"]: m for m in store.read_json(ts.team.team_json)["members"]}
        self.assertEqual(alpha["alpha-reviewer"]["terminal_id"], "term_old")  # beta owns term_r2; pane id + kind must not steal it
        beta = {m["name"]: m for m in store.read_json(other.team_json)["members"]}
        self.assertEqual((beta["beta-reviewer"]["terminal_id"], beta["beta-reviewer"]["status"]), ("term_r2", "active"))


# --------------------------------------------------------------------------
# G12: the loop outlives one bad event or job


LOCK_HOLDER_SCRIPT = """
import sys, time
sys.path.insert(0, {root!r})
from herdr_team import store
lock = store.team_lock({team_dir!r})
lock.acquire()
print("held", flush=True)
time.sleep(60)
"""


class LoopBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def hold_team_lock_elsewhere(self):
        script = LOCK_HOLDER_SCRIPT.format(root=os.fspath(PLUGIN_ROOT), team_dir=os.fspath(self.ts.team.root))
        holder = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(self.ts.env))

        def stop():
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait()
            holder.stdout.close()

        self.addCleanup(stop)
        self.assertEqual(holder.stdout.readline().strip(), b"held")
        return holder

    def test_pane_closed_event_during_a_held_team_lock_is_survived(self):
        """A ``board_locked`` from ``update_roster`` inside ``handle_event`` must not unwind ``run``."""
        self.hold_team_lock_elsewhere()
        original = store.team_lock

        def quick_lock(team, timeout=None):
            return original(team, 0.1)

        store.team_lock = quick_lock
        self.addCleanup(setattr, store, "team_lock", original)
        with self.assertRaises(HerdrTeamError) as ctx:
            store.RosterStore(self.ts.team).update(lambda doc: None)
        self.assertEqual(ctx.exception.code, "board_locked")
        d, api, clock = make_daemon(self.ts)
        api.events = [{"event": "pane_closed", "data": {"pane_id": "w2:p1"}}]
        d._serve_subscription()  # returns normally at the fake stream's EOF
        self.assertFalse(d.stop_requested)
        self.assertEqual(d.counters["events"], 1)
        self.assertEqual(d.counters["phase_errors"], 1, d.logged)
        self.assertTrue(any("tick phase event failed: LockTimeout" in line and "board_locked" in line for line in d.logged), d.logged)
        self.assertEqual(store.read_json(self.ts.team.team_json)["members"][0]["status"], "active")  # the write was refused, not half-done

    def test_programming_error_inside_an_event_is_survived(self):
        d, api, clock = make_daemon(self.ts)

        def boom(_pane_id, _why):
            raise RuntimeError("boom")

        d._mark_pane_gone = boom
        api.events = [{"event": "pane_exited", "data": {"pane_id": "w2:p2"}}, {"event": "pane_focused", "data": {"pane_id": "w2:p2"}}]
        d._serve_subscription()
        self.assertFalse(d.stop_requested)
        self.assertEqual((d.counters["events"], d.counters["phase_errors"]), (2, 1))
        self.assertTrue(any("tick phase event failed: RuntimeError: boom" in line for line in d.logged), d.logged)

    def test_job_raising_a_programming_error_is_consumed_not_fatal(self):
        d, api, clock = make_daemon(self.ts)
        d.on_connected()

        def boom(self, team, member, now):
            raise AttributeError("no such attribute")

        original = D.Daemon._enqueue_briefing
        D.Daemon._enqueue_briefing = boom
        self.addCleanup(setattr, D.Daemon, "_enqueue_briefing", original)
        store.write_json(self.ts.team.jobs_dir / "20260904T000000000Z-brief.json", {"v": 1, "kind": "brief", "member": "alpha-reviewer"})
        d.last_jobs_ms = None
        try:
            d.tick()
        except Exception as err:  # noqa: BLE001 - the point of the test
            self.fail("tick() unwound on a job: {}: {}".format(type(err).__name__, err))
        self.assertFalse(d.stop_requested)
        self.assertEqual(os.listdir(self.ts.team.jobs_dir), [])
        self.assertEqual((d.counters["jobs"], d.counters["phase_errors"]), (1, 1))
        self.assertTrue(any("job 20260904T000000000Z-brief.json failed: AttributeError" in line for line in d.logged), d.logged)
        self.assertNotIn("alpha-reviewer", d.teams["alpha"].pending)

    def test_evaluate_survives_a_programming_error_for_one_member(self):
        d, api, clock = make_daemon(self.ts)
        d.on_connected()
        post(self.ts, "alpha-reviewer")
        d.tick()

        def boom(*_args, **_kwargs):
            raise IndexError("boom")

        d._snapshot = boom
        for _ in range(3):
            clock.advance(5)
            d.tick()
        self.assertFalse(d.stop_requested)
        self.assertGreaterEqual(d.counters["phase_errors"], 1)
        self.assertTrue(any("evaluate alpha-reviewer failed: IndexError: boom" in line for line in d.logged), d.logged)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# M0 verify-live regressions (2026-09-05): gate 4 deadlock and the probe clobbering a briefing


class KindGateRegressionTests(unittest.TestCase):
    """A fresh kind could never be delivered to: gate 4 held until ``verified``, and ``verified``
    needs 20 delivered round trips. A passed ``hooks probe`` (one verified round trip) now opens
    the gate; the ledger then accumulates round trips toward ``verified`` as plan 8.3 says."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def prompts(self, api):
        return [p for m, p in api.calls if m == "agent.prompt"]

    def run_ticks(self, d, clock, count, step=5):
        for _ in range(count):
            clock.advance(step)
            d.tick()

    def test_passed_probe_opens_gate_4(self):
        d, api, clock = make_daemon(self.ts, trust=False)
        store.write_json(self.ts.session.kinds_json, {"codex": {"probe": {"nonce": 51880, "ok": True, "result": "landed_working"}}})
        d.on_connected()
        post(self.ts, "alpha-reviewer")
        d.tick()
        self.run_ticks(d, clock, 14)
        prompts = self.prompts(api)
        self.assertEqual(len(prompts), 1, d.logged)
        self.assertIn("[herdr-team nudge]", prompts[0]["text"])
        self.assertNotIn(gate.HOLD_KIND_UNVERIFIED, [p.hold for p in d.teams["alpha"].pending.values()])
        self.assertFalse(any("kind_unverified" in line for line in d.logged))

    def test_failed_probe_keeps_gate_4_closed(self):
        d, api, clock = make_daemon(self.ts, trust=False)
        store.write_json(self.ts.session.kinds_json, {"codex": {"probe": {"nonce": 1, "ok": False, "result": "hung"}}})
        d.on_connected()
        post(self.ts, "alpha-reviewer")
        d.tick()
        self.run_ticks(d, clock, 14)
        self.assertEqual(self.prompts(api), [])
        self.assertEqual(d.teams["alpha"].pending["alpha-reviewer"].hold, gate.HOLD_KIND_UNVERIFIED)

    def test_who_reports_probed_kind_as_verified(self):
        d, api, clock = make_daemon(self.ts, trust=False)
        store.write_json(self.ts.session.kinds_json, {"codex": {"probe": {"ok": True}}})
        self.assertTrue(d._kind_trusted("codex"))
        self.assertFalse(d._kind_trusted("claude"))
        self.assertFalse(d._kind_trusted(""))


class ProbeResumeRegressionTests(unittest.TestCase):
    """A probe job replaced the member's pending briefing outright, so ``create`` followed by
    ``hooks probe`` lost the briefing (live M0: ``briefed: false``, ``pending_nudges: 0``)."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()

    def job(self, kind, member="alpha-reviewer", **extra):
        obj = {"v": 1, "kind": kind, "member": member, "force": False, "requested_by": {"name": "human"}, "requested_at": "now"}
        obj.update(extra)
        path = self.ts.team.jobs_dir / "{}-{}.json".format(int(self.clock() * 1000), kind)
        store.write_json(path, obj)
        return path

    def consume(self):
        self.clock.advance(1)
        self.d.consume_jobs(self.clock() * 1000)

    def prompts(self):
        return [p for m, p in self.api.calls if m == "agent.prompt"]

    def run_ticks(self, count, step=5):
        for _ in range(count):
            self.clock.advance(step)
            self.d.tick()

    def test_probe_job_keeps_the_pending_briefing_and_delivers_it_afterwards(self):
        self.job("brief")
        self.consume()
        self.assertEqual(self.d.teams["alpha"].pending["alpha-reviewer"].kind, "brief")
        self.job("probe", nonce=4242, agent_kind="codex")
        self.consume()
        pending = self.d.teams["alpha"].pending["alpha-reviewer"]
        self.assertEqual(pending.kind, "probe")
        self.assertIsNotNone(pending.resume)
        self.assertEqual(pending.resume.kind, "brief")
        self.run_ticks(10)  # under the 90 s no-ack re-brief, so exactly probe + briefing
        prompts = self.prompts()
        self.assertEqual(len(prompts), 2, self.d.logged)
        self.assertEqual(prompts[0]["text"], "[herdr-team probe 4242]")
        self.assertTrue(prompts[1]["text"].startswith("[herdr-team briefing]"), prompts[1]["text"])
        self.assertTrue(any("brief resumed after the probe" in line for line in self.d.logged))
        self.assertTrue(any("briefing landed for alpha-reviewer" in line for line in self.d.logged))
        member = next(m for m in store.read_json(self.ts.team.team_json)["members"] if m["name"] == "alpha-reviewer")
        self.assertIsNotNone(member.get("briefed_at"))

    def test_probe_job_keeps_a_pending_nudge(self):
        seq = post(self.ts, "alpha-reviewer")
        self.d.tick()
        self.assertEqual(self.d.teams["alpha"].pending["alpha-reviewer"].seqs, [seq])
        self.job("probe", nonce=7, agent_kind="codex")
        self.consume()
        self.run_ticks(3)
        self.assertEqual(self.prompts()[0]["text"], "[herdr-team probe 7]")
        resumed = self.d.teams["alpha"].pending.get("alpha-reviewer")
        self.assertIsNotNone(resumed)
        self.assertEqual((resumed.kind, resumed.seqs), ("nudge", [seq]))

    def test_probe_without_pending_work_leaves_nothing_behind(self):
        self.job("probe", nonce=9, agent_kind="codex")
        self.consume()
        self.run_ticks(3)
        self.assertEqual(len(self.prompts()), 1)
        self.assertNotIn("alpha-reviewer", self.d.teams["alpha"].pending)

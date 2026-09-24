"""Schedules: the cron engine, the ``schedule`` CLI and its authority, and the notifier firing them.

Every clock here is injected and every zone named, so nothing depends on the
machine's time zone or the time of day the suite runs.
"""

import json
import os
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from herdr_team import daemon as D
from herdr_team import console, operator, render, schedules as S, store, tui_model
from herdr_team.errors import HerdrTeamError
from support import FakeApi, TempState, wait_until
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon

BERLIN = S.load_zone("Europe/Berlin")
NEW_YORK = S.load_zone("America/New_York")
KOLKATA = S.load_zone("Asia/Kolkata")
UTC = timezone.utc


def at(year, month, day, hour=0, minute=0, second=0, tz=BERLIN):
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


def epoch(*args, **kwargs):
    return at(*args, **kwargs).timestamp()


def utc_iso(moments):
    return [m.astimezone(UTC).strftime("%Y-%m-%dT%H:%M") for m in moments]


def local(moments):
    return [m.strftime("%Y-%m-%d %H:%M %Z") for m in moments]


# --------------------------------------------------------------------------
# the cron engine


class CronParseTests(unittest.TestCase):
    def test_steps_ranges_and_lists(self):
        self.assertEqual(S.parse_cron("*/15 * * * *").minutes, (0, 15, 30, 45))
        self.assertEqual(S.parse_cron("5/20 * * * *").minutes, (5, 25, 45), "a/n runs from a to the end, as in Vixie cron")
        self.assertEqual(S.parse_cron("1-10/3 * * * *").minutes, (1, 4, 7, 10))
        spec = S.parse_cron("0,30 9,17 * * *")
        self.assertEqual((spec.minutes, spec.hours), ((0, 30), (9, 17)))

    def test_month_and_day_names_any_case_and_sunday_as_seven(self):
        spec = S.parse_cron("0 9 * JAN-mar Mon-FRI")
        self.assertEqual(spec.months, frozenset({1, 2, 3}))
        self.assertEqual(spec.dows, frozenset({1, 2, 3, 4, 5}))
        self.assertEqual(S.parse_cron("0 9 * * 7").dows, frozenset({0}))
        self.assertEqual(S.parse_cron("0 9 * * 5-7").dows, frozenset({5, 6, 0}))
        self.assertEqual(S.parse_cron("0 9 * * fri-sun").dows, frozenset({5, 6, 0}), "Sunday closes a range")

    def test_macros(self):
        self.assertEqual(S.parse_cron("@daily").hours, (0,))
        self.assertEqual(S.parse_cron("@weekly").dows, frozenset({0}))

    def test_refusals_are_usage_errors(self):
        for bad in ("", "* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-1 * * * *", "x * * * *",
                    "0 9 0 * *", "0 9 * 13 *", "0 9 * * 8", "0 9 * * funday", "0,,5 * * * *"):
            with self.subTest(cron=bad):
                with self.assertRaises(HerdrTeamError) as caught:
                    S.parse_cron(bad)
                self.assertEqual((caught.exception.code, caught.exception.exit_code), ("schedule_invalid", 2))

    def test_a_day_no_month_has_is_refused_at_once(self):
        for bad in ("0 9 30 2 *", "0 9 31 apr,jun,sep,nov *"):
            with self.subTest(cron=bad):
                with self.assertRaises(HerdrTeamError) as caught:
                    S.parse_cron(bad)
                self.assertIn("never fires", caught.exception.message)


class NextFiresTests(unittest.TestCase):
    def test_after_is_exclusive_and_results_are_in_the_zone(self):
        fires = S.next_fires("0 9 * * *", at(2026, 9, 23, 9, 0), 2, tz=BERLIN)
        self.assertEqual(local(fires), ["2026-09-24 09:00 CEST", "2026-09-25 09:00 CEST"])

    def test_weekdays_skip_the_weekend(self):
        fires = S.next_fires("0 9 * * 1-5", at(2026, 9, 25, 10, 0), 2, tz=BERLIN)  # a Friday
        self.assertEqual(local(fires), ["2026-09-28 09:00 CEST", "2026-09-29 09:00 CEST"])

    def test_dom_and_dow_both_restricted_is_an_or(self):
        # the 1st of the month OR any Monday (Vixie): 2026-10-01 is a Thursday
        fires = S.next_fires("0 9 1 * mon", at(2026, 9, 26, tz=UTC), 4, tz=UTC)
        self.assertEqual([f.strftime("%m-%d %a") for f in fires], ["09-28 Mon", "10-01 Thu", "10-05 Mon", "10-12 Mon"])

    def test_a_star_prefixed_day_field_makes_it_an_and(self):
        # */2 day-of-month counts as unrestricted, so this is odd days AND Mondays
        fires = S.next_fires("0 9 */2 * mon", at(2026, 9, 1, tz=UTC), 3, tz=UTC)
        self.assertEqual([f.strftime("%m-%d %a") for f in fires], ["09-07 Mon", "09-21 Mon", "10-05 Mon"])
        self.assertEqual([f.strftime("%m-%d") for f in S.next_fires("0 9 * * mon", at(2026, 9, 1, tz=UTC), 2, tz=UTC)], ["09-07", "09-14"])

    def test_the_31st_skips_short_months(self):
        fires = S.next_fires("0 0 31 * *", at(2026, 4, 1, tz=UTC), 3, tz=UTC)
        self.assertEqual([f.strftime("%Y-%m-%d") for f in fires], ["2026-05-31", "2026-07-31", "2026-08-31"])

    def test_leap_day(self):
        fires = S.next_fires("0 0 29 2 *", at(2026, 1, 1, tz=UTC), 3, tz=UTC)
        self.assertEqual([f.strftime("%Y-%m-%d") for f in fires], ["2028-02-29", "2032-02-29", "2036-02-29"])
        # 2100 is not a leap year
        fires = S.next_fires("0 0 29 2 *", at(2097, 1, 1, tz=UTC), 2, tz=UTC)
        self.assertEqual([f.strftime("%Y-%m-%d") for f in fires], ["2104-02-29", "2108-02-29"])

    def test_zone_without_dst(self):
        fires = S.next_fires("0 9 * * *", at(2026, 9, 23, tz=UTC), 1, tz=KOLKATA)
        self.assertEqual(utc_iso(fires), ["2026-09-23T03:30"])

    def test_spring_forward_shifts_a_skipped_minute_once(self):
        # New York, 2026-03-08: 02:00 EST jumps to 03:00 EDT; 02:30 does not exist that day
        fires = S.next_fires("30 2 * * *", at(2026, 3, 7, 12, tz=UTC), 3, tz=NEW_YORK)
        self.assertEqual(local(fires), ["2026-03-08 03:30 EDT", "2026-03-09 02:30 EDT", "2026-03-10 02:30 EDT"])
        hourly = S.next_fires("30 * * * *", at(2026, 3, 8, 5, 0, tz=UTC), 4, tz=NEW_YORK)
        self.assertEqual(local(hourly), ["2026-03-08 00:30 EST", "2026-03-08 01:30 EST", "2026-03-08 03:30 EDT", "2026-03-08 04:30 EDT"],
                         "the shifted 02:30 and the real 03:30 are one instant: it fires once")

    def test_fall_back_fires_a_repeated_minute_once(self):
        # New York, 2026-11-01: 01:00-02:00 happens twice (EDT, then EST)
        fires = S.next_fires("30 1 * * *", at(2026, 10, 31, 12, tz=UTC), 2, tz=NEW_YORK)
        self.assertEqual(utc_iso(fires), ["2026-11-01T05:30", "2026-11-02T06:30"])
        hourly = S.next_fires("30 * * * *", at(2026, 11, 1, 4, 0, tz=UTC), 3, tz=NEW_YORK)
        self.assertEqual(utc_iso(hourly), ["2026-11-01T04:30", "2026-11-01T05:30", "2026-11-01T07:30"], "no second 01:30 (06:30Z)")
        quarter = S.next_fires("*/15 * * * *", at(2026, 10, 25, 0, 30, tz=UTC), 4, tz=BERLIN)  # Berlin falls back at 03:00 CEST
        self.assertEqual(utc_iso(quarter), ["2026-10-25T00:45", "2026-10-25T02:00", "2026-10-25T02:15", "2026-10-25T02:30"])

    def test_epoch_input_and_count(self):
        fires = S.next_fires(S.parse_cron("0 * * * *"), epoch(2026, 9, 23, 10, 30), 3, tz=BERLIN)
        self.assertEqual([f.strftime("%H:%M") for f in fires], ["11:00", "12:00", "13:00"])
        self.assertEqual(S.next_fires("0 * * * *", 0, 0), [])


class PreviousFireTests(unittest.TestCase):
    def test_latest_at_or_before_and_strictly_after_the_floor(self):
        spec = "0 9 * * 1-5"
        self.assertEqual(local([S.previous_fire(spec, at(2026, 9, 23, 12, 0), tz=BERLIN)]), ["2026-09-23 09:00 CEST"])
        self.assertEqual(local([S.previous_fire(spec, at(2026, 9, 23, 9, 0), tz=BERLIN)]), ["2026-09-23 09:00 CEST"], "inclusive")
        self.assertEqual(local([S.previous_fire(spec, at(2026, 9, 28, 8, 0), tz=BERLIN)]), ["2026-09-25 09:00 CEST"], "back over a weekend")
        self.assertIsNone(S.previous_fire(spec, at(2026, 9, 23, 12, 0), tz=BERLIN, not_before=at(2026, 9, 23, 9, 0)))
        self.assertIsNone(S.previous_fire(spec, at(2026, 9, 23, 8, 0), tz=BERLIN, not_before=at(2026, 9, 22, 10, 0)))

    def test_across_the_spring_forward_gap(self):
        slot = S.previous_fire("30 2 * * *", at(2026, 3, 8, 8, 0, tz=UTC), tz=NEW_YORK)
        self.assertEqual(local([slot]), ["2026-03-08 03:30 EDT"])


class PresetTests(unittest.TestCase):
    def test_presets_map_to_crons(self):
        self.assertEqual(S.preset_cron("hourly"), "0 * * * *")
        self.assertEqual(S.preset_cron("hourly", ":15"), "15 * * * *")
        self.assertEqual(S.preset_cron("daily"), "0 9 * * *")
        self.assertEqual(S.preset_cron("weekdays", "17:30"), "30 17 * * 1-5")
        self.assertEqual(S.preset_cron("weekly", "08:05", "thu,Monday"), "5 8 * * mon,thu")
        self.assertEqual(S.preset_cron("weekly"), "0 9 * * mon")

    def test_preset_refusals(self):
        for every, when, day in (("hourly", "09:15", None), ("daily", "24:00", None), ("daily", "9", None), ("daily", None, "mon"), ("weekly", None, "someday")):
            with self.subTest(every=every, at=when, day=day):
                with self.assertRaises(HerdrTeamError):
                    S.preset_cron(every, when, day)

    def test_durations(self):
        self.assertEqual(S.parse_duration("30m", "--grace", "m", S.MAX_GRACE_S), 1800)
        self.assertEqual(S.parse_duration("45", "--grace", "m", S.MAX_GRACE_S), 2700)
        self.assertEqual(S.parse_duration("90", "--precheck-timeout", "s", 3600), 90)
        with self.assertRaises(HerdrTeamError):
            S.parse_duration("8d", "--grace", "m", S.MAX_GRACE_S)
        with self.assertRaises(HerdrTeamError):
            S.parse_duration("soon", "--grace", "m", S.MAX_GRACE_S)


class ZoneTests(unittest.TestCase):
    def test_unknown_zone_is_a_clear_usage_error(self):
        for bad in ("Mars/Olympus", "../etc/passwd", "/etc/localtime", ""):
            with self.subTest(tz=bad):
                with self.assertRaises(HerdrTeamError) as caught:
                    S.load_zone(bad)
                self.assertEqual((caught.exception.code, caught.exception.exit_code), ("tz_unknown", 2))
        self.assertIs(S.load_zone("UTC"), timezone.utc)

    def test_local_zone_reads_tz_then_localtime_then_falls_back_to_utc(self):
        self.assertEqual(S.local_zone({"TZ": "America/New_York"})[0], "America/New_York")
        self.assertEqual(S.local_zone({"TZ": ":Europe/Berlin"})[0], "Europe/Berlin")
        with TempState() as ts:
            with mock.patch.object(S, "LOCALTIME_PATH", os.fspath(ts.tmp / "no-such-localtime")):
                self.assertEqual(S.local_zone({}), ("UTC", timezone.utc))
                self.assertEqual(S.local_zone({"TZ": "Not/AZone"})[0], "UTC")


class ModelTests(unittest.TestCase):
    def entry(self, **overrides):
        base = {"id": "s1", "text": "Scan competitor pricing pages", "cron": "0 8 * * mon", "to": ["all"]}
        base.update(overrides)
        return base

    def test_defaults_and_unknown_keys_survive(self):
        sched = S.Schedule.from_json(self.entry(future_field={"x": 1}))
        self.assertEqual((sched.kind, sched.action_type, sched.grace_s, sched.enabled), ("request", "post", 1800, True))
        self.assertEqual(sched.to_json()["future_field"], {"x": 1})

    def test_invalid_entries_name_the_problem(self):
        for overrides in ({"id": "brand"}, {"text": " "}, {"cron": "61 * * * *"}, {"to": []}, {"kind": "handoff"},
                          {"tz": "Mars/Olympus"}, {"action": "post"}, {"grace_s": -1}, {"enabled": "yes"}, {"name": "s7"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(HerdrTeamError):
                    S.Schedule.from_json(self.entry(**overrides))

    def test_describe(self):
        cases = (("hourly", "15 * * * *", "hourly at :15"), ("daily", "0 9 * * *", "daily 09:00"),
                 ("weekdays", "30 17 * * 1-5", "weekdays 17:30"), ("weekly", "0 9 * * mon,thu", "weekly mon,thu 09:00"),
                 (None, "0 9 1 * *", "cron '0 9 1 * *'"), ("daily", "0 9 1 * *", "cron '0 9 1 * *'"))
        for every, cron, text in cases:
            self.assertEqual(S.describe(S.Schedule.from_json(self.entry(every=every, cron=cron))), text)

    def test_ids_are_never_reused(self):
        doc = {"schedules": [{"id": "s4"}], "next_id": 2}
        self.assertEqual(S.allocate_id(doc), "s5")
        doc["schedules"] = []
        self.assertEqual(S.allocate_id(doc), "s6")


# --------------------------------------------------------------------------
# the CLI

T_ADD = epoch(2026, 9, 23, 8, 59)  # Wednesday 08:59 CEST


def cli(ts, argv, env=None, api=None, now=T_ADD):
    with mock.patch.object(S, "_now", lambda: now):
        return json_out(run_cli(["--json"] + list(argv), env if env is not None else env_no_daemon(ts, TZ="Europe/Berlin"), api or FakeApi()))


def board(ts):
    return store.BoardStore(ts.team).read()


def audit_events(ts):
    text = ts.team.audit_jsonl.read_text(encoding="utf-8") if ts.team.audit_jsonl.exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def add(self, *extra, text="Triage yesterday's brand mentions"):
        return cli(self.ts, ["schedule", "add", text] + list(extra))

    def test_add_prints_the_id_and_the_next_three_fires(self):
        code, out, err = self.add("--every", "weekdays", "--at", "09:00", "--to", "role:reviewer", "--name", "brand-mentions")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["id"], "s1")
        self.assertEqual(out["next"], ["2026-09-23T09:00:00+02:00", "2026-09-24T09:00:00+02:00", "2026-09-25T09:00:00+02:00"])
        self.assertEqual((out["to"], out["to_role"]), (["alpha-reviewer"], "reviewer"))
        stored = json.loads(S.definitions_path(self.ts.team).read_text(encoding="utf-8"))
        entry = stored["schedules"][0]
        self.assertEqual((entry["cron"], entry["tz"], entry["to"], entry["kind"], entry["action"]), ("0 9 * * 1-5", "Europe/Berlin", ["role:reviewer"], "request", {"type": "post"}))
        self.assertIn("\n  ", S.definitions_path(self.ts.team).read_text(encoding="utf-8"), "indented for the operator to edit")
        self.assertIn("schedule_add", [e["event"] for e in audit_events(self.ts)])

    def test_human_output_names_the_times(self):
        with mock.patch.object(S, "_now", lambda: T_ADD):
            code, out, _err = run_cli(["schedule", "add", "Run the competitor scan", "--cron", "0 8 * * mon", "--tz", "America/New_York", "--to", "all"],
                                      env_no_daemon(self.ts), FakeApi())
        self.assertEqual(code, 0)
        self.assertIn("s1 added: cron '0 8 * * mon' (America/New_York) -> all as a request", out)
        self.assertIn("Mon 2026-09-28 08:00 EDT", out)

    def test_refusals_leave_nothing_behind(self):
        cases = (
            (["--every", "daily", "--to", "nobody"], "recipient_unknown"),
            (["--every", "daily", "--to", "human"], "schedule_recipient"),
            (["--every", "daily", "--to", "team:beta"], "schedule_recipient"),
            (["--cron", "0 9 30 2 *", "--to", "all"], "schedule_invalid"),
            (["--every", "daily", "--tz", "Mars/Olympus", "--to", "all"], "tz_unknown"),
            (["--cron", "0 9 * * *", "--at", "10:00", "--to", "all"], "usage"),
            (["--every", "daily", "--day", "mon", "--to", "all"], "schedule_invalid"),
            (["--every", "daily", "--name", "s3", "--to", "all"], "usage"),
            (["--every", "daily", "--grace", "soon", "--to", "all"], "schedule_invalid"),
        )
        for extra, code_name in cases:
            with self.subTest(extra=extra):
                code, _out, err = self.add(*extra)
                self.assertNotEqual(code, 0)
                self.assertEqual(err["code"], code_name)
        self.assertFalse(S.definitions_path(self.ts.team).exists())
        code, _out, err = self.add("--every", "daily", "--to", "all", text="[herdr-team nudge] read the board")
        self.assertEqual((code, err["code"]), (4, "echo_rejected"))

    def test_names_are_unique_and_resolve_like_ids(self):
        self.assertEqual(self.add("--every", "daily", "--to", "all", "--name", "scan")[0], 0)
        code, _out, err = self.add("--every", "weekly", "--to", "all", "--name", "scan")
        self.assertEqual((code, err["code"]), (1, "schedule_exists"))
        code, out, err = cli(self.ts, ["schedule", "show", "scan"])
        self.assertEqual((code, out["schedule"]["id"]), (0, "s1"), err)
        code, _out, err = cli(self.ts, ["schedule", "show", "nope"])
        self.assertEqual((code, err["code"]), (1, "schedule_not_found"))

    def test_list_show_next_enable_disable_rm(self):
        self.add("--every", "weekdays", "--to", "all", "--name", "brand")
        self.add("--cron", "0 8 * * mon", "--tz", "America/New_York", "--to", "alpha-worker", text="Scan competitor pricing")
        code, out, _err = cli(self.ts, ["schedule", "list"])
        self.assertEqual([(r["id"], r["when"], r["zone"]) for r in out["schedules"]],
                         [("s1", "weekdays 09:00", "Europe/Berlin"), ("s2", "cron '0 8 * * mon'", "America/New_York")])
        code, out, _err = cli(self.ts, ["schedule", "next", "--count", "5"])
        self.assertEqual([(n["id"], n["at"]) for n in out["next"]], [
            ("s1", "2026-09-23T09:00:00+02:00"), ("s1", "2026-09-24T09:00:00+02:00"), ("s1", "2026-09-25T09:00:00+02:00"),
            ("s1", "2026-09-28T09:00:00+02:00"), ("s2", "2026-09-28T08:00:00-04:00")])
        code, out, _err = cli(self.ts, ["schedule", "disable", "brand"])
        self.assertEqual((code, out["enabled"], out["changed"]), (0, False, True))
        self.assertEqual([n["id"] for n in cli(self.ts, ["schedule", "next", "--count", "2"])[1]["next"]], ["s2", "s2"])
        code, out, _err = cli(self.ts, ["schedule", "enable", "s1"], now=epoch(2026, 9, 24, 12, 0))
        self.assertEqual((out["changed"], out["next"]), (True, "2026-09-25T09:00:00+02:00"))
        self.assertEqual(S.read_state(self.ts.team)["s1"]["armed_at"], "2026-09-24T10:00:00.000Z", "enabling arms it now")
        code, out, _err = cli(self.ts, ["schedule", "rm", "s1"])
        self.assertEqual((code, out["id"]), (0, "s1"))
        self.assertNotIn("s1", S.read_state(self.ts.team))
        self.assertEqual([r["id"] for r in cli(self.ts, ["schedule", "list"])[1]["schedules"]], ["s2"])
        self.assertEqual(self.add("--every", "daily", "--to", "all")[1]["id"], "s3", "a removed id is not reused")
        events = [e["event"] for e in audit_events(self.ts)]
        for event in ("schedule_add", "schedule_disable", "schedule_enable", "schedule_rm"):
            self.assertIn(event, events)

    def test_the_trailing_json_flag_works_after_the_action(self):
        self.add("--every", "daily", "--to", "all")
        with mock.patch.object(S, "_now", lambda: T_ADD):
            code, out, err = run_cli(["schedule", "list", "--json"], env_no_daemon(self.ts, TZ="Europe/Berlin"), FakeApi())
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["schedules"][0]["id"], "s1")

    def test_a_broken_file_is_never_overwritten(self):
        path = S.definitions_path(self.ts.team)
        path.write_text('{"schedules": [ {"id": "s1", oops', encoding="utf-8")
        code, _out, err = self.add("--every", "daily", "--to", "all")
        self.assertEqual((code, err["code"]), (1, "schedules_unreadable"))
        self.assertEqual(path.read_text(encoding="utf-8"), '{"schedules": [ {"id": "s1", oops')

    def test_run_posts_now_as_the_operator_via_schedule(self):
        self.add("--every", "weekdays", "--to", "role:reviewer", "--name", "brand")
        code, out, err = cli(self.ts, ["schedule", "run", "brand"])
        self.assertEqual((code, out["outcome"]), (0, "posted"), err)
        rec = board(self.ts)[-1]
        self.assertEqual(rec["seq"], out["seq"])
        self.assertEqual((rec["from"], rec["to"], rec["to_role"], rec["kind"]), ("human", ["alpha-reviewer"], "reviewer", "request"))
        self.assertEqual(rec["text"], "[scheduled brand] Triage yesterday's brand mentions")
        self.assertEqual({k: rec["origin"][k] for k in ("via", "verified", "schedule", "scheduled_by", "manual", "run_by")},
                         {"via": "schedule", "verified": True, "schedule": "s1", "scheduled_by": "human", "manual": True, "run_by": "human"})
        self.assertEqual(rec["schedule"], {"id": "s1", "name": "brand", "slot": None, "manual": True})
        state = S.read_state(self.ts.team)["s1"]
        self.assertEqual((state["last_manual_outcome"], state["last_manual_seq"]), ("posted", rec["seq"]))
        self.assertNotIn("last_slot", state, "a manual run consumes no slot")
        self.assertIn("schedule_run", [e["event"] for e in audit_events(self.ts)])

    def test_run_with_a_failing_precheck_posts_nothing(self):
        self.add("--every", "daily", "--to", "all", "--precheck", "exit 3")
        before = len(board(self.ts))
        code, out, err = cli(self.ts, ["schedule", "run", "s1"])
        self.assertEqual((code, out["outcome"], out["precheck"]["exit_code"]), (0, "skipped", 3), err)
        self.assertEqual(len(board(self.ts)), before)
        self.add("--every", "daily", "--to", "all", "--precheck", "sleep 5; echo late", "--precheck-timeout", "1")
        started = time.monotonic()
        code, _out, err = cli(self.ts, ["schedule", "run", "s2"])
        self.assertEqual((code, err["code"]), (1, "schedule_precheck_failed"))
        self.assertLess(time.monotonic() - started, 4.0)

    def test_who_shows_the_next_fire(self):
        self.add("--every", "weekdays", "--to", "all", "--name", "brand")
        code, out, err = cli(self.ts, ["who"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["schedules"]["next"]["id"], "s1")
        self.assertEqual((out["schedules"]["count"], out["schedules"]["enabled"]), (1, 1))
        self.assertIn("schedules: 1 of 1 on; next s1 (brand)", S.summary_line(out["schedules"]))


class AuthorityTests(unittest.TestCase):
    """Writing is operator authority; a precheck needs the operator in person."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = live_api()
        self.member_env = env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2", TZ="Europe/Berlin")  # alpha-worker's pane

    def as_member(self, argv):
        return cli(self.ts, argv, env=self.member_env, api=self.api)

    def test_a_plain_member_may_read_but_not_write(self):
        cli(self.ts, ["schedule", "add", "Triage mentions", "--every", "daily", "--to", "all"])
        code, _out, err = self.as_member(["schedule", "add", "Me too", "--every", "daily", "--to", "all"])
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        for argv in (["schedule", "run", "s1"], ["schedule", "disable", "s1"], ["schedule", "rm", "s1"]):
            self.assertEqual(self.as_member(argv)[2]["code"], "author_mismatch", argv)
        code, out, err = self.as_member(["schedule", "list"])
        self.assertEqual((code, [r["id"] for r in out["schedules"]]), (0, ["s1"]), err)

    def test_a_delegate_may_schedule_but_not_with_a_precheck(self):
        operator.grant(self.ts.session, "alpha", "alpha-worker", ttl_s=0, note="runs the weekly research digest")
        code, out, err = self.as_member(["schedule", "add", "Draft the weekly digest", "--every", "weekly", "--day", "fri", "--to", "all"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["schedule"]["created_by"], "alpha-worker")
        code, _out, err = self.as_member(["schedule", "add", "Digest if there is news", "--every", "daily", "--to", "all", "--precheck", "test -s news.txt"])
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertIn("operator in person", err["message"])
        # the operator may; the delegate may then pause or remove it, not run or re-arm it
        self.assertEqual(cli(self.ts, ["schedule", "add", "Digest if there is news", "--every", "daily", "--to", "all", "--precheck", "test -s news.txt"])[0], 0)
        self.assertEqual(self.as_member(["schedule", "run", "s2"])[2]["code"], "author_mismatch")
        self.assertEqual(self.as_member(["schedule", "disable", "s2"])[0], 0)
        self.assertEqual(self.as_member(["schedule", "enable", "s2"])[2]["code"], "author_mismatch")
        self.assertEqual(self.as_member(["schedule", "run", "s1"])[1]["outcome"], "posted", "no precheck: a delegate may run it")
        self.assertEqual(self.as_member(["schedule", "rm", "s2"])[0], 0)
        refused = [e for e in audit_events(self.ts) if e["event"] == "author_mismatch"]
        self.assertTrue(refused and all(e["author"] == "alpha-worker" for e in refused))


# --------------------------------------------------------------------------
# the notifier


class WallClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def add_entry(ts, sid, cron, to=("alpha-reviewer",), created=T_ADD, tz="Europe/Berlin", **extra):
    entry = {"id": sid, "text": extra.pop("text", "Summarise yesterday's brand mentions"), "cron": cron, "tz": tz, "to": list(to),
             "kind": "request", "action": {"type": "post"}, "grace_s": extra.pop("grace_s", 1800), "enabled": True,
             "created_at": S.iso_now(created), "created_by": "human"}
    entry.update(extra)

    def mutate(doc):
        doc["schedules"] = [e for e in doc.get("schedules", []) if e.get("id") != sid] + [entry]

    S.update_document(ts.team, mutate)


def posts(ts, sid=None):
    return [r for r in board(ts) if isinstance(r.get("schedule"), dict) and r.get("kind") != "system" and (sid is None or r["schedule"]["id"] == sid)]


def events(ts, event):
    return [r for r in board(ts) if r.get("event") == event]


class DaemonFiringTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.wall = WallClock(T_ADD)

    def daemon(self):
        d, api, clock = make_daemon(self.ts)
        d.wall_clock = self.wall
        d.on_connected()
        return d, api, clock

    def step(self, d, clock, seconds):
        self.wall.t += seconds
        clock.advance(seconds)
        d.tick()

    def test_fires_once_per_slot_as_the_operator_and_the_recipient_gets_pending_work(self):
        add_entry(self.ts, "s1", "0 9 * * 1-5", name="brand")
        d, _api, clock = self.daemon()
        d.tick()
        self.assertEqual(posts(self.ts), [], "08:59: not due")
        self.step(d, clock, 61)  # 09:00:01
        [rec] = posts(self.ts)
        self.assertEqual((rec["from"], rec["to"], rec["schedule"]["slot"], rec["schedule"]["manual"]), ("human", ["alpha-reviewer"], "2026-09-23T07:00:00Z", False))
        self.assertEqual({k: rec["origin"][k] for k in ("via", "verified", "schedule", "scheduled_by", "slot")},
                         {"via": "schedule", "verified": True, "schedule": "s1", "scheduled_by": "human", "slot": "2026-09-23T07:00:00Z"})
        self.assertTrue(D.Daemon._counts_for_nudges(rec))
        self.assertTrue(store.is_member_mail(rec, "alpha-reviewer"))
        self.assertFalse(render.is_unverified(rec))
        pending = d.teams["alpha"].pending.get("alpha-reviewer")
        self.assertIsNotNone(pending, "the ordinary delivery path picked it up")
        self.assertIn(rec["seq"], pending.seqs)
        state = S.read_state(self.ts.team)["s1"]
        self.assertEqual((state["last_slot"], state["last_outcome"], state["last_seq"]), ("2026-09-23T07:00:00Z", "posted", rec["seq"]))
        self.assertEqual(state["next_due"], "2026-09-24T07:00:00Z")
        for _ in range(4):
            self.step(d, clock, 10)
        self.assertEqual(len(posts(self.ts)), 1, "once per slot")
        fired = [e for e in audit_events(self.ts) if e["event"] == "schedule_fire"]
        self.assertEqual([(e["details"]["outcome"], e["details"]["seq"]) for e in fired], [("posted", rec["seq"])])

    def test_a_restart_does_not_fire_the_same_slot_again(self):
        add_entry(self.ts, "s1", "0 9 * * *")
        d, _api, clock = self.daemon()
        self.step(d, clock, 61)
        self.assertEqual(len(posts(self.ts)), 1)
        self.wall.t += 20
        d2, _api2, _clock2 = self.daemon()
        d2.tick()
        self.assertEqual(len(posts(self.ts)), 1, "last_slot persisted")
        # A daemon that died after the append but before the state write: the board is the witness.
        S.update_state(self.ts.team, {"s1": None})
        d3, _api3, _clock3 = self.daemon()
        d3.tick()
        self.assertEqual(len(posts(self.ts)), 1)
        self.assertEqual(S.read_state(self.ts.team)["s1"]["last_slot"], "2026-09-23T07:00:00Z")
        self.assertTrue(any("already posted" in line for line in d3.logged))

    def test_a_slot_missed_within_the_grace_fires_once_for_the_latest_slot(self):
        add_entry(self.ts, "s1", "0 * * * *", created=epoch(2026, 9, 23, 6, 0))  # hourly, grace 30m
        self.wall.t = epoch(2026, 9, 23, 9, 10)  # the daemon was down 07:00, 08:00 and 09:00
        d, _api, _clock = self.daemon()
        d.tick()
        self.assertEqual([r["schedule"]["slot"] for r in posts(self.ts)], ["2026-09-23T07:00:00Z"], "09:00 CEST only")
        self.assertEqual(events(self.ts, "schedule_missed"), [])

    def test_beyond_the_grace_one_schedule_missed_record_and_move_on(self):
        add_entry(self.ts, "s1", "0 9 * * *", created=epoch(2026, 9, 23, 8, 0))
        self.wall.t = epoch(2026, 9, 23, 10, 0)
        d, api, clock = self.daemon()
        d.tick()
        self.step(d, clock, 10)
        self.step(d, clock, 10)
        [missed] = events(self.ts, "schedule_missed")
        self.assertEqual((missed["from"], missed["to"], missed["schedule"]["id"], missed["schedule"]["slot"]), ("system", ["human"], "s1", "2026-09-23T07:00:00Z"))
        self.assertIn("grace of 30m", missed["text"])
        self.assertEqual(posts(self.ts), [])
        self.assertNotIn("alpha-reviewer", d.teams["alpha"].pending)
        self.assertEqual(S.read_state(self.ts.team)["s1"]["last_outcome"], "missed")
        self.wall.t = epoch(2026, 9, 24, 9, 0, 5)
        clock.advance(10)
        d.tick()
        self.assertEqual([r["schedule"]["slot"] for r in posts(self.ts)], ["2026-09-24T07:00:00Z"], "the next day fires normally")

    def test_disabled_does_not_fire_and_re_enabling_by_hand_owes_nothing(self):
        add_entry(self.ts, "s1", "0 9 * * *", enabled=False)
        d, _api, clock = self.daemon()
        self.step(d, clock, 61)
        self.assertEqual(posts(self.ts), [])
        self.assertTrue(S.read_state(self.ts.team)["s1"]["disabled_seen"])
        self.wall.t = epoch(2026, 9, 23, 9, 20)  # re-enabled by editing the file, inside the grace of the 09:00 slot
        add_entry(self.ts, "s1", "0 9 * * *", enabled=True)
        self.step(d, clock, 6)
        self.assertEqual(posts(self.ts), [])
        self.assertEqual(events(self.ts, "schedule_missed"), [])
        self.assertEqual(S.read_state(self.ts.team)["s1"]["next_due"], "2026-09-24T07:00:00Z")

    def test_an_invalid_hand_edit_and_an_unknown_action_are_reported_once(self):
        add_entry(self.ts, "s1", "0 9 * * *")
        add_entry(self.ts, "s2", "0 9 * * *")

        def break_them(doc):
            doc["schedules"][0]["cron"] = "0 25 * * *"
            doc["schedules"][1]["action"] = {"type": "launch-rocket"}

        S.update_document(self.ts.team, break_them)
        d, _api, clock = self.daemon()
        for _ in range(3):
            self.step(d, clock, 61)
        failed = events(self.ts, "schedule_failed")
        self.assertEqual(sorted(r["schedule"]["id"] for r in failed), ["s1", "s2"])
        self.assertEqual(posts(self.ts), [])

    def test_a_copied_entry_with_the_same_id_is_reported_once_and_the_original_still_fires(self):
        add_entry(self.ts, "s1", "0 9 * * *")

        def duplicate(doc):
            doc["schedules"].append(dict(doc["schedules"][0], text="a pasted copy"))

        S.update_document(self.ts.team, duplicate)
        d, _api, clock = self.daemon()
        for _ in range(4):
            self.step(d, clock, 61)
        [failed] = events(self.ts, "schedule_failed")
        self.assertIn("repeats the id", failed["text"])
        self.assertEqual([r["text"] for r in posts(self.ts)], ["[scheduled s1] Summarise yesterday's brand mentions"])

    def test_a_vacated_role_is_a_schedule_failed_toast(self):
        add_entry(self.ts, "s1", "0 9 * * *", to=("role:analyst",))
        d, api, clock = self.daemon()
        self.step(d, clock, 61)
        [failed] = events(self.ts, "schedule_failed")
        self.assertEqual(failed["to"], ["human"])
        self.assertIn("recipient_unknown", S.read_state(self.ts.team)["s1"]["last_detail"])
        self.assertTrue([p for m, p in api.calls if m == "notification.show"], "schedule_failed reaches the operator's toasts")

    def runner_with(self, d, result):
        runner = S.Runner(d.layout, "alpha", d.env, log=d.log)
        runner.inline_prechecks = True
        seen = []

        def fake(command, cwd, env, timeout):
            seen.append((command, cwd, env, timeout))
            return result

        runner.precheck_fn = fake
        d.teams["alpha"].schedules = runner
        return seen

    def test_precheck_nonzero_skips_the_run_without_a_post_or_a_toast(self):
        add_entry(self.ts, "s1", "0 9 * * *", precheck="grep -q . mentions.txt", precheck_timeout_s=45)
        d, api, clock = self.daemon()
        seen = self.runner_with(d, S.PrecheckResult("nonzero", 1, "no new mentions"))
        self.step(d, clock, 61)
        self.assertEqual(S.read_state(self.ts.team)["s1"]["running"]["slot"], "2026-09-23T07:00:00Z")
        self.step(d, clock, 0.25)
        self.assertEqual(posts(self.ts), [])
        self.assertEqual(events(self.ts, "schedule_failed"), [])
        state = S.read_state(self.ts.team)["s1"]
        self.assertEqual((state["last_outcome"], state["last_slot"], state["running"]), ("skipped", "2026-09-23T07:00:00Z", None))
        command, cwd, env, timeout = seen[0]
        self.assertEqual((command, cwd, timeout), ("grep -q . mentions.txt", os.fspath(self.ts.team.root), 45.0))
        self.assertEqual(sorted(env), ["HERDR_SYNAPSE_SCHEDULE", "HERDR_SYNAPSE_TEAM", "HOME", "LANG", "PATH"], "no Herdr identity or socket")
        self.assertEqual(env["HOME"], self.ts.env["HOME"])
        self.step(d, clock, 30)
        self.assertEqual(len(seen), 1, "one precheck per slot")
        self.assertIn(("skipped", None), [(e["details"]["outcome"], e["details"]["seq"]) for e in audit_events(self.ts) if e["event"] == "schedule_fire"])

    def test_precheck_ok_posts_and_timeout_is_schedule_failed(self):
        add_entry(self.ts, "s1", "0 9 * * *", precheck="true")
        d, _api, clock = self.daemon()
        self.runner_with(d, S.PrecheckResult("ok", 0, ""))
        self.step(d, clock, 61)
        self.step(d, clock, 0.25)
        self.assertEqual(len(posts(self.ts)), 1)
        self.wall.t = epoch(2026, 9, 24, 8, 59)
        d.teams["alpha"].schedules = None
        _seen = self.runner_with(d, S.PrecheckResult("timeout", None, ""))
        self.step(d, clock, 61)
        self.step(d, clock, 0.25)
        self.assertEqual(len(posts(self.ts)), 1, "no post after a timeout")
        [failed] = events(self.ts, "schedule_failed")
        self.assertEqual((failed["to"], failed["schedule"]["precheck"]), (["human"], "timeout"))
        self.assertIn("timed out after 1m", failed["text"])

    def test_a_real_precheck_runs_in_a_worker_thread(self):
        add_entry(self.ts, "s1", "0 9 * * *", precheck="test -d .")
        runner = S.Runner(self.ts.layout, "alpha", self.ts.env)
        start = epoch(2026, 9, 23, 9, 0, 1)
        runner.tick(start, 100.0)
        job = runner.jobs.get("s1")
        self.assertIsNotNone(job)
        self.assertTrue(wait_until(lambda: job.done, timeout_s=10.0))
        runner.tick(start + 1, 100.25)
        self.assertEqual(len(posts(self.ts)), 1)
        self.assertEqual(S.read_state(self.ts.team)["s1"]["last_precheck"]["status"], "ok")


class DstFiringTests(unittest.TestCase):
    """The Runner stepped minute by minute across both DST changes: one post per local minute."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def walk(self, cron, tz, start, minutes):
        add_entry(self.ts, "s1", cron, tz=tz, created=start - 1, grace_s=0)
        runner = S.Runner(self.ts.layout, "alpha", self.ts.env)
        wall, mono = start, 0.0
        for _ in range(minutes * 4):
            runner.tick(wall, mono)
            wall += 15.0
            mono += 15.0
        return [r["schedule"]["slot"] for r in posts(self.ts)]

    def test_spring_forward(self):
        slots = self.walk("30 * * * *", "America/New_York", at(2026, 3, 8, 5, 0, tz=UTC).timestamp(), 180)
        self.assertEqual(slots, ["2026-03-08T05:30:00Z", "2026-03-08T06:30:00Z", "2026-03-08T07:30:00Z"])

    def test_fall_back(self):
        slots = self.walk("30 1 * * *", "America/New_York", at(2026, 11, 1, 5, 0, tz=UTC).timestamp(), 120)
        self.assertEqual(slots, ["2026-11-01T05:30:00Z"], "the repeated 01:30 fires once")


class ConsoleTests(unittest.TestCase):
    def test_the_slash_command_parses_list_and_operations_only(self):
        self.assertEqual(tui_model.parse_input_line("/schedule", "alpha").args["action"], "list")
        intent = tui_model.parse_input_line("/schedule run brand", "alpha")
        self.assertEqual((intent.kind, intent.args["action"], intent.args["ref"]), ("schedule", "run", "brand"))
        self.assertEqual(tui_model.parse_input_line("/schedule add scan --every daily", "alpha").kind, "error")
        self.assertIn("/schedule", tui_model.SLASH_USAGE)

    def test_the_console_lists_and_fires_through_the_cli(self):
        with TempState() as ts:
            cli(ts, ["schedule", "add", "Triage yesterday's brand mentions", "--every", "weekdays", "--to", "all", "--name", "brand"])
            env = env_no_daemon(ts, TZ="Europe/Berlin")
            state = console.ConsoleState(ts.layout, "alpha", env)
            state.width = 120
            model = console.build_model(ts.layout, "alpha", state, env=env)

            timeouts = []

            def fake_cli(args, env, timeout=console.CLI_TIMEOUT_S):
                timeouts.append(timeout)
                with mock.patch.object(S, "_now", lambda: T_ADD):
                    return json_out(run_cli(["--json"] + list(args), env, FakeApi()))

            with mock.patch("herdr_team.console.run_cli", side_effect=fake_cli):
                console.execute_intent(tui_model.parse_input_line("/schedule", "alpha"), model, state, FakeApi())
                self.assertIn("s1 brand weekdays 09:00 (Europe/Berlin) -> all: next 2026-09-23 09:00", "\n".join(model.peek))
                console.execute_intent(tui_model.parse_input_line("/schedule run brand", "alpha"), model, state, FakeApi())
                self.assertEqual(model.status, "brand: posted #1")
            self.assertGreater(timeouts[-1], timeouts[0], "a run may wait on its precheck")


class PrecheckProcessTests(unittest.TestCase):
    def test_exit_codes_output_and_a_killed_process_group(self):
        with TempState() as ts:
            env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(ts.home), "LANG": "C"}
            ok = S.run_precheck("echo fresh mentions", os.fspath(ts.tmp), env, 5)
            self.assertEqual((ok.status, ok.exit_code, ok.output), ("ok", 0, "fresh mentions"))
            self.assertEqual(S.run_precheck("exit 7", os.fspath(ts.tmp), env, 5).exit_code, 7)
            started = time.monotonic()
            slow = S.run_precheck("sleep 5; echo never", os.fspath(ts.tmp), env, 0.3)
            self.assertEqual(slow.status, "timeout")
            self.assertLess(time.monotonic() - started, 3.0, "the sleeping child died with the shell")
            self.assertEqual(S.run_precheck("true", os.fspath(ts.tmp / "missing"), env, 5).status, "error")


if __name__ == "__main__":
    unittest.main()

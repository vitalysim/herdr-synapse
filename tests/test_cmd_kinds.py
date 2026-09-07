"""`herdr-synapse kinds list|trust|untrust`: the owner override behind gate 4."""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from herdr_team import cli, roster, store
from support import FakeApi, TempState


def run_cli(argv, env, api=None):
    out, err = io.StringIO(), io.StringIO()
    fake = api if api is not None else FakeApi()
    with mock.patch("herdr_team.api.HerdrApi", lambda socket_path, env=None, **kw: fake):
        code = cli.main(list(argv), env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def json_out(result):
    code, out, err = result
    return code, (json.loads(out) if out.strip() else None), (json.loads(err.splitlines()[0]) if err.strip() and err.lstrip().startswith("{") else err)


class KindsCommandTests(unittest.TestCase):
    def test_list_on_a_fresh_session_is_empty_and_points_at_trust(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "kinds", "list"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["kinds"], [])
            code, out, _ = run_cli(["--team", "alpha", "kinds", "list"], ts.env)
            self.assertEqual(code, 0)
            self.assertIn("kinds trust", out)

    def test_trust_writes_the_override_the_gate_reads(self):
        with TempState() as ts:
            self.assertFalse(roster.kind_trusted(store.read_json(ts.session.kinds_json, default=None), "claude"))
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "kinds", "trust", "claude", "--reason", "verified in rig"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertTrue(payload["row"]["delivers"])
            self.assertTrue(payload["row"]["trusted"])
            doc = store.read_json(ts.session.kinds_json, default=None)
            self.assertTrue(roster.kind_trusted(doc, "claude"))
            self.assertEqual(doc["claude"]["reason"], "verified in rig")
            self.assertEqual(doc["claude"]["trusted_by"], "owner")
            self.assertIn("trusted_at", doc["claude"])

    def test_trust_keeps_other_entries_and_probe_data(self):
        with TempState() as ts:
            store.write_json(ts.session.kinds_json, {"codex": {"probe": {"ok": True}}, "claude": {"multiline": {"one_submission": True}}})
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "kinds", "trust", "claude"], ts.env))
            self.assertEqual(code, 0, err)
            doc = store.read_json(ts.session.kinds_json, default=None)
            self.assertEqual(doc["codex"], {"probe": {"ok": True}})
            self.assertEqual(doc["claude"]["multiline"], {"one_submission": True})
            self.assertEqual(payload["row"]["multiline"], {"one_submission": True})
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "kinds", "list"], ts.env))
            self.assertEqual(code, 0, err)
            rows = {r["kind"]: r for r in payload["kinds"]}
            self.assertTrue(rows["codex"]["delivers"] and rows["codex"]["probe_ok"] and not rows["codex"]["trusted"])
            self.assertTrue(rows["claude"]["delivers"] and rows["claude"]["trusted"])

    def test_untrust_removes_only_the_override(self):
        with TempState() as ts:
            store.write_json(ts.session.kinds_json, {"claude": {"trusted": True, "trusted_at": "x", "trusted_by": "owner", "reason": "r", "probe": {"ok": False}}})
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "kinds", "untrust", "claude"], ts.env))
            self.assertEqual(code, 0, err)
            self.assertFalse(payload["row"]["delivers"])
            doc = store.read_json(ts.session.kinds_json, default=None)
            self.assertEqual(doc["claude"], {"probe": {"ok": False}})
            self.assertFalse(roster.kind_trusted(doc, "claude"))

    def test_unknown_kind_and_missing_kind_are_refused(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "kinds", "trust", "not-a-kind"], ts.env))
            self.assertEqual(code, 1)
            self.assertEqual(err["code"], "kind_unknown")
            self.assertIn("claude", err["kinds"])
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "kinds", "trust"], ts.env))
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

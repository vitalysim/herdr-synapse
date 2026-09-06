"""``skills/herdr-team/SKILL.md`` contract (plan 9.1) and ``skill install|check`` (plan 9.3)."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from herdr_team import SKILL_VERSION, VERSION, cli, cmd_skill, paths

SKILL = paths.skill_file()


def run_cli(argv: List[str], env: Dict[str, str]):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class SkillFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()

    def frontmatter(self) -> Dict[str, str]:
        self.assertEqual(self.lines[0], "---")
        end = self.lines.index("---", 1)
        fm: Dict[str, str] = {}
        for line in self.lines[1:end]:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip().strip('"')
        return fm

    def test_frontmatter_name_and_gated_description(self):
        fm = self.frontmatter()
        self.assertEqual(fm["name"], "herdr-team")
        description = fm["description"]
        self.assertIn("HERDR_ENV=1", description)
        self.assertIn("herdr-team me", description)
        self.assertIn("[herdr-team", description)
        self.assertRegex(description, r"[Oo]nly when|[Oo]nly if|[Oo]nly use")

    def test_body_length_and_version_marker(self):
        self.assertLessEqual(len(self.lines), 150, "SKILL.md body must stay at or under 150 lines")
        import re

        found = re.search(r"<!-- herdr-team skill v(\d+), cli >= ([0-9]+\.[0-9]+) -->", self.text)
        self.assertIsNotNone(found, "SKILL.md must carry the version marker")
        assert found is not None
        self.assertEqual(int(found.group(1)), SKILL_VERSION)
        self.assertEqual(cmd_skill.skill_version_of(self.text), SKILL_VERSION)
        # The floor the marker advertises must be a CLI that actually exists.
        self.assertTrue(VERSION.startswith(found.group(2)), "the marker's cli floor matches the plugin version line")

    def test_gate_and_commands(self):
        self.assertIn('test "${HERDR_ENV:-}" = 1 && herdr-team me', self.text)
        for command in ("herdr-team me", "herdr-team who", "herdr-team charter", "herdr-team board --new", "herdr-team post", "herdr-team task", "herdr-team ack"):
            self.assertIn(command, self.text, command)
        self.assertIn("--help", self.text)
        self.assertRegex(self.text, r"--help[^\n]*authority")
        self.assertRegex(self.text, r"[Nn]ever create a team")

    def test_authority_and_request_rules(self):
        lowered = self.text.lower()
        self.assertIn("operator authority", lowered)
        self.assertRegex(lowered, r"charter[^\n]*brief|brief[^\n]*charter")
        self.assertRegex(lowered, r"anyone other than `?human`?[^\n]*request|other than `?human`?")
        self.assertRegex(lowered, r"context, not a task")
        self.assertIn("[herdr-team", self.text)
        self.assertIn("post --kind question --to human", self.text)

    def test_discipline(self):
        lowered = self.text.lower()
        self.assertIn("start of every turn", lowered)
        self.assertIn("end of every task", lowered)
        self.assertIn("500 characters", lowered)
        self.assertIn("--ref", self.text)
        self.assertRegex(lowered, r"never post secrets|never post[^\n]*secrets")
        for kind in ("done", "blocked", "request", "question"):
            self.assertIn("--kind {}".format(kind), self.text)

    def test_peer_rules(self):
        lowered = self.text.lower()
        self.assertRegex(lowered, r"teammates are peers")
        for forbidden in ("agent prompt", "send-keys", "agent rename", "pane close", "agent read", "integration install", "plugin link", "server stop"):
            self.assertIn(forbidden, lowered, forbidden)
        self.assertRegex(lowered, r"herdr config")
        self.assertRegex(lowered, r"overrides[^\n]*upstream|upstream[^\n]*teammates only")
        self.assertRegex(lowered, r"never `send-keys` or `send-text` into any pane you did not start")
        self.assertIn("board --kind direct", lowered)
        self.assertNotRegex(lowered, r"operator authority[^\n]*raw line|raw line[^\n]*operator authority")

    def test_skill_flag_prints_the_file(self):
        code, out, err = run_cli(["--skill"], {})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, self.text if self.text.endswith("\n") else self.text + "\n")
        code, out, _ = run_cli(["--skill", "--json"], {})
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["skill_version"], SKILL_VERSION)
        self.assertEqual(payload["skill"], self.text)


class SkillInstallCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ht-skill-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {"HOME": os.fspath(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        self.source = SKILL.read_text(encoding="utf-8")

    def skill(self, *argv: str, home: Optional[Path] = None):
        args = ["skill"] + list(argv) + ["--json"]
        if home is not None:
            args += ["--home", os.fspath(home)]
        code, out, err = run_cli(args, self.env)
        payload = json.loads(out) if out.strip() else None
        error = json.loads(err.splitlines()[0]) if err.strip() else None
        return code, payload, error

    def path(self, *parts: str) -> Path:
        return self.home.joinpath(*parts)

    def agent_dirs(self, *names: str) -> None:
        for name in names:
            self.path(name).mkdir(parents=True, exist_ok=True)


class SkillInstallTests(SkillInstallCase):
    def test_layout_matches_npx_skills(self):
        self.agent_dirs(".claude", ".codex/skills/.system/builtin", ".gemini")
        (self.path(".codex/skills/.system/builtin") / "SKILL.md").write_text("system skill\n", encoding="utf-8")
        code, payload, _ = self.skill("install")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["version"], SKILL_VERSION)
        canonical = self.path(".agents", "skills", "herdr-team", "SKILL.md")
        self.assertEqual(payload["canonical"], os.fspath(canonical))
        self.assertEqual(canonical.read_text(encoding="utf-8"), self.source)
        self.assertFalse(canonical.parent.is_symlink())
        claude_copy = self.path(".claude", "skills", "herdr-team", "SKILL.md")
        self.assertTrue(claude_copy.is_file() and not claude_copy.parent.is_symlink(), "real copy in ~/.claude/skills")
        self.assertEqual(claude_copy.read_text(encoding="utf-8"), self.source)
        for agent in (".codex", ".gemini"):
            link = self.path(agent, "skills", "herdr-team")
            self.assertTrue(link.is_symlink(), agent)
            self.assertEqual(Path(os.path.realpath(link)), Path(os.path.realpath(canonical.parent)))
            self.assertEqual((link / "SKILL.md").read_text(encoding="utf-8"), self.source)
        self.assertFalse(self.path(".copilot").exists(), "absent agent dirs are never created")
        self.assertEqual((self.path(".codex/skills/.system/builtin") / "SKILL.md").read_text(encoding="utf-8"), "system skill\n")
        by_path = {e["path"]: e for e in payload["installed"]}
        self.assertEqual(by_path[os.fspath(canonical)]["kind"], "canonical")
        self.assertEqual(by_path[os.fspath(claude_copy)]["kind"], "copy")
        self.assertEqual(by_path[os.fspath(self.path(".codex", "skills", "herdr-team"))]["kind"], "symlink")
        skipped = {s["path"]: s["reason"] for s in payload["skipped"]}
        self.assertEqual(skipped, {os.fspath(self.path(".copilot", "skills", "herdr-team")): "agent_dir_missing"})
        self.assertEqual(payload["stale"], [])

    def test_install_is_idempotent_and_check_passes(self):
        self.agent_dirs(".claude", ".copilot")
        self.skill("install")
        code, payload, _ = self.skill("install")
        self.assertEqual(code, 0)
        self.assertTrue(all(e["state"] == "unchanged" for e in payload["installed"]), payload["installed"])
        code, payload, _ = self.skill("check")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["stale"], [])
        self.assertTrue(all(e["state"] == "current" for e in payload["installed"]))

    def test_check_before_install_and_after_a_stale_copy(self):
        self.agent_dirs(".claude", ".codex")
        code, payload, _ = self.skill("check")
        self.assertEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertTrue(all(s["reason"] in ("missing", "agent_dir_missing") for s in payload["skipped"]), payload["skipped"])
        self.assertEqual(payload["installed"], [])
        self.skill("install")
        stale_copy = self.path(".claude", "skills", "herdr-team", "SKILL.md")
        stale_copy.write_text(self.source.replace("skill v{}".format(SKILL_VERSION), "skill v0", 1) + "\n# edited\n", encoding="utf-8")
        code, payload, _ = self.skill("check")
        self.assertFalse(payload["ok"])
        self.assertEqual([s["path"] for s in payload["stale"]], [os.fspath(stale_copy)])
        self.assertEqual(payload["stale"][0]["reason"], "hash_mismatch")
        self.assertEqual(payload["stale"][0]["version"], 0)
        # install repairs it
        code, payload, _ = self.skill("install")
        self.assertTrue(payload["ok"])
        self.assertEqual(stale_copy.read_text(encoding="utf-8"), self.source)
        self.assertTrue(self.skill("check")[1]["ok"])

    def test_dry_run_writes_nothing(self):
        self.agent_dirs(".claude", ".codex")
        code, payload, _ = self.skill("install", "--check")
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual({e["state"] for e in payload["installed"]}, {"would_write", "would_link"})
        self.assertFalse(self.path(".agents").exists())
        self.assertFalse(self.path(".claude", "skills").exists())
        self.assertFalse(self.path(".codex", "skills").exists())

    def test_foreign_directory_refused_without_force(self):
        self.agent_dirs(".claude")
        foreign = self.path(".agents", "skills", "herdr-team")
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("---\nname: herdr-team\n---\nsomeone else's skill\n", encoding="utf-8")
        (foreign / "extra.txt").write_text("keep", encoding="utf-8")
        code, payload, _ = self.skill("install")
        self.assertEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertIn({"path": os.fspath(foreign), "reason": "foreign"}, payload["skipped"])
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "---\nname: herdr-team\n---\nsomeone else's skill\n")
        self.assertTrue((foreign / "extra.txt").exists())
        # the claude copy still lands; only the foreign target is skipped
        self.assertTrue(self.path(".claude", "skills", "herdr-team", "SKILL.md").is_file())
        code, payload, _ = self.skill("check")
        self.assertFalse(payload["ok"])
        self.assertIn("foreign", {s["reason"] for s in payload["skipped"]})
        code, payload, _ = self.skill("install", "--force")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), self.source)
        self.assertFalse((foreign / "extra.txt").exists(), "--force replaces the whole foreign directory")

    def test_foreign_symlink_refused_without_force(self):
        self.agent_dirs(".codex", ".claude")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "SKILL.md").write_text("other\n", encoding="utf-8")
        link = self.path(".codex", "skills", "herdr-team")
        link.parent.mkdir(parents=True)
        os.symlink(os.fspath(elsewhere), os.fspath(link))
        code, payload, _ = self.skill("install")
        self.assertFalse(payload["ok"])
        self.assertIn({"path": os.fspath(link), "reason": "foreign_symlink"}, payload["skipped"])
        self.assertEqual(Path(os.path.realpath(link)), Path(os.path.realpath(elsewhere)))
        code, payload, _ = self.skill("install", "--force")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(Path(os.path.realpath(link)), Path(os.path.realpath(self.path(".agents", "skills", "herdr-team"))))
        self.assertEqual((elsewhere / "SKILL.md").read_text(encoding="utf-8"), "other\n", "the link target is never touched")

    def test_symlinked_canonical_is_foreign(self):
        self.agent_dirs(".claude")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "SKILL.md").write_text(self.source, encoding="utf-8")
        canonical = self.path(".agents", "skills", "herdr-team")
        canonical.parent.mkdir(parents=True)
        os.symlink(os.fspath(elsewhere), os.fspath(canonical))
        code, payload, _ = self.skill("install")
        self.assertFalse(payload["ok"])
        self.assertIn({"path": os.fspath(canonical), "reason": "foreign_symlink"}, payload["skipped"])
        self.assertTrue(canonical.is_symlink())

    def test_old_copy_where_a_symlink_belongs_is_reported_stale(self):
        self.agent_dirs(".gemini")
        old = self.path(".gemini", "skills", "herdr-team")
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text(self.source, encoding="utf-8")
        code, payload, _ = self.skill("check")
        self.assertIn({"path": os.fspath(old), "reason": "copy_instead_of_symlink"}, payload["stale"])
        code, payload, _ = self.skill("install")
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(old.is_symlink())

    def test_home_override_and_human_output(self):
        other = self.tmp / "other-home"
        (other / ".claude").mkdir(parents=True)
        code, payload, _ = self.skill("install", home=other)
        self.assertTrue(payload["ok"])
        self.assertTrue((other / ".agents" / "skills" / "herdr-team" / "SKILL.md").is_file())
        self.assertFalse(self.path(".agents").exists())
        code, out, err = run_cli(["skill", "check", "--home", os.fspath(other)], self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("skill check (v{}): ok".format(SKILL_VERSION)), out)
        self.assertIn("canonical", out)

    def test_usage_errors(self):
        code, out, err = run_cli(["skill"], self.env)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err.splitlines()[0])["code"], "usage")
        code, out, err = run_cli(["skill", "remove", "--json"], self.env)
        self.assertEqual(code, 2)

    def test_version_marker_helper(self):
        self.assertEqual(cmd_skill.skill_version_of("<!-- herdr-team skill v7, cli >= 0.3 -->"), 7)
        self.assertIsNone(cmd_skill.skill_version_of("no marker"))
        self.assertEqual(cmd_skill.sha256("a"), "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb")


if __name__ == "__main__":
    unittest.main()

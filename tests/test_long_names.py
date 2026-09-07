"""Longer team and role names, without breaking the member name.

A member name IS its Herdr agent name, and Herdr caps those at 32 bytes
(`src/app/agents.rs::valid_agent_name`), so that ceiling is not ours to move.
Team names were capped at 15 and roles at 32, both of which a real team hit
exactly. Raising them naively made every member of a long-named team collapse
to the same truncated team string.
"""

from __future__ import annotations

import unittest

from herdr_team import paths, roster, sanitize
from herdr_team.errors import HerdrTeamError


class LimitTests(unittest.TestCase):
    def test_the_new_ceilings(self):
        self.assertEqual((paths.MAX_TEAM_CHARS, paths.MAX_ROLE_CHARS), (32, 64))

    def test_the_member_ceiling_is_herdrs_and_unchanged(self):
        self.assertEqual(paths.MAX_MEMBER_CHARS, 32)
        self.assertEqual(roster.validate_member_name("a" * 32), "a" * 32)
        with self.assertRaises(HerdrTeamError):
            roster.validate_member_name("a" * 33)

    def test_a_long_team_is_accepted(self):
        for name in ("clickhouse-vulnerability-hunt", "a" * 32):
            self.assertEqual(paths.validate_team_name(name), name)
        with self.assertRaises(HerdrTeamError):
            paths.validate_team_name("a" * 33)

    def test_a_long_role_is_accepted(self):
        for role in ("claude-vulnerability-research-specialist", "a" * 64):
            self.assertEqual(roster.validate_role(role), role)
        with self.assertRaises(HerdrTeamError):
            roster.validate_role("a" * 65)

    def test_the_error_message_names_the_real_limit(self):
        with self.assertRaises(HerdrTeamError) as caught:
            roster.validate_role("a" * 65)
        self.assertIn("up to 64 characters", caught.exception.message)
        self.assertIn("yours is 65", caught.exception.message)

    def test_sanitize_agrees_with_the_patterns(self):
        self.assertEqual(sanitize.sanitize_name("a" * 32, "team"), "a" * 32)
        self.assertEqual(sanitize.sanitize_role("a" * 64), "a" * 64)


class MemberNameFittingTests(unittest.TestCase):
    """The role must survive, and two members must never share a name."""

    ROLES = ("manager", "reviewer", "vulnerability-research", "ideation")

    def names(self, team: str):
        return [roster.fit_member_name(team, role) for role in self.ROLES]

    def test_every_derived_name_fits_herdrs_cap(self):
        for team in ("red-dev", "clickhouse-hunt", "clickhouse-vulnerability-hunt", "a" * 32):
            for name in self.names(team):
                self.assertLessEqual(len(name), 32, (team, name))

    def test_a_long_team_does_not_collapse_its_members_to_one_name(self):
        """Truncating the team gave every member the same name."""
        for team in ("clickhouse-vulnerability-hunt", "a" * 32, "a" * 31):
            names = self.names(team)
            self.assertEqual(len(set(names)), len(names), (team, names))

    def test_the_role_always_survives(self):
        for team in ("clickhouse-vulnerability-hunt", "a" * 32):
            for role, name in zip(self.ROLES, self.names(team)):
                self.assertTrue(name.endswith(role) or role.endswith(name.rsplit("-", 1)[-1]), (team, role, name))

    def test_a_short_team_is_unchanged(self):
        self.assertEqual(roster.fit_member_name("red-dev", "manager"), "red-dev-manager")
        self.assertEqual(roster.fit_member_name("alpha", "reviewer"), "alpha-reviewer")

    def test_whole_team_segments_are_kept_where_they_fit(self):
        name = roster.fit_member_name("clickhouse-vulnerability-hunt", "manager")
        self.assertEqual(name, "clickhouse-vulnerability-manager")
        self.assertLessEqual(len(name), 32)

    def test_the_generic_role_lead_still_goes_first(self):
        self.assertEqual(roster.fit_member_name("alpha", "codex-reviewer-of-everything"), "alpha-reviewer-of-everything")

    def test_derive_name_accepts_the_new_lengths(self):
        name = roster.derive_name("clickhouse-vulnerability-hunt", "vulnerability-research")
        self.assertLessEqual(len(name), 32)
        self.assertEqual(roster.validate_member_name(name), name)

    def test_plain_naming_still_needs_a_role_that_fits_a_member_name(self):
        roster.derive_name("clickhouse-vulnerability-hunt", "a" * 32, naming="plain")
        with self.assertRaises(HerdrTeamError):
            roster.derive_name("clickhouse-vulnerability-hunt", "a" * 33, naming="plain")


if __name__ == "__main__":
    unittest.main()

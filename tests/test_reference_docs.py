"""The GitHub command/shortcut reference follows the executable registries."""

import unittest

from herdr_team import cmd_misc, reference_docs, roster, sanitize, tui_model
from herdr_team.cli import load_commands
from support import PLUGIN_ROOT


class ReferenceDocsTests(unittest.TestCase):
    def setUp(self):
        self.path = PLUGIN_ROOT / "docs" / "reference.md"
        self.document = self.path.read_text(encoding="utf-8")

    def _section(self, name):
        begin, end = reference_docs.SECTION_MARKERS[name]
        return self.document.split(begin, 1)[1].split(end, 1)[0]

    def test_generated_sections_are_current(self):
        self.assertEqual(reference_docs.render_document(self.document), self.document)
        self.assertEqual(reference_docs.main(["--check"]), 0)

    def test_every_cli_command_appears_once(self):
        commands = load_commands()
        self.assertEqual(len(commands), 91)
        self.assertEqual(len([command for command in commands if not command.hidden]), 81)
        self.assertEqual(len([command for command in commands if command.hidden]), 10)
        section = self._section("cli")
        for command in commands:
            token = "<summary><code>{}</code>".format(command.name)
            self.assertEqual(section.count(token), 1, command.name)

    def test_every_default_key_and_manifest_action_appears_once(self):
        section = self._section("actions")
        manifest = reference_docs.MANIFEST_PATH.read_text(encoding="utf-8")
        actions = reference_docs.manifest_actions(manifest)
        self.assertEqual(len(actions), 9)
        for action in actions:
            invocation = "herdr plugin action invoke {}.{}".format(reference_docs.PLUGIN_ID, action["id"])
            self.assertEqual(section.count(invocation), 1, action["id"])
        for action in cmd_misc.KEY_ACTIONS:
            row = "| `{}` | `{}` |".format(
                cmd_misc.KEYS[action], "{}.{}".format(reference_docs.PLUGIN_ID, action)
            )
            self.assertEqual(section.count(row), 1, action)

    def test_every_console_command_appears_once(self):
        section = self._section("console")
        self.assertEqual(set(tui_model.SLASH_COMMANDS), set(tui_model.SLASH_USAGE))
        for command in tui_model.SLASH_COMMANDS:
            self.assertEqual(section.count("| `{}` |".format(command)), 1, command)

    def test_reference_is_linked_from_primary_docs(self):
        for relative in ("README.md", "docs/cli.md", "docs/capabilities.md"):
            text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("reference.md", text, relative)

    def test_readme_pi_core_support_keeps_hooks_and_account_usage_distinct(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("| Pi (`pi`) | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ | ◐ |", readme)
        self.assertIn("Hooks (prompt/stop)", readme)
        self.assertIn("hooks install pi", readme)
        self.assertIn("explicitly labelled token estimates", readme)
        self.assertIn("provider-account quotas, not context tracking", readme)

    def test_readme_compatibility_matrix_covers_every_herdr_state_integration(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        for feature in (
            "Teams and authority", "Board and collaboration", "Team-to-team coordination",
            "Instructions and knowledge", "Human interaction and UI", "Operations and safety",
        ):
            self.assertIn("| {} |".format(feature), readme, feature)
        section = readme.split("<!-- BEGIN: integration-compatibility -->", 1)[1].split(
            "<!-- END: integration-compatibility -->", 1)[0]
        for feature in (
            "Resume", "Idle nudge", "Safe `!`", "Running `!!` / interrupt",
            "Ask wait", "Hooks", "Model / effort", "Context", "Compact / clear", "Usage",
        ):
            self.assertIn(feature, section, feature)
        rows = [line for line in section.splitlines() if line.startswith("|")]
        width = len(rows[0].split("|"))
        for row in rows:
            self.assertEqual(len(row.split("|")), width, row)
        integration_ids = {
            source.split(":", 1)[1].replace("_", "-") for source in roster.RESUME_COMMANDS
        }
        self.assertEqual(len(integration_ids), 17)
        for integration_id in integration_ids:
            self.assertEqual(section.count("`{}`".format(integration_id)), 1, integration_id)
        integrated_kinds = {entry[0] for entry in roster.RESUME_COMMANDS.values()}
        for kind in sanitize.KIND_LABELS - integrated_kinds:
            self.assertIn("`{}`".format(kind), readme, kind)


if __name__ == "__main__":
    unittest.main()

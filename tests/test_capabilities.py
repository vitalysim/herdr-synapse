"""Live Herdr capability discovery: release strings are not feature gates."""

import unittest

from herdr_team import capabilities
from support import FakeApi


class CapabilityProbeTests(unittest.TestCase):
    def test_supported_method_probe_never_carries_terminal_text(self):
        api = FakeApi()
        self.assertTrue(capabilities.probe_atomic_idle_prompt(api))
        method, params = api.calls[-1]
        self.assertEqual(method, "agent.prompt_if_idle")
        self.assertEqual(params["text"], "")
        self.assertEqual(params["target"], "__herdr_synapse_capability_probe__")

    def test_missing_method_and_unreachable_server_are_distinct(self):
        api = FakeApi()
        api.set_error("agent.prompt_if_idle", "invalid_request", "unknown variant agent.prompt_if_idle")
        self.assertFalse(capabilities.probe_atomic_idle_prompt(api))
        api.unreachable = True
        self.assertIsNone(capabilities.probe_atomic_idle_prompt(api))

    def test_daemon_json_reader_is_backward_compatible(self):
        self.assertIsNone(capabilities.atomic_idle_prompt_of({"version": "0.15.5"}))
        self.assertIsNone(capabilities.atomic_idle_prompt_of({"capabilities": {"atomic_idle_prompt": None}}))
        self.assertFalse(capabilities.atomic_idle_prompt_of({"capabilities": {"atomic_idle_prompt": False}}))
        self.assertTrue(capabilities.atomic_idle_prompt_of({"capabilities": {"atomic_idle_prompt": True}}))


if __name__ == "__main__":
    unittest.main()

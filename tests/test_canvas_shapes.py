"""Repair predictions must not reuse another team's same-id geometry."""
import unittest
from herdr_team.canvas_kinds import graph as G


class Helpers(unittest.TestCase):
    def test_the_check_memo_tells_two_boards_with_the_same_ids_apart(self):
        root = {"id": "E-1", "updated_seq": 3}
        one = [{"id": "E-2", "x": 0, "y": 0, "w": 10, "h": 10, "updated_seq": 3}]
        two = [{"id": "E-2", "x": 500, "y": 0, "w": 10, "h": 10, "updated_seq": 3}]
        self.assertNotEqual(G._fresh_key(root, one), G._fresh_key(root, two))

"""The scene3d relation solver (canvas v2 phase 4, 3.3 and 8.2): every relation and layout with worked coordinates.

Axes: y up, x right, z toward the viewer. An object's ``pos`` is the centre of
its footprint at its base; ``aabb`` is ``[x0, y0, z0, x1, y1, z1]``.
"""
from __future__ import annotations

import random
import time
import unittest

from herdr_team.canvas_scene3d import _solver, _spec
from herdr_team.canvas_scene3d import _vec as V
from herdr_team.errors import HerdrTeamError

#: A 2 × 1 × 2 box standing at the origin: the reference most cases place against.
A = {"id": "a", "shape": "box", "size": [2, 1, 2]}
UNIT = [1, 1, 1]


def solve(*objects, links=()):
    """Validate like the op does, then solve."""
    objs = [_spec.object_item(_spec.PURE, o, "objects[{}]".format(i)) for i, o in enumerate(objects)]
    lks = [_spec.link_item(_spec.PURE, l, "links[{}]".format(i)) for i, l in enumerate(links)]
    _spec.number_links(lks)
    _spec.check_refs(_spec.PURE, objs, lks)
    return _solver.solve(objs, lks)


def pos(solved, ident):
    return solved["objects"][ident]["pos"]


def box(solved, ident):
    return solved["objects"][ident]["aabb"]


def refusal(*objects):
    try:
        solve(*objects)
    except HerdrTeamError as err:
        return err
    raise AssertionError("expected a refusal")


class Relations(unittest.TestCase):
    def test_on_rests_on_the_top_centred_or_offset_by_at(self):
        s = solve(A, {"id": "b", "shape": "box", "on": "a"})
        self.assertEqual(pos(s, "b"), [0.0, 1.0, 0.0])
        self.assertEqual(s["objects"]["b"]["rel"], "on a")
        s = solve(A, {"id": "b", "shape": "box", "size": [0.5, 0.5, 0.5], "on": "a", "at": [0.5, -0.25]})
        self.assertEqual(pos(s, "b"), [0.5, 1.0, -0.25])
        self.assertEqual(s["objects"]["b"]["rel"], "on a, at (0.5, −0.2)")

    def test_above_and_below_keep_the_gap(self):
        s = solve(A, {"id": "b", "shape": "box", "above": "a", "gap": 0.5})
        self.assertEqual(pos(s, "b"), [0.0, 1.5, 0.0])
        self.assertEqual(s["objects"]["b"]["rel"], "above a (gap 0.50)")
        s = solve(A, {"id": "b", "shape": "box", "below": "a", "gap": 0.2})
        self.assertEqual(box(s, "b")[4], -0.2, "its top is the gap under a's base")
        self.assertEqual(pos(s, "b"), [0.0, -1.2, 0.0])

    def test_left_and_right_of_are_base_aligned_and_centred_in_depth(self):
        s = solve(A, {"id": "b", "shape": "box", "left_of": "a", "gap": 0.3}, {"id": "c", "shape": "box", "right_of": "a", "gap": 0.3})
        self.assertEqual(pos(s, "b"), [-1.8, 0.0, 0.0])
        self.assertEqual(pos(s, "c"), [1.8, 0.0, 0.0])
        self.assertEqual(box(s, "c")[0], 1.3)

    def test_in_front_of_and_behind(self):
        s = solve(A, {"id": "f", "shape": "box", "in_front_of": "a", "gap": 0.3}, {"id": "k", "shape": "box", "behind": "a", "gap": 0.3})
        self.assertEqual(pos(s, "f"), [0.0, 0.0, 1.8])
        self.assertEqual(pos(s, "k"), [0.0, 0.0, -1.8])

    def test_gap_tokens_are_shares_of_the_larger_footprint(self):
        # m = 25 % of max(1, 1, 2, 2) = 0.5; s = 10 % = 0.2; l = 50 % = 1.0.
        for token, gap in (("s", 0.2), ("m", 0.5), ("l", 1.0)):
            s = solve(A, {"id": "b", "shape": "box", "right_of": "a", "gap": token})
            self.assertEqual(box(s, "b")[0], 1.0 + gap, token)
        s = solve(A, {"id": "b", "shape": "box", "right_of": "a"})
        self.assertEqual(box(s, "b")[0], 1.5, "the default gap is m")

    def test_align_on_the_axes_a_relation_does_not_set(self):
        small = {"id": "b", "shape": "box", "size": [0.5, 0.5, 0.5], "right_of": "a", "gap": 0}
        self.assertEqual(pos(solve(A, small), "b"), [1.25, 0.0, 0.0], "default: base-aligned, centred in depth")
        self.assertEqual(pos(solve(A, dict(small, align="start")), "b"), [1.25, 0.0, -0.75])
        self.assertEqual(pos(solve(A, dict(small, align="center")), "b"), [1.25, 0.25, 0.0])
        self.assertEqual(pos(solve(A, dict(small, align="end")), "b"), [1.25, 0.5, 0.75])

    def test_inside_sits_on_the_inner_floor_and_must_fit(self):
        host = {"id": "h", "shape": "box", "size": [2, 2, 2]}
        s = solve(host, {"id": "b", "shape": "box", "inside": "h"})
        self.assertEqual(pos(s, "b"), [0.0, 0.02, 0.0])
        self.assertEqual(s["conflicts"], [])
        s = solve(host, {"id": "b", "shape": "box", "size": [3, 1, 1], "inside": "h"})
        conflict = s["conflicts"][0]
        self.assertEqual(conflict["code"], "does_not_fit")
        self.assertEqual(conflict["ids"], ["b", "h"])
        self.assertIn("3.00×1.00×1.00 does not fit in 1.96×1.98×1.96", conflict["message"])
        self.assertEqual(conflict["fix"], {"update": {"objects": [{"id": "h", "size": [3.09, 2, 2]}]}})

    def test_around_spaces_siblings_evenly_from_minus_90_degrees(self):
        bus = {"id": "bus", "shape": "cylinder", "radius": 0.8, "height": 0.3}
        ring = [{"id": n, "shape": "box", "size": [0.6, 0.6, 0.6], "around": "bus"} for n in "abcd"]
        s = solve(bus, *ring)
        # Ring radius: bus footprint radius (hypot(1.6, 1.6) / 2) + gap m (25 % of 1.6) + own radius (hypot(.6, .6) / 2).
        radius = 1.1314 + 0.4 + 0.4243
        self.assertAlmostEqual(pos(s, "a")[0], 0.0, 3)
        self.assertAlmostEqual(pos(s, "a")[2], -radius, 3)
        self.assertAlmostEqual(pos(s, "b")[0], radius, 3)
        self.assertAlmostEqual(pos(s, "c")[2], radius, 3)
        self.assertAlmostEqual(pos(s, "d")[0], -radius, 3)
        self.assertTrue(s["objects"]["b"]["rel"].startswith("around bus (ring 1.96, 0°)"), s["objects"]["b"]["rel"])
        given = solve(bus, {"id": "a", "shape": "box", "around": "bus", "ring": 3, "angle": 0})
        self.assertEqual(pos(given, "a"), [3.0, 0.0, 0.0])

    def test_one_relation_per_axis_group(self):
        err = refusal(A, {"id": "b", "shape": "box", "right_of": "a", "left_of": "a"})
        self.assertEqual(err.details["field"], "objects[1].right_of")
        self.assertIn("left_of and right_of both place x; keep one", str(err))
        err = refusal(A, {"id": "b", "shape": "box", "on": "a", "above": "a"})
        self.assertIn("on and above both place y", str(err))
        err = refusal(A, {"id": "b", "shape": "box", "around": "a", "behind": "a"})
        self.assertIn("both place z", str(err))
        s = solve(A, {"id": "b", "shape": "box", "on": "a", "right_of": "a", "in_front_of": "a"})
        self.assertEqual(s["objects"]["b"]["rel"], "on a, right_of a (gap 0.50), in_front_of a (gap 0.50)")

    def test_pos_overrides_and_is_refused_with_a_relation(self):
        s = solve(A, {"id": "b", "shape": "box", "pos": [3, 2, 1]})
        self.assertEqual(pos(s, "b"), [3.0, 2.0, 1.0])
        self.assertEqual(s["objects"]["b"]["rel"], "pos (3.0, 2.0, 1.0)")
        err = refusal(A, {"id": "b", "shape": "box", "pos": [3, 2, 1], "on": "a"})
        self.assertEqual(err.details["field"], "objects[1].pos")

    def test_a_cycle_is_refused_naming_it(self):
        err = refusal({"id": "a", "shape": "box", "right_of": "b"}, {"id": "b", "shape": "box", "right_of": "c"},
                      {"id": "c", "shape": "box", "above": "a"})
        self.assertEqual(err.code, "relation_cycle")
        self.assertIn("a → b → c → a", str(err))
        self.assertEqual(err.details["cycle"], ["a", "b", "c", "a"])
        err = refusal({"id": "a", "shape": "box", "on": "a"})
        self.assertIn("itself", str(err))

    def test_unrelated_objects_flow_in_a_row_on_the_ground(self):
        s = solve({"id": "a", "shape": "box"}, {"id": "b", "shape": "box", "size": [2, 1, 1]}, {"id": "c", "shape": "sphere"})
        self.assertEqual(pos(s, "a"), [0.0, 0.0, 0.0])
        # m between a (1 wide) and b (2 wide): 25 % of 2 = 0.5.
        self.assertEqual(box(s, "b")[0], 1.0)
        self.assertEqual(box(s, "c")[0], box(s, "b")[3] + 0.5)
        self.assertEqual(s["objects"]["b"]["rel"], "")

    def test_rotation_turns_the_box_it_is_placed_by(self):
        s = solve({"id": "r", "shape": "box", "size": [2, 1, 1], "rotate": 90})
        self.assertEqual(s["objects"]["r"]["ext"], [1.0, 1.0, 2.0])
        self.assertEqual(s["objects"]["r"]["rot"], [0.0, 90.0, 0.0])
        s = solve({"id": "r", "shape": "box", "size": [2, 1, 1], "rotate": [0, 0, 90]})
        self.assertEqual(s["objects"]["r"]["ext"], [1.0, 2.0, 1.0])


class Collisions(unittest.TestCase):
    def test_a_collision_is_pushed_along_the_relation_and_noted(self):
        s = solve(A, {"id": "b", "shape": "box", "right_of": "a", "gap": 0.3}, {"id": "c", "shape": "box", "right_of": "a", "gap": 0.3})
        self.assertEqual(pos(s, "c"), [3.1, 0.0, 0.0])
        self.assertEqual(s["notes"], ["c overlapped b by 1.00 m; moved right 1.30"])

    def test_three_pushes_then_a_conflict(self):
        items = [A] + [{"id": "d{}".format(i), "shape": "box", "size": [0.4, 0.4, 0.4], "on": "a"} for i in range(1, 6)]
        s = solve(*items)
        self.assertEqual(len(s["notes"]), 1 + 2 + 3 + 3)
        self.assertEqual([c["code"] for c in s["conflicts"]], ["unresolved_overlap"])
        conflict = s["conflicts"][0]
        self.assertEqual(conflict["ids"][0], "d5")
        self.assertEqual(conflict["fix"]["update"]["objects"][0]["above"], conflict["ids"][1])
        self.assertIsNone(conflict["fix"]["update"]["objects"][0]["on"])

    def test_overlap_true_is_left_where_it_is(self):
        s = solve(A, {"id": "b", "shape": "box", "pos": [0.5, 0.5, 0.5], "overlap": True})
        self.assertEqual(pos(s, "b"), [0.5, 0.5, 0.5])
        self.assertEqual(s["notes"], [])
        s = solve(A, {"id": "b", "shape": "box", "right_of": "a", "gap": 0}, {"id": "c", "shape": "box", "right_of": "a", "gap": 0,
                                                                              "overlap": True})
        self.assertEqual(pos(s, "c"), pos(s, "b"), "an intended intersection is not pushed")

    def test_contact_with_what_it_rests_on_is_no_collision(self):
        s = solve(A, {"id": "b", "shape": "box", "on": "a"}, {"id": "h", "shape": "box", "size": [3, 3, 3], "pos": [8, 0, 0]},
                  {"id": "i", "shape": "box", "inside": "h"})
        self.assertEqual(s["notes"], [])


class Layouts(unittest.TestCase):
    def group(self, layout, *sizes, **extra):
        items = [dict({"id": "g", "shape": "group", "layout": layout}, **extra)]
        items += [{"id": "c{}".format(i), "shape": "box", "size": size, "in": "g"} for i, size in enumerate(sizes)]
        return solve(*items)

    def test_row(self):
        s = self.group("row", [1, 1, 1], [2, 0.5, 1], gap=0.2)
        self.assertEqual(s["objects"]["g"]["ext"], [3.2, 1.0, 1.0])
        self.assertEqual(pos(s, "c0"), [-1.1, 0.0, 0.0])
        self.assertEqual(pos(s, "c1"), [0.6, 0.0, 0.0])
        self.assertEqual(s["objects"]["c1"]["rel"], "in g")
        self.assertEqual(s["objects"]["c1"]["parent"], "g")

    def test_stack(self):
        s = self.group("stack", [1, 0.2, 1], [1, 0.3, 1], [0.5, 0.1, 0.5], gap=0.05)
        self.assertEqual([pos(s, c)[1] for c in ("c0", "c1", "c2")], [0.0, 0.25, 0.6])
        self.assertEqual(s["objects"]["g"]["ext"], [1.0, 0.7, 1.0])

    def test_grid(self):
        s = self.group("grid", UNIT, UNIT, UNIT, cols=2, gap=0.5)
        self.assertEqual(pos(s, "c0"), [-0.75, 0.0, -0.75])
        self.assertEqual(pos(s, "c1"), [0.75, 0.0, -0.75])
        self.assertEqual(pos(s, "c2"), [-0.75, 0.0, 0.75])
        self.assertEqual(s["objects"]["g"]["ext"], [2.5, 1.0, 2.5])
        default = self.group("grid", UNIT, UNIT, UNIT, UNIT, UNIT)
        self.assertEqual(sorted({pos(default, "c{}".format(i))[0] for i in range(5)}).__len__(), 3, "3 columns: ceil(sqrt(5))")

    def test_ring(self):
        s = self.group("ring", [0.5, 0.5, 0.5], [0.5, 0.5, 0.5], [0.5, 0.5, 0.5], gap=0.2)
        radius = (0.5 * 2 ** 0.5 + 0.2) / (2 * 3 ** 0.5 / 2)
        first = pos(s, "c0")
        centre_z = (box(s, "g")[2] + box(s, "g")[5]) / 2
        self.assertAlmostEqual(first[0], 0.0, 4)
        centres = [(pos(s, c)[0], pos(s, c)[2]) for c in ("c0", "c1", "c2")]
        distances = [V.length((centres[i][0] - centres[j][0], 0, centres[i][1] - centres[j][1])) for i, j in ((0, 1), (1, 2), (2, 0))]
        for d in distances:
            self.assertAlmostEqual(d, radius * 3 ** 0.5, 3, "evenly spaced on the ring")
        self.assertLess(first[2], centre_z, "the first one is at −90° (toward −z)")

    def test_free_children_place_themselves_against_siblings(self):
        s = solve({"id": "g", "shape": "group", "layout": "free", "on": "a"}, A,
                  {"id": "x", "shape": "box", "in": "g"}, {"id": "y", "shape": "box", "in": "g", "above": "x", "gap": 0})
        self.assertEqual(pos(s, "y")[1] - pos(s, "x")[1], 1.0)
        self.assertEqual(pos(s, "x")[1], 1.0, "the group rests on a")
        self.assertEqual(s["objects"]["y"]["rel"], "in g, above x (gap 0.00)")
        err = refusal({"id": "g", "shape": "group"}, A, {"id": "x", "shape": "box", "in": "g", "on": "a"})
        self.assertIn("layout free", str(err))

    def test_nested_groups_are_laid_out_children_first(self):
        s = solve({"id": "outer", "shape": "group", "layout": "row", "gap": 0},
                  {"id": "inner", "shape": "group", "layout": "stack", "gap": 0, "in": "outer"},
                  {"id": "p", "shape": "box", "in": "inner"}, {"id": "q", "shape": "box", "in": "inner"},
                  {"id": "r", "shape": "box", "in": "outer"})
        self.assertEqual(s["objects"]["inner"]["ext"], [1.0, 2.0, 1.0])
        self.assertEqual(s["objects"]["outer"]["ext"], [2.0, 2.0, 1.0])
        self.assertEqual(box(s, "q"), [-1.0, 1.0, -0.5, 0.0, 2.0, 0.5])
        self.assertEqual(box(s, "r"), [0.0, 0.0, -0.5, 1.0, 1.0, 0.5])
        err = refusal({"id": "g", "shape": "group", "in": "h"}, {"id": "h", "shape": "group", "in": "g"})
        self.assertEqual(err.code, "relation_cycle")


class Floors(unittest.TestCase):
    """A plane with no ``size`` fits what stands on it (QA phase34: objects crammed in a corner of an oversized floor)."""

    def test_a_floor_covers_what_rests_on_it_with_a_margin_centred_under_it(self):
        found = solve({"id": "f", "shape": "plane"}, {"id": "a", "shape": "box", "size": [2, 1, 1], "on": "f", "at": [-3, 0]},
                      {"id": "b", "shape": "box", "size": [1, 1, 1], "on": "f", "right_of": "a", "gap": 1})
        # Footprint x −4…0 (4 wide), z −0.5…0.5 (1 deep); margin max(0.1 × 4, 0.25 × 1) = 0.4.
        self.assertEqual(found["objects"]["f"]["ext"], [4.8, 0.02, 1.8])
        self.assertEqual(box(found, "f"), [-4.4, 0.0, -0.9, 0.4, 0.02, 0.9])
        self.assertEqual(pos(found, "a"), [-3.0, 0.02, 0.0], "what stands on it stays where it was placed")
        self.assertEqual(found["bounds"][0], -4.4)
        self.assertEqual(solve({"id": "f", "shape": "plane", "size": "fit"}, {"id": "a", "shape": "box", "on": "f"})["objects"]["f"]["ext"],
                         [1.5, 0.02, 1.5])

    def test_what_is_placed_against_a_rider_counts_and_what_is_beside_the_floor_does_not(self):
        found = solve({"id": "f", "shape": "plane"}, {"id": "a", "shape": "box", "on": "f"},
                      {"id": "up", "shape": "sphere", "radius": 0.5, "above": "a", "at": [0, -2]},
                      {"id": "g", "shape": "group", "on": "f", "behind": "a"}, {"id": "g1", "shape": "box", "in": "g"},
                      {"id": "near", "shape": "box", "right_of": "f", "gap": 1})
        f = box(found, "f")
        for ident in ("a", "up", "g1"):
            b = box(found, ident)
            self.assertTrue(f[0] < b[0] and b[3] < f[3] and f[2] < b[2] and b[5] < f[5], ident)
        self.assertGreater(box(found, "near")[0], f[3] - 1e-9, "beside it, not on it")

    def test_a_given_size_is_kept_and_a_bare_floor_keeps_the_default(self):
        self.assertEqual(solve({"id": "f", "shape": "plane", "size": [8, 5]}, {"id": "a", "shape": "box", "on": "f"})["objects"]["f"]["ext"],
                         [8.0, 0.02, 5.0])
        self.assertEqual(solve({"id": "f", "shape": "plane"})["objects"]["f"]["ext"], [4.0, 0.02, 4.0])

    def test_a_floor_on_a_floor_is_fitted_first(self):
        found = solve({"id": "big", "shape": "plane"}, {"id": "top", "shape": "plane", "on": "big", "at": [2, 0]},
                      {"id": "a", "shape": "box", "size": [2, 1, 2], "on": "top"}, {"id": "b", "shape": "box", "on": "big", "at": [-2, 0]})
        top, big = box(found, "top"), box(found, "big")
        self.assertEqual(found["objects"]["top"]["ext"], [3.0, 0.02, 3.0])
        self.assertTrue(big[0] < top[0] and top[3] < big[3], (big, top))


class Links(unittest.TestCase):
    def test_links_run_between_the_facing_sides(self):
        s = solve({"id": "a", "shape": "box"}, {"id": "b", "shape": "box", "right_of": "a", "gap": 1}, links=["a -> b: SQL"])
        link = s["links"][0]
        self.assertEqual(link["key"], "a->b")
        self.assertEqual(link["points"], [[0.5, 0.5, 0.0], [1.5, 0.5, 0.0]])
        self.assertEqual(link["label"], "SQL")

    def test_arrow3d_objects_are_segments_too(self):
        s = solve({"id": "a", "shape": "box"}, {"id": "b", "shape": "box", "right_of": "a", "gap": 1},
                  {"id": "ab", "shape": "arrow3d", "from": "a", "to": "b"}, {"id": "up", "shape": "arrow3d", "from": [0, 1, 0], "to": [0, 3, 0]})
        self.assertEqual(s["objects"]["ab"]["points"], [[0.5, 0.5, 0.0], [1.5, 0.5, 0.0]])
        self.assertEqual(s["objects"]["up"]["points"], [[0.0, 1.0, 0.0], [0.0, 3.0, 0.0]])
        self.assertEqual(s["bounds"][4], 3.0, "an arrow's ends count in the bounds")

    def test_parallel_links_are_numbered(self):
        links = [_spec.link_item(_spec.PURE, l, "links[0]") for l in ("a -> b", "a -> b", {"from": "a", "to": "b", "label": "x"})]
        _spec.number_links(links)
        self.assertEqual([l["id"] for l in links], ["a->b", "a->b#2", "a->b#3"])


class Stored(unittest.TestCase):
    def test_compact_and_expand_round_trip(self):
        s = solve(A, {"id": "b", "shape": "sphere", "above": "a"}, {"id": "ab", "shape": "arrow3d", "from": "a", "to": "b"}, links=["a -> b"])
        back = _solver.expand(_solver.compact(s))
        self.assertEqual(back["order"], s["order"])
        for ident in s["order"]:
            for key in ("pos", "rot", "ext", "rel", "parent"):
                self.assertEqual(back["objects"][ident][key], s["objects"][ident][key], (ident, key))
        self.assertEqual(back["objects"]["ab"]["points"], s["objects"]["ab"]["points"])
        self.assertEqual(back["links"][0]["points"], s["links"][0]["points"])
        self.assertEqual(_solver.expand("junk")["objects"], {})
        self.assertEqual(_solver.expand({"objects": [["x", "bad"]], "links": [1]})["objects"], {})

    def test_numbers_are_snapped(self):
        s = solve({"id": "a", "shape": "box", "size": [1 / 3, 1, 1]}, {"id": "b", "shape": "box", "right_of": "a", "gap": 1 / 7})
        for value in s["objects"]["b"]["pos"] + s["objects"]["b"]["aabb"] + s["bounds"]:
            self.assertEqual(value, round(value, 4))


class Determinism(unittest.TestCase):
    def tree(self):
        items = [{"id": "root", "shape": "plane", "size": [20, 20]}]
        for i in range(1, 30):
            parent = "root" if i < 6 else "n{}".format((i - 1) // 5)
            rel = ("on", "above", "right_of", "behind", "in_front_of")[i % 5]
            item = {"id": "n{}".format(i), "shape": ("box", "sphere", "cylinder", "cone")[i % 4], rel: parent}
            if rel in ("right_of", "behind", "in_front_of"):
                item["on"] = "root"
            items.append(item)
        return items

    def test_the_same_constraints_in_another_order_solve_the_same(self):
        items = [{"id": "base", "shape": "plane", "size": [9, 9]}, {"id": "a", "shape": "box", "on": "base", "at": [-3, 0]},
                 {"id": "b", "shape": "sphere", "above": "a"}, {"id": "c", "shape": "cylinder", "on": "base", "right_of": "a", "gap": 1},
                 {"id": "d", "shape": "cone", "on": "base", "behind": "c"}, {"id": "e", "shape": "box", "on": "base", "in_front_of": "c"}]
        first = solve(*items)
        rng = random.Random(7)
        for _ in range(5):
            shuffled = list(items)
            rng.shuffle(shuffled)
            again = solve(*shuffled)
            self.assertEqual({k: v["aabb"] for k, v in again["objects"].items()}, {k: v["aabb"] for k, v in first["objects"].items()})
            self.assertEqual(again["bounds"], first["bounds"])

    def test_twice_is_identical(self):
        self.assertEqual(solve(*self.tree()), solve(*self.tree()))

    def test_150_objects_solve_within_the_budget(self):
        items = [{"id": "floor", "shape": "plane", "size": [60, 60]}]
        for i in range(149):
            item = {"id": "o{}".format(i), "shape": ("box", "sphere", "cylinder")[i % 3], "on": "floor"}
            if i % 10:
                item["right_of"] = "o{}".format(i - 1)
            elif i:
                item["behind"] = "o{}".format(i - 10)
            items.append(item)
        objs = [_spec.object_item(_spec.PURE, o, "objects[{}]".format(n)) for n, o in enumerate(items)]
        best = min(_timed(lambda: _solver.solve(objs)) for _ in range(3))
        self.assertLess(best, 0.15, "150 objects in {:.0f} ms".format(best * 1000))


def _timed(fn):
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


if __name__ == "__main__":
    unittest.main()

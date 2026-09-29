"""Mind maps (canvas v2 phase 2, 4.7.2): the tree and topics forms, branch tones, both sides, patches with subtrees, pins,
and the round trip (T-B1)."""
from __future__ import annotations

import unittest

from test_kind_graph import GraphRig

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_kinds as R

LAUNCH = {"op": "mindmap", "id": "launch", "title": "Q4 launch", "at": [0, 0], "intent": "brainstorm the launch",
          "tree": {"Audience": ["Platform devs", "Engineering managers"],
                   "Channels": {"Podcast tour": [], "Blog": ["SEO pillar post", "Guest posts"], "Conference talk": []},
                   "Risks": ["Docs not ready", "Pricing unclear"]},
          "branch_tones": "auto", "side": "both"}


class MindMap(GraphRig):
    def topic(self, text):
        return next(e for e in self.members("launch") if e.get("text") == text and e["type"] == "box")

    def test_the_tree_becomes_topics_and_branch_lines(self):
        self.ok(LAUNCH)
        root = self.root("launch")
        self.assertEqual((root["type"], root["block"]), ("frame", "mindmap"))
        topics = [e for e in self.members("launch") if e["type"] == "box"]
        self.assertEqual(len(topics), 13)
        centre = self.topic("Q4 launch")
        self.assertEqual((centre["part"], centre["style"]["tone"], centre["style"]["variant"]), ("root", "accent", "solid"))
        tones = {self.topic(t)["style"]["tone"] for t in ("Audience", "Channels", "Risks")}
        self.assertEqual(len(tones), 3, "each branch its own tone")
        self.assertEqual(self.topic("Blog")["style"]["variant"], "outline")
        self.assertEqual(self.topic("Guest posts")["style"]["tone"], self.topic("Channels")["style"]["tone"])
        sides = {t: C.bounds(self.topic(t))[0] > C.bounds(centre)[0] for t in ("Audience", "Channels", "Risks")}
        self.assertEqual(sorted(sides.values()), [False, True, True], "both sides, balanced by leaf count")
        lines = [e for e in self.members("launch") if e["type"] == "arrow"]
        self.assertEqual(len(lines), 12)
        self.assertTrue(all(e["head"] == "none" and e["style"]["route"] == "curved" for e in lines))
        self.assertEqual([p for p in __import__("herdr_team.canvas_check", fromlist=["x"]).problems(self.scene()["elements"])
                          if p["code"] in ("overlap", "arrow_through")], [])

    def test_patch_adds_under_a_text_and_removes_a_subtree(self):
        self.ok(LAUNCH)
        blog = self.topic("Blog")["part"]
        self.ok({"op": "patch", "id": "launch", "add": {"topics": [{"under": "Blog", "text": "Newsletter"}]}, "intent": "t"})
        self.assertEqual(next(e for e in self.members("launch") if e.get("part") == "e:{}->{}".format(blog, self.topic("Newsletter")["part"]))["type"],
                         "arrow")
        self.ok({"op": "patch", "id": "launch", "remove": {"topics": [blog]}, "intent": "t"})
        texts = {e["text"] for e in self.members("launch") if e["type"] == "box"}
        self.assertFalse(texts & {"Blog", "SEO pillar post", "Guest posts", "Newsletter"})
        self.assertIn("Podcast tour", texts)
        refused = self.refused({"op": "patch", "id": "launch", "add": {"topics": [{"under": "Nowhere", "text": "x"}]}, "intent": "t"})
        self.assertEqual(refused["details"]["field"], "topics[9].under")

    def test_a_pinned_topic_holds(self):
        self.ok(LAUNCH)
        risks = self.topic("Risks")
        self.ok({"op": "move", "id": risks["id"], "by": [0, 300], "intent": "drag"}, test_canvas_operator())
        held = C.bounds(self.topic("Risks"))
        self.ok({"op": "patch", "id": "launch", "add": {"topics": [{"text": "Budget"}]}, "intent": "t"})
        self.assertEqual(C.bounds(self.topic("Risks")), held)

    def test_a_topics_entry_inside_the_tree_is_refused_not_drawn_as_its_field_names(self):
        """QA phase 6 F2: ``tree`` walked a ``{id, text, tone}`` topic as further nested branches, so its **field
        names** became topics and the real content hung under them - 110 marks that ``check`` called clean, and the
        one place in the language where the wrong shape was accepted instead of named."""
        bad = dict(LAUNCH, tree={"Audience": [{"id": "devs", "text": "Platform developers", "tone": "info"}],
                                 "Risks": [{"id": "docs", "text": "Docs not ready", "tone": "danger"}]})
        refused = self.refused(bad)
        self.assertEqual(refused["details"]["field"], "tree.Audience[0]")
        self.assertIn("is a topic (id, text, tone), not a branch", refused["message"])
        self.assertIn("topics: [{id, text, under, tone, icon}]", refused["message"])
        # The run that found this passed `children` as a sixth key, which is not a topic field: the two that are
        # still give the topic away, wherever in the tree it sits.
        with_children = dict(LAUNCH, tree={"Audience": [{"id": "devs", "text": "Platform developers",
                                                         "children": ["Backend", "Frontend"]}]})
        self.assertEqual(self.refused(with_children)["details"]["field"], "tree.Audience[0]")
        self.assertEqual(self.refused(dict(LAUNCH, tree={"id": "devs", "text": "Platform developers"}))["details"]["field"], "tree")
        # `topics` spelled into the tree is refused by the entry it holds, which is the form the message names.
        self.assertIn("is a topic", self.refused(dict(LAUNCH, tree={"topics": [{"id": "devs", "text": "Devs"}]}))["message"])
        # Nested branch objects still work.
        self.ok(dict(LAUNCH, tree={"Channels": [{"Blog": ["SEO"]}]}))

    def test_a_branch_named_like_a_topic_field_still_draws(self):
        """QA phase 6, 3.3: the F2 guard refused any dict that named a topic field, so a mind map of a data model
        (``{"User": {"id": [...], "text": [...]}}``) could not use the tree form at all - and the advice it got, to
        pass it as ``topics``, did not apply. A topic entry is what holds its fields' own values; a branch called
        ``id`` or ``text`` holds children, and draws."""
        model = dict(LAUNCH, tree={"User": {"id": ["primary key"], "text": ["display name"]},
                                   "Order": {"icon": ["cart"], "tone": {"paid": ["invoice"]}}})
        self.ok(model)
        texts = {e["text"] for e in self.members("launch") if e["type"] == "box"}
        for branch in ("User", "id", "primary key", "text", "display name", "Order", "icon", "cart", "tone", "paid"):
            self.assertIn(branch, texts, branch)
        # One topic field alone, and a mix of a value and children, are branches too.
        self.ok(dict(LAUNCH, id="one", at=[0, 3000], tree={"text": ["one"]}))
        self.ok(dict(LAUNCH, id="mixed", at=[0, 6000], tree={"User": {"id": "primary key", "text": ["display name"]}}))

    def test_round_trip(self):
        kctx = C._KindCtx(None)
        kind = R.get("mindmap")
        self.ok(LAUNCH)
        self.assertEqual(B.comparable(B.normalized(kctx, kind, self.spec("launch"))), B.comparable(B.normalized(kctx, kind, LAUNCH)))
        self.assertIn("tree", self.spec("launch"))
        topics = {"op": "mindmap", "id": "hire", "title": "Hiring", "at": [0, 3000], "side": "right", "intent": "t",
                  "topics": [{"id": "eng", "text": "Engineering"}, {"id": "be", "text": "Backend", "under": "eng"},
                             {"id": "gtm", "text": "Go to market", "tone": "accent"}]}
        self.ok(topics)
        self.assertEqual(B.comparable(B.normalized(kctx, kind, self.spec("hire"))), B.comparable(B.normalized(kctx, kind, topics)))


def test_canvas_operator():
    import test_canvas

    return test_canvas.OPERATOR


if __name__ == "__main__":
    unittest.main()

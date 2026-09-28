"""The ``callout`` kind (canvas v2 phase 2, 4.2): its kind picks tone and icon; it hugs up to 400."""
from __future__ import annotations

from test_canvas import CanvasRig

from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team.canvas_kinds import callout as K


class Callout(CanvasRig):
    def test_each_kind_has_its_tone_and_icon(self):
        for index, kind in enumerate(K.KINDS_TONE):
            eid = self.ok({"op": "callout", "kind": kind, "title": kind, "at": [0, index * 200], "intent": "t"})["ids"][0]
            entry = D.entry(self.el(eid), D.environment(self.scene()))
            self.assertEqual(entry["items"][0]["fill"], "tone.{}.fill".format(K.KINDS_TONE[kind]))
            self.assertEqual(entry["items"][2]["k"], "group", "{}: its icon".format(kind))

    def test_hug_up_to_400_and_readback(self):
        eid = self.ok({"op": "callout", "kind": "decision", "title": "Open question", "body": "Keep sessions or go stateless? " * 6,
                       "at": [0, 0], "intent": "t"})["ids"][0]
        el = self.el(eid)
        self.assertEqual(el["w"], 400)
        self.assertEqual(R.get("callout").readback(dict(el, body="Keep sessions or go stateless?"), False),
                         '{} callout decision "Open question": Keep sessions or go stateless?'.format(eid))
        self.assertEqual(self.refused({"op": "callout", "kind": "rant", "title": "x", "at": [0, 0], "intent": "t"})["details"]["field"], "kind")

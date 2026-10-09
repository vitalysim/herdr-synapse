"""Regression tests for the findings the folder-invariant oracle and round 4 named, one shape each.

``tests/test_folder_invariant.py`` is the specification: a model-based property test that holds every
step of a random folder history to NO LOSS, NO FALSE REPORT and NO AUTOMATIC COPY. These tests pin the
shortest reproduction of each finding it (or round 4) reached, so a regression names the finding rather
than a seed; the finding ids are the ones in ``.local/qa/inheritance/round4-findings.md`` and the oracle
stage's report (N1-N4). Since the never-overwrite decision (2026-10-08) each shape asserts the new
contract: a file this team did not write is held, untouched and uncopied, until an operator act.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest import mock

from herdr_team import charter, roster, store, workdir
from herdr_team import document_sync as DS
from herdr_team import facts as F
from herdr_team.identity import Author
from support import TempState
from test_cmd_board import pane_api
from test_cmd_roster import json_out, run_cli


def human() -> Author:
    return Author(name="human", kind="human", via="console", verified=True)


class Rig(object):
    """One team on one throwaway project folder."""

    def __init__(self, case: unittest.TestCase, mode: Optional[str] = "manual") -> None:
        self.case = case
        self.project = Path(tempfile.mkdtemp(prefix="ht-fix-")).resolve()
        case.addCleanup(self._cleanup)
        self.state = TempState()
        case.addCleanup(self.state.cleanup)
        self.layout, self.name = self.state.layout, self.state.team_name
        self.paths = self.layout.team(self.name)

        def configure(doc: roster.Team) -> None:
            doc.config["project_dir"] = os.fspath(self.project)
            if mode is None:
                doc.config.pop("document_sync", None)
            else:
                doc.config["document_sync"] = mode

        roster.update_team(self.paths, configure)
        self.files = workdir.paths_for(os.fspath(self.project), self.name)

    def _cleanup(self) -> None:
        for root, dirs, _files in os.walk(self.project):
            for name in dirs:
                try:
                    os.chmod(os.path.join(root, name), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.project, ignore_errors=True)

    def render(self, force: bool = False) -> Dict[str, Any]:
        return workdir.render(self.layout, self.name, force=force)

    def cli(self, *argv: str) -> Tuple[int, Any, Any]:
        return json_out(run_cli(["--json"] + list(argv) + ["--team", self.name], self.state.env, pane_api()))

    def inherited(self) -> Path:
        return self.files["inherited"]

    def everything_under_inherited(self) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        for root, _dirs, names in os.walk(self.inherited()):
            for name in names:
                path = Path(root) / name
                if not name.startswith(".tmp-"):
                    out[os.fspath(path)] = path.read_bytes()
        return out

    def member_document(self, name: str, token: str, outside: bool = True) -> bytes:
        path = self.files["members"] / (name + ".md")
        lines = [workdir.marker_for(path), "# " + name, ""]
        if outside:
            lines += ["A note above the first heading, only here: " + token, ""]
        lines += ["## Mission", "", "mission " + token, ""]
        return "\n".join(lines).encode("utf-8")


class AssetFindings(unittest.TestCase):

    def test_comp_2_create_never_replaces_a_file_that_is_there(self):
        """COMP-2: the guard's own byte writer proves absence when it writes, not its caller beforehand."""
        rig = Rig(self)
        rig.files["canvas_assets"].mkdir(parents=True)
        target = rig.files["canvas_assets"] / ("ab" * 16 + ".png")
        target.write_bytes(b"a previous team's picture")
        guard = DS.FolderGuard(rig.layout, rig.name, rig.files)
        self.assertFalse(guard.create(target, b"this team's bytes"))
        self.assertEqual(target.read_bytes(), b"a previous team's picture")
        fresh = rig.files["canvas_assets"] / ("cd" * 16 + ".png")
        self.assertTrue(guard.create(fresh, b"this team's bytes"))
        self.assertEqual(fresh.read_bytes(), b"this team's bytes")

    def test_comp_2_a_file_landing_after_the_absence_check_is_refused_by_the_exclusive_publish(self):
        """The window inside ``create`` itself: after its absence check, before the bytes are published.

        This lands the colleague's file while the temporary sibling is being written, so only the link's ``EEXIST``
        can save it.
        """
        rig = Rig(self)
        rig.files["canvas_assets"].mkdir(parents=True)
        target = rig.files["canvas_assets"] / ("ef" * 16 + ".png")
        real = store.atomic_write

        def racing(path: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
            result = real(path, data, *args, **kwargs)
            if Path(path).name.startswith(".tmp-new-"):
                target.write_bytes(b"a colleague's file")
            return result

        guard = DS.FolderGuard(rig.layout, rig.name, rig.files)
        with mock.patch.object(store, "atomic_write", racing):
            self.assertFalse(guard.create(target, b"this team's bytes"))
        self.assertEqual(target.read_bytes(), b"a colleague's file")
        self.assertEqual(sorted(p.name for p in rig.files["canvas_assets"].iterdir()), [target.name],
                         "the temporary sibling is gone")

    def test_comp_2_a_file_landing_in_the_asset_window_is_reported_and_kept(self):
        """The real caller: a colleague's file lands between the mirror's look and ``write_bytes``' absence check."""
        rig = Rig(self)
        rig.files["canvas_assets"].mkdir(parents=True)
        source = Path(tempfile.mkdtemp(prefix="ht-src-"))
        self.addCleanup(shutil.rmtree, source, True)
        data = b"this team's picture"
        name = hashlib.sha256(data).hexdigest()[:32] + ".png"
        (source / name).write_bytes(data)
        real = store.read_bytes

        def racing(path: Any, *args: Any, **kwargs: Any) -> Any:
            found = real(path, *args, **kwargs)
            if Path(path).parent == source:
                (rig.files["canvas_assets"] / name).write_bytes(b"a colleague's file")
            return found

        guard = DS.FolderGuard(rig.layout, rig.name, rig.files)
        with mock.patch.object(store, "read_bytes", racing):
            out = workdir.mirror_canvas_assets(source, rig.files["canvas_assets"], [name], guard)
        self.assertEqual(out["copied"], [])
        self.assertEqual(out["conflict"], [{"name": name}])
        self.assertEqual((rig.files["canvas_assets"] / name).read_bytes(), b"a colleague's file")

    def test_data_4_the_prune_judges_authorship_on_one_look_and_keeps_what_is_not_ours(self):
        """DATA-4: authorship and the action come from one look; a picture this team did not write is never deleted."""
        rig = Rig(self)
        rig.files["canvas_assets"].mkdir(parents=True)
        data = b"a previous team's picture"
        entry = rig.files["canvas_assets"] / (hashlib.sha256(data).hexdigest()[:32] + ".png")
        entry.write_bytes(data)
        guard = DS.FolderGuard(rig.layout, rig.name, rig.files)
        looks = []
        real = DS.look

        def counting(path: Any) -> Any:
            looks.append(Path(path))
            return real(path)

        with mock.patch.object(DS, "look", counting):
            out = workdir.prune_canvas_assets(guard, rig.files["canvas_assets"], [])
        self.assertEqual(looks, [entry], "one look per file")
        self.assertEqual(out["held"], [entry.name])
        self.assertEqual(entry.read_bytes(), data)
        self.assertFalse(rig.inherited().exists(), "and nothing copied")


class EvidenceStoreFindings(unittest.TestCase):

    def test_comp_3_a_file_where_inherited_belongs_is_left_alone_and_only_stops_force(self):
        """COMP-3: ``touch <team>/inherited`` used to freeze the mirror. Now the mirror never needs ``inherited/``:
        an ordinary render is unaffected, and ``--force`` refuses with the reason rather than writing without a copy."""
        rig = Rig(self)
        rig.render()
        charter.set_rules(rig.layout, rig.name, human(), "this team's rules")
        rig.inherited().write_bytes(b"what the agent touched")
        self.assertIn(os.fspath(rig.files["knowledge"]), rig.render()["written"], "this team's own writes go on")
        previous = b"\n".join([workdir.MARKER.encode(), b"# alpha facts", b"", b"- **F-1** the only copy", b""])
        rig.files["facts"].write_bytes(previous)
        result = rig.render(force=True)
        self.assertEqual(rig.files["facts"].read_bytes(), previous)
        self.assertEqual(rig.inherited().read_bytes(), b"what the agent touched")
        held = [r for r in result["holds"] if r["path"] == os.fspath(rig.files["facts"])]
        self.assertEqual([r["reason"] for r in held], ["copy_failed"])
        self.assertIn("not a directory", held[0]["why"])


class HoldFindings(unittest.TestCase):

    def held_member_then_replaced(self) -> Tuple[Rig, Path, bytes]:
        rig = Rig(self, mode="auto")
        path = rig.files["members"] / "alpha-worker.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(rig.member_document("alpha-worker", "VERSION-ONE"))
        rig.render()
        second = rig.member_document("alpha-worker", "VERSION-TWO")
        path.write_bytes(second)
        rig.render()
        return rig, path, second

    def test_data_2_force_over_a_replaced_held_member_keeps_the_replacement(self):
        """DATA-2: the ``adopt`` hold is looked at again, so ``--force`` writes over a copied document."""
        rig, path, second = self.held_member_then_replaced()
        result = rig.render(force=True)
        self.assertNotEqual(path.read_bytes(), second)
        self.assertIn(second, rig.everything_under_inherited().values())
        record = [r for r in result.get("inherited") or [] if r["path"] == os.fspath(path)]
        if record and record[0].get("regenerated"):
            self.assertEqual(Path(record[0]["snapshot"]).read_bytes(), second)

    def test_trus_1_adopt_names_a_copy_of_the_bytes_it_drops(self):
        """TRUS-1: ``--adopt`` copies the one reading it adopts from, and names that copy."""
        rig, path, second = self.held_member_then_replaced()
        code, payload, err = rig.cli("instructions", "alpha-worker", "--adopt", "--yes")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["adopted"], payload)
        self.assertEqual(Path(payload["snapshot"]).read_bytes(), second)
        rig.render()
        self.assertIn(second, rig.everything_under_inherited().values())

    def test_n2_an_adoption_whose_copy_cannot_be_made_leaves_the_document_in_place(self):
        """N2: the lines outside the ``##`` sections are never dropped without a checked copy: the file stays held."""
        rig, path, second = self.held_member_then_replaced()
        rig.inherited().write_bytes(b"")
        code, payload, err = rig.cli("instructions", "alpha-worker", "--adopt", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), second)
        self.assertEqual(payload["held"]["reason"], "copy_failed", payload)
        self.assertNotIn("snapshot", payload)

    def test_n4_adopt_on_an_undecodable_document_is_a_refusal_not_a_traceback(self):
        """N4: one invalid byte used to raise ``UnicodeDecodeError`` out of ``cli.main``."""
        rig, path, _second = self.held_member_then_replaced()
        path.write_bytes(rig.member_document("alpha-worker", "x") + b"\xff\xfe not utf-8\n")
        code, _payload, err = rig.cli("instructions", "alpha-worker", "--adopt", "--yes")
        self.assertNotEqual(code, 0)
        self.assertEqual(err.get("code"), "workdir_unreadable", err)

    def test_n4_a_held_line_does_not_print_adopt_for_a_document_adopt_cannot_read(self):
        """The printed repair is chosen from the file as it stands, so it runs when it is printed."""
        rig, path, _second = self.held_member_then_replaced()
        entry = DS.load(rig.paths)[os.fspath(path)]
        self.assertTrue(DS.record_of(path, entry)["commands"][0].endswith("--adopt"))
        path.write_bytes(b"<<<<<<< HEAD\nmerge conflict\n=======\n>>>>>>> branch\n")
        self.assertEqual(DS.record_of(path, entry)["commands"], ["herdr-synapse project render --force"])

    def test_n1_the_automatic_adoption_copies_the_lines_it_would_drop(self):
        """N1: auto mode's adoption of a replaced member document used to keep only the ``##`` sections. Since D-AUTO
        the adoption is the operator's ``project confirm``; what it drops is still copied before the file is replaced."""
        rig = Rig(self, mode="auto")
        charter.set_instructions(rig.layout, rig.name, human(), "alpha-worker", "## Mission\n\nthe first mission\n", None)
        rig.render()
        path = rig.files["members"] / "alpha-worker.md"
        edited = path.read_text(encoding="utf-8").replace("the first mission", "the operator's new mission")
        edited = edited.replace("## Mission", "A note the operator wrote above the heading.\n\n## Mission", 1)
        path.write_text(edited, encoding="utf-8")
        DS.scan(rig.layout, rig.name, now=100.0)
        result = DS.scan(rig.layout, rig.name, now=103.0)
        self.assertEqual((result["imported"], result["proposed"]), ([], [os.fspath(path)]), result)
        self.assertNotIn("new mission", charter.get_instructions(rig.layout, rig.name, "alpha-worker"))
        DS.confirm(rig.layout, rig.name, human(), DS.pending_changes(rig.paths)[0]["id"])
        self.assertIn("the operator's new mission", charter.get_instructions(rig.layout, rig.name, "alpha-worker"))
        rig.render()
        self.assertNotIn("A note the operator wrote", path.read_text(encoding="utf-8"))
        self.assertIn(edited.encode("utf-8"), rig.everything_under_inherited().values())


class ReportFindings(unittest.TestCase):

    def test_n3_a_payload_copy_that_is_not_a_scene_is_not_offered_to_canvas_import(self):
        """N3: ``canvas import --from <copy> --dry-run`` exits 1 on a merge-conflicted payload copy."""
        rig = Rig(self)
        rig.inherited().mkdir(parents=True)
        for data, dry in ((b"<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> b\n", False),
                          (json.dumps({"payload": 1, "team": "beta", "scene_version": 3, "elements": []}).encode(), True)):
            snapshot = rig.inherited() / "canvas-{}.json".format(workdir.digest_bytes(data))
            snapshot.write_bytes(data)
            entry = {"copy": {"digest": workdir.digest_bytes(data), "snapshot": os.fspath(snapshot),
                              "kind": "canvas-payload"}}
            commands = DS.record_of(rig.files["canvas_json"], entry)["commands"]
            self.assertEqual(any("--dry-run" in c for c in commands), dry, commands)

    def test_comp_1_a_copy_deleted_since_is_never_named(self):
        """COMP-1's second half: a re-emitted record whose copy is gone says so instead of naming it."""
        rig = Rig(self)
        entry = {"copy": {"digest": "0" * 16, "snapshot": os.fspath(rig.inherited() / "facts-0000000000000000.md"),
                          "kind": "facts"}}
        shown = DS.record_of(rig.files["facts"], entry)
        self.assertIsNone(shown["snapshot"])
        self.assertTrue(shown["copy_gone"])
        for style in ("board", "list"):
            line = DS.describe_inherited(shown, style=style)
            self.assertIn("no longer holds them", line)
            self.assertNotIn("read back and checked", line)

    def test_comp_4_the_asset_line_prints_no_placeholder(self):
        """COMP-4 / DATA-6: the asset line printed ``canvas import --from <that folder>/canvas.json``."""
        record = {"path": "/p/canvas-assets/a.png", "kind": "canvas-asset", "held": True, "reason": "unnamed",
                  "commands": []}
        for style in ("board", "list"):
            self.assertNotIn("<that folder>", DS.describe_inherited(record, style=style))


class TrustFindings(unittest.TestCase):

    def test_trus_3_a_late_knowledge_md_in_auto_mode_is_held_and_never_adopted(self):
        """TRUS-3: a stranger's ``knowledge.md`` pulled over this team's own is held; nothing in it is adopted, and the
        notifier names the true state instead of accusing the operator of an edit."""
        rig = Rig(self, mode="auto")
        charter.set_rules(rig.layout, rig.name, human(), "our rules")
        rig.render()
        stranger = "\n".join([workdir.MARKER, "# alpha knowledge", "", DS.RULES_HEADING, "", "their rules", "",
                              DS.FINDINGS_HEADING, "", "- **beta-worker** (2026-01-01): their finding", ""])
        rig.files["knowledge"].write_text(stranger, encoding="utf-8")
        result = rig.render()
        DS.scan(rig.layout, rig.name, now=100.0)
        errors = DS.scan(rig.layout, rig.name, now=103.0)["errors"]
        self.assertEqual(rig.files["knowledge"].read_text(encoding="utf-8"), stranger)
        self.assertIn(os.fspath(rig.files["knowledge"]), result.get("sync_pending") or [])
        self.assertEqual(charter.get_rules(rig.layout, rig.name), "our rules")
        self.assertFalse(rig.inherited().exists())
        self.assertTrue(errors)
        for error in errors:
            self.assertNotIn("restore that section", error["error"])
            self.assertIn("project render --force", error["error"])

    def test_trus_2_a_markerless_record_is_held_on_an_ordinary_render_and_copied_by_nothing(self):
        """TRUS-2: a markerless record is the plainest case of a file this team did not write."""
        for mode in ("manual", "auto"):
            with self.subTest(mode=mode):
                rig = Rig(self, mode=mode)
                rig.files["root"].mkdir(parents=True)
                rig.files["knowledge"].write_text("# hand written rules, no marker\n", encoding="utf-8")
                result = rig.render()
                self.assertEqual(rig.files["knowledge"].read_text(encoding="utf-8"), "# hand written rules, no marker\n")
                self.assertEqual(rig.everything_under_inherited(), {})
                held = [r for r in result["holds"] if r["path"] == os.fspath(rig.files["knowledge"])]
                self.assertEqual([r["reason"] for r in held], ["found"])


class RecordSizeFindings(unittest.TestCase):

    def facts(self, paths: Any, count: int) -> None:
        lines = []
        for n in range(count):
            lines.append(json.dumps({"op": "add", "id": "F-{}".format(n + 1), "at": "2026-09-01T10:00:00Z",
                                     "by": "alpha-worker", "by_kind": "claude",
                                     "statement": "measured value {} for a subsystem".format(n),
                                     "about": "subsystem", "attribute": "metric-{}".format(n),
                                     "sources": [{"kind": "url", "url": "https://example.com/{}".format(n)}],
                                     "v": F.FACTS_VERSION}))
        F.facts_jsonl(paths).parent.mkdir(parents=True, exist_ok=True)
        F.facts_jsonl(paths).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_rele_5_facts_md_is_split_and_imported_whole(self):
        """RELE-5: ``facts.md`` truncated at about 1,100 facts and the import reported the short count."""
        rig = Rig(self)
        self.facts(rig.paths, 2500)
        rig.render()
        parts = sorted(p.name for p in rig.files["root"].glob("facts*.md"))
        self.assertGreater(len(parts), 1, parts)
        for name in parts:
            self.assertLessEqual((rig.files["root"] / name).stat().st_size, workdir.MAX_FACTS_BYTES, name)
        self.assertEqual(workdir.facts_parts_declared(rig.files["facts"].read_text(encoding="utf-8")),
                         (len(parts), 2500))
        other = Rig(self)
        code, payload, err = other.cli("knowledge", "import", "--from", os.fspath(rig.files["root"]), "--yes",
                                       "--no-canvas", "--no-rules")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["facts"]["of"], 2500, payload["facts"])
        self.assertNotIn("missing", payload["facts"])

    def test_rele_5_a_missing_part_is_named_with_the_true_count(self):
        rig = Rig(self)
        self.facts(rig.paths, 2500)
        rig.render()
        (rig.files["root"] / "facts-2.md").unlink()
        other = Rig(self)
        code, payload, err = other.cli("knowledge", "import", "--from", os.fspath(rig.files["root"]), "--yes",
                                       "--no-canvas", "--no-rules")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["facts"]["declared"], 2500)
        self.assertIn("facts-2.md", payload["facts"]["missing"])
        self.assertLess(payload["facts"]["of"], 2500)

    def test_rele_1_the_fact_log_is_read_once_per_render_and_not_at_all_when_unchanged(self):
        """RELE-1: ``findings_view`` and ``mirror_rows`` each replayed the whole log, twice per render."""
        rig = Rig(self)
        self.facts(rig.paths, 50)
        calls = []
        real = F.load

        def counting(team: Any) -> Any:
            calls.append(team)
            return real(team)

        with mock.patch.object(F, "load", counting):
            rig.render()
            self.assertEqual(len(calls), 1, "one replay for both views")
            rig.render()
            self.assertEqual(len(calls), 1, "an unchanged log is not replayed again")
            F.add(rig.paths, {"statement": "a new fact", "about": "x", "attribute": "y", "by": "alpha-worker",
                              "by_kind": "claude"}, mode="add")
            calls.clear()
            rig.render()
            self.assertIn("a new fact", rig.files["facts"].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class DocumentationFindings(unittest.TestCase):
    """COMP-5: the documents a reader of the folder sees cannot fall behind the classification table."""

    ROOT = Path(__file__).resolve().parent.parent
    NAMES = {"knowledge": "knowledge.md", "facts": "facts.md", "members": "members/", "canvas_json": "canvas.json",
             "canvas_assets": "canvas-assets/", "inherited": "inherited/", "artifacts": "artifacts/",
             "exports": "exports/", "canvas_md": "canvas.md", "board": "board.md", "readme": "README.md",
             "gitignore": ".gitignore"}

    def test_docs_inheritance_names_every_record_and_its_class(self):
        text = (self.ROOT / "docs" / "inheritance.md").read_text(encoding="utf-8")
        contract = text.split("## What `knowledge import` adopts", 1)[0]
        for key, record in workdir.MIRRORED_RECORDS.items():
            if key in ("shared", "root"):
                continue
            self.assertIn(self.NAMES[key], contract, key)
        for name in ("canvas.md", "board.md", "README.md", ".gitignore"):
            self.assertFalse(workdir.MIRRORED_RECORDS[[k for k, v in self.NAMES.items() if v == name][0]].durable)
        for stale in ("in git | yes | read it directly", "`inherited/` is committed", "already there at first contact",
                      "found in place at first contact", "Two things are outside that"):
            self.assertNotIn(stale, text, stale)

    def test_every_surface_leads_with_the_same_promise(self):
        lead = "left exactly as it is"
        for path in (self.ROOT / "docs" / "inheritance.md", self.ROOT / "README.md",
                     self.ROOT / "docs" / "capabilities.md", self.ROOT / "skill-guides" / "references" / "inheritance.md"):
            self.assertIn(lead, " ".join(path.read_text(encoding="utf-8").split()), path.name)
        self.assertIn(lead, " ".join(workdir.README_BODY.split()))


class SecondFixStageFindings(unittest.TestCase):
    """The second fix stage: the findings the extended oracle and the verifiers named that its scripts do not pin."""

    def test_an_edit_of_this_teams_own_document_is_not_reported_as_a_strangers(self):
        """An upgraded manual-mode operator's hand edit of knowledge.md was announced as "not written by this team"."""
        rig = Rig(self)
        charter.set_rules(rig.layout, rig.name, human(), "Every change needs a review.")
        rig.render()
        path = rig.files["knowledge"]
        path.write_text(path.read_text(encoding="utf-8").replace("Every change needs a review.",
                                                                 "Every change needs two reviews."), encoding="utf-8")
        records = rig.render().get("inherited") or []
        self.assertEqual([r["reason"] for r in records], ["changed"], records)
        board = DS.describe_inherited(records[0])
        self.assertIn("it changed after this team last wrote it", board)
        self.assertNotIn("no record of writing", board)
        self.assertIn("two reviews", path.read_text(encoding="utf-8"))

    def test_a_symlinked_canvas_assets_directory_is_refused_and_said(self):
        """X1's unit: the prune lists nothing through a symlinked ``canvas-assets/`` and says why."""
        rig = Rig(self)
        outside = Path(tempfile.mkdtemp(prefix="ht-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "main.py").write_text("print('the repository')\n", encoding="utf-8")
        rig.files["root"].mkdir(parents=True, exist_ok=True)
        os.symlink(os.fspath(outside), os.fspath(rig.files["canvas_assets"]))
        guard = DS.FolderGuard(rig.layout, rig.name, rig.files)
        out = workdir.prune_canvas_assets(guard, rig.files["canvas_assets"], [])
        self.assertEqual(out["pruned"], [])
        self.assertIn("symlink", out.get("refused", ""))
        self.assertTrue((outside / "main.py").is_file())
        self.assertFalse(guard.create(rig.files["canvas_assets"] / ("ab" * 16 + ".png"), b"picture"))
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["main.py"])

    def test_a_fifo_at_a_record_never_stalls_the_render(self):
        """``mkfifo knowledge.md`` blocked ``drifted``'s read for good, and with it the notifier's tick."""
        import signal

        rig = Rig(self, mode="auto")
        rig.files["root"].mkdir(parents=True, exist_ok=True)
        os.mkfifo(os.fspath(rig.files["knowledge"]))

        def stalled(*_args: Any) -> None:
            raise AssertionError("render blocked on a FIFO")

        previous = signal.signal(signal.SIGALRM, stalled)
        signal.alarm(20)
        try:
            rig.render()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assertTrue(stat.S_ISFIFO(os.lstat(os.fspath(rig.files["knowledge"])).st_mode), "left exactly as it is")

    def test_an_identical_copy_is_one_file_however_many_seconds_apart(self):
        """X10's unit: a copy is named by its content digest, so the same bytes copied again are the same file."""
        rig = Rig(self)
        path = rig.files["knowledge"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# not ours\n", encoding="utf-8")
        guard = DS.FolderGuard(rig.layout, rig.name, rig.files, force=True)
        first, _ = guard.copy_verified(path, b"# not ours\n")
        second, _ = guard.copy_verified(path, b"# not ours\n")
        self.assertEqual(first, second)
        self.assertEqual(len(list(rig.files["inherited"].glob("knowledge-*.md"))), 1)

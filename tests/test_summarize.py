"""Tests for the stepwise ``summarize`` flow and the download routing.

Step 0 (``source``) drives ``pipeline`` and therefore whisper, so these tests
SEED the run state directly - a ``run.json`` plus a ``transcript.txt`` - and then
exercise the steps that decide routing, naming, archiving and cleanup. That is
where the behaviour this redesign adds actually lives.
"""

from __future__ import annotations

import json
import os

import pytest

from zoombie.commands import summarize as sz
from zoombie.lib import workspace
from zoombie.lib.errors import ZoombieError


def _seed_run(tmp_path, monkeypatch, *, source: str, internal: bool,
              proposed: str = "Proposed Name", transcript: str = "intro words\n\nbody words\n"):
    """Create a run scratch dir the name/prose/verify steps can consume."""
    monkeypatch.chdir(tmp_path)
    run = tmp_path / ".tmp" / "zoombie-summarize" / "abc123"
    run.mkdir(parents=True)
    if internal:
        destination = os.path.dirname(os.path.abspath(source))
    else:
        destination = os.path.join(
            workspace.unsorted_dir(workspace.KIND_SUMMARIES), proposed
        )
    base = run / "transcript"
    (run / "transcript.txt").write_text(transcript, encoding="utf-8")
    (run / "run.json").write_text(json.dumps({
        "source": source, "kind": "video", "internal": internal,
        "destination": destination, "proposed": proposed,
        "media": None, "base": str(base).replace("\\", "/"),
        "destinationFinal": None,
    }), encoding="utf-8")
    return str(run), destination


def _args(**overrides):
    import argparse

    base = dict(step=None, source=None, run=None, name=None, slides=None,
                times=None, title=None, summary_text=None, criticism=None,
                sections=None, no_media=False, language="auto",
                work_root=None, dry_run=False, force=False)
    base.update(overrides)
    return argparse.Namespace(**base)


class TestRouting:
    def test_an_external_source_lands_under_unsorted(self, tmp_path, monkeypatch):
        source = str(tmp_path / "elsewhere" / "video.mp4")
        run, destination = _seed_run(tmp_path, monkeypatch, source=source, internal=False)
        outcome = sz.run(_args(step="name", run=run, name="My Item"))
        item = outcome.data["itemDir"]
        assert os.path.normcase(item).startswith(
            os.path.normcase(str(tmp_path / "_unsorted" / "summaries"))
        )
        assert os.path.basename(item) == "My Item"

    def test_an_internal_source_keeps_its_own_folder(self, tmp_path, monkeypatch):
        folder = tmp_path / "lore" / "scandinavian"
        folder.mkdir(parents=True)
        source = str(folder / "berserk.mp4")
        (folder / "berserk.mp4").write_bytes(b"\x00")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        outcome = sz.run(_args(step="name", run=run))
        assert outcome.data["itemDir"] == str(folder)

    def test_a_name_is_made_unique(self, tmp_path, monkeypatch):
        (tmp_path / "_unsorted" / "summaries" / "My Item").mkdir(parents=True)
        source = str(tmp_path / "elsewhere" / "video.mp4")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=False)
        outcome = sz.run(_args(step="name", run=run, name="My Item"))
        assert os.path.basename(outcome.data["itemDir"]) == "My Item (2)"


class TestSteps:
    def test_prose_before_name_is_refused(self, tmp_path, monkeypatch):
        source = str(tmp_path / "elsewhere" / "video.mp4")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=False)
        with pytest.raises(ZoombieError, match="name step"):
            sz.run(_args(step="prose", run=run, title="T"))

    def test_an_unknown_run_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ZoombieError, match="no run"):
            sz.run(_args(step="name", run=str(tmp_path / "nope")))

    def test_prose_writes_the_document_and_runs_postprocess(self, tmp_path, monkeypatch):
        folder = tmp_path / "item"
        folder.mkdir()
        source = str(folder / "berserk.mp4")
        (folder / "berserk.mp4").write_bytes(b"\x00")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        sz.run(_args(step="name", run=run))
        outcome = sz.run(_args(
            step="prose", run=run, title="Berserk",
            summary_text="A short summary.",
            sections=json.dumps([{"heading": "Вступление", "at": "intro"}]),
        ))
        summary = folder / "summary.md"
        assert summary.is_file()
        text = summary.read_text(encoding="utf-8")
        assert text.startswith("# Berserk")
        assert "## 4." in text and "## 6." in text
        assert outcome.data["archived"] is None
        # postprocess ran in the same call: the block-6 heading is anchored.
        assert '<a id="s-1">' in text

    def test_a_missing_title_is_refused(self, tmp_path, monkeypatch):
        folder = tmp_path / "item"
        folder.mkdir()
        source = str(folder / "b.mp4")
        (folder / "b.mp4").write_bytes(b"\x00")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        sz.run(_args(step="name", run=run))
        with pytest.raises(ZoombieError, match="Title"):
            sz.run(_args(step="prose", run=run))


class TestArchive:
    def test_an_existing_summary_is_archived_at_the_start(self, tmp_path, monkeypatch):
        """The archive is the USER's file, taken at the NAME step (task start)."""
        folder = tmp_path / "item"
        folder.mkdir()
        source = str(folder / "b.mp4")
        (folder / "b.mp4").write_bytes(b"\x00")
        (folder / "summary.md").write_text("# Old\n", encoding="utf-8")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        named = sz.run(_args(step="name", run=run))
        archived = named.data["archived"]
        assert archived is not None
        assert os.path.basename(archived["to"]).startswith("summary_")
        # The snapshot holds the file as the USER left it, moved away already.
        assert (folder / "summary.md").exists() is False
        assert open(archived["to"], encoding="utf-8").read().startswith("# Old")

        outcome = sz.run(_args(step="prose", run=run, title="New", summary_text="s"))
        assert (folder / "summary.md").read_text(encoding="utf-8").startswith("# New")

    def test_a_second_prose_run_does_not_archive_again(self, tmp_path, monkeypatch):
        """Prose can run many times; only the name step snapshots the user's file."""
        folder = tmp_path / "item"
        folder.mkdir()
        source = str(folder / "b.mp4")
        (folder / "b.mp4").write_bytes(b"\x00")
        (folder / "summary.md").write_text("# Old\n", encoding="utf-8")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        sz.run(_args(step="name", run=run))
        sz.run(_args(step="prose", run=run, title="First", summary_text="a"))
        sz.run(_args(step="prose", run=run, title="Second", summary_text="b"))
        archives = [n for n in os.listdir(folder) if n.startswith("summary_")]
        assert len(archives) == 1, archives
        assert (folder / "summary.md").read_text(encoding="utf-8").startswith("# Second")


class TestCleanup:
    def test_verify_removes_the_run_scratch_on_success(self, tmp_path, monkeypatch):
        folder = tmp_path / "item"
        folder.mkdir()
        source = str(folder / "b.mp4")
        (folder / "b.mp4").write_bytes(b"\x00")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        sz.run(_args(step="name", run=run))
        sz.run(_args(step="prose", run=run, title="T", summary_text="s"))
        outcome = sz.run(_args(step="verify", run=run))
        assert outcome.data["cleaned"] is True
        assert not os.path.isdir(run)
        # Only the document, the media and img/ remain.
        assert os.path.isfile(folder / "summary.md")
        assert os.path.isfile(folder / "b.mp4")

    def test_verify_prunes_the_item_sidecars_and_reading_copies(self, tmp_path, monkeypatch):
        """A finished item keeps only the inlined figures, not the run's sidecars."""
        folder = tmp_path / "item"
        folder.mkdir()
        source = str(folder / "b.mp4")
        (folder / "b.mp4").write_bytes(b"\x00")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        # Simulate what the slides step leaves in the item's img/.
        img = folder / "img"
        img.mkdir()
        (img / "001 - p01.png").write_bytes(b"\x89PNG")
        (img / "manifest.json").write_text("{}", encoding="utf-8")
        (img / "README.md").write_text("# x\n", encoding="utf-8")
        readings = img / "readings"
        readings.mkdir()
        (readings / "001.q3.jpg").write_bytes(b"j")

        sz.run(_args(step="name", run=run))
        sz.run(_args(step="prose", run=run, title="T", summary_text="s"))
        outcome = sz.run(_args(step="verify", run=run))

        assert "prunedSidecars" in outcome.data
        assert not (img / "manifest.json").exists()
        assert not (img / "README.md").exists()
        assert not readings.exists()
        # The figure the document references survives.
        assert (img / "001 - p01.png").is_file()


class TestWorkspaceHelper:
    def test_inside_workspace_uses_whole_segments(self, tmp_path):
        assert workspace.inside_workspace(str(tmp_path / "a" / "b"), str(tmp_path))
        assert not workspace.inside_workspace(str(tmp_path) + "x", str(tmp_path))

    def test_sanitize_strips_illegal_and_percent_decodes(self):
        assert workspace.sanitize_name("a/b:c?d*e") == "abcde"
        assert workspace.sanitize_name("My%20Video") == "My Video"
        assert workspace.sanitize_name("") == "item"

    def test_sanitize_folds_fullwidth_punctuation(self):
        assert "?" not in workspace.sanitize_name("уже сегодня？")[-1:]

    def test_destination_dir_reports_internal(self, tmp_path):
        inside = tmp_path / "lore" / "x.mp4"
        destination, internal = workspace.destination_dir(str(inside), workspace.KIND_SUMMARIES,
                                                          root=str(tmp_path))
        assert internal is True
        assert destination == str(tmp_path / "lore")

    def test_a_sibling_workspace_is_external(self, tmp_path):
        """A path under a DIFFERENT workspace routes out -- never treated as internal.

        The toolchain knows ONE root (the current workspace). A sibling project --
        ``repos/zoombie`` while the source lives in ``repos/kb`` -- must be external,
        exactly like a URL, so it can never be mis-routed into a folder it does not
        belong to because the toolchain "recognised" it from another run.
        """
        here = tmp_path / "zoombie"
        here.mkdir()
        elsewhere = tmp_path / "kb" / "Crimson" / "video.mp4"
        destination, internal = workspace.destination_dir(
            str(elsewhere), workspace.KIND_SUMMARIES, root=str(here)
        )
        assert internal is False
        assert destination.startswith(str(here / "_unsorted" / "summaries"))

    def test_destination_dir_routes_a_url_out(self, tmp_path):
        destination, internal = workspace.destination_dir(
            "https://example.com/watch?v=abc", workspace.KIND_SUMMARIES,
            root=str(tmp_path), name="A Talk",
        )
        assert internal is False
        assert destination == str(tmp_path / "_unsorted" / "summaries" / "A Talk")


class TestDownloadRouting:
    """The standalone download tool uses the SAME workspace rule as summarize."""

    def test_a_url_lands_under_unsorted_download(self, tmp_path, monkeypatch):
        from zoombie.commands import download

        monkeypatch.chdir(tmp_path)
        directory, internal = download.destination(_args(
            source="https://example.com/watch?v=abc", name="A Talk"
        ))
        assert internal is False
        assert directory == str(tmp_path / "_unsorted" / "download" / "A Talk")

    def test_an_internal_source_keeps_its_folder(self, tmp_path, monkeypatch):
        from zoombie.commands import download

        monkeypatch.chdir(tmp_path)
        source = tmp_path / "lore" / "clip.mp4"
        directory, internal = download.destination(_args(source=str(source)))
        assert internal is True
        assert directory == str(tmp_path / "lore")

    def test_an_explicit_download_dir_wins(self, tmp_path, monkeypatch):
        from zoombie.commands import download

        monkeypatch.chdir(tmp_path)
        target = tmp_path / "elsewhere"
        directory, internal = download.destination(_args(
            source="https://example.com/watch?v=abc", download_dir=str(target)
        ))
        assert internal is False
        assert directory == str(target)

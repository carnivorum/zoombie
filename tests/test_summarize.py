"""Tests for the stepwise ``summarize`` flow and the download routing.

Step 0 (``source``) drives ``pipeline`` and therefore whisper, so these tests
SEED the run state directly - a ``run.json`` plus a ``transcript.txt`` - and then
exercise the steps that decide routing, naming, archiving and cleanup. That is
where the behaviour this redesign adds actually lives.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from zoombie.commands import summarize as sz
from zoombie.lib import workspace
from zoombie.lib.errors import ZoombieError


def _seed_run(tmp_path, monkeypatch, *, source: str, internal: bool,
              proposed: str = "Proposed Name", transcript: str = "intro words\n\nbody words\n",
              media: str | None = None, kind: str = "video",
              external_local: bool | None = None, run_version: int | None = None):
    """Create a run scratch dir the name/prose/verify steps can consume.

    ``media`` is the readable media handle the source step would have resolved: the
    source's own path for a local file, the scratch download for a URL.
    ``external_local`` mirrors the source step's record of whether the USER owns the
    media and must be asked how to place it; it defaults to "a local, non-internal
    file with a handle". ``run_version`` overrides the recorded schema version
    (used to drive the M3 pre-change-run refusal).
    """
    monkeypatch.chdir(tmp_path)
    run = tmp_path / ".tmp" / "zoombie-summarize" / "abc123"
    run.mkdir(parents=True)
    if internal:
        destination = os.path.dirname(os.path.abspath(source))
    else:
        destination = os.path.join(
            workspace.unsorted_dir(workspace.KIND_SUMMARIES), proposed
        )
    if external_local is None:
        external_local = (not internal) and bool(media)
    base = run / "transcript"
    (run / "transcript.txt").write_text(transcript, encoding="utf-8")
    (run / "run.json").write_text(json.dumps({
        "source": source, "kind": kind, "internal": internal,
        "destination": destination, "proposed": proposed,
        "sourceMedia": media, "media": media,
        "externalLocal": external_local,
        "mediaMode": None, "mediaPlaced": None,
        "base": str(base).replace("\\", "/"),
        "destinationFinal": None,
        "runVersion": sz.RUN_VERSION if run_version is None else run_version,
    }), encoding="utf-8")
    return str(run), destination


def _args(**overrides):
    import argparse

    base = dict(step=None, source=None, run=None, name=None, slides=None,
                times=None, title=None, summary_text=None, criticism=None,
                sections=None, media=None, confirm_move=False, language="auto",
                work_root=None, dry_run=False, force=False,
                overwrite=False, archive=False, keep=None, drop=None)
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
        """A folder that is NOT ours is never written into: the name is uniquified.

        The colliding folder carries a foreign file, which is what makes it someone
        else's. An EMPTY folder is treated as an aborted run's leftover and reused --
        see ``test_an_existing_item_is_reused_not_suffixed`` -- so only a folder with
        content of its own falls through to ``unique_name``.
        """
        taken = tmp_path / "_unsorted" / "summaries" / "My Item"
        taken.mkdir(parents=True)
        (taken / "notes.txt").write_text("not ours", encoding="utf-8")
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


class TestOverwriteSafety:
    """An existing summary is never silently destroyed: the step REFUSES, the user chooses."""

    @staticmethod
    def _item(tmp_path, *, figures: int = 0):
        folder = tmp_path / "item"
        folder.mkdir()
        (folder / "b.mp4").write_bytes(b"\x00")
        (folder / "summary.md").write_text("# Old\n", encoding="utf-8")
        if figures:
            image_dir = folder / "img"
            image_dir.mkdir()
            for index in range(1, figures + 1):
                (image_dir / f"{index:03d} - 00-0{index}-00.png").write_bytes(b"png")
        return str(folder / "b.mp4"), folder

    def test_an_existing_summary_is_refused_without_a_choice(self, tmp_path, monkeypatch):
        """The document at risk is NAMED and nothing is touched until the user chooses."""
        source, folder = self._item(tmp_path, figures=2)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        with pytest.raises(ZoombieError) as caught:
            sz.run(_args(step="name", run=run))
        message = str(caught.value)
        assert "-Archive" in message and "-Overwrite" in message
        # The refusal is a no-op on disk: the document and its figures are intact.
        assert (folder / "summary.md").read_text(encoding="utf-8").startswith("# Old")
        assert len(os.listdir(folder / "img")) == 2

    def test_archive_moves_the_document_and_its_figures_together(
        self, tmp_path, monkeypatch
    ):
        """The archive is a FOLDER, because the figures are index-numbered.

        A flat ``summary_<stamp>.md`` beside a rebuilt ``img/`` would repoint the
        archived document at the wrong pictures, so the pair travels together and
        every relative ``img/...`` link keeps resolving.
        """
        source, folder = self._item(tmp_path, figures=2)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        named = sz.run(_args(step="name", run=run, archive=True))
        archived = named.data["archived"]
        assert archived is not None and archived["images"] == 2
        archive_dir = archived["to"]
        assert os.path.isdir(archive_dir)
        assert open(os.path.join(archive_dir, "summary.md"), encoding="utf-8").read().startswith("# Old")
        assert sorted(os.listdir(os.path.join(archive_dir, "img"))) == [
            "001 - 00-01-00.png", "002 - 00-02-00.png",
        ]
        # The live item is clean: the next run builds a fresh document and img/.
        assert not (folder / "summary.md").exists()
        assert not (folder / "img").exists()
        # The MEDIA is never collateral damage.
        assert (folder / "b.mp4").is_file()
        assert named.data["overwrite"] == {"required": True, "archived": True, "mode": "archive"}

    def test_overwrite_replaces_with_no_backup_and_keeps_the_media(
        self, tmp_path, monkeypatch
    ):
        """-Overwrite is the user's explicit "no backup", and the source survives it."""
        source, folder = self._item(tmp_path, figures=1)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        named = sz.run(_args(step="name", run=run, overwrite=True))
        assert named.data["archived"] is None
        assert named.data["overwrite"] == {"required": True, "archived": False, "mode": "overwrite"}
        # No snapshot was taken, but the document is not removed HERE either: the
        # prose step rewrites it. The media is untouched.
        assert (folder / "summary.md").is_file()
        assert (folder / "b.mp4").is_file()
        assert not any(name.startswith("summary_") for name in os.listdir(folder))

    def test_a_fresh_target_needs_no_flag(self, tmp_path, monkeypatch):
        """The gate must not tax the ordinary first run."""
        folder = tmp_path / "item"
        folder.mkdir()
        (folder / "b.mp4").write_bytes(b"\x00")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=str(folder / "b.mp4"), internal=True)
        named = sz.run(_args(step="name", run=run))
        assert named.data["overwrite"] == {"required": False, "archived": False, "mode": None}
        assert named.data["existing"]["summary"] is None

    def test_an_existing_item_is_reused_not_suffixed(self, tmp_path, monkeypatch):
        """A re-run is a re-run of the SAME item, not ``Name (2)``."""
        source, folder = self._item(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        named = sz.run(_args(step="name", run=run, overwrite=True))
        assert named.data["itemDir"] == str(folder)

    def test_a_foreign_folder_still_gets_a_unique_name(self, tmp_path, monkeypatch):
        """A directory that is not ours is never written into."""
        (tmp_path / "_unsorted" / "summaries" / "My Item").mkdir(parents=True)
        (tmp_path / "_unsorted" / "summaries" / "My Item" / "notes.txt").write_text("mine")
        run, _dest = _seed_run(tmp_path, monkeypatch,
                               source=str(tmp_path / "elsewhere" / "video.mp4"),
                               internal=False)
        named = sz.run(_args(step="name", run=run, name="My Item"))
        assert os.path.basename(named.data["itemDir"]) == "My Item (2)"

    def test_an_archived_item_is_still_reused(self, tmp_path, monkeypatch):
        """Regression: an archive leaves no ``summary.md``, so the item looked foreign.

        The live test caught this: after ``-Archive`` the item root holds only the
        archive folder and the media, and the next run then created ``... (2)``
        instead of reusing the item it had just archived.
        """
        source, folder = self._item(tmp_path, figures=1)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        first = sz.run(_args(step="name", run=run, archive=True))
        archive_dir = first.data["archived"]["to"]
        assert os.path.isdir(archive_dir)
        # The item now holds NO summary.md and NO img/ -- only the archive + media.
        # This is the state that made it look like a stranger's folder.
        assert not (folder / "summary.md").exists()
        assert not (folder / "img").exists()
        assert (folder / "b.mp4").is_file()

        # A second run against a source in that SAME folder. Nothing is written to
        # the folder first: the point is the state the archive left behind. The
        # first run's scratch is cleared because ``_seed_run`` uses a fixed name.
        shutil.rmtree(tmp_path / ".tmp", ignore_errors=True)
        run2, _dest2 = _seed_run(tmp_path, monkeypatch,
                                 source=str(folder / "b.mp4"), internal=True)
        second = sz.run(_args(step="name", run=run2))
        assert second.data["itemDir"] == str(folder), (
            "an archived item must be REUSED, not suffixed with (2)"
        )

    def test_a_second_prose_run_does_not_archive_again(self, tmp_path, monkeypatch):
        """Prose can run many times; only the name step snapshots the user's file."""
        source, folder = self._item(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=True)
        sz.run(_args(step="name", run=run, archive=True))
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


class TestMediaPlacement:
    """An EXISTING local source: the user's keep/copy/move/none choice."""

    @staticmethod
    def _local(tmp_path) -> str:
        """A local media file OUTSIDE the workspace (routes to _unsorted)."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        media = elsewhere / "clip.mp4"
        media.write_bytes(b"media-bytes")
        return str(media)

    def test_default_copies_an_external_local_source(self, tmp_path, monkeypatch):
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        outcome = sz.run(_args(step="name", run=run, name="My Item"))
        item = outcome.data["itemDir"]
        assert outcome.data["mediaMode"] == "copy"
        assert os.path.isfile(os.path.join(item, "clip.mp4"))
        # A copy leaves the user's own file exactly where it was.
        assert os.path.isfile(media)

    def test_move_without_the_confirm_does_not_relocate_the_user_file(
        self, tmp_path, monkeypatch
    ):
        """H3: a bare ``move`` is REFUSED -- the user's file is never touched.

        ``move`` relocates a file the user owns, with no snapshot, so a mistyped or
        accidental move must not destroy the source location. Without the explicit
        acknowledgement the step refuses and the original stays put.
        """
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        with pytest.raises(ZoombieError, match="ConfirmMove"):
            sz.run(_args(step="name", run=run, name="My Item", media="move"))
        # No relocation and no copy: the user's file is exactly where it was.
        assert os.path.isfile(media)
        item = os.path.join(str(tmp_path / "_unsorted" / "summaries"), "My Item")
        assert not os.path.exists(os.path.join(item, "clip.mp4"))
        # H2: the refusal happened BEFORE any folder was created, so the name was
        # not consumed either.
        assert not os.path.isdir(item)

    def test_move_relocates_the_media_only_when_confirmed(self, tmp_path, monkeypatch):
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        outcome = sz.run(_args(step="name", run=run, name="My Item",
                               media="move", confirm_move=True))
        item = outcome.data["itemDir"]
        assert outcome.data["mediaMode"] == "move"
        assert os.path.isfile(os.path.join(item, "clip.mp4"))
        # A CONFIRMED move removes the original.
        assert not os.path.exists(media)

    def test_an_unknown_mode_leaves_no_folder_and_consumes_no_name(
        self, tmp_path, monkeypatch
    ):
        """H2: the media answer is validated BEFORE the destination is created.

        A folder created first would make a retry find the name taken (``My Item
        (2)``) and litter an empty item; validating first means one typo costs one
        refusal and nothing on disk.
        """
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        with pytest.raises(ZoombieError, match="Media"):
            sz.run(_args(step="name", run=run, name="My Item", media="teleport"))
        item = os.path.join(str(tmp_path / "_unsorted" / "summaries"), "My Item")
        assert not os.path.isdir(item), "a bad -Media must not create the folder"

        # A corrected retry keeps the plain name: ``My Item``, never ``My Item (2)``.
        outcome = sz.run(_args(step="name", run=run, name="My Item", media="copy"))
        assert os.path.basename(outcome.data["itemDir"]) == "My Item"

    def test_a_vanished_source_media_fails_at_name_naming_the_source(
        self, tmp_path, monkeypatch
    ):
        """M2: deleting the source between source and name is named at NAME.

        The alternative is the misleading ``slides`` message ("no retained media to
        extract slides from"), which blames the slides step for a file that was
        gone before it ran.
        """
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        os.remove(media)
        with pytest.raises(ZoombieError) as caught:
            sz.run(_args(step="name", run=run, name="My Item"))
        message = str(caught.value)
        assert "source media is gone" in message
        assert "clip.mp4" in message

    def test_the_external_parent_comes_from_the_recorded_destination(
        self, tmp_path, monkeypatch
    ):
        """M1: the item lands under the parent the SOURCE step reported.

        The name step used to rebuild the parent from ``unsorted_dir`` (a fresh cwd
        computation). It must instead use the recorded ``destination``'s parent, so
        the item can never drift from the ``data.destination`` the source step
        already showed the agent.
        """
        media = self._local(tmp_path)
        run, destination = _seed_run(tmp_path, monkeypatch, source=media,
                                     internal=False, media=media,
                                     proposed="Recorded Name")
        assert os.path.basename(destination) == "Recorded Name"
        outcome = sz.run(_args(step="name", run=run, name="My Item"))
        assert os.path.dirname(outcome.data["itemDir"]) == os.path.dirname(destination)

    def test_an_old_run_json_without_source_media_is_refused(self, tmp_path, monkeypatch):
        """M3: a run scratch is not resumable across versions; say so explicitly."""
        media = self._local(tmp_path)
        # Simulate a pre-change run: no ``runVersion`` at all.
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media, run_version=None)
        raw = json.loads(open(os.path.join(run, "run.json"), encoding="utf-8").read())
        raw.pop("runVersion", None)
        open(os.path.join(run, "run.json"), "w", encoding="utf-8").write(
            json.dumps(raw)
        )
        with pytest.raises(ZoombieError, match="not resumable across versions"):
            sz.run(_args(step="name", run=run, name="My Item"))

    def test_none_keeps_no_copy_but_the_source_survives(self, tmp_path, monkeypatch):
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        outcome = sz.run(_args(step="name", run=run, name="My Item", media="none"))
        item = outcome.data["itemDir"]
        assert outcome.data["mediaMode"] == "none"
        assert outcome.data["media"] is None
        assert not os.path.exists(os.path.join(item, "clip.mp4"))
        assert os.path.isfile(media)

    def test_an_in_place_source_defaults_to_keep(self, tmp_path, monkeypatch):
        folder = tmp_path / "item"
        folder.mkdir()
        media = folder / "berserk.mp4"
        media.write_bytes(b"\x00")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=str(media),
                               internal=True, media=str(media))
        outcome = sz.run(_args(step="name", run=run))
        assert outcome.data["mediaMode"] == "keep"
        assert outcome.data["media"] == str(media)

    def test_none_on_an_in_place_source_keeps_no_copy(self, tmp_path, monkeypatch):
        folder = tmp_path / "item"
        folder.mkdir()
        media = folder / "berserk.mp4"
        media.write_bytes(b"\x00")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=str(media),
                               internal=True, media=str(media))
        outcome = sz.run(_args(step="name", run=run, media="none"))
        assert outcome.data["mediaMode"] == "none"
        assert outcome.data["media"] is None
        # A "none" choice is a no-op for the file, never a delete.
        assert media.is_file()

    def test_an_unknown_mode_is_refused(self, tmp_path, monkeypatch):
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        with pytest.raises(ZoombieError, match="Media"):
            sz.run(_args(step="name", run=run, name="X", media="teleport"))

    def test_the_name_step_echoes_the_media_choice(self, tmp_path, monkeypatch):
        """H1/L1/L2: the answer travels back under its own name (``mediaChoice``).

        The source step describes the QUESTION (``data.media``, a dict); the name
        step reports the ANSWER under a distinct key, so the two steps' media
        shapes never collide and the decision is machine-readable beside ``why``.
        """
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        outcome = sz.run(_args(step="name", run=run, name="My Item", media="copy"))
        choice = outcome.data["mediaChoice"]
        assert isinstance(choice, dict)
        assert choice == {"mode": "copy", "choiceRequired": True, "default": "copy"}

    def test_no_answer_is_required_for_a_kind_with_no_media(self, tmp_path, monkeypatch):
        """L2: a PDF/image kind gets NO media question and NO misleading default."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sz, "_dispatch", lambda *_a, **_k: _empty_outcome())
        source = str(tmp_path / "paper.pdf")
        open(source, "wb").write(b"%PDF")
        outcome = sz.run(_args(step="source", source=source))
        block = outcome.data["media"]
        assert block["choiceRequired"] is False
        assert "default" not in block, "no question means no default"
        assert "-Media" not in outcome.data["next"]["args"]

    @pytest.mark.parametrize("internal", [True, False])
    def test_a_local_source_actually_computes_the_media_handle(
        self, tmp_path, monkeypatch, internal
    ):
        """The SOURCE step itself was uncovered (``_seed_run`` injected the handle).

        Drive ``_step_source`` for a real LOCAL file with the transcription/render
        stubbed, and assert the handle, the question flag, the default and that the
        run records a non-null ``sourceMedia`` -- internal and external. The bug
        that shipped was that only the seed was exercised, never the step.
        """
        workspace_root = tmp_path / "ws"
        workspace_root.mkdir()
        if internal:
            folder = workspace_root / "lore"
            folder.mkdir()
            source = folder / "clip.mp4"
        else:
            outside = tmp_path / "outside"
            outside.mkdir()
            source = outside / "clip.mp4"
        source.write_bytes(b"media")

        monkeypatch.chdir(workspace_root)
        monkeypatch.setattr(sz, "_dispatch", lambda *_a, **_k: _source_outcome())
        outcome = sz.run(_args(step="source", source=str(source)))

        block = outcome.data["media"]
        assert block["source"] == sz.paths.absolute(str(source))
        assert block["insideItem"] is internal
        # Only an EXTERNAL local file asks how to place it.
        assert block["choiceRequired"] is (not internal)
        assert ("default" in block) is (not internal)
        if not internal:
            assert block["default"] == "copy"
        assert "-Media" not in outcome.data["next"]["args"]
        # The run.json records a non-null handle.
        state = sz._read_run(outcome.data["run"])
        assert state["sourceMedia"] == sz.paths.absolute(str(source))

    def test_slides_falls_back_to_the_source_when_no_copy_was_kept(
        self, tmp_path, monkeypatch
    ):
        """The ``media=None`` of a "none" choice must not blind ``slides``.

        ``slides`` reads ``state.media or state.sourceMedia``; this drives the step
        with ``media`` None and a readable ``sourceMedia`` to prove the fallback.
        """
        media = self._local(tmp_path)
        run, _dest = _seed_run(tmp_path, monkeypatch, source=media,
                               internal=False, media=media)
        sz.run(_args(step="name", run=run, name="My Item", media="none"))

        seen: dict = {}

        def fake_dispatch(command, argv):
            if command == "slides":
                seen["source"] = argv[argv.index("-Source") + 1]
                return _empty_outcome()
            return _empty_outcome()

        monkeypatch.setattr(sz, "_dispatch", fake_dispatch)
        outcome = sz.run(_args(step="slides", run=run, slides="true"))
        assert outcome.data["slides"]["requested"] is True
        # The slides step read the SOURCE (media is None under a "none" choice).
        assert seen["source"] == sz.paths.absolute(media)


def _empty_outcome():
    from zoombie.cli import Outcome

    return Outcome(ok=True, data={})


def _source_outcome():
    """A pipeline/readpdf-style result with no keepable media of its own."""
    from zoombie.cli import Outcome

    return Outcome(ok=True, data={"source": {"title": None}})


class TestBlockTwoSource:
    """L3: a raw origin path must never be emitted bare into block 2."""

    def test_a_source_with_spaces_and_cyrillic_is_a_code_span(self, tmp_path, monkeypatch):
        folder = tmp_path / "item"
        folder.mkdir()
        source = "https://example.com/видео (1).mp4"
        run, _dest = _seed_run(tmp_path, monkeypatch, source=source, internal=False,
                               media=None, kind="video", external_local=False)
        sz.run(_args(step="name", run=run, name="My Item", media="none"))
        outcome = sz.run(_args(step="prose", run=run, title="T", summary_text="s"))
        text = open(outcome.data["summary"], encoding="utf-8").read()
        # The raw URL is wrapped in a code span, not emitted as bare text.
        assert f"`{source}`" in text

    def test_a_backtick_in_the_source_is_fenced_out(self):
        assert sz._code_span("a`b") == "`` a`b ``"
        assert sz._code_span("plain") == "`plain`"
        assert sz._code_span("") == ""


class TestNextArgsAreReplayable:
    """Guard: a step's recommended ``data.next.args`` must parse through the CLI.

    ``args`` is the recommended INVOCATION, so the agent may replay it verbatim;
    it therefore must never carry a value argparse rejects. The defect that
    shipped was exactly that class -- the source step emitted
    ``-Media "keep|copy|move|none"``, which is not in ``-Media``'s ``choices`` and
    would exit the parser. This walks the real step seams (source -> name for an
    in-place AND an external source -- the source step is where the placeholder
    lived -- and name -> slides) and feeds each ``next.args`` through the real
    :func:`cli.build_parser`. An unparseable ``args`` is a test failure here,
    rather than a runtime surprise.
    """

    def test_every_recommended_next_call_parses_through_the_cli(
        self, tmp_path, monkeypatch
    ):
        from zoombie import cli

        def replay(block: dict) -> None:
            # The source step's block is ``command="summarize"``; the next step is
            # the ``-Step`` value already carried in ``args``.
            assert block["command"] == "summarize"
            argv = ["summarize"]
            for key, value in block["args"].items():
                argv.extend([key, str(value)])
            cli.build_parser().parse_args(argv)  # must not raise

        # source -> name, for an in-place and an external local source.
        for internal in (True, False):
            root = tmp_path / ("ws_inside" if internal else "ws_outside")
            root.mkdir()
            if internal:
                folder = root / "lore"
                folder.mkdir()
                source = folder / "clip.mp4"
            else:
                outside = tmp_path / "outside"
                outside.mkdir()
                source = outside / "clip.mp4"
            source.write_bytes(b"media")
            monkeypatch.chdir(root)
            monkeypatch.setattr(sz, "_dispatch", lambda *_a, **_k: _source_outcome())
            outcome = sz.run(_args(step="source", source=str(source)))
            assert outcome.data["next"]["args"]["-Step"] == "name"
            replay(outcome.data["next"])

        # name -> slides.
        media = tmp_path / "item" / "clip.mp4"
        media.parent.mkdir(exist_ok=True)
        media.write_bytes(b"media")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=str(media),
                               internal=False, media=str(media))
        outcome = sz.run(_args(step="name", run=run, name="My Item", media="copy"))
        assert outcome.data["next"]["args"]["-Step"] == "slides"
        replay(outcome.data["next"])


class TestSlideSelection:
    """The silent no-op: a keep/drop must never be parsed and then discarded.

    The defect that shipped: ``-Drop`` without ``-Slides`` returned ok/exit 0 with an
    UNCHANGED frame set, so the whole frame set was published instead of the kept
    subset and the agent had no signal. A narrowing intent that fails OPEN is the
    worst shape a selection can have, so both halves are pinned here -- the flag is
    inferred, and the recommended prune invocation carries it.
    """

    @staticmethod
    def _framed_dispatch(seen: dict):
        from zoombie.cli import Outcome

        def dispatch(command, argv):
            if command != "slides":
                return Outcome(ok=True, data={})
            seen["argv"] = list(argv)
            drop = argv[argv.index("-Drop") + 1] if "-Drop" in argv else None
            applied = bool(drop)
            return Outcome(ok=True, data={
                "images": {
                    "count": 1 if applied else 3, "proposed": 3,
                    "selection": {
                        "applied": applied, "keep": [], "drop": [drop] if drop else [],
                        "dropped": 1 if applied else 0,
                        "droppedIds": [drop] if drop else [],
                    },
                },
                "visionFrames": [
                    {"id": f"f{index:03d}", "path": f"r/{index}.jpg",
                     "timeSec": float(index), "timecode": f"00-00-0{index}"}
                    for index in (1, 2, 3)
                ],
            })
        return dispatch

    def _named_run(self, tmp_path, monkeypatch):
        media = tmp_path / "item" / "clip.mp4"
        media.parent.mkdir(exist_ok=True)
        media.write_bytes(b"media")
        run, _dest = _seed_run(tmp_path, monkeypatch, source=str(media),
                               internal=False, media=str(media))
        sz.run(_args(step="name", run=run, name="My Item", media="copy"))
        return run

    def test_a_drop_without_slides_is_inferred_not_ignored(self, tmp_path, monkeypatch):
        run = self._named_run(tmp_path, monkeypatch)
        seen: dict = {}
        monkeypatch.setattr(sz, "_dispatch", self._framed_dispatch(seen))
        outcome = sz.run(_args(step="slides", run=run, drop="f001"))
        # The run HAPPENED: the selection reached the tool rather than being dropped.
        assert "-Drop" in seen["argv"]
        assert seen["argv"][seen["argv"].index("-Drop") + 1] == "f001"
        assert outcome.data["slides"]["requested"] is True
        assert outcome.data["slides"]["selection"]["applied"] is True
        assert outcome.data["slides"]["selection"]["droppedIds"] == ["f001"]

    def test_the_prune_invocation_carries_slides_true(self, tmp_path, monkeypatch):
        """``data.next.args`` must be replayable -- it must not reproduce the no-op."""
        run = self._named_run(tmp_path, monkeypatch)
        monkeypatch.setattr(sz, "_dispatch", self._framed_dispatch({}))
        outcome = sz.run(_args(step="slides", run=run, slides="true"))
        # The command is always ``summarize``; the STEP lives in ``args``.
        assert outcome.data["next"]["args"]["-Step"] == "slides"
        assert outcome.data["next"]["args"]["-Slides"] == "true"
        # Replaying it verbatim must actually prune, not silently accept everything.
        # ``-Slides true`` is the flag the old invocation omitted, which is why the
        # replay used to reproduce the no-op.
        pruned = sz.run(_args(step="slides", run=run, slides="true", drop="f002"))
        assert pruned.data["slides"]["selection"]["applied"] is True
        assert pruned.data["slides"]["selection"]["droppedIds"] == ["f002"]

    def test_the_why_names_the_flag_and_the_applied_check(self, tmp_path, monkeypatch):
        run = self._named_run(tmp_path, monkeypatch)
        monkeypatch.setattr(sz, "_dispatch", self._framed_dispatch({}))
        outcome = sz.run(_args(step="slides", run=run, slides="true"))
        why = outcome.data["next"]["why"]
        assert "-Slides true" in why
        assert "selection.applied" in why

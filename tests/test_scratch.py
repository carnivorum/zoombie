"""Tests for the CLI-owned scratch lifecycle (plan §10).

Four properties are pinned here, and each maps to one line of the brief:

1. a DEFAULT run cleans up after itself and leaves nothing under the toolchain
   root, the workspace or ``C:\\Temp`` (``data.scratch.leftover`` is false);
2. ``-KeepScratch`` retains the run's scratch for inspection;
3. ``clean -CleanScratch`` removes only toolchain-owned scratch -- a user folder in
   a root, and a ``-Output``/``-ImageDir`` destination, are reported, never deleted;
4. a KILLED run's leftover (``remove_quietly``/``remove_work_dir`` gave up on a busy
   directory) is exactly the case the verb exists for.

The roots are redirected with ``ZOOMBIE_ENV_ROOT`` so nothing touches the real
``%USERPROFILE%\\zoombie-env``.
"""

from __future__ import annotations

import argparse
import os
from types import SimpleNamespace

import pytest

from zoombie import cli
from zoombie.commands import slides as slides_cmd
from zoombie.lib import paths, scratch

# A real GUID shape, so a name classified as ours really is one this toolchain
# creates (new_ascii_dir/new_temp_dir use uuid4().hex).
GUID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Point the toolchain root at tmp_path so every root is hermetic."""
    monkeypatch.setenv("ZOOMBIE_ENV_ROOT", str(tmp_path / "env"))
    return tmp_path / "env"


def _png() -> bytes:
    """A 2x2 PNG, so slides.png_size reads a genuine IHDR."""
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    ihdr = (2).to_bytes(4, "big") + (2).to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])
    raw = b"".join(b"\x00" + b"\x10\x10" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------- #
# naming: what counts as ours
# --------------------------------------------------------------------------- #

class TestRunMarker:
    @pytest.mark.parametrize("name", [
        GUID,
        f"tmp-{GUID}",
        f"zoombie-unpack-{GUID}",
        f"zoombie-tesseract-dl-{GUID}",
        f"zoombie-pipeline-{GUID}",
    ])
    def test_a_run_name_is_recognised(self, name):
        assert scratch.is_run_dir(name) is True

    @pytest.mark.parametrize("name", [
        "", "work", "notes", "projects", "My Documents",
        "0123456789abcdef0123456789abcde",       # 31 hex: not a UUID
        "0123456789abcdef0123456789abcdef00",    # 34 hex
        GUID.upper(),                            # uuids are lowercase
    ])
    def test_a_foreign_name_is_not_ours(self, name):
        assert scratch.is_run_dir(name) is False

    def test_ownership_requires_the_parent_to_be_a_root(self, tmp_path, monkeypatch):
        """A GUID name AWAY from a root is not ours -- even a real GUID folder."""
        monkeypatch.setenv("ZOOMBIE_ENV_ROOT", str(tmp_path / "env"))
        elsewhere = tmp_path / "workspace" / GUID
        elsewhere.mkdir(parents=True)
        assert scratch.is_owned(str(elsewhere)) is False

    def test_a_guid_under_the_work_root_is_owned(self, root):
        work = root / "work" / GUID
        work.mkdir(parents=True)
        assert scratch.is_owned(str(work)) is True


# --------------------------------------------------------------------------- #
# inventory + sweep
# --------------------------------------------------------------------------- #

class TestSweep:
    def _roots(self, root):
        work = root / "work"
        temp = root / "tmp"
        work.mkdir(parents=True, exist_ok=True)
        temp.mkdir(parents=True, exist_ok=True)
        return work, temp

    def test_the_sweep_removes_ours_and_spares_foreign(self, root):
        work, temp = self._roots(root)
        (work / GUID).mkdir()
        (temp / f"zoombie-unpack-{GUID}").mkdir()
        # A user's own folder that merely sits under a root:
        (work / "my-notes").mkdir()

        result = scratch.clean()

        assert set(result["removed"]) == {str(work / GUID), str(temp / f"zoombie-unpack-{GUID}")}
        assert result["removedCount"] == 2
        assert result["spared"] == [str(work / "my-notes")]
        assert not (work / GUID).exists()
        assert (work / "my-notes").exists()

    def test_dry_run_reports_and_removes_nothing(self, root):
        work, _temp = self._roots(root)
        (work / GUID).mkdir()

        result = scratch.clean(dry_run=True)

        assert result["dryRun"] is True
        assert result["removed"] == [str(work / GUID)]
        assert (work / GUID).exists(), "a dry run must not delete"

    def test_the_sweep_never_reaches_outside_a_root(self, root, tmp_path):
        """A workspace /.tmp destination is the caller's artifact, not our scratch."""
        self._roots(root)
        workspace_tmp = tmp_path / "workspace" / ".tmp" / GUID
        workspace_tmp.mkdir(parents=True)

        result = scratch.clean()

        assert result["removed"] == []
        assert workspace_tmp.exists()

    def test_a_missing_root_is_not_an_error(self, root):
        result = scratch.clean()
        assert result["removed"] == []
        assert result["removedCount"] == 0


# --------------------------------------------------------------------------- #
# the killed-run case the verb exists for
# --------------------------------------------------------------------------- #

class TestKilledRunLeftover:
    def test_an_orphaned_scratch_dir_is_cleanable(self, root):
        """The retry-without-blocking helpers can give up on a busy dir; the verb
        is what removes the survivor."""
        work = root / "work"
        work.mkdir(parents=True)
        orphan = work / GUID
        (orphan / "whisper.log").parent.mkdir(parents=True)
        (orphan / "whisper.log").write_text("held by a dead child", encoding="utf-8")

        assert scratch.is_owned(str(orphan)) is True
        result = scratch.clean()

        assert str(orphan) in result["removed"]
        assert not orphan.exists()

    def test_a_busy_dir_is_reported_not_hidden(self, root, monkeypatch):
        work = root / "work" / GUID
        work.mkdir(parents=True)
        # Emulate the "still held" case without needing a live handle.
        monkeypatch.setattr(scratch.paths, "remove_work_dir", lambda path, **kw: False)

        block = scratch.report(str(work), kept=False)

        assert block["leftover"] is True
        assert block["removed"] is False
        assert "clean -CleanScratch" in block["hint"]


# --------------------------------------------------------------------------- #
# the per-run report
# --------------------------------------------------------------------------- #

class TestReport:
    def test_default_removes(self, root):
        work = root / "work" / GUID
        work.mkdir(parents=True)
        block = scratch.report(str(work), kept=False)
        assert block == {"path": str(work), "kept": False, "removed": True, "leftover": False}
        assert not work.exists()

    def test_kept_retains(self, root):
        work = root / "work" / GUID
        work.mkdir(parents=True)
        block = scratch.report(str(work), kept=True)
        assert block["kept"] is True and block["leftover"] is False
        assert work.exists()

    def test_no_work_dir_is_reported_honestly(self):
        assert scratch.report(None, kept=False) == {
            "path": None, "kept": False, "removed": False, "leftover": False,
        }

    def test_keep_requested_accepts_both_spellings(self):
        assert scratch.keep_requested(SimpleNamespace(keep_scratch=True)) is True
        assert scratch.keep_requested(SimpleNamespace(keep_work=True)) is True
        assert scratch.keep_requested(SimpleNamespace()) is False


# --------------------------------------------------------------------------- #
# the CLI surface
# --------------------------------------------------------------------------- #

class TestCliFlags:
    def test_keep_scratch_and_keep_work_are_both_accepted(self):
        parser = cli.build_parser()
        args = parser.parse_args(["slides", "-Source", "x", "-KeepScratch"])
        assert args.keep_scratch is True and args.keep_work is False
        alias = parser.parse_args(["slides", "-Source", "x", "-KeepWork"])
        assert alias.keep_work is True and alias.keep_scratch is False

    def test_clean_scratch_parses_on_the_clean_verb(self):
        args = cli.build_parser().parse_args(["clean", "-CleanScratch", "-DryRun"])
        assert args.clean_scratch is True and args.dry_run is True

    def test_clean_scratch_defaults_off(self):
        assert cli.build_parser().parse_args(["clean"]).clean_scratch is False


class TestCleanScratchVerb:
    def _seed(self, root):
        work = root / "work"
        temp = root / "tmp"
        work.mkdir(parents=True, exist_ok=True)
        temp.mkdir(parents=True, exist_ok=True)
        owned = work / GUID
        owned.mkdir()
        foreign = work / "my-notes"
        foreign.mkdir()
        return owned, foreign

    def test_it_removes_only_toolchain_owned_scratch(self, root, capsys):
        owned, foreign = self._seed(root)
        code = cli.main(["clean", "-CleanScratch"])
        assert code == 0
        assert not owned.exists()
        assert foreign.exists()

    def test_it_reports_what_it_removed(self, root, capsys):
        owned, _foreign = self._seed(root)
        code = cli.main(["clean", "-CleanScratch"])
        assert code == 0
        payload = capsys.readouterr().out
        assert "removedCount" in payload
        assert "spared" in payload

    def test_dry_run_removes_nothing(self, root, capsys):
        owned, _foreign = self._seed(root)
        code = cli.main(["clean", "-CleanScratch", "-DryRun"])
        assert code == 0
        assert owned.exists(), "-DryRun must write nothing"
        import json

        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["dryRun"] is True
        assert payload["data"]["removed"] == [str(owned)]

    def test_the_legacy_default_is_unchanged(self, root, capsys):
        """Without -CleanScratch, clean still removes per-job dirs under the root."""
        owned, _foreign = self._seed(root)
        code = cli.main(["clean"])
        assert code == 0
        assert not owned.exists()


# --------------------------------------------------------------------------- #
# a run cleans up after itself (slides, driven with ffmpeg/OCR stubbed)
# --------------------------------------------------------------------------- #

class TestRunLeavesNoScratch:
    def _args(self, source: str, output: str, work_root: str, **overrides):
        values = {
            "source": source, "output": output, "image_dir": None, "times": "00:01",
            "times_file": None, "srt": None, "scale": 1280, "sample_rate": 0.25,
            "diff_threshold": 3.0, "hash_distance": 8, "min_slide_seconds": 2.0,
            # min_px 0: the fixture PNG is deliberately tiny, so the pixel floor
            # must not drop it -- this test is about scratch, not frame sizing.
            "sample_interval": 30.0, "min_px": 0, "min_frame_bytes": 0,
            "min_text_chars": 12, "no_text_gate": False, "lang": None,
            "dry_run": False, "force": True, "keep_work": False,
            "keep_scratch": False, "work_root": work_root, "attach_limit": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _patch(self, monkeypatch):
        class Env:
            ffprobe = None

            def require(self, *_a, **_k):
                return "ffmpeg"

        def fake_argv(_ffmpeg, _source, _seconds, output, _width):
            return ["ffmpeg", output]

        def fake_run_text(argv, **_kwargs):
            with open(argv[-1], "wb") as handle:
                handle.write(_png())
            return 0, ""

        monkeypatch.setattr(slides_cmd.env_mod, "resolve", lambda: Env())
        monkeypatch.setattr(slides_cmd.slides, "single_frame_argv", fake_argv)
        monkeypatch.setattr(slides_cmd.process, "run_text", fake_run_text)
        monkeypatch.setattr(slides_cmd.ocr, "ocr_image", lambda *_a, **_k: "text")

    def _run(self, tmp_path, root, monkeypatch, **overrides):
        self._patch(monkeypatch)
        source = tmp_path / "media.mp4"
        source.write_bytes(b"\x00" * 32)
        output = tmp_path / "item"
        output.mkdir()
        work_root = root / "work"
        outcome = slides_cmd.run(
            self._args(str(source), str(output), str(work_root), **overrides)
        )
        return outcome, work_root

    def test_a_default_run_removes_its_scratch(self, tmp_path, root, monkeypatch):
        outcome, work_root = self._run(tmp_path, root, monkeypatch)

        block = outcome.data["scratch"]
        assert block["kept"] is False
        assert block["removed"] is True
        assert block["leftover"] is False
        # The root may exist, but it holds no run dir of ours.
        if work_root.exists():
            assert [e.name for e in os.scandir(work_root)] == []

    def test_the_advertised_attach_paths_outlive_the_scratch(self, tmp_path, root, monkeypatch):
        """The F-crux: data.next.attach must not point into the deleted scratch."""
        outcome, _work_root = self._run(tmp_path, root, monkeypatch)

        attach = outcome.data["next"]["attach"]
        assert attach, "the run kept a frame, so there is something to attach"
        for entry in attach:
            assert paths.is_file(entry["path"]), entry["path"]
            # Every advertised path is in the caller's -Output item, not scratch.
            assert str(root) not in entry["path"]
        # And data.next.budget counted files that exist -- not deleted ones.
        assert outcome.data["next"]["budget"]["bytes"] > 0

    def test_keep_scratch_retains_the_dir(self, tmp_path, root, monkeypatch):
        outcome, work_root = self._run(
            tmp_path, root, monkeypatch, keep_scratch=True
        )

        block = outcome.data["scratch"]
        assert block["kept"] is True
        assert paths.is_dir(block["path"])
        assert os.path.dirname(paths.absolute(block["path"])) == paths.absolute(str(work_root))
        # And the directory still holds the run's own intermediates.
        assert [e.name for e in os.scandir(block["path"])] != []

    def test_the_retained_scratch_is_removable_by_the_verb(self, tmp_path, root, monkeypatch):
        outcome, _work_root = self._run(tmp_path, root, monkeypatch, keep_scratch=True)
        retained = outcome.data["scratch"]["path"]
        assert paths.is_dir(retained)

        code = cli.main(["clean", "-CleanScratch"])
        assert code == 0
        assert not paths.exists(retained)


class TestReadpdfDryRun:
    def test_readpdf_dry_run_removes_its_work_dir(self, tmp_path, root, monkeypatch):
        """readpdf creates its work dir BEFORE the dry-run return; §10 requires it
        to remove it too, or a planning call leaves litter under the root."""
        from zoombie.commands import readpdf

        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")
        helper = tmp_path / "extract_pdf.py"
        helper.write_text("# helper\n", encoding="utf-8")

        class Env:
            pdf_script = str(helper)

        monkeypatch.setattr(readpdf.env_mod, "resolve", lambda *_a, **_k: Env())
        args = argparse.Namespace(
            source=str(tmp_path / "doc.pdf"), output=str(tmp_path / "book"),
            ocr=False, images=False, images_only=False, image_dir=None,
            min_px=64, min_pt=30, pages=None, lang="eng",
            work_root=str(root / "work"), keep_work=False, keep_scratch=False,
            force=False, dry_run=True,
        )
        outcome = readpdf.run(args)

        assert outcome.data["dryRun"] is True
        assert outcome.data["scratch"]["removed"] is True
        assert outcome.data["scratch"]["leftover"] is False
        # The root itself may remain (it is the toolchain's, not a run's); what must
        # NOT remain is the run's own <guid> dir.
        work_root = root / "work"
        assert not work_root.exists() or [e.name for e in os.scandir(work_root)] == []


class TestUnpackScratch:
    def _install(self, monkeypatch, tmp_path):
        from zoombie.lib import unpack as unpack_mod

        extractor = tmp_path / "fake-7z.exe"
        extractor.write_bytes(b"7z")
        monkeypatch.setattr(unpack_mod, "extractor_path", lambda: str(extractor))

        def _fake_extract(source, exe, dest, **kwargs):
            os.makedirs(dest, exist_ok=True)
            with open(os.path.join(dest, "tesseract.exe"), "wb") as handle:
                handle.write(b"exe")
            return dest

        monkeypatch.setattr(unpack_mod, "extract", _fake_extract)

    def test_a_default_run_reports_a_removed_scratch(self, tmp_path, root, monkeypatch):
        self._install(monkeypatch, tmp_path)
        source = tmp_path / "setup.exe"
        source.write_bytes(b"installer")
        destination = tmp_path / "out"

        code = cli.main(["unpack", "-Source", str(source), "-Output", str(destination)])

        assert code == 0
        assert (destination / "tesseract.exe").exists()
        # No scratch survives under either root.
        for kind in ("work", "tmp"):
            directory = root / kind
            if directory.exists():
                assert [e.name for e in os.scandir(directory)] == []

    def test_keep_scratch_retains_the_extraction_scratch(self, tmp_path, root, monkeypatch, capsys):
        self._install(monkeypatch, tmp_path)
        source = tmp_path / "setup.exe"
        source.write_bytes(b"installer")
        destination = tmp_path / "out"

        code = cli.main([
            "unpack", "-Source", str(source), "-Output", str(destination), "-KeepScratch",
        ])

        assert code == 0
        import json

        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["scratch"]["kept"] is True
        assert paths.is_dir(payload["data"]["scratch"]["path"])


class TestDryRunLeavesNoScratch:
    def test_slides_dry_run_removes_the_empty_work_dir(self, tmp_path, root, monkeypatch):
        # '' -DryRun returns before any work dir is created in slides, so there is
        # nothing to remove there; this pins that no root litter results either way.
        source = tmp_path / "media.mp4"
        source.write_bytes(b"\x00" * 32)
        output = tmp_path / "item"
        output.mkdir()

        class Env:
            ffprobe = None

            def require(self, *_a, **_k):
                return "ffmpeg"

        monkeypatch.setattr(slides_cmd.env_mod, "resolve", lambda: Env())
        args = argparse.Namespace(
            source=str(source), output=str(output), image_dir=None, times=None,
            times_file=None, srt=None, scale=1280, sample_rate=0.25,
            diff_threshold=3.0, hash_distance=8, min_slide_seconds=2.0,
            sample_interval=30.0, min_px=64, min_frame_bytes=0, min_text_chars=12,
            no_text_gate=False, lang=None, dry_run=True, force=False,
            keep_work=False, keep_scratch=False, work_root=str(root / "work"),
            attach_limit=None,
        )
        outcome = slides_cmd.run(args)

        assert outcome.data["dryRun"] is True
        assert not (output / ".data").exists()
        assert not (root / "work").exists()

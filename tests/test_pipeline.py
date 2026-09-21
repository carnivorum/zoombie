"""Tests for ``pipeline``'s source-retention step.

``_retain_source`` reverses the old default: the media is kept at the item root
beside ``summary.md`` instead of being deleted after transcription. It is a pure
function of ``(video_path, output_base)`` -- it previously needed a real network
download to exercise, which is why it was only ever inspected -- so it is tested
directly here, with its three outcomes pinned: copied, absent, and over-budget.

The over-budget case is the one that must NOT fail a run whose transcript is
already written, so the copy is best effort and the outcome is recorded in the
``sourceKept`` / ``sourceFile`` fields a reader relies on.
"""

from __future__ import annotations

import argparse

from zoombie.commands import pipeline
from zoombie.lib import paths, stt


def _args(tmp_path, source, output):
    """A Namespace with pipeline's defaults, like the parser would produce."""
    return argparse.Namespace(
        source=str(source), output=str(output), model=None, dry_run=False,
        work_root=str(tmp_path / "work"), keep_work=False, force=True,
        download_dir=None, language="auto", srt=False, no_srt=False,
        no_gpu=True, no_flash_attn=False, threads=0,
        allow_cpu_fallback=False, strict_gpu=False,
    )


class _Env:
    """Stand-in for ``env.Env``: only ``require`` is reached by ``run``."""

    model = "model.bin"

    def require(self, key, name):
        return name


class TestRetainSource:
    def test_it_copies_the_media_beside_the_transcript(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        source = downloads / "video.mp4"
        source.write_bytes(b"media-payload")
        base = str(tmp_path / "item" / "transcript")

        kept = pipeline._retain_source(str(source), base)
        assert kept == str(tmp_path / "item" / "video.mp4")
        assert (tmp_path / "item" / "video.mp4").read_bytes() == b"media-payload"
        # A copy, not a move: the caller still cleans the download dir up.
        assert source.is_file()

    def test_missing_media_returns_none_not_an_exception(self, tmp_path):
        assert pipeline._retain_source(
            str(tmp_path / "nope.mp4"), str(tmp_path / "item" / "transcript")
        ) is None

    def test_an_over_budget_path_warns_and_returns_none(self, tmp_path, monkeypatch, capsys):
        """The path budget must degrade to a warning, never fail the run.

        A media file name is content-controlled, so a long title can push the
        destination past what Windows accepts. The transcript is already written
        at that point, so this is a skip with a reason -- and the reason has to be
        logged, because the absence is otherwise silent.
        """
        source = tmp_path / "video.mp4"
        source.write_bytes(b"x")

        def boom(*_args, **_kwargs):
            raise paths.PathTooDeepError("destination is too long for Windows")

        monkeypatch.setattr(pipeline.paths, "assert_fits", boom)
        assert pipeline._retain_source(str(source), str(tmp_path / "item" / "t")) is None
        assert "source not retained" in capsys.readouterr().err


class TestRunRecordsWhatHappened:
    def _run(self, tmp_path, monkeypatch, source):
        seen: dict = {}

        def fake_transcribe(environment, request):
            seen["request"] = request
            return stt.Report(output_base=request.output_base)

        monkeypatch.setattr(pipeline.env_mod, "resolve", lambda *_a, **_k: _Env())
        monkeypatch.setattr(pipeline.process, "run_checked", lambda *_a, **_k: None)
        monkeypatch.setattr(pipeline.stt, "transcribe", fake_transcribe)

        output = tmp_path / "item" / "transcript"
        outcome = pipeline.run(_args(tmp_path, source, output))
        return outcome, seen["request"]

    def test_source_kept_is_true_when_the_copy_succeeded(self, tmp_path, monkeypatch):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        source = downloads / "video.mp4"
        source.write_bytes(b"media")

        outcome, request = self._run(tmp_path, monkeypatch, source)
        assert outcome.ok is True
        assert request.source_kept is True
        assert request.source_file == "video.mp4"
        assert (tmp_path / "item" / "video.mp4").is_file()

    def test_source_kept_is_false_when_the_copy_did_not(self, tmp_path, monkeypatch):
        """The field records what HAPPENED, not what was requested."""
        outcome, request = self._run(tmp_path, monkeypatch, tmp_path / "gone.mp4")
        assert outcome.ok is True
        assert request.source_kept is False
        assert request.source_file is None


class TestSidecarPlumbing:
    def test_the_retained_name_reaches_the_sidecar_as_source_file(self):
        request = stt.Request(
            audio_path=r"C:\media\input.wav",
            output_base=r"C:\media\transcript",
            source_kept=True,
            source_file="video.mp4",
        )
        payload = stt.source_metadata(request, stt.Report(output_base=request.output_base))
        assert payload["sourceKept"] is True
        assert payload["sourceFile"] == "video.mp4"

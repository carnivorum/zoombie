"""Tests for the transcription stage's source-producer contract.

The stage must emit the ``.srt`` (the only timing source, because ``-nt`` strips
timestamps from the ``.txt``) plus a ``<base>.source.json`` origin sidecar -- and
it must refuse to overwrite an existing transcript without ``-Force``.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from zoombie import cli
from zoombie.commands import transcribe as transcribe_cmd
from zoombie.lib import stt, whisper, ytdlp
from zoombie.lib.errors import ZoombieError


def _args(**overrides):
    """A Namespace with transcribe's defaults, like the parser would produce."""
    base = dict(
        source="",
        output=None,
        model=None,
        language="auto",
        srt=False,
        no_srt=False,
        force=False,
        no_gpu=True,
        no_flash_attn=False,
        threads=0,
        allow_cpu_fallback=False,
        strict_gpu=False,
        work_root=None,
        keep_work=False,
        dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _request(**overrides) -> stt.Request:
    base = dict(
        audio_path=r"C:\media\input.wav",
        output_base=r"C:\media\transcript",
        source_url="https://example.com/watch?v=abc123",
        source_title="A Talk",
        source_id="abc123",
        source_kind="video",
        source_kept=False,
    )
    base.update(overrides)
    return stt.Request(**base)


def _report() -> stt.Report:
    return stt.Report(
        output_base=r"C:\media\transcript",
        backend="cuda",
        model=r"C:\zoombie-env\models\ggml-large-v3-turbo.bin",
        device_used="cuda",
        device_selected=True,
        audio_duration_sec=600.0,
        realtime_factor=0.25,
    )


class TestBuildArgs:
    def test_srt_is_emitted_by_default(self):
        argv, _ = stt.build_args(
            "model.bin", "in.wav", "out", "auto", whisper.Capabilities(),
            want_srt=False, no_gpu=True, no_flash_attn=True, threads=None,
        )
        assert "-osrt" in argv
        # The txt flags are unconditional: the SRT ADDS to them, never replaces.
        assert "-otxt" in argv
        assert "-nt" in argv

    def test_no_srt_omits_the_srt_flag(self):
        argv, _ = stt.build_args(
            "model.bin", "in.wav", "out", "auto", whisper.Capabilities(),
            want_srt=False, no_gpu=True, no_flash_attn=True, threads=None,
            no_srt=True,
        )
        assert "-osrt" not in argv
        assert "-otxt" in argv
        assert "-nt" in argv

    def test_want_srt_is_only_a_no_op_alias(self):
        """-Srt can never REMOVE the default SRT, so both values match."""
        on, _ = stt.build_args(
            "model.bin", "in.wav", "out", "auto", whisper.Capabilities(),
            want_srt=True, no_gpu=True, no_flash_attn=True, threads=None,
        )
        off, _ = stt.build_args(
            "model.bin", "in.wav", "out", "auto", whisper.Capabilities(),
            want_srt=False, no_gpu=True, no_flash_attn=True, threads=None,
        )
        assert on == off
        assert "-osrt" in on


class TestSidecar:
    def test_writes_the_documented_keys(self, tmp_path):
        base = str(tmp_path / "transcript")
        artifact = stt.write_source_sidecar(
            base, _request(), _report(), extension_count=2
        )
        assert artifact is not None
        path = artifact["path"]
        assert os.path.basename(path) == "transcript.source.json"
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert set(payload) == {
            "kind", "url", "title", "id", "durationSec", "language", "model",
            "backend", "deviceUsed", "deviceVerified", "realtimeFactor",
            "toolchainVersion", "createdAt", "sourceKept", "sourceFile",
        }
        assert payload["url"] == "https://example.com/watch?v=abc123"
        assert payload["title"] == "A Talk"
        assert payload["id"] == "abc123"
        assert payload["kind"] == "video"
        assert payload["durationSec"] == 600.0
        assert payload["backend"] == "cuda"
        assert payload["deviceUsed"] == "cuda"
        assert payload["deviceVerified"] is True
        assert payload["realtimeFactor"] == 0.25
        # This is the field that records whether the media survived the run. It is
        # False here because the request did not retain it; the pipeline now keeps
        # the source beside the transcript and sets both fields together.
        assert payload["sourceKept"] is False
        assert payload["sourceFile"] is None
        assert payload["createdAt"].endswith("Z")

    def test_local_audio_falls_back_to_the_input_path(self, tmp_path):
        payload = stt.source_metadata(
            _request(source_url=None, source_kind="audio",
                     audio_path=r"C:\media\talk.mp3"),
            _report(),
        )
        assert payload["url"] == r"C:\media\talk.mp3"
        assert payload["kind"] == "audio"

    def test_write_failure_is_not_fatal(self, tmp_path, monkeypatch):
        """A sidecar that cannot be written must not fail the transcription.

        The 260-character budget is the realistic failure: the sidecar has the
        longest suffix, so a base that fits the txt can still lose the sidecar.
        The contract is that the run WARNS and returns None, never raises.
        """
        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", boom)
        artifact = stt.write_source_sidecar(
            str(tmp_path / "transcript"), _request(), _report(), extension_count=2
        )
        assert artifact is None

    def test_reported_as_artifacts_sidecar(self, tmp_path):
        """The sidecar is named in the report's artifacts object."""
        base = str(tmp_path / "transcript")
        report = stt.Report(output_base=base)
        report.artifacts["sidecar"] = stt.write_source_sidecar(
            base, _request(), report, extension_count=2
        )
        data = report.to_data()
        assert data["artifacts"]["sidecar"]["path"].endswith(".source.json")

    def test_the_sidecar_is_written_after_the_timings_are_known(self, tmp_path, monkeypatch):
        """Regression: the sidecar was written BEFORE the timing block.

        ``transcribe`` computed ``audio_duration_sec`` and ``realtime_factor``
        *after* the sidecar was already on disk, so ``source_metadata`` recorded
        ``durationSec`` and ``realtimeFactor`` as null -- the exact false negative
        the origin sidecar exists to prevent. This drives the real ``transcribe``
        with only whisper stubbed, so the WRITE ORDER is what is exercised.
        """
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFF")
        model = tmp_path / "ggml-small.bin"
        model.write_bytes(b"m")
        exe = tmp_path / "whisper-cli.exe"
        exe.write_bytes(b"x")
        base = str(tmp_path / "transcript")

        class _Env:
            backend = "cpu"
            ffprobe = None
            gpu_backend_configured = False

            def __init__(self):
                self.model = str(model)

            def require(self, key, name):
                return str(exe)

        # Stub only the whisper SURFACE transcribe calls, never the orchestration.
        monkeypatch.setattr(stt.whisper, "capabilities", lambda _e: whisper.Capabilities())
        monkeypatch.setattr(
            stt.whisper, "probe_backend",
            lambda _e: whisper.BackendProbe(device="cpu"),
        )
        monkeypatch.setattr(stt.whisper, "run_whisper", lambda *_a, **_k: whisper.RunResult())
        monkeypatch.setattr(stt.whisper, "read_log", lambda _p: ["whisper_init_from_file_with_params_no_state: loading model"])
        monkeypatch.setattr(
            stt.whisper, "device_info",
            lambda _l: whisper.DeviceInfo(device="cpu", device_selected=True),
        )
        monkeypatch.setattr(
            stt.whisper, "timings", lambda _l: whisper.Timings(total_ms=1000.0)
        )
        monkeypatch.setattr(stt, "audio_duration_seconds", lambda *_a, **_k: 5.0)

        request = stt.Request(
            audio_path=str(source), output_base=base, no_gpu=True, force=True,
            work_root=str(tmp_path / "work"),
        )
        report = stt.transcribe(_Env(), request)

        with open(f"{base}.source.json", encoding="utf-8") as handle:
            payload = json.load(handle)
        # 5.0s of audio decoded in 1000ms => a realtime factor of 0.2.
        assert payload["durationSec"] == 5.0
        assert payload["realtimeFactor"] == 0.2
        assert report.artifacts["sidecar"] is not None


class TestReportShape:
    def test_to_data_names_the_model_actually_used(self):
        """A default downgrade must be visible, not silent."""
        report = stt.Report(
            output_base="x", model=r"C:\zoombie-env\models\ggml-small.bin"
        )
        assert report.to_data()["model"] == "ggml-small.bin"

    def test_a_missing_model_is_none(self):
        assert stt.Report(output_base="x").to_data()["model"] is None

    def test_item_dir_is_echoed_and_absent_is_null(self):
        report = stt.Report(output_base="x", item_dir=r"C:\ws\item")
        assert report.to_data()["itemDir"] == r"C:\ws\item"
        assert stt.Report(output_base="x").to_data()["itemDir"] is None


class TestItemLayout:
    """``-Output`` names the item folder; the artifacts always go to ``.data/``.

    This is the fix for the split the field reported: the transcribe/pipeline
    skills documented ``<item>/.data/transcript.*`` while the producers wrote
    flat beside ``summary.md``. The layout is unconditional -- there is no flat
    mode left -- so a transcript can never again land at the item root.
    """

    def test_output_names_the_item_folder_and_artifacts_go_to_data(self, tmp_path):
        base = stt.resolve_output_base(str(tmp_path / "audio.wav"), str(tmp_path / "item"))
        assert base == str(tmp_path / "item" / ".data" / "transcript")

    def test_omitting_output_uses_the_source_folder_as_the_item(self, tmp_path):
        source = tmp_path / "audio.wav"
        assert stt.item_dir_for(None, str(source)) == str(tmp_path)
        assert stt.resolve_output_base(str(source), None) == str(
            tmp_path / ".data" / "transcript"
        )

    def test_a_trailing_separator_names_the_same_item(self, tmp_path):
        """``C:\\ws\\item`` and ``C:\\ws\\item\\`` must resolve to one folder."""
        assert stt.item_dir_for(str(tmp_path / "item"), "x") == stt.item_dir_for(
            str(tmp_path / "item") + os.sep, "x"
        )

    def test_the_base_the_guard_checks_is_the_base_written(self, tmp_path):
        """Guard and writer derive from one helper, so they cannot disagree."""
        base = stt.resolve_output_base(str(tmp_path / "audio.wav"), str(tmp_path / "item"))
        assert base == stt.transcript_base(str(tmp_path / "item"))


class TestOverwriteGuard:
    def test_refuses_an_existing_transcript(self, tmp_path):
        base = str(tmp_path / "transcript")
        (tmp_path / "transcript.txt").write_text("old\n", encoding="utf-8")
        with pytest.raises(ZoombieError) as excinfo:
            stt.guard_overwrite(base, force=False)
        assert "use -Force" in str(excinfo.value)
        assert "transcript.txt" in str(excinfo.value)

    def test_force_proceeds(self, tmp_path):
        base = str(tmp_path / "transcript")
        (tmp_path / "transcript.txt").write_text("old\n", encoding="utf-8")
        stt.guard_overwrite(base, force=True)

    def test_no_existing_file_is_fine(self, tmp_path):
        stt.guard_overwrite(str(tmp_path / "fresh"), force=False)


class TestTranscribeCommand:
    def test_guard_refuses_before_any_work(self, tmp_path, monkeypatch):
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFF")
        # The guard now checks the ITEM layout: -Output names the folder, so an
        # existing transcript lives at <item>/.data/transcript.txt.
        (tmp_path / ".data").mkdir()
        (tmp_path / ".data" / "transcript.txt").write_text("old\n", encoding="utf-8")

        monkeypatch.setattr(
            transcribe_cmd.env_mod, "resolve", lambda *_a, **_k: object()
        )
        monkeypatch.setattr(
            transcribe_cmd.stt, "transcribe",
            lambda *_a, **_k: pytest.fail("transcribe must not run"),
        )
        with pytest.raises(ZoombieError, match="use -Force"):
            transcribe_cmd.run(
                _args(source=str(source), output=str(tmp_path), force=False)
            )

    def test_the_request_carries_the_item_folder(self, tmp_path, monkeypatch):
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFF")
        seen: dict = {}

        def fake_transcribe(environment, request):
            seen["request"] = request
            return _report()

        monkeypatch.setattr(
            transcribe_cmd.env_mod, "resolve", lambda *_a, **_k: object()
        )
        monkeypatch.setattr(transcribe_cmd.stt, "transcribe", fake_transcribe)

        item = tmp_path / "item"
        transcribe_cmd.run(_args(source=str(source), output=str(item)))
        assert seen["request"].item_dir == str(item)
        assert os.path.dirname(seen["request"].output_base) == str(item / ".data")

    def test_force_reaches_the_request(self, tmp_path, monkeypatch):
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFF")
        seen: dict = {}

        def fake_transcribe(environment, request):
            seen["request"] = request
            return _report()

        monkeypatch.setattr(
            transcribe_cmd.env_mod, "resolve", lambda *_a, **_k: object()
        )
        monkeypatch.setattr(transcribe_cmd.stt, "transcribe", fake_transcribe)

        outcome = transcribe_cmd.run(
            _args(source=str(source), output=str(tmp_path / "out"),
                  force=True, no_srt=True)
        )
        assert outcome.ok is True
        assert seen["request"].force is True
        assert seen["request"].no_srt is True

    def test_no_srt_flag_is_real_and_srt_is_a_no_op(self):
        parser = cli.build_parser()
        off = parser.parse_args(["transcribe", "-Source", "a.wav", "-NoSrt"])
        assert off.no_srt is True
        # The legacy flag still PARSES (compatibility) but no longer governs.
        legacy = parser.parse_args(["transcribe", "-Source", "a.wav", "-Srt"])
        assert legacy.srt is True
        assert legacy.no_srt is False

    def test_pipeline_has_no_format_flag(self):
        """The pipeline's audio is fixed at 16 kHz mono WAV, so -Format is gone."""
        parser = cli.build_parser()
        args = parser.parse_args(["pipeline", "-Source", "https://x/y"])
        assert not hasattr(args, "format")
        assert args.no_srt is False
        with pytest.raises(SystemExit):
            parser.parse_args(["pipeline", "-Source", "https://x/y", "-Format", "mp3"])


class TestNewestDownload:
    def test_ignores_a_newer_subtitle_or_info_file(self, tmp_path, monkeypatch):
        media = tmp_path / "video.mp4"
        media.write_bytes(b"media")
        subtitle = tmp_path / "video.en.srt"
        subtitle.write_text("1\n", encoding="utf-8")
        info = tmp_path / "video.info.json"
        info.write_text("{}", encoding="utf-8")
        vtt = tmp_path / "video.en.vtt"
        vtt.write_text("WEBVTT\n", encoding="utf-8")

        # Make every sidecar file strictly NEWER than the media.
        import time

        base_time = time.time()
        os.utime(str(media), (base_time, base_time))
        for stray in (subtitle, info, vtt):
            os.utime(str(stray), (base_time + 60, base_time + 60))

        produced = ytdlp.newest_download(str(tmp_path))
        assert produced is not None
        assert produced["file"] == str(media)

    def test_picks_the_newest_media_when_several_exist(self, tmp_path):
        import time

        older = tmp_path / "a.mkv"
        older.write_bytes(b"a")
        newer = tmp_path / "b.webm"
        newer.write_bytes(b"b")
        now = time.time()
        os.utime(str(older), (now, now))
        os.utime(str(newer), (now + 60, now + 60))
        produced = ytdlp.newest_download(str(tmp_path))
        assert produced["file"] == str(newer)

    def test_only_sidecars_means_no_media(self, tmp_path):
        (tmp_path / "video.en.srt").write_text("1\n", encoding="utf-8")
        assert ytdlp.newest_download(str(tmp_path)) is None

    def test_empty_directory(self, tmp_path):
        assert ytdlp.newest_download(str(tmp_path)) is None


class TestMetadata:
    def test_parse_metadata_reads_the_printed_line(self):
        text = (
            "[download] Destination: video.mp4\n"
            "abc123\tA Talk\thttps://example.com/watch?v=abc123\t612.5\n"
        )
        origin = ytdlp.parse_metadata(text)
        assert origin is not None
        assert origin.id == "abc123"
        assert origin.title == "A Talk"
        assert origin.url == "https://example.com/watch?v=abc123"
        assert origin.duration_sec == 612.5

    def test_unparsable_output_is_none(self):
        assert ytdlp.parse_metadata("[download] 100% of 10MiB\n") is None
        assert ytdlp.parse_metadata("") is None

    def test_metadata_args_print_after_move(self):
        args = ytdlp.metadata_args()
        assert "--print" in args
        assert any("after_move:" in value for value in args)
        # It must not WRITE an extra file: -o/-o only produce the media.
        assert "--write-info-json" not in args

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
from zoombie.lib.errors import StepFailedError, ZoombieError


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
        from_time=None,
        to_time=None,
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
        artifact = stt.write_source_sidecar(base, _request(), _report())
        assert artifact is not None
        path = artifact["path"]
        # The sidecar is RUN-LOCAL: a plain source.json beside the transcript, not
        # a <base>.source.json, so it is deleted with the run scratch.
        assert os.path.basename(path) == "source.json"
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert set(payload) == {
            "kind", "url", "urlReason", "title", "id", "durationSec", "language",
            "model", "backend", "deviceUsed", "deviceVerified", "realtimeFactor",
            "toolchainVersion", "createdAt", "sourceKept", "sourceFile",
            "sourceReason",
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
        # No deliberate non-copy was recorded for this request, so the reason is
        # null (it is only set when the absence was a DECISION, e.g. a local
        # -Source the pipeline refuses to duplicate).
        assert payload["sourceReason"] is None
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
            str(tmp_path / "transcript"), _request(), _report()
        )
        assert artifact is None

    def test_reported_as_artifacts_sidecar(self, tmp_path):
        """The sidecar is named in the report's artifacts object."""
        base = str(tmp_path / "transcript")
        report = stt.Report(output_base=base)
        report.artifacts["sidecar"] = stt.write_source_sidecar(base, _request(), report)
        data = report.to_data()
        assert data["artifacts"]["sidecar"]["path"].endswith("source.json")

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
        def fake_run(_exe, argv, _stdout, _stderr):
            # A run with NO .txt is now a hard failure, so this test must produce
            # the deliverable it asserts the sidecar was written against.
            out_base = argv[argv.index("-of") + 1]
            with open(f"{out_base}.txt", "w", encoding="utf-8") as handle:
                handle.write("hello\n")
            return whisper.RunResult()

        monkeypatch.setattr(stt.whisper, "run_whisper", fake_run)
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

        with open(os.path.join(os.path.dirname(base), "source.json"), encoding="utf-8") as handle:
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
    """``-Output`` names the destination folder; the artifacts land directly in it.

    With the ``.data/`` sidecar directory gone, a transcription's artifacts are
    RUN-LOCAL: the summarize flow points ``-Output`` at a run scratch dir, and a
    bare ``transcribe`` writes into whatever folder the caller names (or the
    source's own folder, or ``_unsorted/summaries/<name>`` when it is external).
    """

    def test_output_names_the_folder_and_artifacts_go_there(self, tmp_path):
        base = stt.resolve_output_base(str(tmp_path / "audio.wav"), str(tmp_path / "item"))
        assert base == str(tmp_path / "item" / "transcript")

    def test_omitting_output_uses_the_source_folder_when_it_is_in_the_workspace(
        self, tmp_path, monkeypatch
    ):
        """A source INSIDE the workspace keeps its own folder (routing rule)."""
        monkeypatch.chdir(tmp_path)
        source = tmp_path / "audio.wav"
        assert stt.item_dir_for(None, str(source)) == str(tmp_path)
        assert stt.resolve_output_base(str(source), None) == str(tmp_path / "transcript")

    def test_a_trailing_separator_names_the_same_item(self, tmp_path):
        """``C:\\ws\\item`` and ``C:\\ws\\item\\`` must resolve to one folder."""
        assert stt.item_dir_for(str(tmp_path / "item"), "x") == stt.item_dir_for(
            str(tmp_path / "item") + os.sep, "x"
        )

    def test_the_base_the_guard_checks_is_the_base_written(self, tmp_path):
        """Guard and writer derive from one helper, so they cannot disagree."""
        base = stt.resolve_output_base(str(tmp_path / "audio.wav"), str(tmp_path / "item"))
        assert base == stt.transcript_base(str(tmp_path / "item"))

    def test_both_item_dir_and_output_base_write_into_the_folder(self, tmp_path, monkeypatch):
        """Both fields set: the artifacts go into the destination folder.

        Regression for Defect 1. ``pipeline`` and ``transcribe`` pass a correctly
        resolved ``output_base`` AND a non-empty ``item_dir``; the old precedence
        ``item_dir or output_base`` let ``item_dir`` win and wrote every artifact
        FLAT off the item folder. This drives the real ``transcribe`` with only the
        whisper surface stubbed, so the WRITE PATH executes -- the previous tests
        monkeypatched ``stt.transcribe`` away and never reached it.
        """
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFF")
        model = tmp_path / "ggml-small.bin"
        model.write_bytes(b"m")
        exe = tmp_path / "whisper-cli.exe"
        exe.write_bytes(b"x")
        item = tmp_path / "item"

        class _Env:
            backend = "cpu"
            ffprobe = None
            gpu_backend_configured = False

            def __init__(self):
                self.model = str(model)

            def require(self, key, name):
                return str(exe)

        monkeypatch.setattr(stt.whisper, "capabilities", lambda _e: whisper.Capabilities())
        monkeypatch.setattr(
            stt.whisper, "probe_backend", lambda _e: whisper.BackendProbe(device="cpu")
        )

        def fake_run(_exe, argv, _stdout, _stderr):
            # Emulate whisper writing ``<out_base>.txt`` beside the ``-of`` path.
            out_base = argv[argv.index("-of") + 1]
            with open(f"{out_base}.txt", "w", encoding="utf-8") as handle:
                handle.write("hello\n")
            return whisper.RunResult()

        monkeypatch.setattr(stt.whisper, "run_whisper", fake_run)
        monkeypatch.setattr(stt.whisper, "read_log", lambda _p: [])
        monkeypatch.setattr(
            stt.whisper, "device_info", lambda _l: whisper.DeviceInfo(device="cpu")
        )
        monkeypatch.setattr(stt.whisper, "timings", lambda _l: whisper.Timings())

        base = stt.resolve_output_base(str(source), str(item))
        request = stt.Request(
            audio_path=str(source), output_base=base, item_dir=str(item),
            no_gpu=True, no_srt=True, force=True, work_root=str(tmp_path / "work"),
        )
        report = stt.transcribe(_Env(), request)

        written = tmp_path / "item" / "transcript.txt"
        assert written.is_file(), "the artifact must land in the item folder"
        assert report.output_base == str(item / "transcript")

    def test_the_reported_output_base_sits_in_the_item_folder(self, tmp_path):
        """``Report.to_data()`` echoes both ``outputBase`` and ``itemDir``."""
        item = r"C:\ws\My Item"
        base = stt.resolve_output_base(r"C:\media\audio.wav", item)
        request = stt.Request(audio_path=r"C:\media\audio.wav", output_base=base,
                              item_dir=item)
        report = stt.Report(
            output_base=stt.effective_output_base(request.output_base, request.item_dir),
            item_dir=request.item_dir,
        )
        data = report.to_data()
        assert data["itemDir"] == item
        assert data["outputBase"] == os.path.join(item, "transcript")


class TestEffectiveOutputBase:
    def test_output_base_wins(self):
        assert stt.effective_output_base(r"C:\i\.data\transcript", r"C:\i") == \
            r"C:\i\.data\transcript"

    def test_item_dir_is_the_fallback(self):
        assert stt.effective_output_base("", r"C:\i") == r"C:\i"

    def test_pipeline_and_writer_agree(self, tmp_path):
        """The guard's base and the writer's base are the same expression."""
        item = str(tmp_path / "item")
        base = stt.resolve_output_base(str(tmp_path / "audio.wav"), item)
        assert stt.effective_output_base(base, item) == stt.effective_output_base(base, item)


class TestEnsureItemDir:
    def test_output_named_folder_is_created(self, tmp_path):
        item = tmp_path / "item"
        assert stt.ensure_item_dir(str(item), str(item)) is True
        assert item.is_dir()

    def test_no_output_creates_nothing(self, tmp_path):
        """A bare transcribe must not create a folder the caller did not name."""
        item = tmp_path / "loose"
        assert stt.ensure_item_dir(None, str(item)) is False
        assert not item.exists()

    def test_creating_the_folder_does_not_make_it_an_item(self, tmp_path):
        """Only a summary.md makes an item now -- an empty folder is not one."""
        from zoombie.item import paths as item_paths

        item = tmp_path / "item"
        stt.ensure_item_dir(str(item), str(item))
        assert not item_paths.is_item(str(item))


class TestScratchAudioHonesty:
    """A dead scratch path must not be recorded as the sidecar's origin."""

    def test_scratch_audio_url_is_null_with_a_reason(self, tmp_path):
        work = tmp_path / "work" / "abc"
        work.mkdir(parents=True)
        scratch = work / "audio.wav"
        payload = stt.source_metadata(
            _request(source_url=None, audio_path=str(scratch), work_root=str(tmp_path / "work")),
            _report(),
        )
        assert payload["url"] is None
        assert payload["urlReason"] and "scratch" in payload["urlReason"]

    def test_a_real_local_audio_file_is_still_recorded(self, tmp_path):
        real = tmp_path / "talk.mp3"
        payload = stt.source_metadata(
            _request(source_url=None, audio_path=str(real)), _report()
        )
        assert payload["url"] == str(real)
        assert payload["urlReason"] is None

    def test_an_explicit_scratch_flag_reasons_even_outside_the_work_root(self, tmp_path):
        scratch = tmp_path / "elsewhere" / "audio.wav"
        payload = stt.source_metadata(
            _request(source_url=None, audio_path=str(scratch), audio_is_scratch=True),
            _report(),
        )
        assert payload["url"] is None
        assert payload["urlReason"]


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
        # -Output names the folder, so an existing transcript lives at
        # <folder>/transcript.txt.
        (tmp_path / "transcript.txt").write_text("old\n", encoding="utf-8")

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

    def test_a_window_is_not_refused_by_the_full_transcript(self, tmp_path, monkeypatch):
        """The guard checks the WINDOW-qualified base, not the full transcript."""
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFF")
        (tmp_path / "transcript.txt").write_text("old\n", encoding="utf-8")

        seen: dict = {}

        def fake_transcribe(environment, request):
            seen["request"] = request
            return _report()

        monkeypatch.setattr(
            transcribe_cmd.env_mod, "resolve", lambda *_a, **_k: object()
        )
        monkeypatch.setattr(transcribe_cmd.stt, "transcribe", fake_transcribe)

        # No -Force, and the FULL transcript exists: the window is a different
        # file, so this must run rather than be refused.
        outcome = transcribe_cmd.run(_args(
            source=str(source), output=str(tmp_path),
            from_time="00:47:00", to_time="00:55:00",
        ))
        assert outcome.ok is True
        assert seen["request"].from_time == "00:47:00"

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
        assert seen["request"].output_base == str(item / "transcript")

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


class TestVideoInputRefusal:
    """Defect 7: ``transcribe`` must refuse a video up front, naming ``pipeline``.

    whisper.cpp reads WAV and little else, so a video used to fail DEEP inside
    whisper -- exit 0, no transcript, a success report. The misuse is documented,
    so the tool says so instead of failing opaquely.
    """

    def test_a_known_video_container_is_refused_with_the_remedy_named(self, tmp_path):
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"not-really-mp4")
        with pytest.raises(ZoombieError) as excinfo:
            stt.transcribe(_env(tmp_path), stt.Request(
                audio_path=str(source), output_base=str(tmp_path / "transcript"),
                no_gpu=True, force=True,
            ))
        message = str(excinfo.value)
        assert "pipeline" in message, "the refusal must name the command that converts"
        assert ".mp4" in message

    @pytest.mark.parametrize(
        "name", ["a.mp4", "a.mkv", "a.webm", "a.mov", "a.avi", "a.m4v", "A.MP4"]
    )
    def test_every_video_container_is_refused(self, name):
        with pytest.raises(ZoombieError, match="pipeline"):
            stt.refuse_video_input(name)

    @pytest.mark.parametrize(
        "name", ["a.wav", "a.mp3", "a.m4a", "a.flac", "a.ogg", "a.opus", "a.aac"]
    )
    def test_audio_containers_are_not_refused(self, name):
        # whisper MIGHT read these; the missing-.txt post-check is their backstop,
        # so the up-front refusal must not reject them.
        stt.refuse_video_input(name)


class TestMissingTranscriptFailure:
    """Defect 7: an exit-0 run with no ``.txt`` must fail, not report success.

    The old run reported ``ok:true`` with no ``txt``, ``srt:null`` and a sidecar
    written anyway -- the contradiction signature. These drive the REAL
    ``transcribe`` with only the whisper surface stubbed, so the post-artifact
    check actually executes.
    """

    def _base_and_request(self, tmp_path, source):
        base = str(tmp_path / "transcript")
        request = stt.Request(
            audio_path=str(source), output_base=base, no_gpu=True, force=True,
            work_root=str(tmp_path / "work"),
        )
        return base, request

    def test_a_missing_txt_fails_and_names_the_audio_read_error(self, tmp_path, monkeypatch):
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFF")

        def fake_run(_exe, _argv, _stdout, _stderr):
            # Exit 0 and write NO artifact -- exactly the failing-run shape.
            return whisper.RunResult(exit_code=0)

        monkeypatch.setattr(stt.whisper, "run_whisper", fake_run)
        monkeypatch.setattr(stt.whisper, "read_log", lambda _p: [
            "whisper_backend_init_gpu: using CUDA0 backend",
            "read_audio_data: failed to read audio data",
            "error: failed to read audio file 'C:\\work\\input.wav'",
        ])
        monkeypatch.setattr(stt.whisper, "audio_read_failure",
                            lambda lines: [ln for ln in lines if "failed to read" in ln])
        monkeypatch.setattr(stt.whisper, "device_info",
                            lambda _l: whisper.DeviceInfo(device="cpu"))
        monkeypatch.setattr(stt.whisper, "timings", lambda _l: whisper.Timings())

        base, request = self._base_and_request(tmp_path, source)
        with pytest.raises(StepFailedError) as excinfo:
            stt.transcribe(_env(tmp_path), request)
        message = str(excinfo.value)
        assert "no transcript" in message
        assert "failed to read audio data" in message
        # NO sidecar is written on a failed run: a sidecar describing a transcript
        # that does not exist is the contradiction this fix removes.
        assert not os.path.isfile(os.path.join(os.path.dirname(base), "source.json"))
        assert not os.path.isfile(f"{base}.txt")

    def test_srt_null_with_a_live_sidecar_is_impossible(self, tmp_path, monkeypatch):
        """The contradiction signature: ``srt: null`` must never sit beside a sidecar.

        On the failing run the report carried ``artifacts.srt == null`` AND a
        non-null sidecar. Making the sidecar unreachable on a no-.txt run removes
        the possibility outright.
        """
        source = tmp_path / "input.wav"
        source.write_bytes(b"RIFF")
        monkeypatch.setattr(stt.whisper, "run_whisper",
                            lambda *_a, **_k: whisper.RunResult(exit_code=0))
        monkeypatch.setattr(stt.whisper, "read_log", lambda _p: [])
        monkeypatch.setattr(stt.whisper, "device_info",
                            lambda _l: whisper.DeviceInfo(device="cpu"))
        monkeypatch.setattr(stt.whisper, "timings", lambda _l: whisper.Timings())

        base, request = self._base_and_request(tmp_path, source)
        with pytest.raises(StepFailedError):
            stt.transcribe(_env(tmp_path), request)
        assert not os.path.isfile(os.path.join(os.path.dirname(base), "source.json"))

    def test_pipeline_with_a_wav_is_unaffected_by_the_refusal(self, tmp_path, monkeypatch):
        """``pipeline`` extracts a WAV, so the video refusal must not fire for it."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF")

        def fake_run(_exe, argv, _stdout, _stderr):
            out_base = argv[argv.index("-of") + 1]
            with open(f"{out_base}.txt", "w", encoding="utf-8") as handle:
                handle.write("hello\n")
            return whisper.RunResult()

        monkeypatch.setattr(stt.whisper, "run_whisper", fake_run)
        monkeypatch.setattr(stt.whisper, "read_log", lambda _p: [])
        monkeypatch.setattr(stt.whisper, "device_info",
                            lambda _l: whisper.DeviceInfo(device="cpu"))
        monkeypatch.setattr(stt.whisper, "timings", lambda _l: whisper.Timings())

        base, request = self._base_and_request(tmp_path, audio)
        report = stt.transcribe(_env(tmp_path), request)
        assert os.path.isfile(f"{base}.txt")
        assert report.artifacts.get("txt")


def _env(tmp_path):
    """A stand-in environment whose only reached method is ``require``."""
    model = tmp_path / "ggml-small.bin"
    if not model.exists():
        model.write_bytes(b"m")
    exe = tmp_path / "whisper-cli.exe"
    if not exe.exists():
        exe.write_bytes(b"x")

    class _Env:
        backend = "cpu"
        ffprobe = None
        gpu_backend_configured = False

        def __init__(self):
            self.model = str(model)

        def require(self, key, name):
            return str(exe)

    return _Env()


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

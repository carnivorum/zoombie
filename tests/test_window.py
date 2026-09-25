"""Tests for the ``-From``/``-To`` time window on ``transcribe``.

One test per behaviour of the window, plus the regression that matters most: a
run with NO window must be byte-identical to before, so its artifact names and
its whisper argv are asserted unchanged.

``conftest.py`` puts ``scripts/`` on ``sys.path``, so the package imports directly.
The whisper surface is stubbed (as ``test_stt_args.py`` does), and for the window
path ffmpeg is stubbed too, so no native tool runs.
"""

from __future__ import annotations

import argparse
import os

import pytest

from zoombie.lib import stt, whisper
from zoombie.lib.errors import ZoombieError

# A produced SRT with times RELATIVE to the slice (whisper always writes from
# 00:00:00), which is exactly what the offset pass must rewrite.
SRT_RELATIVE = (
    "1\n"
    "00:00:01,000 --> 00:00:04,500\n"
    "first cue\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "second cue\n"
)
# The same shape, but with the same phrase repeated across eight cues -- a
# repetition loop the detector must still catch on a WINDOW's output.
SRT_LOOPING = "\n\n".join(
    f"{i}\n00:00:{i * 5:02d},000 --> 00:00:{i * 5 + 4:02d},000\nin the same."
    for i in range(1, 9)
) + "\n"


def _env(tmp_path):
    """A stand-in environment; ``require`` returns fake tool paths."""
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"m")
    exe = tmp_path / "whisper-cli.exe"
    exe.write_bytes(b"x")

    class _Env:
        backend = "cpu"
        ffprobe = None
        gpu_backend_configured = False

        def __init__(self):
            self.model = str(model)

        def require(self, key, name):
            return "ffmpeg.exe" if key == "ffmpeg" else str(exe)

    return _Env()


def _patch_whisper(monkeypatch, run):
    monkeypatch.setattr(stt.whisper, "capabilities", lambda _e: whisper.Capabilities())
    monkeypatch.setattr(
        stt.whisper, "probe_backend", lambda _e: whisper.BackendProbe(device="cpu")
    )
    monkeypatch.setattr(stt.whisper, "run_whisper", run)
    monkeypatch.setattr(stt.whisper, "read_log", lambda _p: [])
    monkeypatch.setattr(stt.whisper, "device_info", lambda _l: whisper.DeviceInfo(device="cpu"))
    monkeypatch.setattr(stt.whisper, "timings", lambda _l: whisper.Timings())


def _request(tmp_path, **overrides):
    source = tmp_path / "audio.wav"
    if not source.exists():
        source.write_bytes(b"RIFF")
    item = tmp_path / "item"
    base = stt.resolve_output_base(str(source), str(item))
    values = dict(
        audio_path=str(source), output_base=base, item_dir=str(item),
        no_gpu=True, force=True, work_root=str(tmp_path / "work"),
    )
    values.update(overrides)
    return stt.Request(**values)


class TestParseAndValidate:
    """``checked_window`` reuses ``slides.parse_time`` and refuses an inversion."""

    def test_hhmmss_mmss_and_seconds_all_parse(self, tmp_path):
        assert stt.checked_window(_request(tmp_path, from_time="00:47:00",
                                            to_time="00:55:00")) == (2820.0, 3300.0)
        # Two fields are minutes and seconds, never hours.
        assert stt.checked_window(_request(tmp_path, from_time="47:00")) == (2820.0, None)
        assert stt.checked_window(_request(tmp_path, from_time="2820")) == (2820.0, None)

    def test_no_window_is_none(self, tmp_path):
        assert stt.checked_window(_request(tmp_path)) is None
        # A -To alone is a window from the start.
        assert stt.checked_window(_request(tmp_path, to_time="00:00:30")) == (0.0, 30.0)

    def test_to_before_from_is_refused(self, tmp_path):
        with pytest.raises(ZoombieError) as excinfo:
            stt.checked_window(_request(tmp_path, from_time="00:55:00", to_time="00:47:00"))
        assert "earlier than -From" in str(excinfo.value)


class TestWindowNaming:
    """The artifact base is qualified so a window never overwrites the transcript."""

    def test_tags_preserve_what_the_caller_typed(self, tmp_path):
        assert stt.window_tag(_request(tmp_path, from_time="00:46:30",
                                       to_time="00:58:00"), 2790.0) == "00-46-30 - 00-58-00"
        # -To omitted is the literal ``end``, so an open window is legible.
        assert stt.window_tag(_request(tmp_path, from_time="00:46:30"), 2790.0) == \
            "00-46-30 - end"

    def test_base_stays_inside_data(self, tmp_path):
        item = str(tmp_path / "item")
        base = stt.window_output_base(
            item, _request(tmp_path, from_time="00:46:30", to_time="00:58:00"), 2790.0
        )
        assert base == str(tmp_path / "item" / ".data" / "transcript - 00-46-30 - 00-58-00")
        assert os.path.dirname(os.path.dirname(base)) == item


class TestTimestampOffsetting:
    """``shift_timestamps`` moves only the timing lines, by the window start."""

    def test_full_parse_format_round_trip(self):
        from zoombie.lib import srt as srt_mod

        assert srt_mod.format_timestamp(2820.0) == "00:47:00,000"
        assert srt_mod.parse_timestamp("00:47:00,000") == 2820.0
        assert srt_mod.parse_timestamp("00:47:01,500") == 2821.5

    def test_every_timing_line_is_shifted(self):
        from zoombie.lib import srt as srt_mod

        shifted = srt_mod.shift_timestamps(SRT_RELATIVE, 2820.0)
        assert "00:47:01,000 --> 00:47:04,500" in shifted
        assert "00:47:05,000 --> 00:47:08,000" in shifted
        assert "00:00:01,000" not in shifted

    def test_a_timestamp_inside_cue_text_is_left_alone(self):
        from zoombie.lib import srt as srt_mod

        text = "1\n00:00:01,000 --> 00:00:02,000\nsee 00:00:30 for details\n"
        shifted = srt_mod.shift_timestamps(text, 60.0)
        assert "00:01:01,000 --> 00:01:02,000" in shifted
        # The clock time quoted in the cue body is content, not a timing.
        assert "see 00:00:30 for details" in shifted

    def test_zero_offset_is_a_no_op(self):
        from zoombie.lib import srt as srt_mod

        assert srt_mod.shift_timestamps(SRT_RELATIVE, 0.0) == SRT_RELATIVE


class TestWindowRun:
    """End to end through ``stt.transcribe`` with whisper and ffmpeg stubbed."""

    def test_the_window_is_sliced_named_offset_and_reported(self, tmp_path, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            stt.process, "run_checked",
            lambda argv, **kwargs: captured.update(slice_argv=argv),
        )

        def fake_run(_exe, argv, _out, _err):
            captured["whisper_argv"] = argv
            out_base = argv[argv.index("-of") + 1]
            with open(f"{out_base}.txt", "w", encoding="utf-8") as handle:
                handle.write("hello\n")
            with open(f"{out_base}.srt", "w", encoding="utf-8") as handle:
                handle.write(SRT_RELATIVE)
            return whisper.RunResult()

        _patch_whisper(monkeypatch, fake_run)
        report = stt.transcribe(
            _env(tmp_path),
            _request(tmp_path, from_time="00:47:00", to_time="00:55:00"),
        )
        data = report.to_data()

        # (5) the result carries the range and the output paths.
        assert data["range"] == {"from": 2820.0, "to": 3300.0}
        tag = "transcript - 00-47-00 - 00-55-00"
        assert os.path.basename(data["outputBase"]) == tag
        assert os.path.basename(data["artifacts"]["txt"]["path"]) == f"{tag}.txt"
        assert os.path.basename(data["artifacts"]["srt"]["path"]) == f"{tag}.srt"

        # (2)/(3) the SRT carries the RECORDING clock, not the slice's 00:00:00.
        srt_text = open(data["artifacts"]["srt"]["path"], encoding="utf-8").read()
        assert "00:47:01,000 --> 00:47:04,500" in srt_text
        assert "00:00:01,000" not in srt_text

        # The slice used the ffmpeg helper's shape: -ss before -i, 16 kHz mono PCM.
        argv = captured["slice_argv"]
        assert argv.index("-ss") < argv.index("-i")
        assert "2820.000" in argv and "3300.000" in argv
        assert "-ar" in argv and argv[argv.index("-ar") + 1] == "16000"
        assert "-ac" in argv and argv[argv.index("-ac") + 1] == "1"
        assert "pcm_s16le" in argv
        # whisper is handed the SLICE, and -ss never leaks into its own argv.
        whisper_argv = captured["whisper_argv"]
        assert whisper_argv[whisper_argv.index("-f") + 1].endswith("input.wav")
        assert "-ss" not in whisper_argv

    def test_an_open_window_has_no_to_argument(self, tmp_path, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            stt.process, "run_checked",
            lambda argv, **kwargs: captured.update(slice_argv=argv),
        )

        def fake_run(_exe, argv, _out, _err):
            out_base = argv[argv.index("-of") + 1]
            with open(f"{out_base}.txt", "w", encoding="utf-8") as handle:
                handle.write("hi\n")
            return whisper.RunResult()

        _patch_whisper(monkeypatch, fake_run)
        report = stt.transcribe(_env(tmp_path), _request(tmp_path, from_time="00:47:00"))
        assert report.to_data()["range"] == {"from": 2820.0, "to": None}
        assert os.path.basename(report.output_base) == "transcript - 00-47-00 - end"
        assert "-to" not in captured["slice_argv"]

    def test_a_looping_window_is_reported_not_swallowed(self, tmp_path, monkeypatch):
        """(4) the detector runs on the window output and its loop is surfaced."""
        monkeypatch.setattr(stt.process, "run_checked", lambda argv, **kwargs: None)

        def fake_run(_exe, argv, _out, _err):
            out_base = argv[argv.index("-of") + 1]
            with open(f"{out_base}.txt", "w", encoding="utf-8") as handle:
                handle.write("looping\n")
            with open(f"{out_base}.srt", "w", encoding="utf-8") as handle:
                handle.write(SRT_LOOPING)
            return whisper.RunResult()

        _patch_whisper(monkeypatch, fake_run)
        report = stt.transcribe(
            _env(tmp_path), _request(tmp_path, from_time="00:47:00", to_time="00:55:00")
        )
        assert report.repetitions, "a window that still loops must say so"
        # The reported start is the OFFSET clock, proving the detector ran on the
        # rewritten SRT rather than the raw slice output (first cue at 00:00:05).
        assert report.repetitions[0]["start"] == "00:47:05"

    def test_an_inverted_window_is_refused_before_any_scratch_dir(self, tmp_path, monkeypatch):
        work_root = tmp_path / "work"
        monkeypatch.setattr(
            stt.whisper, "capabilities",
            lambda _e: pytest.fail("no work may start on an inverted window"),
        )
        with pytest.raises(ZoombieError, match="earlier than -From"):
            stt.transcribe(
                _env(tmp_path),
                _request(tmp_path, from_time="00:55:00", to_time="00:47:00",
                         work_root=str(work_root)),
            )
        assert not work_root.exists(), "no scratch dir may be created on a refusal"


class TestNoWindowIsUnchanged:
    """The regression the brief demands: absent -From/-To nothing shifts."""

    def test_names_and_range_are_the_full_file_path(self, tmp_path, monkeypatch):
        def fake_run(_exe, argv, _out, _err):
            out_base = argv[argv.index("-of") + 1]
            with open(f"{out_base}.txt", "w", encoding="utf-8") as handle:
                handle.write("full\n")
            with open(f"{out_base}.srt", "w", encoding="utf-8") as handle:
                handle.write(SRT_RELATIVE)
            return whisper.RunResult()

        _patch_whisper(monkeypatch, fake_run)
        report = stt.transcribe(_env(tmp_path), _request(tmp_path))
        data = report.to_data()

        assert data["range"] is None
        assert os.path.basename(data["outputBase"]) == "transcript"
        assert os.path.basename(data["artifacts"]["txt"]["path"]) == "transcript.txt"
        assert os.path.basename(data["artifacts"]["srt"]["path"]) == "transcript.srt"
        # No offset: the SRT is copied verbatim.
        srt_text = open(data["artifacts"]["srt"]["path"], encoding="utf-8").read()
        assert "00:00:01,000 --> 00:00:04,500" in srt_text

    def test_no_ffmpeg_slice_and_unchanged_whisper_argv(self, tmp_path, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            stt.process, "run_checked",
            lambda argv, **kwargs: pytest.fail("the full-file path must not slice"),
        )

        def fake_run(_exe, argv, _out, _err):
            captured["argv"] = argv
            out_base = argv[argv.index("-of") + 1]
            with open(f"{out_base}.txt", "w", encoding="utf-8") as handle:
                handle.write("full\n")
            return whisper.RunResult()

        _patch_whisper(monkeypatch, fake_run)
        report = stt.transcribe(_env(tmp_path), _request(tmp_path))

        argv = captured["argv"]
        # The window-only flags are absent, and the argv is exactly what
        # build_args produces for the same inputs -- the window never touches it.
        assert "-ss" not in argv and "-to" not in argv
        expected, _ = stt.build_args(
            report.model, argv[argv.index("-f") + 1], argv[argv.index("-of") + 1],
            "auto", whisper.Capabilities(),
            want_srt=False, no_srt=False, no_gpu=True, no_flash_attn=False,
            threads=None,
        )
        assert argv == expected


def _args(tmp_path, **overrides):
    """A Namespace with transcribe's defaults, including the window flags."""
    values = {
        "source": str(tmp_path / "audio.wav"), "output": None, "model": None,
        "language": "auto", "srt": False, "no_srt": False, "force": False,
        "no_gpu": True, "no_flash_attn": False, "threads": 0,
        "allow_cpu_fallback": False, "strict_gpu": False, "work_root": None,
        "keep_work": False, "dry_run": False, "from_time": None, "to_time": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class TestGuardAgreesWithTheWriter:
    """The 678-suite defect this demo caught: the early guard checked the wrong base.

    With a full ``transcript.txt`` present, a window run was refused as if it would
    overwrite it -- the guard checked ``transcript.txt`` while the writer wrote
    ``transcript - <from> - <to>.txt``. ``request_output_base`` is the one
    expression both now read.
    """

    def test_a_window_run_is_not_refused_by_an_existing_full_transcript(self, tmp_path):
        from zoombie.commands import transcribe as transcribe_cmd

        source = tmp_path / "audio.wav"
        source.write_bytes(b"RIFF")
        item = tmp_path / "item"
        data = item / ".data"
        data.mkdir(parents=True)
        (data / "transcript.txt").write_text("the full transcript\n", encoding="utf-8")

        request = stt.Request(
            audio_path=str(source), item_dir=str(item),
            output_base=stt.resolve_output_base(str(source), str(item)),
            from_time="00:47:00", to_time="00:55:00",
        )
        # No raise: the window's base is a different file.
        stt.guard_overwrite(stt.request_output_base(request), force=False)
        assert os.path.basename(stt.request_output_base(request)) == \
            "transcript - 00-47-00 - 00-55-00"

        # The full-file base is still guarded, so the existing transcript is safe.
        full = stt.Request(
            audio_path=str(source), item_dir=str(item),
            output_base=stt.resolve_output_base(str(source), str(item)),
        )
        assert stt.request_output_base(full) == full.output_base

    def test_request_output_base_matches_effective_without_a_window(self, tmp_path):
        request = _request(tmp_path)
        assert stt.request_output_base(request) == stt.effective_output_base(
            request.output_base, request.item_dir
        )


class TestCommandPlumbing:
    def test_the_window_reaches_the_request(self, tmp_path, monkeypatch):
        from zoombie.commands import transcribe as transcribe_cmd

        source = tmp_path / "audio.wav"
        source.write_bytes(b"RIFF")
        seen: dict = {}

        def fake_transcribe(environment, request):
            seen["request"] = request
            return stt.Report(output_base=request.output_base)

        monkeypatch.setattr(transcribe_cmd.env_mod, "resolve", lambda *_a, **_k: object())
        monkeypatch.setattr(transcribe_cmd.stt, "transcribe", fake_transcribe)

        transcribe_cmd.run(_args(
            tmp_path, source=str(source), output=str(tmp_path / "item"),
            from_time="00:47:00", to_time="00:55:00",
        ))
        assert seen["request"].from_time == "00:47:00"
        assert seen["request"].to_time == "00:55:00"

    def test_both_commands_expose_the_flags(self):
        from zoombie import cli

        parser = cli.build_parser()
        for command in ("transcribe", "pipeline"):
            parsed = parser.parse_args(
                [command, "-Source", "x", "-From", "00:47:00", "-To", "00:55:00"]
            )
            assert parsed.from_time == "00:47:00"
            assert parsed.to_time == "00:55:00"

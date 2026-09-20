"""Tests for whisper log parsing, device classification and failure signatures.

These are the regression tests for the silent-CPU-fallback bug: a CUDA build that
initialises but never selects a device, and exits 0.
"""

from __future__ import annotations

import sys

from zoombie.lib import whisper


class TestDeviceInfo:
    def test_selected_cuda_is_proof_of_use(self):
        info = whisper.device_info(
            [
                "ggml_cuda_init: found 1 CUDA devices",
                "whisper_backend_init_gpu: using CUDA backend",
            ]
        )
        assert info.device == "cuda"
        assert info.device_selected is True
        assert info.backend_initialised is True

    def test_selected_vulkan(self):
        info = whisper.device_info(["whisper_backend_init_gpu: using Vulkan backend"])
        assert info.device == "vulkan"
        assert info.device_selected is True

    def test_loaded_but_not_selected_is_cpu(self):
        # Capability, not usage: this is exactly the silent fallback.
        info = whisper.device_info(
            [
                "ggml_cuda_init: found 1 CUDA devices",
                "load_backend: loaded CUDA backend",
            ]
        )
        assert info.device == "cpu"
        assert info.device_selected is False
        assert info.backend_initialised is True
        assert info.reason and "silent CPU fallback" in info.reason

    def test_ng_run_is_cpu_not_cuda(self):
        # `-ng` still loads the CUDA module, so the disabled signal must win.
        info = whisper.device_info(["use gpu = 0", "load_backend: loaded CUDA backend"])
        assert info.device == "cpu"
        assert info.device_selected is False

    def test_empty_log_is_unverified(self):
        info = whisper.device_info([])
        assert info.device == "cpu"
        assert info.reason and "no log output" in info.reason

    def test_no_cuda_devices_names_the_cause(self):
        info = whisper.device_info(["ggml_cuda_init: no CUDA devices found"])
        assert info.device == "cpu"
        assert info.reason and "no CUDA devices" in info.reason


class TestTimings:
    def test_parses_the_block(self):
        result = whisper.timings(
            [
                "whisper_print_timings:     load time =   123.45 ms",
                "whisper_print_timings:     encode time =  1000.00 ms",
                "whisper_print_timings:     decode time =  2000.50 ms",
                "whisper_print_timings:     total time =  3123.95 ms",
            ]
        )
        assert result.load_ms == 123.45
        assert result.encode_ms == 1000.0
        assert result.decode_ms == 2000.5
        assert result.total_ms == 3123.95

    def test_missing_block_is_all_none(self):
        result = whisper.timings(["nothing here"])
        assert result.total_ms is None
        assert result.load_ms is None

    def test_empty_log(self):
        result = whisper.timings([])
        assert result.total_ms is None


class TestGpuFailure:
    def test_zero_exit_is_never_a_failure(self):
        assert not whisper.looks_like_gpu_failure(0, ["CUDA error: out of memory"])

    def test_cuda_error_is_retryable(self):
        assert whisper.looks_like_gpu_failure(3, ["CUDA error: out of memory"])

    def test_cublas_error_is_retryable(self):
        assert whisper.looks_like_gpu_failure(3, ["CUBLAS_STATUS_NOT_INITIALIZED"])

    def test_non_gpu_error_is_not_retried(self):
        # Retrying here would re-run the whole job at CPU speed and bury the cause.
        assert not whisper.looks_like_gpu_failure(3, ["error: failed to open model file"])

    def test_unattributed_crash_stays_retryable(self):
        assert whisper.looks_like_gpu_failure(3, [])


class TestReadLog:
    def test_reads_utf8(self, tmp_path):
        log = tmp_path / "whisper.log"
        log.write_bytes(b"line one\nline two\n")
        assert whisper.read_log(str(log)) == ["line one", "line two", ""]

    def test_reads_utf16le_with_bom(self, tmp_path):
        # A redirected native stderr can be UTF-16; decoding it as UTF-8 would
        # leave NUL bytes and no pattern would ever match.
        log = tmp_path / "whisper.log"
        text = "ggml_cuda_init: found 1 CUDA devices\n"
        log.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
        lines = whisper.read_log(str(log))
        assert any("ggml_cuda_init" in line for line in lines)

    def test_missing_file_is_empty(self, tmp_path):
        assert whisper.read_log(str(tmp_path / "nope.log")) == []

    def test_empty_file_is_empty(self, tmp_path):
        log = tmp_path / "empty.log"
        log.write_bytes(b"")
        assert whisper.read_log(str(log)) == []


class TestCapabilities:
    def test_missing_exe_reports_nothing(self):
        caps = whisper.capabilities(None)
        assert caps.checked is False
        assert caps.flash_attention is False
        assert caps.threads is False

    def test_missing_exe_path(self, tmp_path):
        caps = whisper.capabilities(str(tmp_path / "nope.exe"))
        assert caps.checked is False


class TestProbeBackend:
    def test_missing_exe_reports_not_found(self, tmp_path):
        probe = whisper.probe_backend(str(tmp_path / "nope.exe"))
        assert probe.device is None
        assert probe.reason and "not found" in probe.reason


class TestRunWhisper:
    def test_detects_completion_by_output_not_exit(self, tmp_path):
        """A process that prints the timings marker and then never exits is a SUCCESS.

        This build can finish its work and hang on teardown, so waiting for exit
        would never return. The completion condition is whisper's own output.
        """
        script = tmp_path / "hang.py"
        script.write_text(
            "import sys, time\n"
            "sys.stderr.write('whisper_print_timings:     total time =   100.00 ms\\n')\n"
            "sys.stderr.flush()\n"
            "time.sleep(600)\n"
        )
        stdout_log = tmp_path / "out.log"
        stderr_log = tmp_path / "err.log"

        result = whisper.run_whisper(
            sys.executable,
            [str(script)],
            str(stdout_log),
            str(stderr_log),
            grace_seconds=1.0,
            hard_cap_seconds=30.0,
        )

        assert result.hang_detected is True
        assert result.exit_code == 0
        assert result.timed_out is False

    def test_clean_exit_reports_exit_code(self, tmp_path):
        script = tmp_path / "fine.py"
        script.write_text("import sys\nsys.exit(0)\n")
        result = whisper.run_whisper(
            sys.executable,
            [str(script)],
            str(tmp_path / "out.log"),
            str(tmp_path / "err.log"),
            grace_seconds=0.2,
            hard_cap_seconds=30.0,
        )
        assert result.exit_code == 0
        assert result.hang_detected is False

    def test_nonzero_exit_is_reported(self, tmp_path):
        script = tmp_path / "fail.py"
        script.write_text("import sys\nsys.exit(3)\n")
        result = whisper.run_whisper(
            sys.executable,
            [str(script)],
            str(tmp_path / "out.log"),
            str(tmp_path / "err.log"),
            grace_seconds=0.2,
            hard_cap_seconds=30.0,
        )
        assert result.exit_code == 3

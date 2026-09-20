"""Everything about running whisper.cpp: probing, logging, timings, GPU proof.

The theme of this module is the bug it exists to prevent: a CUDA build missing its
cuBLAS runtime *exits 0* while transcribing on the CPU. So the device is PROVED
from whisper's own log, never assumed from the manifest, and capability is kept
strictly separate from usage:

* ``device_selected``     - a GPU backend was chosen for decoding. This is the
                            only positive proof the GPU was used.
* ``backend_initialised`` - the backend loaded. Capability, not usage.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Sequence

from . import process

# The signal that whisper has FINISHED its work, regardless of whether the
# process then exits. See run_whisper for why exit is not the success condition.
TIMINGS_MARKER = re.compile(r"whisper_print_timings:\s+total time")

# Device-selection signals, in the order they must be checked. The later ones are
# also present in the negative cases, which is why order matters:
#   1. `whisper_backend_init_gpu: using <NAME> backend` - actually SELECTED.
#   2. `use gpu = 0` - the caller disabled the GPU (-ng). MUST be checked before
#      the "loaded backend" signal, because -ng still loads the CUDA module and
#      would otherwise be reported as a GPU run.
#   3. loaded/initialised backend - capability only, never usage.
_SELECTED = re.compile(r"whisper_backend_init_gpu:\s+using\s+(\S+)\s+backend")
_GPU_DISABLED = re.compile(r"use gpu\s*=\s*0")
_CUDA_INITIALISED = re.compile(
    r"load_backend:\s+loaded CUDA backend|ggml_cuda_init:\s*found \d+ CUDA devices"
)
_VULKAN_INITIALISED = re.compile(r"load_backend:\s+loaded Vulkan backend")

_LOAD_TIME = re.compile(r"whisper_print_timings:\s+load time\s*=\s*([0-9.]+)\s*ms")
_TOTAL_TIME = re.compile(r"whisper_print_timings:\s+total time\s*=\s*([0-9.]+)\s*ms")
_ENCODE_TIME = re.compile(r"whisper_print_timings:\s+encode time\s*=\s*([0-9.]+)\s*ms")
_DECODE_TIME = re.compile(r"whisper_print_timings:\s+decode time\s*=\s*([0-9.]+)\s*ms")

# Signatures that make a non-zero exit plausibly the GPU's fault. The CPU retry
# restarts the WHOLE job, so it must only fire when the failure looks like this;
# otherwise a bad model path is silently re-run at CPU speed and the real cause
# is buried under the retry's own output.
_GPU_FAILURE = re.compile(
    r"gpu|ggml_backend_cuda|ggml_cuda_init|CUDA error|cudaError|CUDA_ERROR|"
    r"out of memory|CUBLAS_STATUS|cublas|cublasLt|device-side|uncorrectable|"
    r"invalid device|no CUDA devices|failed to load [^\r\n]*cuda|driver",
    re.IGNORECASE,
)

# Cache for the per-exe capability probe, so one run reads --help once.
_CAPABILITY_CACHE: dict[str, "Capabilities"] = {}


@dataclass
class DeviceInfo:
    """Which device the run actually used, and on what evidence."""

    device: str = "cpu"
    device_name: str | None = None
    device_selected: bool = False
    backend_initialised: bool = False
    reason: str | None = None


@dataclass
class Timings:
    """The whisper_print_timings block, in milliseconds."""

    load_ms: float | None = None
    total_ms: float | None = None
    encode_ms: float | None = None
    decode_ms: float | None = None


@dataclass
class Capabilities:
    """Which optional flags the installed build advertises.

    Flags such as flash attention (-fa) and a thread count (-t) exist only in some
    builds, and passing an unknown flag ABORTS the run. whisper-cli --help lists
    exactly the flags the binary accepts, so it is read once and cached per exe
    path. A false negative merely omits a flag; a false positive fails the run.
    """

    checked: bool = False
    ok: bool = False
    raw: str = ""
    flash_attention: bool = False
    threads: bool = False
    vad: bool = False
    vad_model: bool = False
    best_of: bool = False
    no_fallback: bool = False

    def to_report(self) -> dict:
        return {
            "probed": self.checked,
            "available": self.ok,
            "flashAttention": self.flash_attention,
            "threads": self.threads,
            "vad": self.vad,
        }


@dataclass
class RunResult:
    """Outcome of one whisper invocation."""

    exit_code: int = 0
    hang_detected: bool = False
    timed_out: bool = False
    wall_ms: float = 0.0


def read_log(path: str | None) -> list[str]:
    """Read a captured whisper log as a list of lines (empty when unusable).

    whisper.cpp writes its backend banner and timings block to STDERR, so the
    caller redirects stderr to a file and reads it here.

    The encoding is detected from the BOM because a redirected native stderr can
    be UTF-16. Decoding such a file as UTF-8 would leave interleaved NUL bytes, so
    NO pattern (device, timings, backend) would ever match and device detection
    would silently degrade to "cpu". This is the one place the PowerShell version
    needed a heuristic NUL-strip; here the BOM check plus a NUL fallback is
    enough.
    """
    from . import paths

    if not path or not paths.is_file(path):
        return []
    try:
        raw = open(paths.to_extended(path), "rb").read()
    except OSError:
        return []
    if not raw:
        return []

    if raw[:2] == b"\xff\xfe":
        text = raw[2:].decode("utf-16-le", errors="replace")
    elif raw[:2] == b"\xfe\xff":
        text = raw.decode("utf-16-be", errors="replace")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw[3:].decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
        # A BOM-less UTF-16 file decodes into NUL-laden text; re-read it properly.
        if "\x00" in text:
            text = raw.decode("utf-16-le", errors="replace")

    return text.replace("\r\n", "\n").split("\n")


def device_info(log_lines: Sequence[str] | None) -> DeviceInfo:
    """Determine the device whisper ACTUALLY used, from its captured log."""
    info = DeviceInfo()
    text = "\n".join(log_lines) if log_lines else ""
    if not text.strip():
        info.reason = "whisper wrote no log output; the device could not be verified"
        return info

    # 1. The backend that was actually selected (definitive, real runs only).
    match = _SELECTED.search(text)
    if match:
        name = match.group(1)
        info.device_name = name
        info.device = "vulkan" if "vulkan" in name.lower() else "cuda"
        info.device_selected = True
        info.backend_initialised = True
        return info

    # 2. GPU explicitly disabled by the caller (-ng): a deliberate CPU run.
    if _GPU_DISABLED.search(text):
        info.reason = "GPU explicitly disabled for this run (-ng): use gpu = 0"
        info.backend_initialised = bool(
            _CUDA_INITIALISED.search(text) or _VULKAN_INITIALISED.search(text)
        )
        return info

    # 3. A backend initialised but NO device was selected: capability only.
    if _CUDA_INITIALISED.search(text):
        info.device_name = "CUDA"
        info.backend_initialised = True
        info.reason = "a CUDA backend initialised but no device was selected for decoding (silent CPU fallback)"
        return info
    if _VULKAN_INITIALISED.search(text):
        info.device_name = "Vulkan"
        info.backend_initialised = True
        info.reason = "a Vulkan backend initialised but no device was selected for decoding (silent CPU fallback)"
        return info

    # 4. No GPU backend initialised even though one was expected: name the
    # observable cause so a silent CPU fallback is never just "slow".
    if re.search(r"ggml_cuda_init:\s*no CUDA devices", text):
        info.reason = "ggml_cuda_init found no CUDA devices"
        return info

    failed = re.search(r"load_backend:\s+failed to load (?:ggml-cuda|CUDA)[^\r\n]*", text)
    if failed:
        info.reason = f"the CUDA backend failed to load: {failed.group(0).strip()}"
        return info

    if re.search(r"ggml_cuda_init[^\r\n]*error", text):
        info.reason = "ggml_cuda_init reported an error"
        return info

    info.reason = "no GPU backend was initialised (silent CPU fallback)"
    return info


def timings(log_lines: Sequence[str] | None) -> Timings:
    """Parse the whisper_print_timings block, in milliseconds."""
    result = Timings()
    for line in log_lines or []:
        match = _LOAD_TIME.search(line)
        if match:
            result.load_ms = float(match.group(1))
            continue
        match = _TOTAL_TIME.search(line)
        if match:
            result.total_ms = float(match.group(1))
            continue
        match = _ENCODE_TIME.search(line)
        if match:
            result.encode_ms = float(match.group(1))
            continue
        match = _DECODE_TIME.search(line)
        if match:
            result.decode_ms = float(match.group(1))
    return result


def looks_like_gpu_failure(exit_code: int, log_lines: Sequence[str] | None) -> bool:
    """Does a non-zero exit LOOK like a GPU failure worth retrying?

    An EMPTY log with a non-zero exit is retryable: there is no evidence either
    way, and refusing would turn a transient native crash into a hard failure. A
    log that shows a non-GPU error is not retried.
    """
    if exit_code == 0:
        return False
    text = "\n".join(log_lines) if log_lines else ""
    if not text.strip():
        return True
    return bool(_GPU_FAILURE.search(text))


def help_text(whisper_exe: str | None) -> str:
    """Run ``whisper-cli --help`` and return its combined output.

    --help performs the full backend initialisation and then exits, so it proves
    what the GPU can do in about a second, before any model or audio is involved.
    It needs no model and writes no file, which is what makes it safe in -Check
    and -DryRun.
    """
    from . import paths

    if not whisper_exe or not paths.is_file(whisper_exe):
        return ""
    _code, text = process.run_text([whisper_exe, "--help"], timeout=120)
    return text


def capabilities(whisper_exe: str | None) -> Capabilities:
    """Which optional flags the installed build advertises (cached per exe)."""
    from . import paths

    if not whisper_exe or not paths.is_file(whisper_exe):
        # The executable is absent, so there is nothing honest to report. All
        # flags stay false, which makes the caller omit them rather than guess.
        return Capabilities()

    cached = _CAPABILITY_CACHE.get(whisper_exe)
    if cached is not None:
        return cached

    raw = help_text(whisper_exe)
    caps = Capabilities(checked=True)
    if raw and raw.strip():
        caps.ok = True
        caps.raw = raw
        if re.search(r"(?m)^\s*-fa\b|--flash-attn\b", raw):
            caps.flash_attention = True
        if re.search(r"(?m)^\s*-t\b[^\r\n]*threads|--threads\b", raw):
            caps.threads = True
        if re.search(r"--vad\b", raw):
            caps.vad = True
        if re.search(r"(?m)^\s*-vm\b|--vad-model\b", raw):
            caps.vad_model = True
        if re.search(r"(?m)^\s*-bs\b|--beam-size\b|--best-of\b", raw):
            caps.best_of = True
        if re.search(r"(?m)^\s*-nf\b|--no-fallback\b", raw):
            caps.no_fallback = True

    _CAPABILITY_CACHE[whisper_exe] = caps
    return caps


@dataclass
class BackendProbe:
    """Result of the pre-run ``--help`` backend probe."""

    device: str | None = None
    device_name: str | None = None
    reason: str | None = None
    log: list[str] = field(default_factory=list)


def probe_backend(whisper_exe: str | None) -> BackendProbe:
    """Ask whisper-cli which device it can ACTUALLY initialise, without a file.

    env.json records what was INTENDED; only a probe reports what is usable, which
    is exactly how a CUDA install missing its cuBLAS runtime stays invisible
    otherwise.

    No captured output at all is "unknown", NOT "cpu". Returning cpu there is what
    made a build whose --help exits before backend init look like a failed GPU
    machine.
    """
    from . import paths

    result = BackendProbe()
    if not whisper_exe or not paths.is_file(whisper_exe):
        result.reason = "whisper-cli.exe not found; the backend could not be probed"
        return result

    text = help_text(whisper_exe)
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    result.log = lines
    if not lines:
        result.device = None
        result.reason = "whisper-cli --help produced no output; the backend could not be probed"
        return result

    info = device_info(lines)
    # --help never loads a model, so the device-selected signal does not appear
    # even on a healthy CUDA box. Capability therefore comes from
    # backend_initialised, while device_selected stays reserved for real runs.
    if info.backend_initialised and not info.device_selected:
        result.device = "vulkan" if (info.device_name or "").lower().find("vulkan") >= 0 else "cuda"
        result.device_name = info.device_name
        result.reason = (
            f"backend initialised ({info.device_name}); reported as capability, "
            "not as a selected device"
        )
        return result

    result.device = info.device
    result.device_name = info.device_name
    result.reason = info.reason
    return result


def run_whisper(
    whisper_exe: str,
    args: Sequence[str],
    stdout_path: str,
    stderr_path: str,
    *,
    grace_seconds: float = 10.0,
    hard_cap_seconds: float = 21600.0,
) -> RunResult:
    """Run whisper-cli to completion, tolerating a hang at process exit.

    This build can finish all of its work and then FAIL TO EXIT: the transcript is
    written, the timings block is printed, and the process then hangs on teardown
    (a CUDA/driver shutdown hang). Waiting for exit therefore never returns.

    So the completion condition is WHISPER'S OWN OUTPUT, not process exit: once
    the log contains the timings block, the work is done. A short grace period is
    allowed for a clean exit; if the process is still alive after it, it is killed
    and the run is treated as successful, with ``hang_detected`` set so the caller
    can report it.

    The grace period and hard cap keep this safe for long files: the kill only
    ever fires AFTER the work has demonstrably completed, or after an absurd cap.
    """
    from . import paths

    result = RunResult()
    started = time.monotonic()

    with open(paths.to_extended(stdout_path), "wb") as out_handle, open(
        paths.to_extended(stderr_path), "wb"
    ) as err_handle:
        try:
            proc = subprocess.Popen(
                [whisper_exe, *args],
                stdin=subprocess.DEVNULL,
                stdout=out_handle,
                stderr=err_handle,
                env=process.child_env(),
            )
        except OSError as exc:
            result.exit_code = -1
            process.log(f"could not launch whisper-cli: {exc}", "error")
            return result

        marker_seen = False
        while True:
            try:
                exited = proc.poll() is not None
            except OSError:
                exited = True

            if exited:
                break

            elapsed = time.monotonic() - started
            if elapsed > hard_cap_seconds:
                _terminate(proc)
                result.timed_out = True
                result.exit_code = -1  # genuinely did not finish: a real failure
                result.wall_ms = round((time.monotonic() - started) * 1000, 1)
                return result

            if not marker_seen and paths.is_file(stderr_path):
                # A sharing violation while whisper is still writing simply means
                # "not done yet"; read_log already returns [] in that case.
                lines = read_log(stderr_path)
                if lines and any(TIMINGS_MARKER.search(line) for line in lines):
                    marker_seen = True

            if marker_seen:
                # The work is finished; give a clean exit a moment, then stop it.
                time.sleep(grace_seconds)
                if proc.poll() is None:
                    _terminate(proc)
                    result.hang_detected = True
                break

            time.sleep(0.25)

        code = proc.returncode
        if result.hang_detected:
            result.exit_code = 0
        elif code is None:
            # Exited without a usable code: trust the completed work rather than
            # fail on a reporting quirk.
            result.exit_code = 0
        else:
            result.exit_code = int(code)

    result.wall_ms = round((time.monotonic() - started) * 1000, 1)
    return result


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        pass

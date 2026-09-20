"""``doctor``: report tool status, and the two backend truths.

The critical distinction this command exists to expose: ``backendConfigured``
comes from env.json (what was INTENDED at install time) while ``backendObserved``
comes from running whisper-cli (what it can ACTUALLY initialise). Reporting only
the former is exactly how a CUDA install with a missing cuBLAS runtime looked
healthy while every run silently used the CPU.

The probe is ``whisper-cli --help``, which performs the full backend init and
exits: no model, no audio, no filesystem writes.
"""

from __future__ import annotations

from .. import SKILL_VERSION
from ..cli import Outcome
from ..lib import cublas, env as env_mod, paths, process, tools, whisper


def _backend_probe_reason(configured: str | None, runtime: cublas.RuntimeStatus, probe_reason: str | None) -> str | None:
    """Name the likely cause when the CUDA runtime DLLs are absent.

    A bare "cpu" would leave the mismatch unexplained; naming the missing DLLs
    makes it actionable.
    """
    if configured == "cuda" and not runtime.ready and runtime.missing:
        missing = ", ".join(runtime.missing)
        return (
            f"the CUDA runtime is incomplete (missing: {missing}); ggml-cuda.dll "
            "cannot create a device, so whisper falls back to the CPU"
        )
    return probe_reason


def build_report(environment: env_mod.Env) -> dict:
    """Assemble the full status report."""
    configured = environment.backend
    probe = whisper.probe_backend(environment.whisper)
    runtime = cublas.runtime_status(environment.whisper)
    caps = whisper.capabilities(environment.whisper)
    probe_reason = _backend_probe_reason(configured, runtime, probe.reason)

    model = environment.model
    model_length = paths.path_length(model) if model else 0

    return {
        "root": environment.root,
        "asciiRoot": environment.ascii_root,
        # The ASCII invariant has a sibling: whisper.cpp cannot use the \\?\
        # escape hatch, so an over-long root is a hard failure that must be
        # diagnosable rather than a mystery "failed to open model".
        "paths": paths.root_path_report(environment.root),
        "ffmpeg": {
            "path": environment.ffmpeg,
            "version": tools.tool_version(environment.ffmpeg, ["-version"]),
        },
        "ffprobe": {
            "path": environment.ffprobe,
            "version": tools.tool_version(environment.ffprobe, ["-version"]),
        },
        "ytDlp": {
            "via": "python -m yt_dlp",
            "python": environment.python,
            "version": tools.tool_version(environment.python, ["-m", "yt_dlp", "--version"]),
        },
        "whisper": {
            "path": environment.whisper,
            # Kept for existing callers; equals backendConfigured.
            "backend": configured,
            "backendConfigured": configured,
            "backendObserved": probe.device,
            "deviceName": probe.device_name,
            "probeReason": probe_reason,
            "cudaRuntime": runtime.to_report(),
            # The flags the installed build actually advertises, so a caller can
            # tell "flash attention is unavailable" from "it was not requested".
            "capabilities": caps.to_report(),
        },
        "model": {
            "path": model,
            "exists": bool(model and paths.is_file(model)),
            # Measured so a model that exists but cannot be opened by whisper
            # (over-long path) is distinguishable from a missing file.
            "length": model_length,
            "fits": bool(model) and model_length <= paths.PATH_BUDGET,
        },
    }


def run(args) -> Outcome:  # noqa: ARG001 - no per-command options
    environment = env_mod.resolve()
    report = build_report(environment)

    missing: list[str] = []
    if not report["ffmpeg"]["path"]:
        missing.append("ffmpeg")
    if not report["ffprobe"]["path"]:
        missing.append("ffprobe")
    if not report["ytDlp"]["version"]:
        missing.append("yt-dlp")
    if not report["whisper"]["path"]:
        missing.append("whisper-cli")
    if not report["model"]["exists"]:
        missing.append("model")

    configured = report["whisper"]["backendConfigured"]
    observed = report["whisper"]["backendObserved"]
    runtime = cublas.runtime_status(environment.whisper)
    probe_reason = report["whisper"]["probeReason"]

    # A mismatch is a warning, not a failure: the CPU fallback still transcribes,
    # it is just orders of magnitude slower, and the user must be told.
    warnings: list[str] = []
    if configured and observed and configured != observed:
        warnings.append(
            f"backend mismatch: configured '{configured}' but whisper initialises "
            f"'{observed}' ({probe_reason})"
        )
    if configured == "cuda" and not runtime.ready:
        warnings.append(f"CUDA runtime incomplete (missing: {', '.join(runtime.missing)})")
    if not observed:
        warnings.append(f"backend could not be probed: {probe_reason}")

    for warning in warnings:
        process.log(warning, "warn")

    ok = not missing
    error = f"Missing: {', '.join(missing)}" if missing else None
    return Outcome(
        ok=ok,
        data={"report": report, "missing": missing, "warnings": warnings, "version": SKILL_VERSION},
        error=error,
    )

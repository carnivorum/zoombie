"""Transcription orchestration, shared by ``transcribe`` and ``pipeline``.

The invariants this module owns, all of which encode hard-won fixes:

1. **ASCII isolation.** whisper.cpp misbehaves when a path it receives contains
   non-ASCII characters, so the audio is ALWAYS copied into an ASCII work dir,
   whisper runs entirely inside it with an ASCII ``-of``, and artifacts are copied
   back to the user's real (possibly Cyrillic) destination. Silent detection is
   never trusted.
2. **GPU proved, not assumed.** A GPU backend is only counted as *used* when a
   device-selection line appears in whisper's own log. ``backend_initialised``
   alone is capability.
3. **A CPU run on a GPU-configured machine is a FAILURE.** A CUDA build missing
   its cuBLAS runtime exits 0 while transcribing on the CPU, so success must be
   refused unless the user opted out with ``-NoGpu``/``-AllowCpuFallback``.
4. **Narrow GPU retry.** The CPU retry restarts the whole job, so it only fires
   when the failure actually looks like a GPU failure; each attempt gets its own
   log so the GPU error survives.
5. **Diagnostics outlive the scratch dir.** A fallback or policy violation keeps a
   copy of the whisper log beside the transcript.
6. **A pure source producer.** The stage emits the ``.txt`` AND the ``.srt`` on
   every run (``-NoSrt`` opts out), plus a ``<base>.source.json`` origin sidecar.
   Nothing downstream has to re-run whisper to recover timings or the origin URL.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .. import SKILL_VERSION
from . import cublas, env as env_mod, paths, process, whisper
from .errors import StepFailedError, ZoombieError


@dataclass
class Request:
    """Everything one transcription needs, independent of the CLI."""

    audio_path: str
    output_base: str
    language: str = "auto"
    # Accepted no-op alias for the old -Srt flag: the SRT is written by default
    # now, so the only thing that can remove it is no_srt.
    want_srt: bool = False
    no_srt: bool = False
    force: bool = False
    no_gpu: bool = False
    no_flash_attn: bool = False
    threads: int = 0
    allow_cpu_fallback: bool = False
    strict_gpu: bool = False
    work_root: str | None = None
    keep_work: bool = False
    dry_run: bool = False
    # Origin metadata for the ``<base>.source.json`` sidecar. Filled by the
    # download stage in ``pipeline``; the defaults describe a local input file.
    source_url: str | None = None
    source_title: str | None = None
    source_id: str | None = None
    source_kind: str = "video"
    # False when the caller deletes the media after transcribing (pipeline's
    # default), which is exactly the case the sidecar exists for.
    source_kept: bool = False


@dataclass
class Report:
    """The outcome, already shaped for the CLI's JSON ``data`` object."""

    output_base: str
    artifacts: dict = field(default_factory=dict)
    backend: str | None = None
    model: str | None = None
    device_used: str = "cpu"
    device_name: str | None = None
    device_selected: bool = False
    backend_initialised: bool = False
    gpu_capable: bool = False
    gpu_required: bool = False
    flash_attention: bool = False
    threads: int | None = None
    load_ms: float | None = None
    total_ms: float | None = None
    encode_ms: float | None = None
    decode_ms: float | None = None
    audio_duration_sec: float | None = None
    realtime_factor: float | None = None
    wall_ms: float = 0.0
    gpu_attempt_wall_ms: float | None = None
    fallback_reason: str | None = None
    silent_cpu_fallback: bool = False
    whisper_exit_hang: bool = False
    whisper_timed_out: bool = False
    log_path: str | None = None
    log: list[str] = field(default_factory=list)

    def to_data(self) -> dict:
        """The documented camelCase result shape."""
        return {
            "outputBase": self.output_base,
            "artifacts": self.artifacts,
            "backend": self.backend,
            # backendConfigured is the manifest's intent; deviceUsed is what the
            # run actually did. They diverge in exactly the failure this reports.
            "backendConfigured": self.backend,
            "deviceUsed": self.device_used,
            "deviceName": self.device_name,
            "deviceSelected": self.device_selected,
            # deviceVerified is the positive proof that a GPU device was selected
            # for decoding (unlike backendInitialised, which only means the module
            # loaded).
            "deviceVerified": self.device_selected,
            "backendInitialised": self.backend_initialised,
            "gpuCapable": self.gpu_capable,
            "gpuRequired": self.gpu_required,
            "flashAttention": self.flash_attention,
            "threads": self.threads,
            "loadMs": self.load_ms,
            "totalMs": self.total_ms,
            "encodeMs": self.encode_ms,
            "decodeMs": self.decode_ms,
            "audioDurationSec": self.audio_duration_sec,
            "realtimeFactor": self.realtime_factor,
            "wallMs": self.wall_ms,
            "gpuAttemptWallMs": self.gpu_attempt_wall_ms,
            "fallbackReason": self.fallback_reason,
            "silentCpuFallback": self.silent_cpu_fallback,
            "whisperExitHang": self.whisper_exit_hang,
            "whisperTimedOut": self.whisper_timed_out,
            "logPath": self.log_path,
            "log": self.log,
            "asciiSafe": True,
        }


def audio_duration_seconds(ffprobe: str | None, audio_path: str) -> float | None:
    """Input duration from ffprobe, or None when it cannot be determined.

    Reported as null rather than guessed: the realtime factor is only meaningful
    when the duration is real.
    """
    if not ffprobe or not paths.is_file(ffprobe):
        return None
    code, text = process.run_text(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", audio_path],
        timeout=120,
    )
    if code != 0:
        return None
    try:
        duration = float(text.strip())
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def build_args(
    model: str,
    input_path: str,
    output_base: str,
    language: str,
    caps: whisper.Capabilities,
    *,
    want_srt: bool,
    no_gpu: bool,
    no_flash_attn: bool,
    threads: int | None,
    no_srt: bool = False,
) -> tuple[list[str], bool]:
    """Assemble the whisper-cli arguments, returning ``(argv, flash_attn_used)``.

    Optional flags (-fa, -t) exist only in some builds and an unknown flag aborts
    the run, so they are added only when ``--help`` listed them. That is the
    "probe the binary, never assume" rule: a false negative merely omits a flag,
    a false positive fails the run.

    ``-osrt`` is NOT optional: it is emitted on every run unless ``no_srt`` is set.
    ``want_srt`` is kept as an accepted no-op alias of the old ``-Srt`` flag, both
    so existing callers keep working and because the flag can only ever ADD what
    the default already produces.
    """
    # -nt strips the timestamps from the .txt, which is exactly WHY the .srt is the
    # ONLY timing source in the output set: with no [00:00:00.000 --> ...] lines in
    # the .txt, a downstream indexing pass has nowhere else to read the timings
    # from. Removing -nt without giving the .txt its own timestamps would silently
    # break that pass, so the two decisions must be changed together.
    argv = ["-m", model, "-f", input_path, "-l", language, "-otxt", "-nt"]
    if not no_srt:
        argv.append("-osrt")
    if no_gpu:
        argv.append("-ng")
    argv += ["-of", output_base]

    # Flash attention: GPU runs only, and only when the build advertises -fa.
    flash_attn = False
    if not no_gpu and not no_flash_attn and caps.flash_attention:
        argv.append("-fa")
        flash_attn = True

    # Thread count matters most on the CPU path, so it is set on a deliberate -ng
    # run rather than left at the build's default.
    if no_gpu and caps.threads and threads and threads > 0:
        argv += ["-t", str(threads)]

    return argv, flash_attn


def transcribe(environment: env_mod.Env, request: Request) -> Report:
    """Run the full transcription stage and return a populated report."""
    exe = environment.require("whisper", "whisper-cli")
    if not environment.model or not paths.is_file(environment.model):
        raise ZoombieError(
            f"Whisper model not found: {environment.model}. Run the zoombie setup first."
        )
    # whisper.cpp opens the model with a plain fopen and cannot use the \\?\
    # escape hatch, so an over-long model path is refused here with the real cause
    # instead of failing later as "failed to open model".
    paths.assert_fits(environment.model, "The whisper model path")

    work_root = request.work_root or paths.env_path(paths.WORK_FOLDER)
    if not paths.is_ascii(work_root):
        raise ZoombieError(f"Work root must be ASCII: {work_root}")
    paths.assert_fits(work_root, "The work root (-WorkRoot)", slack=80)

    output_base = request.output_base
    # The longest suffix appended to the base is ``.source.json`` (12 characters) +
    # the dot, so the budget is checked with room for the sidecar.
    paths.assert_fits(output_base, "The transcribe output path", slack=13)

    # Refuse BEFORE any scratch dir is created: overwriting an existing transcript
    # used to happen silently, unlike ``extract``/``readpdf``, which guard.
    guard_overwrite(output_base, request.force)

    report = Report(output_base=output_base, backend=environment.backend,
                    model=environment.model)

    # Probe the binary ONCE for the flags it advertises and for whether a GPU
    # backend can initialise at all.
    caps = whisper.capabilities(exe)
    probe = whisper.probe_backend(exe)
    gpu_capable = probe.device in ("cuda", "vulkan")
    report.gpu_capable = gpu_capable

    # A CPU-only machine (backend 'cpu') is never forced into a failure.
    gpu_configured = environment.gpu_backend_configured
    gpu_required = gpu_configured and not request.no_gpu and not request.allow_cpu_fallback
    report.gpu_required = gpu_required

    threads = request.threads if request.threads > 0 else process.cpu_threads()

    work = paths.new_ascii_dir(work_root)
    if request.dry_run:
        extension = paths.extension_of(request.audio_path) or ".bin"
        input_path = os.path.join(work, "input" + extension.lower())
    else:
        copied = paths.copy_into_safe_work(request.audio_path, work)
        input_path = copied["input_path"]
    out_base = os.path.join(work, "out")

    argv, flash_attn = build_args(
        environment.model,
        input_path,
        out_base,
        request.language,
        caps,
        want_srt=request.want_srt,
        no_srt=request.no_srt,
        no_gpu=request.no_gpu,
        no_flash_attn=request.no_flash_attn,
        threads=threads,
    )
    report.flash_attention = flash_attn
    report.threads = threads if request.no_gpu else None

    process.log(f"whisper-cli (ascii-safe) -> {output_base}", "step")
    process.log(
        f"  work={work}  backend={environment.backend}  gpuCapable={gpu_capable}  "
        f"model={os.path.basename(environment.model or '')}"
    )

    if request.dry_run:
        report.output_base = output_base
        return report

    whisper_log = os.path.join(work, "whisper.log")
    retry_log = os.path.join(work, "whisper.retry.log")
    retry_stdout = os.path.join(work, "whisper.retry.stdout.log")
    stdout_log = os.path.join(work, "whisper.stdout.log")

    run = whisper.run_whisper(exe, argv, stdout_log, whisper_log)
    exit_code = run.exit_code
    report.whisper_exit_hang = run.hang_detected
    report.whisper_timed_out = run.timed_out
    report.wall_ms = run.wall_ms

    fallback_reason: str | None = None
    gpu_attempt_wall_ms: float | None = None
    log_lines = whisper.read_log(whisper_log)

    # The GPU path may fail mid-run. The retry is conditional on the failure
    # actually LOOKING like a GPU failure: retrying on any non-zero exit re-ran
    # the whole job at CPU speed for unrelated errors (a bad model path, an
    # unsupported codec) and hid the real cause behind the retry's own output.
    if exit_code != 0 and not request.no_gpu:
        if whisper.looks_like_gpu_failure(exit_code, log_lines):
            gpu_attempt_wall_ms = report.wall_ms
            fallback_reason = (
                f"whisper exited {exit_code} on the GPU path; the whole job was "
                "restarted on the CPU (-ng)"
            )
            process.log(fallback_reason, "warn")

            retry_args, _ = build_args(
                environment.model,
                input_path,
                out_base,
                request.language,
                caps,
                want_srt=request.want_srt,
                no_srt=request.no_srt,
                no_gpu=True,
                no_flash_attn=request.no_flash_attn,
                threads=threads,
            )
            retry = whisper.run_whisper(exe, retry_args, retry_stdout, retry_log)
            exit_code = retry.exit_code
            report.wall_ms = retry.wall_ms
            report.whisper_exit_hang = report.whisper_exit_hang or retry.hang_detected
            report.whisper_timed_out = report.whisper_timed_out or retry.timed_out
            log_lines = whisper.read_log(retry_log)

    if exit_code != 0:
        detail = ""
        if not request.no_gpu and not fallback_reason:
            detail = (
                f" (exit {exit_code} does not match a GPU failure signature, so no "
                "CPU retry was attempted; the GPU attempt's own error is the cause)"
            )
        raise StepFailedError(f"whisper-cli failed (exit {exit_code}){detail}")

    # Copy artifacts back to the real (possibly non-ASCII) destination.
    output_dir = os.path.dirname(output_base)
    if output_dir:
        paths.ensure_dir(output_dir)
    # TXT and SRT are BOTH produced on every run; only -NoSrt removes the SRT from
    # the whisper arguments, so the list no longer depends on a per-run flag.
    extensions = ["txt"] if request.no_srt else ["txt", "srt"]
    artifacts: dict[str, dict] = {}
    for extension in extensions:
        source = f"{out_base}.{extension}"
        if paths.is_file(source):
            destination = f"{output_base}.{extension}"
            # The base may be exactly at the budget; the appended extension is
            # what actually breaks it, so the length is checked on the final path.
            paths.assert_fits(destination, f"The transcript output path (.{extension})")
            paths.copy_file(source, destination)
            artifacts[extension] = {"path": destination, "size": paths.file_size(destination)}
    report.artifacts = artifacts
    # Report the SRT slot explicitly even when -NoSrt suppressed it, so a caller
    # can tell "timings were not requested" from "the run failed to produce them".
    if "srt" not in artifacts:
        report.artifacts["srt"] = None

    # Capture the ORIGIN before anything can delete the media: ``pipeline`` removes
    # the downloaded file (and its directory) at the end of the run, so this
    # sidecar is the only surviving link back to the source URL.
    report.artifacts["sidecar"] = write_source_sidecar(
        output_base, request, report, extension_count=len(extensions)
    )

    # Read the captured log for the device ACTUALLY used and the timings. whisper
    # emits both on stderr, so this must read the log file and not stdout.
    device = whisper.device_info(log_lines)
    timings = whisper.timings(log_lines)
    report.device_used = device.device
    report.device_name = device.device_name
    report.device_selected = device.device_selected
    report.backend_initialised = device.backend_initialised
    report.load_ms = timings.load_ms
    report.total_ms = timings.total_ms
    report.encode_ms = timings.encode_ms
    report.decode_ms = timings.decode_ms

    duration = audio_duration_seconds(environment.ffprobe, request.audio_path)
    report.audio_duration_sec = duration
    if timings.total_ms and duration:
        report.realtime_factor = round((timings.total_ms / 1000.0) / duration, 4)

    # A successful exit that used no GPU device WHILE a GPU backend is configured
    # is the SILENT CPU fallback: exit code 0, no error, hours of CPU work.
    silent_fallback = (
        not request.no_gpu and gpu_configured and device.device != environment.backend
    )
    report.silent_cpu_fallback = silent_fallback
    if silent_fallback and not fallback_reason:
        fallback_reason = f"whisper exited 0 but used the CPU ({device.reason})"
    report.fallback_reason = fallback_reason
    if fallback_reason:
        process.log(f"CPU fallback: {fallback_reason}", "warn")

    if report.realtime_factor:
        process.log(
            f"  device={device.device} totalMs={timings.total_ms} "
            f"realtimeFactor={report.realtime_factor}"
        )
    else:
        process.log(f"  device={device.device} (timings unavailable)")

    # GPU POLICY: if a GPU backend is configured for this install and the user did
    # not opt out, the run MUST have used the GPU. Both failure shapes are covered:
    # the backend cannot initialise at all (the missing-cuBLAS case, which exits
    # 0), and the backend initialised but no device was selected.
    violation_reason: str | None = None
    if gpu_required:
        if not gpu_capable:
            violation_reason = (
                f"the '{environment.backend}' backend cannot initialise on this "
                "machine, so whisper would run on the CPU"
            )
        elif not device.device_selected and not device.backend_initialised:
            violation_reason = (
                f"whisper ran on '{device.device}' but the configured backend is "
                f"'{environment.backend}'"
            )
        elif not device.device_selected and device.backend_initialised:
            # Ambiguous: the backend loaded but no device-selected line was seen.
            # That usually means a banner-format difference rather than a CPU run,
            # so failing by default could reject a healthy run. It is reported as
            # deviceVerified=false with a loud warning; -StrictGpu upgrades exactly
            # this case to a hard failure.
            if request.strict_gpu:
                violation_reason = (
                    f"the '{environment.backend}' backend initialised but no device "
                    "selection was observed, so GPU use is unproven (-StrictGpu)"
                )
            else:
                process.log(
                    f"the '{environment.backend}' backend initialised but no device "
                    "selection was observed in the log; GPU use is unproven "
                    "(deviceVerified=false, silentCpuFallback=true). "
                    "Pass -StrictGpu to make this a failure.",
                    "warn",
                )
                report.silent_cpu_fallback = True

    # Diagnostics must outlive the scratch dir: the one file that explains a
    # fallback must not be deleted along with the work dir.
    preserved_log: str | None = None
    if silent_fallback or fallback_reason or violation_reason:
        preserved_log = preserve_logs(output_base, whisper_log, retry_log)
    report.log_path = preserved_log

    report.log = [
        line
        for line in log_lines
        if any(
            token in line
            for token in ("load_backend", "ggml_cuda_init", "using CUDA", "use gpu",
                          "backend_init_gpu", "error", "cannot", "fail")
        )
    ][:12]

    if not request.keep_work:
        if not paths.remove_work_dir(work):
            process.log(f"  scratch dir left behind (busy): {work}")

    if violation_reason:
        suffix = f" Whisper log: {preserved_log}" if preserved_log else ""
        raise ZoombieError(
            f"GPU policy violation: {violation_reason}. A GPU backend is configured "
            "and usable, so the toolchain refuses to report success from a CPU run; "
            "pass -NoGpu to force the CPU deliberately, or -AllowCpuFallback to "
            f"permit it.{suffix}"
        )

    report.gpu_attempt_wall_ms = gpu_attempt_wall_ms
    return report


def guard_overwrite(output_base: str, force: bool) -> None:
    """Refuse to overwrite an existing transcript unless ``-Force`` was given.

    The same guard ``extract`` and ``readpdf`` already apply to their own output.
    Without it a re-run replaced ``<base>.txt`` -- the artifact a caller is most
    likely to have post-processed -- and reported success anyway.
    """
    existing = f"{output_base}.txt"
    if paths.is_file(existing) and not force:
        raise ZoombieError(f"Output exists (use -Force to overwrite): {existing}")


def source_metadata(request: Request, report: Report) -> dict:
    """The origin payload written to ``<base>.source.json``.

    This is what lets the summarize pass fill its "source" block (the origin link)
    for a video whose local file was DELETED after download, which is pipeline's
    default behaviour. Every field is either captured by the download stage,
    measured by this run, or ``null``; nothing here is guessed.
    """
    return {
        "kind": request.source_kind,
        # Falling back to the local path keeps the sidecar useful (and honest) for
        # a plain transcribe of an audio file, where there is no URL at all.
        "url": request.source_url or request.audio_path,
        "title": request.source_title,
        "id": request.source_id,
        "durationSec": report.audio_duration_sec,
        "language": request.language,
        "model": report.model,
        "backend": report.backend,
        "deviceUsed": report.device_used,
        "deviceVerified": report.device_selected,
        "realtimeFactor": report.realtime_factor,
        "toolchainVersion": SKILL_VERSION,
        "createdAt": process.utc_now_iso(),
        # The explicit record of whether the media this came from still exists.
        "sourceKept": request.source_kept,
    }


def write_source_sidecar(
    output_base: str,
    request: Request,
    report: Report,
    *,
    extension_count: int,
) -> dict | None:
    """Write ``<base>.source.json`` beside the artifacts. Best-effort, never fatal.

    Same error discipline as :func:`preserve_logs`: a sidecar that cannot be
    written is a warning, and the transcript stays the deliverable. ``-NoSrt``
    removes ``.srt`` from ``output_base``'s suffixes, so the length check is
    computed from the suffixes actually produced rather than a fixed worst case.
    """
    output_dir = os.path.dirname(output_base)
    if output_dir:
        paths.ensure_dir(output_dir)
    destination = f"{output_base}.source.json"
    try:
        # "..source.json" = 13 characters for the shortest case (TXT + sidecar);
        # add 4 more for the ".srt" suffix when it is produced.
        slack = 13 + (0 if extension_count <= 1 else 4)
        paths.assert_fits(destination, "The origin-metadata sidecar path", slack=slack)
        with open(paths.to_extended(destination), "w", encoding="utf-8") as handle:
            json.dump(source_metadata(request, report), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        size = paths.file_size(destination)
    except (OSError, ValueError, paths.PathTooDeepError) as exc:
        process.log(f"could not write the origin-metadata sidecar: {exc}", "warn")
        return None
    return {"path": destination, "size": size}


def preserve_logs(output_base: str, whisper_log: str, retry_log: str) -> str | None:
    """Copy the whisper logs beside the transcript. Best-effort, never fatal."""
    output_dir = os.path.dirname(output_base)
    if output_dir:
        paths.ensure_dir(output_dir)
    preserved = f"{output_base}.whisper.log"
    try:
        # The preserved log carries the longest appended suffix of any output, and
        # it is exactly the diagnostic a fallback needs, so losing it to the path
        # limit would hide the very failure it documents.
        paths.assert_fits(preserved, "The preserved whisper log path")
        paths.copy_file(whisper_log, preserved)
        if paths.is_file(retry_log):
            paths.copy_file(retry_log, f"{output_base}.whisper.retry.log")
    except (OSError, paths.PathTooDeepError) as exc:
        process.log(f"could not preserve the whisper log: {exc}", "warn")
        return None
    return preserved


def resolve_output_base(source: str, output: str | None) -> str:
    """Default the output basename from the source, dropping any extension."""
    base = output or os.path.join(os.getcwd(), paths.without_extension(os.path.basename(source)))
    base = paths.absolute(base)
    if paths.extension_of(base):
        base = paths.without_extension(base)
    return base

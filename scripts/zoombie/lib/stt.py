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
from ..item import paths as item_paths
from . import cublas, env as env_mod, paths, process, scratch, slides, srt as srt_mod, whisper
from .errors import StepFailedError, ZoombieError


# Containers whisper.cpp cannot read: it consumes WAV and little else, so a video
# handed to ``transcribe`` failed DEEP inside whisper -- exit 0, no transcript, and
# a success report. ``transcribe`` does not convert; ``pipeline`` is the command
# that extracts a 16 kHz mono WAV first. Refusing these up front names the remedy
# instead of failing opaquely. Audio containers whisper MIGHT read (.wav, .mp3,
# .m4a, .flac, .ogg, .opus, .aac) are deliberately NOT listed: the post-run "no
# .txt produced" check is their backstop.
VIDEO_CONTAINERS = frozenset({".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"})


def refuse_video_input(audio_path: str) -> None:
    """Refuse a known video container, naming ``pipeline`` as the fix.

    ``transcribe`` copies the input into its work dir and hands it straight to
    whisper.cpp; a video therefore produced no transcript while still reporting
    success. Using ``transcribe`` on a video is a documented misuse, so the tool
    says so rather than failing opaquely. The check is on the extension only --
    using :func:`zoombie.lib.paths.extension_of` -- so it costs nothing and never
    fires for the WAV ``pipeline`` produces before it calls this stage.
    """
    extension = (paths.extension_of(audio_path) or "").lower()
    if extension in VIDEO_CONTAINERS:
        raise ZoombieError(
            f"transcribe reads audio only, but the input is a video container "
            f"({extension}): {audio_path}. transcribe does not convert video; run "
            "'pipeline' on this file instead -- it extracts a 16 kHz mono WAV "
            "before transcribing."
        )


@dataclass
class Request:
    """Everything one transcription needs, independent of the CLI."""

    audio_path: str
    output_base: str
    # The item folder the artifacts belong to. The stage ALWAYS writes into
    # ``<item_dir>/.data/`` when this is set, which is what keeps a transcript
    # beside its siblings rather than flat at the item root. Empty means a direct
    # programmatic caller that did not name an item, and the flat ``output_base``
    # is used instead.
    item_dir: str = ""
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
    # True when ``audio_path`` is an EXTRACTED SCRATCH file rather than the
    # user's own source. ``pipeline`` extracts a 16 kHz WAV into its work dir and
    # deletes it at the end, so recording that path as the origin produced a
    # ``url`` pointing at a file that no longer exists -- the origin sidecar
    # carrying a dead temp path instead of an honest ``null``. A bare
    # ``transcribe`` of a real audio file leaves this False and keeps recording
    # the input path.
    audio_is_scratch: bool = False
    # A time WINDOW of the input, as the caller typed it (``HH:MM:SS``/``MM:SS``/
    # seconds). ``from_time`` None and ``to_time`` None is the full-file path, and
    # that path must stay byte-identical: the artifact names and argv are not
    # touched when no window is given. Parsed and validated by
    # :func:`checked_window`; kept as the raw strings too so the artifact TAG
    # preserves what the caller typed (see :func:`window_tag`).
    from_time: str | None = None
    to_time: str | None = None
    source_title: str | None = None
    source_id: str | None = None
    source_kind: str = "video"
    # True when the caller RETAINED the media beside the transcript -- the item
    # contract's normal case. False stays valid and meaningful: a URL-only item, or
    # a media name too long to place. The sidecar exists so that either state is
    # legible without the file.
    source_kept: bool = False
    # Name of the media file when it WAS retained, so a reader can find it without
    # globbing the item folder for a media extension.
    source_file: str | None = None
    # Why the media was NOT retained, when that was a DELIBERATE decision rather
    # than a failure. ``pipeline`` never duplicates a user-supplied local -Source
    # (the user already owns the file where they put it), and the sidecar records
    # that reason so "not copied" reads as intent, not as a silent loss. Same shape
    # as ``urlReason``. Stays ``None`` when the source WAS retained, and when the
    # absence was a failure (a missing file, an over-budget destination).
    source_reason: str | None = None


@dataclass
class Report:
    """The outcome, already shaped for the CLI's JSON ``data`` object."""

    output_base: str
    # Echoed so a caller can see which item folder received the artifacts.
    item_dir: str = ""
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
    # Decoder repetition loops found in the produced SRT. A REPORT, never a
    # failure: a loop is evidence about the transcript, and the caller's job is to
    # surface it (the Crimson run left a 10.5-minute loop unreported).
    #
    # The check runs on a WINDOW's SRT too, so a window that STILL loops says so:
    # that is the signal to escalate to a different model, and swallowing it would
    # defeat the point of the feature.
    repetitions: list[dict] = field(default_factory=list)
    # ``{"from": float, "to": float|None}`` when a window was decoded, else None.
    # Echoed into the result JSON so a caller sees exactly what range was decoded.
    range_spec: dict | None = None
    # The scratch lifecycle for this run (plan §10): ``{path, kept, removed,
    # leftover}``. Reported rather than left to prose, so a caller can verify the
    # run cleaned up after itself (``leftover: false``) or that -KeepScratch kept
    # the dir deliberately.
    scratch: dict | None = None

    def to_data(self) -> dict:
        """The documented camelCase result shape."""
        return {
            "outputBase": self.output_base,
            "itemDir": self.item_dir or None,
            # The decoded window, or null for a full-file run. A caller must be
            # able to see WHICH range a set of artifacts covers without inferring
            # it from the file name.
            "range": self.range_spec,
            "artifacts": self.artifacts,
            "backend": self.backend,
            # The model ACTUALLY used. Surfaced so a caller can tell a default
            # downgrade (``ggml-small``) from a deliberate choice: the transcript
            # quality depends on it, and it was previously invisible.
            "model": os.path.basename(self.model) if self.model else None,
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
            # Empty list, never null: the count is the thing a caller acts on, and
            # "no loops" and "not checked" must be distinguishable (the check always
            # runs, so the empty list means checked-and-clean).
            "repetitions": self.repetitions,
            "scratch": self.scratch,
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


# The one audio format whisper.cpp consumes, matching ``extract.SAMPLE_RATE`` /
# ``pipeline``'s inline ffmpeg call. Duplicated as a literal here so this module
# needs no dependency on a command module (the dependency runs the other way).
WINDOW_SAMPLE_RATE = 16000


def _read_text(path: str) -> str:
    """Read a produced artifact as text, keeping its own line endings."""
    with open(paths.to_extended(path), "r", encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def _slice_to_wav(
    environment: env_mod.Env, source: str, destination: str,
    start: float, end: float | None,
) -> None:
    """Slice ``[start, end)`` of ``source`` into a 16 kHz mono WAV.

    Mirrors ``pipeline``'s ffmpeg call exactly (``-vn -ac 1 -ar 16000 -c:a
    pcm_s16le``); ``-ss`` BEFORE ``-i`` is the fast input seek the slide
    extractor also uses, and every dropped frame is silently ignored for an audio
    slice. ``end`` omitted means "to the end of the input", which is why ``-to``
    is simply absent rather than a huge number.
    """
    ffmpeg = environment.require("ffmpeg", "ffmpeg")
    argv = [
        ffmpeg, "-y", "-hide_banner", "-nostats", "-v", "error",
        "-ss", f"{start:.3f}",
        "-i", source,
    ]
    if end is not None:
        argv += ["-to", f"{end:.3f}"]
    argv += [
        "-vn", "-ac", "1", "-ar", str(WINDOW_SAMPLE_RATE),
        "-c:a", "pcm_s16le", destination,
    ]
    process.run_checked(argv, what="ffmpeg (window slice)")


def checked_window(request: Request) -> tuple[float, float | None] | None:
    """Parse and validate ``request``'s window; ``None`` for a full-file run.

    The times are validated with :func:`zoombie.lib.slides.parse_time` -- the one
    timestamp parser the toolchain already owns -- so ``HH:MM:SS``, ``MM:SS`` and
    plain seconds are all accepted, and ``02:03`` can never be read as two hours.

    A ``-To`` earlier than ``-From`` is REFUSED here rather than passed on: an
    inverted window would decode the whole file from ``From`` to the end and look
    like a silently empty result, which is the failure mode the brief names.
    """
    if not request.from_time and not request.to_time:
        return None
    start = slides.parse_time(request.from_time or "0")
    end = slides.parse_time(request.to_time) if request.to_time else None
    if end is not None and end < start:
        raise ZoombieError(
            f"-To ({request.to_time} = {end:g}s) is earlier than -From "
            f"({request.from_time} = {start:g}s); refusing to run. An inverted "
            "window would decode the whole file from -From and read as a silently "
            "empty result."
        )
    return start, end


def window_tag(request: Request, start: float) -> str:
    """The file-name tag for a window: ``<start> - <end|end>``.

    Preserves what the CALLER typed (``00-46-30`` from ``00:46:30``, not the
    parsed second), because that is the timestamp the user will search for, and
    ``end`` is the literal ``end`` when ``-To`` was omitted -- a caller who gave
    only ``-From`` must be able to tell that window from one that named both
    bounds. Only ``:`` is replaced (with ``-``); every other character is kept.
    """
    frm = (request.from_time or "0").replace(":", "-")
    to = (request.to_time or "end").replace(":", "-")
    return f"{frm} - {to}"


def window_output_base(item_dir: str, request: Request, start: float) -> str:
    """``<item>/.data/transcript - <from> - <to>`` -- the window's base.

    The artifacts still hang off :func:`transcript_base`, so the item layout and
    the path budget are unchanged; only the suffix is qualified. That is what
    stops a window's ``transcript.txt`` from OVERWRITING the full transcript --
    the two are distinguished by name, not by a directory, so no new directory is
    introduced. The full-file path never reaches this function.
    """
    return f"{transcript_base(item_dir)} - {window_tag(request, start)}"


def request_output_base(request: Request) -> str:
    """The base a request's artifacts are written to, window included.

    ONE expression read by the writer AND by the early guard in the command, so a
    window run cannot have its guard check ``transcript.txt`` while the writer
    writes ``transcript - <from> - <to>.txt`` -- the same "guard and writer must
    agree" invariant :func:`effective_output_base` encodes, extended to the
    window. With no window this is exactly :func:`effective_output_base`, byte for
    byte, so the full-file path is untouched.

    ``checked_window`` is called here, so an inverted window is refused before any
    base is computed and before any scratch dir exists.
    """
    base = effective_output_base(request.output_base, request.item_dir)
    window = checked_window(request)
    if window is None:
        return base
    return window_output_base(request.item_dir, request, window[0])


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

    # ``-nf`` / ``--no-fallback`` is DELIBERATELY never added, even though the
    # probe reports whether the build advertises it (``caps.no_fallback``).
    # ``--no-fallback`` disables whisper's TEMPERATURE FALLBACK, which is the
    # built-in defence AGAINST a repetition loop -- passing it would make the loop
    # worse, not better. It is probed so this absence can be asserted rather than
    # assumed. (An earlier draft listed it among the anti-loop knobs; that was
    # wrong and is withdrawn.)

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
    # Refused before anything is resolved or copied: a video cannot be read by
    # whisper.cpp, and ``pipeline`` -- which passes a WAV it produced -- never
    # reaches this with a video extension.
    refuse_video_input(request.audio_path)
    # Validate the window BEFORE anything is resolved or copied. An inverted
    # ``-To``/``-From`` is refused here, which is the check ``pipeline`` also gets
    # for free because it calls this function with whichever window it was given.
    window = checked_window(request)
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

    # The artifacts land in the item's ``.data/`` directory: ``output_base`` IS
    # ``<item>/.data/transcript``. The caller resolves that from ``-Output`` (see
    # :func:`resolve_output_base`).
    #
    # PRECEDENCE lives in :func:`effective_output_base` (``output_base`` wins over
    # ``item_dir``). Inverting the two was the split defect: ``pipeline`` and
    # ``transcribe`` pass BOTH a correctly resolved ``output_base`` AND a
    # non-empty ``item_dir``, so ``item_dir`` won and every artifact was written
    # FLAT off the item folder (``<item>.txt``, ``<item>.srt``,
    # ``<item>.source.json``) -- while the path budget and the overwrite guard
    # below validated/checked that shorter string instead of the file actually
    # written. Reading the precedence from ONE function is what lets the
    # ``pipeline`` guard and this writer agree.
    # A window gets a QUALIFIED base so its artifacts can never overwrite the full
    # transcript. Purely a name qualifier: the base is still exactly
    # ``<item>/.data/transcript`` plus `` - <from> - <to>``, so the item layout and
    # the path budget are untouched. With no window this is the original
    # ``output_base`` byte for byte -- the full-file behaviour must not shift.
    # This is the SAME expression the early guards in ``transcribe``/``pipeline``
    # read, so the guard and the writer can never name two different files.
    #
    # NO AUTOMATIC SPLICING: this feature produces and NAMES a window's artifacts;
    # deciding how (or whether) to merge them back into the full transcript is a
    # separate decision and is deliberately NOT done here. The caller gets the
    # range and the paths and decides.
    output_base = effective_output_base(request.output_base, request.item_dir)
    if window is not None:
        output_base = window_output_base(request.item_dir, request, window[0])
    # The longest suffix appended to the base is ``.source.json`` (12 characters) +
    # the dot, so the budget is checked with room for the sidecar.
    paths.assert_fits(output_base, "The transcribe output path", slack=13)

    # Refuse BEFORE any scratch dir is created: overwriting an existing transcript
    # used to happen silently, unlike ``extract``/``readpdf``, which guard.
    #
    # This is the SAME base ``pipeline`` guards at its own line, because both go
    # through :func:`effective_output_base` -- which encodes the Request
    # precedence once. When the two diverged, the pipeline's guard passed against
    # one path while the writer collided with another.
    guard_overwrite(output_base, request.force)

    report = Report(output_base=output_base, item_dir=request.item_dir,
                    backend=environment.backend, model=environment.model)
    if window is not None:
        report.range_spec = {"from": window[0], "to": window[1]}
        process.log(
            f"  window: from {window[0]:g}s to "
            f"{'the end of the input' if window[1] is None else f'{window[1]:g}s'} "
            f"-> {output_base}"
        )

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
    elif window is None:
        # The full-file path: copy the input into the ASCII work dir unchanged.
        # Unchanged byte for byte, which is what the no-window regression test
        # asserts -- a window must not alter this branch.
        copied = paths.copy_into_safe_work(request.audio_path, work)
        input_path = copied["input_path"]
    else:
        # A WINDOW: slice the input to a 16 kHz mono WAV inside the ASCII work dir
        # with the SAME ffmpeg parameters ``pipeline``/``extract`` use, so the
        # engine is handed exactly the format it is handed on the full-file path.
        # whisper then decodes the slice as a short file, which is what the
        # experiment showed resets the repetition loop.
        start, end = window
        sliced = os.path.join(work, "input.wav")
        _slice_to_wav(environment, request.audio_path, sliced, start, end)
        input_path = sliced
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
        f"  work={work}  backend={environment.backend}  gpuCapable={gpu_capable}"
    )
    # Named on its own line, deliberately: a silent model DOWNGRADE (an install
    # that defaulted to ggml-small) is a real cause of a worse transcript, and it
    # must be legible in the run's own output rather than only in the sidecar.
    process.log(f"  model={os.path.basename(environment.model or '')}")

    if request.dry_run:
        # -DryRun writes nothing -- including to disk -- so the scratch dir it just
        # created is removed here too, and reported (plan §10). A dry run that left
        # an empty <guid> under the work root would be exactly the litter the
        # CLI-owned lifecycle exists to remove.
        report.output_base = output_base
        report.scratch = scratch.report(work, kept=False)
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
            if window is not None and extension == "srt":
                # OFFSET THE TIMESTAMPS by the window start, so the window's SRT
                # carries the RECORDING's clock (``00:47:00``), not the slice's
                # (``00:00:00``). This is the whole point of the feature: a caller
                # can splice or compare without mental arithmetic. Only the SRT is
                # rewritten -- the -.txt is written with ``-nt`` and has no timings.
                shifted = srt_mod.shift_timestamps(_read_text(source), window[0])
                with open(paths.to_extended(destination), "w", encoding="utf-8", newline="") as handle:
                    handle.write(shifted)
            else:
                paths.copy_file(source, destination)
            artifacts[extension] = {"path": destination, "size": paths.file_size(destination)}
    report.artifacts = artifacts

    # NO SUCCESS WITHOUT A TRANSCRIPT. The .txt is the deliverable and is written
    # on EVERY successful run (-NoSrt removes only the .srt), so a missing .txt is
    # never a legitimate outcome. whisper.cpp can exit 0 having written nothing --
    # an undecodable stream (the video case is now refused up front) produces
    # exactly this -- and the run previously reported ok:true with the transcript
    # silently absent. The sidecar is deliberately NOT written here: a sidecar
    # describing a transcript that does not exist is the contradiction
    # (artifacts.srt==null alongside a live sidecar) this check exists to remove.
    if "txt" not in artifacts:
        read_failures = whisper.audio_read_failure(log_lines)
        failure_log = preserve_logs(output_base, whisper_log, retry_log)
        report.scratch = scratch.report(work, kept=request.keep_work)
        detail = (
            " Whisper's own log names the cause: " + " | ".join(read_failures)
            if read_failures
            else ""
        )
        suffix = (
            f" Whisper log: {failure_log}"
            if failure_log
            else " (the whisper log could not be preserved)"
        )
        raise StepFailedError(
            "whisper-cli exited 0 but produced no transcript (.txt); whisper.cpp "
            f"could not read the input audio.{detail}{suffix}"
        )

    # A repetition loop is a decoder FAILURE, not a property of the audio, and the
    # only reliable symptom is in the SRT the decode just produced. Checked here,
    # right after the copy, so the report carries it whether or not the caller goes
    # on to summarize. It NEVER fails the run: the deliverable is the report.
    srt_path = artifacts.get("srt") or {}
    if srt_path.get("path"):
        report.repetitions = srt_mod.repetition_runs(srt_mod.parse(srt_path["path"]))
        for loop in report.repetitions:
            process.log(
                f"repetition loop in the transcript: {loop['detail']} "
                f"(phrase: {loop['unit'][:60]!r})",
                "warn",
            )

    # Report the SRT slot explicitly even when -NoSrt suppressed it, so a caller
    # can tell "timings were not requested" from "the run failed to produce them".
    if "srt" not in artifacts:
        report.artifacts["srt"] = None

    # The origin sidecar is written LATER, after the timing block below: it reports
    # ``durationSec``, ``realtimeFactor``, ``deviceUsed`` and ``deviceVerified``,
    # and every one of those is only known once the log has been read. Writing it
    # at this point recorded them all as null/default -- exactly the false
    # negatives the sidecar exists to prevent.

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

    # CLI-owned cleanup (plan §10): the artifacts were copied out above, so the
    # scratch dir is removed unless -KeepScratch/-KeepWork retained it. A busy dir
    # (a killed child still holding a log) becomes a REPORTED leftover naming the
    # sweep verb, rather than a silent leak or a blocked run.
    scratch_block = scratch.report(work, kept=request.keep_work)
    report.scratch = scratch_block
    if scratch_block["leftover"]:
        process.log(
            f"  scratch dir left behind (busy): {work}; "
            "run `zoombie clean -CleanScratch` to remove it",
            "warn",
        )

    if violation_reason:
        suffix = f" Whisper log: {preserved_log}" if preserved_log else ""
        raise ZoombieError(
            f"GPU policy violation: {violation_reason}. A GPU backend is configured "
            "and usable, so the toolchain refuses to report success from a CPU run; "
            "pass -NoGpu to force the CPU deliberately, or -AllowCpuFallback to "
            f"permit it.{suffix}"
        )

    report.gpu_attempt_wall_ms = gpu_attempt_wall_ms

    # Written here, not earlier: ``source_metadata`` reports the duration, the
    # realtime factor and the device actually used, all of which are only known
    # after the timing block above. Writing before that point recorded
    # ``durationSec``/``realtimeFactor`` as null and the device as the default.
    report.artifacts["sidecar"] = write_source_sidecar(
        output_base, request, report, extension_count=len(extensions)
    )
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


def is_scratch_audio(path: str | None, work_root: str | None = None) -> bool:
    """True when ``path`` is our own deleted-per-run scratch, not a real source.

    whisper always runs on a copy inside ``<root>\\work\\<guid>`` (both
    ``transcribe`` and ``pipeline`` do this), and that directory is removed at the
    end of the run. Recording such a path as the sidecar's ``url`` therefore
    persists a link to a file that is already gone -- which is what the Crimson
    item's ``source.json`` did (``...\\work\\<guid>\\audio.wav``). Path-containment
    in the toolchain work folder is the tell; the explicit
    :attr:`Request.audio_is_scratch` flag covers a caller that stages its scratch
    elsewhere.
    """
    if not path:
        return False
    try:
        root = work_root or paths.env_path(paths.WORK_FOLDER)
        work = os.path.normcase(paths.absolute(root))
        target = os.path.normcase(paths.absolute(path))
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False
    return target == work or target.startswith(work + os.sep)


def source_metadata(request: Request, report: Report) -> dict:
    """The origin payload written to ``<base>.source.json``.

    This is what lets the summarize pass fill its "source" block (the origin link)
    for a video whose local file was DELETED after download, which is pipeline's
    default behaviour. Every field is either captured by the download stage,
    measured by this run, or ``null``; nothing here is guessed.

    ``url`` is ``null`` for a scratch-only source with the reason in ``urlReason``:
    a dead ``file://``-shaped temp path satisfies the schema while telling a
    reader nothing, and a reader who follows it finds nothing there. An honest
    ``null`` plus "why" is the better answer for an origin that was never the
    user's file in the first place.
    """
    scratch = request.audio_is_scratch or is_scratch_audio(
        request.audio_path, request.work_root
    )
    if request.source_url:
        url = request.source_url
        url_reason = None
    elif scratch:
        url = None
        url_reason = (
            "the input was an extracted scratch file under the toolchain work "
            "folder, removed at the end of the run, so there is no durable origin "
            "path; the retained media (if any) is named by sourceFile"
        )
    else:
        # A real local file the caller owns: the input path IS the honest origin.
        url = request.audio_path
        url_reason = None
    return {
        "kind": request.source_kind,
        "url": url,
        "urlReason": url_reason,
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
        # The explicit record of whether the media this came from still exists,
        # and its name when it does.
        "sourceKept": request.source_kept,
        "sourceFile": request.source_file,
        # Why the media was NOT kept, when that was a deliberate decision (a local
        # -Source is never duplicated). Same shape as urlReason.
        "sourceReason": request.source_reason,
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


def ensure_item_dir(output: str | None, item_dir: str) -> bool:
    """Create ``<item>/.data/`` when ``-Output`` named an item; return whether.

    The ORDERING CONTRADICTION, decided here: three skills document "transcribe ->
    then summarize", but a correct run created neither ``summary.md`` nor ``.data/``
    and :func:`zoombie.item.paths.is_item` requires one of the two -- so the folder
    was unrecognised by ``items``/``index`` until the summary existed, which does not
    fit the documented order. The choice is to **create ``.data/`` at ``-Output``
    time**, because:

    * the artifacts are about to be written INTO it, so creating it is not a new
      side effect, only an earlier one -- the directory was already an inevitability
      of the run;
    * recognition then follows the DOCUMENTED order rather than contradicting it, so
      the three skills need no behaviour inversion;
    * the alternative (document "``.data/`` appears only after the summary") leaves
      a correct intermediate state indistinguishable from a folder the user just
      dropped media into, which is exactly the ambiguity ``is_item`` exists to
      resolve.

    Only a directory the user explicitly named with ``-Output`` is created; the
    ``item_dir`` defaulted from the source file's own folder is NOT touched, so a
    bare transcribe of a loose audio file never litters its parent with a ``.data/``.
    """
    if not output:
        return False
    target = item_paths.data_dir(item_dir)
    paths.assert_fits(target, "The item's .data directory", slack=24)
    paths.ensure_dir(target)
    return True


def item_dir_for(output: str | None, source: str) -> str:
    """The item folder a transcription targets.

    ``-Output`` designates the item folder itself, not a basename: the artifacts
    are always written under ``<item>/.data/``. :func:`os.path.normpath` settles
    the spelling -- it drops a trailing separator so ``<ws>\\item`` and
    ``<ws>\\item\\`` name one folder, while PRESERVING a drive root (``C:\\``),
    which a naive ``rstrip`` would reduce to a drive-relative ``C:``. An omitted
    ``-Output`` defaults the item to the source file's own directory -- the natural
    "summarise this recording where it already lives" case.
    """
    chosen = output or os.path.dirname(paths.absolute(source))
    return os.path.normpath(paths.absolute(chosen))


def effective_output_base(output_base: str, item_dir: str) -> str:
    """The base a transcription's artifacts are ACTUALLY written to.

    One expression, called by :func:`transcribe` (which guards and writes) and by
    ``pipeline`` (which guards before ffmpeg runs), so the guard and the writer
    can never validate two different paths. ``output_base`` wins; ``item_dir`` is
    the fallback for a caller that named an item but no base.

    This is the reconciliation of the duplicate guard: the defect was not the
    guard itself but that the pipeline guarded ``base`` while ``transcribe``
    rewrote it to ``item_dir``. Both now read the same function.
    """
    return output_base or item_dir


def transcript_base(item_dir: str) -> str:
    """``<item>/.data/transcript`` -- the fixed base every artifact hangs off."""
    return item_paths.transcript_path(item_dir, "").rstrip(".")


def resolve_output_base(source: str, output: str | None) -> str:
    """Resolve the whisper output base for one transcription.

    The base is ALWAYS ``<item>/.data/transcript``: an item's transcript has a
    fixed name, because the item holds one source and a reader must be able to find
    it without knowing the source name. The historical flat form (``<base>.txt``
    beside ``summary.md``) is gone -- it is what scattered a transcript and its
    ``.srt`` at the item root, out of step with the documented ``.data/`` layout.

    Derived from :func:`item_dir_for` rather than defaulting separately, so the
    base the overwrite guard checks is exactly the base the artifacts are written
    to -- the two disagreeing is how a guard passes and the write still collides.
    """
    return transcript_base(item_dir_for(output, source))

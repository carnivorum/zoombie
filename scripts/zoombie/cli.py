"""The single entry point: parse, dispatch, emit one JSON line.

Contract:

* stdout carries exactly one JSON line: ``{ok, action, error, data, timestamp}``
* stderr carries all human-readable progress
* exit code 0 on success, 1 on failure

Option names use the single-dash capitalised spelling (``-Source``, ``-Output``),
with conventional lowercase aliases accepted too, so both ``-Source`` and
``--source`` work. Renaming them would churn every skill and both docs for no
benefit.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

from . import SKILL_VERSION
from .lib import next as next_mod, pdf, process, reading, slides
from .lib.errors import ZoombieError


@dataclass
class Outcome:
    """What a command returns to the CLI."""

    ok: bool = True
    data: dict = field(default_factory=dict)
    error: str | None = None


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-DryRun", "--dry-run", action="store_true", help="plan only; write nothing")
    parser.add_argument("-Force", "--force", action="store_true", help="overwrite existing outputs")


def _add_attach_limit(parser: argparse.ArgumentParser) -> None:
    """``-AttachLimit``: how many images a result may advertise for inline reading.

    The cap is a TRANSPORT guarantee (plan §9): a result never hands an agent more
    than this many image paths, because reading all of them is what recreated the
    413 (§3). The flag lets a caller RAISE it deliberately for a known-small set; it
    is never a way to smuggle a 96-frame deck into one result, and ``-Force`` does
    not affect it. Read via ``getattr`` because a programmatic caller (and the
    existing tests) may build a Namespace without it; omitting it -- or passing 0 --
    means the built-in :data:`zoombie.lib.next.DEFAULT_ATTACH_CAP`.
    """
    parser.add_argument(
        "-AttachLimit", "--attach-limit", dest="attach_limit", type=int, default=None,
        help=(
            "max image paths a result advertises for inline reading "
            f"(default: {next_mod.DEFAULT_ATTACH_CAP}); the excess stays on disk and "
            "is named in data.next.overAttach. Raise it only for a deliberately small set"
        ),
    )


def _add_no_reading_copy(parser: argparse.ArgumentParser) -> None:
    """``-NoReadingCopy``: advertise the PNG frames instead of the JPEG copies.

    The compressed reading copies (plan §12) are the default: a JPEG of each frame
    the agent may read inline, at the source resolution, so a session is not blown by
    full-size PNGs (§3). This flag turns them off for a caller that wants the PNG
    paths. Read via ``getattr`` so a Namespace built without it still defaults on.
    """
    parser.add_argument(
        "-NoReadingCopy", "--no-reading-copy", dest="no_reading_copy",
        action="store_true",
        help=(
            "do not write compressed reading copies; advertise the PNG frames "
            f"(default: a -q:v {reading.DEFAULT_QUALITY} JPEG per frame, escalating "
            f"to -q:v {reading.DENSE_QUALITY} above {reading.DENSE_NUMERIC_TOKENS} "
            "numeric tokens)"
        ),
    )


def _add_source_output(parser: argparse.ArgumentParser, output_help: str = "output file or basename") -> None:
    parser.add_argument("-Source", "--source", dest="source", required=True, help="source file or URL")
    parser.add_argument("-Output", "--output", dest="output", default=None, help=output_help)


def _add_work(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-WorkRoot", "--work-root", dest="work_root", default=None, help="scratch root")
    # -KeepScratch is the plan-§10 name; -KeepWork is the historical alias for the
    # same decision (retain this run's scratch under the toolchain root for
    # inspection). Both are accepted so no script or skill breaks.
    parser.add_argument(
        "-KeepWork", "--keep-work", dest="keep_work", action="store_true",
        help="keep the work dir (alias of -KeepScratch)",
    )
    parser.add_argument(
        "-KeepScratch", "--keep-scratch", dest="keep_scratch", action="store_true",
        help="retain this run's scratch dir under the toolchain root for inspection",
    )


def _add_model(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-Model", "--model", dest="model", default=None, help="model name or path")


def _add_gpu_policy(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-NoGpu", "--no-gpu", dest="no_gpu", action="store_true", help="force the CPU (-ng)")
    parser.add_argument("-NoFlashAttn", "--no-flash-attn", dest="no_flash_attn", action="store_true")
    parser.add_argument("-Threads", "--threads", dest="threads", type=int, default=0)
    parser.add_argument("-AllowCpuFallback", "--allow-cpu-fallback", dest="allow_cpu_fallback", action="store_true")
    parser.add_argument("-StrictGpu", "--strict-gpu", dest="strict_gpu", action="store_true")


def _add_time_window(parser: argparse.ArgumentParser) -> None:
    """``-From``/``-To``: decode only ``[From, To)`` of the input.

    A window is decoded as a SHORT FILE sliced out of the input (16 kHz mono WAV)
    and its artifacts are written under a window-qualified name, so they never
    overwrite the full transcript. ``-To`` omitted means "to the end of the input".
    ``HH:MM:SS``, ``MM:SS`` and plain seconds are all accepted.
    """
    parser.add_argument(
        "-From", "--from", dest="from_time", default=None,
        help="window start (HH:MM:SS, MM:SS or seconds); omit for the whole file",
    )
    parser.add_argument(
        "-To", "--to", dest="to_time", default=None,
        help="window end; omit for 'to the end of the input'",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zoombie",
        description="Deterministic Windows text-extraction toolchain (download, extract, transcribe, PDF).",
    )
    parser.add_argument("--version", action="version", version=f"zoombie {SKILL_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- doctor -----------------------------------------------------------
    subparsers.add_parser("doctor", help="report tool status")

    # --- clean ------------------------------------------------------------
    # Default: remove the per-job ``<WorkRoot>\\<guid>`` dirs. -CleanScratch: sweep
    # EVERY toolchain-owned scratch dir under both roots (work + tmp), which is the
    # verb a killed run's leftover needs. Ownership is by run marker, so a user
    # artifact that happens to sit in a root is reported in ``spared``, never deleted.
    clean = subparsers.add_parser("clean", help="remove scratch dirs")
    clean.add_argument("-WorkRoot", "--work-root", dest="work_root", default=None)
    clean.add_argument("-DryRun", "--dry-run", action="store_true")
    clean.add_argument(
        "-CleanScratch", "--clean-scratch", dest="clean_scratch", action="store_true",
        help=(
            "sweep every toolchain-owned scratch dir under the toolchain root "
            "(work + tmp), e.g. the leftover of a killed run; user folders in a "
            "root are reported, never removed"
        ),
    )

    # --- download ---------------------------------------------------------
    download = subparsers.add_parser("download", help="download a video or its audio")
    _add_source_output(download)
    download.add_argument("-DownloadDir", "--download-dir", dest="download_dir", default=None)
    download.add_argument("-AudioOnly", "--audio-only", dest="audio_only", action="store_true")
    download.add_argument("-Format", "--format", dest="format", default="wav",
                          choices=["wav", "mp3", "m4a", "flac"])
    _add_common(download)

    # --- extract ----------------------------------------------------------
    extract = subparsers.add_parser("extract", help="extract audio from a video")
    _add_source_output(extract)
    extract.add_argument("-Format", "--format", dest="format", default="wav",
                         choices=["wav", "mp3", "m4a", "flac"])
    _add_common(extract)

    # --- unpack -----------------------------------------------------------
    # The generic NSIS/7z capability: an installer (or archive) is DATA, never an
    # executed program. -Check lists the archive without extracting; -DryRun
    # extracts to scratch and reports the real file list, writing nothing to the
    # destination. Installer scaffolding ($PLUGINSDIR) is excluded by directory.
    unpack_cmd = subparsers.add_parser(
        "unpack", help="extract an NSIS installer or a 7z archive into a folder"
    )
    _add_source_output(
        unpack_cmd,
        output_help=(
            "the folder that receives the extracted files (required; the "
            "destination is copied to, never the scratch dir)"
        ),
    )
    unpack_cmd.add_argument(
        "-Strip", "--strip", dest="strip", type=int, default=0,
        help="drop this many leading path levels from every entry (default: 0)",
    )
    unpack_cmd.add_argument(
        "-Include", "--include", dest="include", action="append", default=None,
        help=(
            "only take entries matching this glob; repeatable, and matched against "
            "the full path AND the base name (e.g. -Include *.dll -Include tessdata/*)"
        ),
    )
    unpack_cmd.add_argument(
        "-Check", "--check", action="store_true",
        help="list the archive's contents; extract nothing and write nothing",
    )
    unpack_cmd.add_argument(
        "-KeepWork", "--keep-work", dest="keep_work", action="store_true",
        help="keep the scratch dir the payload was extracted into",
    )
    unpack_cmd.add_argument(
        "-KeepScratch", "--keep-scratch", dest="keep_scratch", action="store_true",
        help="alias of -KeepWork: retain the extraction scratch dir",
    )
    _add_common(unpack_cmd)

    # --- transcribe -------------------------------------------------------
    transcribe = subparsers.add_parser("transcribe", help="transcribe audio to text")
    _add_source_output(
        transcribe,
        output_help=(
            "the item folder that receives the transcripts; the artifacts land in "
            "<item>/.data/ as transcript.txt, transcript.srt, source.json "
            "(default: the source file's folder)"
        ),
    )
    _add_model(transcribe)
    _add_work(transcribe)
    _add_gpu_policy(transcribe)
    transcribe.add_argument("-Language", "--language", dest="language", default="auto")
    transcribe.add_argument(
        "-Srt", "--srt", action="store_true",
        help="accepted no-op: .srt subtitles are emitted by default",
    )
    transcribe.add_argument(
        "-NoSrt", "--no-srt", dest="no_srt", action="store_true",
        help="suppress the .srt subtitles (the timings are then lost)",
    )
    _add_time_window(transcribe)
    _add_common(transcribe)

    # --- pipeline ---------------------------------------------------------
    pipeline = subparsers.add_parser("pipeline", help="download, extract and transcribe in one call")
    _add_source_output(
        pipeline,
        output_help=(
            "the item folder; the transcripts land in <item>/.data/ and the retained "
            "media at the item root (default: the source file's folder)"
        ),
    )
    _add_model(pipeline)
    _add_work(pipeline)
    _add_gpu_policy(pipeline)
    pipeline.add_argument("-DownloadDir", "--download-dir", dest="download_dir", default=None)
    pipeline.add_argument("-Language", "--language", dest="language", default="auto")
    pipeline.add_argument(
        "-Srt", "--srt", action="store_true",
        help="accepted no-op: .srt subtitles are emitted by default",
    )
    pipeline.add_argument(
        "-NoSrt", "--no-srt", dest="no_srt", action="store_true",
        help="suppress the .srt subtitles (the timings are then lost)",
    )
    # A window is reachable from the pipeline too, but only meaningfully for a
    # LOCAL source: a URL is downloaded in full and then sliced. It composes with
    # the same flags as ``transcribe``.
    _add_time_window(pipeline)
    # NOTE: the pipeline deliberately exposes no -Format. Its audio is a 16 kHz
    # mono WAV because that is the only thing whisper.cpp consumes; the flag used
    # to be registered here and never read. -Format lives on download/extract,
    # where it is honoured.
    _add_common(pipeline)

    # --- readpdf ----------------------------------------------------------
    readpdf = subparsers.add_parser("readpdf", help="convert a PDF to Markdown")
    _add_source_output(readpdf)
    _add_work(readpdf)
    readpdf.add_argument("-Ocr", "--ocr", action="store_true", help="OCR scanned pages with Tesseract")
    readpdf.add_argument(
        "-Vision", "--vision", dest="vision_dir", default=None,
        help=(
            "render pages with NO text layer to PNG in this directory for a vision "
            "reader (text pages are never rendered; ignored when -Ocr is given)"
        ),
    )
    readpdf.add_argument(
        "-Dpi", "--dpi", dest="dpi", type=int, default=200,
        help="render dpi for -Vision (default: 200)",
    )
    readpdf.add_argument("-Images", "--images", action="store_true", help="also extract embedded images")
    readpdf.add_argument(
        "-ImagesOnly", "--images-only", dest="images_only", action="store_true",
        help="extract images and the sidecar only; render no Markdown (no .md guard)",
    )
    readpdf.add_argument(
        "-ImageDir", "--image-dir", dest="image_dir", default=None,
        help=(
            "explicit image directory (default: <item>/.data/img when the output "
            "folder is an item, else <base>.images)"
        ),
    )
    readpdf.add_argument(
        "-MinPx", "--min-px", dest="min_px", type=int, default=pdf.DEFAULT_MIN_PX,
        help=f"drop images below this pixel size (default: {pdf.DEFAULT_MIN_PX})",
    )
    readpdf.add_argument(
        "-MinPt", "--min-pt", dest="min_pt", type=int, default=pdf.DEFAULT_MIN_PT,
        help=f"drop images below this on-page size in points (default: {pdf.DEFAULT_MIN_PT})",
    )
    readpdf.add_argument("-Pages", "--pages", dest="pages", default=None, help="page range, e.g. 1-5,8")
    # Default is DERIVED, not hard-coded: a sibling transcript's script decides
    # (Cyrillic -> eng+rus, Latin -> eng, none -> eng+rus). -Lang still overrides.
    readpdf.add_argument(
        "-Lang", "--lang", dest="lang", default=None,
        help="Tesseract language code (default: derived from a transcript, else eng+rus)",
    )
    _add_no_reading_copy(readpdf)
    _add_attach_limit(readpdf)
    _add_common(readpdf)

    # --- readimages -------------------------------------------------------
    # The image-input half of images-to-md: a loose image or a folder of them.
    # Without -Ocr it records the images and their sidecar for a VISION reader
    # and writes no text; with -Ocr it runs Tesseract. It never guesses text.
    readimages = subparsers.add_parser("readimages", help="turn images into Markdown (OCR or vision)")
    _add_source_output(readimages)
    readimages.add_argument("-Ocr", "--ocr", action="store_true", help="read the images with Tesseract")
    readimages.add_argument(
        "-Lang", "--lang", dest="lang", default=None,
        help="Tesseract language code (default: derived from a transcript, else eng+rus)",
    )
    readimages.add_argument(
        "-ImageDir", "--image-dir", dest="image_dir", default=None,
        help="where to write the sidecar (default: <base>.images)",
    )
    _add_attach_limit(readimages)
    _add_common(readimages)

    # --- slides -----------------------------------------------------------
    # Extract slide frames from a video plus a placement manifest, so
    # ``postprocess`` can embed them in ``summary.md`` block 6. Two modes:
    # exact -Times (one frame each, the minimal set) or auto-detect with a
    # perceptual-hash dedup. The narration spoken during each slide's interval
    # becomes the manifest's anchor_text, which is the join to the audio.
    slides_cmd = subparsers.add_parser("slides", help="extract slides from a video with a manifest")
    _add_source_output(slides_cmd)
    _add_work(slides_cmd)
    slides_cmd.add_argument(
        "-ImageDir", "--image-dir", dest="image_dir", default=None,
        help="explicit image directory (default: <Output>/.data/img)",
    )
    slides_cmd.add_argument(
        "-Times", "--times", dest="times", default=None,
        help="exact slide timestamps, comma or newline separated (HH:MM:SS or SS)",
    )
    slides_cmd.add_argument(
        "-TimesFile", "--times-file", dest="times_file", default=None,
        help="read exact timestamps from a file, one per line",
    )
    slides_cmd.add_argument(
        "-Srt", "--srt", dest="srt", default=None,
        help="SRT used for the slide anchor text (default: <item>/.data/transcript.srt)",
    )
    slides_cmd.add_argument(
        "-Scale", "--scale", dest="scale", type=int, default=slides.DEFAULT_SCALE_WIDTH,
        help=f"output frame width in px (default: {slides.DEFAULT_SCALE_WIDTH})",
    )
    slides_cmd.add_argument(
        "-SampleRate", "--sample-rate", dest="sample_rate", type=float,
        default=slides.DEFAULT_SAMPLE_RATE,
        help=(
            "auto-detect sampling cadence, frames per second -- BOUNDARY detection, "
            f"not the image count (default: {slides.DEFAULT_SAMPLE_RATE})"
        ),
    )
    slides_cmd.add_argument(
        "-DiffThreshold", "--diff-threshold", dest="diff_threshold", type=float,
        default=slides.DEFAULT_DIFF_THRESHOLD,
        help=(
            "mean grayscale diff (0-255) that starts a new slide -- the boundary "
            f"signal, because a dHash cannot see a fade (default: {slides.DEFAULT_DIFF_THRESHOLD})"
        ),
    )
    slides_cmd.add_argument(
        "-HashDistance", "--hash-distance", dest="hash_distance", type=int,
        default=slides.DEFAULT_HASH_DISTANCE,
        help=(
            "max perceptual-hash distance for 'same picture' -- DEDUP only now "
            f"(default: {slides.DEFAULT_HASH_DISTANCE})"
        ),
    )
    slides_cmd.add_argument(
        "-MinSlideSec", "--min-slide-sec", dest="min_slide_seconds", type=float,
        default=slides.DEFAULT_MIN_SLIDE_SECONDS,
        help=(
            "merge runs shorter than this many seconds "
            f"(default: {slides.DEFAULT_MIN_SLIDE_SECONDS})"
        ),
    )
    slides_cmd.add_argument(
        "-SampleIntervalSec", "--sample-interval-sec", dest="sample_interval", type=float,
        default=slides.DEFAULT_SAMPLE_INTERVAL_SECONDS,
        help=(
            "inside a run longer than this many seconds, keep one extra interior "
            f"sample per interval (default: {slides.DEFAULT_SAMPLE_INTERVAL_SECONDS})"
        ),
    )
    slides_cmd.add_argument(
        "-MinPx", "--min-px", dest="min_px", type=int, default=slides.DEFAULT_MIN_PX,
        help=f"drop frames below this pixel size (default: {slides.DEFAULT_MIN_PX})",
    )
    # The byte prefilter and the text threshold are BOTH non-destructive now: the
    # byte value only decides whether to spend an OCR call, and the text value is a
    # reporting flag. Neither removes a frame. The byte value sits inside the
    # measured gap between flat talking-head frames (200-470 KB) and text slides
    # (590-960 KB) on the Crimson item.
    slides_cmd.add_argument(
        "-MinFrameBytes", "--min-frame-bytes", dest="min_frame_bytes", type=int,
        default=slides.DEFAULT_MIN_FRAME_BYTES,
        help=(
            "skip the OCR call for a frame smaller than this many bytes (the frame "
            f"is KEPT; default: {slides.DEFAULT_MIN_FRAME_BYTES})"
        ),
    )
    slides_cmd.add_argument(
        "-MinTextChars", "--min-text-chars", dest="min_text_chars", type=int,
        default=slides.DEFAULT_MIN_TEXT_CHARS,
        help=(
            "flag (never drop) a frame whose OCR text is shorter than this as "
            f"'likely image-only' (default: {slides.DEFAULT_MIN_TEXT_CHARS})"
        ),
    )
    # Text is REPORTING, not a decision. OCR runs once per run and its text is
    # written to .data/ocr.json for the agent to score; -NoTextGate disables it.
    slides_cmd.add_argument(
        "-NoTextGate", "--no-text-gate", dest="no_text_gate", action="store_true",
        help="disable the per-run OCR reporting pass (keep frames, write no ocr.json)",
    )
    _add_no_reading_copy(slides_cmd)
    slides_cmd.add_argument(
        "-Lang", "--lang", dest="lang", default=None,
        help=(
            "Tesseract language for the OCR reporting pass "
            "(default: derived from a transcript, else eng+rus)"
        ),
    )
    _add_attach_limit(slides_cmd)
    _add_common(slides_cmd)

    # --- postprocess ------------------------------------------------------
    # The mechanical half of the summarize skill. Dry run by default: it writes
    # ONLY with -Apply, because a skill calls it and must not be able to corrupt
    # a document by accident.
    postprocess = subparsers.add_parser(
        "postprocess", help="assign anchors, timestamps, index and images in a summary.md"
    )
    postprocess.add_argument("-Md", "--md", dest="md", default=None, help="a single .md file")
    postprocess.add_argument("-Dir", "--dir", dest="dir", default=None, help="a folder of .md files")
    postprocess.add_argument(
        "-Recurse", "--recurse", action="store_true", help="recurse into subfolders (with -Dir)"
    )
    postprocess.add_argument(
        "-Srt", "--srt", dest="srt", default=None,
        help="SRT for the heading timestamps (default: <item>/.data/transcript.srt, "
             "or a sibling <base>.srt)",
    )
    postprocess.add_argument(
        "-ImageDir", "--image-dir", dest="image_dir", default=None,
        help="image directory to read manifest.json from (default: sibling img/)",
    )
    postprocess.add_argument(
        "-Apply", "--apply", action="store_true", help="write the changes (default: dry run)"
    )
    postprocess.add_argument(
        "-Report", "--report", dest="report", default=None, help="write the report JSON to this path"
    )
    _add_attach_limit(postprocess)

    # --- verify -----------------------------------------------------------
    verify = subparsers.add_parser("verify", help="check a produced summary tree (exit 1 on problems)")
    verify.add_argument("-Dir", "--dir", dest="dir", default=None, help="root folder to check")
    verify.add_argument(
        "-Recurse", "--recurse", action="store_true", help="recurse into subfolders"
    )
    verify.add_argument(
        "-Json", "--json", action="store_true", help="report shape only; details stay in data"
    )

    # --- index ------------------------------------------------------------
    # Regenerates the library README.md from the item folders. Dry run by
    # default for the same reason as postprocess: a skill calls it and must not
    # be able to overwrite the index by accident.
    index = subparsers.add_parser("index", help="regenerate the README.md index of a library")
    index.add_argument(
        "-Dir", "--dir", dest="dir", required=True, help="the library root folder"
    )
    index.add_argument(
        "-Output", "--output", dest="output", default=None,
        help="index path (default: <Dir>/README.md)",
    )
    index.add_argument("-Json", "--json", action="store_true", help="quiet; the scan stays in data")
    index.add_argument(
        "-DryRun", "--dry-run", action="store_true",
        help="accepted no-op: writing already requires -Apply",
    )
    index.add_argument(
        "-Apply", "--apply", action="store_true", help="write the index (default: dry run)"
    )

    # --- items ------------------------------------------------------------
    # Enumerate the items in a workspace. Defaults to the CURRENT directory
    # because that is what an agent has in hand, and returns the whole picture
    # in one JSON line so learning "what summaries exist here" costs one call
    # rather than a directory listing plus a document read per item.
    items = subparsers.add_parser(
        "items", help="scan a workspace for items and report them compactly"
    )
    items.add_argument(
        "-Root", "--root", dest="root", default=None,
        help="workspace root (default: the current directory)",
    )
    # ONE control for depth. ``default=None`` is deliberate: it is what makes "the
    # caller did not ask" distinguishable from an explicit ``-Depth 1``, and that
    # distinction is what the previous shape lacked -- a default baked into argparse
    # meant the flag was ignored unless a second flag was also given.
    items.add_argument(
        "-Depth", "--depth", dest="depth", type=int, default=None,
        help="levels below the root to search; 1 = direct children only (default: 1)",
    )
    items.add_argument(
        "-Recurse", "--recurse", action="store_true",
        help="alias for -Depth 2: also look inside non-item subfolders",
    )
    items.add_argument(
        "-Title", "--title", dest="title", default=None,
        help="propose folder names for a new item with this title ('|' separates)",
    )
    items.add_argument(
        "-Date", "--date", dest="date", default=None,
        help="ISO date for a proposed name (default: leave the date out)",
    )
    items.add_argument("-Json", "--json", action="store_true", help="quiet; the scan stays in data")

    # --- migrate ----------------------------------------------------------
    # Moves a pre-item-model library onto <item>/.data/. Dry run by default,
    # like index and postprocess, and for a stronger reason: this one moves the
    # user's files.
    migrate = subparsers.add_parser(
        "migrate", help="move a library onto the .data/ item layout (dry run by default)"
    )
    migrate.add_argument(
        "-Dir", "--dir", dest="dir", required=True, help="the library root folder"
    )
    migrate.add_argument("-Json", "--json", action="store_true", help="quiet; the plan stays in data")
    migrate.add_argument(
        "-Apply", "--apply", action="store_true",
        help="perform the moves (default: dry run)",
    )

    # --- modes ------------------------------------------------------------
    # Redeploys the Zoombie role into the global custom_modes.yaml without a full
    # setup, so a prompt edit can ship on its own. Dry run by default for the same
    # reason as postprocess/index: this edits a file the USER owns.
    modes = subparsers.add_parser(
        "modes", help="deploy the Zoombie role into the global Zoo Code custom modes"
    )
    modes.add_argument(
        "-Target", "--target", dest="target", default=None,
        help="explicit custom_modes.yaml (default: the global one)",
    )
    modes.add_argument(
        "-Check", "--check", action="store_true",
        help="report the planned action without writing",
    )
    modes.add_argument(
        "-Apply", "--apply", action="store_true", help="write the change (default: dry run)"
    )
    modes.add_argument(
        "-Force", "--force", action="store_true",
        help="rewrite even when the content already matches",
    )

    # --- mcp --------------------------------------------------------------
    # Registers the MCP facade (``python -m zoombie.mcp``) in the client's global
    # mcp_settings.json, so the server is available without a full setup and a
    # prompt-only change ships on its own. Dry run by default for the same reason
    # as modes: this edits a file the USER owns (and may hold other servers).
    mcp = subparsers.add_parser(
        "mcp", help="register the MCP server in the global Zoo Code MCP settings"
    )
    mcp.add_argument(
        "-Target", "--target", dest="target", default=None,
        help="explicit mcp_settings.json (default: the global one)",
    )
    mcp.add_argument(
        "-Check", "--check", action="store_true",
        help="report the planned action without writing",
    )
    mcp.add_argument(
        "-Apply", "--apply", action="store_true", help="write the change (default: dry run)"
    )
    mcp.add_argument(
        "-Force", "--force", action="store_true",
        help="rewrite even when the entry already matches",
    )

    return parser


def _dispatch(args: argparse.Namespace) -> Outcome:
    """Import the command module lazily, so `doctor` works without ffmpeg etc."""
    if args.command == "doctor":
        from .commands import doctor
        return doctor.run(args)
    if args.command == "clean":
        from .commands import clean
        return clean.run(args)
    if args.command == "download":
        from .commands import download
        return download.run(args)
    if args.command == "extract":
        from .commands import extract
        return extract.run(args)
    if args.command == "unpack":
        from .commands import unpack
        return unpack.run(args)
    if args.command == "transcribe":
        from .commands import transcribe
        return transcribe.run(args)
    if args.command == "pipeline":
        from .commands import pipeline
        return pipeline.run(args)
    if args.command == "readpdf":
        from .commands import readpdf
        return readpdf.run(args)
    if args.command == "readimages":
        from .commands import readimages
        return readimages.run(args)
    if args.command == "slides":
        from .commands import slides
        return slides.run(args)
    if args.command == "postprocess":
        from .commands import postprocess
        return postprocess.run(args)
    if args.command == "verify":
        from .commands import verify
        return verify.run(args)
    if args.command == "index":
        from .commands import library
        return library.run(args)
    if args.command == "items":
        from .commands import items as items_cmd
        return items_cmd.run(args)
    if args.command == "migrate":
        from .commands import migrate
        return migrate.run(args)
    if args.command == "modes":
        from .commands import modes
        return modes.run(args)
    if args.command == "mcp":
        from .commands import mcp as mcp_cmd
        return mcp_cmd.run(args)
    raise ZoombieError(f"unknown command: {args.command}")


def expand_arg_files(argv: list[str] | None = None) -> list[str]:
    """Resolve ``@path`` arguments to the stripped UTF-8 text of that file.

    Windows console code pages mangle non-ASCII (Cyrillic) ARGUMENTS: a Cyrillic
    ``-Output``/``-DownloadDir``/``-Source`` passed inline arrives corrupted
    whatever the shell's encoding is set to. The escape hatch is an ``@file``
    argument -- the value is read from a UTF-8 file by Python, so the console
    code page is never involved. This mirrors the ``@file`` convention other
    CLI tools use.

    Only a token that BEGINS with ``@`` is expanded, so an argument that merely
    CONTAINS ``@`` (an email, a URL with userinfo) is untouched. A path that does
    not exist is left verbatim, because a leading ``@`` is also a legal file name.
    """
    if argv is None:
        argv = sys.argv[1:]
    expanded: list[str] = []
    for token in argv:
        if token.startswith("@") and len(token) > 1:
            path = token[1:]
            if os.path.isfile(path):
                try:
                    # utf-8-sig so a Notepad-saved BOM does not become a leading
                    # \\ufeff in the value; strip the trailing newline a file ends
                    # with.
                    with open(path, "r", encoding="utf-8-sig") as handle:
                        token = handle.read().strip()
                except OSError:
                    pass
        expanded.append(token)
    return expanded


def main(argv: list[str] | None = None) -> int:
    """Parse, run one command, emit exactly one JSON result line."""
    parser = build_parser()
    args = parser.parse_args(expand_arg_files(argv))

    try:
        outcome = _dispatch(args)
    except ZoombieError as exc:
        process.write_result(args.command, ok=False, error=str(exc))
        return 1
    except KeyboardInterrupt:
        process.write_result(args.command, ok=False, error="interrupted")
        return 1
    except Exception as exc:  # noqa: BLE001 - the contract is a clean error line
        process.write_result(args.command, ok=False, error=f"{type(exc).__name__}: {exc}")
        return 1

    process.write_result(
        args.command,
        ok=outcome.ok,
        data=outcome.data,
        error=outcome.error,
    )
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())

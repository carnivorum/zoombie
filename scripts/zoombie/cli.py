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
from .lib import pdf, process, slides
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


def _add_source_output(parser: argparse.ArgumentParser, output_help: str = "output file or basename") -> None:
    parser.add_argument("-Source", "--source", dest="source", required=True, help="source file or URL")
    parser.add_argument("-Output", "--output", dest="output", default=None, help=output_help)


def _add_work(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-WorkRoot", "--work-root", dest="work_root", default=None, help="scratch root")
    parser.add_argument("-KeepWork", "--keep-work", dest="keep_work", action="store_true", help="keep the work dir")


def _add_model(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-Model", "--model", dest="model", default=None, help="model name or path")


def _add_gpu_policy(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-NoGpu", "--no-gpu", dest="no_gpu", action="store_true", help="force the CPU (-ng)")
    parser.add_argument("-NoFlashAttn", "--no-flash-attn", dest="no_flash_attn", action="store_true")
    parser.add_argument("-Threads", "--threads", dest="threads", type=int, default=0)
    parser.add_argument("-AllowCpuFallback", "--allow-cpu-fallback", dest="allow_cpu_fallback", action="store_true")
    parser.add_argument("-StrictGpu", "--strict-gpu", dest="strict_gpu", action="store_true")


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
    clean = subparsers.add_parser("clean", help="remove scratch dirs")
    clean.add_argument("-WorkRoot", "--work-root", dest="work_root", default=None)
    clean.add_argument("-DryRun", "--dry-run", action="store_true")

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
    readpdf.add_argument("-Lang", "--lang", dest="lang", default="eng", help="Tesseract language code")
    _add_common(readpdf)

    # --- readimages -------------------------------------------------------
    # The image-input half of images-to-md: a loose image or a folder of them.
    # Without -Ocr it records the images and their sidecar for a VISION reader
    # and writes no text; with -Ocr it runs Tesseract. It never guesses text.
    readimages = subparsers.add_parser("readimages", help="turn images into Markdown (OCR or vision)")
    _add_source_output(readimages)
    readimages.add_argument("-Ocr", "--ocr", action="store_true", help="read the images with Tesseract")
    readimages.add_argument("-Lang", "--lang", dest="lang", default="eng", help="Tesseract language code")
    readimages.add_argument(
        "-ImageDir", "--image-dir", dest="image_dir", default=None,
        help="where to write the sidecar (default: <base>.images)",
    )
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
        help=f"auto-detect sampling, frames per second (default: {slides.DEFAULT_SAMPLE_RATE})",
    )
    slides_cmd.add_argument(
        "-HashDistance", "--hash-distance", dest="hash_distance", type=int,
        default=slides.DEFAULT_HASH_DISTANCE,
        help=(
            "max perceptual-hash distance for 'same slide' "
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
        "-MinPx", "--min-px", dest="min_px", type=int, default=slides.DEFAULT_MIN_PX,
        help=f"drop frames below this pixel size (default: {slides.DEFAULT_MIN_PX})",
    )
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

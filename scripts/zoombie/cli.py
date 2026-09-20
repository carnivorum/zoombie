"""The single entry point: parse, dispatch, emit one JSON line.

Contract (unchanged from the PowerShell CLI, so no caller has to change):

* stdout carries exactly one JSON line: ``{ok, action, error, data, timestamp}``
* stderr carries all human-readable progress
* exit code 0 on success, 1 on failure

Option names keep their PowerShell spelling (``-Source``, ``-Output``, ...),
including ``-Source`` rather than ``-Input`` even though the reason for that
choice no longer applies. Renaming would churn five skills and two docs for no
benefit; lowercase aliases are accepted too, so both ``-Source`` and ``--source``
work.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from . import SKILL_VERSION
from .lib import process
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


def _add_source_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-Source", "--source", dest="source", required=True, help="source file or URL")
    parser.add_argument("-Output", "--output", dest="output", default=None, help="output file or basename")


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
    _add_source_output(transcribe)
    _add_model(transcribe)
    _add_work(transcribe)
    _add_gpu_policy(transcribe)
    transcribe.add_argument("-Language", "--language", dest="language", default="auto")
    transcribe.add_argument("-Srt", "--srt", action="store_true", help="also emit .srt subtitles")
    _add_common(transcribe)

    # --- pipeline ---------------------------------------------------------
    pipeline = subparsers.add_parser("pipeline", help="download, extract and transcribe in one call")
    _add_source_output(pipeline)
    _add_model(pipeline)
    _add_work(pipeline)
    _add_gpu_policy(pipeline)
    pipeline.add_argument("-DownloadDir", "--download-dir", dest="download_dir", default=None)
    pipeline.add_argument("-Language", "--language", dest="language", default="auto")
    pipeline.add_argument("-Srt", "--srt", action="store_true")
    pipeline.add_argument("-Format", "--format", dest="format", default="wav",
                          choices=["wav", "mp3", "m4a", "flac"])
    _add_common(pipeline)

    # --- readpdf ----------------------------------------------------------
    readpdf = subparsers.add_parser("readpdf", help="convert a PDF to Markdown")
    _add_source_output(readpdf)
    _add_work(readpdf)
    readpdf.add_argument("-Ocr", "--ocr", action="store_true", help="OCR scanned pages with Tesseract")
    readpdf.add_argument("-Images", "--images", action="store_true", help="also extract embedded images")
    readpdf.add_argument("-Pages", "--pages", dest="pages", default=None, help="page range, e.g. 1-5,8")
    readpdf.add_argument("-Lang", "--lang", dest="lang", default="eng", help="Tesseract language code")
    _add_common(readpdf)

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
    raise ZoombieError(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    """Parse, run one command, emit exactly one JSON result line."""
    parser = build_parser()
    args = parser.parse_args(argv)

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

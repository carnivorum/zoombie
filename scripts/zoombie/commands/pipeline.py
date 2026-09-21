"""``pipeline``: download (when given a URL), extract audio, transcribe.

The audio is extracted DIRECTLY into the ASCII work dir, so it never exists under
a non-ASCII path and whisper.cpp is isolated from the start. That is a stronger
guarantee than ``transcribe`` alone, which has to copy a possibly-Cyrillic input
into the work dir first.
"""

from __future__ import annotations

import os
import re

from ..cli import Outcome
from ..lib import env as env_mod, paths, process, stt, ytdlp
from ..lib.errors import StepFailedError, ZoombieError

URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


def _retain_source(video_path: str, output_base: str) -> str | None:
    """Copy the source media beside the transcript; return where, or ``None``.

    The item contract says the source sits at the item root next to ``summary.md``,
    so this is a COPY and not a move: the download directory is still cleaned up
    afterwards, and a failure to copy must not lose the original while the
    transcription is already written.

    Best effort by design. A media file name is content-controlled -- a video
    title may be long, non-ASCII, or both -- so the destination can exceed the
    Windows budget, and that must be a logged skip rather than a failed run whose
    transcript is already on disk. The caller records what happened in
    ``sourceKept``, so the absence is visible in the sidecar instead of silent.
    """
    if not paths.is_file(video_path):
        return None

    destination = os.path.join(os.path.dirname(output_base), os.path.basename(video_path))
    try:
        # Room for the extension and the Windows path budget; the NAME is not ours
        # to shorten, so an over-long one is refused rather than truncated.
        paths.assert_fits(destination, "The retained source path")
    except Exception as exc:  # noqa: BLE001 - any budget/length refusal is a skip
        process.log(f"  source not retained beside the transcript: {exc}", "warn")
        return None

    try:
        paths.copy_file(video_path, destination)
    except OSError as exc:
        process.log(f"  source not retained beside the transcript: {exc}", "warn")
        return None

    process.log(f"  source retained: {destination}")
    return destination


def _download(environment: env_mod.Env, args, log_dir: str) -> tuple[str, str, ytdlp.Origin | None]:
    """Download the source URL. Returns ``(video_path, owned_dir, origin)``.

    ``owned_dir`` is the temp directory this command created, or "" when the user
    supplied -DownloadDir and therefore owns the directory.

    ``origin`` is the source video's identity as reported by yt-dlp, captured from
    the SAME invocation as the download (``--print after_move:``). It has to be
    captured now because this command DELETES the media at the end of the run: once
    the file is gone, the URL would otherwise be unrecoverable, and the summarize
    pass would have no origin link to put in its "source" block.
    """
    python = environment.require("python", "python")

    if args.download_dir:
        directory = args.download_dir
        owned = ""
    else:
        # Default to a fresh temp dir rather than the shared work root: the video
        # is deleted right after transcription, so it must not sit in the work
        # root, nor (if the run fails) block anything else there.
        directory = paths.new_temp_dir("zoombie-pipeline")
        owned = directory

    paths.ensure_dir(directory)
    # Same content-controlled-title problem as `download`: keep the produced name
    # and the .part/.temp scratch names inside the Windows budget.
    paths.assert_fits(directory, "The pipeline download directory", slack=60)

    argv = [
        python, "-m", "yt_dlp",
        # -Force is forwarded: without it a re-run reused the cached download
        # while still reporting a fresh transcription of the OLD media.
        *ytdlp.download_args(directory, audio_only=False, force=args.force),
        *ytdlp.metadata_args(),
        args.source,
    ]
    # run_text (not run_checked) because stdout carries the metadata line: the
    # exit code is inspected here so a real download failure still raises.
    code, text = process.run_text(argv, timeout=None)
    if code != 0:
        raise StepFailedError(f"yt-dlp failed (exit {code})")
    origin = ytdlp.parse_metadata(text)
    if not origin:
        # Best-effort: a missing metadata line degrades the sidecar, it does not
        # fail a run whose media downloaded fine.
        process.log("yt-dlp reported no origin metadata for this source", "warn")

    produced = ytdlp.newest_download(directory)
    if not produced or not produced["file"]:
        raise ZoombieError("yt-dlp produced no file to transcribe")
    return produced["file"], owned, origin


def run(args) -> Outcome:
    environment = env_mod.resolve(args.model)

    is_url = bool(URL_PATTERN.match(args.source))
    video_path = args.source
    owned_dl_dir = ""
    downloaded: str | None = None
    origin: ytdlp.Origin | None = None

    if is_url:
        process.log("pipeline: download stage", "step")
        if args.dry_run:
            return Outcome(
                ok=True,
                data={"dryRun": True, "stage": "download", "url": args.source},
            )
        video_path, owned_dl_dir, origin = _download(environment, args, args.work_root or "")
        downloaded = video_path
        process.log(f"downloaded: {video_path}")
        if origin and origin.title:
            process.log(f"  source: {origin.title}")

    # Extract audio into the ASCII work dir directly.
    work_root = args.work_root or paths.env_path(paths.WORK_FOLDER)
    if not paths.is_ascii(work_root):
        raise ZoombieError(f"Work root must be ASCII: {work_root}")
    paths.assert_fits(work_root, "The work root (-WorkRoot)", slack=80)

    work = paths.new_ascii_dir(work_root)
    audio_out = os.path.join(work, "audio.wav")

    base = stt.resolve_output_base(video_path, args.output)
    # Room for ``.source.json`` (the longest appended suffix) plus the dot.
    paths.assert_fits(base, "The pipeline output path", slack=13)
    # ffmpeg and yt-dlp both take this path as a native argument, so only its
    # length can be validated: the extended prefix must not reach them.
    paths.assert_fits(video_path, "The downloaded video path")

    if args.dry_run:
        if not is_url:
            return Outcome(
                ok=True,
                data={"dryRun": True, "stage": "extract+transcribe",
                      "video": video_path, "outputBase": base, "work": work},
            )
        return Outcome(ok=True, data={"dryRun": True, "stage": "download", "url": args.source})

    try:
        # Checked here -- after the download but BEFORE ffmpeg -- so a refusal
        # still runs the finally that removes the media we just fetched.
        stt.guard_overwrite(base, args.force)

        ffmpeg = environment.require("ffmpeg", "ffmpeg")
        process.run_checked(
            [ffmpeg, "-y", "-hide_banner", "-nostats", "-v", "error",
             "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", audio_out],
            what="ffmpeg",
        )

        # REQUIREMENT: the source media is kept, beside ``summary.md`` at the item
        # root. This REVERSES the previous default, which deleted the download right
        # after transcription and recorded ``sourceKept: false`` -- the very case the
        # origin sidecar existed to survive.
        #
        # Retention is best effort, logged either way: a destination that cannot fit
        # the Windows budget must not fail a run whose transcription succeeded, and the
        # sidecar records what actually happened, so an item whose media was not kept
        # stays well-formed.
        kept_source = _retain_source(video_path, base)
    
        request = stt.Request(
            audio_path=audio_out,
            output_base=base,
            language=args.language,
            want_srt=args.srt,
            no_srt=args.no_srt,
            force=args.force,
            no_gpu=args.no_gpu,
            no_flash_attn=args.no_flash_attn,
            threads=args.threads,
            allow_cpu_fallback=args.allow_cpu_fallback,
            strict_gpu=args.strict_gpu,
            work_root=work_root,
            keep_work=args.keep_work,
            # Parity with transcribe: -DryRun must reach the transcription stage
            # too, not just the pipeline's own early returns.
            dry_run=args.dry_run,
            # Origin metadata, so the sidecar can name the deleted source.
            source_url=(origin.url if origin and origin.url else args.source) if is_url else None,
            source_title=origin.title if origin else None,
            source_id=origin.id if origin else None,
            source_kind="video" if is_url else "audio",
            # True when the media was actually placed beside the transcript; read
            # from what happened, not from what was requested.
            source_kept=bool(kept_source),
            source_file=os.path.basename(kept_source) if kept_source else None,
        )
        report = stt.transcribe(environment, request)
    finally:
        # Remove the DOWNLOAD FOLDER and the intermediate audio.wav. The media
        # itself survives in the item when it was copied out, which is the change:
        # only genuinely temporary material is cleaned up here. whisper's own
        # scratch was already cleaned inside stt.transcribe, and the download dir
        # goes even under -KeepWork, because -KeepWork now means "do not tidy the
        # work dir", not "delete the video".
        if owned_dl_dir:
            paths.remove_quietly(owned_dl_dir, recursive=True)
        paths.remove_quietly(work, recursive=True)

    return Outcome(ok=True, data=report.to_data())

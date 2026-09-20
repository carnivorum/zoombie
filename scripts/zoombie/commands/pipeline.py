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
            # The media is removed below unless -KeepWork, and that is exactly what
            # the sidecar exists to survive.
            source_kept=bool(args.keep_work),
        )
        report = stt.transcribe(environment, request)
    finally:
        # Remove the downloaded video (its transcript is the actual output), the
        # temp download dir when we created it, and this stage's work dir with the
        # intermediate audio.wav. whisper's own scratch was already cleaned inside
        # stt.transcribe. Removal failures are reported, not fatal: a silent
        # failure is how a download leak stays invisible.
        if not args.keep_work:
            if downloaded:
                paths.remove_quietly(downloaded)
            if owned_dl_dir:
                paths.remove_quietly(owned_dl_dir, recursive=True)
            paths.remove_quietly(work, recursive=True)

    return Outcome(ok=True, data=report.to_data())

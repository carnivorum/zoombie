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


def _download(environment: env_mod.Env, args, log_dir: str) -> tuple[str, str]:
    """Download the source URL. Returns ``(video_path, owned_dir)``.

    ``owned_dir`` is the temp directory this command created, or "" when the user
    supplied -DownloadDir and therefore owns the directory.
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
        *ytdlp.download_args(directory, audio_only=False, force=False),
        args.source,
    ]
    process.run_checked(argv, what="yt-dlp")

    produced = ytdlp.newest_download(directory)
    if not produced or not produced["file"]:
        raise ZoombieError("yt-dlp produced no file to transcribe")
    return produced["file"], owned


def run(args) -> Outcome:
    environment = env_mod.resolve(args.model)

    is_url = bool(URL_PATTERN.match(args.source))
    video_path = args.source
    owned_dl_dir = ""
    downloaded: str | None = None

    if is_url:
        process.log("pipeline: download stage", "step")
        if args.dry_run:
            return Outcome(
                ok=True,
                data={"dryRun": True, "stage": "download", "url": args.source},
            )
        video_path, owned_dl_dir = _download(environment, args, args.work_root or "")
        downloaded = video_path
        process.log(f"downloaded: {video_path}")

    # Extract audio into the ASCII work dir directly.
    work_root = args.work_root or paths.env_path(paths.WORK_FOLDER)
    if not paths.is_ascii(work_root):
        raise ZoombieError(f"Work root must be ASCII: {work_root}")
    paths.assert_fits(work_root, "The work root (-WorkRoot)", slack=80)

    work = paths.new_ascii_dir(work_root)
    audio_out = os.path.join(work, "audio.wav")

    base = stt.resolve_output_base(video_path, args.output)
    paths.assert_fits(base, "The pipeline output path", slack=12)
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
            no_gpu=args.no_gpu,
            no_flash_attn=args.no_flash_attn,
            threads=args.threads,
            allow_cpu_fallback=args.allow_cpu_fallback,
            strict_gpu=args.strict_gpu,
            work_root=work_root,
            keep_work=args.keep_work,
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

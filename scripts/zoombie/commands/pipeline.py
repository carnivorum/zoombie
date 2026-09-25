"""``pipeline``: download (when given a URL), extract audio, transcribe.

The audio is extracted DIRECTLY into the ASCII work dir, so it never exists under
a non-ASCII path and whisper.cpp is isolated from the start. That is a stronger
guarantee than ``transcribe`` alone, which has to copy a possibly-Cyrillic input
into the work dir first.
"""

from __future__ import annotations

import os
import re
import unicodedata

from ..cli import Outcome
from ..lib import env as env_mod, paths, process, scratch, stt, ytdlp
from ..lib.errors import StepFailedError, ZoombieError

URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)

# Characters Windows forbids in a file name. yt-dlp's --windows-filenames strips
# them, but a fullwidth ``？`` (U+FF1F) is NOT the ASCII ``?`` and slips through,
# so the name is re-checked here after fullwidth folding.
_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'

# A retention larger than this is a logged SKIP, not a copy: the origin can
# re-produce the media, but a surprise multi-GB duplicate cannot be un-copied once
# the disk is full. Same best-effort discipline as the over-budget path check. The
# guard is a safety net for the URL-download path -- a local -Source is never
# copied at all (see :func:`_retain_source`).
MAX_RETAIN_BYTES = 2 * 1024 * 1024 * 1024


def _same_file(a: str, b: str) -> bool:
    """True when two paths name the same file, resolving case and ``./``."""
    return os.path.normcase(paths.absolute(a)) == os.path.normcase(paths.absolute(b))


def _retention_destination(video_path: str, item_dir: str) -> str:
    """Where the media WOULD be retained: the item root under a sanitized name."""
    return os.path.join(item_dir, _sanitize_media_name(os.path.basename(video_path)))


def _sanitize_media_name(name: str, *, max_len: int = 150) -> str:
    """A safe basename for the retained media: NFC, ASCII punctuation, capped.

    A video title is content-controlled and routinely carries fullwidth forms --
    ``？`` (U+FF1F) renders like ``?`` but is a different code point, so a title
    ending in one produced ``… уже сегодня？ [hash].mp4`` and forced a
    percent-encoded link. Normalising here gives an ASCII-punctuation, NFC,
    quote-free name with the stem capped and the extension preserved.
    """
    normalised = unicodedata.normalize("NFC", name)
    stem, dot, extension = normalised.rpartition(".")
    if not dot:
        stem, extension = normalised, ""
    folded = "".join(
        chr(ord(ch) - 0xFEE0) if "\uff01" <= ch <= "\uff5e"
        else " " if ch == "\u3000"
        else ch
        for ch in stem
    )
    cleaned = "".join(
        ch for ch in folded if ch not in _ILLEGAL_NAME_CHARS and ord(ch) >= 32
    )
    cleaned = cleaned.rstrip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    cleaned = cleaned or "media"
    safe_extension = "".join(
        ch for ch in extension if ch not in _ILLEGAL_NAME_CHARS and ord(ch) >= 32
    )
    safe_extension = safe_extension.rstrip(" .")
    return f"{cleaned}.{safe_extension}" if safe_extension else cleaned


def _retain_source(
    video_path: str, item_dir: str, *, local_source: bool = False
) -> str | None:
    """Copy the source media to the item root; return where, or ``None``.

    The item contract says the source sits at the item root next to ``summary.md``
    -- NOT in ``.data/``, which holds derived material only -- so this is addressed
    at ``item_dir`` directly and is a COPY, not a move: the download directory is
    still cleaned up afterwards, and a failure to copy must not lose the original
    while the transcription is already written.

    Only media the pipeline ITSELF produced (the URL-download path) is copied in.
    A user-supplied local ``-Source`` is NEVER duplicated: the user already owns the
    file where they put it, the sidecar records its path (``url`` falls back to
    ``audio_path``), and duplicating 1.8 GB into a throwaway item is pure waste --
    the defect this parameter fixes. The same-file short-circuit below still runs
    first, so media that already sits in the item records ``sourceKept: true``.

    Best effort by design. A media file name is content-controlled -- a video
    title may be long, non-ASCII, or both -- so the destination can exceed the
    Windows budget, and that must be a logged skip rather than a failed run whose
    transcript is already on disk. An oversized retention (over ``MAX_RETAIN_BYTES``)
    is likewise a logged skip. The caller records what happened in ``sourceKept``,
    so the absence is visible in the sidecar instead of silent.
    """
    if not paths.is_file(video_path):
        return None

    destination = _retention_destination(video_path, item_dir)

    # Same-file is RETENTION, not a failure. When -DownloadDir equals the item
    # folder -- the natural layout -- the source already sits where it belongs,
    # and ``shutil.copyfile`` raises SameFileError on identical paths. The old
    # code reported that as "not retained", which then recorded the false
    # ``sourceKept:false`` / ``sourceFile:null`` the sidecar exists to prevent.
    # The paths are RESOLVED before comparing, so a case-only or ``./``-prefix
    # difference still counts as the same file.
    if _same_file(video_path, destination):
        process.log(f"  source retained: {destination}")
        return destination

    # A local -Source is the user's own file; do not duplicate it into the item.
    if local_source:
        process.log(
            f"  source not retained beside the transcript: {video_path} is a local "
            f"file the user already owns; {destination} was not created"
        )
        return None

    try:
        # Room for the extension and the Windows path budget; the NAME is not ours
        # to shorten, so an over-long one is refused rather than truncated.
        paths.assert_fits(destination, "The retained source path")
    except Exception as exc:  # noqa: BLE001 - any budget/length refusal is a skip
        process.log(f"  source not retained beside the transcript: {exc}", "warn")
        return None

    try:
        size = paths.file_size(video_path)
    except OSError:
        size = None
    if size is not None and size > MAX_RETAIN_BYTES:
        process.log(
            f"  source not retained beside the transcript: {size} bytes exceeds the "
            f"{MAX_RETAIN_BYTES}-byte retention budget; re-download from the origin "
            "to keep a copy",
            "warn",
        )
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

    # ``-Output`` names the ITEM folder; the artifacts go to ``<item>/.data/`` and
    # the retained media to the item root, so both are measured against the budget.
    item_dir = stt.item_dir_for(args.output, video_path)
    base = stt.resolve_output_base(video_path, args.output)
    # Create ``<item>/.data/`` now, before the download/ffmpeg work: a bare
    # transcribe/pipeline run then produces a RECOGNISED item, so ``items``/``index``
    # see it and the documented "transcribe -> then summarize" order holds. See
    # :func:`zoombie.lib.stt.ensure_item_dir` for why this is the choice made.
    stt.ensure_item_dir(args.output, item_dir)
    paths.assert_fits(item_dir, "The pipeline item folder")
    # Room for ``.source.json`` (the longest appended suffix) plus the dot.
    paths.assert_fits(base, "The pipeline output path", slack=13)
    # ffmpeg and yt-dlp both take this path as a native argument, so only its
    # length can be validated: the extended prefix must not reach them.
    paths.assert_fits(video_path, "The downloaded video path")

    if args.dry_run:
        # The work dir was created a few lines up (that is where audio.wav WOULD
        # go), so -DryRun removes it here and reports that it did: a dry run must
        # leave no scratch under the toolchain root (plan §10).
        dry_scratch = scratch.report(work, kept=False)
        if not is_url:
            return Outcome(
                ok=True,
                data={"dryRun": True, "stage": "extract+transcribe",
                      "video": video_path, "itemDir": item_dir,
                      "outputBase": base, "work": work, "scratch": dry_scratch},
            )
        return Outcome(ok=True, data={"dryRun": True, "stage": "download",
                                      "url": args.source, "scratch": dry_scratch})

    try:
        # Checked here -- after the download but BEFORE ffmpeg -- so a refusal
        # still runs the finally that removes the media we just fetched.
        #
        # The base comes from the SAME helper ``transcribe`` uses, so the guard
        # here and the write there cannot name two different files. Reading
        # ``base`` directly (as before) was the other half of the split: the guard
        # passed against the resolved base while ``transcribe`` rewrote it.
        # Guard the base the WRITER will use, window included -- the same
        # ``request_output_base`` the transcribe command and the writer read,
        # so a windowed pipeline run cannot guard one file and write another.
        watched_base = stt.request_output_base(stt.Request(
            audio_path=audio_out, output_base=base, item_dir=item_dir,
            from_time=getattr(args, "from_time", None),
            to_time=getattr(args, "to_time", None),
        ))
        stt.guard_overwrite(watched_base, args.force)

        ffmpeg = environment.require("ffmpeg", "ffmpeg")
        process.run_checked(
            [ffmpeg, "-y", "-hide_banner", "-nostats", "-v", "error",
             "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", audio_out],
            what="ffmpeg",
        )

        # REQUIREMENT: media the pipeline PRODUCED is kept, beside ``summary.md`` at
        # the item root. This REVERSES the previous default, which deleted the
        # download right after transcription and recorded ``sourceKept: false`` --
        # the very case the origin sidecar existed to survive.
        #
        # A user-supplied local ``-Source`` is NOT duplicated: the user already owns
        # it, and copying 1.8 GB into a throwaway item is waste (the defect this
        # fixes). ``local_source`` encodes that; the same-file short-circuit inside
        # ``_retain_source`` still lets media already in the item record
        # ``sourceKept: true``.
        #
        # Retention is best effort, logged either way: a destination that cannot fit
        # the Windows budget must not fail a run whose transcription succeeded, and the
        # sidecar records what actually happened, so an item whose media was not kept
        # stays well-formed.
        kept_source = _retain_source(video_path, item_dir, local_source=not is_url)
        # The sidecar's ``sourceReason`` explains a DELIBERATE non-copy. A local
        # -Source is deliberately not duplicated; when it already sat in the item the
        # same-file short-circuit retained it, so there is no absence to explain.
        source_reason: str | None = None
        if (
            kept_source is None
            and not is_url
            and paths.is_file(video_path)
            and not _same_file(video_path, _retention_destination(video_path, item_dir))
        ):
            source_reason = (
                "the input is a local file the user already owns where they put it, "
                "so pipeline did not duplicate it into the item; the origin is "
                "recorded as the source url (the input path)"
            )
    
        request = stt.Request(
            audio_path=audio_out,
            output_base=base,
            item_dir=item_dir,
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
            # -KeepScratch/-KeepWork reach the transcription stage too, so the same
            # decision governs both this command's work dir and whisper's own.
            keep_work=scratch.keep_requested(args),
            # The pipeline's audio is an extracted scratch WAV deleted in the
            # ``finally`` below, so the sidecar must not record it as the origin
            # (a dead temp path). ``source_file`` names the retained media instead.
            audio_is_scratch=True,
            # The same -From/-To window ``transcribe`` accepts. For a local source
            # the media is sliced after its one extraction; the artifacts get the
            # window-qualified names and offset timestamps.
            from_time=getattr(args, "from_time", None),
            to_time=getattr(args, "to_time", None),
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
            # Why nothing was kept, when that was a deliberate choice (a local
            # source is never duplicated). Same shape as ``urlReason``.
            source_reason=source_reason,
        )
        report = stt.transcribe(environment, request)
    finally:
        # CLI-owned cleanup (plan §10): remove the DOWNLOAD FOLDER and the
        # intermediate audio.wav unless -KeepScratch/-KeepWork asked to retain this
        # run's scratch. The media the run KEPT already survives in the item (that
        # copy-back happens above, which is the ordering E requires), so removing
        # this scratch invalidates nothing the result advertises. whisper's own
        # work dir is cleaned inside stt.transcribe under the SAME decision.
        kept = scratch.keep_requested(args)
        work_block = scratch.report(work, kept=kept)
        dl_block = scratch.report(owned_dl_dir, kept=kept) if owned_dl_dir else None
        pipeline_scratch = {
            "kept": kept,
            "work": work_block,
            "downloadDir": dl_block,
            "leftover": bool(
                work_block["leftover"] or (dl_block and dl_block["leftover"])
            ),
        }

    data = report.to_data()
    # Keep the transcription stage's own block (its whisper work dir) nested inside
    # this command's, so both scratches of one pipeline run are reported.
    pipeline_scratch["transcribe"] = data.get("scratch")
    data["scratch"] = pipeline_scratch
    return Outcome(ok=True, data=data)

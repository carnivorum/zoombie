"""yt-dlp argument assembly, shared by ``download`` and ``pipeline``.

yt-dlp is pure Python and therefore ASCII-path safe (unlike whisper.cpp), so it
needs none of the ASCII isolation the native tools get. It is always invoked
through the interpreter as a module: ``python -m yt_dlp``. That removes the PATH
lookup and the console-launcher shim entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import paths

OUTPUT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"

# The extensions that can legitimately be the downloaded MEDIA. A download
# directory may also hold sidecar files (a .srt, .vtt, .info.json, .description,
# .part) and those are NOT the video: handing one to ffmpeg as "the video" fails
# the run with a confusing codec error, so the candidate set is restricted here.
MEDIA_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".flac", ".opus"}
)


def name_args(directory: str) -> list[str]:
    """Flags that keep a produced file name inside the Windows budget.

    The output template embeds ``%(title)s``, which is content-controlled and can
    be hundreds of characters. yt-dlp bounds an individual path COMPONENT but does
    not know about the 260-character total and never emits an extended-length
    path, so a long title in a deep download dir yields a file that neither
    yt-dlp nor ffmpeg (which consumes it next, in ``pipeline``) can open.

    ``--windows-filenames`` also strips reserved device names (CON, PRN, AUX...),
    NTFS-illegal characters and trailing dots/spaces, which are independent
    failures a length check alone would not catch.
    """
    # Leave room for the directory, the separator, the extension and yt-dlp's own
    # ".part"/".temp" suffixes.
    available = paths.PATH_BUDGET - paths.path_length(directory) - 8
    trim = max(40, min(available, 150))
    return ["--windows-filenames", "--trim-filenames", str(trim)]


def output_args(directory: str) -> list[str]:
    return ["-o", os.path.join(directory, OUTPUT_TEMPLATE), "--no-playlist"]


def download_args(
    directory: str,
    *,
    audio_only: bool = False,
    audio_format: str = "wav",
    force: bool = False,
) -> list[str]:
    """The full ``python -m yt_dlp`` argument list for one download.

    A whisper-ready WAV is requested directly (-ac 1 -ar 16000) when the audio
    format is wav, so the download and the extraction stages cannot disagree about
    the sample rate.
    """
    if audio_only:
        argv = ["-f", "bestaudio/best", "-x", "--audio-format", audio_format]
        if audio_format == "wav":
            argv += ["--postprocessor-args", "-ac 1 -ar 16000"]
    else:
        argv = ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]

    argv += name_args(directory)
    argv += output_args(directory)

    if force:
        argv.append("--force-overwrites")
    else:
        argv += ["--no-overwrites", "--no-mtime"]
    return argv


def newest_download(directory: str) -> dict | None:
    """The most recently written MEDIA file in ``directory``, with its size.

    Restricted to :data:`MEDIA_EXTENSIONS` on purpose. The previous version
    returned the newest file of ANY type, so a stray ``.srt``/``.vtt``/
    ``.info.json`` written after the media would be selected as "the video" and
    handed to ffmpeg. Naming is not trusted either: the filter is on the
    extension, and yt-dlp's own ``.part``/``.ytdl`` scratch names are excluded
    because they are not in the set.
    """
    best: str | None = None
    best_time = -1.0
    for entry in paths.list_dir(directory, files=True):
        if paths.extension_of(entry.name).lower() not in MEDIA_EXTENSIONS:
            continue
        if entry.mtime > best_time:
            best_time = entry.mtime
            best = entry.path
    if not best:
        return None
    try:
        size = paths.file_size(best)
    except OSError:
        size = 0
    return {"file": best, "size": size}


# The metadata is printed as ONE tab-separated line by yt-dlp's ``--print``, which
# is why the field order below and in :func:`parse_metadata` must stay in step.
_METADATA_TEMPLATE = "%(id)s\t%(title)s\t%(webpage_url)s\t%(duration)s"


def metadata_args() -> list[str]:
    """Flags that make yt-dlp PRINT the origin metadata instead of writing a file.

    ``--print`` (with the default ``video`` prefix) is used rather than
    ``--write-info-json`` because it needs no extra file, no extra cleanup and no
    extra path-budget headroom, and it cannot leave an unreadable ``.info.json``
    behind if the process dies. ``after_move`` is the right template prefix: it
    fires AFTER a merge, so the values are post-processed and the line is printed
    even when the download only had to be renamed.
    """
    return ["--no-simulate", "--print", f"after_move:{_METADATA_TEMPLATE}"]


@dataclass(frozen=True)
class Origin:
    """The source video's identity, as reported by yt-dlp."""

    id: str | None = None
    title: str | None = None
    url: str | None = None
    duration_sec: float | None = None


def parse_metadata(text: str) -> Origin | None:
    """Parse the ``--print after_move:`` line out of captured yt-dlp output.

    Best-effort by contract: an unparsable payload returns ``None`` and the caller
    keeps whatever it already knew (at worst the original ``-Source`` URL), because
    losing the origin metadata must never fail a run whose media downloaded fine.
    yt-dlp may interleave progress lines, so the LAST line with three tabs is
    taken: the payload is printed at the end of the post-processing step.
    """
    for line in reversed((text or "").splitlines()):
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        identifier, title, url, duration = (field.strip() for field in fields)
        if not identifier or not url:
            continue
        seconds: float | None = None
        try:
            seconds = float(duration)
        except (TypeError, ValueError):
            seconds = None
        return Origin(
            id=identifier,
            title=title or None,
            url=url or None,
            duration_sec=seconds if seconds and seconds > 0 else None,
        )
    return None

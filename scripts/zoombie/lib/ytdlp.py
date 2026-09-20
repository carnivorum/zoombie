"""yt-dlp argument assembly, shared by ``download`` and ``pipeline``.

yt-dlp is pure Python and therefore ASCII-path safe (unlike whisper.cpp), so it
needs none of the ASCII isolation the native tools get. It is always invoked
through the interpreter as a module: ``python -m yt_dlp``. That removes the PATH
lookup and the console-launcher shim entirely.
"""

from __future__ import annotations

import os

from . import paths

OUTPUT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"


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
    """The most recently written file in ``directory``, with its size."""
    path = paths.newest_file(directory)
    if not path:
        return None
    try:
        size = paths.file_size(path)
    except OSError:
        size = 0
    return {"file": path, "size": size}

"""``extract``: pull the audio track out of a video with ffmpeg.

The output is always 16 kHz mono (``-ac 1 -ar 16000``), which is exactly the
format whisper.cpp consumes, so the two stages cannot disagree.
"""

from __future__ import annotations

import os

from ..cli import Outcome
from ..lib import env as env_mod, paths, process
from ..lib.errors import ZoombieError

# Codec flags per container. WAV is the default because it is what whisper wants.
CODECS: dict[str, list[str]] = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-c:a", "aac", "-b:a", "128k"],
    "flac": ["-c:a", "flac"],
}

SAMPLE_RATE = 16000
CHANNELS = 1


def output_path(source: str, output: str | None, audio_format: str) -> str:
    """Resolve the destination, defaulting to the source name with a new suffix."""
    if output:
        destination = output
    else:
        destination = os.path.splitext(source)[0] + "." + audio_format
    if not paths.extension_of(destination):
        destination = f"{destination}.{audio_format}"
    return paths.absolute(destination)


def run(args) -> Outcome:
    environment = env_mod.resolve()
    ffmpeg = environment.require("ffmpeg", "ffmpeg")

    if not paths.is_file(args.source):
        raise ZoombieError(f"Input not found: {args.source}")

    destination = output_path(args.source, args.output, args.format)
    paths.assert_fits(destination, "The extract output path")

    if paths.is_file(destination) and not args.force:
        raise ZoombieError(f"Output exists (use -Force to overwrite): {destination}")

    # ffmpeg reads -i and writes the output through the C runtime, so these paths
    # must stay prefix-free: only their LENGTH can be validated.
    paths.assert_fits(args.source, "The extract input path")

    argv = [
        ffmpeg, "-y", "-hide_banner", "-nostats", "-v", "error",
        "-i", args.source,
        "-vn", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        *CODECS[args.format],
        destination,
    ]

    process.log(f"ffmpeg extract -> {destination}", "step")
    if args.dry_run:
        return Outcome(ok=True, data={"dryRun": True, "output": destination, "args": argv[1:]})

    process.run_checked(argv, what="ffmpeg")

    return Outcome(
        ok=True,
        data={
            "output": destination,
            "size": paths.file_size(destination),
            "format": args.format,
            "channels": CHANNELS,
            "sampleRate": SAMPLE_RATE,
        },
    )

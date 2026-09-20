"""``download``: fetch a video (or its audio) with yt-dlp."""

from __future__ import annotations

import os

from ..cli import Outcome
from ..lib import env as env_mod, paths, process, ytdlp


def run(args) -> Outcome:
    environment = env_mod.resolve()
    python = environment.require("python", "python")

    directory = args.download_dir or os.path.join(os.getcwd(), "downloads")
    if not paths.is_dir(directory):
        if args.dry_run:
            process.log(f"would create {directory}", "step")
        else:
            paths.ensure_dir(directory)

    # The download dir is the prefix of a content-controlled file name, so its own
    # length is checked before the template is assembled.
    paths.assert_fits(directory, "The download directory", slack=60)

    argv = [
        python, "-m", "yt_dlp",
        *ytdlp.download_args(
            directory,
            audio_only=args.audio_only,
            audio_format=args.format,
            force=args.force,
        ),
        args.source,
    ]

    process.log(" ".join(argv), "step")
    if args.dry_run:
        return Outcome(
            ok=True,
            data={"dryRun": True, "dir": directory, "args": argv[2:]},
        )

    process.run_checked(argv, what="yt-dlp")

    produced = ytdlp.newest_download(directory) or {"file": None, "size": 0}
    return Outcome(
        ok=True,
        data={"dir": directory, "file": produced["file"], "size": produced["size"]},
    )

"""``download``: fetch a video (or its audio) with yt-dlp.

The destination follows the SAME workspace rule as ``summarize``: a source
INSIDE the workspace keeps its own folder, and a URL (or a file outside it) is
routed to ``<workspace>/_unsorted/download/<name>/``. An explicit ``-DownloadDir``
still wins, and ``-Name`` refines the extrapolated folder name.
"""

from __future__ import annotations

import os

from ..cli import Outcome
from ..lib import env as env_mod, paths, process, workspace, ytdlp


def destination(args) -> tuple[str, bool]:
    """The folder a download goes into, and whether the source was in-workspace.

    An explicit ``-DownloadDir`` is honoured verbatim. Otherwise
    :func:`zoombie.lib.workspace.destination_dir` applies the one routing rule, so
    a URL lands under ``_unsorted/download/<name>`` and never in the workspace root.
    """
    explicit = getattr(args, "download_dir", None)
    if explicit:
        return paths.absolute(explicit), False
    return workspace.destination_dir(
        args.source, workspace.KIND_DOWNLOAD, name=getattr(args, "name", None)
    )


def run(args) -> Outcome:
    environment = env_mod.resolve()
    python = environment.require("python", "python")

    directory, internal = destination(args)
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
            data={"dryRun": True, "dir": directory, "internal": internal,
                  "args": argv[2:]},
        )

    process.run_checked(argv, what="yt-dlp")

    produced = ytdlp.newest_download(directory) or {"file": None, "size": 0}
    return Outcome(
        ok=True,
        data={"dir": directory, "internal": internal,
              "file": produced["file"], "size": produced["size"]},
    )

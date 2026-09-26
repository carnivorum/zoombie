"""Build the live-test harness and write the task list.

The live test drives the REAL ``summarize`` flow against real media: it
transcribes on the GPU, renders a PDF and downloads from the network, so it is
never part of the unit suite. This script only PREPARES the run - it copies the
sample media into place and writes a task list - and never invokes the flow.

The instructions are in ``readme.md`` beside this file; the task list is the
machine-checkable form of the same thing, templated with the paths that actually
resolved on this machine.

Paths are DERIVED, never hardcoded: the samples are untracked, and the Cyrillic
PDF name must survive the trip to the task list, which is why that list is written
as UTF-8 to a FILE rather than printed to a console whose code page would mangle
it.

    python run_livetest.py            # create the harness, write ws/TASKS.md
    python run_livetest.py --reset    # clear the harness first

Stdlib only, and ASCII-safe in its own source.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

# Where an INSIDE source goes: under the workspace, so the flow treats it as the
# user's own material and summarizes it in place. The workspace root is the cwd
# of the process that runs the flow, and this repository is what a client opens,
# so anything under HERE counts as inside.
#
# ONE CASE, ONE FOLDER. An in-place source IS its item, so putting both samples in
# a single directory made the second run overwrite the first: the video item became
# the folder the PDF run had to write into, and the overwrite gate fired on what
# should have been a clean first run. The CASE is the unit of isolation.
WORKSPACE = HERE / "ws"
INSIDE_DIRS = {"video": WORKSPACE / "video", "pdf": WORKSPACE / "pdf"}

# Where an OUTSIDE source goes: a temp dir off the repository entirely, so the
# flow routes it to _unsorted. Deliberately NOT under WORKSPACE.
OUTSIDE = Path(tempfile.gettempdir()) / "zoombie-livetest" / "outside"

TASKS_NAME = "TASKS.md"

# The remote source. No placement question applies: a download is ours.
REMOTE_URL = "https://rutube.ru/video/ee7e9f4af68a1b34df85219d147bfe99/"


class HarnessError(RuntimeError):
    """A missing sample or an unusable path, named for the operator."""


def find_sample(suffix: str) -> Path:
    """The sole sample media file with ``suffix`` beside this script.

    Discovered rather than named: the media are untracked and the PDF's name is
    long, Cyrillic and carries a doubled space, so spelling it here would be a
    second place to keep in sync with the file on disk.

    Raises :class:`HarnessError` naming what was looked for, because a fresh
    clone has no samples at all and a bare ``FileNotFoundError`` would not say why.
    """
    matches = sorted(
        Path(path) for path in glob.glob(str(HERE / f"*{suffix}"))
        if os.path.isfile(path)
    )
    if not matches:
        raise HarnessError(
            f"no {suffix} sample in {HERE}. The samples are NOT tracked by git "
            f"(they are the user's own media), so place one {suffix} file beside "
            "readme.md before running the live test."
        )
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise HarnessError(
            f"{len(matches)} {suffix} samples in {HERE}: {names}. Keep exactly one "
            "so the inside and outside cases are unambiguous."
        )
    return matches[0]


def reset() -> None:
    """Remove the harness this script builds. Touches nothing else."""
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    shutil.rmtree(OUTSIDE, ignore_errors=True)


def prepare() -> dict:
    """Create the harness and return the resolved paths.

    Copies, never moves: the samples ARE the fixture, so an in-place action must
    not consume them.
    """
    video = find_sample(".mp4")
    pdf = find_sample(".pdf")

    for directory in (*INSIDE_DIRS.values(), OUTSIDE):
        directory.mkdir(parents=True, exist_ok=True)

    inside_video = INSIDE_DIRS["video"] / video.name
    inside_pdf = INSIDE_DIRS["pdf"] / pdf.name
    outside_video = OUTSIDE / video.name
    outside_pdf = OUTSIDE / pdf.name
    for source, target in (
        (video, inside_video), (pdf, inside_pdf),
        (video, outside_video), (pdf, outside_pdf),
    ):
        shutil.copy2(source, target)

    return {
        "workspace": WORKSPACE,
        "videoInside": inside_video,
        "pdfInside": inside_pdf,
        "videoOutside": outside_video,
        "pdfOutside": outside_pdf,
    }


def task_list(paths: dict) -> str:
    """The task list as Markdown, with the real paths substituted.

    Written to a file because the paths carry Cyrillic and spaces; a console
    round-trip would corrupt exactly the values the test exists to stress.
    """
    return f"""# Live-test tasks

Generated by `run_livetest.py`. Read `readme.md` for what each step must prove.

Workspace root this run assumes: `{REPO}`
(derived: `workspace_root()` is the working directory, so an INSIDE source is one
under it and an OUTSIDE source is one off it.)

## Sources

| Case | Source | Placement |
|------|--------|-----------|
| video inside | `{paths["videoInside"]}` | inside the workspace |
| video outside | `{paths["videoOutside"]}` | outside the workspace |
| pdf inside | `{paths["pdfInside"]}` | inside the workspace |
| pdf outside | `{paths["pdfOutside"]}` | outside the workspace |

Each inside case sits in its OWN folder, because an in-place source IS its item:
sharing one directory would make the second case overwrite the first.
| remote | `{REMOTE_URL}` | a link, no placement question |

## Subtasks, one at a time

1. video inside: `summarize {{source: "{paths["videoInside"]}"}}`
2. video outside: `summarize {{source: "{paths["videoOutside"]}"}}` (expect the
   media question; answer `copy`)
3. pdf inside: `summarize {{source: "{paths["pdfInside"]}"}}` (`slides: "false"`)
4. pdf outside: `summarize {{source: "{paths["pdfOutside"]}"}}` (`slides: "false"`)
5. remote: `summarize {{source: "{REMOTE_URL}"}}`

Each: `name` -> `slides` -> `prose` -> `verify`. For a video, prune the frames and
check `data.slides.selection.applied` is `true`.

Then do the review pass in `readme.md` and report every hiccup.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the live-test harness.")
    parser.add_argument(
        "--reset", action="store_true",
        help="clear the harness (ws/ and the outside copies) and exit; build nothing",
    )
    parser.add_argument(
        "--print", dest="print_list", action="store_true",
        help="echo the task list to stdout too (may mangle Cyrillic; the file is authoritative)",
    )
    args = parser.parse_args(argv)

    # ``--reset`` CLEARS and exits; it does not rebuild. The review pass ends with
    # this command, so a "clean up" that left a fresh harness behind would be a
    # trap. Building is the default; the build is idempotent, so a rebuild after a
    # reset is just a second run of this script.
    if args.reset:
        reset()
        print("livetest: harness cleared")
        return 0

    try:
        paths = prepare()
    except HarnessError as exc:
        print(f"livetest: {exc}", file=sys.stderr)
        return 1

    tasks = WORKSPACE / TASKS_NAME
    tasks.write_text(task_list(paths), encoding="utf-8")

    print("livetest: harness ready")
    print(f"  workspace : {REPO} (the real one; inside sources sit under it)")
    print(f"  inside    : {paths['videoInside'].parent}")
    print(f"  outside   : {OUTSIDE}")
    print(f"  tasks     : {tasks}")
    if args.print_list:
        print(task_list(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

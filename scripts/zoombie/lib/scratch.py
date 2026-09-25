"""Toolchain-owned scratch: the roots, the ownership test, and the sweep.

Plan §10 (``plans/zoombie-image-pipeline-rework.md``) moves the scratch lifecycle
out of prose and into the CLI. Two properties are enforced by code here rather
than left to an agent to remember:

* **Per-run scratch lives under the TOOLCHAIN ROOT**, never the user's workspace
  and never ``C:\\Temp``. :func:`zoombie.lib.paths.new_temp_dir` already chooses
  that root (the two roots below), because whisper.cpp, PyMuPDF and Tesseract need
  ASCII paths; this module only *names* the roots so cleanup and reporting read
  from one place instead of each command inventing its own rule.
* **Removal never touches a user artifact.** A ``-Output``/``-ImageDir``/``-Vision``
  destination belongs to the caller; even when it sits under a workspace ``/.tmp``
  it is NOT scratch and is never deleted here. The sweep only ever removes a
  DIRECTORY that is a direct child of one of the two roots **and** whose name
  carries our run marker (32 hex GUID, optionally behind a prefix).

Why a sweep verb exists at all: :func:`zoombie.lib.paths.remove_quietly` and
:func:`zoombie.lib.paths.remove_work_dir` deliberately never block a run on a busy
directory, so a KILLED child can leave ``<root>\\tmp\\zoombie-...`` behind with
nothing left to clean it. ``zoombie clean -CleanScratch`` is that cleaner. The
normal path is :func:`report`: a run removes its own scratch as it finishes, and a
busy directory becomes a *reported* leftover with the verb named, never a silent
leak.

The ``data.scratch`` block :func:`report` returns is the machine-checkable proof
of the default behaviour: ``kept`` is true only under ``-KeepScratch``/``-KeepWork``,
and ``leftover`` is false for a run that cleaned up after itself.
"""

from __future__ import annotations

import os
import re

from . import paths

__all__ = [
    "TEMP_ROOT_NAME",
    "is_run_dir",
    "is_owned",
    "inventory",
    "clean",
    "report",
    "keep_requested",
]

# The two roots, both direct children of the (ASCII) toolchain root:
#   <root>\work\<guid>            per-job work dirs (new_ascii_dir/new_work_dir)
#   <root>\tmp\<prefix>-<guid>    downloads and extraction (new_temp_dir)
WORK_ROOT_NAME = paths.WORK_FOLDER  # "work"
TEMP_ROOT_NAME = "tmp"

# A run-created name is a 32-char lowercase hex GUID, optionally behind a
# lowercase prefix (``zoombie-unpack-<guid>``, ``tmp-<guid>``). The prefix may
# itself contain hyphens (``zoombie-tesseract-dl-<guid>``). Requiring the GUID is
# what keeps a user's own folder that merely sits under a root -- ``notes``,
# ``projects`` -- from being swept.
_RUN_NAME = re.compile(r"^(?:[a-z][a-z0-9-]*-)?[0-9a-f]{32}$")


def _roots(work_root: str | None, include_temp: bool) -> list[tuple[str, str]]:
    """The scratch roots as ``(path, kind)``; ``kind`` is ``"work"`` or ``"tmp"``."""
    root = paths.env_root()
    entries: list[tuple[str, str]] = [
        (work_root or os.path.join(root, WORK_ROOT_NAME), "work")
    ]
    if include_temp:
        entries.append((os.path.join(root, TEMP_ROOT_NAME), "tmp"))
    return entries


def is_run_dir(name: str) -> bool:
    """True when a directory NAME is one this toolchain's runs create."""
    return bool(_RUN_NAME.match(name or ""))


def is_owned(path: str | None, work_root: str | None = None) -> bool:
    """True when ``path`` is a directory this toolchain created as scratch.

    The test is deliberately narrow: a real directory, a run-marker name, and a
    parent that is exactly one of the scratch roots. A user's ``-Output`` that
    happens to be named like a GUID but lives elsewhere is not owned.
    """
    if not path or not paths.is_dir(path):
        return False
    name = os.path.basename(os.path.normpath(paths.from_extended(path)))
    if not is_run_dir(name):
        return False
    parent = os.path.normcase(paths.absolute(os.path.dirname(paths.from_extended(path))))
    for root, _kind in _roots(work_root, include_temp=True):
        if parent == os.path.normcase(paths.absolute(root)):
            return True
    return False


def inventory(work_root: str | None = None, include_temp: bool = True) -> list[dict]:
    """Every direct child of every scratch root, marked owned or foreign.

    ``owned`` is false for a directory that does not carry our run marker -- it is
    reported, never removed, so a caller can see what was deliberately spared.
    """
    found: list[dict] = []
    for root, kind in _roots(work_root, include_temp):
        for entry in paths.list_dir(root, dirs=True):
            found.append(
                {
                    "path": entry.path,
                    "root": root,
                    "kind": kind,
                    "owned": is_run_dir(entry.name),
                    "mtime": entry.mtime,
                }
            )
    return found


def clean(
    work_root: str | None = None,
    include_temp: bool = True,
    dry_run: bool = False,
) -> dict:
    """Remove toolchain-owned scratch; report what was removed and what was spared.

    Never removes a foreign child of a root, and never removes anything outside a
    root -- so a caller's ``-Output``/``-ImageDir``/``-Vision`` is untouched even
    when it lives in the workspace ``/.tmp``. A directory a live process still
    holds is reported in ``failed`` rather than failing the whole sweep.
    """
    roots = _roots(work_root, include_temp)
    removed: list[str] = []
    failed: list[str] = []
    spared: list[str] = []
    for root, _kind in roots:
        if not paths.is_dir(root):
            continue
        for entry in paths.list_dir(root, dirs=True):
            if not is_run_dir(entry.name):
                spared.append(entry.path)
                continue
            if dry_run:
                removed.append(entry.path)
                continue
            if paths.remove_quietly(entry.path, recursive=True):
                removed.append(entry.path)
            else:
                failed.append(entry.path)
    return {
        "roots": [root for root, _kind in roots],
        "removed": removed,
        "removedCount": len(removed),
        "failed": failed,
        "spared": spared,
        "dryRun": dry_run,
    }


def keep_requested(args) -> bool:
    """True when a run was asked to RETAIN its scratch.

    ``-KeepScratch`` is the plan-§10 spelling; ``-KeepWork`` is the historical
    alias and is accepted on the same commands (they are the same decision).
    Read via ``getattr`` because a programmatic caller and the existing unit tests
    build a Namespace with only the flags they knew about.
    """
    return bool(
        getattr(args, "keep_scratch", False) or getattr(args, "keep_work", False)
    )


def report(work: str | None, kept: bool) -> dict:
    """Finish a run: delete its scratch unless kept, and describe what happened.

    Returns the ``data.scratch`` block. ``kept`` is the caller's decision
    (``-KeepScratch``/``-KeepWork``); when it is false the directory is removed
    with the retry-without-blocking behaviour, and a directory that is still held
    becomes ``leftover: true`` with the sweep verb named in ``hint``.
    """
    if not work:
        return {"path": None, "kept": False, "removed": False, "leftover": False}
    if kept:
        return {"path": work, "kept": True, "removed": False, "leftover": False}
    ok = paths.remove_work_dir(work)
    block = {"path": work, "kept": False, "removed": ok, "leftover": not ok}
    if not ok:
        block["hint"] = (
            "the scratch dir is still held by another process; run "
            "`zoombie clean -CleanScratch` to remove it"
        )
    return block

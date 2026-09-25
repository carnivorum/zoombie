"""``clean``: remove the per-job scratch dirs under the work root.

Two levels, and the flag is the difference:

* **default** -- remove the ``<WorkRoot>\\<guid>`` directories, exactly as before.
* **``-CleanScratch``** -- the plan-§10 verb: sweep EVERY toolchain-owned scratch
  directory, i.e. both roots the toolchain writes runs into (``<root>\\work`` and
  ``<root>\\tmp``). This exists for the leftover a KILLED run leaves: the normal
  path is that a run cleans up after itself (see :mod:`zoombie.lib.scratch`), but
  ``remove_quietly``/``remove_work_dir`` never block on a busy directory, so one
  can survive a crash with nothing left to remove it.

Ownership is decided by :func:`zoombie.lib.scratch.is_owned`, never by "is under a
root": a user's own folder that happens to sit in the scratch root -- or a
``-Output``/``-ImageDir``/``-Vision`` destination, which may live in the workspace
``/.tmp`` -- carries no run marker and is reported in ``spared``, never deleted.
``-DryRun`` reports the same lists and removes nothing.
"""

from __future__ import annotations

from ..cli import Outcome
from ..lib import manifest, paths, process, scratch


def run(args) -> Outcome:
    # The plan-§10 sweep: both toolchain-owned roots, run-marker names only.
    if getattr(args, "clean_scratch", False):
        work_root = args.work_root or paths.env_path(paths.WORK_FOLDER)
        result = scratch.clean(work_root=args.work_root, dry_run=args.dry_run)
        result["workRoot"] = work_root
        verb = "would remove" if args.dry_run else "removed"
        process.log(
            f"clean -CleanScratch: {verb} {result['removedCount']} scratch dir(s); "
            f"{len(result['spared'])} non-scratch entr(y/ies) spared"
        )
        return Outcome(ok=True, data=result)

    # The original behaviour, unchanged: per-job dirs under the work root.
    work_root = args.work_root or paths.env_path(paths.WORK_FOLDER)

    if not paths.is_dir(work_root):
        return Outcome(ok=True, data={"removed": 0, "workRoot": work_root})

    directories = paths.list_dir(work_root, dirs=True)
    if args.dry_run:
        return Outcome(
            ok=True,
            data={"dryRun": True, "wouldRemove": len(directories), "workRoot": work_root},
        )

    removed = 0
    for entry in directories:
        # Best-effort: a directory a live process still holds is reported and
        # skipped rather than failing the whole clean.
        try:
            paths.remove(entry.path, recursive=True)
            removed += 1
        except OSError as exc:
            process.log(f"could not remove {entry.path}: {exc}", "warn")

    return Outcome(ok=True, data={"removed": removed, "workRoot": work_root})

"""``clean``: remove the per-job scratch dirs under the work root."""

from __future__ import annotations

from ..cli import Outcome
from ..lib import manifest, paths, process


def run(args) -> Outcome:
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

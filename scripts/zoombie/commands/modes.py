"""``modes``: deploy the Zoombie role into the global Zoo Code custom modes.

This exists so a prompt edit can be shipped without re-running the whole
installer. It merges only our own entry, so it is safe to run at any time and
against a file the user has edited by hand.
"""

from __future__ import annotations

from ..cli import Outcome
from ..lib import env as env_mod, modes as modes_mod, paths, process


def run(args) -> Outcome:
    source_root = modes_mod.source_dir(env_mod.cli_dir())
    target = args.target or modes_mod.global_modes_path()

    planned = modes_mod.deploy_all(
        source_root, target, dry_run=True, force=args.force
    )

    if args.check:
        for record in planned:
            process.log(f"{record['slug']}: {record['action']} -> {record['path']}")
        return Outcome(ok=True, data={
            "check": True,
            "target": target,
            "source": source_root,
            "entries": planned,
        })

    if not args.apply:
        # Dry run is the default, mirroring postprocess/index: the caller opts in
        # to a write so a stray invocation can never edit the user's config.
        return Outcome(ok=True, data={
            "dryRun": True,
            "target": target,
            "source": source_root,
            "entries": planned,
        })

    results = modes_mod.deploy_all(source_root, target, force=args.force)
    if not results:
        return Outcome(
            ok=False,
            data={"target": target, "source": source_root},
            error=f"No mode sources found under {source_root}",
        )

    for record in results:
        process.log(f"mode '{record['slug']}' {record['action']} -> {record['path']}")
    return Outcome(ok=True, data={
        "applied": True,
        "target": target,
        "source": source_root,
        "entries": results,
        "exists": paths.is_file(target),
    })

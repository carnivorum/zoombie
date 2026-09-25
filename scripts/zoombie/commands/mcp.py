"""``mcp``: register the MCP facade in the client's global MCP settings.

This exists for the same reason ``modes`` does: wiring the MCP server into the
client is a separate, re-runnable step, so it can be shipped (or repaired) without
running the whole installer, and it writes with an explicit opt-in because the
target file is one the user also owns.

Dry run is the default: only ``-Apply`` writes. ``-Check`` reports the plan
without writing. The merge replaces only ``mcpServers.zoombie`` and preserves
every foreign server, so running it at any time is safe.
"""

from __future__ import annotations

from ..cli import Outcome
from ..lib import mcpsettings, paths, process


def run(args) -> Outcome:
    target = args.target or mcpsettings.global_settings_path()

    python = mcpsettings.interpreter()
    if not python:
        return Outcome(
            ok=False,
            data={"target": target},
            error=(
                "No Python interpreter resolved, so the MCP server cannot be "
                "registered. Install Python (winget install Python.Python.3.12) "
                "and re-run."
            ),
        )

    planned = mcpsettings.deploy(target, dry_run=True)
    process.log(
        f"mcp server '{planned['name']}': {planned['action']} -> {target}"
    )

    if args.check:
        return Outcome(ok=True, data={
            "check": True,
            "target": target,
            "entry": mcpsettings.server_entry(python),
            "action": planned["action"],
            "report": mcpsettings.report(),
        })

    if not args.apply:
        # Dry run is the default, mirroring modes/postprocess/index: the caller
        # opts in to a write so a stray invocation cannot edit the user's config.
        return Outcome(ok=True, data={
            "dryRun": True,
            "target": target,
            "entry": mcpsettings.server_entry(python),
            "action": planned["action"],
        })

    record = mcpsettings.deploy(target, force=args.force)
    process.log(f"mcp server '{record['name']}' {record['action']} -> {record['path']}")
    return Outcome(ok=True, data={
        "applied": True,
        "target": target,
        "entry": mcpsettings.server_entry(python),
        "entries": [record],
        "exists": paths.is_file(target),
    })

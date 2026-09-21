"""``index``: regenerate the ``README.md`` index of an item library.

A library is a folder of **items**. An item is a directory of any name holding a
``summary.md`` and a ``.data/`` directory; see :mod:`zoombie.item.paths`. The
folder name is the user's to choose and is never parsed here -- number, date and
title come from ``.data/item.json``, falling back to a legacy name and then to the
summary's own H1.

This module is a **renderer over the shared scan** (:func:`zoombie.item.scan.scan`).
That is deliberate and load-bearing: the ``items`` command an agent calls and the
index a human reads must agree about what a library contains, and two independent
parsers is how they would eventually stop agreeing. There is one walk of the tree,
in :mod:`zoombie.item.scan`, and both callers render its result.

**Dry run by default.** The index is written only with ``-Apply``, and the whole
document is built in memory and written once, so a failure halfway through can
never leave a half-written ``README.md`` behind. This mirrors
:mod:`zoombie.commands.postprocess` exactly, and for the same reason: a skill calls
this command, so it must not be able to corrupt a file by accident.
"""

from __future__ import annotations

import os

from .. import SKILL_VERSION
from ..cli import Outcome
from ..item import meta as item_meta, paths as item_paths, registry, scan
from ..lib import paths, process
from ..lib.errors import ZoombieError
from ..lib.textnorm import percent_encode_dest

SUMMARY_NAME = item_paths.SUMMARY_NAME
MANIFEST_NAME = item_paths.MANIFEST_NAME
DATA_DIR_NAME = item_paths.DATA_DIR_NAME
DEFAULT_OUTPUT_NAME = "README.md"

# Kept as a module-level name for callers that referenced it. It is now the LEGACY
# reader ONLY -- it back-fills metadata for an item that predates ``item.json`` --
# and never decides whether something is an item.
FOLDER_RE = item_meta.LEGACY_FOLDER_RE


def scan_library(root: str) -> list[dict]:
    """Every item under ``root``, sorted for reading.

    Retained as the reusable half the summarize skill's "reindex the workspace"
    offer calls, so the offer and the command cannot disagree. Both now delegate to
    :func:`zoombie.item.scan.scan`, which is the single definition of an item.
    """
    return scan.scan(root).items


def scan_library_detail(root: str) -> tuple[list[dict], list[dict]]:
    """``(items, skipped)`` for ``root``.

    ``skipped`` names every immediate subfolder that is not an item, with the
    reason, so a caller can report a mis-named -- or rather mis-populated -- folder
    instead of silently dropping it from the index.
    """
    result = scan.scan(root)
    return result.items, result.skipped


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _cell(text: str) -> str:
    """Make ``text`` safe inside a Markdown table cell.

    A pipe would start a new column and a newline would end the row, so both are
    neutralized. The backslash must be escaped first, or the escapes added below
    would themselves be escaped.
    """
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _summary_link(root: str, item: dict) -> str:
    """The ``summary.md`` link for one item, or ``-`` when it has none."""
    if not item["summary"]:
        return "-"
    relative = os.path.relpath(item_paths.summary_path(item["path"]), root)
    return f"[{SUMMARY_NAME}]({percent_encode_dest(relative.replace(chr(92), '/'))})"


def render_table(root: str, items: list[dict]) -> str:
    """The Markdown table, one row per item.

    The link destination goes through :func:`percent_encode_dest` -- the same
    helper ``postprocess`` uses for its links -- so a folder name containing a space
    is a valid destination while the Cyrillic in it stays human-readable rather
    than turning into ``%D0%97`` noise.
    """
    lines = [
        "| Item | # | Date | Title | Summary | Source | Img | Manifest |",
        "|------|---|------|-------|---------|--------|-----|----------|",
    ]
    for item in items:
        name = _cell(item["name"])
        item_link = f"[{name}]({percent_encode_dest(item['name'] + '/')})"
        number = "-" if item["number"] is None else str(item["number"])
        # The date is RENDERED here, not stored: ``item.json`` holds ISO so that it
        # sorts, and the display form is the Russian convention this project uses.
        date = registry.date_display(item["date"]) or "-"
        source = item["source"].get("file") or "-"
        images = str(item["images"]) if item["images"] else "-"
        manifest = "yes" if item["hasManifest"] else "-"
        lines.append(
            f"| {item_link} | {number} | {_cell(date)} | {_cell(item['title'])} "
            f"| {_summary_link(root, item)} | {_cell(source)} | {images} | {manifest} |"
        )
    return "\n".join(lines)


def render_markdown(root: str, items: list[dict], skipped: list[dict],
                    naming: dict | None = None) -> str:
    """The full ``README.md`` text for a library.

    Built entirely from the scan result, so two runs over an unchanged library
    produce byte-identical output -- which is what makes ``-Apply`` safe to run
    again and what a caller can diff.
    """
    parts = [
        "# Library index",
        "",
        f"Items: {len(items)}. Generated by `zoombie index` from `{root}`.",
        f"Toolchain: {SKILL_VERSION}.",
        "",
    ]

    if naming:
        parts.extend(
            [
                f"Naming convention: `{naming.get('description')}` "
                f"(confidence: {naming.get('confidence')}).",
                "",
            ]
        )

    parts.append(render_table(root, items) if items else "_No items found._")

    if skipped:
        parts.extend(
            [
                "",
                "## Skipped",
                "",
                "Folders that hold neither a `summary.md` nor a `.data/` directory:",
                "",
            ]
        )
        for entry in skipped:
            parts.append(f"- `{_cell(entry['name'])}` -- {entry['reason']}")

    # Exactly one trailing newline, the same discipline postprocess enforces.
    return "\n".join(parts).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def run(args) -> Outcome:
    """Entry point behind ``zoombie index``."""
    if not args.dir:
        raise ZoombieError("Pass -Dir <library-root>.")

    root = paths.absolute(args.dir)
    if not paths.is_dir(root):
        raise ZoombieError(f"Directory not found: {root}")
    paths.assert_fits(root, "The library root")

    output = paths.absolute(args.output) if args.output else os.path.join(root, DEFAULT_OUTPUT_NAME)
    paths.assert_fits(output, "The index output path")

    result = scan.scan(root)
    markdown = render_markdown(root, result.items, result.skipped, result.naming)

    written = False
    if args.apply:
        # Single write of a fully built string: a failure while rendering can
        # never leave a half-written index behind.
        with open(paths.to_extended(output), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        written = True

    if not args.json:
        for item in result.items:
            number = "-" if item["number"] is None else item["number"]
            process.log(
                f"  {number}: {item['date'] or '-'} {item['title']} "
                f"summary={'yes' if item['summary'] else 'no'} "
                f"images={item['images']} "
                f"source={item['source']['file'] or '-'}"
            )
        for entry in result.skipped:
            process.log(f"  skipped: {entry['name']} -- {entry['reason']}", "warn")

    process.log(
        f"index: {len(result.items)} item(s), {len(result.skipped)} skipped -> {output}"
        f"{'' if written else ' (dry run; pass -Apply to write)'}"
    )

    return Outcome(
        ok=True,
        data={
            "root": root,
            "output": output,
            "dryRun": not args.apply,
            "written": written,
            "naming": result.naming,
            "items": result.items,
            "skipped": result.skipped,
        },
    )

"""``index``: regenerate the ``README.md`` index of a summary library.

A "library" is a folder of item folders named after the established reference
convention::

    <number> - <DD.MM.YYYY> - <title>

e.g. ``12 - 06.05.2020 - Заметки``. Each item folder may carry a ``summary.md``
(the prose the ``zoombie-summarize`` skill writes) and an ``img/`` folder with a
``manifest.json`` sidecar (written by :mod:`zoombie.commands.readpdf`).

The scan is deliberately **regex + :mod:`os.path`**, never ``pathlib``:

* the folder name is parsed with :data:`FOLDER_RE`, so the three fields are
  extracted independently. ``os.path.splitext`` would cut at the FIRST dot and
  turn ``12 - 06.05.2020 - Заметки`` into ``12 - 06.05``, and
  ``pathlib.Path().stem`` mangles it the same way;
* ``os.path.splitext`` is still used where it belongs -- on a *file* name, to
  find the ``<base>.md`` next to a transcript.

**Dry run by default.** The index is written only with ``-Apply``, and the whole
document is built in memory and written once, so a failure halfway through can
never leave a half-written ``README.md`` behind. This mirrors
:mod:`zoombie.commands.postprocess` exactly, and for the same reason: a skill
calls this command, so it must not be able to corrupt a file by accident.

:func:`scan_library` is the reusable half. The summarize skill's "reindex the
workspace" offer calls it directly, so the offer and the command can never
disagree about what the library contains.
"""

from __future__ import annotations

import os
import re

from ..cli import Outcome
from ..lib import paths, process
from ..lib.errors import ZoombieError
from ..lib.textnorm import percent_encode_dest

# The item-folder naming convention: number, DD.MM.YYYY date, free-text title.
# The title group is allowed to be empty so a folder that lost its title is
# still indexed (with an empty title) rather than silently skipped.
FOLDER_RE = re.compile(r"^(\d+)\s*-\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(.*)$")

SUMMARY_NAME = "summary.md"
MANIFEST_NAME = "manifest.json"
IMAGE_DIR_NAME = "img"
DEFAULT_OUTPUT_NAME = "README.md"

# The first ATX H1 of a document: the item's display title.
H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _read_text(path: str) -> str | None:
    """Read a UTF-8 (BOM-tolerant) file, or ``None`` when it is unreadable."""
    try:
        with open(paths.to_extended(path), "r", encoding="utf-8-sig") as handle:
            return handle.read()
    except OSError:
        return None


def title_of(summary_text: str | None, fallback: str) -> str:
    """The item's display title: the summary's H1, else the folder-name title.

    The H1 is what a reader sees, so it wins when it exists; the folder-name
    title (group 3 of :data:`FOLDER_RE`) is the fallback, which is what an item
    folder without a ``summary.md`` still reports.
    """
    if summary_text:
        match = H1_RE.search(summary_text)
        if match:
            title = match.group(1).strip()
            if title:
                return title
    return fallback.strip()


def image_count(item_dir: str) -> int:
    """Number of ``img/*.png`` files in an item folder."""
    image_dir = os.path.join(item_dir, IMAGE_DIR_NAME)
    if not paths.is_dir(image_dir):
        return 0
    return sum(
        1
        for entry in paths.list_dir(image_dir, files=True)
        if entry.name.lower().endswith(".png")
    )


def manifest_of(item_dir: str) -> str | None:
    """The ``manifest.json`` an item folder carries, or ``None``.

    Two locations are accepted because they are both produced by this toolchain:
    ``readpdf`` writes the sidecar into the image directory (``img/manifest.json``
    by default), while an explicit ``-ImageDir <item>`` puts it beside the
    Markdown. Checking both means an item is never reported as manifest-less
    merely because a non-default image directory was used.
    """
    for candidate in (
        os.path.join(item_dir, IMAGE_DIR_NAME, MANIFEST_NAME),
        os.path.join(item_dir, MANIFEST_NAME),
    ):
        if paths.is_file(candidate):
            return candidate
    return None


def scan_library(root: str) -> list[dict]:
    """Every recognizable item folder under ``root``, sorted by number.

    Returns one dict per item with the keys the CLI reports: ``number``, ``date``,
    ``title``, ``path``, ``summary`` (the ``summary.md`` path, or ``None``),
    ``imageCount`` and ``hasManifest``. Folders that do not match
    :data:`FOLDER_RE` are not items and are omitted; :func:`scan_library_detail`
    returns them separately as ``skipped``.

    Sorting is **numeric** (``2`` before ``10``), not lexicographic, which is the
    whole reason the number is captured as an ``int`` rather than kept as text.
    """
    return scan_library_detail(root)[0]


def scan_library_detail(root: str) -> tuple[list[dict], list[dict]]:
    """``(items, skipped)`` for ``root`` -- the full scan behind :func:`scan_library`.

    ``skipped`` names every immediate subfolder that is not an item folder, with
    the reason, so a caller can report a mis-named folder instead of silently
    dropping it from the index.
    """
    items: list[dict] = []
    skipped: list[dict] = []

    for entry in paths.list_dir(root, dirs=True):
        match = FOLDER_RE.match(entry.name)
        if match is None:
            skipped.append(
                {
                    "name": entry.name,
                    "path": entry.path,
                    "reason": "does not match '<number> - <DD.MM.YYYY> - <title>'",
                }
            )
            continue

        summary_path = os.path.join(entry.path, SUMMARY_NAME)
        has_summary = paths.is_file(summary_path)
        summary = _read_text(summary_path) if has_summary else None

        items.append(
            {
                "number": int(match.group(1)),
                "date": match.group(2),
                "title": title_of(summary, match.group(3)),
                "path": entry.path,
                "summary": summary_path if has_summary else None,
                "imageCount": image_count(entry.path),
                "hasManifest": manifest_of(entry.path) is not None,
            }
        )

    items.sort(key=lambda item: (item["number"], item["date"]))
    skipped.sort(key=lambda item: item["name"].lower())
    return items, skipped


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


def render_table(root: str, items: list[dict]) -> str:
    """The Markdown table, one row per item folder.

    The link destination goes through :func:`percent_encode_dest` -- the same
    helper ``postprocess`` uses for its links -- so a folder name containing a
    space is a valid destination while the Cyrillic in it stays human-readable
    rather than turning into ``%D0%97`` noise.
    """
    lines = [
        "| # | Date | Title | Summary | Img | Manifest |",
        "|---|------|-------|---------|-----|----------|",
    ]
    for item in items:
        number = str(item["number"])
        if item["summary"]:
            relative = os.path.relpath(item["summary"], root).replace("\\", "/")
            summary = f"[{SUMMARY_NAME}]({percent_encode_dest(relative)})"
        else:
            summary = "-"
        images = str(item["imageCount"]) if item["imageCount"] else "-"
        manifest = "yes" if item["hasManifest"] else "-"
        lines.append(
            f"| {number} | {_cell(item['date'])} | {_cell(item['title'])} "
            f"| {summary} | {images} | {manifest} |"
        )
    return "\n".join(lines)


def render_markdown(root: str, items: list[dict], skipped: list[dict]) -> str:
    """The full ``README.md`` text for a library.

    Built entirely from the scan result, so two runs over an unchanged library
    produce byte-identical output -- which is what makes ``-Apply`` safe to run
    again and what a caller can diff.
    """
    parts = [
        "# Library index",
        "",
        f"Items: {len(items)}. Generated by `zoombie index` from `{root}`.",
        "",
    ]
    parts.append(render_table(root, items) if items else "_No item folders found._")

    if skipped:
        parts.extend(
            [
                "",
                "## Skipped",
                "",
                "Folders that do not follow the `<number> - <DD.MM.YYYY> - <title>`"
                " convention:",
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

    items, skipped = scan_library_detail(root)
    markdown = render_markdown(root, items, skipped)

    written = False
    if args.apply:
        # Single write of a fully built string: a failure while rendering can
        # never leave a half-written index behind.
        with open(paths.to_extended(output), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        written = True

    if not args.json:
        for item in items:
            process.log(
                f"  {item['number']}: {item['date']} {item['title']} "
                f"summary={'yes' if item['summary'] else 'no'} "
                f"images={item['imageCount']} manifest={'yes' if item['hasManifest'] else 'no'}"
            )
        for entry in skipped:
            process.log(f"  skipped: {entry['name']} -- {entry['reason']}", "warn")

    process.log(
        f"index: {len(items)} item(s), {len(skipped)} skipped -> {output}"
        f"{'' if written else ' (dry run; pass -Apply to write)'}"
    )

    return Outcome(
        ok=True,
        data={
            "root": root,
            "output": output,
            "dryRun": not args.apply,
            "written": written,
            "items": items,
            "skipped": skipped,
        },
    )

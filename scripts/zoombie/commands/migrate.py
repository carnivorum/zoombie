"""``migrate``: bring a pre-item-model library onto the item layout.

The old layout put everything an item owned in its own root::

    <item>/                       <item>/
        summary.md                    summary.md      (unchanged)
        img/                          <source media>  (unchanged, if present)
        001 - p01.png             ->  .data/
        manifest.json                     img/
        README.md                             001 - p01.png
        transcript.txt                        manifest.json
        transcript.srt                        README.md
        transcript.source.json                transcript.txt
                                              transcript.srt
                                              source.json
                                              item.json

Two properties matter more than the moves themselves:

* **Dry run by default**, and the whole plan is computed before anything moves, so
  ``-Apply`` either completes or reports what it could not do. ``index`` and
  ``postprocess`` set this precedent because a skill calls them; this command
  moves user files, which is a stronger reason still.
* **Nothing is renamed inside ``img/``.** The image files and their two sidecars
  move as a directory, so image numbering, the manifest's own references and the
  ownership test in :func:`zoombie.commands.readpdf` all keep working. Only the
  *prefix* changes in ``summary.md``.

The folder name is never changed and never validated: a migration that renamed a
user's folders to satisfy a parser would be exactly the failure this design exists
to remove.
"""

from __future__ import annotations

import os
import re

from ..cli import Outcome
from ..item import meta as item_meta, paths as item_paths, registry, scan
from ..lib import io, paths, process
from ..lib.errors import ZoombieError

__all__ = ["plan_item", "apply_item", "rewrite_image_links", "run"]

# The old on-disk image directory, and the old link prefix that addressed it.
LEGACY_IMAGE_DIR = "img"
LEGACY_IMAGE_PREFIX = "img/"

# A sidecar from ``transcribe``/``pipeline``: ``<base>.source.json``.
_SOURCE_RE = re.compile(r"\.source\.json$", re.IGNORECASE)


def rewrite_image_links(text: str, old_prefix: str, new_prefix: str) -> tuple[str, int]:
    """Re-address image links from ``old_prefix`` to ``new_prefix``.

    Only the prefix is replaced, so a percent-encoded file name (``001%20-%20p01.png``)
    survives byte-for-byte -- which is what keeps the result identical to what
    ``postprocess`` would have written. The spans come from the shared
    balanced-paren scanner in :mod:`zoombie.commands.postprocess`, so a destination
    containing parentheses is read whole rather than truncated mid-link.

    This MUST run before any ``postprocess -Apply`` on a migrated document: the
    strip pass matches the image path, so a document whose links still said ``img/``
    while the files lived in ``.data/img/`` would accumulate a duplicate of every
    figure on each pass.
    """
    from .postprocess import iter_link_spans

    edits: list[tuple[int, int, str]] = []
    for _label_start, _label, dest_start, dest_end in iter_link_spans(text):
        dest = text[dest_start:dest_end]
        if dest.startswith(old_prefix):
            edits.append((dest_start, dest_end, new_prefix + dest[len(old_prefix):]))

    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return text, len(edits)


def _root_files(item_dir: str) -> list[str]:
    return sorted(entry.name for entry in paths.list_dir(item_dir, files=True))


def plan_item(item_dir: str) -> dict:
    """What migrating ``item_dir`` would do. Reads only; writes nothing."""
    name = os.path.basename(item_dir.rstrip("\\/"))
    data_dir = item_paths.data_dir(item_dir)
    already = paths.is_dir(data_dir)

    moves: list[dict] = []
    conflicts: list[str] = []

    def _plan(source: str, destination: str) -> None:
        if not paths.exists(source):
            return
        if paths.exists(destination):
            conflicts.append(
                f"{os.path.relpath(destination, item_dir)} already exists; "
                f"{os.path.relpath(source, item_dir)} left in place"
            )
            return
        moves.append({"from": source, "to": destination})

    # 1. the image directory, moved WHOLE -- no renames inside it.
    _plan(
        os.path.join(item_dir, LEGACY_IMAGE_DIR),
        item_paths.image_dir(item_dir),
    )

    # 2. the transcript pair and the origin sidecar, to their fixed names.
    for file_name in _root_files(item_dir):
        lowered = file_name.lower()
        if lowered in {"summary.md", "readme.md"}:
            continue
        if _SOURCE_RE.search(file_name):
            _plan(os.path.join(item_dir, file_name), item_paths.source_path(item_dir))
        elif lowered.endswith(".txt"):
            _plan(os.path.join(item_dir, file_name), item_paths.transcript_path(item_dir, "txt"))
        elif lowered.endswith(".srt"):
            _plan(os.path.join(item_dir, file_name), item_paths.transcript_path(item_dir, "srt"))

    legacy = item_meta.legacy_fields(name)
    stored = item_meta.read(item_dir)
    summary_path = item_paths.summary_path(item_dir)
    summary_text = io.read_text(summary_path) if paths.is_file(summary_path) else None
    title = item_meta.title_from_summary(summary_text, legacy["title"] or name)

    link_rewrites = 0
    if summary_text:
        _updated, link_rewrites = rewrite_image_links(
            summary_text, LEGACY_IMAGE_PREFIX, f"{item_paths.DATA_DIR_NAME}/{LEGACY_IMAGE_DIR}/"
        )

    return {
        "item": item_dir,
        "name": name,
        "summary": paths.is_file(summary_path),
        "alreadyMigrated": already and not moves,
        "moves": moves,
        "conflicts": conflicts,
        "linkRewrites": link_rewrites,
        "itemJson": {
            "number": legacy["number"] if not stored else stored.get("number"),
            "date": legacy["date"] if not stored else stored.get("date"),
            "title": title,
        },
    }


def apply_item(plan: dict) -> dict:
    """Perform one planned migration. Returns applied moves and the item.json path."""
    item_dir = plan["item"]
    paths.ensure_dir(item_paths.data_dir(item_dir))

    applied: list[dict] = []
    for move in plan["moves"]:
        paths.move(move["from"], move["to"])
        applied.append(move)

    # Write item.json only after the material is in place, so an interrupted run
    # leaves an item that is still recognisable rather than a metadata file
    # describing a layout that does not exist yet.
    fields = plan["itemJson"]
    written = item_meta.write(
        item_dir,
        number=fields["number"],
        date=fields["date"],
        title=fields["title"],
    )

    summary_path = item_paths.summary_path(item_dir)
    rewrites = 0
    if paths.is_file(summary_path):
        text = io.read_text(summary_path)
        updated, rewrites = rewrite_image_links(
            text, LEGACY_IMAGE_PREFIX, f"{item_paths.DATA_DIR_NAME}/{LEGACY_IMAGE_DIR}/"
        )
        if updated != text:
            io.write_text(summary_path, updated)

    return {"applied": applied, "itemJson": written, "linkRewrites": rewrites}


def run(args) -> Outcome:
    """Entry point behind ``zoombie migrate``."""
    if not args.dir:
        raise ZoombieError("Pass -Dir <library-root>.")

    root = paths.absolute(args.dir)
    if not paths.is_dir(root):
        raise ZoombieError(f"Directory not found: {root}")
    paths.assert_fits(root, "The library root")

    result = scan.scan(root)

    # A folder that follows the OLD convention is an item even before it has a
    # .data/ directory, which is precisely what ``scan`` cannot see -- it
    # recognises the new layout. So the legacy names are added here.
    legacy_only: list[dict] = []
    for name in registry.child_names(root):
        item_dir = registry.item_directory(root, name)
        if item_paths.is_item(item_dir):
            continue
        if item_meta.LEGACY_FOLDER_RE.match(name) and paths.is_dir(os.path.join(item_dir, LEGACY_IMAGE_DIR)):
            legacy_only.append({"name": name, "path": item_dir})

    plans = [plan_item(item["path"]) for item in result.items]
    plans.extend(plan_item(item["path"]) for item in legacy_only)

    pending = [plan for plan in plans if plan["moves"] or not plan["alreadyMigrated"]]
    for plan in pending:
        plan["needsWork"] = True

    moved = 0
    written: list[str] = []
    if args.apply:
        for plan in pending:
            outcome = apply_item(plan)
            moved += len(outcome["applied"])
            written.append(outcome["itemJson"])

        # Pin the convention once, so a second machine cannot re-measure and
        # disagree about how new items in this library should be named.
        verdict = result.naming
        registry.write_record(
            root,
            {
                "convention": verdict.get("convention"),
                "confidence": verdict.get("confidence"),
                "description": verdict.get("description"),
                "recordedAt": process.utc_now_iso(),
            },
        )

    if not args.json:
        for plan in pending:
            if not plan["moves"] and plan["alreadyMigrated"]:
                continue
            process.log(f"  {plan['name']}: {len(plan['moves'])} move(s), "
                        f"{plan['linkRewrites']} link(s)")
            for move in plan["moves"]:
                process.log(f"      {os.path.relpath(move['from'], plan['item'])}"
                            f" -> {os.path.relpath(move['to'], plan['item'])}")
            for conflict in plan["conflicts"]:
                process.log(f"      skip: {conflict}", "warn")

    process.log(
        f"migrate: {len(pending)} item(s) planned, {moved} move(s) applied"
        f"{'' if args.apply else ' (dry run; pass -Apply to write)'}"
    )

    return Outcome(
        ok=True,
        data={
            "root": root,
            "dryRun": not args.apply,
            "planned": len(pending),
            "moved": moved,
            "itemJsonWritten": written if args.apply else [],
            "plans": plans,
        },
    )

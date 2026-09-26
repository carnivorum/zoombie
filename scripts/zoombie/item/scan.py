"""One scan of a workspace, shared by ``items``.

Two commands need to know what a directory holds -- the enumeration the agent
calls, and the library index a human reads -- and if each parsed the tree itself
they would eventually disagree about what an item is. So the tree is read here
once, and both render from the result.

Recognition is :func:`zoombie.item.paths.is_item` (a ``summary.md``); the title
comes from the summary's own H1, and the number from the naming convention
measured over the siblings. **No part of this module requires a folder name to
parse**, which is what lets a user name their folders anything at all.

A finished item now holds only its document, its media and its ``img/`` figures
-- there is no ``.data/`` sidecar to report -- so the record is correspondingly
smaller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..lib import paths, process
from . import paths as item_paths, registry

__all__ = ["ScanResult", "scan"]

# Media extensions worth reporting as "the source this summary was made from".
SOURCE_EXTENSIONS = (
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac",
    ".pdf",
)

_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass
class ScanResult:
    """What a directory holds, plus what it says about naming."""

    root: str
    items: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    naming: dict = field(default_factory=dict)

    @property
    def next_number(self) -> int | None:
        return self.naming.get("nextNumber")

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "count": len(self.items),
            "nextNumber": self.naming.get("nextNumber"),
            "naming": self.naming,
            "items": self.items,
            "skipped": self.skipped,
        }


def _source_of(item_dir: str) -> dict:
    """The source media at the item root, as ``{"file", "kind", "present"}``.

    Read from the media actually on disk, never trusted from a sidecar: a user may
    delete a 900 MB video and keep the summary, and a scan that reported the file
    as present would be lying about the folder in front of them.
    """
    media = sorted(
        entry.name
        for entry in paths.list_dir(item_dir, files=True)
        if entry.name.lower().endswith(SOURCE_EXTENSIONS)
    )
    if media:
        return {"file": media[0], "kind": _kind_of(media[0]), "present": True,
                "extra": media[1:]}
    return {"file": None, "kind": None, "present": False, "extra": []}


def _kind_of(file_name: str) -> str:
    if file_name.lower().endswith(".pdf"):
        return "pdf"
    if file_name.lower().rsplit(".", 1)[-1] in {"mp3", "wav", "m4a", "flac", "ogg", "opus", "aac"}:
        return "audio"
    return "video"


def _holds_source_material(item_dir: str) -> bool:
    """True when a non-item folder holds source material but no summary.

    Such a folder is a download awaiting its write-up, and the index should say so
    rather than describe it as an unrelated folder.
    """
    for entry in paths.list_dir(item_dir, files=True):
        if entry.name.lower().endswith(SOURCE_EXTENSIONS):
            return True
    return False


def _title_of(item_dir: str) -> str:
    """The summary's H1, or the folder name when it has none."""
    summary = item_paths.summary_path(item_dir)
    if paths.is_file(summary):
        try:
            with open(paths.to_extended(summary), "r", encoding="utf-8-sig") as handle:
                match = _H1_RE.search(handle.read())
        except OSError:
            match = None
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def _item_record(item_dir: str, name: str) -> dict:
    """One item, described in the shape the CLI publishes."""
    return {
        "name": name,
        "path": item_dir,
        "title": _title_of(item_dir) or name,
        "summary": paths.is_file(item_paths.summary_path(item_dir)),
        "source": _source_of(item_dir),
        "images": item_paths.image_count(item_dir),
    }


def _children(root: str) -> ScanResult:
    """The immediate children of ``root``: items, non-items, and the naming verdict.

    Non-items are **reported with a reason, never silently dropped** -- a
    mis-recognized folder that vanishes from the index is the failure mode this
    design exists to remove.
    """
    result = ScanResult(root=root)
    names = registry.child_names(root)

    for name in names:
        item_dir = registry.item_directory(root, name)
        if item_paths.is_item(item_dir):
            result.items.append(_item_record(item_dir, name))
        else:
            result.skipped.append(
                {
                    "name": name,
                    "path": item_dir,
                    "kind": "media" if _holds_source_material(item_dir) else "folder",
                    "reason": "no summary.md",
                }
            )

    verdict = registry.measure(names)
    result.naming = verdict.to_dict()
    result.naming["itemCount"] = len(result.items)
    result.naming["nextNumber"] = _successor_number(
        result.items, result.naming.get("convention")
    )

    result.items.sort(key=_sort_key)
    result.skipped.sort(key=lambda entry: entry["name"].lower())
    return result


def _successor_number(items: list[dict], convention_id: str | None) -> int | None:
    """The next item number, or ``None`` when numbering is not in use.

    Suppressed under a ``title-only`` convention with no numbered items, so a
    caller is never invited to invent a number the workspace does not use.
    """
    existing = [registry.LEGACY_FOLDER_RE.match(item["name"]) for item in items]
    numbers = [int(match.group(1)) for match in existing if match]
    if convention_id == "title-only" and not numbers:
        return None
    return registry.next_number(numbers)


def _relative(root: str, path: str) -> str:
    """``path`` relative to ``root``, with forward slashes and no leading separator."""
    return path[len(root):].lstrip("\\/").replace("\\", "/")


def _sort_key(item: dict) -> tuple:
    """Items in reading order: number, then title."""
    match = registry.LEGACY_FOLDER_RE.match(item["name"])
    number = int(match.group(1)) if match else None
    return (
        0 if isinstance(number, int) else 1,
        number if isinstance(number, int) else 0,
        item["title"].lower(),
    )


def scan(root: str, depth: int = 1) -> ScanResult:
    """Scan ``root``: the items it holds, the non-items, and the naming verdict.

    ``depth`` is how many levels of children to consider; ``depth=2`` also looks
    inside each non-item child for a nested collection. A directory that is itself
    an item is reported as one and **not descended into** at any depth -- an item's
    ``img/`` is its material, not a nested workspace.
    """
    depth = max(1, depth)
    result = _children(root)

    if depth <= 1:
        result.naming["nextNumber"] = _successor_number(
            result.items, result.naming.get("convention")
        )
        return result

    for name in registry.child_names(root):
        child = registry.item_directory(root, name)
        if item_paths.is_item(child):
            continue
        nested = scan(child, depth - 1)
        for item in nested.items:
            found = dict(item)
            found["relative"] = _relative(root, found["path"])
            result.items.append(found)

    for item in result.items:
        if "relative" not in item:
            item["relative"] = _relative(root, item["path"])

    result.items.sort(key=_sort_key)
    result.naming["itemCount"] = len(result.items)
    result.naming["nextNumber"] = _successor_number(
        result.items, result.naming.get("convention")
    )
    return result


def log_scan(result: ScanResult) -> None:
    """Human-readable progress on stderr; stdout stays the one JSON line."""
    for item in result.items:
        process.log(
            f"  {item['name']} "
            f"summary={'yes' if item['summary'] else 'no'} "
            f"images={item['images']} "
            f"source={item['source']['file'] or '-'}"
        )
    for entry in result.skipped:
        process.log(f"  skipped: {entry['name']} -- {entry['reason']}", "warn")

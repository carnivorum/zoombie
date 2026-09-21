"""One scan of a workspace, shared by ``items`` and ``index``.

Two commands need to know what a directory holds -- the enumeration the agent
calls, and the library index a human reads -- and if each parsed the tree itself
they would eventually disagree about what an item is. So the tree is read here
once, and both render from the result.

Recognition is :func:`zoombie.item.paths.is_item` (a ``summary.md`` or a ``.data/``
directory); metadata falls back through ``item.json``, then a legacy folder name,
then the summary's H1. **No part of this module requires a folder name to parse**,
which is what lets a user name their folders anything at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..lib import paths, process
from . import meta, paths as item_paths, registry

__all__ = ["ScanResult", "scan"]

# Media extensions worth reporting as "the source this summary was made from".
SOURCE_EXTENSIONS = (
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac",
    ".pdf",
)


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


def _source_of(item_dir: str, stored: dict | None) -> dict:
    """The source media at the item root, as ``{"file", "kind", "present"}``.

    Read from the media actually on disk rather than trusted from metadata: a user
    may delete a 900 MB video and keep the summary, and a scan that reported the
    file as present would be lying about the folder in front of them.
    """
    files = [entry.name for entry in paths.list_dir(item_dir, files=True)]
    media = [
        name
        for name in files
        if name.lower().endswith(SOURCE_EXTENSIONS)
    ]
    media.sort()
    sidecar = (stored or {}).get("source") or {}
    if media:
        return {
            "file": media[0],
            "kind": sidecar.get("kind") or _kind_of(media[0]),
            "present": True,
            "extra": media[1:],
        }
    return {
        "file": sidecar.get("file"),
        "kind": sidecar.get("kind"),
        "present": False,
        "extra": [],
    }


def _kind_of(file_name: str) -> str:
    if file_name.lower().endswith(".pdf"):
        return "pdf"
    if file_name.lower().rsplit(".", 1)[-1] in {"mp3", "wav", "m4a", "flac", "ogg", "opus", "aac"}:
        return "audio"
    return "video"


def _item_record(item_dir: str, name: str) -> dict:
    """One item, described in the shape the CLI publishes."""
    resolved = meta.fields(item_dir, name)
    warnings: list[str] = []
    if resolved["number"] is None:
        warnings.append("no number in item.json or the folder name")
    if resolved["date"] is None:
        warnings.append("no date in item.json or the folder name")
    if not resolved["stored"]:
        warnings.append("no .data/item.json")

    data_dir = item_paths.data_dir(item_dir)
    return {
        "name": name,
        "path": item_dir,
        "number": resolved["number"],
        "date": resolved["date"],
        "title": resolved["title"],
        "summary": paths.is_file(item_paths.summary_path(item_dir)),
        "source": _source_of(item_dir, resolved["stored"]),
        "transcript": {
            "txt": paths.is_file(item_paths.transcript_path(item_dir, "txt")),
            "srt": paths.is_file(item_paths.transcript_path(item_dir, "srt")),
        },
        "images": item_paths.image_count(item_dir),
        "hasManifest": paths.is_file(item_paths.manifest_path(item_dir)),
        "dataDir": data_dir if paths.is_dir(data_dir) else None,
        "warnings": warnings,
    }


def _children(root: str) -> ScanResult:
    """The immediate children of ``root``: items, non-items, and the naming verdict.

    Private, and deliberately so: :func:`scan` is the entry point, and the only
    caller that wants one level is :func:`scan` itself while recursing. Exposing
    both -- as ``scan_root`` and ``scan_children`` were -- is what let the recursive
    totals go stale, because a second public way in had to remember to fix up what
    the first had already computed.

    Non-items are **reported with a reason, never silently dropped** -- a
    mis-recognized folder that vanishes from the index is the failure mode this
    whole redesign exists to remove.
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
                    "reason": "no summary.md and no .data/ directory",
                }
            )

    # The naming verdict is measured over the ITEMS plus the other siblings, so a
    # workspace of plainly-named item folders reports "title-only" rather than
    # being read as convention-less just because those folders are ours.
    verdict = registry.measure(names)
    result.naming = verdict.to_dict()
    result.naming["itemCount"] = len(result.items)

    # ...but a STRONG verdict is CAPPED to weak unless the items are also a real
    # SHARE of the folders that were measured. A directory of source folders
    # (``scripts``, ``tests``, ``plans``) is "plainly named" too, and reporting
    # that as a strong convention would have the toolchain confidently proposing
    # names for a folder that holds no items at all -- but the same is true of
    # nine source folders beside one real item, which a mere "zero items" test
    # could not catch. The convention is still reported (detecting ``a``/``b``/``c``
    # in a target directory is the point), just as a weak signal to put to the
    # user, and the reason is recorded so a caller can explain the downgrade.
    if verdict.confidence == "strong":
        if not result.items:
            result.naming["confidence"] = "weak"
            result.naming["cappedBecauseNoItems"] = True
        elif not _items_are_a_share(len(result.items), verdict.total):
            result.naming["confidence"] = "weak"
            result.naming["cappedBecauseFewItems"] = True

    # The successor number considers existing items, not arbitrary siblings, so a
    # stray folder named "99 - notes" cannot push the next item to 100.
    existing = [item["number"] for item in result.items]
    result.naming["nextNumber"] = registry.next_number(existing)

    result.items.sort(key=_sort_key)
    result.skipped.sort(key=lambda entry: entry["name"].lower())
    return result


def _items_are_a_share(item_count: int, sample_count: int, *, share: float = 0.5) -> bool:
    """True when the items are at least ``share`` of the folders measured.

    The naming verdict is measured over EVERY sibling, because a workspace of
    plainly-named item folders must read as ``title-only``. That means the sample
    set is larger than the item set whenever there are non-item folders, and a
    strong convention can be read from folders that are OURS rather than the
    user's -- ``scripts``, ``tests`` and ``plans`` share a shape just as surely as
    three dated items do.

    Requiring the items to be half the samples is the honest boundary: below it
    the verdict describes the toolchain's own directories more than it describes
    the library, so it is reported weakly and the reason is recorded.

    ``sample_count`` of zero cannot arise here -- a strong verdict needs agreeing
    samples -- but the guard keeps the arithmetic total rather than dividing by a
    count that a future refactor could leave empty.
    """
    if sample_count <= 0:
        return False
    return item_count >= sample_count * share


def _relative(root: str, path: str) -> str:
    """``path`` relative to ``root``, with forward slashes and no leading separator.

    Computed by string slicing rather than :func:`os.path.relpath` because ``root``
    here is the prefix the path was BUILT from, so the two always agree. Normalizing
    the separator matters: a caller prints this value, and a mixed ``one/two\\three``
    reads as a mistake.
    """
    return path[len(root):].lstrip("\\/").replace("\\", "/")


def _sort_key(item: dict) -> tuple:
    """Items in reading order: number, then date, then title.

    A missing number sorts last rather than first -- an unnumbered item is not
    item #0, and a dateless one (`""`) still sorts consistently against dated ones.
    """
    number = item["number"]
    return (
        0 if isinstance(number, int) else 1,
        number if isinstance(number, int) else 0,
        item["date"] or "",
        item["title"].lower(),
    )


def scan(root: str, depth: int = 1) -> ScanResult:
    """Scan ``root``: the items it holds, the non-items, and the naming verdict.

    ``depth`` is how many levels of children to consider. The default of 1 looks at
    ``root``'s immediate children, which is what a library is; ``depth=2`` also
    looks inside each non-item child for a nested collection, and so on. There is
    one function rather than a shallow one and a recursive one because the totals
    (``itemCount``, ``nextNumber``) are computed from whatever was found, and a
    second entry point is how those two drifted apart.

    A ``depth`` below 1 is read as 1 rather than recursing forever: the value
    arrives from a CLI flag, and a negative one must not become an infinite walk.

    A directory that is itself an item is reported as one and **not descended
    into** at any depth -- an item's ``.data/`` is its internals, not a nested
    workspace. That rule is what keeps a deeper scan from reporting every image
    directory as a skippable folder.
    """
    depth = max(1, depth)
    result = _children(root)

    if depth <= 1:
        # One level: what ``_children`` computed IS the answer.
        result.naming["nextNumber"] = registry.next_number(
            [item["number"] for item in result.items]
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

    # Every item gets a ``relative`` key once the scan goes deeper than one level,
    # including the ones found DIRECTLY under the root. Those come from ``_children``
    # with no such key, so without this a caller would be handed a list where some
    # records carry ``relative`` and some do not -- and the key's whole purpose is to
    # let a caller address a nested item without reconstructing the path.
    for item in result.items:
        if "relative" not in item:
            item["relative"] = _relative(root, item["path"])

    result.items.sort(key=_sort_key)

    # Recomputed over EVERYTHING found, not just the immediate children. Leaving
    # these stale under-reports a deeper scan twice over: the count is short, and
    # the next number ignores nested items -- so a library whose item 7 lives one
    # folder down would be handed 7 again. The confidence cap reads ``itemCount``
    # too, so a stale value would also downgrade a genuinely strong verdict.
    result.naming["itemCount"] = len(result.items)
    result.naming["nextNumber"] = registry.next_number(
        [item["number"] for item in result.items]
    )
    return result


def log_scan(result: ScanResult) -> None:
    """Human-readable progress on stderr; stdout stays the one JSON line."""
    for item in result.items:
        process.log(
            f"  {item['number'] if item['number'] is not None else '-'}: "
            f"{item['date'] or '-'} {item['title']} "
            f"summary={'yes' if item['summary'] else 'no'} "
            f"images={item['images']} "
            f"source={item['source']['file'] or '-'}"
        )
    for entry in result.skipped:
        process.log(f"  skipped: {entry['name']} -- {entry['reason']}", "warn")

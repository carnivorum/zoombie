"""``item.json``: the item's metadata, held OFF its folder name.

Once the name is the user's to choose, ``number`` / ``date`` / ``title`` have
nowhere to live -- so they live here, exactly as an origin URL lives in
``source.json`` rather than in a file name
(:func:`zoombie.lib.stt.source_metadata`). The folder name becomes presentation.

:data:`LEGACY_FOLDER_RE` is the old ``<number> - <DD.MM.YYYY> - <title>`` pattern.
It is **demoted from runtime gate to legacy-name reader**: it is used only to
back-fill metadata for an item that predates ``item.json``, and by the migration
pass. Recognition no longer consults it -- see :func:`zoombie.item.paths.is_item`.
"""

from __future__ import annotations

import os
import re

from .. import SKILL_VERSION
from ..lib import io, paths
from . import paths as item_paths, registry

__all__ = [
    "LEGACY_FOLDER_RE",
    "read",
    "write",
    "fields",
    "title_from_summary",
    "legacy_fields",
    "normalize_date",
    "resolve_number",
]

# The historical convention. Kept because the past is full of these names and a
# migration has to read them; never consulted to decide whether something is an
# item.
LEGACY_FOLDER_RE = re.compile(r"^(\d+)\s*-\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(.*)$")

# The first ATX H1 of a document: the item's display title.
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def read(item_dir: str) -> dict | None:
    """Parse ``<item>/.data/item.json``, or ``None`` when absent or unreadable."""
    return io.read_json(item_paths.item_path(item_dir))


def write(
    item_dir: str,
    *,
    number: int | None,
    date: str | None,
    title: str,
    source: dict | None = None,
) -> str:
    """Write ``<item>/.data/item.json``; return the path.

    ``date`` is stored **ISO** (``2020-05-06``), never ``DD.MM.YYYY``. Two reasons:
    an ISO string sorts chronologically, which the old display-format keys did not
    -- within a repeated number ``01.06.2020`` sorted before ``31.12.2019`` -- and
    the display format becomes a rendering decision (:func:`registry.date_display`)
    instead of something baked into stored data.
    """
    payload = {
        "number": number,
        "date": date,
        "title": title,
        "source": source or {},
        "toolchainVersion": SKILL_VERSION,
    }
    path = item_paths.item_path(item_dir)
    io.write_json(path, payload)
    return path


def normalize_date(value: str | None) -> str | None:
    """Accept either date style and return ISO, or ``None`` when it is neither."""
    if not value:
        return None
    return registry.date_iso(value) or registry.date_iso(registry.date_display(value))


def title_from_summary(summary_text: str | None, fallback: str) -> str:
    """The display title: the summary's H1 when it has one, else ``fallback``.

    The H1 is what a reader sees, so it wins; the fallback is whatever the caller
    already knows, which for a migrated item is the old folder-name title.
    """
    if summary_text:
        match = _H1_RE.search(summary_text)
        if match:
            title = match.group(1).strip()
            if title:
                return title
    return (fallback or "").strip()


def legacy_fields(folder_name: str) -> dict:
    """``number`` / ``date`` / ``title`` as read from a pre-``item.json`` name.

    Returns empty values when the name does not follow the old convention, so a
    caller can fall through to the summary's H1 rather than fail.
    """
    match = LEGACY_FOLDER_RE.match(folder_name)
    if match is None:
        return {"number": None, "date": None, "title": ""}
    number, display_date, title = match.groups()
    return {
        "number": int(number),
        "date": registry.date_iso(display_date),
        "title": title.strip(),
    }


def fields(item_dir: str, folder_name: str) -> dict:
    """Resolve an item's metadata, in the documented fallback order.

    1. ``item.json`` -- authoritative, and the only writer-owned source.
    2. the legacy folder name -- for an item that predates the sidecar.
    3. the summary's H1 -- for the title, when neither of the above had one.

    Also returns ``"stored"``: whether a sidecar existed, so a caller can report a
    metadata-less item (and a migration can decide there is work to do) without
    reading the file twice.
    """
    stored = read(item_dir)
    legacy = legacy_fields(folder_name)
    summary_path = item_paths.summary_path(item_dir)
    summary_text = io.read_text(summary_path) if paths.is_file(summary_path) else None

    number = legacy["number"]
    date = legacy["date"]
    title = legacy["title"]
    if stored:
        if isinstance(stored.get("number"), int):
            number = stored["number"]
        date = normalize_date(stored.get("date")) or date
        title = (stored.get("title") or "").strip() or title

    # The summary's H1 is the LAST resort, not an override: a writer-set title in
    # item.json is authoritative, and letting the H1 win would mean the metadata a
    # command recorded could be silently replaced by a heading someone reworded.
    if not title and summary_text:
        title = title_from_summary(summary_text, title)
    return {
        "number": number,
        "date": date,
        "title": title,
        "stored": stored,
    }


def resolve_number(existing: list[int | None]) -> int:
    """The number a NEW item takes: ``max(existing) + 1``.

    Kept here rather than in the scan so the successor rule has one definition, and
    so a caller can answer "what number would this get?" without a full scan.
    """
    return registry.next_number(existing)

"""Where an item's parts live, in one place.

An item is a folder of ANY name (the user's choice) holding::

    <item>/
        summary.md            the deliverable, at the root where a human looks
        <source media>        the original video / audio / PDF, kept as-is
        img/                  ONLY the figures the summary inlines, visible

That is the whole layout. There is no ``.data/`` sidecar directory any more:
everything the toolchain used to keep there (transcripts, the origin sidecar,
the OCR report, item metadata, the image README) is either THROWAWAY -- produced
in a run scratch dir and deleted when the task finishes -- or rebuilt on demand.
A finished item therefore holds the document, the media it was made from, and
the figures the document references, and nothing else.

``img/`` is deliberately VISIBLE (no leading dot, no nested folder): a user who
moves a summary file must be able to see the folder its figures live in and move
it alongside, which a hidden ``.data/img`` defeated.

**Every function here takes the item folder -- never the name.** Nothing in this
module parses a name, and no other module may spell a path by hand.
"""

from __future__ import annotations

import os
import re

from ..lib import paths

__all__ = [
    "IMAGE_DIR_NAME",
    "SUMMARY_NAME",
    "MANIFEST_NAME",
    "TRANSCRIPT_STEM",
    "SOURCE_NAME",
    "ITEM_MARKERS",
    "image_dir",
    "summary_path",
    "manifest_path",
    "item_markers",
    "is_item",
    "is_archive_dir",
    "is_archived_summary",
    "archive_dir_name",
    "image_count",
]

# The visible image directory. Its name is also the fragment ``postprocess``
# matches to strip a previous run's figures, and ``verify`` looks for; keeping
# that one string in one place is what stops the pass and the checker drifting.
IMAGE_DIR_NAME = "img"

SUMMARY_NAME = "summary.md"
MANIFEST_NAME = "manifest.json"

# The fixed stem a transcription's artifacts hang off (``transcript.txt``,
# ``transcript.srt``). These are THROWAWAY during a summarize -- produced into the
# run scratch, read to build block 6, then deleted -- so the name is stable but
# the files are not a durable part of a finished item.
TRANSCRIPT_STEM = "transcript"

# The origin sidecar's file name. It is a RUN-LOCAL artifact now: written into
# the run scratch during a summarize (where the composition reads it for block 2's
# link) and deleted with the scratch. It deliberately has no ``<base>.`` prefix,
# so it names the origin of the run's single source rather than hanging off a base.
SOURCE_NAME = "source.json"

# Files whose presence means "this folder is one of ours". A finished item is
# recognized by its document alone: the media and the figures are the user's
# material, and requiring a sidecar would make an item unrecognizable the moment
# the toolchain stopped writing one.
ITEM_MARKERS = (SUMMARY_NAME,)

# An ARCHIVED summary, as the summarize flow leaves one when the user chooses
# -Archive. It is a FOLDER holding the superseded document AND the figures it
# inlines, so the pair stays consistent -- figures are index-numbered, so a flat
# copy beside a live ``img/`` would repoint at the wrong pictures.
#
# The pattern lives HERE, not in the producer and the checker separately: an
# archive folder holds a ``summary.md``, so without one shared definition a
# re-summarize would archive happily while ``verify`` and ``items`` counted the
# snapshot as a second item of its own.
_ARCHIVE_STAMP = r"\d{8}_\d{4}(?: \(\d+\))?"
_ARCHIVE_DIR_RE = re.compile(rf"^summary_{_ARCHIVE_STAMP}$", re.IGNORECASE)
_ARCHIVED_SUMMARY_RE = re.compile(rf"^summary_{_ARCHIVE_STAMP}\.md$", re.IGNORECASE)


def is_archive_dir(name: str) -> bool:
    """True for an archive FOLDER name, ``summary_<yyyyMMdd_HHmm>[ (n)]``."""
    return bool(_ARCHIVE_DIR_RE.match(name))


def is_archived_summary(name: str) -> bool:
    """True for a legacy flat archive name, ``summary_<yyyyMMdd_HHmm>[ (n)].md``.

    The folder form is current; this matches snapshots written by the older
    file-per-archive layout, which some items still carry.
    """
    return bool(_ARCHIVED_SUMMARY_RE.match(name))


def archive_dir_name(stamp: str, index: int = 1) -> str:
    """The archive folder for ``stamp``, suffixed by ``index`` when one is taken."""
    return f"summary_{stamp}" if index <= 1 else f"summary_{stamp} ({index})"


def image_dir(item_dir: str) -> str:
    """``<item>/img``."""
    return os.path.join(item_dir, IMAGE_DIR_NAME)


def summary_path(item_dir: str) -> str:
    """``<item>/summary.md``."""
    return os.path.join(item_dir, SUMMARY_NAME)


def manifest_path(item_dir: str) -> str:
    """``<item>/img/manifest.json`` -- the image placement sidecar.

    Written into the run scratch during a summary, and used by ``postprocess``
    for the duration of that run; it is not a durable artifact of a finished
    item, so its absence after a run is normal, not a defect.
    """
    return os.path.join(image_dir(item_dir), MANIFEST_NAME)


def item_markers(item_dir: str) -> list[str]:
    """Which recognition markers ``item_dir`` actually carries, in layout order."""
    found: list[str] = []
    for marker in ITEM_MARKERS:
        if paths.exists(os.path.join(item_dir, marker)):
            found.append(marker)
    return found


def is_item(item_dir: str) -> bool:
    """True when ``item_dir`` holds a ``summary.md`` and is not an archive.

    **Recognition is evidence-based and never parses the name.** A folder named
    ``a Заметки``, ``2020-05-06 Заметки`` or plain ``Заметки`` is equally an item
    when it holds our document.

    The ONE name-shaped exception is the archive folder: it holds a superseded
    ``summary.md`` too, so evidence alone cannot tell a snapshot from a live item.
    The pattern is specific enough (a date-stamp stem) that a user naming a real
    item ``summary_20260926_1410`` is the only false negative, and that is a fair
    trade for never reporting one snapshot as a second item.
    """
    if is_archive_dir(os.path.basename(os.path.normpath(item_dir))):
        return False
    return bool(item_markers(item_dir))


def image_count(item_dir: str) -> int:
    """Number of ``.png`` files in the item's image directory."""
    directory = image_dir(item_dir)
    if not paths.is_dir(directory):
        return 0
    return sum(
        1
        for entry in paths.list_dir(directory, files=True)
        if entry.name.lower().endswith(".png")
    )

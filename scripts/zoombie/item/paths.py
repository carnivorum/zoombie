"""Where an item's parts live, in one place.

An item is a folder of ANY name (the user's choice) holding::

    <item>/
        summary.md            the deliverable, at the root where a human looks
        <source media>        the original video / audio / PDF, when kept
        .data/                everything mechanical and derived
            img/              001 - p01.png, manifest.json, README.md
            transcript.txt
            transcript.srt
            source.json
            item.json

**Every function here takes the item folder -- never the name.** Nothing in this
module parses a name, and no other module may spell a path by hand: the eight
hard-coded ``"img"`` sites this module replaces are exactly how the layout drifted
between commands in the first place.

Why ``img/`` is nested rather than flat in ``.data/``: ``postprocess`` strips a
previous run's figures by matching a link against the literal ``img/``
(:data:`zoombie.lib.markdown.INSERTED_IMG_RE`). ``.data/img/001%20-%20p01.png``
still contains that substring, so the byte-idempotency guarantee survives the move;
``.data/001%20-%20p01.png`` would not, and every pass would append another copy of
every figure. Nesting also keeps the image directory holding image material only,
which is what lets ``readpdf`` keep its "is this our own output?" ownership test
unchanged.
"""

from __future__ import annotations

import os

from ..lib import paths

__all__ = [
    "DATA_DIR_NAME",
    "IMAGE_DIR_NAME",
    "SUMMARY_NAME",
    "MANIFEST_NAME",
    "IMAGE_README_NAME",
    "TRANSCRIPT_STEM",
    "SOURCE_NAME",
    "ITEM_NAME",
    "ITEM_MARKERS",
    "data_dir",
    "image_dir",
    "summary_path",
    "manifest_path",
    "image_readme_path",
    "transcript_path",
    "source_path",
    "item_path",
    "is_item",
    "item_markers",
    "image_count",
]

# The hidden derived-material directory. Dot-prefixed, so Explorer and a default
# ``Get-ChildItem`` hide it: the price of keeping the item folder readable.
DATA_DIR_NAME = ".data"
IMAGE_DIR_NAME = "img"

SUMMARY_NAME = "summary.md"
MANIFEST_NAME = "manifest.json"
IMAGE_README_NAME = "README.md"

# The transcript is written under fixed names, not ``<base>.txt``: the item holds
# one source, so there is nothing for a discriminating prefix to distinguish, and
# a fixed name is what lets a reader find it without knowing the source name.
TRANSCRIPT_STEM = "transcript"
SOURCE_NAME = "source.json"
ITEM_NAME = "item.json"

# Files whose presence means "this folder is one of ours". Used for ownership
# decisions -- an image directory is ours if it holds a manifest, and a folder is
# an item if it holds a summary or a .data/ directory.
ITEM_MARKERS = (SUMMARY_NAME, DATA_DIR_NAME)


def data_dir(item_dir: str) -> str:
    """``<item>/.data``."""
    return os.path.join(item_dir, DATA_DIR_NAME)


def image_dir(item_dir: str) -> str:
    """``<item>/.data/img``."""
    return os.path.join(data_dir(item_dir), IMAGE_DIR_NAME)


def summary_path(item_dir: str) -> str:
    """``<item>/summary.md``."""
    return os.path.join(item_dir, SUMMARY_NAME)


def manifest_path(item_dir: str) -> str:
    """``<item>/.data/img/manifest.json`` -- the image placement sidecar."""
    return os.path.join(image_dir(item_dir), MANIFEST_NAME)


def image_readme_path(item_dir: str) -> str:
    """``<item>/.data/img/README.md`` -- the human-readable image table."""
    return os.path.join(image_dir(item_dir), IMAGE_README_NAME)


def transcript_path(item_dir: str, extension: str) -> str:
    """``<item>/.data/transcript.<extension>`` (``txt`` or ``srt``)."""
    return os.path.join(data_dir(item_dir), f"{TRANSCRIPT_STEM}.{extension.lstrip('.')}")


def source_path(item_dir: str) -> str:
    """``<item>/.data/source.json`` -- the origin sidecar."""
    return os.path.join(data_dir(item_dir), SOURCE_NAME)


def item_path(item_dir: str) -> str:
    """``<item>/.data/item.json`` -- the item's metadata."""
    return os.path.join(data_dir(item_dir), ITEM_NAME)


def item_markers(item_dir: str) -> list[str]:
    """Which recognition markers ``item_dir`` actually carries, in layout order."""
    found: list[str] = []
    for marker in ITEM_MARKERS:
        if paths.exists(os.path.join(item_dir, marker)):
            found.append(marker)
    return found


def is_item(item_dir: str) -> bool:
    """True when ``item_dir`` holds a summary or a ``.data/`` directory.

    **Recognition is evidence-based and never parses the name.** That is the whole
    point of the item model: a folder called ``a Заметки``, ``2020-05-06 Заметки``
    or plain ``Заметки`` is equally an item when it holds one of our markers, and
    the naming-convention discussion becomes about *style* rather than about
    whether a document is Indexed at all.
    """
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

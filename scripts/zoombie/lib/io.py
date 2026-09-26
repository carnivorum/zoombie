"""Small file primitives every command shares.

Extracted so ``items``, ``summarize`` and ``postprocess`` cannot drift in
how they read a document: the encoding, the BOM tolerance and the line endings are
decided once here rather than re-chosen at each call site.

Nothing here applies the extended-length prefix itself -- that is
:func:`zoombie.lib.paths.to_extended`, which every function below goes through, so
managed I/O is long-path safe by construction.
"""

from __future__ import annotations

import json
import os

from . import paths

__all__ = ["read_text", "write_text", "read_json", "write_json"]


def read_text(path: str) -> str:
    """Read a UTF-8, BOM-tolerant text file.

    ``utf-8-sig`` rather than ``utf-8`` deliberately: a document a user saved from
    an editor that added a byte-order mark must still parse, and a BOM left in the
    string would otherwise become the first character of the first heading.
    """
    with open(paths.to_extended(path), "r", encoding="utf-8-sig") as handle:
        return handle.read()


def write_text(path: str, text: str) -> None:
    """Write UTF-8 text with LF line endings, creating the parent directory."""
    parent = os.path.dirname(path)
    if parent:
        paths.ensure_dir(parent)
    with open(paths.to_extended(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read_json(path: str) -> dict | None:
    """Parse a JSON object, or ``None``.

    ``None`` -- not an exception -- for every failure mode: missing file,
    unreadable, invalid JSON, or a payload that is not an object. Callers read an
    unreadable sidecar as "not ours", which is the same rule ``postprocess``
    already applies to a corrupt image manifest: guessing from a damaged file is
    worse than declining to act on it.
    """
    try:
        with open(paths.to_extended(path), "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json(path: str, payload: dict) -> None:
    """Write a JSON object: UTF-8, no BOM, LF endings, one trailing newline.

    The same discipline :mod:`zoombie.lib.manifest` uses, so a sidecar written by
    any command in the toolchain diffs cleanly against another.
    """
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

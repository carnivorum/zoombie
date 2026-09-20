"""Zip extraction, long-path aware.

``zipfile`` does not validate an assembled output path the way PowerShell's
``Expand-Archive`` does, but the destination still has to survive a deep tree, so
every written target carries the extended-length prefix.
"""

from __future__ import annotations

import os
import zipfile

from . import paths, process


def extract_zip(zip_path: str, destination: str, *, strict: bool = False) -> list[str]:
    """Extract a zip into ``destination``, returning the paths written.

    A single unwritable entry (an over-long archive-internal path the OS refuses
    even with the prefix) does not abort the whole extraction: it is warned about
    and skipped, and the caller verifies the files it actually needs afterwards.
    Set ``strict=True`` to raise instead, which is what the installer wants when
    it is about to depend on the extracted contents.
    """
    paths.ensure_dir(destination)
    written: list[str] = []
    with zipfile.ZipFile(paths.to_extended(zip_path)) as archive:
        for entry in archive.infolist():
            relative = entry.filename.replace("/", os.sep)
            if not relative or relative.endswith(os.sep):
                continue
            target = os.path.join(destination, relative)
            try:
                paths.ensure_dir(os.path.dirname(target))
                with archive.open(entry) as source, open(
                    paths.to_extended(target), "wb"
                ) as handle:
                    handle.write(source.read())
                written.append(target)
            except OSError as exc:
                if strict:
                    raise
                process.log(f"could not extract '{entry.filename}': {exc}", "warn")
    return written


def extract_member(zip_path: str, member: str, destination: str) -> str:
    """Extract one named member of a zip to an explicit destination path."""
    paths.ensure_dir(os.path.dirname(destination))
    with zipfile.ZipFile(paths.to_extended(zip_path)) as archive:
        with archive.open(member) as source, open(
            paths.to_extended(destination), "wb"
        ) as handle:
            handle.write(source.read())
    return destination


def find_member(zip_path: str, name: str) -> str | None:
    """Name of the first archive member whose base name matches ``name``."""
    with zipfile.ZipFile(paths.to_extended(zip_path)) as archive:
        for entry in archive.infolist():
            base = entry.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if base.lower() == name.lower():
                return entry.filename
    return None

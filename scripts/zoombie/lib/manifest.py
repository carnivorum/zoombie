"""Read and write ``<root>\\env.json``.

Written by the installer, read by every command. The manifest resolves the
toolchain's absolute paths, so nothing depends on PATH or on where a tool was
installed.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from . import paths


def load() -> dict:
    """Read env.json, or an empty dict when it is absent or unparseable.

    A manifest produced by an older or partial install can lack a whole section.
    Returning an empty dict (rather than raising) keeps every command runnable:
    the callers read through :func:`dig`, which yields a default for a missing
    link instead of aborting with a vague error.
    """
    path = paths.manifest_path()
    if not paths.is_file(path):
        return {}
    try:
        with open(paths.to_extended(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(manifest: dict) -> str:
    """Write env.json as UTF-8 WITHOUT a BOM, atomically.

    Atomic because a half-written manifest is worse than an old one: every
    command reads it, and a truncated file would poison the whole toolchain.
    """
    root = paths.env_root()
    paths.ensure_dir(root)
    path = paths.manifest_path()

    handle_fd, temp_path = tempfile.mkstemp(
        prefix="env-", suffix=".json", dir=paths.to_extended(root)
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_path, paths.to_extended(path))
    except OSError:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    return path


def dig(obj: Any, dotted: str, default: Any = None) -> Any:
    """Read a dotted path from nested dicts, returning ``default`` if absent.

    Replaces the PowerShell ``Get-ManifestValue``/``Get-Field`` pair. In Python a
    missing key is already an ordinary ``KeyError`` rather than a strict-mode
    crash, but a dotted walk still makes the defensive reads shorter.
    """
    current = obj
    for key in dotted.split("."):
        if isinstance(current, dict):
            if key not in current:
                return default
            current = current[key]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(key)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return default if current is None else current

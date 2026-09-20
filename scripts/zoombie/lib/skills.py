"""Skill ownership marker and deployment.

Deployment target is the GLOBAL skills root, ``%USERPROFILE%\\.roo\\skills``. It
is always the absolute path built from the profile, never a relative ``..\\..``
path, which would create a stray directory.

Every skill is namespaced ``zoombie-*``, so a name can never collide with a
foreign skill and deployment simply overwrites. The version marker only decides
whether to report ``up to date`` or ``updated``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import paths, process

MARKER_KEY = "cvrm-zoombie-version"


@dataclass
class Marker:
    owned: bool = False
    version: str | None = None


def skills_root() -> str:
    """The global skills root: ``<profile>\\.roo\\skills``."""
    profile = os.environ.get("USERPROFILE") or paths.env_root()
    return os.path.join(profile, ".roo", "skills")


def read_marker(path: str) -> Marker:
    """Read the ownership + version marker from a SKILL.md file.

    Only the front matter (the first dozen lines) is inspected: the marker lives
    there, and a later line mentioning the key in prose must not count.
    """
    result = Marker()
    if not paths.is_file(path):
        return result
    try:
        with open(paths.to_extended(path), "r", encoding="utf-8", errors="replace") as handle:
            head = [next(handle, "") for _ in range(12)]
    except OSError:
        return result

    pattern = re.compile(rf"^\s*{re.escape(MARKER_KEY)}\s*:\s*(\S+)")
    for line in head:
        match = pattern.match(line)
        if match:
            result.owned = True
            result.version = match.group(1)
            return result
    return result


def deploy(source_root: str, version: str) -> list[dict]:
    """Deploy every ``<source_root>\\<name>\\SKILL.md`` to the global skills root.

    Returns one record per skill: ``{"skill", "action", "path"}``.
    """
    root = skills_root()
    results: list[dict] = []

    if not paths.is_dir(source_root):
        process.log("no skills folder to deploy; skipping", "warn")
        return results

    for entry in paths.list_dir(source_root, dirs=True):
        source = os.path.join(entry.path, "SKILL.md")
        if not paths.is_file(source):
            continue
        destination = os.path.join(root, entry.name, "SKILL.md")

        if not paths.is_file(destination):
            action = "created"
        else:
            marker = read_marker(destination)
            if marker.version == version:
                action = "up to date"
            elif marker.version:
                action = f"updated ({marker.version} -> {version})"
            else:
                action = f"updated (no version -> {version})"

        if action != "up to date":
            paths.ensure_dir(os.path.dirname(destination))
            paths.copy_file(source, destination)

        results.append({"skill": entry.name, "action": action, "path": destination})

    process.log(f"skills deployed -> {root}")
    return results

"""Skill ownership marker, include expansion and deployment.

Deployment target is the GLOBAL skills root, ``%USERPROFILE%\\.roo\\skills``. It
is always the absolute path built from the profile, never a relative ``..\\..``
path, which would create a stray directory.

Every skill is namespaced ``zoombie-*``, so a name can never collide with a
foreign skill and deployment simply overwrites.

The repo sources under ``skills/`` share four boilerplate blocks (how to resolve
the launcher, the JSON contract, the repo fallback and the shell note) through
``skills/_shared/``. Deployment EXPANDS those includes, so the installed skill is
self-contained and an agent never has to resolve an include at runtime.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import paths, process
from .errors import ZoombieError

MARKER_KEY = "cvrm-zoombie-version"

# Folder under ``skills/`` holding the canonical include bodies. It carries no
# SKILL.md, so both deployment and the inventory tests skip it as a skill.
SHARED_FOLDER = "_shared"

_INCLUDE_OPEN_RE = re.compile(
    r"^[ \t]*<!--\s*zoombie:include\s+(?P<name>[A-Za-z0-9._-]+)\s*-->[ \t]*$"
)
_INCLUDE_CLOSE_RE = re.compile(r"^[ \t]*<!--\s*/zoombie:include\s*-->[ \t]*$")


@dataclass
class Marker:
    owned: bool = False
    version: str | None = None


def skills_root() -> str:
    """The global skills root: ``<profile>\\.roo\\skills``."""
    profile = os.environ.get("USERPROFILE") or paths.env_root()
    return os.path.join(profile, ".roo", "skills")


def shared_dir(source_root: str) -> str:
    """The include bodies folder beside the skill sources."""
    return os.path.join(source_root, SHARED_FOLDER)


def _read_text(path: str) -> str:
    with open(paths.to_extended(path), "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def read_include(name: str, shared_directory: str) -> str:
    """The body of one include, or a hard failure.

    An unknown name fails rather than expanding to nothing: a typo would
    otherwise ship a skill with its launcher-resolution step silently missing.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        raise ZoombieError(f"Invalid include name: {name!r}")
    path = os.path.join(shared_directory, f"{name}.md")
    if not paths.is_file(path):
        raise ZoombieError(
            f"Unknown skill include '{name}': no {path}. "
            f"Add the block there or fix the marker in the SKILL.md."
        )
    return _read_text(path)


def expand_includes(text: str, shared_directory: str) -> str:
    """Replace every ``<!-- zoombie:include NAME -->`` region with its body.

    The markers themselves are removed, so the returned text is what a deployed
    skill carries. Text with no markers is returned unchanged, which is what
    makes the function idempotent. An unclosed marker is a failure.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        open_match = _INCLUDE_OPEN_RE.match(lines[index])
        if not open_match:
            out.append(lines[index])
            index += 1
            continue

        name = open_match.group("name")
        body = read_include(name, shared_directory)
        index += 1
        while index < len(lines) and not _INCLUDE_CLOSE_RE.match(lines[index]):
            index += 1
        if index >= len(lines):
            raise ZoombieError(
                f"Unclosed skill include '{name}': the matching "
                f"<!-- /zoombie:include --> marker is missing."
            )
        index += 1
        out.append(body.rstrip("\n"))
    return "\n".join(out)


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

    The source is expanded (its includes resolved) before it is written, and the
    action is decided by comparing the EXPANDED text against what is installed,
    so a change to a shared block redeploys without a version bump and a second
    run on unchanged content writes nothing.

    Returns one record per skill: ``{"skill", "action", "path"}``.
    """
    root = skills_root()
    shared = shared_dir(source_root)
    results: list[dict] = []

    if not paths.is_dir(source_root):
        process.log("no skills folder to deploy; skipping", "warn")
        return results

    for entry in paths.list_dir(source_root, dirs=True):
        source = os.path.join(entry.path, "SKILL.md")
        if not paths.is_file(source):
            continue
        destination = os.path.join(root, entry.name, "SKILL.md")
        expanded = expand_includes(_read_text(source), shared)

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

        if action == "up to date" and _read_text(destination) != expanded:
            # The version marker matched but the body did not: a shared block or
            # a body edit changed without a bump. Say so rather than skipping.
            action = "updated (content)"

        if action != "up to date":
            paths.ensure_dir(os.path.dirname(destination))
            if paths.exists(destination):
                paths.remove(destination)
            with open(paths.to_extended(destination), "w", encoding="utf-8", newline="") as handle:
                handle.write(expanded)

        results.append({"skill": entry.name, "action": action, "path": destination})

    process.log(f"skills deployed -> {root}")
    return results

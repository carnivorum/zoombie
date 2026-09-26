"""Skill ownership marker, include expansion and deployment.

Deployment target is the GLOBAL skills root, ``%USERPROFILE%\\.roo\\skills``. It
is always the absolute path built from the profile, never a relative ``..\\..``
path, which would create a stray directory.

Every skill is namespaced ``zoombie-*``, so a name can never collide with a
foreign skill and deployment simply overwrites.

The repo sources under ``skills/`` share five boilerplate blocks (how to resolve
the launcher, the JSON contract, the repo fallback, the shell note and the
scratch-dir rule) through ``skills/_shared/``. Deployment EXPANDS those includes,
so the installed skill is self-contained and an agent never has to resolve an
include at runtime.
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


def prune(root: str, keep: set[str], *, dry_run: bool = False) -> list[dict]:
    """Remove deployed ``zoombie-*`` skills that no longer have a source.

    A rename (``zoombie-pdf-to-md`` -> ``zoombie-images-to-md``) leaves the old
    directory behind, and two skills then advertise the same job -- the agent
    picks one arbitrarily. Removal is deliberately narrow so a foreign skill is
    never touched:

    * only directories whose name starts with ``zoombie-`` are candidates;
    * only a directory whose ``SKILL.md`` carries OUR version marker is removed
      (an unowned folder that merely starts with ``zoombie-`` is left alone);
    * a name still present in ``keep`` is never removed.

    ``dry_run`` reports the same removals while deleting nothing, so ``-Check``
    tells the truth about what a real run would drop.
    """
    removed: list[dict] = []
    if not paths.is_dir(root):
        return removed
    for entry in paths.list_dir(root, dirs=True):
        if entry.name in keep or entry.name == SHARED_FOLDER:
            continue
        if not entry.name.startswith("zoombie-"):
            continue
        skill_file = os.path.join(entry.path, "SKILL.md")
        if not read_marker(skill_file).owned:
            continue  # not ours: a foreign folder that happens to be named so
        if not dry_run:
            paths.remove(entry.path, recursive=True)
        removed.append({"skill": entry.name, "action": "removed (stale)"})
    return removed


def preflight(source_root: str) -> list[str]:
    """Expand every skill source up front, so a broken include fails before ANY
    write. Returns the skill names in source order; raises on an unknown or
    unclosed include.

    This is the fix for "install might fail due to stale versions in skills": the
    old flow wrote the package, then discovered a skill/include error mid-deploy
    and left a half-updated tree. Now every source is expanded before the first
    byte is written.
    """
    shared = shared_dir(source_root)
    names: list[str] = []
    for entry in paths.list_dir(source_root, dirs=True):
        source = os.path.join(entry.path, "SKILL.md")
        if not paths.is_file(source):
            continue
        # Side effect only on failure: an unexpandable source aborts the install.
        expand_includes(_read_text(source), shared)
        names.append(entry.name)
    return names


def deploy(source_root: str, version: str, *, dry_run: bool = False) -> list[dict]:
    """Deploy every ``<source_root>\\<name>\\SKILL.md`` to the global skills root.

    The source is expanded (its includes resolved) before it is written, and the
    action is decided by comparing the EXPANDED text against what is installed,
    so a change to a shared block redeploys without a version bump and a second
    run on unchanged content writes nothing.

    ``dry_run`` computes the same records and writes nothing — the marker is no
    longer trusted for "up to date", only the bytes are, which is what makes
    ``-Check`` able to report a stale skill (the exact drift a matching marker
    used to hide).

    Returns one record per skill: ``{"skill", "action", "path"}`` with ``action``
    in ``added`` / ``updated`` / ``unchanged`` (plus ``removed (stale)`` records
    from :func:`prune`).
    """
    root = skills_root()
    shared = shared_dir(source_root)
    results: list[dict] = []

    if not paths.is_dir(source_root):
        process.log("no skills folder to deploy; skipping", "warn")
        return results

    sources = [
        entry for entry in paths.list_dir(source_root, dirs=True)
        if paths.is_file(os.path.join(entry.path, "SKILL.md"))
    ]
    # Remove stale skills BEFORE writing, so a rename cannot leave both the old
    # and the new skill installed at the end of the run.
    results.extend(prune(root, {entry.name for entry in sources}, dry_run=dry_run))

    for entry in sources:
        source = os.path.join(entry.path, "SKILL.md")
        destination = os.path.join(root, entry.name, "SKILL.md")
        expanded = expand_includes(_read_text(source), shared)

        installed = _read_text(destination) if paths.is_file(destination) else None
        if installed is None:
            action = "added"
        elif installed != expanded:
            action = "updated"
        else:
            action = "unchanged"

        if action != "unchanged" and not dry_run:
            paths.ensure_dir(os.path.dirname(destination))
            if paths.exists(destination):
                paths.remove(destination)
            with open(paths.to_extended(destination), "w", encoding="utf-8", newline="") as handle:
                handle.write(expanded)

        results.append({"skill": entry.name, "action": action, "path": destination})

    process.log(f"skills {'planned' if dry_run else 'deployed'} -> {root}")
    return results

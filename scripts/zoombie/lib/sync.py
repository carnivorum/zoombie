"""Content-based reconciliation: plan a file tree by hash, not by version.

The install/update path used to trust a ``cvrm-zoombie-version`` marker per skill
and a ``zoombieVersion`` per manifest. Both lied: a deployed package and six
deployed skills all read ``5.1.0`` while four package files and one skill were
stale, because a marker records an INTENT and says nothing about the bytes on
disk. The fix is to compare content directly: hash the desired (fetched) tree and
the installed tree, then add, update and remove to make them agree.

Two properties this module owns, and both are pure so they are cheap to test:

* :func:`hash_tree` maps a tree to ``relative path -> sha256``, excluding the
  artefacts a Python package carries that are not source (``__pycache__``,
  ``*.pyc``) and a caller-supplied extra set. Paths are POSIX-normalised, so a
  Windows install and a Linux checkout produce the same keys.
* :func:`plan` is a pure function of the two maps: it never touches the disk. The
  caller applies the result, which is what lets ``-Check`` report the EXACT plan
  it would execute while writing nothing.
"""

from __future__ import annotations

import hashlib
import os

from . import paths

# Directory/file artefacts that are build output, never source. A reconciled
# package must not carry a __pycache__ from the build machine, and must not
# delete on the target one either.
DEFAULT_EXCLUDES = ("__pycache__",)
DEFAULT_EXCLUDE_SUFFIXES = (".pyc", ".pyo")

_CHUNK = 1024 * 1024


def _is_excluded(relative: str, excludes: tuple[str, ...], suffixes: tuple[str, ...]) -> bool:
    """True when a relative POSIX path is build output rather than source."""
    parts = relative.split("/")
    if any(part in excludes for part in parts):
        return True
    return relative.lower().endswith(suffixes)


def hash_file(path: str) -> str:
    """sha256 hex digest of one file, read in chunks so a big file cannot spike RAM."""
    digest = hashlib.sha256()
    with open(paths.to_extended(path), "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_text(text: str) -> str:
    """sha256 of a string, encoded UTF-8, so a generated file (a launcher, a
    skill's expanded body) can be compared with the same currency as a file."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_tree(
    root: str,
    *,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    exclude_suffixes: tuple[str, ...] = DEFAULT_EXCLUDE_SUFFIXES,
) -> dict[str, str]:
    """Map every file under ``root`` to ``relative-posix -> sha256``.

    A missing ``root`` yields an empty map (the caller then adds everything),
    which is the same shape a genuinely empty tree has: "nothing installed yet".
    """
    result: dict[str, str] = {}
    if not paths.is_dir(root):
        return result

    # Walk the EXTENDED root and measure the relative path against that same
    # rooted string: os.walk yields prefixed children, so slicing a prefix-free
    # base length off a prefixed path would land mid-name.
    walk_root = paths.to_extended(root).rstrip("\\/")
    base_len = len(walk_root) + 1
    for dirpath, dirnames, filenames in os.walk(walk_root):
        # Prune excluded directories in place so os.walk never descends.
        dirnames[:] = [name for name in dirnames if name not in excludes]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            relative = full[base_len:].replace("\\", "/")
            if _is_excluded(relative, excludes, exclude_suffixes):
                continue
            result[relative] = hash_file(paths.from_extended(full))
    return result


def plan(desired: dict[str, str], installed: dict[str, str]) -> dict:
    """Compare two ``{relative: sha256}`` maps into an actionable plan.

    Returns ``{"added", "updated", "unchanged", "removed"}`` — each a sorted list
    of relative paths. Pure: the caller owns every filesystem effect.
    """
    added = [key for key in desired if key not in installed]
    updated = [key for key in desired if key in installed and desired[key] != installed[key]]
    unchanged = [key for key in desired if key in installed and desired[key] == installed[key]]
    removed = [key for key in installed if key not in desired]
    return {
        "added": sorted(added),
        "updated": sorted(updated),
        "unchanged": sorted(unchanged),
        "removed": sorted(removed),
    }


def plan_text(desired_text: str, installed_text: str | None) -> str:
    """Classify one file from desired and installed TEXT.

    ``installed_text`` is ``None`` when the file is absent. Content comparison,
    not a version marker, is what makes ``added``/``updated``/``unchanged``
    truthful for a generated file such as the launcher.
    """
    if installed_text is None:
        return "added"
    if desired_text == installed_text:
        return "unchanged"
    return "updated"


def is_noop(result: dict) -> bool:
    """True when a plan changes nothing, i.e. the tree is already in sync."""
    return not result["added"] and not result["updated"] and not result["removed"]


def applied(actions: list[dict]) -> bool:
    """True when every recorded action is a no-op ("unchanged"/"up to date")."""
    return all(record.get("action", "").startswith(("unchanged", "up to date")) for record in actions)

"""Apply the content-based plan to every installed tree the repo owns.

This is the effectful half of :mod:`zoombie.lib.sync`. The plan itself is pure;
this module turns it into file operations, and it is the ONE place the install
decides what to write and what to delete.

Removal policy (the part that must never damage a user's files):

* the reconciler only removes a file that is **inside a toolchain-owned
  directory** (``<root>\\bin\\zoombie``, the global skills root) or a recorded
  legacy orphan — never an arbitrary path;
* within the package it removes any file the fetched tree no longer carries
  (a module was deleted or renamed) plus the named pre-Port orphans;
* a path that is neither desired nor recorded-ours is **reported, never deleted**.
"""

from __future__ import annotations

import os

from .. import SKILL_VERSION
from ..lib import env as env_mod, paths, process, skills, sync

# Files the pre-Port PowerShell implementation left in the deployed bin dir.
# They exist in no source tree, so a copy-only install can never remove them.
LEGACY_ORPHANS = ("zoombie.ps1", "lib/ZoombieEnv.psm1")

LAUNCHER_NAME = "zoombie.cmd"
INSTALLER_NAME = "zoombie-install.cmd"


def _apply_tree(
    modes,
    *,
    label: str,
    desired_root: str,
    installed_root: str,
    excludes: tuple[str, ...] = sync.DEFAULT_EXCLUDES,
) -> dict:
    """Reconcile one desired tree into ``installed_root``.

    Copying is content-driven: only an added or updated file is written, so a
    re-run on unchanged sources writes nothing. Removal is limited to files the
    caller lists in ``LEGACY_ORPHANS`` or that the fetched tree dropped.
    """
    desired = sync.hash_tree(desired_root, excludes=excludes)
    installed = sync.hash_tree(installed_root, excludes=excludes)
    result = sync.plan(desired, installed)

    if modes.may_write:
        for relative in result["added"] + result["updated"]:
            paths.copy_file(
                os.path.join(desired_root, relative.replace("/", os.sep)),
                os.path.join(installed_root, relative.replace("/", os.sep)),
            )
        for relative in result["removed"]:
            paths.remove_quietly(os.path.join(installed_root, relative.replace("/", os.sep)))

    process.log(
        f"{label}: {len(result['added'])} added, {len(result['updated'])} updated, "
        f"{len(result['removed'])} removed, {len(result['unchanged'])} unchanged"
    )
    return {"label": label, "root": installed_root, **result}


def _write_if_changed(modes, path: str, text: str) -> str:
    """Write ``text`` only when it differs; return added/updated/unchanged.

    Both sides are read and written with ``newline=""`` so a ``\\r\\n`` in the
    desired text (the launcher, the getter) is compared verbatim instead of being
    normalised to ``\\n`` on read — that translation is what made an unchanged file
    report ``updated`` on every run.
    """
    installed = None
    if paths.is_file(path):
        with open(
            paths.to_extended(path), "r", encoding="utf-8", errors="replace", newline=""
        ) as handle:
            installed = handle.read()
    action = sync.plan_text(text, installed)
    if action != "unchanged" and modes.may_write:
        paths.ensure_dir(os.path.dirname(path))
        with open(paths.to_extended(path), "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return action


def _remove_orphans(modes, base: str) -> list[str]:
    """Remove the named pre-Port files, and report ONLY the ones present.

    Presence is checked in both modes, so ``-Check`` reports an orphan that exists
    (a real removal) and stays silent once it is gone — an unconditional list would
    keep claiming a removal that already happened.
    """
    removed: list[str] = []
    for relative in LEGACY_ORPHANS:
        target = os.path.join(base, relative.replace("/", os.sep))
        if paths.is_file(target):
            if modes.may_write:
                paths.remove_quietly(target)
            removed.append(relative)
    return removed


def reconcile_launcher(modes, launcher_path: str, launcher_text: str) -> dict:
    """Reconcile the deployed ``zoombie.cmd`` against freshly generated text."""
    action = _write_if_changed(modes, launcher_path, launcher_text)
    return {"path": launcher_path, "action": action}


def reconcile_installer(modes, source: str) -> dict:
    """Copy the getter into the root so a re-run needs no network raw fetch."""
    destination = os.path.join(paths.env_root(), INSTALLER_NAME)
    if not paths.is_file(source):
        return {"path": destination, "action": "absent"}
    with open(
        paths.to_extended(source), "r", encoding="utf-8", errors="replace", newline=""
    ) as handle:
        text = handle.read()
    action = _write_if_changed(modes, destination, text)
    return {"path": destination, "action": action}


def reconcile(modes, launcher_text: str) -> dict:
    """Reconcile the package, pdf helper, requirements, launcher and getter.

    Skills are reconciled by :func:`zoombie.lib.skills.deploy` (which already
    expands includes); modes and the MCP entry are merges owned by their own
    modules. This function covers the trees those do not.
    """
    source = env_mod.cli_dir()  # <repo>\scripts
    bin_dir = paths.env_path("bin", "zoombie")

    trees = [
        _apply_tree(
            modes, label="package",
            desired_root=os.path.join(source, "zoombie"),
            installed_root=os.path.join(bin_dir, "zoombie"),
        ),
        _apply_tree(
            modes, label="pdf",
            desired_root=os.path.join(source, "pdf"),
            installed_root=os.path.join(bin_dir, "pdf"),
        ),
    ]

    # Top-level requirement files travel beside the package so the documented
    # remedy works from the deployed directory, not only from a checkout.
    requirements: list[dict] = []
    for name in ("requirements-pdf.txt", "requirements-selftest.txt"):
        origin = os.path.join(source, name)
        if not paths.is_file(origin):
            continue
        destination = os.path.join(bin_dir, name)
        with open(
            paths.to_extended(origin), "r", encoding="utf-8", errors="replace", newline=""
        ) as handle:
            action = _write_if_changed(modes, destination, handle.read())
        requirements.append({"path": destination, "action": action})

    orphans = _remove_orphans(modes, bin_dir)
    launcher = reconcile_launcher(modes, os.path.join(bin_dir, LAUNCHER_NAME), launcher_text)
    installer = reconcile_installer(modes, os.path.join(source, INSTALLER_NAME))

    return {
        "version": SKILL_VERSION,
        "trees": trees,
        "requirements": requirements,
        "launcher": launcher,
        "installer": installer,
        "orphansRemoved": orphans,
    }


__all__ = ["reconcile", "reconcile_launcher", "reconcile_installer", "LEGACY_ORPHANS"]

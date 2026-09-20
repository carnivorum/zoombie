"""Resolve tools and interpreters to absolute paths; keep PATH honest.

The rule this module enforces: never rely on PATH, and never assume an install
location. Every tool is resolved to an absolute path and called by it.
"""

from __future__ import annotations

import glob
import os
import sys
from typing import Sequence

from . import manifest, paths, process

# pip can drop console launchers into %APPDATA%\Python\<ver>\Scripts. Nothing in
# this toolchain calls them (helpers import their libraries directly), so they
# are pure clutter -- but they are also how a foreign tool could break, so the
# installer snapshots the folder and removes only what its own install created.


def is_store_stub(path: str | None) -> bool:
    """True when a python path is the Microsoft Store alias stub, not a real one."""
    return bool(path) and "\\WindowsApps\\" in path


def tool_version(exe: str | None, args: list[str] | None = None) -> str | None:
    """Run a tool with version arguments and return its first output line."""
    if not exe or not paths.exists(exe):
        return None
    argv = [exe] + (args if args is not None else ["--version"])
    _code, text = process.run_text(argv, timeout=60)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def resolve(name: str, candidates: Sequence[str | None] | None = None) -> str | None:
    """Resolve a tool to an absolute path, or None.

    Resolution order (first hit wins):
      1. explicit candidate paths (e.g. from env.json)
      2. the toolchain bin directories -- no PATH lookup at all
      3. PATH, refreshed from the registry first
    """
    for candidate in candidates or []:
        if candidate and paths.is_file(candidate):
            return paths.absolute(candidate)

    bin_dirs = [paths.env_path("bin"), paths.env_path("bin", "whisper")]
    for directory in bin_dirs:
        if not paths.is_dir(directory):
            continue
        for file_name in (f"{name}.exe", name):
            candidate = os.path.join(directory, file_name)
            if paths.is_file(candidate):
                return paths.absolute(candidate)

    process.refresh_path_from_registry()
    import shutil

    found = shutil.which(name)
    if found:
        return found

    # A few tools ship in fixed system locations that are not always on PATH in a
    # non-interactive process (a service, a scheduled task, or a stripped shell).
    for directory in _system_dirs():
        for file_name in (f"{name}.exe", name):
            candidate = os.path.join(directory, file_name)
            if paths.is_file(candidate):
                return paths.absolute(candidate)
    return None


def _system_dirs() -> list[str]:
    """Fixed Windows locations worth probing when PATH does not resolve a tool."""
    if sys.platform != "win32":
        return []
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return [
        os.path.join(system_root, "System32"),
        system_root,
        os.path.join(system_root, "System32", "wbem"),
    ]


def find_python() -> str | None:
    """Absolute path of the Python interpreter this toolchain uses, or None.

    yt-dlp and the PDF helper both run on this interpreter, so the installer
    installs both dependency sets into it.

    Order (first hit wins), so a stale PATH can never cause a false negative:
      1. ``python.path`` recorded in env.json (exactly what setup installed into)
      2. the toolchain bin dirs
      3. PATH, after a registry refresh, skipping the Store alias stub
    """
    from_manifest = manifest.dig(manifest.load(), "python.path")
    if from_manifest and paths.is_file(from_manifest):
        return paths.absolute(from_manifest)

    resolved = resolve("python")
    if resolved and not is_store_stub(resolved):
        return resolved
    return None


def find_python_candidates() -> list[str]:
    """Well-known per-user Python locations, newest first.

    Used by the installer when PATH yields only the Store stub. Membership in
    this list is a hint, not a guarantee: each path is existence-checked.
    """
    local = os.environ.get("LOCALAPPDATA", "")
    candidates: list[str] = []
    for version in ("313", "312", "311", "310"):
        candidates.append(os.path.join(local, "Programs", "Python", f"Python{version}", "python.exe"))
    return candidates


def pip_script_dirs() -> list[str]:
    """Directories where pip may drop console entry-point shims for a --user install."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return []
    python_root = os.path.join(appdata, "Python")
    dirs: list[str] = []
    for entry in paths.list_dir(python_root, dirs=True):
        scripts = os.path.join(entry.path, "Scripts")
        if paths.is_dir(scripts):
            dirs.append(scripts)
    return dirs


def pip_shim_snapshot() -> dict[str, set[str]]:
    """File names present in every pip script dir, before an install.

    Lets the caller tell exactly which shims its own install created, so anything
    already there (a foreign tool such as yt-dlp.exe) is preserved.
    """
    snapshot: dict[str, set[str]] = {}
    for directory in pip_script_dirs():
        snapshot[directory] = {entry.name for entry in paths.list_dir(directory, files=True)}
    return snapshot


def remove_new_pip_shims(before: dict[str, set[str]]) -> int:
    """Remove only the pip shims created since ``before``. Best-effort."""
    removed = 0
    for directory in pip_script_dirs():
        known = before.get(directory, set())
        for entry in paths.list_dir(directory, files=True):
            if entry.name in known:
                continue
            try:
                paths.remove(entry.path)
                removed += 1
            except OSError:
                process.log(f"could not remove pip shim (in use?): {entry.path}", "warn")
    return removed


def python_module_ok(python: str | None, module: str) -> bool:
    """True when ``python -m <module>`` imports successfully."""
    if not python or not paths.is_file(python):
        return False
    code, _ = process.run_text([python, "-c", f"import {module}"], timeout=120)
    return code == 0


def pip_install(
    python: str,
    args: list[str],
    *,
    quiet: bool = True,
) -> int:
    """Run ``pip install`` with the flags this toolchain uses everywhere.

    ``--user`` so installation never needs elevation and never writes into a
    protected location.
    """
    argv = [
        python,
        "-m",
        "pip",
        "install",
        "--user",
        "--no-warn-script-location",
        "--disable-pip-version-check",
    ]
    if quiet:
        argv.append("--quiet")
    argv.extend(args)
    code, _ = process.run_text(argv, timeout=1800)
    return code


def glob_in(directory: str, pattern: str, *, recursive: bool = False) -> list[str]:
    """Glob inside a directory, long-path safe, returning prefix-free paths."""
    if not paths.is_dir(directory):
        return []
    target = paths.to_extended(directory)
    search = os.path.join(target, "**", pattern) if recursive else os.path.join(target, pattern)
    found = glob.glob(search, recursive=recursive)
    return [paths.from_extended(item) for item in found]


def find_file_named(root: str, name: str, *, recursive: bool = True) -> str | None:
    """First file called ``name`` under ``root``, or None."""
    target = paths.to_extended(root)
    if not os.path.isdir(target):
        return None
    if not recursive:
        candidate = os.path.join(target, name)
        return paths.from_extended(candidate) if os.path.isfile(candidate) else None
    for current, _dirs, files in os.walk(target):
        if name in files:
            return paths.from_extended(os.path.join(current, name))
    return None

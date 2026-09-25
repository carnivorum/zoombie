"""7z / NSIS extraction: a provisioned extractor plus a scaffolding-aware copier.

Why this module exists
----------------------
Tesseract is the component that *needed* an NSIS installer unpacked, but nothing
about unpacking is Tesseract-specific: any upstream that ships only an installer
(``*-setup-*.exe``) is provisioned the same way. This module holds that machinery
so it is one capability with one set of rules rather than a detail buried inside
the OCR engine.

It is deliberately a *capability an agent never performs by hand*. Manual
unpacking pollutes the workspace with a tree nobody asked for and then burns
tokens on listing files the agent should never have read. Two properties make
that safe:

* **The extractor is pinned and hash-verified.** 7-Zip's NSIS handler lives in
  the FULL build (``7z.exe`` + ``7z.dll``), which ships inside the SFX
  ``7z2603-x64.exe`` -- and that SFX REQUESTS ELEVATION when executed, so it is
  only ever EXTRACTED, using the pinned standalone ``7zr.exe``. Nothing is ever
  run that would ask for elevation, and no file is accepted before its sha256
  matches the published digest.
* **The payload copier filters by DIRECTORY, not only by extension.** An NSIS
  installer carries its own scaffolding under ``$PLUGINSDIR\\`` -- DLLs such as
  ``nsDialogs.dll`` or ``System.dll`` that belong to the *installer*, not to the
  product. A blanket ``*.dll`` rule copies them into the runtime tree, where they
  are dead weight and a permanent residue (measured: 6 files, 96,768 B). They are
  excluded here by directory, so every consumer inherits the exclusion.

``lib/archive.py`` stays what it is -- zip-only, stdlib-only -- because a zip is
readable without a native tool and an NSIS payload is not.

The ASCII-work-dir invariant applies: extraction happens in a scratch dir under
the (ASCII) toolchain root and the results are copied to the caller's confirmed
destination, so no native tool is ever handed a non-ASCII path.
"""

from __future__ import annotations

import fnmatch
import os

from . import download, paths, process

__all__ = [
    "EXTRACTOR",
    "EXTRACTOR_BOOTSTRAP",
    "SCAFFOLDING_DIRS",
    "extractor_path",
    "install_extractor",
    "extract",
    "list_archive",
    "sha256_file",
    "verify_archive",
    "runs",
    "require_runs",
    "is_scaffolding",
    "normalise_relative",
    "strip_relative",
    "matches_include",
    "select_payload",
    "copy_payload",
]

# ---------------------------------------------------------------------------
# Pinned rows -- each URL was fetched and its sha256 computed from the download.
# ---------------------------------------------------------------------------

# The NSIS-capable console extractor. 7-Zip's NSIS handler lives in the FULL build
# (``7z.exe`` + ``7z.dll``), which ships inside the installer SFX ``7z2603-x64.exe``
# -- and that SFX REQUESTS ELEVATION when executed, so it must never be run. It is
# only ever EXTRACTED, using the pinned standalone ``7zr.exe`` below, and the two
# resulting console files (``7z.exe`` + ``7z.dll``) run without elevation. The SFX
# is sha256-verified before that extraction.
EXTRACTOR = {
    "name": "7z.exe",
    "version": "26.03",
    "url": "https://github.com/ip7z/7zip/releases/download/26.03/7z2603-x64.exe",
    # GitHub's published asset digest for 7z2603-x64.exe (the SFX).
    "sha256": "0859c524b8a63551848f0c246abddcb1d0b7b656b0fbfe879f8d85e61a9e6edd",
    # Only these two files are needed to read the NSIS payload; the rest of the
    # SFX (7zG.exe, 7zFM.exe, the shell extension, the uninstaller) is dropped.
    "files": ["7z.exe", "7z.dll"],
}

# The bootstrap extractor: a SINGLE self-contained executable that reads .7z but
# has no NSIS support and needs no elevation. It is what unpacks the SFX above.
EXTRACTOR_BOOTSTRAP = {
    "name": "7zr.exe",
    "version": "26.03",
    "url": "https://github.com/ip7z/7zip/releases/download/26.03/7zr.exe",
    # GitHub's published asset digest for 7zr.exe.
    "sha256": "ad4c82fadcbdf93c03b4fc440f300509c7d60c5c2f4d183e35d9d70d6957037d",
}

# Directories that belong to the INSTALLER, never to the product. An NSIS payload
# places its own runtime here; copying it into a product tree is the residue this
# module removes. Matched as a whole path COMPONENT (case-insensitive), so both
# ``$PLUGINSDIR\\x.dll`` and ``a\\$PLUGINSDIR\\x.dll`` are excluded.
SCAFFOLDING_DIRS = ("$PLUGINSDIR",)

# The same names, lowercased once, for the case-insensitive comparison. Kept
# separate so the public constant stays readable while the match cannot drift.
_SCAFFOLDING_LOWER = frozenset(name.lower() for name in SCAFFOLDING_DIRS)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def extractor_path() -> str:
    """``<root>\\bin\\7z.exe`` -- the NSIS-capable console extractor."""
    return os.path.join(paths.env_path("bin"), EXTRACTOR["name"])


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    """Hex sha256 of a file, read in chunks so a large archive is not held in RAM."""
    import hashlib

    digest = hashlib.sha256()
    with open(paths.to_extended(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_archive(path: str, expected: str, *, what: str = "archive") -> None:
    """Raise when a downloaded file does not match its published sha256."""
    actual = sha256_file(path)
    if actual != expected.upper():
        raise RuntimeError(
            f"{what} hash mismatch: expected {expected}, got {actual} "
            f"for {os.path.basename(path)}"
        )


# ---------------------------------------------------------------------------
# Provisioning the extractor (writes; the caller gates on Modes.may_write)
# ---------------------------------------------------------------------------

def install_extractor(dest_dir: str | None = None) -> str:
    """Provision the NSIS-capable console 7-Zip into ``<root>\\bin``.

    Two downloads, both sha256-verified:

      1. ``7zr.exe`` -- a single self-contained executable (no elevation, no NSIS)
         used ONLY to unpack step 2.
      2. ``7z2603-x64.exe`` -- the full 7-Zip SFX, which holds ``7z.exe`` +
         ``7z.dll``. It is EXTRACTED, never executed: running it directly requests
         elevation, which this toolchain never does.

    Only ``7z.exe`` and ``7z.dll`` are kept, which is all the NSIS reader needs.
    """
    dest_dir = dest_dir or paths.env_path("bin")
    paths.ensure_dir(dest_dir)
    dest_exe = os.path.join(dest_dir, EXTRACTOR["name"])

    tmp = paths.new_temp_dir("zoombie-7zip")
    try:
        bootstrap = os.path.join(tmp, EXTRACTOR_BOOTSTRAP["name"])
        download.download(EXTRACTOR_BOOTSTRAP["url"], bootstrap, force=True)
        verify_archive(bootstrap, EXTRACTOR_BOOTSTRAP["sha256"], what="7zr bootstrap")

        sfx = os.path.join(tmp, "7zip-sfx.exe")
        download.download(EXTRACTOR["url"], sfx, force=True)
        verify_archive(sfx, EXTRACTOR["sha256"], what="7-Zip SFX")

        unpacked = os.path.join(tmp, "unpacked")
        paths.ensure_dir(unpacked)
        code, text = process.run_text(
            [bootstrap, "x", "-y", f"-o{paths.to_extended(unpacked)}", paths.to_extended(sfx)],
            timeout=300,
        )
        if code != 0:
            raise RuntimeError(
                f"7zr could not unpack the 7-Zip SFX (exit {code}): {text.strip()[:200]}"
            )
        for name in EXTRACTOR["files"]:
            source = os.path.join(unpacked, name)
            if not paths.is_file(source):
                raise RuntimeError(f"{name} was not found in the 7-Zip SFX")
            paths.copy_file(source, os.path.join(dest_dir, name))
        return dest_exe
    finally:
        paths.remove_quietly(tmp, recursive=True)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract(source: str, extractor: str, dest_dir: str, *, timeout: float = 600) -> str:
    """Unpack ``source`` (an NSIS ``.exe`` or a ``.7z``) into ``dest_dir``.

    7-Zip reads an NSIS installer payload as an archive, so the installer is data,
    never an executed program: no ``/S`` switch, no registry writes, no elevation.
    """
    paths.ensure_dir(dest_dir)
    code, text = process.run_text(
        [extractor, "x", "-y", f"-o{paths.to_extended(dest_dir)}", paths.to_extended(source)],
        timeout=timeout,
    )
    if code != 0:
        raise RuntimeError(
            f"7-Zip could not extract {os.path.basename(source)} (exit {code}): "
            f"{text.strip()[:300]}"
        )
    return dest_dir


def list_archive(source: str, extractor: str, *, timeout: float = 300) -> list[dict]:
    """List an archive's entries WITHOUT extracting it (no writes at all).

    ``7z l -slt`` prints one ``key = value`` block per entry, separated from the
    archive header by a dashed line. Only file entries are returned; the caller
    reports counts, so directory rows would only inflate them.
    """
    code, text = process.run_text(
        [extractor, "l", "-slt", paths.to_extended(source)], timeout=timeout
    )
    if code != 0:
        return []

    entries: list[dict] = []
    current: dict | None = None
    in_entries = False
    for line in text.splitlines():
        if line.startswith("----------"):
            in_entries = True
            current = None
            continue
        if not in_entries:
            continue
        if line.startswith("Path = "):
            if current:
                entries.append(current)
            current = {"path": line[len("Path = "):].strip(), "dir": False}
        elif current is not None and line.startswith("Attributes = "):
            attributes = line.split("=", 1)[1].strip()
            current["dir"] = "D" in attributes
    if current:
        entries.append(current)
    return [entry for entry in entries if entry["path"] and not entry["dir"]]


# ---------------------------------------------------------------------------
# Post-extraction probe (gates on the EXIT CODE)
# ---------------------------------------------------------------------------

def runs(exe: str, args: tuple[str, ...] = ("--version",), *, timeout: float = 60) -> tuple[int, str]:
    """Run ``exe`` and return ``(exit_code, text)``. Never raises.

    An unlaunchable executable yields ``(-1, "")``, which is what a missing or
    renamed DLL produces: the Windows loader refuses to start the process at all.
    """
    return process.run_text([exe, *args], timeout=timeout)


def require_runs(
    exe: str,
    what: str,
    args: tuple[str, ...] = ("--version",),
    *,
    timeout: float = 60,
) -> str:
    """Prove a provisioned executable actually STARTS, by its exit code.

    A presence check is not enough. ``tesseract --version`` may print its banner
    and still exit non-zero -- or fail to launch entirely -- when a DLL it loads is
    missing, so a version *string* is not proof that the engine works. Only a zero
    exit code is. Measured asymmetry this closes: a missing ``tesseract.exe`` was
    already loud, while a missing (or renamed) engine DLL was dropped silently and
    surfaced much later as an ungated runtime failure.

    Raises :class:`RuntimeError` naming the executable and the exit code, so a
    broken payload fails at PROVISION time with an actionable message.
    """
    code, text = runs(exe, args=args, timeout=timeout)
    if code != 0:
        detail = next(
            (line.strip() for line in text.splitlines() if line.strip()),
            "no output",
        )
        raise RuntimeError(
            f"{what} failed its '{args[0]}' probe (exit {code}): {detail[:200]}. "
            "An executable whose DLLs are missing or renamed fails here; check the "
            "extracted payload for a dropped or renamed library."
        )
    return text


# ---------------------------------------------------------------------------
# Payload selection + copy (the generic choke point)
# ---------------------------------------------------------------------------

def normalise_relative(relative: str) -> str:
    """Payload-internal path with forward slashes, for uniform matching."""
    return relative.replace("\\", "/")


def is_scaffolding(relative: str) -> bool:
    """True when any path COMPONENT is an installer-scaffolding directory.

    Component-wise, not a prefix test, so ``a/$PLUGINSDIR/x.dll`` is excluded as
    well as ``$PLUGINSDIR/x.dll``. Case-insensitive, because NSIS is not
    case-stable about it.
    """
    parts = normalise_relative(relative).lower().split("/")
    return any(part in _SCAFFOLDING_LOWER for part in parts)


def strip_relative(relative: str, levels: int) -> str | None:
    """Drop ``levels`` leading path components, or ``None`` when too shallow.

    ``-Strip 1`` turns the common ``<product>-<version>\\bin\\x.dll`` payload into
    ``bin/x.dll``. An entry with fewer components than requested is skipped rather
    than silently flattened to nothing.
    """
    if levels <= 0:
        return relative
    parts = normalise_relative(relative).split("/")
    if len(parts) <= levels:
        return None
    return "/".join(parts[levels:])


def matches_include(relative: str, include: str | list[str] | tuple[str, ...] | None) -> bool:
    """True when an entry matches an ``-Include`` glob (full path OR base name).

    Matching BOTH forms is what makes ``-Include *.dll`` and ``-Include tessdata/*``
    behave as a caller expects. Globs are matched case-insensitively, because the
    Windows filesystem is.
    """
    if not include:
        return True
    patterns = [include] if isinstance(include, str) else list(include)
    if not patterns:
        return True
    lowered = normalise_relative(relative).lower()
    base = lowered.rsplit("/", 1)[-1]
    for pattern in patterns:
        candidate = normalise_relative(pattern).lower()
        if fnmatch.fnmatch(lowered, candidate) or fnmatch.fnmatch(base, candidate):
            return True
    return False


def _safe_relative(relative: str) -> bool:
    """False for an entry that would escape the destination (path traversal)."""
    parts = normalise_relative(relative).split("/")
    return ".." not in parts


def select_payload(
    root: str,
    *,
    strip: int = 0,
    include: str | list[str] | tuple[str, ...] | None = None,
    keep_scaffolding: bool = False,
) -> list[dict]:
    """Every FILE under ``root`` that belongs in the destination, in stable order.

    Applies, in order: the installer-scaffolding DIRECTORY exclusion, the optional
    ``strip``, and the optional ``include`` globs. Returns ``{"source", "relative"}``
    rows so a caller can report exactly what it took without re-walking the tree.
    """
    root_extended = paths.to_extended(root)
    selected: list[dict] = []
    for current, _dirs, files in os.walk(root_extended):
        for name in files:
            full = os.path.join(current, name)
            relative = normalise_relative(os.path.relpath(full, root_extended))
            if not _safe_relative(relative):
                continue
            if not keep_scaffolding and is_scaffolding(relative):
                continue
            if strip:
                stripped = strip_relative(relative, strip)
                if stripped is None:
                    continue
                relative = stripped
            if not matches_include(relative, include):
                continue
            selected.append({"source": paths.from_extended(full), "relative": relative})
    return sorted(selected, key=lambda item: item["relative"].lower())


def copy_payload(
    payload_dir: str,
    dest_dir: str,
    *,
    strip: int = 0,
    include: str | list[str] | tuple[str, ...] | None = None,
    predicate=None,
    keep_scaffolding: bool = False,
) -> list[str]:
    """Copy a selected payload into ``dest_dir``, returning the relative paths taken.

    This is the single choke point for "what leaves an extracted payload", so the
    scaffolding exclusion cannot be forgotten by one consumer and honoured by
    another. ``predicate`` is an extra, product-specific filter (Tesseract's
    runtime-versus-SDK rule); it runs AFTER the generic exclusions.
    """
    copied: list[str] = []
    for item in select_payload(
        payload_dir, strip=strip, include=include, keep_scaffolding=keep_scaffolding
    ):
        if predicate is not None and not predicate(item["relative"]):
            continue
        paths.copy_file(item["source"], os.path.join(dest_dir, item["relative"]))
        copied.append(item["relative"])
    return copied

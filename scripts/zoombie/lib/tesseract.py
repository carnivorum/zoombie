"""The Tesseract OCR engine as a pinned, toolchain-owned component.

Why this module exists
----------------------
Tesseract was previously *detect-only*: the installer looked for
``%ProgramFiles%\\Tesseract-OCR\\tesseract.exe`` and only reported what it found,
while :func:`zoombie.lib.ocr.available` resolved the engine through PATH. The two
probes therefore disagreed by construction, and on a machine with neither a system
install nor a PATH entry ``available()`` could only ever return a failure. On a
fresh Windows machine that means OCR never works, which is the exact problem this
module closes.

The upstream facts this row set encodes (all measured, not assumed)
-------------------------------------------------------------------
* The official project publishes **no portable archive**. The only Windows asset
  across the recent ``tesseract-ocr/tesseract`` releases is an NSIS installer
  (``...-setup-....exe``); SourceForge's only portable zips are the legacy 3.x
  line, with no published hash. There is therefore no ``.zip``/``.7z`` to pin.
* That installer nevertheless **unpacks without being run**. 7-Zip reads the NSIS
  payload directly, and the extracted ``tesseract.exe`` runs standalone (observed:
  ``tesseract v5.5.3.20260724``) and finds a ``tessdata`` folder beside itself.
  So a *portable* engine is obtainable from the installer form, with no elevation
  and nothing written into a system location -- the installer is only ever
  *extracted*, never executed.
* GitHub now publishes a per-asset ``digest`` (sha256) for releases, so both the
  installer and the 7-Zip extractor are hash-verifiable before use.
* The installer carries no ``*.traineddata`` (only ``tessdata\\configs`` and
  ``pdf.ttf``), so language data is fetched separately from the official
  ``tesseract-ocr/tessdata`` repository, pinned to a commit (raw Git URLs are
  content-stable for a full commit sha).

The verification discipline is the same one :mod:`zoombie.lib.cublas` uses: every
download is checked against a published sha256 **before** anything is written into
the toolchain root, and everything lives under the ASCII root.
"""

from __future__ import annotations

import os
import re

from . import archive, download, paths, process, unpack

__all__ = [
    "EXTRACTOR_BOOTSTRAP",
    "EXTRACTOR",
    "ENGINE",
    "LANGUAGES",
    "DEFAULT_LANG",
    "DEFAULT_LANGUAGES",
    "engine_dir",
    "engine_path",
    "tessdata_dir",
    "traineddata_path",
    "extractor_path",
    "language_list",
    "engine_version",
    "resolve_engine",
    "install_extractor",
    "extract_installer",
    "install_engine",
    "install_languages",
    "sha256_file",
    "verify_archive",
]

# ---------------------------------------------------------------------------
# Pinned rows -- each URL was fetched and its sha256 computed from the download.
# ---------------------------------------------------------------------------

# The NSIS/7-Zip machinery is NOT Tesseract-specific, so it lives in
# :mod:`zoombie.lib.unpack` and is re-exported here for continuity: the names
# ``EXTRACTOR``, ``EXTRACTOR_BOOTSTRAP``, ``install_extractor()``,
# ``extractor_path()``, ``sha256_file()`` and ``verify_archive()`` keep working
# exactly as before, but the rows and the code behind them are now shared with
# every other installer-only component.
EXTRACTOR = unpack.EXTRACTOR
EXTRACTOR_BOOTSTRAP = unpack.EXTRACTOR_BOOTSTRAP

# The OCR engine. ``kind: "nsis-installer"`` records WHY no archive URL appears:
# there is no portable archive, so the installer's payload is extracted instead of
# executed.
ENGINE = {
    "kind": "nsis-installer",
    "product": "Tesseract OCR",
    "version": "5.5.3",
    "url": (
        "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/"
        "tesseract-ocr-w64-setup-5.5.3.20260724.exe"
    ),
    # GitHub's published asset digest for the installer. Verified by extracting the
    # payload AFTER this hash matches; the installer is never run.
    "sha256": "bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4",
    "size": 26573224,
    # Language data lives beside the exe; the installer does NOT ship any.
    "shipsTraineddata": False,
}

# Official traineddata, pinned to a full commit of the ``tesseract-ocr/tessdata``
# repository (the "fast" integer models are what Debian/Ubuntu package). The raw
# URL for a commit sha is immutable, so these hashes cannot go stale underneath us.
TESSDATA_COMMIT = "ced78752cc61322fb554c280d13360b35b8684e4"
TESSDATA_URL_BASE = (
    f"https://raw.githubusercontent.com/tesseract-ocr/tessdata/{TESSDATA_COMMIT}"
)

# The languages the toolchain owns. ``rus`` is not a nicety: the user's own deck is
# Russian, and ``-Lang`` defaults to ``eng+rus`` when no transcript is available to
# derive a script from.
LANGUAGES: dict[str, dict] = {
    "eng": {
        "file": "eng.traineddata",
        "url": f"{TESSDATA_URL_BASE}/eng.traineddata",
        "sha256": "daa0c97d651c19fba3b25e81317cd697e9908c8208090c94c3905381c23fc047",
        "size": 23466654,
    },
    "rus": {
        "file": "rus.traineddata",
        "url": f"{TESSDATA_URL_BASE}/rus.traineddata",
        "sha256": "681be2c2bead1bc7bd235df88c44e8e60ae73ae866840c0ad4e3b4c247bd37c2",
        "size": 19920885,
    },
}

# The languages provisioned by default, in a stable order.
DEFAULT_LANGUAGES = ("eng", "rus")

# The fallback OCR language set when no transcript script can be derived. Tesseract
# accepts a ``+``-joined list, so BOTH engines are loaded and each script is
# recognised. This is the default ``-Lang`` for the OCR consumers.
DEFAULT_LANG = "+".join(DEFAULT_LANGUAGES)


# ---------------------------------------------------------------------------
# Locations (all under the ASCII toolchain root)
# ---------------------------------------------------------------------------

def engine_dir() -> str:
    """``<root>\\tesseract`` -- the engine and its language data."""
    return paths.env_path("tesseract")


def engine_path() -> str:
    """``<root>\\tesseract\\tesseract.exe`` -- the engine executable."""
    return os.path.join(engine_dir(), "tesseract.exe")


def tessdata_dir() -> str:
    """``<root>\\tesseract\\tessdata`` -- beside the exe, where Tesseract looks."""
    return os.path.join(engine_dir(), "tessdata")


def traineddata_path(lang: str) -> str:
    """``<root>\\tesseract\\tessdata\\<lang>.traineddata``."""
    return os.path.join(tessdata_dir(), f"{lang}.traineddata")


def language_list() -> list[str]:
    """The installed traineddata, by language code, in a stable order.

    Read from disk rather than tracked in a variable: the manifest is a report of
    what is ACTUALLY there, so a language a user deleted must disappear from it.
    """
    directory = tessdata_dir()
    if not paths.is_dir(directory):
        return []
    installed = {
        entry.name[: -len(".traineddata")].lower()
        for entry in paths.list_dir(directory, files=True)
        if entry.name.lower().endswith(".traineddata")
    }
    ordered = [lang for lang in DEFAULT_LANGUAGES if lang in installed]
    ordered += sorted(installed - set(DEFAULT_LANGUAGES))
    return ordered


# ---------------------------------------------------------------------------
# Reporting (no writes)
# ---------------------------------------------------------------------------

def engine_version(exe: str | None = None) -> str | None:
    """The engine's reported version string, or ``None``.

    Prefers ``--version`` (stdout) and falls back to ``--list-langs``, because a
    Tesseract build has been observed to put its banner on either stream depending
    on configuration. Never raises: an unreachable engine is simply ``None``.
    """
    target = exe or engine_path()
    if not target or not paths.is_file(target):
        return None
    for args in (["--version"], ["--list-langs"]):
        code, text = process.run_text([target] + args, timeout=60)
        if code != 0 and not text:
            continue
        match = re.search(r"tesseract\s+v?(\d[\w.\-]*)", text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Resolution (read-only; the single source of truth for OCR consumers)
# ---------------------------------------------------------------------------

def resolve_engine() -> tuple[str | None, str]:
    """Resolve the engine path and a human-readable reason. Never raises.

    Order (first hit wins), which is exactly what makes the two probes agree:

      1. the engine the installer provisioned, in the toolchain root
      2. the system locations a winget/UB-Mannheim install uses
      3. PATH

    The toolchain-owned copy is deliberately first: it is the pinned, verified one,
    and preferring it means a stack of unrelated system Tesseracts cannot shadow
    the engine the manifest records.
    """
    provisioned = engine_path()
    if paths.is_file(provisioned):
        return provisioned, f"toolchain engine ({provisioned})"

    for candidate in _system_candidates():
        if candidate and paths.is_file(candidate):
            return paths.absolute(candidate), f"system install ({candidate})"

    process.refresh_path_from_registry()
    import shutil

    found = shutil.which("tesseract")
    if found:
        return found, f"PATH ({found})"
    return None, "not found in the toolchain, a system location, or PATH"


def _system_candidates() -> list[str]:
    """The fixed Windows install locations, checked without touching PATH."""
    if os.name != "nt":  # pragma: no cover - the toolchain is Windows-only
        return []
    roots = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
    return [os.path.join(root, "Tesseract-OCR", "tesseract.exe") for root in roots if root]


# ---------------------------------------------------------------------------
# Provisioning (writes; the caller gates on Modes.may_write)
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    """Hex sha256 of a file, read in chunks so a large archive is not held in RAM."""
    return unpack.sha256_file(path)


def verify_archive(path: str, expected: str) -> None:
    """Raise when a downloaded file does not match its published sha256.

    Delegates to :func:`zoombie.lib.unpack.verify_archive`; the message keeps its
    ``tesseract component`` wording so every existing caller and test sees the
    same text as before the refactor.
    """
    unpack.verify_archive(path, expected, what="tesseract component")


def extractor_path() -> str:
    """``<root>\\bin\\7z.exe`` -- the NSIS-capable console extractor."""
    return unpack.extractor_path()


def install_extractor(dest_dir: str | None = None) -> str:
    """Provision the NSIS-capable console 7-Zip into ``<root>\\bin``.

    Thin wrapper over :func:`zoombie.lib.unpack.install_extractor`: the extractor
    is a general capability, not a Tesseract detail, but this name stays because
    the installer already calls it.
    """
    return unpack.install_extractor(dest_dir)


def extract_installer(installer: str, extractor: str, dest_dir: str) -> str:
    """Unpack the NSIS installer payload into ``dest_dir`` WITHOUT running it.

    7-Zip reads the NSIS payload as an archive, so the installer is data, never an
    executed program: no ``/S`` switch, no registry writes, no elevation. The
    extracted tree places ``tesseract.exe`` and its DLLs at the root of ``dest_dir``.

    The extraction itself is the generic :func:`zoombie.lib.unpack.extract`; only
    the engine-presence assertion is Tesseract's, because only Tesseract knows the
    name to look for.
    """
    unpack.extract(installer, extractor, dest_dir)
    found = _find_engine(dest_dir)
    if not found:
        raise RuntimeError(
            "tesseract.exe was not found in the extracted installer payload; the "
            "installer layout may have changed"
        )
    return found


def _find_engine(root: str) -> str | None:
    """Locate ``tesseract.exe`` under ``root`` (the payload is flat, but be safe)."""
    direct = os.path.join(root, "tesseract.exe")
    if paths.is_file(direct):
        return direct
    for current, _dirs, files in os.walk(paths.to_extended(root)):
        if "tesseract.exe" in files:
            return paths.from_extended(os.path.join(current, "tesseract.exe"))
    return None


def install_engine(
    installer_path: str,
    extractor_path: str,
    dest_dir: str | None = None,
    *,
    keep_unneeded: bool = False,
) -> dict:
    """Provision the engine into the toolchain root from an ALREADY-VERIFIED installer.

    ``installer_path`` must already have passed :func:`verify_archive`; this
    function does not re-check it, so the single verification happens once, at the
    download site, exactly as cuBLAS does it.

    Only the files Tesseract needs to RUN are kept: the engine, the DLLs it loads,
    and the ``tessdata`` configuration folder. The development tools
    (``*.exe`` trainers), the Java viewer jars and the HTML documentation are
    dropped, so the provisioned tree is the runtime, not the SDK. The generic
    scaffolding exclusion in :mod:`zoombie.lib.unpack` runs first, so the
    installer's own ``$PLUGINSDIR`` DLLs can never reach the runtime tree.
    """
    dest_dir = dest_dir or engine_dir()
    tmp = paths.new_temp_dir("zoombie-tesseract")
    try:
        payload = os.path.join(tmp, "payload")
        extract_installer(installer_path, extractor_path, payload)

        paths.ensure_dir(dest_dir)
        # ``copy_payload`` is the generic choke point: it excludes installer
        # scaffolding by directory and then applies Tesseract's own runtime rule.
        kept = unpack.copy_payload(
            payload, dest_dir, predicate=lambda relative: _wanted(relative, keep_unneeded)
        )
        engine = os.path.join(dest_dir, "tesseract.exe")
        # A missing DLL was previously dropped in silence: the exe still copied and
        # the failure surfaced much later. Gate on the EXIT CODE, so a payload that
        # lost (or renamed) a library it loads fails HERE, at provision time.
        if not keep_unneeded:
            unpack.require_runs(engine, "tesseract.exe")
        return {"engine": engine, "files": kept}
    finally:
        paths.remove_quietly(tmp, recursive=True)


# Runtime files: the engine, every DLL, and the tessdata configuration data (NOT
# traineddata -- that is fetched separately). Trainers, the Java viewer and the
# documentation are the SDK and are skipped.
_TRAINER_SUFFIX = "training.exe"
_SKIP_TOOLS = {
    "ambiguous_words.exe", "classifier_tester.exe", "cntraining.exe",
    "combine_lang_model.exe", "combine_tessdata.exe", "dawg2wordlist.exe",
    "lstmeval.exe", "lstmtraining.exe", "merge_unicharsets.exe", "mftraining.exe",
    "set_unicharset_properties.exe", "shapeclustering.exe", "text2image.exe",
    "unicharset_extractor.exe", "wordlist2dawg.exe",
}
_SKIP_SUFFIXES = (".html", ".jar")

# The two configuration trees that ARE part of the runtime: Tesseract reads the
# named configs (``txt``, ``hocr``, ``pdf``, ...) and the ``tessconfigs`` batch
# wrappers at run time, so they are kept even though they carry no recognizable
# extension. Matched by DIRECTORY because their names alone are not distinctive.
_CONFIG_DIRS = ("tessdata/configs/", "tessdata/tessconfigs/")


def _wanted(relative: str, keep_unneeded: bool) -> bool:
    """True when an extracted payload file belongs in the provisioned engine.

    ``relative`` is the payload-internal path (``tessdata\\configs\\txt``). The full
    path rather than just the base name is needed because the tessdata
    configuration files have extension-less names and are only identifiable by the
    directory they live in.
    Installer scaffolding is NOT handled here: :func:`zoombie.lib.unpack.copy_payload`
    excludes it by DIRECTORY before this predicate runs, so this rule only has to
    separate the product's runtime from its SDK.
    """
    if keep_unneeded:
        return True
    lower = unpack.normalise_relative(relative).lower()
    name = lower.rsplit("/", 1)[-1]

    # Documentation and the Java viewer/logs are not runtime.
    if lower.startswith("doc/") or name.endswith(_SKIP_SUFFIXES):
        return False
    # The engine, every DLL it loads, and the PDF font are the runtime.
    if name == "tesseract.exe" or name.endswith(".dll") or name == "pdf.ttf":
        return True
    # Language data is provisioned from the official tessdata repo, not the installer.
    if name.endswith(".traineddata"):
        return False
    # The runtime configuration trees, matched by DIRECTORY (extension-less names).
    if any(lower.startswith(prefix) for prefix in _CONFIG_DIRS):
        return True
    # Trainers and the remaining convert/training tools are the SDK.
    if name in _SKIP_TOOLS or name.endswith(_TRAINER_SUFFIX):
        return False
    return False


def install_languages(dest_dir: str | None = None, languages: tuple[str, ...] = DEFAULT_LANGUAGES) -> dict:
    """Download and verify the pinned traineddata into ``<engine>\\tessdata``.

    Each file is verified against its published sha256 BEFORE it is accepted; a
    mismatch raises and leaves the previous file (if any) untouched.
    """
    target_dir = os.path.join(dest_dir, "tessdata") if dest_dir else tessdata_dir()
    paths.ensure_dir(target_dir)
    installed: list[str] = []
    for lang in languages:
        spec = LANGUAGES.get(lang)
        if not spec:
            raise RuntimeError(f"no pinned traineddata row for language '{lang}'")
        destination = os.path.join(target_dir, spec["file"])
        tmp = destination + ".download"
        download.download(spec["url"], tmp, force=True)
        verify_archive(tmp, spec["sha256"])
        os.replace(paths.to_extended(tmp), paths.to_extended(destination))
        installed.append(lang)
    return {"languages": installed, "dir": target_dir}

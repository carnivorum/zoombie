"""OCR for standalone image files (PNG/JPG/TIFF/...).

This is the *loose image* counterpart to the page OCR that lives in
:mod:`zoombie.lib.pdf`. The two are deliberately separate: ``pdf.py`` must stay
import-free so the standalone ``scripts/pdf/extract_pdf.py`` wrapper can run
without the package on ``sys.path``, and its OCR is a page-rasterizing one. This
module is the one the package commands use for an image file that is already on
disk.

Tesseract is a **toolchain-owned component**, not an optional system install: the
installer provisions a pinned, hash-verified engine into the ASCII root (see
:mod:`zoombie.lib.tesseract`), and this module prefers that engine. Resolution
order is the toolchain engine, then a system install, then PATH -- so the two
probes that used to disagree (the installer looked in Program Files while
``pytesseract`` looked on PATH) now answer the same question the same way.
:func:`available` still reports (as a version string or as the error) rather than
assuming, because a caller must be able to surface a missing engine as a named
failure instead of a silent empty result.
"""

from __future__ import annotations

import os
import re

__all__ = [
    "available",
    "ocr_image",
    "IMAGE_EXTENSIONS",
    "DEFAULT_LANG",
    "LANG_LATIN",
    "LANG_CYRILLIC",
    "contains_cyrillic",
    "sibling_transcripts",
    "derive_lang",
]


# The OCR language sets. ``eng+rus`` is the default because the toolchain's own
# deck is Russian and Tesseract accepts a ``+``-joined list (both engines load, so
# a slide mixing Russian prose with English terms still reads). A Latin-only
# transcript lets the default be narrowed to ``eng``.
DEFAULT_LANG = "eng+rus"
LANG_LATIN = "eng"
LANG_CYRILLIC = "eng+rus"

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


# Extensions treated as "an image" by the images-in path. Kept here so the
# command and any caller agree on one list.
IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif",
)


def _engine() -> tuple[str | None, str]:
    """The resolved engine path plus the reason, without importing the package.

    Imported lazily so this module stays usable in the import-light contexts
    ``pdf.py`` cares about, and kept defensive so a partially installed checkout
    degrades to a named failure rather than an ImportError.
    """
    try:
        from . import tesseract
    except Exception as exc:  # noqa: BLE001 - a broken checkout must not raise here
        return None, f"tesseract resolver unavailable: {exc}"
    return tesseract.resolve_engine()


def available() -> tuple[bool, str]:
    """Return ``(usable, version_or_error)`` for Tesseract, without raising.

    The engine is resolved by :func:`zoombie.lib.tesseract.resolve_engine` (toolchain
    engine -> system install -> PATH) and then actually run, so this distinguishes
    "the package is installed" from "the engine is reachable" -- the latter is what
    OCR needs. On a machine where the installer provisioned the engine this returns
    a version; that is the acceptance criterion it exists to satisfy.
    """
    exe, reason = _engine()
    if not exe:
        return False, f"Tesseract is not available: {reason}"
    try:
        # pytesseract is the OCR driver; point it at the RESOLVED exe rather than
        # letting it search PATH again, which is what made this disagree with the
        # installer's own probe.
        import pytesseract
    except Exception as exc:  # noqa: BLE001 - environment dependent
        return False, f"pytesseract is not installed: {exc}"
    try:
        pytesseract.pytesseract.tesseract_cmd = exe
        return True, str(pytesseract.get_tesseract_version())
    except Exception as exc:  # noqa: BLE001 - engine present but refused to run
        return False, str(exc)


def ocr_image(path: str, lang: str = "eng") -> str:
    """OCR one image file and return its text (stripped).

    Raises ``RuntimeError`` when Tesseract is unreachable, with the same message
    shape the PDF path uses, so a caller can react identically.
    """
    ok, detail = available()
    if not ok:
        raise RuntimeError(
            "OCR was requested but Tesseract is not available. Run the zoombie setup "
            "to provision the engine, or drop the OCR flag. "
            f"Detail: {detail}"
        )

    import pytesseract
    from PIL import Image

    exe, _reason = _engine()
    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe

    with Image.open(path) as image:
        # ``image_to_string`` needs an RGB-ish frame; an alpha channel or a
        # palette image is normalized so a PNG with transparency does not raise.
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        return pytesseract.image_to_string(image, lang=lang).strip()


def image_extension(path: str) -> str:
    """The lowercase extension of an image path (or ``""``)."""
    return os.path.splitext(path)[1].lower()


# ---------------------------------------------------------------------------
# Language derivation (the default for the OCR consumers)
# ---------------------------------------------------------------------------

def contains_cyrillic(text: str) -> bool:
    """True when ``text`` holds at least one Cyrillic code point."""
    return bool(text) and _CYRILLIC_RE.search(text) is not None


def sibling_transcripts(source: str) -> list[str]:
    """Transcript/SRT files that could tell us which script the media uses.

    Two layouts are probed, because both occur in practice:

    * an ITEM -- ``<item>/.data/transcript.txt`` / ``.srt`` beside the source
    * a STANDALONE file -- ``<base>.txt`` / ``.srt`` named after the source

    The item paths are spelled with a local join rather than importing
    ``zoombie.item.paths``: that module imports ``zoombie.lib.paths`` only, but
    keeping this module free of package sibling imports is what lets ``ocr`` stay
    usable from the import-light contexts ``pdf.py`` needs.
    """
    if not source:
        return []
    base = os.path.splitext(source)[0]
    candidates: list[str] = []
    # Sibling-of-source files (a standalone transcript).
    for extension in ("srt", "txt"):
        candidates.append(f"{base}.{extension}")
    # Item layout: <item>/.data/transcript.<ext>, where source may be <item>/media.
    parent = os.path.dirname(source)
    for item_dir in (parent, os.path.dirname(parent)):
        if not item_dir:
            continue
        data_dir = os.path.join(item_dir, ".data")
        for extension in ("srt", "txt"):
            candidates.append(os.path.join(data_dir, f"transcript.{extension}"))

    seen: set[str] = set()
    found: list[str] = []
    for candidate in candidates:
        norm = os.path.normcase(candidate)
        if norm in seen or not os.path.isfile(candidate):
            continue
        seen.add(norm)
        found.append(candidate)
    return found


def derive_lang(source: str, override: str | None = None) -> str:
    """The OCR language to use: an explicit override, else derived from a sibling.

    ``override`` wins verbatim -- ``-Lang`` stays exactly the escape hatch it was.
    Otherwise the first sibling transcript that exists decides:

      * Cyrillic present  -> ``eng+rus``
      * Latin only        -> ``eng``
      * no transcript     -> ``eng+rus`` (the toolchain's deck is Russian, so the
        safe default reads both scripts rather than silently missing Russian)
    """
    if override:
        return override
    for path in sibling_transcripts(source):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                if contains_cyrillic(handle.read()):
                    return LANG_CYRILLIC
        except OSError:
            continue
        return LANG_LATIN
    return DEFAULT_LANG

"""OCR for standalone image files (PNG/JPG/TIFF/...).

This is the *loose image* counterpart to the page OCR that lives in
:mod:`zoombie.lib.pdf`. The two are deliberately separate: ``pdf.py`` must stay
import-free so the standalone ``scripts/pdf/extract_pdf.py`` wrapper can run
without the package on ``sys.path``, and its OCR is a page-rasterizing one. This
module is the one the package commands use for an image file that is already on
disk.

Tesseract is an optional dependency: it is a system install, not a pip package,
so :func:`available` reports whether it can be reached (as a version string or as
the error) rather than assuming it. Callers surface that to the user and offer to
proceed without OCR, because a missing Tesseract must never be a silent empty
result.
"""

from __future__ import annotations

import os

__all__ = ["available", "ocr_image", "IMAGE_EXTENSIONS"]


# Extensions treated as "an image" by the images-in path. Kept here so the
# command and any caller agree on one list.
IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif",
)


def available() -> tuple[bool, str]:
    """Return ``(usable, version_or_error)`` for Tesseract, without raising.

    ``get_tesseract_version`` is what actually shells out to the binary, so this
    distinguishes "the package is installed" from "the engine is reachable" --
    the latter is what OCR needs.
    """
    try:
        import pytesseract
    except Exception as exc:  # noqa: BLE001 - environment dependent
        return False, f"pytesseract is not installed: {exc}"
    try:
        return True, str(pytesseract.get_tesseract_version())
    except Exception as exc:  # noqa: BLE001 - Tesseract binary missing/misconfigured
        return False, str(exc)


def ocr_image(path: str, lang: str = "eng") -> str:
    """OCR one image file and return its text (stripped).

    Raises ``RuntimeError`` when Tesseract is unreachable, with the same message
    shape the PDF path uses, so a caller can react identically.
    """
    ok, detail = available()
    if not ok:
        raise RuntimeError(
            "OCR was requested but Tesseract is not available. Install it "
            "(winget install UB-Mannheim.TesseractOCR) and re-run, or drop the OCR "
            f"flag. Detail: {detail}"
        )

    import pytesseract
    from PIL import Image

    with Image.open(path) as image:
        # ``image_to_string`` needs an RGB-ish frame; an alpha channel or a
        # palette image is normalized so a PNG with transparency does not raise.
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        return pytesseract.image_to_string(image, lang=lang).strip()


def image_extension(path: str) -> str:
    """The lowercase extension of an image path (or ``""``)."""
    return os.path.splitext(path)[1].lower()

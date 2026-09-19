#!/usr/bin/env python3
"""Deterministic PDF -> Markdown extractor for the zoombie toolchain.

Called only by ``zoombie.ps1`` (command: ``readpdf``). A skill never invokes this
directly, and never re-assembles its arguments by hand.

Design
------
* Text-based pages are converted with ``pymupdf4llm.to_markdown`` from PyMuPDF,
  which understands headings, lists, tables and bold/italic runs -- so the output
  is real Markdown, not a wall of plain text.
* Image-only (scanned) pages are, when ``--ocr`` is given, rasterized with
  PyMuPDF and passed through Tesseract via ``pytesseract``. OCR is opt-in because
  it needs a system Tesseract install; without it, scanned pages are skipped and
  reported rather than silently dropped.

ASCII invariant
---------------
Every path this script receives is ASCII. ``zoombie.ps1`` copies the input PDF
into an ASCII scratch directory before calling here, mirroring the fix used for
whisper.cpp, so a Cyrillic source or destination path can never reach a native
library through this script.

Exit codes
----------
0  Markdown written (possibly with skipped scanned pages, reported in JSON)
1  A real failure (unreadable PDF, OCR requested but Tesseract missing, ...)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys


def _log(message: str) -> None:
    """Human-readable progress on stderr, so stdout stays a single JSON line."""
    sys.stderr.write("    " + message + "\n")
    sys.stderr.flush()


def parse_pages(spec: str | None) -> list[int] | None:
    """Turn a '1-5,8' page spec into a sorted list of 0-based page indices."""
    if not spec:
        return None
    pages: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            if "-" in chunk:
                start_s, end_s = chunk.split("-", 1)
                start, end = int(start_s), int(end_s)
                if start > end:
                    start, end = end, start
                pages.update(range(start - 1, end))
            else:
                pages.add(int(chunk) - 1)
        except ValueError as exc:
            raise SystemExit(f"Invalid --pages value '{chunk}': {exc}") from exc
    return sorted(p for p in pages if p >= 0)


def page_has_text(page) -> bool:
    """True when a page yields extractable text (i.e. it is not a scan)."""
    try:
        return bool(page.get_text("text").strip())
    except Exception:
        return False


def ocr_page(page, lang: str) -> str:
    """Rasterize one page and run Tesseract over it."""
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=200)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(image, lang=lang).strip()


def ocr_available() -> tuple[bool, str | None]:
    """Return (available, version-or-error) for Tesseract without raising."""
    try:
        import pytesseract

        return True, str(pytesseract.get_tesseract_version())
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, str(exc)


def build_markdown(doc, selected: list[int], args) -> tuple[str, bool, list[int]]:
    """Return (markdown, ocr_used, skipped_scan_pages)."""
    import pymupdf4llm

    blocks: list[str] = []
    ocr_used = False
    skipped: list[int] = []

    # OCR is prepared lazily so a text-only PDF never pays for it.
    ocr_ready = False
    if args.ocr:
        available, detail = ocr_available()
        if not available:
            raise SystemExit(
                "OCR requested (--ocr) but Tesseract is not available. "
                "Install it (winget install UB-Mannheim.TesseractOCR) and re-run, "
                f"or drop --ocr. Detail: {detail}"
            )
        ocr_ready = True

    image_kwargs = {}
    if args.images:
        os.makedirs(args.images, exist_ok=True)
        image_kwargs = {"write_images": True, "image_path": args.images, "image_format": "png"}

    # Group consecutive pages of the same kind so the output keeps page order.
    index = 0
    total = len(selected)
    while index < total:
        is_text = page_has_text(doc[selected[index]])
        end = index
        while end < total and page_has_text(doc[selected[end]]) == is_text:
            end += 1
        group = selected[index:end]

        if is_text:
            part = pymupdf4llm.to_markdown(doc, pages=group, **image_kwargs)
            if part and part.strip():
                blocks.append(part)
        elif ocr_ready:
            for page_no in group:
                text = ocr_page(doc[page_no], args.lang)
                if text:
                    blocks.append(f"<!-- page {page_no + 1} (OCR) -->\n\n{text}\n")
                    ocr_used = True
        else:
            skipped.extend(p + 1 for p in group)

        index = end

    return "\n\n".join(b.rstrip() for b in blocks).strip() + "\n", ocr_used, skipped


def emit(payload: dict, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a PDF to Markdown (zoombie).")
    parser.add_argument("--input", required=True, help="ASCII path to the source PDF")
    parser.add_argument("--output", required=True, help="ASCII path of the .md to write")
    parser.add_argument("--ocr", action="store_true", help="OCR image-only pages with Tesseract")
    parser.add_argument("--images", default=None, help="directory to write extracted images into")
    parser.add_argument("--pages", default=None, help="page range, e.g. 1-5,8")
    parser.add_argument("--lang", default="eng", help="Tesseract language code (default: eng)")
    parser.add_argument("--json", action="store_true", help="print one JSON result line on stdout")
    args = parser.parse_args(argv)

    result_data: dict = {
        "output": args.output,
        "pages": 0,
        "ocrUsed": False,
        "keptScannedPages": [],
        "engine": "pymupdf4llm",
        "images": args.images,
        "bytes": 0,
    }

    try:
        import pymupdf  # PyMuPDF (the `fitz` alias is deprecated and warns)

        if not os.path.isfile(args.input):
            raise SystemExit(f"Input not found: {args.input}")

        doc = pymupdf.open(args.input)
        try:
            total_pages = doc.page_count
            selected = parse_pages(args.pages)
            if selected is None:
                selected = list(range(total_pages))
            else:
                selected = [p for p in selected if p < total_pages]
            if not selected:
                raise SystemExit("No pages selected (check --pages).")

            _log(
                f"readpdf: {total_pages} page(s), extracting {len(selected)} "
                f"(ocr={'on' if args.ocr else 'off'})"
            )

            markdown, ocr_used, skipped = build_markdown(doc, selected, args)
        finally:
            doc.close()

        out_dir = os.path.dirname(os.path.abspath(args.output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)

        result_data.update(
            {
                "pages": len(selected),
                "ocrUsed": ocr_used,
                "keptScannedPages": skipped,
                "bytes": os.path.getsize(args.output),
                "textPages": len(selected) - len(skipped),
            }
        )
        emit({"ok": True, "action": "readpdf", "data": result_data, "error": None}, args.json)
        return 0

    except SystemExit as exc:
        message = str(exc) if exc.code else "readpdf failed"
        emit({"ok": False, "action": "readpdf", "data": result_data, "error": message}, args.json)
        return 1
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the CLI
        emit({"ok": False, "action": "readpdf", "data": result_data, "error": f"{exc}"}, args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

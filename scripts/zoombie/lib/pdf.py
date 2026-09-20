"""PDF -> Markdown, importable and also usable as a script.

Text-based pages are converted with ``pymupdf4llm.to_markdown``, which understands
headings, lists, tables and bold/italic runs, so the output is real Markdown
rather than a wall of plain text.

Image-only (scanned) pages are, when OCR is requested, rasterized with PyMuPDF and
passed through Tesseract via ``pytesseract``. OCR is opt-in because it needs a
system Tesseract install; without it, scanned pages are reported as skipped
rather than silently dropped.

The ASCII invariant applies exactly as it does for whisper.cpp: the CLI copies the
source PDF into an ASCII scratch dir first, so a Cyrillic source or destination
can never reach a native library through this module.

This module is intentionally free of any ``zoombie`` package imports so the
standalone ``scripts/pdf/extract_pdf.py`` wrapper and the installed CLI can both
use it, whether or not the package is on ``sys.path``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass, field


def log(message: str) -> None:
    """Human-readable progress on stderr, so stdout stays a single JSON line."""
    sys.stderr.write("    " + message + "\n")
    sys.stderr.flush()


def parse_pages(spec: str | None) -> list[int] | None:
    """Turn a ``'1-5,8'`` page spec into a sorted list of 0-based indices."""
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
            raise ValueError(f"Invalid --pages value '{chunk}': {exc}") from exc
    return sorted(page for page in pages if page >= 0)


@dataclass
class Options:
    """Everything the converter needs, decoupled from argparse."""

    input: str
    output: str
    ocr: bool = False
    images: str | None = None
    pages: str | None = None
    lang: str = "eng"


@dataclass
class Result:
    """Converter outcome, shaped for the CLI's JSON ``data`` object."""

    output: str
    pages: int = 0
    ocr_used: bool = False
    kept_scanned_pages: list[int] = field(default_factory=list)
    engine: str = "pymupdf4llm"
    images: str | None = None
    bytes: int = 0
    text_pages: int = 0

    def to_data(self) -> dict:
        return {
            "output": self.output,
            "pages": self.pages,
            "ocrUsed": self.ocr_used,
            "keptScannedPages": list(self.kept_scanned_pages),
            "engine": self.engine,
            "images": self.images,
            "bytes": self.bytes,
            "textPages": self.text_pages,
        }


def page_has_text(page) -> bool:
    """True when a page yields extractable text (i.e. it is not a scan)."""
    try:
        return bool(page.get_text("text").strip())
    except Exception:
        return False


def ocr_available() -> tuple[bool, str | None]:
    """Return ``(available, version_or_error)`` for Tesseract without raising."""
    try:
        import pytesseract

        return True, str(pytesseract.get_tesseract_version())
    except Exception as exc:  # environment dependent
        return False, str(exc)


def ocr_page(page, lang: str) -> str:
    """Rasterize one page and run Tesseract over it."""
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=200)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(image, lang=lang).strip()


def _as_text(part: object) -> str:
    """Normalize a pymupdf4llm result to plain Markdown text.

    ``to_markdown`` returns a string, but some versions/options return a list of
    per-page dicts carrying the text under ``text``. Both shapes are accepted so a
    library upgrade cannot silently produce empty output.
    """
    if isinstance(part, str):
        return part
    if isinstance(part, list):
        chunks: list[str] = []
        for item in part:
            if isinstance(item, dict):
                value = item.get("text")
                if isinstance(value, str):
                    chunks.append(value)
            elif isinstance(item, str):
                chunks.append(item)
        return "\n\n".join(chunks)
    return ""


def build_markdown(doc, selected: list[int], options: Options) -> tuple[str, bool, list[int]]:
    """Return ``(markdown, ocr_used, skipped_scanned_pages)``."""
    import pymupdf4llm

    blocks: list[str] = []
    ocr_used = False
    skipped: list[int] = []

    # OCR is prepared lazily so a text-only PDF never pays for it.
    ocr_ready = False
    if options.ocr:
        available, detail = ocr_available()
        if not available:
            raise RuntimeError(
                "OCR requested (--ocr) but Tesseract is not available. Install it "
                "(winget install UB-Mannheim.TesseractOCR) and re-run, or drop "
                f"--ocr. Detail: {detail}"
            )
        ocr_ready = True

    image_kwargs: dict = {}
    if options.images:
        os.makedirs(options.images, exist_ok=True)
        image_kwargs = {
            "write_images": True,
            "image_path": options.images,
            "image_format": "png",
        }

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
            text_part = _as_text(part)
            if text_part.strip():
                blocks.append(text_part)
        elif ocr_ready:
            for page_no in group:
                text = ocr_page(doc[page_no], options.lang)
                if text:
                    blocks.append(f"<!-- page {page_no + 1} (OCR) -->\n\n{text}\n")
                    ocr_used = True
        else:
            skipped.extend(page + 1 for page in group)

        index = end

    markdown = "\n\n".join(block.rstrip() for block in blocks).strip() + "\n"
    return markdown, ocr_used, skipped


def convert(options: Options) -> Result:
    """Convert a PDF to Markdown at ``options.output``."""
    import pymupdf  # PyMuPDF; the `fitz` alias is deprecated and warns

    if not os.path.isfile(options.input):
        raise FileNotFoundError(f"Input not found: {options.input}")

    doc = pymupdf.open(options.input)
    try:
        total_pages = doc.page_count
        selected = parse_pages(options.pages)
        if selected is None:
            selected = list(range(total_pages))
        else:
            selected = [page for page in selected if page < total_pages]
        if not selected:
            raise ValueError("No pages selected (check --pages).")

        log(
            f"readpdf: {total_pages} page(s), extracting {len(selected)} "
            f"(ocr={'on' if options.ocr else 'off'})"
        )

        markdown, ocr_used, skipped = build_markdown(doc, selected, options)
    finally:
        doc.close()

    output_dir = os.path.dirname(os.path.abspath(options.output))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(options.output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markdown)

    return Result(
        output=options.output,
        pages=len(selected),
        ocr_used=ocr_used,
        kept_scanned_pages=skipped,
        text_pages=len(selected) - len(skipped),
        images=options.images,
        bytes=os.path.getsize(options.output),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert a PDF to Markdown (zoombie).")
    parser.add_argument("--input", required=True, help="ASCII path to the source PDF")
    parser.add_argument("--output", required=True, help="ASCII path of the .md to write")
    parser.add_argument("--ocr", action="store_true", help="OCR image-only pages with Tesseract")
    parser.add_argument("--images", default=None, help="directory to write extracted images into")
    parser.add_argument("--pages", default=None, help="page range, e.g. 1-5,8")
    parser.add_argument("--lang", default="eng", help="Tesseract language code (default: eng)")
    parser.add_argument("--json", action="store_true", help="print one JSON result line on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: identical flags and JSON shape as before."""
    args = build_parser().parse_args(argv)
    options = Options(
        input=args.input,
        output=args.output,
        ocr=args.ocr,
        images=args.images,
        pages=args.pages,
        lang=args.lang,
    )

    def emit(ok: bool, data: dict, error: str | None) -> None:
        if args.json:
            payload = {"ok": ok, "action": "readpdf", "data": data, "error": error}
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    partial = Result(output=args.output, images=args.images)
    try:
        result = convert(options)
    except (RuntimeError, ValueError, OSError) as exc:
        emit(False, partial.to_data(), str(exc))
        return 1
    emit(True, result.to_data(), None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

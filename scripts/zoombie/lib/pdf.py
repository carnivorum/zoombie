"""PDF -> Markdown, importable and also usable as a script.

Text-based pages are converted with ``pymupdf4llm.to_markdown``, which understands
headings, lists, tables and bold/italic runs, so the output is real Markdown
rather than a wall of plain text.

Image-only (scanned) pages are, when OCR is requested, rasterized with PyMuPDF and
passed through Tesseract via ``pytesseract``. OCR is opt-in because it needs a
system Tesseract install; without it, scanned pages are reported as skipped
rather than silently dropped.

Image extraction does NOT go through pymupdf4llm. Its ``write_images`` only fires
for text pages and produces a flat directory with no notion of *where* a figure
was painted, which is useless for re-inserting figures into an existing Markdown
document. What is used instead is ``page.get_image_info(xrefs=True)`` -- the only
call that reports what is actually *drawn* -- and every kept image is recorded
with its page, bbox, pixel size and the text block it hangs off, then written out
with a sidecar ``manifest.json`` / ``README.md``. ``collect_images`` is the entry
point for that, ``images_sidecar`` for the metadata.

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

# Image filtering thresholds. BOTH are required: the pixel size separates real
# content from the 4x4..52x46 px glyph/icon rasters (in the corpus this was built
# against, 334 unique xrefs were all 1081-byte font-atlas tiles), while the
# on-page box separates content from hairline rules that are wide but flat.
DEFAULT_MIN_PX = 64
DEFAULT_MIN_PT = 30

# Names of the two sidecar files written into an image directory.
SIDECAR_MANIFEST = "manifest.json"
SIDECAR_README = "README.md"


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
    # Placement-aware image extraction. ``images`` stays the legacy "extract
    # images, defaulting the directory to <base>.images" switch; ``images_dir``
    # is an explicit directory, ``images_only`` skips Markdown entirely (so an
    # existing .md is never in the way), ``sidecar`` writes manifest.json +
    # README.md, and the two thresholds feed keep_image().
    images_only: bool = False
    images_dir: str | None = None
    sidecar: bool = True
    min_px: int = DEFAULT_MIN_PX
    min_pt: int = DEFAULT_MIN_PT


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
    images_manifest: str | None = None
    image_count: int = 0
    images_placed: int = 0
    images_skipped: list[dict] = field(default_factory=list)

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
            "imagesManifest": self.images_manifest,
            "imageCount": self.image_count,
            "imagesPlaced": self.images_placed,
            "imagesSkipped": [dict(entry) for entry in self.images_skipped],
        }


@dataclass
class ImageInfo:
    """One drawn image, with everything needed to place it back inline.

    ``bbox`` is in PDF points (``x0, y0, x1, y1``) while ``width``/``height`` are
    raster pixels: the pixels distinguish content from glyph tiles, the points
    distinguish it from flat rules, and only the bbox says *where* on the page the
    figure sat.
    """

    file: str
    page: int
    xref: int
    width: int
    height: int
    bbox: tuple[float, float, float, float]
    anchor_text: str = ""
    page_title: str = ""
    digest: str = ""
    bytes: int = 0

    def to_manifest(self) -> dict:
        """Sidecar manifest row, built only once the file is on disk."""
        return {
            "file": self.file,
            "page": self.page,
            "xref": self.xref,
            "width": self.width,
            "height": self.height,
            "bbox": [round(value, 1) for value in self.bbox],
            "anchor_text": self.anchor_text,
            "page_title": self.page_title,
            "digest": self.digest,
            "bytes": self.bytes,
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


# ---------------------------------------------------------------------------
# Image collection
# ---------------------------------------------------------------------------

def _field(source, name: str, default):
    """Read ``name`` from a mapping or from an :class:`ImageInfo`-like object."""
    if isinstance(source, dict):
        value = source.get(name)
    else:
        value = getattr(source, name, None)
    return default if value is None else value


def keep_image(info, min_px: int = DEFAULT_MIN_PX, min_pt: int = DEFAULT_MIN_PT) -> bool:
    """Decide whether a drawn image is real content or a glyph/icon tile.

    ``info`` is either a raw ``page.get_image_info()`` dict or an
    :class:`ImageInfo`; both shapes are accepted so the filter can be unit-tested
    without a PDF. The larger pixel side must clear ``min_px``, and the on-page
    box must not be small in either
    dimension nor in area.
    """
    width = int(_field(info, "width", 0) or 0)
    height = int(_field(info, "height", 0) or 0)
    if max(width, height) < min_px:
        return False
    bbox = _field(info, "bbox", None)
    if bbox:
        box_w = float(bbox[2]) - float(bbox[0])
        box_h = float(bbox[3]) - float(bbox[1])
        if box_w < min_pt and box_h < min_pt:
            return False
        if box_w * box_h < float(min_pt * min_pt):
            return False
    return True


def _skip(sink: list[dict] | None, reason: str, page: int, info: dict | None = None,
          error: str | None = None) -> dict:
    """Record why an image was not extracted, and return the entry.

    Every drop is reported with one of ``glyph``, ``xref0``, ``duplicate``,
    ``error`` or ``scanned-page``; a silent drop is what made the old ``-Images``
    flag unusable for a library workflow.
    """
    entry: dict = {"reason": reason, "page": page}
    if info is not None:
        entry["xref"] = int(info.get("xref") or 0)
        entry["width"] = int(info.get("width") or 0)
        entry["height"] = int(info.get("height") or 0)
        bbox = info.get("bbox")
        if bbox:
            entry["bbox"] = [round(float(value), 1) for value in bbox[:4]]
    if error:
        entry["error"] = error
    if sink is not None:
        sink.append(entry)
    return entry


def text_blocks(page) -> list[dict]:
    """Text blocks of a page as normalized dicts.

    ``page.get_text("blocks")`` yields ``(x0, y0, x1, y1, text, block_no,
    block_type)``; only ``block_type == 0`` (text) is kept, so image blocks can
    never become an image's own anchor. A page that raises yields an empty list
    rather than aborting the run.
    """
    blocks: list[dict] = []
    try:
        raw = page.get_text("blocks") or []
    except Exception:  # noqa: BLE001 - a broken page must not abort the run
        return blocks
    for block in raw:
        if len(block) < 7 or block[6] != 0:
            continue
        text = str(block[4] or "").strip()
        if not text:
            continue
        blocks.append({
            "x0": float(block[0]),
            "y0": float(block[1]),
            "x1": float(block[2]),
            "y1": float(block[3]),
            "text": text,
        })
    return blocks


def anchor_for(bbox, blocks: list[dict], carry: str = "") -> str:
    """Text of the block immediately above ``bbox`` -- the image's anchor.

    "Closest block strictly above, tie-broken by horizontal overlap" is the rule:
    the *bottom* edge decides, so the paragraph a figure follows wins over a
    narrow sidebar ending higher up. A figure at the very top of a page has no
    block above it, and the anchor is then the last text of the previous page,
    passed in as ``carry``.
    """
    x0, y0 = float(bbox[0]), float(bbox[1])
    best: tuple[float, float, str] | None = None
    for block in blocks:
        if float(block["y1"]) > y0 + 1.0:
            continue  # not strictly above the image
        overlap = min(float(block["x1"]), float(bbox[2])) - max(float(block["x0"]), x0)
        if best is None or (float(block["y1"]), overlap) > best[:2]:
            best = (float(block["y1"]), overlap, block["text"])
    return best[2] if best is not None else carry


def collect_images(page, min_px: int = DEFAULT_MIN_PX, min_pt: int = DEFAULT_MIN_PT,
                   seen: set[str] | None = None, skipped: list[dict] | None = None,
                   carry: str = "") -> list[ImageInfo]:
    """Drawn images on one page, filtered, deduplicated and anchored.

    Uses ``page.get_image_info(xrefs=True)`` and **not** ``page.get_images()``:
    ``get_images()`` over-reports, because it lists every embedded XObject the
    page *references*, including background xrefs inherited from a template that
    are never painted (one deck in the corpus this was built against referenced a
    cover xref on pages it never drew it on). ``get_image_info`` reports what is
    actually drawn, with
    a bbox, which is the only reliable placement metadata.

    Images are first sorted by ``(bbox.y0, bbox.x0)``, then filtered by
    :func:`keep_image` on BOTH the pixel size
    and the on-page box, ``xref <= 0`` entries are skipped (an inline image has no
    XObject stream and ``extract_image(0)`` raises "bad xref"), and duplicates are
    dropped by ``digest`` with ``xref:bbox`` as the fallback key.

    ``seen``/``skipped`` are caller-owned sinks so the dedupe key spans the whole
    document and every drop is reported; ``carry`` is the previous page's last
    text block, used when a figure starts a page.
    """
    try:
        infos = page.get_image_info(xrefs=True) or []
    except Exception as exc:  # noqa: BLE001
        _skip(skipped, "error", page.number + 1, error=str(exc))
        return []

    blocks = text_blocks(page)
    page_number = page.number + 1
    records: list[ImageInfo] = []
    drawable = [info for info in infos if info.get("bbox")]
    drawable.sort(key=lambda item: (round(item["bbox"][1], 1), round(item["bbox"][0], 1)))

    for info in drawable:
        if not keep_image(info, min_px, min_pt):
            _skip(skipped, "glyph", page_number, info)
            continue
        xref = int(info.get("xref") or 0)
        if xref <= 0:
            _skip(skipped, "xref0", page_number, info)
            continue
        digest = str(info.get("digest") or "")
        key = digest or f"{xref}:{info.get('bbox')}"
        if seen is not None:
            if key in seen:
                _skip(skipped, "duplicate", page_number, info)
                continue
            seen.add(key)
        bbox = tuple(float(value) for value in info["bbox"][:4])
        records.append(ImageInfo(
            file="",
            page=page_number,
            xref=xref,
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            bbox=bbox,  # type: ignore[arg-type]
            anchor_text=anchor_for(bbox, blocks, carry),
            # Fallback anchor: the first text of the page, which on a slide deck
            # is the slide title and far more stable than arbitrary chart labels.
            page_title=blocks[0]["text"] if blocks else "",
            digest=digest,
        ))
    return records


def collect_document(doc, selected: list[int], min_px: int = DEFAULT_MIN_PX,
                     min_pt: int = DEFAULT_MIN_PT) -> tuple[list[ImageInfo], list[dict]]:
    """Collect images for the whole document, in reading order.

    Returns ``(records, skipped)``. The digest set spans the document, so a cover
    tile redrawn on several pages is kept once, and the anchor carry is the
    previous page's *last* text block. A page with no text at all that yields no
    images is reported as ``scanned-page`` rather than being left invisible.
    """
    records: list[ImageInfo] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    carry = ""
    for page_number in selected:
        page = doc.load_page(page_number)
        page_records = collect_images(
            page, min_px, min_pt, seen=seen, skipped=skipped, carry=carry
        )
        blocks = text_blocks(page)
        if not page_records and not page_has_text(page):
            _skip(skipped, "scanned-page", page_number + 1)
        records.extend(page_records)
        if blocks:
            carry = max(blocks, key=lambda block: block["y1"])["text"]
    return records, skipped


def extract_image_png(doc, xref: int) -> tuple[bytes, str]:
    """Return ``(bytes, extension)`` for one xref, preferring a real PNG.

    Alpha needs an explicit mask merge: PyMuPDF hands an image and its ``smask``
    back separately, and a colorspace
    with 4 or more components (CMYK, or an unmerged alpha) raises on a straight
    PNG save, so it is converted to ``csRGB`` first. Anything that still fails
    falls back to the native stream from ``doc.extract_image`` with its own
    extension -- which is what a JPEG/JPX figure needs anyway.
    """
    import pymupdf  # PyMuPDF; the `fitz` alias is deprecated and warns

    try:
        info = doc.extract_image(xref)
        pix = pymupdf.Pixmap(doc, xref)
        smask = info.get("smask")
        if smask:
            pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, smask))
        if pix.colorspace is None:
            raise ValueError("mask-only image")
        if pix.colorspace.n >= 4:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        return pix.tobytes("png"), "png"
    except Exception:  # noqa: BLE001 - fall back to the native stream
        pass

    info = doc.extract_image(xref)
    return info["image"], str(info.get("ext") or "png")


def _unique_name(taken: set[str], stem: str, ext: str) -> str:
    """Name ``001 - p01.png``, suffixed only if that exact name is taken."""
    name = f"{stem}.{ext}"
    counter = 2
    while name.lower() in taken:
        name = f"{stem} ({counter}).{ext}"
        counter += 1
    return name


def write_images(doc, records: list[ImageInfo], directory: str) -> tuple[list[dict], list[dict]]:
    """Write every collected image into ``directory``; return ``(entries, skipped)``.

    Manifest entries are built HERE, after each file is on disk, and only for
    files that were actually written. Building the report before the write loop is
    the bug that produced a manifest claiming 211 images while only 203 files
    existed -- the per-image errors were invisible. Failures land in ``skipped``
    with ``reason: "error"`` instead.
    """
    entries: list[dict] = []
    skipped: list[dict] = []
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    taken = {name.lower() for name in os.listdir(directory)}

    for index, record in enumerate(records, start=1):
        stem = f"{index:03d} - p{record.page:02d}"
        try:
            data, ext = extract_image_png(doc, record.xref)
        except Exception as exc:  # noqa: BLE001
            _skip(skipped, "error", record.page, {"xref": record.xref}, error=str(exc))
            continue
        name = _unique_name(taken, stem, ext)
        try:
            with open(os.path.join(directory, name), "wb") as handle:
                handle.write(data)
        except OSError as exc:
            _skip(skipped, "error", record.page, {"xref": record.xref}, error=str(exc))
            continue
        taken.add(name.lower())
        record.file = name
        record.bytes = len(data)
        entries.append(record.to_manifest())
    return entries, skipped


def _table_cell(text: str, limit: int = 60) -> str:
    """One-line, pipe-safe cell for the README table."""
    cell = " ".join(str(text or "").split()).replace("|", "\\|")
    if len(cell) > limit:
        cell = cell[: limit - 1].rstrip() + "\u2026"
    return cell or "\u2014"


def images_sidecar(directory: str, entries: list[dict], source: str = "") -> str:
    """Write the image directory's sidecar: ``manifest.json`` plus ``README.md``.

    Both are rendered from ``entries`` -- the records :func:`write_images`
    actually produced -- so every row describes a file that exists on disk. The
    JSON is the machine-readable placement record (page, bbox, anchor, digest)
    that lets a caller inline the figures without reopening the PDF; README.md is
    the same table for a human. Returns the manifest path.
    """
    manifest_path = os.path.join(directory, SIDECAR_MANIFEST)
    payload = {
        "source": os.path.basename(source) if source else "",
        "count": len(entries),
        "images": entries,
    }
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    lines = ["# Extracted images", ""]
    if source:
        lines.append(f"Source: `{os.path.basename(source)}`")
    lines.append(f"Images: {len(entries)}")
    lines.extend([
        "",
        "| File | Page | Size, px | Place in PDF, pt | Anchor | Bytes |",
        "|------|------|----------|------------------|--------|-------|",
    ])
    for entry in entries:
        bbox = entry.get("bbox") or [0, 0, 0, 0]
        placement = "{:.0f},{:.0f}".format(float(bbox[0]), float(bbox[1]))
        lines.append(
            f"| `{entry.get('file', '')}` | {entry.get('page', '')} "
            f"| {entry.get('width', 0)}\u00d7{entry.get('height', 0)} "
            f"| {placement} | {_table_cell(entry.get('anchor_text', ''))} "
            f"| {entry.get('bytes', 0)} |"
        )
    lines.append("")
    with open(os.path.join(directory, SIDECAR_README), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return manifest_path


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

    # Images are deliberately NOT delegated to pymupdf4llm's write_images: it only
    # fires for text pages (so scanned pages silently get nothing), writes a flat
    # directory with no placement metadata, and would duplicate the files that
    # collect_images()/write_images() produce. Image extraction lives in convert().
    index = 0
    total = len(selected)
    while index < total:
        is_text = page_has_text(doc[selected[index]])
        end = index
        while end < total and page_has_text(doc[selected[end]]) == is_text:
            end += 1
        group = selected[index:end]

        if is_text:
            part = pymupdf4llm.to_markdown(doc, pages=group)
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
    """Convert a PDF to Markdown at ``options.output`` (and extract its images)."""
    import pymupdf  # PyMuPDF; the `fitz` alias is deprecated and warns

    if not os.path.isfile(options.input):
        raise FileNotFoundError(f"Input not found: {options.input}")

    images_dir = options.images_dir or options.images
    if options.images_only and not images_dir:
        raise ValueError(
            "Image-only mode needs an image directory: pass --image-dir (the CLI "
            "defaults it to <base>.images)."
        )

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

        markdown = ""
        ocr_used = False
        skipped_pages: list[int] = []
        if options.images_only:
            log("readpdf: images only - Markdown is not rendered")
        else:
            markdown, ocr_used, skipped_pages = build_markdown(doc, selected, options)

        entries: list[dict] = []
        images_skipped: list[dict] = []
        manifest: str | None = None
        if images_dir:
            records, images_skipped = collect_document(doc, selected, options.min_px, options.min_pt)
            log(f"readpdf: {len(records)} image(s) kept, {len(images_skipped)} skipped")
            entries, failed = write_images(doc, records, images_dir)
            images_skipped.extend(failed)
            if options.sidecar:
                manifest = images_sidecar(images_dir, entries, options.input)
    finally:
        doc.close()

    if options.images_only:
        written_bytes = 0  # no Markdown was rendered, by design
    else:
        output_dir = os.path.dirname(os.path.abspath(options.output))
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(options.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        written_bytes = os.path.getsize(options.output)

    placed = sum(1 for entry in entries if entry.get("file"))
    return Result(
        output=options.output,
        pages=len(selected),
        ocr_used=ocr_used,
        kept_scanned_pages=skipped_pages,
        text_pages=len(selected) - len(skipped_pages),
        images=images_dir,
        bytes=written_bytes,
        images_manifest=manifest,
        image_count=len(entries),
        images_placed=placed,
        images_skipped=images_skipped,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert a PDF to Markdown (zoombie).")
    parser.add_argument("--input", required=True, help="ASCII path to the source PDF")
    parser.add_argument(
        "--output", required=True,
        help="ASCII path of the .md to write (ignored by --images-only)",
    )
    parser.add_argument("--ocr", action="store_true", help="OCR image-only pages with Tesseract")
    parser.add_argument("--images", default=None, help="directory to write extracted images into")
    parser.add_argument("--pages", default=None, help="page range, e.g. 1-5,8")
    parser.add_argument("--lang", default="eng", help="Tesseract language code (default: eng)")
    parser.add_argument(
        "--images-only", action="store_true",
        help="extract images and the sidecar only; render no Markdown",
    )
    parser.add_argument("--image-dir", default=None, help="explicit image directory")
    parser.add_argument("--min-px", type=int, default=DEFAULT_MIN_PX,
                        help=f"drop images below this pixel size (default: {DEFAULT_MIN_PX})")
    parser.add_argument("--min-pt", type=int, default=DEFAULT_MIN_PT,
                        help=f"drop images below this on-page size in points (default: {DEFAULT_MIN_PT})")
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
        images_only=args.images_only,
        images_dir=args.image_dir,
        min_px=args.min_px,
        min_pt=args.min_pt,
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

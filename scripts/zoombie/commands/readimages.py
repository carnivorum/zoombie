"""``readimages``: turn a loose image (or a folder of them) into Markdown.

This is the image-input half of the images-to-md capability; ``readpdf`` is the
PDF half. It exists because a PDF's text layer is the cheap path and an image has
none: an image is either read by the vision model (the default assumption for a
zoombie agent) or by Tesseract when ``-Ocr`` asks for deterministic text.

The command never guesses text. Without ``-Ocr`` it records the images and their
sidecar and renders only the image links, leaving the reading to the model --
which is exactly the "vision is the reader" contract. With ``-Ocr`` it runs
Tesseract and renders the text under each image.

Like every other stage it is ASCII-path safe: the images are read through the
managed helpers (so a Cyrillic source folder is fine), and the Markdown is written
where the user confirmed.
"""

from __future__ import annotations

import hashlib
import os

from ..cli import Outcome
from ..lib import ocr, paths, process
from ..lib.errors import SetupRequiredError, ZoombieError


def destination_base(source: str, output: str | None) -> str:
    """Resolve the Markdown basename, defaulting from the source name."""
    if output:
        base = output
    else:
        name = os.path.basename(os.path.normpath(source))
        stem = paths.without_extension(name) if paths.extension_of(name) else name
        base = os.path.join(os.getcwd(), stem)
    base = paths.absolute(base)
    if paths.extension_of(base).lower() == ".md":
        base = paths.without_extension(base)
    return base


def collect_images(source: str) -> list[str]:
    """Every image under ``source`` (a file is returned as-is), in name order.

    A directory is scanned non-recursively: an images folder is flat in practice,
    and recursing could silently swallow an unrelated library. Sorting by name is
    what makes ``001 - ...`` ordering stable across runs.
    """
    if paths.is_file(source):
        return [source]
    if not paths.is_dir(source):
        raise ZoombieError(f"Input not found: {source}")
    found: list[str] = []
    for entry in paths.list_dir(source, files=True):
        if entry.name.lower().endswith(ocr.IMAGE_EXTENSIONS):
            found.append(entry.path)
    return sorted(found)


def _image_size(path: str) -> tuple[int, int]:
    """``(width, height)`` for an image; ``(0, 0)`` when it cannot be read.

    PIL is optional here (it ships with the OCR extras), so a missing Pillow
    degrades the recorded size instead of failing a run that otherwise worked.
    """
    width, height = (0, 0)
    if path.lower().endswith(".png"):
        # A PNG's IHDR is readable with no dependency at all.
        from ..lib.slides import png_size

        width, height = png_size(path)
        if width and height:
            return width, height
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001 - an unreadable size is not fatal
        return 0, 0


def _digest(path: str, limit: int = 1 << 20) -> str:
    """A short content digest, so a reader can spot a duplicate image."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha1(handle.read(limit)).hexdigest()[:16]
    except OSError:
        return ""


def _relative_link(base_dir: str, image_path: str) -> str:
    """A forward-slash relative link from the Markdown to an image."""
    relative = os.path.relpath(image_path, base_dir).replace("\\", "/")
    return relative


def run(args) -> Outcome:
    if not paths.exists(args.source):
        raise ZoombieError(f"Input not found: {args.source}")

    images = collect_images(args.source)
    if not images:
        raise ZoombieError(
            f"No images found in: {args.source} (looked for "
            f"{', '.join(ocr.IMAGE_EXTENSIONS)})."
        )

    base = destination_base(args.source, args.output)
    output_md = f"{base}.md"
    paths.assert_fits(output_md, "The readimages output path", slack=7)

    if paths.is_file(output_md) and not args.force:
        raise ZoombieError(f"Output exists (use -Force to overwrite): {output_md}")

    # The sidecar directory: explicit -ImageDir, else beside the document. It
    # holds manifest.json + README.md (placement metadata for a later summarize
    # pass and a human-readable table), NOT copies of the user's images.
    image_dir = paths.absolute(args.image_dir) if args.image_dir else f"{base}.images"
    paths.assert_fits(image_dir, "The readimages sidecar directory", slack=24)

    ocr_requested = bool(args.ocr)
    if ocr_requested:
        usable, detail = ocr.available()
        if not usable:
            raise SetupRequiredError(
                f"OCR requested but Tesseract is not available: {detail}. Install it "
                "(winget install UB-Mannheim.TesseractOCR), or drop -Ocr and let the "
                "vision model read the images."
            )

    if args.dry_run:
        return Outcome(
            ok=True,
            data={
                "dryRun": True,
                "output": output_md,
                "images": image_dir,
                "count": len(images),
                "ocr": ocr_requested,
                "files": [os.path.basename(path) for path in images],
            },
        )

    document_dir = os.path.dirname(output_md) or os.getcwd()
    rows: list[dict] = []
    blocks: list[str] = []
    ocr_used = False

    for index, image_path in enumerate(images, start=1):
        name = os.path.basename(image_path)
        width, height = _image_size(image_path)
        link = _relative_link(document_dir, image_path)
        rows.append({
            "file": name,
            "index": index,
            "width": width,
            "height": height,
            "anchor_text": name,
            "page_title": name,
            "digest": _digest(image_path),
            "bytes": paths.file_size(image_path),
        })

        title = f"## {name}"
        body: list[str] = [f"![{name}]({link})"]
        if ocr_requested:
            try:
                text = ocr.ocr_image(image_path, args.lang)
            except RuntimeError as exc:
                raise SetupRequiredError(f"OCR failed for {name}: {exc}") from exc
            if text:
                ocr_used = True
                body.append("")
                body.append(text)
        blocks.append(title + "\n\n" + "\n".join(body))

    markdown = "\n\n".join(blocks).rstrip() + "\n"

    paths.ensure_dir(document_dir)
    with open(paths.to_extended(output_md), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markdown)

    # Render the sidecar with the same manifest shape the other image producers
    # use, so summarize/postprocess can consume it without a special case.
    from ..lib.slides import write_sidecar

    paths.ensure_dir(image_dir)
    manifest = write_sidecar(image_dir, rows, args.source)

    process.log(
        f"readimages: {len(images)} image(s), ocr={'on' if ocr_requested else 'off'}"
        f" -> {output_md}",
        "step",
    )

    return Outcome(
        ok=True,
        data={
            "output": output_md,
            "artifacts": {
                "md": {"path": output_md, "size": paths.file_size(output_md)},
                "images": {
                    "path": image_dir,
                    "count": len(rows),
                    "manifest": manifest,
                },
            },
            "count": len(rows),
            "ocrUsed": ocr_used,
            # Explicit: with no -Ocr this run recorded images for a vision reader
            # and wrote NO text, rather than writing an empty document silently.
            "visionOnly": not ocr_requested,
            "asciiSafe": True,
        },
    )

"""``readpdf``: convert a PDF to Markdown, optionally OCR'ing scans.

Mirrors ``extract``'s contract, but the extractor is an in-process Python call
rather than a native executable.

The ASCII invariant still applies: PyMuPDF and Tesseract are native libraries
that misbehave on non-ASCII paths, so the source PDF is copied into the ASCII
work dir first, the converter runs entirely there, and the produced Markdown
(and any images) are copied back to the confirmed, possibly Cyrillic,
destination.
"""

from __future__ import annotations

import os

from ..cli import Outcome
from ..lib import env as env_mod, paths, pdf, process
from ..lib.errors import SetupRequiredError, ZoombieError


def destination_base(source: str, output: str | None) -> str:
    """Resolve the Markdown basename, defaulting from the source file name."""
    base = output or os.path.join(os.getcwd(), paths.without_extension(os.path.basename(source)))
    base = paths.absolute(base)
    if paths.extension_of(base).lower() == ".md":
        base = paths.without_extension(base)
    return base


def run(args) -> Outcome:
    environment = env_mod.resolve()

    if not paths.is_file(args.source):
        raise ZoombieError(f"Input not found: {args.source}")

    helper = environment.pdf_script
    if not helper or not paths.is_file(helper):
        raise SetupRequiredError(
            f"PDF helper not found: {helper}. Run the zoombie setup first."
        )

    base = destination_base(args.source, args.output)
    output_md = f"{base}.md"
    # '.images' is appended to the same base by the -Images branch.
    paths.assert_fits(output_md, "The readpdf output path", slack=7)

    if paths.is_file(output_md) and not args.force:
        raise ZoombieError(f"Output exists (use -Force to overwrite): {output_md}")

    images_dir = f"{base}.images" if args.images else None

    work_root = args.work_root or paths.env_path(paths.WORK_FOLDER)
    if not paths.is_ascii(work_root):
        raise ZoombieError(f"Work root must be ASCII: {work_root}")
    work = paths.new_ascii_dir(work_root)

    if args.dry_run:
        safe_input = os.path.join(work, "input.pdf")
        safe_markdown = os.path.join(work, "out.md")
        work_images = os.path.join(work, "images")
    else:
        copied = paths.copy_into_safe_work(args.source, work)
        safe_input = copied["input_path"]
        safe_markdown = os.path.join(work, "out.md")
        work_images = os.path.join(work, "images")

    process.log(f"readpdf -> {output_md}", "step")
    process.log(
        f"  work={work}  ocr={bool(args.ocr)}  pages={args.pages or 'all'}"
    )

    options = pdf.Options(
        input=safe_input,
        output=safe_markdown,
        ocr=args.ocr,
        images=work_images if args.images else None,
        pages=args.pages,
        lang=args.lang,
    )

    if args.dry_run:
        return Outcome(
            ok=True,
            data={"dryRun": True, "work": work, "output": output_md, "options": {
                "ocr": options.ocr, "pages": options.pages, "images": options.images,
            }},
        )

    try:
        result = pdf.convert(options)
    except (RuntimeError, ValueError, OSError) as exc:
        raise ZoombieError(f"readpdf failed: {exc}") from exc

    # Copy the Markdown back to the real (possibly non-ASCII) destination.
    output_dir = os.path.dirname(output_md)
    if output_dir:
        paths.ensure_dir(output_dir)
    paths.copy_file(result.output, output_md)

    artifacts: dict[str, dict] = {
        "md": {"path": output_md, "size": paths.file_size(output_md)}
    }

    if images_dir and paths.is_dir(work_images):
        if paths.exists(images_dir):
            paths.remove(images_dir, recursive=True)
        paths.move(work_images, images_dir)
        count = len(paths.list_dir(images_dir, files=True))
        artifacts["images"] = {"path": images_dir, "count": count}

    if not args.keep_work:
        paths.remove_quietly(work, recursive=True)

    return Outcome(
        ok=True,
        data={
            "output": output_md,
            "artifacts": artifacts,
            "pages": result.pages,
            "ocrUsed": result.ocr_used,
            "keptScannedPages": result.kept_scanned_pages,
            "asciiSafe": True,
        },
    )

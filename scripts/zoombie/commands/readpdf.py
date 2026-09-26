"""``readpdf``: convert a PDF to Markdown, optionally OCR'ing scans.

Mirrors ``extract``'s contract, but the extractor is an in-process Python call
rather than a native executable.

The ASCII invariant still applies: PyMuPDF and Tesseract are native libraries
that misbehave on non-ASCII paths, so the source PDF is copied into the ASCII
work dir first, the converter runs entirely there, and the produced Markdown
(and any images) are copied back to the confirmed, possibly Cyrillic,
destination.

Image extraction is no longer delegated to pymupdf4llm. ``-Images``/
``-ImagesOnly``/``-ImageDir`` run :func:`zoombie.lib.pdf.collect_images` over the
drawn images and write a sidecar with placement metadata (``manifest.json`` +
``README.md``) next to the PNGs, named ``001 - p01.png`` in reading order.
``-ImagesOnly`` skips Markdown entirely and therefore does NOT apply the
``<base>.md`` exists guard, so the flag is usable from a library workflow where
the Markdown was already generated (that guard is what blocked its reuse).
"""

from __future__ import annotations

import json
import os

from ..cli import Outcome
from ..item import paths as item_paths
from ..lib import env as env_mod, next as next_mod, ocr, paths, pdf, process, reading, scratch
from ..lib.errors import SetupRequiredError, ZoombieError


def destination_base(source: str, output: str | None) -> str:
    """Resolve the Markdown basename, defaulting from the source file name."""
    base = output or os.path.join(os.getcwd(), paths.without_extension(os.path.basename(source)))
    base = paths.absolute(base)
    if paths.extension_of(base).lower() == ".md":
        base = paths.without_extension(base)
    return base


def _owned_files(manifest_path: str) -> list[str] | None:
    """Image file names recorded by a previous run's sidecar manifest.

    Returns ``None`` when the manifest is missing or unreadable, which the caller
    reads as "this directory is not ours" -- an unreadable manifest cannot be
    trusted as an ownership list.
    """
    if not paths.is_file(manifest_path):
        return None
    try:
        with open(paths.to_extended(manifest_path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    images = payload.get("images")
    if not isinstance(images, list):
        return None
    names: list[str] = []
    for entry in images:
        if isinstance(entry, dict) and entry.get("file"):
            names.append(str(entry["file"]))
    return names


def _prepare_images_dir(images_dir: str, force: bool) -> int:
    """Make ``images_dir`` ready for this run WITHOUT ever deleting it wholesale.

    Chosen rule (replacing the old ``paths.remove(images_dir, recursive=True)``
    that destroyed an existing image directory without asking):

    * **missing** -> created, nothing pruned.
    * **contains our ``manifest.json``** -> the directory is our own previous
      output, and only the files that manifest lists are pruned. Stale files must
      not survive, because dropping a non-extractable image renumbers everything
      after it; but pruning per file means anything a user put there by hand
      survives. No prompt is needed: over our own output, re-running is the
      expected idempotent behaviour.
    * **no (readable) manifest** -> treated as foreign and REFUSED unless
      ``-Force`` is given, so a hand-curated directory is never wiped by accident.
      With ``-Force`` the run proceeds but still does not delete anything that is
      not ours: our own names are simply (re)written over.

    Returns the number of files pruned.
    """
    if not paths.is_dir(images_dir):
        return 0
    owned = _owned_files(os.path.join(images_dir, pdf.SIDECAR_MANIFEST))
    if owned is None:
        if not force:
            raise ZoombieError(
                f"Image directory exists and was not written by readpdf (no "
                f"{pdf.SIDECAR_MANIFEST}): {images_dir}. Pass -Force to write into it, "
                f"or pick a different -ImageDir. Nothing was deleted."
            )
        process.log(
            f"  {images_dir} is not readpdf's; -Force given, our files will be added "
            f"alongside what is already there (nothing deleted)"
        )
        return 0

    existing = {entry.name.lower() for entry in paths.list_dir(images_dir, files=True)}
    pruned = 0
    for name in owned:
        if name.lower() in existing:
            paths.remove_quietly(os.path.join(images_dir, name))
            pruned += 1
    return pruned


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
    # '.images' is appended to the same base by the default image branch.
    paths.assert_fits(output_md, "The readpdf output path", slack=7)

    # -ImageDir is an explicit directory; -ImagesOnly implies extraction too, so
    # that `readpdf -Source x.pdf -ImagesOnly` works with no second flag.
    want_images = bool(args.images or args.images_only or args.image_dir)
    # Vision escalation is an EXPLICIT opt-in: the user names the directory the
    # rendered scans should land in. It is per-page and only for pages with no
    # text layer, so a normal document is unaffected. Read via getattr because a
    # programmatic caller (and the existing unit tests) builds a Namespace with
    # only the original flags.
    vision_arg = getattr(args, "vision_dir", None)
    dpi = getattr(args, "dpi", 200) or 200
    vision_dir = paths.absolute(vision_arg) if vision_arg else None
    if vision_dir:
        paths.assert_fits(vision_dir, "The readpdf vision directory", slack=16)
    images_dir = None
    images_default_reason: str | None = None
    if want_images:
        # An explicit -ImageDir always wins -- that is the escape hatch.
        if args.image_dir:
            images_dir = paths.absolute(args.image_dir)
        else:
            parent = os.path.dirname(base)
            # Default to the VISIBLE item layout -- ``<item>/img`` -- because
            # readpdf is the item producer: the figures it extracts belong to the
            # summary written beside them, not to the PDF's own basename. That
            # default is only right INSIDE an item, though: for a standalone
            # conversion it would scatter an ``img`` beside an unrelated folder
            # that merely happens to be the PDF's parent. So the item layout is
            # used only when the parent actually IS an item, and the historical
            # ``<base>.images`` is the fallback -- reported, because a caller
            # wondering why the figures are not in ``img`` must read the reason.
            if item_paths.is_item(parent):
                images_dir = item_paths.image_dir(parent)
                images_default_reason = "the output folder is an item"
            else:
                images_dir = f"{base}.images"
                images_default_reason = (
                    "the output folder is not an item, so the historical "
                    "<base>.images was used instead of <item>/img"
                )
        # Slack covers the longest thing appended inside it: '<NNN> - pNN.png'.
        paths.assert_fits(images_dir, "The readpdf image directory", slack=16)

    # -ImagesOnly renders no Markdown, so an existing <base>.md is not in the way.
    if not args.images_only and paths.is_file(output_md) and not args.force:
        raise ZoombieError(f"Output exists (use -Force to overwrite): {output_md}")

    work_root = args.work_root or paths.env_path(paths.WORK_FOLDER)
    if not paths.is_ascii(work_root):
        raise ZoombieError(f"Work root must be ASCII: {work_root}")
    work = paths.new_ascii_dir(work_root)

    safe_markdown = os.path.join(work, "out.md")
    work_images = os.path.join(work, "images")
    work_vision = os.path.join(work, "vision")
    # Compressed reading copies (plan §12) are encoded in an ASCII work subdir and
    # copied back beside the rendered scans, in their own subdirectory.
    work_read_vision = os.path.join(work, "reading")
    vision_read_dir = reading.reading_dir(vision_dir) if vision_dir else None
    if args.dry_run:
        safe_input = os.path.join(work, "input.pdf")
    else:
        copied = paths.copy_into_safe_work(args.source, work)
        safe_input = copied["input_path"]

    if args.images_only:
        process.log(f"readpdf (images only) -> {images_dir}", "step")
    else:
        process.log(f"readpdf -> {output_md}", "step")
    process.log(
        f"  work={work}  ocr={bool(args.ocr)}  pages={args.pages or 'all'}"
    )

    # -Lang defaults to DERIVED: a sibling transcript's script decides (Cyrillic ->
    # eng+rus, Latin -> eng, none -> eng+rus). An explicit -Lang still wins. Read via
    # getattr because a programmatic caller may build a Namespace without it.
    language = getattr(args, "lang", None) or ocr.derive_lang(args.source)
    options = pdf.Options(
        input=safe_input,
        output=safe_markdown,
        ocr=args.ocr,
        images=work_images if want_images else None,
        pages=args.pages,
        lang=language,
        images_only=bool(args.images_only),
        images_dir=work_images if want_images else None,
        min_px=args.min_px,
        min_pt=args.min_pt,
        vision_dir=work_vision if vision_dir else None,
        dpi=dpi,
    )

    if args.dry_run:
        # -DryRun writes nothing, including to disk: the work dir created a few
        # lines up is removed here and reported (plan §10), so a dry run leaves no
        # scratch under the toolchain root.
        dry_scratch = scratch.report(work, kept=False)
        return Outcome(
            ok=True,
            data={
                "dryRun": True,
                "work": work,
                "output": output_md,
                "imagesOnly": bool(args.images_only),
                "images": images_dir,
                "imagesDefaultReason": images_default_reason,
                "options": {
                    "ocr": options.ocr, "pages": options.pages,
                    "images": options.images_dir, "minPx": options.min_px,
                    "minPt": options.min_pt,
                },
                # Nothing was rendered or written, so there is nothing to attach;
                # the recommended step is the same call without -DryRun.
                "next": next_mod.build(
                    "readpdf",
                    {"-Source": args.source, "-Output": base},
                    why=(
                        "dry run: nothing was written; re-run without -DryRun to "
                        "convert the document, then read any pages it reports as "
                        "having no text layer"
                    ),
                    attachable=[],
                ),
                "scratch": dry_scratch,
            },
        )

    try:
        result = pdf.convert(options)
    except (RuntimeError, ValueError, OSError) as exc:
        raise ZoombieError(f"readpdf failed: {exc}") from exc

    artifacts: dict[str, dict] = {}
    vision_reading: dict | None = None

    # Copy the Markdown back to the real (possibly non-ASCII) destination.
    if not args.images_only:
        output_dir = os.path.dirname(output_md)
        if output_dir:
            paths.ensure_dir(output_dir)
        paths.copy_file(result.output, output_md)
        artifacts["md"] = {"path": output_md, "size": paths.file_size(output_md)}

    if images_dir:
        # Prune only our own previous output (see _prepare_images_dir), then merge
        # the work dir in: copy_tree, not move, because shutil.move into an
        # EXISTING directory would nest the whole folder one level deeper.
        pruned = _prepare_images_dir(images_dir, args.force)
        if pruned:
            process.log(f"  pruned {pruned} image file(s) from a previous run")
        if paths.is_dir(work_images):
            paths.copy_tree(work_images, images_dir)
        artifacts["images"] = {
            "path": images_dir,
            "count": result.image_count,
            "manifest": result.images_manifest
            and os.path.join(images_dir, os.path.basename(result.images_manifest)),
            "skipped": len(result.images_skipped),
            # Why THIS directory was chosen: the item layout when the output
            # folder is an item, the historical ``<base>.images`` when it is not.
            "defaultReason": images_default_reason,
        }

    # Copy the rendered scans back. Only text-less pages were rendered, and only
    # when -Vision was given and OCR did not run, so this stays empty on a normal
    # text document.
    if vision_dir and result.vision_pages:
        paths.ensure_dir(vision_dir)
        if paths.is_dir(work_vision):
            paths.copy_tree(work_vision, vision_dir)
        artifacts["vision"] = {
            "path": vision_dir,
            "count": len(result.vision_pages),
            "pages": [dict(entry) for entry in result.vision_pages],
        }
        # A compressed reading copy per rendered page (plan §12), beside the PNG
        # scans in their own subdirectory. A rendered page has no OCR text (that is
        # why it was rendered), so the rule defaults to -q:v 3 for every page -- and
        # says so, rather than guessing an escalation.
        if not reading.disabled(args):
            vision_read_plan = reading.plan([
                {"file": entry["file"], "ocrText": None}
                for entry in result.vision_pages
            ])
            vision_read = reading.make_copies(
                vision_dir, work_read_vision, vision_read_plan
            )
            if vision_read.get("available") and vision_read_dir:
                keep = {item["file"] for item in vision_read["byFrame"].values()}
                reading.prune(vision_read_dir, keep)
                if paths.is_dir(work_read_vision):
                    paths.copy_tree(work_read_vision, vision_read_dir)
                vision_reading = vision_read

    # CLI-owned cleanup (plan §10): remove this run's scratch unless
    # -KeepScratch/-KeepWork asked to retain it. Every advertised path lives in the
    # confirmed ``-Vision``/``-ImageDir`` destination, which was COPIED to above,
    # so deleting the scratch cannot invalidate the attach list or data.next.
    # It runs after both copy-backs, which is the bounded read E defined.
    scratch_block = scratch.report(work, kept=scratch.keep_requested(args))

    # The rendered scans are the only thing readpdf produces that a vision reader
    # still has to LOOK at, so they are the attach list -- HARD-CAPPED (plan §9)
    # exactly as ``slides`` caps its frames. A text-first run renders nothing, so
    # its attach list is legitimately empty: the text is already in the Markdown.
    # ``path`` is the reading copy when one was written, and ``bytes`` is THAT file's
    # size, so ``budget`` reports what the agent is about to spend (plan §9/§12) -- a
    # JPEG, not a larger PNG. The PNG is still identified by ``file``.
    read_map = (vision_reading or {}).get("byFrame", {}) if vision_dir else {}
    vision_attachable = [
        {
            "page": entry["page"],
            "file": entry["file"],
            "path": (
                os.path.join(vision_read_dir, read_map[entry["file"]]["file"])
                if entry["file"] in read_map and vision_read_dir
                else os.path.join(vision_dir, entry["file"])
            ),
            "bytes": (
                read_map[entry["file"]]["bytes"] if entry["file"] in read_map
                else paths.file_size(os.path.join(vision_dir, entry["file"]))
                if paths.is_file(os.path.join(vision_dir, entry["file"])) else 0
            ),
        }
        for entry in result.vision_pages
    ] if vision_dir else []
    attach_cap = next_mod.cap_of(args)
    vision_selected = next_mod.select(vision_attachable, attach_cap)

    if vision_attachable:
        next_command = "postprocess"
        next_args = {"-Md": output_md} if not args.images_only else {"-Dir": os.path.dirname(output_md)}
        next_why = (
            "the pages with no text layer were rendered; read the attach list with "
            "your OWN vision, then run postprocess -Apply to place the figures and "
            "stamp the headings"
        )
    elif args.images_only:
        next_command = None
        next_args = None
        next_why = (
            "figures and the sidecar were written and no Markdown was requested; "
            "hand the manifest to zoombie-summarize's postprocess pass"
        )
    else:
        next_command = None
        next_args = None
        next_why = (
            "the text layer was read directly, so there is nothing left to look at; "
            "hand the Markdown to zoombie-summarize"
        )

    return Outcome(
        ok=True,
        data={
            "output": None if args.images_only else output_md,
            "imagesOnly": bool(args.images_only),
            "artifacts": artifacts,
            "pages": result.pages,
            "ocrUsed": result.ocr_used,
            "keptScannedPages": result.kept_scanned_pages,
            # Render-only escalation: the scans a vision reader still has to look
            # at. Present only when -Vision was given and pages had no text layer.
            "visionDir": result.vision_dir if result.vision_pages else None,
            # CAPPED (plan §9): ``visionPages`` is the agent-facing read list; the
            # full count is ``visionPageCount`` and the deferred pages are named in
            # ``data.next.overAttach``.
            "visionPages": [
                {**entry,
                 "path": (
                     os.path.join(vision_read_dir, read_map[entry["file"]]["file"])
                     if entry["file"] in read_map and vision_read_dir
                     else os.path.join(vision_dir, entry["file"])
                 )}
                for entry in vision_selected["attach"]
            ] if vision_dir else [],
            "visionPageCount": len(result.vision_pages),
            # The compressed reading copies of the rendered scans (plan §12). A
            # rendered page has no OCR text, so every page uses the default -q:v 3.
            "readingCopy": {
                "dir": vision_read_dir if vision_reading else None,
                "available": bool(vision_reading),
                "count": (vision_reading or {}).get("count", 0),
                "bytes": (vision_reading or {}).get("bytes", 0),
                "qualities": reading.summary(
                    reading.plan([{"file": e["file"], "ocrText": None}
                                  for e in result.vision_pages])
                ).get("qualities", {}) if vision_reading else {},
                "reason": (
                    "no rendered scans, so there is nothing to read inline"
                    if not result.vision_pages else
                    "disabled by -NoReadingCopy" if reading.disabled(args) else
                    f"a -q:v {reading.DEFAULT_QUALITY} JPEG per rendered page; a "
                    "rendered page has no OCR text to escalate from, so every page "
                    "uses the default quality"
                    if vision_reading else
                    "ffmpeg was not available, so the PNG scans were advertised"
                ),
            },
            "next": next_mod.build(
                next_command, next_args, why=next_why,
                attachable=vision_attachable, attach_cap=attach_cap,
            ),
            # The scratch lifecycle, reported rather than left to prose (plan §10).
            "scratch": scratch_block,
            "asciiSafe": True,
        },
    )

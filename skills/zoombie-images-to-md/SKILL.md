---
name: zoombie-images-to-md
cvrm-zoombie-version: 5.1.0
description: Turn a visual source into SOURCE material only - a PDF, a loose image, or a folder of images - as a faithful Markdown rendering plus extracted figures with placement metadata. A text PDF is read directly and costs nothing; only pages with NO text layer (scans) or images are read by the vision model or by Tesseract OCR. Use when the user wants the Markdown or the extracted text itself, e.g. extract text from a PDF, OCR a scan or a screenshot. It deliberately does not summarise. When the goal is a readable DOCUMENT from this PDF or these images, use zoombie-summarize instead - it is the front door and runs this stage for you. Always inspects the project first, proposes candidate output paths, and confirms before writing anything.
---

# Skill: zoombie-images-to-md

Thin wrapper over the deterministic CLI. The conversion — PyMuPDF4LLM for a
text PDF, Tesseract for a scan or an image, the ASCII-path isolation, the image
extraction and the placement sidecar — lives in the CLI. This skill only decides
*where* to write, *how* to read a page that has no text, and asks the user to
confirm.

**Do not summarise, renumber, or rename the output; `zoombie-summarize` does
that.** Your deliverable is a FAITHFUL rendering plus images with placement
metadata — not a summary, not an index, and no inlining of figures.

## Read the input the CHEAP way first

This is the rule that keeps the cost down, and it is not optional:

1. **A PDF with extractable text is read directly and costs nothing.** Do not
   send it through OCR or vision — ever. Run `readpdf` and if it reports no
   `data.keptScannedPages`, you are DONE. A document whose text layer exists must
   never be rasterized.
2. **Only a page with NO text layer is escalated.** `readpdf` reports exactly
   those pages as `data.keptScannedPages`. Only then ask which reader to use.
3. **Loose images have no text layer at all**, so they are the one case that goes
   to a reader by design: the vision model by default, `ocr: true` for
   deterministic text.

So: a normal text PDF → zero images, zero OCR. A scanned PDF → only its scanned
pages are read. An image folder → the images are read.

## Producer contract

| Input | Tool | Writes |
|-------|------|--------|
| PDF (text) | `readpdf` | `<base>.md`, and images with a sidecar when `images: true` |
| PDF (scanned pages) | `readpdf` with `ocr: true` or `vision: "<dir>"` | the text layer plus OCR text, or `<dir>\page-NNN.png` for you to read |
| image / folder | `readimages`, optionally `ocr: true` | `<base>.md` (image links, plus OCR text with `ocr: true`) and `<base>.images\` with the sidecar |

- The image sidecar is `manifest.json` + `README.md`, with the placement
  metadata that lets a summary re-insert each figure: `{source, count, images:
  [...]}`, each entry `{file, page, xref, width, height, bbox, anchor_text,
  page_title, digest, bytes}`.
- The JSON result `data` keys for `readpdf` are: `output`, `imagesOnly`,
  `artifacts`, `pages`, `ocrUsed`, `keptScannedPages`, `visionDir`,
  `visionPages`, `readingCopy`. Inside `artifacts`, `md` is `{path, size}` and
  `images` is `{path, count, manifest, skipped}`.
- `visionPages[i].path` is a **compressed reading copy** (a JPEG under
  `readings/`, plan §12), not the PNG: open the path you are given. `readingCopy`
  reports the directory and the quality routing. The PNG scan stays the figure.
- For `readimages`: `output`, `artifacts`, `count`, `ocrUsed`, `visionOnly`.
  `visionOnly` is `true` when no `ocr` was given — the images were recorded for
  YOU to read, and no text was written.

## Run this (MCP tool first)

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Call the `readpdf` (or `readimages`) MCP tool with the confirmed paths as JSON
arguments. A PDF whose text you want — the cheap path, no escalation:

```json
{"source": "<pdf>", "output": "<confirmed-basename>"}
```

A SCANNED PDF — pick exactly ONE escalation for the text-less pages:

```json
{"source": "<pdf>", "output": "<confirmed-basename>", "ocr": true}
{"source": "<pdf>", "output": "<confirmed-basename>", "vision": ".tmp\\vision"}
```

An image, or a folder of images (`ocr: true` for deterministic text instead of
your own reading):

```json
{"source": "<image-or-dir>", "output": "<confirmed-basename>", "lang": "eng"}
```

CLI fallback only:
`& $cli readpdf -Source "<pdf>" -Output "<confirmed-basename>" [-Ocr]`;
`& $cli readimages -Source "<image-or-dir>" -Output "<confirmed-basename>"`.

<!-- zoombie:include repo-fallback -->
<!-- /zoombie:include -->

<!-- zoombie:include json-contract -->
<!-- /zoombie:include -->

Read `data.output` for the `.md` path, `data.artifacts.images.path` for the image
directory and `data.artifacts.images.manifest` for the sidecar. Do **not**
hand-assemble a Python command.

## Procedure

1. **Inspect the project** — list the workspace root and look for existing doc
   locations (`docs/`, `pdf/`, `output/`, `data/`, `assets/`).

2. **Find the source.** Use the path the user gave; otherwise look for `.pdf`
   or image files. If several match, ask which.

3. **Propose 2–4 concrete output basenames** consistent with the layout and ask
   the user to confirm. Never assume a destination.

4. **Run the cheap tool first.** `readpdf` (or `readimages`) with no escalation.
   Read the result:
   - `keptScannedPages` **empty** → the document is done. Report it. Do not
     escalate.
   - `keptScannedPages` **non-empty** → ask the user: Tesseract (`ocr: true`) or
     the vision model (`vision: "<dir>"`)? Then re-run with exactly one of them,
     and read the rendered pages yourself when vision was chosen. Pass a `vision`
     directory under the workspace `/.tmp/` folder (e.g. `.tmp\vision`): the
     rendered pages are scratch you read once, not a deliverable, so they must not
     land in the workspace root or beside the source.
   - **images input** → run `readimages`; add `ocr: true` only if the user wants
     deterministic text rather than your own reading.

5. **Report** the Markdown path and size, the image directory, count and
   manifest, pages converted, `ocrUsed`, and any `keptScannedPages` — a skipped
   scan is always said out loud, never left silent.

6. **Hand off to `zoombie-summarize`**, naming the exact artifacts it will
   consume: `<base>.md` and the image directory with its `manifest.json`.

## Notes

- **Text PDFs need no OCR.** PyMuPDF4LLM reads headings, lists, tables and bold
  directly. `ocr: true` for a text PDF is a wasted run.
- **`ocr` and `vision` are the two ways to resolve a scan, and they are the only
  ones.** If Tesseract is not installed, `ocr: true` fails with a clear message
  (`winget install UB-Mannheim.TesseractOCR`); `vision` needs no system
  dependency, because you do the reading.
- **The image directory is handled non-destructively.** Our own previous output
  is pruned per file; a directory without our `manifest.json` is foreign and is
  refused unless `force: true` is given, and even then nothing is deleted.
- **If the result reports the PDF toolchain is missing**, tell the user to re-run
  the setup — fetch `https://raw.githubusercontent.com/carnivorum/zoombie/main/setup.md`
  and follow it. Do not install Python packages by hand.
- **Never overwrite without asking**; pass `force: true` only with consent.

<!-- zoombie:include scratch-note -->
<!-- /zoombie:include -->

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

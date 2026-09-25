---
name: zoombie-images-to-md
cvrm-zoombie-version: 4.8.0
description: Turn a visual source into SOURCE material - a PDF, a loose image, or a folder of images - as a faithful Markdown rendering plus extracted figures with placement metadata. A text PDF is read directly and costs nothing; only pages with NO text layer (scans) or images are read by the vision model or by Tesseract OCR. Use when the user wants to extract text from a PDF or an image, OCR a scan or a screenshot, or convert a document or a picture into Markdown. It deliberately does not summarise; zoombie-summarize does that. Always inspects the project first, proposes candidate output paths, and confirms before writing anything.
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
   to a reader by design: the vision model by default, `-Ocr` for deterministic
   text.

So: a normal text PDF → zero images, zero OCR. A scanned PDF → only its scanned
pages are read. An image folder → the images are read.

## Producer contract

| Input | Command | Writes |
|-------|---------|--------|
| PDF (text) | `readpdf` | `<base>.md`, and images with a sidecar when `-Images` |
| PDF (scanned pages) | `readpdf -Ocr` or `readpdf -Vision <dir>` | the text layer plus OCR text, or `<dir>\page-NNN.png` for you to read |
| image / folder | `readimages` or `readimages -Ocr` | `<base>.md` (image links, plus OCR text with `-Ocr`) and `<base>.images\` with the sidecar |

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
  `visionOnly` is `true` when no `-Ocr` was given — the images were recorded for
  YOU to read, and no text was written.

## Run this, nothing else

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Then call it — these are the only commands this skill needs:

```powershell
# a PDF whose text you want (no OCR, no vision - the cheap path)
& $cli readpdf -Source "<pdf>" -Output "<confirmed-basename>" [-Images] [-ImageDir "<dir>"] [-MinPx N] [-MinPt N] [-Pages "1-5,8"] [-Force]

# a PDF that is SCANNED: pick exactly one escalation for the text-less pages
& $cli readpdf -Source "<pdf>" -Output "<confirmed-basename>" -Ocr          # deterministic
& $cli readpdf -Source "<pdf>" -Output "<confirmed-basename>" -Vision ".tmp\vision"   # render for your eyes

# an image, or a folder of images
& $cli readimages -Source "<image-or-dir>" -Output "<confirmed-basename>" [-Ocr] [-Lang eng] [-ImageDir "<dir>"] [-Force]
```

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

4. **Run the cheap command first.** `readpdf` (or `readimages`) with no
   escalation. Read the result:
   - `keptScannedPages` **empty** → the document is done. Report it. Do not
     escalate.
   - `keptScannedPages` **non-empty** → ask the user: Tesseract (`-Ocr`) or the
     vision model (`-Vision <dir>`)? Then re-run with exactly one of them, and
     read the rendered pages yourself when vision was chosen. Pass a `-Vision`
     directory under the workspace `/.tmp/` folder (e.g. `-Vision ".tmp\vision"`):
     the rendered pages are scratch you read once, not a deliverable, so they must
     not land in the workspace root or beside the source.
   - **images input** → run `readimages`; add `-Ocr` only if the user wants
     deterministic text rather than your own reading.

5. **Report** the Markdown path and size, the image directory, count and
   manifest, pages converted, `ocrUsed`, and any `keptScannedPages` — a skipped
   scan is always said out loud, never left silent.

6. **Hand off to `zoombie-summarize`**, naming the exact artifacts it will
   consume: `<base>.md` and the image directory with its `manifest.json`.

## Notes

- **Text PDFs need no OCR.** PyMuPDF4LLM reads headings, lists, tables and bold
  directly. `-Ocr` for a text PDF is a wasted run.
- **`-Ocr` and `-Vision` are the two ways to resolve a scan, and they are the
  only ones.** If Tesseract is not installed, `-Ocr` fails with a clear message
  (`winget install UB-Mannheim.TesseractOCR`); `-Vision` needs no system
  dependency, because you do the reading.
- **The image directory is handled non-destructively.** Our own previous output
  is pruned per file; a directory without our `manifest.json` is foreign and is
  refused unless `-Force` is given, and even then nothing is deleted.
- **If the CLI reports the PDF toolchain is missing**, tell the user to re-run
  the setup — fetch `https://raw.githubusercontent.com/carnivorum/zoombie/main/setup.md`
  and follow it. Do not install Python packages by hand.
- **Never overwrite without asking**; pass `-Force` only with consent.

<!-- zoombie:include scratch-note -->
<!-- /zoombie:include -->

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

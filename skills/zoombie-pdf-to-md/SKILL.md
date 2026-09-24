---
name: zoombie-pdf-to-md
cvrm-zoombie-version: 4.6.0
description: Convert a PDF into SOURCE material - a faithful Markdown rendering plus extracted images with placement metadata - using PyMuPDF4LLM, with an optional Tesseract OCR fallback for scanned pages. Use when the user wants to extract text from a PDF, turn a PDF into Markdown, read a PDF document, or prepare a PDF for docs. It deliberately does not summarise or restructure the document; zoombie-summarize does that. Always inspects the project first, proposes candidate output paths, and confirms with the user before writing anything.
---

# Skill: zoombie-pdf-to-md

Thin wrapper. The conversion — PyMuPDF4LLM, the page-type detection, the OCR
fallback, the image extraction and the ASCII-path isolation — lives in the
deterministic CLI. This skill only decides *where* to write and asks the user to
confirm.

**Do not summarise, renumber, or rename the output; `zoombie-summarize` does
that.** Your deliverable is a FAITHFUL markdown rendering plus images with
placement metadata — not a summary, not an index, no filename convention, and no
inlining of figures into the document.

## Producer contract

In the confirmed output folder, one `readpdf` call writes:

| Artifact | Content |
|----------|---------|
| `<base>.md` | a faithful markdown rendering of the PDF |
| `<item>\.data\img\` (the default), or the `-ImageDir` target | extracted PNGs named `001 - p01.png`, in reading order |
| `manifest.json` inside the image dir | the placement metadata |
| `<README.md>` inside the image dir | the same data as a human-readable table |

- Images are collected from what is actually **drawn** on the page
  (`get_image_info()`), not from every XObject the page references, so the
  output is not "a pile of PNGs": each row records *where* the figure sits.
- `manifest.json` shape: `{source, count, images: [...]}`, where each image entry
  is `{file, page, xref, width, height, bbox, anchor_text, page_title, digest,
  bytes}`. `README.md` renders the same rows as a table.
- The JSON result `data` keys are: `output`, `imagesOnly`, `artifacts`, `pages`,
  `ocrUsed`, `keptScannedPages`, `asciiSafe`. Inside `artifacts`, `md` is
  `{path, size}` and `images` is `{path, count, manifest, skipped}`.

## Run this, nothing else

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Then call it — this is the only command this skill needs:

```powershell
& $cli readpdf -Source "<pdf>" -Output "<confirmed-basename>" [-Ocr] [-Images] [-ImagesOnly] [-ImageDir "<dir>"] [-MinPx N] [-MinPt N] [-Pages "1-5,8"] [-Force]
```

<!-- zoombie:include repo-fallback -->
<!-- /zoombie:include -->

<!-- zoombie:include json-contract -->
<!-- /zoombie:include -->

Read `data.output` for the `.md` path, `data.artifacts.images.path` for the
image directory and `data.artifacts.images.manifest` for the sidecar, and
`data.pages`, `data.ocrUsed`, `data.keptScannedPages` for what happened.
Do **not** hand-assemble a Python command.

## Images, precisely

| Flag | Effect |
|------|--------|
| `-Images` | extract images (and render the Markdown) |
| `-ImagesOnly` | extract images and the sidecar only; render no Markdown |
| `-ImageDir <dir>` | explicit image directory (default: the item layout `<item>\.data\img`); implies extraction |
| `-MinPx N` | drop images below this pixel size (default 64) |
| `-MinPt N` | drop images below this on-page size in points (default 30) |

- The default `-ImageDir` is the **item layout** (`<item>\.data\img`), so the
  figures land where the summary that will reference them expects them, rather
  than beside the PDF's own basename. Pass `-ImageDir` explicitly for a
  standalone conversion outside an item.
- `-ImagesOnly` is the library-friendly mode: it **skips markdown entirely** and
  therefore does **not** apply the `<base>.md` exists guard, so it can be run
  again after the document already exists. Its result carries
  `data.imagesOnly: true` and `data.output: null`.
- Both thresholds are required together: the pixel size separates real content
  from glyph/icon rasters, while the on-page box separates it from hairline
  rules that are wide but flat.
- **The image directory is handled non-destructively.** It is never deleted
  wholesale. A directory that already holds our `manifest.json` is our own
  previous output, and only the files that manifest lists are pruned (per file
  — anything the user added by hand survives). A directory with no readable
  manifest is treated as foreign and **refused** unless `-Force` is given, and
  even then nothing is deleted: our own names are simply written over.

## Procedure

1. **Inspect the project** — list the workspace root and look for existing doc
   locations (`docs/`, `pdf/`, `output/`, `data/`, `assets/`).

2. **Find the source PDF.** Use the path the user gave; otherwise look for
   `.pdf` files. If several match, ask which one.

3. **Propose 2–4 concrete output basenames** consistent with the layout (a
   `docs/` folder, a sibling `markdown/` folder, the same folder as the PDF, or a
   user-specified location). The CLI writes `<basename>.md`. Never assume a
   destination.

4. **Ask the user to confirm**, in one pass:
   - the destination basename,
   - whether the PDF is **scanned** and should be **OCR'd** (`-Ocr`; requires
     Tesseract to be installed),
   - whether to also **extract embedded images** (`-Images`, default image
     directory `<basename>.images\`),
   - whether to convert **only some pages** (`-Pages "1-5,8"`; default: all).
   Wait for an explicit answer.

5. **Run the CLI** with the confirmed values. Pass `-Force` only if the user
   agreed to overwrite an existing file or to write into an image directory that
   is not ours.

6. **Report** the Markdown path and size (`data.output`; it is `null` with
   `-ImagesOnly`, which still reports the rendering under `data.artifacts.md`),
   the image directory, count and manifest (`data.artifacts.images`), pages
   converted, whether OCR was used, and any scanned pages that were skipped
   because `-Ocr` was not given.

7. **Hand off to `zoombie-summarize`**, naming the exact artifacts it will
   consume: `<base>.md` (the faithful rendering) and the image directory with its
   `manifest.json` (the placement metadata). Write no summary of your own.

## Recommended pattern for a document that will reference its images

Render the markdown into the library folder first, then run `-ImagesOnly` with
an explicit `-ImageDir` so the images land beside the document that will
reference them:

```powershell
& $cli readpdf -Source "<pdf>" -Output "<library-folder>\source"
& $cli readpdf -Source "<pdf>" -Output "<library-folder>\source" -ImagesOnly -ImageDir "<library-folder>\img"
```

The second call renders no markdown and skips the `.md` guard, so re-running it
is safe, and `zoombie-summarize` then finds `img\manifest.json` next to the
document it is writing.

## Notes

- **Text PDFs need no OCR.** PyMuPDF4LLM reads headings, lists, tables and bold
  directly. Only pass `-Ocr` for scanned/image-only documents; the CLI reports
  `data.keptScannedPages` when it encounters a scan and OCR was off.
- **OCR is optional.** If Tesseract is not installed, `-Ocr` fails with a clear
  message. Install it with `winget install UB-Mannheim.TesseractOCR`, or run
  without `-Ocr`.
- **If the CLI reports the PDF toolchain is missing**, tell the user to re-run the
  setup — fetch `https://raw.githubusercontent.com/carnivorum/zoombie/main/setup.md`
  and follow it (manual fallback: `scripts\bootstrap.cmd` from a checkout). It
  installs the `pymupdf4llm`/`pytesseract` dependencies into the Python this repo
  already uses. Do not install Python packages by hand.
- **Never overwrite without asking**; pass `-Force` only with consent. Image
  drops are never silent: the run logs how many images were kept and how many
  were skipped, and `data.artifacts.images.skipped` carries the skipped count
  (the reasons are `glyph`, `xref0`, `duplicate`, `error` or `scanned-page`).

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

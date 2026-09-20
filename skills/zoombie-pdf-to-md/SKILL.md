---
name: zoombie-pdf-to-md
cvrm-zoombie-version: 4.0.0
description: Convert a PDF into Markdown using PyMuPDF4LLM, with an optional Tesseract OCR fallback for scanned or image-only pages. Use when the user wants to extract text from a PDF, turn a PDF into Markdown, read a PDF document, or prepare a PDF for docs. Always inspects the project first, proposes candidate output paths, and confirms with the user before writing anything.
---

# Skill: zoombie-pdf-to-md

Thin wrapper. The conversion — PyMuPDF4LLM, the page-type detection, the OCR
fallback, and the ASCII-path isolation — lives in the deterministic CLI. This
skill only decides *where* to write and asks the user to confirm.

## Run this, nothing else

Resolve the CLI first. It normally lives under the user profile, but on a
machine whose user name is not ASCII the toolchain is installed under
`%PUBLIC%` instead (the native libraries break on non-ASCII paths), so check
both:

```powershell
$cli = @(
    "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.cmd",
    "$env:PUBLIC\zoombie-env\bin\zoombie\zoombie.cmd"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cli) { throw "zoombie CLI not found. Run scripts\bootstrap.cmd first." }
```

Then call it — this is the only command this skill needs:

```powershell
& $cli readpdf -Source "<pdf>" -Output "<confirmed-basename>" [-Ocr] [-Images] [-Pages "1-5,8"] [-Force]
```

If the installed CLI is missing entirely, fall back to the repo copy:

```powershell
cd "<repo>\scripts"
python -m zoombie readpdf -Source "<pdf>" -Output "<confirmed-basename>"
```

The CLI prints one JSON line: `{ ok, action, data, error }`. Read `data.output`
for the `.md` path and `data.pages`, `data.ocrUsed`, `data.keptScannedPages` for
what happened. Do **not** hand-assemble a Python command.

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
   - whether to also **extract embedded images** (`-Images`, written beside the
     Markdown in `<basename>.images\`),
   - whether to convert **only some pages** (`-Pages "1-5,8"`; default: all).
   Wait for an explicit answer.

5. **Run the CLI** with the confirmed values. Pass `-Force` only if the user
   agreed to overwrite an existing file.

6. **Report** the Markdown path and size (`data.output`), plus pages converted,
   whether OCR was used, and any scanned pages that were skipped because `-Ocr`
   was not given.

## Notes

- **Text PDFs need no OCR.** PyMuPDF4LLM reads headings, lists, tables and bold
  directly. Only pass `-Ocr` for scanned/image-only documents; the CLI reports
  `data.keptScannedPages` when it encounters a scan and OCR was off.
- **OCR is optional.** If Tesseract is not installed, `-Ocr` fails with a clear
  message. Install it with `winget install UB-Mannheim.TesseractOCR`, or run
  without `-Ocr`.
- **If the CLI reports the PDF toolchain is missing**, tell the user to run
  `scripts\bootstrap.cmd` (it installs the `pymupdf4llm`/`pytesseract`
  dependencies into the Python this repo already uses). Do not install Python
  packages by hand.
- **Never overwrite without asking**; pass `-Force` only with consent.
- Shell: any. The launcher is a `.cmd` shim, so it works from `cmd.exe`,
  PowerShell or a plain process spawn without a wrapper.

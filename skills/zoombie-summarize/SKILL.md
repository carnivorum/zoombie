---
name: zoombie-summarize
cvrm-zoombie-version: 4.3.0
description: Turn SOURCE material into a readable 6-block summary.md inside a named library folder - a transcript, a PDF-derived Markdown, or arbitrary text. Use when the user wants a summary, notes, a digest, a write-up, or a readable document from something they already have. You write the prose yourself; the mechanical passes (anchors, heading timestamps from the SRT, the regenerated table of contents, link encoding, image re-insertion) are done by zoombie postprocess, so a re-run cannot drift.
---

# Skill: zoombie-summarize

This is the skill the other five hand off to. They produce SOURCE material
(`.txt`, `.srt`, `.md`, images); this one turns it into a DOCUMENT a human reads.

**You write the prose. You never hand-edit the mechanical parts.** Anchors,
heading timestamps, the block-4 contents, percent-encoding and image placement
are produced by the deterministic CLI. Editing them by hand is what makes a
second run drift, and the CLI's byte-idempotency guarantee exists precisely so
that re-running it is safe.

## The 6-block contract

A `summary.md` is exactly six `##` sections, numbered:

| # | Block | Who owns it |
|---|-------|-------------|
| 1 | Title | you (the heading and a line of framing) |
| 2 | Source / provenance | you, linking the source artifact |
| 3 | Short summary | you, optionally ending with a criticism sub-block |
| 4 | Table of contents | **the CLI** — regenerated every run |
| 5 | Related articles | you + the CLI's link repair |
| 6 | Full content, with `###` subheadings | you write the headings and prose; **the CLI adds the anchors and timestamps** |

**The passes address sections by NUMBER, not by title.** `## 4.` is matched as
`^##\s*4\.`, so a title in any language is safe and rephrasing it cannot break
the pass. What must survive is the numbered heading itself. The Russian titles
used by this repo's own tests are the convention:

```
# <title>

## 1. Титул
## 2. Источник
## 3. Краткое содержание
<the summary>

***Критика***
<one line per flaw>

## 4. Содержание
## 5. Связанные статьи
## 6. Полное содержание транскрипта
```

### The criticism sub-block (block 3)

Block 3 may end with a criticism sub-block: the flaws worth flagging in the source
itself — an unsupported claim, a stale figure, a one-sided framing.

**It is bold-italic text, NOT a heading.** That is deliberate and load-bearing:

- `assign_anchors` numbers **every** heading from `###` down to `######`. A `####`
  is no safer than a `###` — both are collected, both would consume an `s-N`.
- The block-4 index is narrowed to block 6 by offset, so that id would have
  nothing linking to it, and **`verify` fails a document with a dangling anchor**.
- Bold-italic is not a heading, so the numbering, timestamp and index passes
  never see it. Nothing about block 3 can disturb block 6.

Form: a lone `***Criticism***` (or `***Критика***`) line, one blank line before
and after, then one line per flaw.

It is **optional, and often absent.** Include it only when there is something
material to say. Do not pad it, do not moralise, and do not manufacture
objections to look rigorous — a criticism nobody can act on is noise. When the
source is sound, omit the sub-block entirely rather than writing "nothing to
criticise", because in a library document that line is pure noise.

### What `postprocess` does to that skeleton

1. Numbers every `###` heading in block 6 and prefixes `<a id="s-N"></a>`.
2. Reads the sibling SRT and stamps each block-6 heading with `HH:MM:SS — `.
3. Regenerates block 4 as an indented bullet index linking `#s-N`.
4. Percent-encodes link destinations and de-brackets link labels.
5. For a PDF-derived document, strips previously inserted images and re-inserts
   them from `img/manifest.json`.
6. Collapses blank runs and ensures exactly one trailing newline.

Each pass works on a *range* rebuilt from the document, never by appending, so
**a second `-Apply` on an unchanged file leaves it byte-identical.** That is the
acceptance criterion, and it is why re-running is always safe.

## Run this, nothing else

Resolve the CLI first. It normally lives under the user profile, but on a
machine whose user name is not ASCII the toolchain is installed under
`%PUBLIC%` instead, so check both:

```powershell
$cli = @(
    "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.cmd",
    "$env:PUBLIC\zoombie-env\bin\zoombie\zoombie.cmd"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cli) { throw "zoombie CLI not found. Run the zoombie bootstrap first: irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex  (or scripts\bootstrap.cmd from a checkout)." }
```

Then the mechanical pass — **dry run by default, and `-Apply` is what writes**:

```powershell
# report what would change, write nothing
& $cli postprocess -Md "<item>\summary.md" -Srt "<base>.srt" -ImageDir "<item>\img"

# write it (byte-identical on a second run)
& $cli postprocess -Md "<item>\summary.md" -Srt "<base>.srt" -ImageDir "<item>\img" -Apply

# a whole folder instead of one file (add -Recurse to walk subfolders)
& $cli postprocess -Dir "<library>" -Apply
```

Rebuild the library index from a deterministic scan of the item folders:

```powershell
& $cli index -Dir "<library>"            # dry run
& $cli index -Dir "<library>" -Apply     # writes <library>\README.md
```

Check a finished tree (exit code 1 on problems: dangling links, missing anchors,
broken image references):

```powershell
& $cli verify -Dir "<library>"
```

The CLI prints one JSON line: `{ ok, action, data, error }`. Read `data` for what
changed. Do **not** hand-assemble a Python command.

## Procedure

1. **Inspect the project** and find the library root and the source artifacts.
   If the work came from another skill, it named them: a `<base>.txt` (wording),
   a `<base>.srt` (timing) and a `<base>.source.json` (origin) from transcribe,
   or a `<base>.md` plus an image directory from `readpdf`.

2. **Confirm the destination.** Items live in a named library folder:
   `<number> - <DD.MM.YYYY> - <title>`. Propose 2-4 concrete options and wait for
   the user to choose. Never invent the number or silently pick a date.

3. **Write the prose** for the blocks you own: 1, 2, 3, and the block-6 headings
   with their content. Put the origin link in block 2 — for a video whose local
   file was deleted, `<base>.source.json` still carries the URL.

4. **Run `postprocess`** with the confirmed paths and `-Apply`.

5. **Verify.** Re-read the result and confirm the anchors resolve and block 4
   indexes what block 6 actually contains.

6. **Offer the reindex**, do not assume it: `index -Apply` rebuilds the library
   `README.md`. Then offer `verify` as the check.

## Rules

- **Never edit what the CLI owns.** If the anchors, timestamps, contents or image
  placement are wrong, fix the *input* or the *prose* and run `postprocess`
  again. Hand-patching block 4 or an `<a id>` is the one way to break the
  idempotency guarantee.
- **One document per item.** The item folder holds `summary.md` and, when there
  are images, `img/`. `img/README.md` and `img/manifest.json` are sidecars, not
  documents.
- **Dry run first** when the document already exists, so you can show the user
  the diff before it is written.
- **Do not re-transcribe or re-convert.** If the source material is missing, hand
  back to the skill that produces it rather than doing its job here.

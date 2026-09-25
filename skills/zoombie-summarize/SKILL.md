---
name: zoombie-summarize
cvrm-zoombie-version: 4.9.0
description: Turn SOURCE material into a readable 6-block summary.md inside an item folder - a transcript, a PDF-derived Markdown, or arbitrary text. Use when the user wants a summary, notes, a digest, a write-up, or a readable document from something they already have. You write the prose yourself; the mechanical passes (anchors, heading timestamps from the SRT, the regenerated table of contents, link encoding, image re-insertion) are done by zoombie postprocess, so a re-run cannot drift.
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
| 6 | **The source text, verbatim** (a copy, not a recap), with `###` subheadings | you write the headings and prose; **the CLI adds the anchors and timestamps** |

**Block 6 is a COPY of the source, not a summary of it** — the source text with
recognition artefacts cleaned out (filler, duplicated cues, machine noise), and
nothing condensed, paraphrased or re-ordered. A reader must be able to treat it as
the source.

**The heading must SAY so**, because nothing else in the file does: a task on
another machine read block 6 as a recap precisely because the heading gave no
signal. Name the copy, in the document's language — e.g. `## 6. Полный текст
источника (копия, очищенная от артефактов распознавания)`, or in English
`## 6. Source text - verbatim copy, cleaned of recognition artifacts`. `verify`
reports a heading that does not declare itself a copy.

**The passes address sections by NUMBER, not by title** (`## 4.` is matched as
`^##\s*4\.`), so a title in any language is safe. The numbering is what must
survive; the Russian titles below are the repo's test convention:

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
## 6. Полный текст источника (копия, очищенная от артефактов распознавания)
```

### The criticism sub-block (block 3)

Block 3 may end with a criticism sub-block: the flaws worth flagging in the source
— an unsupported claim, a stale figure, a one-sided framing.

**Bold-italic text, NOT a heading.** `assign_anchors` numbers every heading from
`###` down, so a `####` would consume an `s-N` that nothing links to and `verify`
fails on a dangling anchor. Form: a lone `***Criticism***` (or `***Критика***`)
line, blank line either side, one line per flaw.

It is **optional and often absent.** Include it only when there is something
material to say; when the source is sound, omit it.

### Slides (video sources only): ask first

For a **video** source, ask before anything expensive: (1) "should I also try to
extract slides?"; (2) if yes, "do you have exact timestamps?" — **yes** runs
`slides -Times "..."` (one frame each, the minimal set), **no** runs `slides` to
auto-detect and dedup. `postprocess -Apply` then embeds them. Frames land in
`.data/img/`; stage any scratch under the workspace `/.tmp/`.

`slides` **promotes, never drops on text**: every stable run keeps ≥1 frame, so an
image-only slide is never lost; `data.images.skippedReasons` names the structural
and duplicate drops. OCR runs **once per run** into **`<item>/.data/ocr.json`**
(`data.ocr.artifact`) — score usefulness from that text, not from char count.

**Block-6 convention:** one `###` per slide, with the **narration FIRST** under
each heading (`anchor_text`, the placement anchor) plus `timeSec` for the stamp.

**Read the kept frames with your OWN vision — OCR filters, it does not read.**
Open each path in `data.visionFrames` and write what the slide SAYS — title,
bullets, table — into the block-6 **body**. The heading stays led by the narration
(so the anchor resolves); the slide's content goes in the body.

### Block-6 subsections: on TOPIC CHANGE, not one per slide

A `###` is a **topic boundary in the narration**, not a slide boundary (one per
slide gave 124 headings for a 96-slide deck). **Block 4 indexes level-3 headings
only**; every heading still gets its `s-N` anchor and stamp, but `####`-and-deeper
stay out of the contents. Use `####` for a sub-point within a topic.

### What `postprocess` does to that skeleton

1. Numbers every `###`-and-deeper block-6 heading and prefixes `<a id="s-N"></a>`.
2. Stamps each heading `HH:MM:SS — ` from the sibling SRT. A manifest's `timeSec`
   is used **only when heading count == image count**; on a mismatch the ordinal
   association is refused (it would shift every stamp) and the SRT search is used.
3. Regenerates block 4 as an indented index linking `#s-N`, **level-3 only**.
4. Percent-encodes link destinations and de-brackets link labels.
5. Re-inserts images from `.data/img/manifest.json`. A figure with a degenerate
   `anchor_text` (a repeated whisper-loop phrase, or under a 3-word floor) is
   **not placed** — reported in `data.files[].imagesSkippedDetail`, counted in
   `skipped`. Do not hand-place it.
6. Collapses blank runs and ensures exactly one trailing newline.

Each pass works on a *range* rebuilt from the document, never by appending, so
**a second `-Apply` on an unchanged file leaves it byte-identical.** That is the
acceptance criterion, and it is why re-running is always safe.

## Run this, nothing else

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Then the mechanical pass — **dry run by default, and `-Apply` is what writes**:

```powershell
# report what would change, write nothing
& $cli postprocess -Md "<item>\summary.md"

# write it (byte-identical on a second run)
& $cli postprocess -Md "<item>\summary.md" -Apply

# a whole folder instead of one file (add -Recurse to walk subfolders)
& $cli postprocess -Dir "<library>" -Apply
```

`-Srt` and `-ImageDir` are optional: the default is the item layout, so the
sibling `.data/transcript.srt` and the `.data/img/` manifest are found for you.
Pass them only to override.

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

<!-- zoombie:include json-contract -->
<!-- /zoombie:include -->

<!-- zoombie:include scratch-note -->
<!-- /zoombie:include -->

Read `data` for what changed. Do **not** hand-assemble a Python command.

## Procedure

1. **Inspect the project** and find the library root and the source artifacts.
   If the work came from another skill, it named them: `.data/transcript.txt`
   (wording), `.data/transcript.srt` (timing) and `.data/source.json` (origin)
   from `transcribe`/`pipeline`, or a `<base>.md` plus an image directory from
   `readpdf`/`readimages`. For a video you may also have `<item>/.data/img` from
   `slides` — ask about that below before writing any prose.

2. **Confirm the destination - and MEASURE before you propose.** An item is a
   folder of ANY name; the name carries no meaning to the toolchain and is never
   parsed, so you never have to fit a format. Its shape is fixed:

   ```
   <item>/                  ← any name; the user's choice
       summary.md           ← the document you write
       <source media>       ← the video/audio/PDF, when kept
       .data/               ← derived material, never hand-edited
           img/             ← figures + manifest.json + README.md
           transcript.txt, transcript.srt, source.json, item.json
   ```

   `.data/` is created by `transcribe`/`pipeline` the moment `-Output` names the
   item, so a folder that has only been transcribed is already a recognised item.
   You do not create `.data/`; you write `summary.md` and run `postprocess`.

   Read what the workspace already does instead of imposing a convention:

   ```powershell
   # what exists here, what number is next, and which naming the folder uses
   & $cli items -Root "<target>" -Title "<the item's title>"
   ```

   `items` reports the naming convention it MEASURED in that directory, with a
   confidence and a sample count, and `-Title` renders concrete name proposals
   from it. Follow the directory's own convention. With no evidence at all, the
   recommendation is `<DD.MM.YYYY> - <title>`; say that is the default. Then
   present the proposals and wait for the user to choose.

   **Use the number you were given.** Copy `nextNumber` from the scan; never
   invent a number, write `NN`, or silently pick a date.

3. **Write the prose** for the blocks you own: 1, 2, 3, and the block-6 headings
   with their content. Put the origin link in block 2 — for a video whose local
   file was deleted, `<base>.source.json` still carries the URL (its `url` is
   `null` with a `urlReason` when the only input was a deleted scratch file: say
   the origin was not durable rather than inventing a link).
   Block-6 subsections follow **topic change, not slides** — see above.

4. **Run `postprocess`** with the confirmed paths and `-Apply`.

5. **Verify.** Re-read the result and confirm the anchors resolve and block 4
   indexes what block 6 actually contains.

6. **Offer the reindex**, do not assume it: `index -Apply` rebuilds the library
   `README.md`. Then offer `verify` as the check.

## Rules

- **Never edit what the CLI owns.** If anchors, timestamps, contents or image
  placement are wrong, fix the *input* or the *prose* and re-run `postprocess`.
  Hand-patching block 4 or an `<a id>` breaks the idempotency guarantee.
- **One document per item.** `summary.md`, the kept media, and `.data/`.
  Everything under `.data/` is a sidecar or derived material — never hand-edit it.
- **The name is the user's.** Do not rename a folder to satisfy a convention, and
  do not read meaning from one: the number, date and title live in
  `.data/item.json`.
- **Dry run first** when the document exists, so you can show the diff first.
- **Do not re-transcribe or re-convert.** If the source material is missing, hand
  back to the skill that produces it.

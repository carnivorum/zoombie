---
name: zoombie-summarize
cvrm-zoombie-version: 6.1.0
description: Produce a readable summary.md from any source - a video or audio recording, a URL, a PDF, or images - the FRONT DOOR for a document. Use when the user asks to summarize, make notes, a digest, a write-up, or a readable document from a recording or a file. One guided flow walks you from the source to the finished document - you supply the name, the title, the short summary and the topic headings, and the backend produces the transcript, splits it into the verbatim source copy, archives any existing summary, and verifies the result. Never hand-assemble ffmpeg, whisper or yt-dlp commands.
---

# Skill: zoombie-summarize

The **front door** for a document, and the ONLY skill. It drives a stepwise
`summarize` MCP tool: you call one step, it returns `data.next` naming the next
step, and so on until `summary.md` exists and is verified.

**You supply choices and prose; the tool assembles the document.** You never
retype the source text and never hand-edit the mechanical parts (anchors,
timestamps, the block-4 contents, link encoding, image placement).

## The document

A `summary.md` is exactly six `##` sections, numbered; the passes address them by
NUMBER, so the titles may be in any language:

| # | Block | Who owns it |
|---|-------|-------------|
| 1 | Title | you (the H1) |
| 2 | Source / provenance | the tool (the media or origin link) |
| 3 | Short summary | you, optionally ending with a `***Criticism***` sub-block |
| 4 | Table of contents | **the tool** |
| 5 | Related articles | you (may be empty) |
| 6 | The source text, verbatim | you name the headings; **the tool splits the transcript and adds anchors + timestamps** |

Block 6 is a COPY of the source, not a recap; its heading must say so (the
`## 6. ...` line the tool writes already does). The tool writes and runs
`postprocess` for you, so the numbering, block 4 and the figures are correct.

## The flow

Call the `summarize` MCP tool once per step and follow `data.next`:

| Step | Call | You do |
|------|------|--------|
| source | `summarize {source: "<url-or-path>"}` | for an EXISTING local file, **ask how to place the media** (below); the tool transcribes/renders into a run scratch and proposes a name |
| name | `summarize {step:"name", run, name, media}` | **ask the user** the name, carry the media answer (`media`, plus `confirm_move: true` only for a user-approved `move`); if the target already holds a summary, **ask whether to archive or overwrite** and pass `archive` or `overwrite` |
| slides | `summarize {step:"slides", run, slides, times}` | **ask the user** the slides question; then review the proposed frames and keep/drop |
| slides (prune) | `summarize {step:"slides", run, slides:"true", keep\|drop}` | **re-run** with the kept or dropped frames; `slides:"true"` is required - without it the selection is **ignored** |
| prose | `summarize {step:"prose", run, title, summary_text, criticism, sections}` | write the title, the short summary, an optional criticism, and the topic headings |
| verify | `summarize {step:"verify", run}` | nothing; the tool gates the tree and deletes the run scratch |

`source` names a URL or a file. For a source that is ALREADY INSIDE the
workspace (see the destination rule below) there is no name question: the
destination is the source's own folder, so go straight to `slides`.

## The questions, asked SEPARATELY

Ask **one** question, let the user answer, then ask the next. Never present a
combined multi-way question.

1. **How the media is placed** (an EXISTING local file only). When the source is a
   file that already exists -- not a URL -- the tool sets
   `data.media.choiceRequired`. Ask whether to **copy** the file into the item,
   **move** it there (the original is removed), or keep **no** copy. Carry the
   answer to `step:"name"` as `media`. The default is `keep` when the file already
   sits in the item, else `copy`; do not ask this for a URL or an in-place source.
   `move` **RELOCATES the user's own file** (the source is deleted, with no
   snapshot), so it is refused unless you also pass `confirm_move: true` **and**
   the user explicitly agreed to the move. Prefer `copy`, which leaves the original
   untouched. The source step's `data.next.args` already carries a `-Media` entry
   with the accepted handles -- do not drop it.
2. **The name.** The tool returns `data.proposed`. Offer it as the default and
   let the user accept it or supply their own; a name the user TYPES wins. Pass
   the answer as `name`.
3. **Slides** (video only). Ask whether to extract slide frames; if the user has
   exact timestamps, pass them as `times`. Answer with `slides: "true"` or `"false"`.

   Auto-detect PROPOSES frames and drops non-sequential repeats (`-GlobalDedup`),
   but a talking-head video still yields webcam frames. The result lists them under
   `data.slides.frames` with ids (`fNNN`) and timestamps; `data.next` then points
   back at `step:"slides"`. **Look at the frames and drop the faces**: re-run with
   `slides: "true"` **and** `keep: "f001,f003,..."` or `drop: "f002,..."`, naming ids
   or timestamps, never a path. Then call `step:"prose"`. Skipping this leaves
   presenters' faces in the document.

   `slides:"true"` is not optional on a prune, even though a `keep`/`drop` implies
   it and the tool will infer it. Then **check `data.slides.selection.applied`**: it
   must be `true`, and `droppedIds` must name the frames you dropped. `applied:false`
   means the selection was **not honoured** and the full frame set is about to be
   published - fix the call rather than continuing to `prose`.

4. **Overwrite** (only when the target already holds a `summary.md`). The `name`
   step **refuses** rather than acting, and `data.existing` names the document at
   risk, its figure count, and the media. Ask the user, who may back down and
   archive by hand. Pass `archive: true` to move the old document **and its
   figures** into a `summary_<timestamp>/` folder first, or `overwrite: true` for a
   clean rewrite with no backup. `data.overwrite.mode` echoes what was done. The
   source media is never removed either way.

## Where the output goes

The tool decides, and reports it as `data.destination` / `data.itemDir`:

- a source **inside the workspace** -> `summary.md` lands LITERALLY beside the
  media, in the source's own folder, whose name is unchanged;
- a URL, or a file **outside** the workspace -> `<workspace>/_unsorted/summaries/<name>/`.

A finished item holds only `summary.md`, the media (per the placement answer), and
a visible **`img/`** folder holding exactly the figures the document inlines.
Everything else (the transcript, the `.srt`, the origin sidecar, the OCR report,
the manifest, reading copies) is throwaway in a run scratch dir and is deleted at
`verify`. When the user chose **no** copy, block 2 names the source where it lies
rather than linking a file the item does not hold.

## Never destroy an existing summary

If the destination already holds a `summary.md`, the `name` step does nothing and
**refuses**: the folder is reused, not copied to a `(2)` sibling, and the error
names the document and figure count at risk. Nothing is touched until the user
chooses, so there is room to back down and archive by hand.

- `archive: true` moves the superseded document AND the figures it inlines into
  `summary_<yyyyMMdd_HHmm>/`, keeping every relative link valid. The path is
  reported in `data.archived`; **tell the user where their work went**.
- `overwrite: true` replaces the document and rebuilds `img/`, with no backup.

Only `summary.md` and `img/` are ever replaced. The **media is never removed** -
for an in-place source the item folder *is* the source folder, so the recording the
user owns survives a rewrite untouched.

## Run this (MCP tool first)

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Call the `summarize` MCP tool with the step's JSON arguments:

```json
{"source": "<url-or-path>"}
{"step": "name", "run": "<run>", "name": "<confirmed-name>", "media": "copy"}
{"step": "name", "run": "<run>", "name": "<name>", "media": "move", "confirm_move": true}
{"step": "name", "run": "<run>", "name": "<name>", "archive": true}
{"step": "name", "run": "<run>", "name": "<name>", "overwrite": true}
{"step": "slides", "run": "<run>", "slides": "true", "times": "00:01:00,00:05:30"}
{"step": "slides", "run": "<run>", "slides": "true", "drop": "f001,f003"}
{"step": "prose", "run": "<run>", "title": "<title>", "summary_text": "<short summary>",
 "sections": "[{\"heading\": \"Вступление\", \"at\": \"first words of the section\"}]"}
{"step": "verify", "run": "<run>"}
```

The first `name` variant is the safe one; the second (`move`) applies only after the
user explicitly agreed, because it deletes the original. The third and fourth apply
only when the target already holds a summary: `archive` keeps a full copy, `overwrite`
does not.

CLI fallback only:
`& $cli summarize -Source "<url-or-path>"`, then `-Step name -Run "<run>" -Name "..."`, etc.

<!-- zoombie:include repo-fallback -->
<!-- /zoombie:include -->

<!-- zoombie:include json-contract -->
<!-- /zoombie:include -->

Read `data.next` after every call. Do **not** hand-assemble a whisper, ffmpeg or
yt-dlp command, and do not write `summary.md` yourself.

## Procedure

1. **Inspect the project** once (workspace layout, where media already lives).
2. **Start** with `summarize {source}`. Read `data.proposed`, `data.internal`,
   `data.destination` and `data.media`. If `data.media.choiceRequired` is true,
   ask that question first (copy / move / no copy).
3. **Ask the name**, separately, then call `step:"name"` with `-Media` when the
   source was an existing local file.
4. **Ask the slides question**, separately, then call `step:"slides"`.
5. **Write the prose.** Read the source text if you need to (the run scratch has
   it), then give the tool the title, the short summary, an optional criticism,
   and the section headings — each with an `at` phrase quoted from the transcript
   at the point that topic starts. The tool splits block 6 there, so you never
   paste the body.
6. **Call `step:"verify"`.** On success the run scratch is removed. Report the
   `summary.md` path, the `img/` figures, and any `data.archived` path.

## Rules

- **Never edit what the tool owns.** If an anchor, a timestamp or a figure is
  wrong, fix the input (the section heading or its `at`) and re-run.
- **One document per item.** `summary.md`, the kept media, `img/`.
- **Never overwrite silently.** An existing summary is archived and reported;
  tell the user.
- **Produce the source once.** Re-running a step is safe; do not re-do the
  source material by hand.

<!-- zoombie:include scratch-note -->
<!-- /zoombie:include -->

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

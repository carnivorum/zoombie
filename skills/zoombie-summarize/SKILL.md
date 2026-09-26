---
name: zoombie-summarize
cvrm-zoombie-version: 6.0.0
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
| source | `summarize {source: "<url-or-path>"}` | nothing; the tool downloads/transcribes/renders into a run scratch and proposes a name |
| name | `summarize {step:"name", run, name}` | **ask the user** the name (below) |
| slides | `summarize {step:"slides", run, slides, times}` | **ask the user** the slides question (below) |
| prose | `summarize {step:"prose", run, title, summary_text, criticism, sections}` | write the title, the short summary, an optional criticism, and the topic headings |
| verify | `summarize {step:"verify", run}` | nothing; the tool gates the tree and deletes the run scratch |

`source` names a URL or a file. For a source that is ALREADY INSIDE the
workspace (see the destination rule below) there is no name question: the
destination is the source's own folder, so go straight to `slides`.

## The two questions, asked SEPARATELY

Ask **one** question, let the user answer, then ask the next. Never present a
combined four-way question.

1. **The name.** The tool returns `data.proposed`. Offer it as the default and
   let the user accept it or supply their own; a name the user TYPES wins. Pass
   the answer as `name`.
2. **Slides** (video only, and only when a `###`-per-topic copy is wanted for a
   slide deck). Ask whether to extract slide frames; if the user has exact
   timestamps, pass them as `times`. Answer with `slides: "true"` or `"false"`.

## Where the output goes

The tool decides, and reports it as `data.destination` / `data.itemDir`:

- a source **inside the workspace** -> `summary.md` lands LITERALLY beside the
  media, in the source's own folder, whose name is unchanged;
- a URL, or a file **outside** the workspace -> `<workspace>/_unsorted/summaries/<name>/`.

A finished item holds only `summary.md`, the kept media, and a visible **`img/`**
folder holding exactly the figures the document inlines. Everything else (the
transcript, the `.srt`, the origin sidecar, the OCR report, the manifest,
reading copies) is throwaway in a run scratch dir and is deleted at `verify`.

## Never destroy an existing summary

If the destination already holds a `summary.md`, the tool RENAMES it to
`summary_<yyyyMMdd_HHmm>.md` (its last-edit time) and reports it in
`data.archived`. **Tell the user where the previous document went** - do not
silently continue. Because a long instruction ("summarize X and rewrite the
criticism as ...") is a deliberate second pass, confirm with the user before
overwriting.

## Run this (MCP tool first)

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Call the `summarize` MCP tool with the step's JSON arguments:

```json
{"source": "<url-or-path>"}
{"step": "name", "run": "<run>", "name": "<confirmed-name>"}
{"step": "slides", "run": "<run>", "slides": "true", "times": "00:01:00,00:05:30"}
{"step": "prose", "run": "<run>", "title": "<title>", "summary_text": "<short summary>",
 "sections": "[{\"heading\": \"Вступление\", \"at\": \"first words of the section\"}]"}
{"step": "verify", "run": "<run>"}
```

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
2. **Start** with `summarize {source}`. Read `data.proposed`, `data.internal` and
   `data.destination`. If `internal` is true, skip to step 4 (slides).
3. **Ask the name**, separately, then call `step:"name"`.
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

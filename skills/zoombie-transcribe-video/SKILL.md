---
name: zoombie-transcribe-video
cvrm-zoombie-version: 5.1.0
description: End-to-end pipeline that turns a video file or a video URL into SOURCE material only - a TXT transcript, an SRT subtitle track and a .source.json origin sidecar - by chaining the download, extract and transcribe stages in one call. Use when the user wants the transcript or subtitles itself, e.g. transcribe this video, transcribe this link. It deliberately writes no summary and no readable document. When the goal is a readable DOCUMENT from this recording, use zoombie-summarize instead - it is the front door and runs this stage (the same pipeline) for you. Collects every output-path confirmation up front in one pass.
---

# Skill: zoombie-transcribe-video

Thin wrapper over the deterministic pipeline. The whole chain — optional
download, audio extraction into an ASCII scratch dir, whisper.cpp, and copying
artifacts back — is one CLI call. This skill only collects confirmations.

**Your deliverable is source material, not a document. Do not create or edit any
`.md` file, and do not summarise — the `zoombie-summarize` skill owns that.**

## Producer contract

`output` names the **item folder**: the transcripts land in its `.data/`
subdirectory, and the retained media at the item root. That is the documented item
layout, so nothing derived sits loose beside `summary.md`.

In the confirmed item folder, one `pipeline` call writes:

| Artifact | Authoritative for |
|----------|-------------------|
| `.data/transcript.txt` | the transcript WORDING |
| `.data/transcript.srt` | the TIMING, and for spotting machine noise |
| `.data/source.json` | the ORIGIN of the video, including the source URL |
| `<source media>` (item root) | the retained download, when one was kept |

`output` names the item folder, so **`<item>/.data/` is created at that point** --
before the transcription even runs. A folder that has only been transcribed is
therefore already a recognised item, which is what keeps the documented
"transcribe → then summarize" order true (see `zoombie.item.paths.is_item`).

- `.txt` and `.srt` come from ONE whisper decode. The SRT is never a separate
  pass and is never a yt-dlp subtitle download: yt-dlp is never asked for
  subtitles.
- whisper always runs with `-nt` (no timestamps in the `.txt`). That is exactly
  why the `.srt` is the ONLY timing source in the output set. Do not drop it.
- The `.srt` is emitted **by default**. `no_srt: true` is the opt-out and it
  destroys the timings.
- `.data/source.json` keys: `kind`, `url`, `title`, `id`, `durationSec`,
  `language`, `model`, `backend`, `deviceUsed`, `deviceVerified`,
  `realtimeFactor`, `toolchainVersion`, `createdAt`, `sourceKept`.
- **The origin survives even when the media is not kept.** For a URL the pipeline
  captures the video's identity from yt-dlp in the SAME invocation as the
  download, via `--print after_move:` (id, title, webpage URL, duration), and
  persists it to the sidecar, so the summarize pass has an origin link whether or
  not the file still exists. `kind` is `video` for a URL and `audio` for a local
  file. `sourceKept`/`sourceFile` record what actually happened: the pipeline
  **keeps the media at the item root by default**, so `sourceKept` is normally
  `true`; it is `false` only when the copy could not be made (an over-budget name).
- A missing metadata line degrades the sidecar (a warning, with `url` falling
  back to the given `source`) rather than failing a run whose media downloaded
  fine.

## Run this (MCP tool first)

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Call the `pipeline` MCP tool with the confirmed paths as JSON arguments:

```json
{"source": "<url-or-file>", "output": "<confirmed-item-folder>", "download_dir": "<dir>", "language": "auto"}
```

CLI fallback only:
`& $cli pipeline -Source "<url-or-file>" -Output "<confirmed-item-folder>"`.

There is no `format` argument here: the pipeline's audio is always a 16 kHz mono
WAV, because that is the only thing whisper.cpp consumes. `format` belongs to the
`download` and `extract` tools.

<!-- zoombie:include repo-fallback -->
<!-- /zoombie:include -->

<!-- zoombie:include json-contract -->
<!-- /zoombie:include -->

Read `data.artifacts.txt.path`, `data.artifacts.srt.path` and
`data.artifacts.sidecar.path` (each with its own `size`), plus `data.outputBase`
and `data.itemDir`. `data.artifacts.srt` is `null` when `no_srt` suppressed it.

## How the Cyrillic-path bug is handled

The pipeline extracts audio directly into an ASCII scratch folder and runs
whisper there, then copies the transcript back to the confirmed destination. A
Cyrillic destination is therefore safe, and a Cyrillic *source* path is safe
too because the audio is written to ASCII before whisper sees it.

A Cyrillic **argument** is a separate Windows problem, and it affects the CLI
**fallback** only — the MCP tools pass arguments in process, so no code page is
involved. For a CLI call, when quoting the value inline is unreliable, write it to
a UTF-8 file under the workspace `/.tmp/` folder and pass `@<file>` in its place —
for example `-Output "@.tmp\itemname.txt"`. The CLI reads the value from the file,
so the code page is not involved. This works for `-Source`, `-Output` and
`-DownloadDir`. Stage that file under `/.tmp/` and delete it when done, never in
`C:\Temp` or the workspace root.

## Procedure

1. **Inspect the project structure once** at the start (workspace layout,
   existing media/output folders, naming conventions).

2. **Determine the source.**
   - **URL** (YouTube, RuTube, Vimeo, Twitch, …) → the pipeline downloads first.
   - **Local file path** → the pipeline skips the download stage.

3. **Collect ALL confirmations up front in one consolidated pass** — do not ask
   twice later:
   - the source (URL or local path),
   - where to put the downloaded video (only for a URL),
   - the **item folder** (`output`); the transcripts go into its `.data/`, so
     say that plainly — `.data/` is hidden and a user looking for `transcript.txt`
     at the item root will not see it,
   - **whether to keep the downloaded media**; the pipeline **keeps it by
     default** at the item root, and the `.source.json` sidecar records its
     origin (`sourceKept`/`sourceFile`) either way.
   Present 2–4 concrete candidate paths for each. Wait for the answers before
   running anything.

4. **Run the `pipeline` tool** (CLI fallback only if it is unavailable). Pass the
   confirmed `source` and `output` (add `download_dir` for a URL). Do **not** opt
   into subtitles: the legacy `-Srt` alias is a parsed no-op, because the `.srt`
   is already produced. Pass `no_srt: true` only if the user explicitly accepts
   losing the only timing source.

5. **Report the full chain**, each item with its path and size: source, the
   retained media at the item root (or a note that it was not kept), and the
   artifacts (`.data/transcript.txt`, `.data/transcript.srt`,
   `.data/source.json`) — plus `data.deviceUsed`, `data.deviceVerified`,
   `data.realtimeFactor` and `data.itemDir`.

6. **Hand off to `zoombie-summarize`**, naming the exact artifacts it will
   consume: `.data/transcript.txt` (the wording), `.data/transcript.srt` (the
   timing) and `.data/source.json` (the origin — note that `sourceKept` records
   whether the media was retained). Write no `.md` file yourself.
   Mention that `zoombie-summarize` will ask the user whether to also extract
   **slides** from the video (via `zoombie slides`, with exact timestamps if the
   user has them) — so decide that with the summarize step, not here.

## Notes

- `zoombie-download-video`, `zoombie-extract-audio`, and
  `zoombie-transcribe-audio` document the individual stages; the `pipeline`
  tool runs them in sequence so you do not need to call them separately.
- **`force: true` also reaches the download stage.** Without it a re-run would
  reuse a cached download while still reporting a fresh transcription of the OLD
  media; with it yt-dlp overwrites, and the `<base>.txt` overwrite guard is
  lifted too. Never pass it without the user's consent.
- **A CPU run on a GPU machine is a failure, not a warning.** When a usable GPU
  backend is configured, the pipeline refuses to return a CPU transcript:
  `ok:false` with an `error` naming the reason (usually an incomplete CUDA
  runtime, e.g. a missing `cublas64_*.dll`), and `data.logPath` points at the
  preserved whisper log. Use `no_gpu: true` only if the user explicitly wants
  the CPU. A machine with no GPU is unaffected and transcribes on the CPU
  normally.
- Read `data.deviceUsed` and `data.realtimeFactor` and report them. On a GPU
  machine `deviceUsed` must be `cuda` (or `vulkan`) and `realtimeFactor` well
  below `1.0`. `data.deviceVerified` is the positive proof the GPU was used;
  `backendInitialised` only means the backend loaded. If `deviceVerified` is
  `false`, GPU use is unproven: report that plainly, and pass `strict_gpu: true`
  on a re-run to make it a hard failure. `gpuAttemptWallMs` reports time wasted
  by an abandoned GPU attempt before a CPU retry.
- Never delete a user-supplied local source file. Never overwrite without
  asking.

<!-- zoombie:include scratch-note -->
<!-- /zoombie:include -->

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

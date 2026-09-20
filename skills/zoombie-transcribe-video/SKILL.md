---
name: zoombie-transcribe-video
cvrm-zoombie-version: 4.3.0
description: End-to-end pipeline that turns a video file or a video URL into SOURCE material - a TXT transcript, an SRT subtitle track and a .source.json origin sidecar - by chaining the zoombie download, extract and transcribe stages in one CLI call. Use when the user says things like transcribe this video, transcribe this link, or give me a transcript of this recording. It deliberately writes no summary and no readable document; zoombie-summarize produces those. Collects every output-path confirmation up front in one pass.
---

# Skill: zoombie-transcribe-video

Thin wrapper over the deterministic pipeline. The whole chain — optional
download, audio extraction into an ASCII scratch dir, whisper.cpp, and copying
artifacts back — is one CLI call. This skill only collects confirmations.

**Your deliverable is source material, not a document. Do not create or edit any
`.md` file, and do not summarise — the `zoombie-summarize` skill owns that.**

## Producer contract

In the confirmed output folder, one `pipeline` call writes:

| Artifact | Authoritative for |
|----------|-------------------|
| `<base>.txt` | the transcript WORDING |
| `<base>.srt` | the TIMING, and for spotting machine noise |
| `<base>.source.json` | the ORIGIN of the video, including the source URL |

- `.txt` and `.srt` come from ONE whisper decode. The SRT is never a separate
  pass and is never a yt-dlp subtitle download: yt-dlp is never asked for
  subtitles.
- whisper always runs with `-nt` (no timestamps in the `.txt`). That is exactly
  why the `.srt` is the ONLY timing source in the output set. Do not drop it.
- The `.srt` is emitted **by default**. `-NoSrt` is the opt-out and it destroys
  the timings.
- `<base>.source.json` keys: `kind`, `url`, `title`, `id`, `durationSec`,
  `language`, `model`, `backend`, `deviceUsed`, `deviceVerified`,
  `realtimeFactor`, `toolchainVersion`, `createdAt`, `sourceKept`.
- **The origin survives the deleted media.** For a URL the pipeline captures the
  video's identity from yt-dlp in the SAME invocation as the download, via
  `--print after_move:` (id, title, webpage URL, duration), and persists it to
  the sidecar. It has to be captured then, because the pipeline **deletes the
  downloaded media by default**: once the file is gone the URL would otherwise be
  unrecoverable, and the summarize pass would have no origin link to put in its
  source block. So `kind` is `video` for a URL and `audio` for a local file, and
  `sourceKept` is `true` only with `-KeepWork`.
- A missing metadata line degrades the sidecar (a warning, with `url` falling
  back to the given `-Source`) rather than failing a run whose media downloaded
  fine.

## Run this, nothing else

Resolve the CLI first. It normally lives under the user profile, but on a
machine whose user name is not ASCII the toolchain is installed under
`%PUBLIC%` instead (whisper.cpp breaks on non-ASCII paths), so check both:

```powershell
$cli = @(
    "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.cmd",
    "$env:PUBLIC\zoombie-env\bin\zoombie\zoombie.cmd"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cli) { throw "zoombie CLI not found. Run the zoombie bootstrap first: irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex  (or scripts\bootstrap.cmd from a checkout)." }
```

Then call it — this is the only command this skill needs:

```powershell
& $cli pipeline -Source "<url-or-file>" -Output "<confirmed-basename>" [-DownloadDir "<dir>"] [-Language auto] [-NoSrt] [-Model "<name>"] [-KeepWork] [-Force] [-NoGpu] [-NoFlashAttn] [-Threads N] [-AllowCpuFallback] [-StrictGpu]
```

There is no `-Format` here: the pipeline's audio is always a 16 kHz mono WAV,
because that is the only thing whisper.cpp consumes. `-Format` belongs to
`download` and `extract`.

If the installed CLI is missing entirely, fall back to the repo copy:

```powershell
cd "<repo>\scripts"
python -m zoombie pipeline -Source "<url-or-file>" -Output "<confirmed-basename>"
```

The CLI prints one JSON line: `{ ok, action, data, error }`. Read
`data.artifacts.txt.path`, `data.artifacts.srt.path` and
`data.artifacts.sidecar.path` (each with its own `size`), plus `data.outputBase`.
`data.artifacts.srt` is `null` when `-NoSrt` suppressed it.

## How the Cyrillic-path bug is handled

The pipeline extracts audio directly into an ASCII scratch folder and runs
whisper there, then copies the transcript back to the confirmed destination. A
Cyrillic destination is therefore safe, and a Cyrillic *source* path is safe
too because the audio is written to ASCII before whisper sees it.

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
   - **whether to keep the downloaded media** (`-KeepWork`); by default the
     pipeline deletes it after transcribing, and the `.source.json` sidecar is
     what preserves its origin — so the media itself is not needed afterwards,
   - the transcript folder and basename.
   Present 2–4 concrete candidate paths for each. Wait for the answers before
   running anything.

4. **Run the CLI** once with the confirmed `-Source` and `-Output` (add
   `-DownloadDir` for a URL, `-KeepWork` if the user wants the media kept). Do
   **not** pass `-Srt`: it still parses but is a legacy no-op alias, because the
   `.srt` is already produced. Pass `-NoSrt` only if the user explicitly accepts
   losing the only timing source.

5. **Report the full chain**, each item with its path and size: source, the
   downloaded video (or a note that it was deleted), and the artifacts
   (`.txt`, `.srt`, `.source.json`) — plus `data.deviceUsed`,
   `data.deviceVerified` and `data.realtimeFactor`.

6. **Hand off to `zoombie-summarize`**, naming the exact artifacts it will
   consume: `<base>.txt` (the wording), `<base>.srt` (the timing) and
   `<base>.source.json` (the origin — note that `sourceKept` may be `false`
   because the pipeline deleted the media). Write no `.md` file yourself.

## Notes

- `zoombie-download-video`, `zoombie-extract-audio`, and
  `zoombie-transcribe-audio` document the individual stages; the `pipeline`
  subcommand runs them in sequence so you do not need to call them separately.
- **`-Force` also reaches the download stage.** Without it a re-run would reuse
  a cached download while still reporting a fresh transcription of the OLD
  media; with it yt-dlp overwrites, and the `<base>.txt` overwrite guard is
  lifted too. Never pass it without the user's consent.
- **A CPU run on a GPU machine is a failure, not a warning.** When a usable GPU
  backend is configured, the pipeline refuses to return a CPU transcript:
  `ok:false` with an `error` naming the reason (usually an incomplete CUDA
  runtime, e.g. a missing `cublas64_*.dll`), and `data.logPath` points at the
  preserved whisper log. Use `-NoGpu` only if the user explicitly wants the CPU.
  A machine with no GPU is unaffected and transcribes on the CPU normally.
- Read `data.deviceUsed` and `data.realtimeFactor` and report them. On a GPU
  machine `deviceUsed` must be `cuda` (or `vulkan`) and `realtimeFactor` well
  below `1.0`. `data.deviceVerified` is the positive proof the GPU was used;
  `backendInitialised` only means the backend loaded. If `deviceVerified` is
  `false`, GPU use is unproven: report that plainly, and pass `-StrictGpu` on a
  re-run to make it a hard failure. `gpuAttemptWallMs` reports time wasted by an
  abandoned GPU attempt before a CPU retry.
- Never delete a user-supplied local source file. Never overwrite without
  asking.
- Shell: any. The launcher is a `.cmd` shim, so it works from `cmd.exe`,
  PowerShell or a plain process spawn without a wrapper.

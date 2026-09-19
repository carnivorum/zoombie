---
name: zoombie-transcribe-video
cvrm-zoombie-version: 3.2.0
description: End-to-end pipeline that turns a video file or a video URL into a transcript by chaining the zoombie download, extract and transcribe skills. Use when the user says things like "transcribe this video", "transcribe this link", "give me a transcript of this recording", or "make subtitles from this video". Collects every output-path confirmation up front in one pass, then runs download (when a URL is given), audio extraction, and whisper.cpp transcription in sequence.
---

# Skill: zoombie-transcribe-video

Thin wrapper over the deterministic pipeline. The whole chain — optional
download, audio extraction into an ASCII scratch dir, whisper.cpp, and copying
artifacts back — is one CLI call. This skill only collects confirmations.

## Run this, nothing else

Resolve the CLI first. It normally lives under the user profile, but on a
machine whose user name is not ASCII the toolchain is installed under
`%PUBLIC%` instead (whisper.cpp breaks on non-ASCII paths), so check both:

```powershell
$cli = @(
    "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.ps1",
    "$env:PUBLIC\zoombie-env\bin\zoombie\zoombie.ps1"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cli) { throw "zoombie CLI not found. Run scripts/setup.ps1 first." }
```

Then call it — this is the only command this skill needs:

```powershell
& $cli pipeline -Source "<url-or-file>" -Output "<confirmed-basename>" [-DownloadDir "<dir>"] [-Srt] [-Language auto] [-Force]
```

If the installed CLI is missing entirely, fall back to the repo copy:

```powershell
& "<repo>\scripts\zoombie.ps1" pipeline -Source "<url-or-file>" -Output "<confirmed-basename>"
```

The CLI prints one JSON line: `{ ok, action, data, error }`. Read
`data.artifacts.txt.path` (and `.srt` when `-Srt` was used).

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
   - the source (URL or local path); if a URL, whether to keep the downloaded
     video or delete it after transcription,
   - where to put the downloaded video (only for a URL),
   - the transcript folder and basename,
   - whether to also produce the readable Markdown (reflowed, no timestamps),
   - whether to produce a summary, and how detailed.
   Present 2–4 concrete candidate paths for each. Wait for the answers before
   running anything.

4. **Run the CLI** once with the confirmed `-Source` and `-Output` (add
   `-DownloadDir` for a URL, `-Srt` for subtitles).

5. **Produce the optional Markdown and summary** only if the user opted in.

6. **Report the full chain**, each item with its path and size: source,
   downloaded video (if any), raw transcript (`.txt`/`.srt`), readable Markdown
   (if requested), summary (if requested).

## Notes

- `zoombie-download-video`, `zoombie-extract-audio`, and
  `zoombie-transcribe-audio` document the individual stages; the `pipeline`
  subcommand runs them in sequence so you do not need to call them separately.
- Never delete a user-supplied local source file. Never overwrite without
  asking.
- Shell: PowerShell. If a command fails with *"is not recognized"*, the runner
  is `cmd.exe`; wrap it as `powershell -NoProfile -Command "<one-line>"`.

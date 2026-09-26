---
name: zoombie-extract-audio
cvrm-zoombie-version: 5.1.0
description: Extract the audio track from a video file into a whisper-ready WAV (or mp3/m4a/flac) using ffmpeg. Use when the user wants the AUDIO FILE itself, e.g. pull audio out of a video, or convert to 16 kHz mono PCM. For a transcript or a readable document from the video, use zoombie-summarize instead - it runs the whole chain for you. Always inspects the project first, proposes candidate output destinations, and confirms with the user before running ffmpeg or writing any file.
---

# Skill: zoombie-extract-audio

Thin wrapper. All ffmpeg flags live in the deterministic CLI — this skill only
decides *where* to write and asks the user to confirm.

## Run this (MCP tool first)

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Call the `extract` MCP tool with the confirmed paths as JSON arguments:

```json
{"source": "<video>", "output": "<confirmed-output>", "format": "wav"}
```

CLI fallback only: `& $cli extract -Source "<video>" -Output "<confirmed-output>"`.

**When the goal is a TRANSCRIPT, do not use this skill** — hand off to
`zoombie-transcribe-video`, whose `pipeline` tool already runs download →
extract → transcribe in one call. This skill is for when the user wants the
FILE itself.

<!-- zoombie:include repo-fallback -->
<!-- /zoombie:include -->

<!-- zoombie:include json-contract -->
<!-- /zoombie:include -->

Read `data.output` and `data.size`. Do **not** hand-assemble an `ffmpeg`
command.

## Procedure

1. **Inspect the project** — list the workspace root and look for existing media
   folders (`media/`, `assets/`, `input/`, `output/`, `data/`).

2. **Find the source video.** Use the path the user gave; otherwise look for
   `.mp4`, `.mkv`, `.mov`, `.avi`, `.webm`, `.m4v`. If still ambiguous, ask.

3. **Propose 2–4 concrete output paths** consistent with the layout (a sibling
   `audio/` folder, an existing `output/`, the same folder with a `.wav`
   extension, or a user-specified location). Never assume a destination.

4. **Ask the user to confirm** the destination and format. Default to 16 kHz
   mono WAV — exactly what whisper.cpp expects. Wait for an explicit answer.

5. **Run the `extract` tool** (CLI fallback only if it is unavailable). Then
   report `data.output` and `data.size`.

## Notes

- The command always produces 16 kHz mono PCM (`-ac 1 -ar 16000`), which is the
  format `zoombie-transcribe-audio` consumes directly.
- Supported formats: `wav` (default), `mp3`, `m4a`, `flac`. Warn the user that
  whisper.cpp performs best with 16 kHz mono WAV.
- Never overwrite without asking; pass `force: true` only with the user's consent.

<!-- zoombie:include scratch-note -->
<!-- /zoombie:include -->

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

---
name: zoombie-extract-audio
cvrm-zoombie-version: 4.5.0
description: Extract the audio track from a video file into a whisper-ready WAV (or mp3/m4a/flac) using ffmpeg. Use when the user wants to pull audio out of a video or recording, prepare media for transcription, or convert to 16 kHz mono PCM. Always inspects the project first, proposes candidate output destinations, and confirms with the user before running ffmpeg or writing any file.
---

# Skill: zoombie-extract-audio

Thin wrapper. All ffmpeg flags live in the deterministic CLI — this skill only
decides *where* to write and asks the user to confirm.

## Run this, nothing else

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Then call it — this is the only command this skill needs:

```powershell
& $cli extract -Source "<video>" -Output "<confirmed-output>" [-Format wav] [-Force]
```

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

5. **Run the CLI** with the confirmed values, then report `data.output` and
   `data.size`.

## Notes

- The CLI always produces 16 kHz mono PCM (`-ac 1 -ar 16000`), which is the
  format `zoombie-transcribe-audio` consumes directly.
- Supported formats: `wav` (default), `mp3`, `m4a`, `flac`. Warn the user that
  whisper.cpp performs best with 16 kHz mono WAV.
- Never overwrite without asking; pass `-Force` only with the user's consent.

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

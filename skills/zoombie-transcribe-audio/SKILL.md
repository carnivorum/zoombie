---
name: zoombie-transcribe-audio
cvrm-zoombie-version: 3.0.0
description: Transcribe an audio file to text (TXT and SRT, plus optional readable Markdown and summary) using a local whisper.cpp binary with a CUDA/Vulkan/CPU backend. Use when the user wants a speech-to-text transcript, subtitles, SRT/TXT output, or a cleaned-up readable version of a recording. Always inspects the project, proposes transcript output paths, and confirms the destination before writing anything.
---

# Skill: zoombie-transcribe-audio

Thin wrapper. The whisper.cpp invocation — including the ASCII-path isolation
that works around the whisper Cyrillic-path bug — lives in the deterministic
CLI. This skill only decides *where* to write and asks the user to confirm.

## Run this, nothing else

```powershell
& "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.ps1" transcribe -Source "<audio>" -Output "<confirmed-basename>" [-Language auto] [-Srt] [-Force]
```

If that path is missing, fall back to the repo copy:

```powershell
& "<repo>\scripts\zoombie.ps1" transcribe -Source "<audio>" -Output "<confirmed-basename>"
```

The CLI prints one JSON line: `{ ok, action, data, error }`. Read
`data.artifacts.txt.path` (and `.srt` when `-Srt` was used). Do **not**
hand-assemble a `whisper-cli` command.

## How the Cyrillic-path bug is handled

The CLI always copies the audio into an ASCII scratch folder
(`zoombie-env\work\<guid>\input.<ext>`), runs whisper there with an ASCII `-of`
path, then copies the artifacts back to the user's real destination. So a
destination folder containing Cyrillic characters is safe. You do not need to
do anything special — just pass the paths the user confirmed.

## Procedure

1. **Inspect the project** — look for existing transcript locations
   (`transcripts/`, `output/`, `docs/`).

2. **Locate the audio input.** Use the user's path, or the project's recent
   `.wav`/`.mp3`/`.m4a`/`.flac`. If ambiguous, ask.

3. **Propose transcript output paths** and **ask the user to confirm** the
   folder and basename before writing anything. Wait for an explicit answer.

4. **Run the CLI** with the confirmed basename. Add `-Srt` if the user wants
   subtitles. Add `-Language <code>` only to force a language (auto by default).

5. **Ask two explicit follow-ups after the raw transcript exists:**
   - "Do you also want a readable Markdown version?" — if yes, write a `.md`
     file (at a confirmed path) with timestamps removed, text reflowed into
     paragraphs, and filler cleaned up.
   - "Should I also create a summary?" — if yes, ask how long and write it.

6. **Report every file produced** with its path and size.

## Notes

- Never overwrite without asking; pass `-Force` only with the user's consent.
- If the CLI returns `ok:false`, report `error` plainly. A GPU/driver mismatch
  is retried automatically with CPU (`-ng`) before failing.
- Shell: PowerShell. If a command fails with *"is not recognized"*, the runner
  is `cmd.exe`; wrap it as `powershell -NoProfile -Command "<one-line>"`.

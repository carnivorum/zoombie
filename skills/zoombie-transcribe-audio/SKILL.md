---
name: zoombie-transcribe-audio
cvrm-zoombie-version: 4.8.0
description: Transcribe an audio file into SOURCE material - a TXT transcript, an SRT subtitle track and a .source.json origin sidecar - using a local whisper.cpp binary with a CUDA/Vulkan/CPU backend. Use when the user wants a speech-to-text transcript, subtitles, or SRT/TXT output for a recording. It deliberately writes no summary and no readable document; zoombie-summarize produces those. Always inspects the project, proposes transcript output paths, and confirms the destination before writing anything.
---

# Skill: zoombie-transcribe-audio

Thin wrapper. The whisper.cpp invocation — including the ASCII-path isolation
that works around the whisper Cyrillic-path bug — lives in the deterministic
CLI. This skill only decides *where* to write and asks the user to confirm.

**Your deliverable is source material, not a document. Do not create or edit any
`.md` file, and do not summarise — the `zoombie-summarize` skill owns that.**

## Producer contract

`-Output` names the **item folder**; the artifacts always land in its `.data/`
subdirectory, which is the documented item layout. Nothing is written at the item
root -- so a transcript can never again sit loose beside `summary.md`.

In the confirmed item folder, one `transcribe` call writes:

| Artifact | Authoritative for |
|----------|-------------------|
| `.data/transcript.txt` | the transcript WORDING |
| `.data/transcript.srt` | the TIMING, and for spotting machine noise |
| `.data/source.json` | the ORIGIN of the audio |

- `.txt` and `.srt` come from ONE whisper decode. The SRT is never a separate
  pass and is never a yt-dlp subtitle download: yt-dlp is never asked for
  subtitles.
- whisper always runs with `-nt` (no timestamps in the `.txt`). That is exactly
  why the `.srt` is the ONLY timing source in the output set: with no cue lines
  in the `.txt`, a downstream indexing pass has nowhere else to read the timings
  from. Do not drop the SRT "to keep things tidy".
- The `.srt` is emitted **by default**. `-NoSrt` is the opt-out and it destroys
  the timings.
- `.data/source.json` records the origin. Keys: `kind`, `url`, `title`, `id`,
  `durationSec`, `language`, `model`, `backend`, `deviceUsed`, `deviceVerified`,
  `realtimeFactor`, `toolchainVersion`, `createdAt`, `sourceKept`. Every value is
  captured by the download stage, measured by this run, or `null` — nothing is
  guessed. For a plain audio file `url` is the input path and `title`/`id` are
  `null`; `kind` is the recorded source category (a `pipeline` run writes `video`
  for a URL and `audio` for a local file, while a plain `transcribe` leaves the
  default `video`); `sourceKept` is `true` only when `pipeline` was told to keep
  the downloaded media (`-KeepWork`).

## Run this, nothing else

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Then call it — this is the only command this skill needs:

```powershell
& $cli transcribe -Source "<audio>" -Output "<confirmed-item-folder>" [-Language auto] [-NoSrt] [-Model "<name>"] [-Force] [-NoGpu] [-NoFlashAttn] [-Threads N] [-AllowCpuFallback] [-StrictGpu]
```

`-NoGpu` forces a deliberate CPU run; `-AllowCpuFallback` permits a CPU run on a
machine whose GPU is configured and usable. You should need neither normally —
see the GPU policy below.

<!-- zoombie:include repo-fallback -->
<!-- /zoombie:include -->

<!-- zoombie:include json-contract -->
<!-- /zoombie:include -->

Read `data.artifacts.txt.path`, `data.artifacts.srt.path` and
`data.artifacts.sidecar.path` (each with its own `size`), plus `data.outputBase`.
`data.artifacts.srt` is `null` when `-NoSrt` suppressed it. Do **not**
hand-assemble a `whisper-cli` command.

## How the Cyrillic-path bug is handled

The CLI always copies the audio into an ASCII scratch folder
(`zoombie-env\work\<guid>\input.<ext>`), runs whisper there with an ASCII `-of`
path, then copies the artifacts back to the user's real destination. So a
destination folder containing Cyrillic characters is safe. You do not need to
do anything special — just pass the paths the user confirmed.

A Cyrillic **argument**, though, can be mangled by the Windows console code page
before the CLI sees it. If quoting inline is unreliable, write the value to a
UTF-8 file under the workspace `/.tmp/` folder and pass `@<file>` instead, e.g.
`-Output "@.tmp\name.txt"`; the CLI reads the value from the file. This works for
`-Source` and `-Output`. Stage that file under `/.tmp/` and delete it when done,
never in `C:\Temp` (outside the workspace) or the workspace root.

## Procedure

1. **Inspect the project** — look for existing transcript locations
   (`transcripts/`, `output/`, `docs/`).

2. **Locate the audio input.** Use the user's path, or the project's recent
   `.wav`/`.mp3`/`.m4a`/`.flac`. If ambiguous, ask.

3. **Propose 2–4 concrete item folders** and **ask the user to confirm** the
   folder before writing anything. Wait for an explicit answer. The transcripts
   will be created under `<item>/.data/` -- say so, because `.data/` is hidden in
   Explorer and a user looking for `transcript.txt` at the item root will not see
   it.

4. **Run the CLI** with the confirmed **item folder** as `-Output`. Add `-Language <code>` only to
   force a language (auto by default). Do **not** pass `-Srt`: it still parses
   but is a legacy no-op alias, because the `.srt` is already produced. Pass
   `-NoSrt` only if the user explicitly accepts losing the only timing source.

5. **Report every artifact produced** with its path and size — `.data/transcript.txt`,
   `.data/transcript.srt` (say so plainly if it is `null`), and `.data/source.json`
   — plus `data.deviceUsed`, `data.deviceVerified` and `data.realtimeFactor`, and
   the resolved `data.itemDir`.

6. **Hand off to `zoombie-summarize`**, naming the exact artifacts it will
   consume: `.data/transcript.txt` (the wording), `.data/transcript.srt` (the
   timing) and `.data/source.json` (the origin). Write no `.md` file yourself.

## Notes

- **Never overwrite without asking.** `transcribe` refuses to overwrite an
  existing `<item>/.data/transcript.txt` unless `-Force` is given, and it refuses
  *before* any scratch directory is created, so an accidental re-run costs
  nothing. Pass `-Force` only with the user's consent.
- If the CLI returns `ok:false`, report `error` plainly.
- **A CPU run on a GPU machine is a failure, not a warning.** When a usable GPU
  backend is configured, the CLI refuses to return a CPU transcript: it returns
  `ok:false` with an `error` naming the reason (most often an incomplete CUDA
  runtime, e.g. a missing `cublas64_*.dll`), and `data.logPath` points at the
  preserved whisper log. Pass `-NoGpu` only if the user explicitly wants the CPU.
  A machine with no GPU is unaffected and transcribes on the CPU normally.
- The GPU retry is narrow: it only re-runs on the CPU when the failure looks like
  a GPU failure. An unrelated error (bad model, unsupported codec) surfaces
  directly instead of being masked by a CPU re-run.
- Read `data.deviceUsed` and `data.realtimeFactor` and report them. On a GPU
  machine `deviceUsed` must be `cuda` (or `vulkan`) and `realtimeFactor` well
  below `1.0`. `data.deviceVerified` is the positive proof the GPU was used;
  `backendInitialised` only means the backend loaded. If `deviceVerified` is
  `false`, GPU use is unproven: report that plainly, and pass `-StrictGpu` on a
  re-run to make it a hard failure. `gpuAttemptWallMs` is the time wasted by an
  abandoned GPU attempt before a CPU retry.

<!-- zoombie:include scratch-note -->
<!-- /zoombie:include -->

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

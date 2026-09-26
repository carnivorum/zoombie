---
name: zoombie-download-video
cvrm-zoombie-version: 5.1.0
description: Download a video (or its audio only) from a URL using yt-dlp, supporting YouTube and other yt-dlp-compatible sites such as RuTube, Vimeo, Twitter/X, Twitch and TikTok. Use when the user provides a link and asks to download, save, grab, or fetch the FILE itself, or when a later step needs a local copy of a remote video. For a transcript or a readable document from the link, use zoombie-summarize instead - it runs the whole chain for you. Always inspects the project first, proposes candidate destinations, and confirms where to save before downloading anything.
---

# Skill: zoombie-download-video

Thin wrapper. All commands live in the deterministic CLI — this skill only
decides *where* to save and asks the user to confirm.

## Run this (MCP tool first)

<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->

Call the `download` MCP tool with the confirmed paths as JSON arguments:

```json
{"source": "<url>", "download_dir": "<confirmed-dir>", "audio_only": false, "format": "wav"}
```

CLI fallback only: `& $cli download -Source "<url>" -DownloadDir "<confirmed-dir>"`.

**When the goal is a TRANSCRIPT, do not use this skill** — hand off to
`zoombie-transcribe-video`, whose `pipeline` tool already runs download →
extract → transcribe in one call. This skill is for when the user wants the
FILE itself.

<!-- zoombie:include repo-fallback -->
<!-- /zoombie:include -->

<!-- zoombie:include json-contract -->
<!-- /zoombie:include -->

Read `data.file` for the saved path. Do **not** hand-assemble a `yt-dlp`
command.

## Procedure

1. **Get the URL.** If the user pasted a link, that is the input. If not, ask.
   Do not assume YouTube — any yt-dlp-compatible site works.

2. **Inspect the project** and propose 2–4 concrete destination folders
   (an existing `media/`, `downloads/`, or `output/`, or a new folder). List the
   workspace root first so the suggestions fit its layout.

3. **Ask the user to confirm** the destination folder and whether they want the
   full video or **audio only** (`audio_only`). Wait for an explicit answer.
   Never assume a destination.

4. **Run the `download` tool** (CLI fallback only if it is unavailable). Pass
   `force: true` only if the user agreed to overwrite files.

5. **Report** `data.file` and its size. If the user also wants a transcript,
   hand off to `zoombie-transcribe-video` (it runs the whole chain in one call
   and produces the `.txt`, `.srt` and `.source.json` source material) instead of
   re-downloading. It produces no summary; document work belongs to
   `zoombie-summarize`.

## Notes

- Legal/etiquette: download only content the user has the right to download,
  and respect each site's terms of service.
- If the result returns `ok:false`, report `error` plainly. Common causes: the
  site is unsupported, the video is region-locked, or it needs cookies/auth.
- `audio_only` can convert straight to a whisper-ready 16 kHz mono WAV
  (`format: "wav"`, the default), which is what the transcription stage wants.

<!-- zoombie:include scratch-note -->
<!-- /zoombie:include -->

<!-- zoombie:include shell-note -->
<!-- /zoombie:include -->

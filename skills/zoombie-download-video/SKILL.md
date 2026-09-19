---
name: zoombie-download-video
cvrm-zoombie-version: 3.0.0
description: Download a video (or its audio only) from a URL using yt-dlp, supporting YouTube and other yt-dlp-compatible sites such as RuTube, Vimeo, Twitter/X, Twitch and TikTok. Use when the user provides a link and asks to download, save, grab, or fetch a video, or when a later step needs a local copy of a remote video. Always inspects the project first, proposes candidate destinations, and confirms where to save before downloading anything.
---

# Skill: zoombie-download-video

Thin wrapper. All commands live in the deterministic CLI — this skill only
decides *where* to save and asks the user to confirm.

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
& $cli download -Source "<url>" -DownloadDir "<confirmed-dir>" [-AudioOnly] [-Force]
```

If the installed CLI is missing entirely, fall back to the repo copy:

```powershell
& "<repo>\scripts\zoombie.ps1" download -Source "<url>" -DownloadDir "<confirmed-dir>"
```

The CLI prints one JSON line: `{ ok, action, data, error }`. Read `data.file`
for the saved path. Do **not** hand-assemble a `yt-dlp` command.

## Procedure

1. **Get the URL.** If the user pasted a link, that is the input. If not, ask.
   Do not assume YouTube — any yt-dlp-compatible site works.

2. **Inspect the project** and propose 2–4 concrete destination folders
   (an existing `media/`, `downloads/`, or `output/`, or a new folder). List the
   workspace root first so the suggestions fit its layout.

3. **Ask the user to confirm** the destination folder and whether they want the
   full video or **audio only** (`-AudioOnly`). Wait for an explicit answer.
   Never assume a destination.

4. **Run the CLI** with the confirmed values. Pass `-Force` only if the user
   agreed to overwrite existing files.

5. **Report** `data.file` and its size. If the user also wants a transcript,
   hand off to the `zoombie-transcribe-video` skill instead of re-downloading.

## Notes

- Legal/etiquette: download only content the user has the right to download,
  and respect each site's terms of service.
- If the CLI returns `ok:false`, report `error` plainly. Common causes: the site
  is unsupported, the video is region-locked, or it needs cookies/auth.
- Shell: PowerShell. If a command fails with *"is not recognized"*, the runner
  is `cmd.exe`; wrap it as `powershell -NoProfile -Command "<one-line>"`.

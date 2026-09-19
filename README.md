# zoombie

A deterministic Windows speech-to-text toolchain: download video → extract
audio → transcribe with whisper.cpp (CUDA/Vulkan/CPU).

Everything fragile lives in tested scripts, not in prose an agent re-interprets.
The skills are thin wrappers that call one CLI.

## Why it is built this way

- **Determinism.** Install and media commands live in [`scripts/`](scripts/), so
  the same prompt produces the same result instead of re-improvising `ffmpeg`
  and `whisper-cli` flags on every run.
- **No install-path reliance.** All tools are fetched into one ASCII root and
  invoked by absolute path resolved from a manifest. PATH is only a fallback,
  refreshed from the registry, so "tool not found" false negatives are gone.
- **Cyrillic-path safe.** whisper.cpp misbehaves with non-ASCII paths. The CLI
  copies every input into an ASCII scratch dir (`zoombie-env\work\<guid>\`), runs
  whisper there, and copies artifacts back to the real, possibly Cyrillic,
  destination. Non-ASCII input names, output folders, and user profiles are safe.
- **Clean output.** whisper runs with `-nt` (no timestamps) and its log banner
  is kept off stdout; UTF-8 is forced so non-ASCII transcript text survives.

## Layout

```
setup.md                        thin setup prompt that drives the scripts
scripts/
  lib/ZoombieEnv.psm1           shared helpers (paths, ASCII guard, manifest, JSON result)
  setup.ps1                     install/verify orchestrator (-Check, -DryRun)
  zoombie.ps1                   runtime CLI (doctor/download/extract/transcribe/pipeline/clean)
  selftest.ps1                  end-to-end test incl. Cyrillic-path regression
skills/
  zoombie-download-video/SKILL.md     thin wrapper -> zoombie.ps1 download
  zoombie-extract-audio/SKILL.md      thin wrapper -> zoombie.ps1 extract
  zoombie-transcribe-audio/SKILL.md   thin wrapper -> zoombie.ps1 transcribe
  zoombie-transcribe-video/SKILL.md   thin wrapper -> zoombie.ps1 pipeline
```

The installed toolchain lives outside the repo, at an ASCII path:

```
%USERPROFILE%\zoombie-env\
  bin\ffmpeg.exe, ffprobe.exe, yt-dlp.exe
  bin\whisper\whisper-cli.exe (+ CUDA/Vulkan DLLs beside it)
  bin\zoombie\zoombie.ps1, lib\ZoombieEnv.psm1   the deployed CLI
  models\ggml-*.bin
  work\<guid>\                                    ASCII scratch for each job
  env.json                                        resolved paths, versions, hardware
```

Skills are deployed to the global root `%USERPROFILE%\.roo\skills\`.

## Quick start

```powershell
# detect only (writes nothing)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -Check

# show the plan (writes nothing)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -DryRun

# install / update everything (idempotent)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1

# verify end to end (includes a Cyrillic-path regression test)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\selftest.ps1
```

## Distributing to other machines

[`scripts/setup.ps1`](scripts/setup.ps1) is the single entry point and is
**self-bootstrapping**: pulled on its own from GitHub raw, it downloads the rest
of the repo (shared module, runtime CLI, self-test, skills) into a local
checkout, then installs. So the distribution unit is one URL.

On a new, **unconfigured** machine, download and run that one file:

```powershell
$src = "$env:USERPROFILE\zoombie-env\src"
New-Item -ItemType Directory -Force -Path $src | Out-Null
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/setup.ps1" -OutFile "$src\setup.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "$src\setup.ps1"
```

Or just paste [`setup.md`](setup.md) into a Zoo task — its Step 0 is exactly this.
Re-running the same command updates the scripts in place (the install is
idempotent).

What travels in the repo vs. what each machine rebuilds:

| Thing | Travels? | Why |
|-------|----------|-----|
| `scripts/`, `skills/`, `setup.md` | yes | the implementation and the sources; `setup.ps1` fetches these itself |
| `%USERPROFILE%\zoombie-env\` | no | machine-local and large (the model alone can be ~1.5 GB); re-fetched so it matches each machine's GPU backend |
| `%USERPROFILE%\.roo\skills\` | no | deployed copies, written from `skills/` by `setup.ps1` |

On a machine that is **already configured**, nothing further is needed: the
deployed CLI at `%USERPROFILE%\zoombie-env\bin\zoombie\zoombie.ps1` is
self-contained (it carries its own `lib\`), and the four `zoombie-*` skills live
in the global root, so "transcribe this video" works from any workspace.

The bootstrap honors environment overrides, so a fork or branch can be used
without editing the script:

```powershell
$env:ZOOMBIE_REPO_SLUG = 'someone/zoombie'   # default: carnivorum/zoombie
$env:ZOOMBIE_REPO_REF  = 'dev'               # default: main
```

## The CLI

`zoombie.ps1` prints exactly one JSON result line per call:
`{ ok, action, data, error, timestamp }`. Human-readable progress goes to stderr.

```powershell
$zoombie = "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.ps1"

& $zoombie doctor                                         # report tool status
& $zoombie download -Source "<url>" -DownloadDir "<dir>" [-AudioOnly]
& $zoombie extract  -Source "<video>" -Output "<out>" [-Format wav|mp3|m4a|flac]
& $zoombie transcribe -Source "<audio>" -Output "<basename>" [-Language auto] [-Srt]
& $zoombie pipeline -Source "<url-or-file>" -Output "<basename>" [-DownloadDir "<dir>"] [-Srt]
& $zoombie clean                                          # remove scratch dirs
```

Every subcommand accepts `-DryRun` (plan only) and most accept `-Force`.
The input parameter is `-Source` (not `-Input`, which PowerShell reserves).

## Skills

The four skills in [`skills/`](skills/) are the canonical sources. They only
inspect the project, propose paths, collect the user's confirmation, and then
call `zoombie.ps1`. They are namespaced `zoombie-*` so their names cannot
collide with a foreign skill, and they carry `cvrm-zoombie-version: 3.0.0`,
which `setup.ps1` compares to decide `up to date` vs `updated`.

## Hacking

- Shared logic belongs in [`scripts/lib/ZoombieEnv.psm1`](scripts/lib/ZoombieEnv.psm1).
- Bump `ZoombieSkillVersion` in that module when skill content changes, so the
  deployment step can tell an installed skill is out of date.
- Run `scripts\selftest.ps1` after any change that touches the pipeline.

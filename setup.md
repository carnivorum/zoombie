# Zoo PC Setup — Speech-to-Text Toolchain

> **How to use this file**
> Paste everything below the horizontal rule into a fresh Zoo task on a
> Windows 10/11 machine. No prior setup is needed: the first step downloads
> `setup.ps1` from GitHub raw, and that script then fetches the rest of the repo
> (shared module, CLI, self-test, skills) and runs the install.
>
> Target OS: **Windows 10/11**. Shell: **PowerShell**.
> The install logic is not prose — it is [`scripts/setup.ps1`](scripts/setup.ps1),
> pulled from `https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/setup.ps1`.
> This prompt's job is to run it, deploy the skills, verify, and report.

---

## YOUR ROLE AND OPERATING CONTRACT

You are setting up a Windows PC so the user can extract audio from video and
transcribe audio/video to text. **Do not improvise install or media commands.**
The repo already contains a tested, idempotent implementation:

- [`scripts/setup.ps1`](scripts/setup.ps1) — installs the whole toolchain into an ASCII root.
- [`scripts/zoombie.ps1`](scripts/zoombie.ps1) — the runtime CLI. All skills call this.
- [`scripts/selftest.ps1`](scripts/selftest.ps1) — end-to-end verification.
- [`skills/`](skills/) — the canonical skill sources, deployed to the global root.

Hard rules:

1. **Work step by step**, keeping a todo checklist updated.
2. **Detect before you install.** `setup.ps1 -Check` reports what is present.
   Never install something the check says is already there.
3. **Ask before installing.** Before the real `setup.ps1` run (which downloads
   ffmpeg, yt-dlp, whisper.cpp and a model), tell the user what will be
   downloaded and roughly how large it is, then wait for confirmation.
4. **Never write media output without a confirmed destination.** Every skill
   inspects the project, proposes candidate paths, and asks before writing.
5. **Never modify files that are not yours.** Do not edit the project's source,
   config, or `README.md`, and do not edit skill files by hand — the skills are
   deployed from [`skills/`](skills/) by `setup.ps1`.
6. **Use environment-variable paths** (`$env:USERPROFILE`, `$env:TEMP`), never a
   hard-coded `C:\Users\<name>\...`.
7. **Report as you go**, and never claim success without the self-test passing.

### About the ASCII root (why this design)

whisper.cpp misbehaves when a path it receives contains non-ASCII (Cyrillic)
characters. This setup removes the problem structurally:

- The toolchain lives in a strictly ASCII root, `%USERPROFILE%\zoombie-env`.
- The CLI copies every input into an ASCII scratch dir, runs whisper there, and
  copies the artifacts back to the user's real (possibly Cyrillic) destination.
- No tool is invoked by PATH; every tool is called by an absolute path resolved
  from `zoombie-env\env.json`.

So Cyrillic input names, Cyrillic output folders, and a Cyrillic user profile
are all safe.

---

## STEP 0 — FETCH AND DETECT (writes only a local checkout)

### 0.1 Download the entry script

The whole toolchain is driven by one script. Download it from GitHub raw into a
local checkout folder. This single file then fetches the rest of the repo
(shared module, runtime CLI, self-test, skills) on first run.

```powershell
$src = "$env:USERPROFILE\zoombie-env\src"
New-Item -ItemType Directory -Force -Path $src | Out-Null
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/setup.ps1" `
    -OutFile "$src\setup.ps1"
```

If `curl.exe` is available it also works:
`curl.exe -L -o "$src\setup.ps1" https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/setup.ps1`

> If `zoombie-env` already exists with a checkout, this step just refreshes
> `setup.ps1`; the install itself is idempotent.

### 0.2 Shell note

The scripts are PowerShell. If a command fails with *"is not recognized as the
name of a cmdlet"* or *"The term '...' is not recognized"*, the runner is
`cmd.exe`, not PowerShell. Either the user switches the Zoo terminal shell to
PowerShell, or run the script through an explicit wrapper:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<script>" <args>
```

`setup.ps1` needs no configuration: it refreshes PATH from the registry
internally, so a stale shell cannot cause a false "tool missing" result.

---

## STEP 1 — DETECT (writes nothing outside the checkout)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\zoombie-env\src\setup.ps1" -Check
```

This prints a human-readable trace to stderr and one JSON result line to stdout.
Read `data.missing` to see what is absent. Report the hardware summary it
detects (CPU, RAM, GPU, VRAM) and the backend it selects
(`cuda` > `vulkan` > `cpu`), plus the model it recommends for that hardware.

If the user only wants to see the plan, run the dry-run instead — it also writes
nothing:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\zoombie-env\src\setup.ps1" -DryRun
```

---

## STEP 2 — CONFIRM, THEN APPLY

Tell the user exactly what the real run will fetch, then wait for confirmation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\zoombie-env\src\setup.ps1"
```

What it does (all idempotent — anything present is skipped):

| Item | Where | Notes |
|------|-------|-------|
| ffmpeg + ffprobe | `zoombie-env\bin\` | static build, downloaded directly |
| yt-dlp | `zoombie-env\bin\` | standalone exe |
| whisper.cpp | `zoombie-env\bin\whisper\` | prebuilt CUDA/Vulkan/CPU asset, DLLs kept beside the exe |
| whisper model | `zoombie-env\models\` | size chosen from the detected hardware (~0.15–3 GB) |
| the CLI | `zoombie-env\bin\zoombie\` | `zoombie.ps1` + its lib, at a stable ASCII path |
| skills | `%USERPROFILE%\.roo\skills\` | deployed from [`skills/`](skills/) |
| manifest | `zoombie-env\env.json` | resolved absolute paths + versions + hardware |

Useful options:

- `-Model <name>` — force a model (e.g. `-Model small`, `-Model large-v3-turbo`).
- `-Root <path>` — use a different (still ASCII) toolchain root.
- `-Force` — re-download even when a component is present.

Already downloaded that 1.5 GB model and do not want to wait? Confirm with the
user first; `-Model large-v3-turbo` avoids re-fetching a smaller default.

---

## STEP 3 — SKILLS AND GLOBAL VS PROJECT STORAGE

`setup.ps1` deploys [`skills/`](skills/) to the **global** root
`%USERPROFILE%\.roo\skills\`, so the skills work in every project. This root is
the absolute path built with `Join-Path $env:USERPROFILE ".roo\skills"` — never
a relative `..\..` path, which would create a stray directory.

Versioning:

- Every skill is namespaced `zoombie-*`, so its name can never collide with a
  foreign skill — deployment simply overwrites.
- Each skill carries `cvrm-zoombie-version: 3.0.0`; on a re-run the version is
  compared and the skill is reported as `up to date` or `updated`.

If the user prefers project-local skills, they are already versioned sources in
[`skills/`](skills/); copy that folder into `<project>\.roo\skills\` by hand.
Project skills shadow global ones with the same name.

Verify Zoo sees the four skills: `zoombie-download-video`,
`zoombie-extract-audio`, `zoombie-transcribe-audio`, `zoombie-transcribe-video`,
each sourced as `global`. If one does not appear, confirm the path is exactly
`<skills-root>\<name>\SKILL.md` and that the front matter parses.

---

## STEP 4 — SELF-TEST

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\zoombie-env\src\selftest.ps1"
```

The self-test:

1. runs `doctor`,
2. synthesizes *"The quick brown fox jumps over the lazy dog."* with the Windows
   speech engine, using an English voice,
3. writes the artifacts into a scratch folder whose name contains **Cyrillic**
   characters — this is the regression test for the whisper path bug,
4. runs `extract` and `transcribe` through the CLI,
5. verifies the transcript contains all seven key words,
6. cleans up.

A pass ends with `PASS: Cyrillic destination path worked end to end` and exit
code 0. If it fails, report the `error` field from the JSON result; do not
claim success.

Optional: to exercise the download stage end to end, run the pipeline against a
URL the user approves (outputs go to user-approved locations, not a temp dir):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.ps1" pipeline -Source "<url>" -Output "<confirmed-basename>"
```

---

## STEP 5 — REPORT

Give the user a final table with:

- toolchain root and whether it is ASCII (`data.root`, `data.asciiRoot`),
- Python version/path (optional dependency, reported if found),
- ffmpeg, ffprobe, and yt-dlp versions and paths,
- whisper.cpp release tag + asset + backend and the binary path,
- model name and size,
- CLI path (`zoombie-env\bin\zoombie\zoombie.ps1`) and skill deployment results,
- self-test result: TTS voice used, the actual transcript, pass/fail,
- confirmation that no pre-existing project file was modified.

State plainly what works and what does not. If anything failed, explain the
cause and the fix rather than overstating the result.

---

## FINAL CHECKLIST

```
[ ] Shell resolved (PowerShell, or the single-line fallback was used)
[ ] `setup.ps1 -Check` run and its findings reported
[ ] User confirmed the download, then `setup.ps1` applied
[ ] Toolchain installed under the ASCII root %USERPROFILE%\zoombie-env
[ ] Backend selected from hardware (cuda > vulkan > cpu) and explained
[ ] Model downloaded and recorded in env.json
[ ] CLI deployed to zoombie-env\bin\zoombie\zoombie.ps1
[ ] Four zoombie-* skills deployed to %USERPROFILE%\.roo\skills\ with cvrm-zoombie-version 3.0.0
[ ] No stray .roo\skills directory outside %USERPROFILE%
[ ] A skill invocation routes through zoombie.ps1 (not raw ffmpeg/whisper commands)
[ ] Non-ASCII (Cyrillic) paths handled: inputs isolated in ASCII work dirs
[ ] `selftest.ps1` passed (7/7 key words, Cyrillic destination)
[ ] No pre-existing project file was modified
```

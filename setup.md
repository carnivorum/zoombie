# Zoo PC Setup — Speech-to-Text Toolchain

> **How to use this file**
> This file IS the procedure. It is not pasted in full by a human; a Zoo agent
> fetches it and follows it, from the short install instruction in
> [`README.md`](README.md) ("Install on a fresh machine"):
>
> ```text
> curl.exe -L -o "%TEMP%\zoombie-setup.md" https://raw.githubusercontent.com/carnivorum/zoombie/main/setup.md
> ```
>
> Then "read that file and carry out every step in it". No prior setup is needed:
> Step 0 fetches a bootstrap script, which ensures a Python interpreter, fetches
> the rest of the repo, and runs the install.
>
> Target OS: **Windows 10/11**. Shell: **PowerShell, cmd.exe, or any process
> spawn** — PowerShell users get a single line, and the batch file beneath it
> needs no wrapper.
> The install logic is not prose — it is [`scripts/bootstrap.cmd`](scripts/bootstrap.cmd),
> pulled from `https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.cmd`,
> which hands off to the Python installer in [`scripts/zoombie/install/`](scripts/zoombie/install/).
> [`scripts/bootstrap.ps1`](scripts/bootstrap.ps1) is a thin shim that downloads and
> runs that batch file, so a PowerShell user never has to save a `.cmd` by hand.
> This procedure's job is to run it, deploy the skills, verify, and report.

---

## YOUR ROLE AND OPERATING CONTRACT

You are setting up a Windows PC so the user can extract audio from video and
transcribe audio/video to text. **Do not improvise install or media commands.**
The repo already contains a tested, idempotent implementation:

- [`scripts/bootstrap.cmd`](scripts/bootstrap.cmd) — the implementation entry
  point, and the only non-Python file. Its entire job is to ensure a Python
  interpreter exists, fetch the latest repo archive, and hand off to the Python
  installer. So **any start of setup means install or update to the latest**;
  there is no cached copy to go stale and no gate that can skip the update.
- [`scripts/bootstrap.ps1`](scripts/bootstrap.ps1) — the PowerShell entry point:
  a thin shim that downloads `bootstrap.cmd` to a temp directory and runs it, so
  a fresh machine needs one line. It holds no install logic, so it cannot drift
  from the batch file. It never prompts — **this prompt is responsible for
  asking the user before the real run.**
- [`scripts/zoombie/install/main.py`](scripts/zoombie/install/main.py) — the
  installer/updater that actually does the work (the `setup-worker` replacement,
  also used directly for local development).
- [`scripts/zoombie/cli.py`](scripts/zoombie/cli.py) — the runtime CLI. The skills
  reach it through the `zoombie` **MCP server** (`python -m zoombie.mcp`), which
  calls the same command modules in process; the deployed `zoombie.cmd` launcher is
  the fallback.
- [`scripts/zoombie/selftest.py`](scripts/zoombie/selftest.py) — end-to-end verification.
- [`skills/`](skills/) — the canonical skill sources, deployed to the global root.

Hard rules:

1. **Work step by step**, keeping a todo checklist updated.
2. **Detect before you install.** `bootstrap.cmd -Check` (or
   `bootstrap.ps1 -Check`) reports what is present. Never install something the
   check says is already there.
3. **Ask before installing. You are the only thing that asks.** `bootstrap.ps1`
   and `bootstrap.cmd` both run unattended by design, so the real run must not be
   started until the user has agreed. Before it (it downloads
   ffmpeg, whisper.cpp and a model, and pip-installs yt-dlp plus the PDF
   dependencies), tell the user what will be fetched and roughly how large it is,
   then wait for confirmation. On a machine with an NVIDIA GPU this also fetches
   the matching cuBLAS runtime (~400 MB), because the whisper.cpp CUDA asset does
   not ship the cuBLAS DLLs that `ggml-cuda.dll` needs. The required version is
   read from the asset name (e.g. `whisper-cublas-11.8.0-...` → cuBLAS 11), and a
   major with no pinned, hash-verified redist is refused rather than guessed.
   **Tesseract is one of these proposed dependencies, not an optional aside**: it
   is a pinned, toolchain-owned component (~25 MB engine + ~44 MB `eng`/`rus`
   language data), so name it when you list what will be fetched and let the user
   agree before the run. There is no portable upstream archive — the installer is
   only ever *extracted*, never run, so no elevation is requested.
4. **Never write media output without a confirmed destination.** Every skill
   inspects the project, proposes candidate paths, and asks before writing.
5. **Never modify files that are not yours.** Do not edit the project's source,
   config, or `README.md`, and do not edit skill files by hand — the skills are
   deployed from [`skills/`](skills/) by the installer.
6. **Use environment-variable paths** (`%USERPROFILE%`, `%TEMP%`), never a
   hard-coded `C:\Users\<name>\...`.
7. **Report as you go**, and never claim success without the self-test passing.

### About the ASCII root (why this design)

whisper.cpp misbehaves when a path it receives contains non-ASCII (Cyrillic)
characters. This setup removes the problem structurally:

- The toolchain lives in an ASCII root: normally `%USERPROFILE%\zoombie-env`, but
  it MOVES to `%PUBLIC%\zoombie-env` automatically when the profile path is not
  ASCII (see below). Any non-Latin Windows username hits this on the very first
  install, so treat the fallback as a normal case, not an aside.
- The CLI copies every input into an ASCII scratch dir, runs whisper there, and
  copies the artifacts back to the user's real (possibly Cyrillic) destination.
- No tool is invoked by PATH; every tool is called by an absolute path resolved
  from `zoombie-env\env.json`.
- If `%USERPROFILE%` itself is not ASCII (a Cyrillic user name such as
  `C:\Users\Мария`), the root automatically moves to the machine-level ASCII
  path `%PUBLIC%\zoombie-env`, because whisper.cpp also breaks when its *binary,
  model, or work dir* is under a non-ASCII path. The installer reports the root it
  chose.

So Cyrillic input names, Cyrillic output folders, and even a Cyrillic user
profile are all safe.

### About path length (the other Windows path limit)

The ASCII fix above does not cover Windows' 260-character `MAX_PATH` limit, which
is a separate failure with a separate remedy, and the two cannot be handled the
same way:

- **Managed file operations** (copying, creating, reading, deleting, unpacking
  archives) go through helpers that prepend the `\\?\` extended-length prefix. That
  prefix — not the `LongPathsEnabled` registry policy — is what works here, because
  the policy only takes effect for a process manifested `longPathAware`, and
  neither the Python CLI nor the native tools are.
- **Native tools** (whisper.cpp, ffmpeg, PyMuPDF, Tesseract) open paths with plain C
  APIs and cannot take the prefix, so their paths are only length-checked and
  refused with a clear message before anything runs. This matters most for the
  whisper model and the whisper exe: whisper.cpp opens the model with `fopen` and
  loads its sibling DLLs through the loader search path.
- **Downloads** are bounded at the source: `yt-dlp` receives
  `--windows-filenames --trim-filenames <n>`, because its output template contains
  `%(title)s`, which is content-controlled and can be long enough on its own to
  break the path.

Practical consequences for the agent:

- The install **refuses to use a toolchain root that is too deep** (limit 240
  characters, with margin) instead of installing a build that would silently fail
  at the first transcription. If setup fails with a message about the root being
  too deep for whisper.cpp, pass a shorter `-Root` (for example `C:\zoombie-env`).
  The default `%USERPROFILE%\zoombie-env` is short and unaffected.
- `doctor` reports the measured lengths (`data.report.paths.*`, including
  `longPathsEnabled`) and each model entry carries `length` and `fits`. Use those
  numbers when a model "exists" but a transcription still cannot open it.
- Do **not** try to fix a long path by hand with `\\?\` when calling the CLI.
  Pass a shorter `-Output`/`-DownloadDir` instead; the CLI applies the prefix
  itself for the operations that can use it and rejects the rest. A source or
  destination under a deep, long-titled tree is the realistic trigger.

---

## STEP 0 — FETCH AND DETECT (writes only a temp checkout)

### 0.1 Run the one-liner

Nothing needs to be saved or checked out first. This single line downloads the
bootstrap, which then ensures Python, fetches the rest of the repo (runtime CLI,
installer, self-test, skills), and runs the install. It works from any shell and
any working directory, and it writes nothing outside a temp directory until the
installer decides to.

```powershell
irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex
```

On a machine where `Invoke-WebRequest` cannot parse the response (it uses the IE
engine on Windows PowerShell 5.1), use the .NET client instead:

```powershell
iex (New-Object Net.WebClient).DownloadString('https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1')
```

From `cmd.exe`, without PowerShell at all:

```bat
curl.exe -L -o "%TEMP%\bootstrap.cmd" https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.cmd
"%TEMP%\bootstrap.cmd"
```

> **Re-running is the update.** The bootstrap always re-fetches the latest repo
> archive from GitHub, so a re-run updates in place rather than installing a
> stale copy. The install itself is idempotent, so only what changed does work.

### 0.2 Shell note

**Python is the only prerequisite.** `bootstrap.cmd` finds an existing
interpreter, and attempts `winget install Python.Python.3.12` when none is
present (it re-probes PATH afterwards and prints explicit manual instructions if
that also fails). Nothing else needs to be installed by hand.

`bootstrap.ps1` is only a shim: it downloads `bootstrap.cmd` to a temp directory
and runs it, so no `.ps1` is saved and nothing needs `Unblock-File` or an
execution-policy change. The batch file it runs takes cmd.exe, PowerShell or any
process spawn with no wrapper. If you have saved `bootstrap.ps1` as a file
instead, parameters bind normally and the script returns the installer's exit
code; run inline (as above) it **throws** on failure rather than exiting, so it
never closes your shell.

Options work in both forms. As a file: `-Check`, `-DryRun`, `-Model <name>`,
`-Root <path>`, `-Force`. For the unattended one-liner, the same choices come from
the environment: `ZOOMBIE_CHECK`, `ZOOMBIE_DRYRUN`, `ZOOMBIE_MODEL`,
`ZOOMBIE_ROOT`, `ZOOMBIE_FORCE` (plus `ZOOMBIE_REPO_SLUG` / `ZOOMBIE_REPO_REF`
for a fork or branch). A switch wins over its variable.

> **`bootstrap.ps1` never prompts.** It runs unattended by design. Asking the
> user before the real run is *this prompt's* job — see hard rule 3.

---

## STEP 1 — DETECT (writes nothing persistent)

```powershell
# inline form: set the mode through the environment, then run the one-liner
$env:ZOOMBIE_CHECK = '1'
irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex
```

Saved as a file, the same thing is `.\bootstrap.ps1 -Check`; from cmd.exe it is
`bootstrap.cmd -Check`.

This prints a human-readable trace to stderr and one JSON result line to stdout.
Read `data.missing` to see what is absent. Report the hardware summary it
detects (CPU, RAM, GPU, VRAM) and the backend it selects
(`cuda` > `vulkan` > `cpu`), plus the model it recommends for that hardware.

If the user only wants to see the plan, run the dry-run instead — it also writes
nothing:

```powershell
$env:ZOOMBIE_DRYRUN = '1'
irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex
```

---

## STEP 2 — CONFIRM, THEN APPLY

Tell the user exactly what the real run will fetch, then wait for confirmation.
**This prompt asks; the script never does.** When the user agrees:

```powershell
# drop the mode variable set above, then run the one-liner unprompted
Remove-Item Env:\ZOOMBIE_CHECK, Env:\ZOOMBIE_DRYRUN -ErrorAction SilentlyContinue
irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex
```

What it does (all idempotent — anything present is skipped). Paths are relative
to the toolchain root, which is `%USERPROFILE%\zoombie-env` on an ASCII profile
and `%PUBLIC%\zoombie-env` when the profile path is not ASCII:

| Item | Where | Notes |
|------|-------|-------|
| ffmpeg + ffprobe | `zoombie-env\bin\` | static build, downloaded directly |
| yt-dlp | the existing Python | `pip install --user`, invoked as `python -m yt_dlp` (pure Python, so no ASCII constraint) |
| whisper.cpp | `zoombie-env\bin\whisper\` | prebuilt CUDA/Vulkan/CPU asset, DLLs kept beside the exe |
| cuBLAS runtime | `zoombie-env\bin\whisper\` | **on a CUDA machine only**: the asset ships `ggml-cuda.dll` but *not* the cuBLAS DLLs it loads, so the runtime matching the asset's major (e.g. `cublas64_11.dll`, `cublasLt64_11.dll` for a `cublas-11.x` asset) is fetched from NVIDIA's redist archive and its sha256 verified before it is placed beside `whisper-cli.exe`. A major with no pinned redist is refused |
| whisper model | `zoombie-env\models\` | size chosen from the detected hardware (~0.15–3 GB) |
| PDF dependencies | the existing Python | `pymupdf4llm` + `pytesseract` via `pip install --user`; Python is already required |
| pip shims | `%APPDATA%\Python\<ver>\Scripts` | any launcher this install creates is removed again; pre-existing tools are left alone |
| Tesseract + `eng`/`rus` traineddata | `zoombie-env\tesseract\` | **provisioned, pinned**. Upstream publishes *no portable archive* — the only Windows release asset is an NSIS installer. That installer is downloaded, its published sha256 verified, and its payload **extracted** (never executed) with a pinned 7-Zip into the ASCII root, so no elevation is needed and nothing lands in a system location. Language data is fetched separately from the official `tesseract-ocr/tessdata` repository, each file sha256-verified against a pinned commit. Needed for `readpdf -Ocr` / `readimages -Ocr` / the `slides` text gate |
| the CLI | `zoombie-env\bin\zoombie\` | `zoombie.cmd` + the packaged `zoombie\` package and `pdf\` helper, at a stable ASCII path |
| skills | `%USERPROFILE%\.roo\skills\` | deployed from [`skills/`](skills/); stays under the profile even when the root is under `%PUBLIC%`, because the editor owns this path and no native tool opens it |
| the Zoombie role | `%APPDATA%\Code\User\globalStorage\zoocodeorganization.zoo-code\settings\custom_modes.yaml` | **merged**, not overwritten, from [`modes/`](modes/): only our entry is replaced, so a hand-written mode in that file survives |
| the MCP server | `%APPDATA%\Code\User\globalStorage\zoocodeorganization.zoo-code\settings\mcp_settings.json` | **merged**, not overwritten: registers the `zoombie` server (`python -m zoombie.mcp`) as `mcpServers.zoombie`, so the tools appear in the client without a manual config. Other MCP servers survive. Re-run any time with `python -m zoombie mcp -Apply` |
| manifest | `zoombie-env\env.json` | resolved absolute paths + versions + hardware |

Useful options (passed straight through `bootstrap.cmd` to the installer). Each
has a switch form and an environment form for the unattended one-liner:

| File form | One-liner form | Effect |
|-----------|----------------|--------|
| `-Model <name>` | `$env:ZOOMBIE_MODEL = '<name>'` | force a model (e.g. `small`, `large-v3-turbo`) |
| `-Root <path>` | `$env:ZOOMBIE_ROOT = '<path>'` | use a different (still ASCII) toolchain root |
| `-Force` | `$env:ZOOMBIE_FORCE = '1'` | re-download even when a component is present |

Already downloaded that 1.5 GB model and do not want to wait? Confirm with the
user first; `-Model large-v3-turbo` avoids re-fetching a smaller default.

---

## STEP 3 — SKILLS AND GLOBAL VS PROJECT STORAGE

The installer deploys [`skills/`](skills/) to the **global** root
`%USERPROFILE%\.roo\skills\`, so the skills work in every project. This root is
the absolute path built from `%USERPROFILE%` — never a relative `..\..` path,
which would create a stray directory. It is the ONE path that stays under the
profile even when the toolchain root moves to `%PUBLIC%`: the editor owns it and
no native tool opens it, so the ASCII rule that governs `zoombie-env` does not
apply to it. Do not relocate it.

Versioning:

- Every skill is namespaced `zoombie-*`, so its name can never collide with a
  foreign skill — deployment simply overwrites.
- Each skill carries `cvrm-zoombie-version: 4.8.0` (the value of
  `zoombie.__init__.SKILL_VERSION`, which is the single source of truth). On a
  re-run the version is compared and the skill is reported as `up to date` or
  `updated`. The marker must stay inside the first 12 lines of `SKILL.md`: the
  comparison reads only the front matter, so a marker pushed below it reads as
  unowned.

If the user prefers project-local skills, they are already versioned sources in
[`skills/`](skills/); copy that folder into `<project>\.roo\skills\` by hand.
Project skills shadow global ones with the same name.

Verify Zoo sees the six skills: `zoombie-download-video`,
`zoombie-extract-audio`, `zoombie-transcribe-audio`, `zoombie-transcribe-video`,
`zoombie-images-to-md`, `zoombie-summarize`, each sourced as `global`. If one does
not appear, confirm the path is exactly `<skills-root>\<name>\SKILL.md` and that
the front matter parses. A rename (`zoombie-pdf-to-md` -> `zoombie-images-to-md`)
leaves the old folder behind, so the installer prunes a deployed `zoombie-*` skill
whose `SKILL.md` carries our marker but which no longer has a source.

### The Zoombie role

The same step deploys the **Zoombie role** — a universal analyst, not a coder —
into the global custom modes file, so it appears in the Zoo Code mode picker in
every workspace. Unlike a skill, the target is a document the user also owns, so
deployment is a **merge**: only the entry whose slug is `zoombie` is replaced, and
every other mode in that file is preserved byte-for-byte.

Its permissions are the point of the design:

| Group | Why |
|-------|-----|
| `read` | read sources and existing artifacts |
| `command` | the ingest skills shell out to `zoombie.cmd`, so the role can consume a video or a PDF itself |
| `modes` | `new_task`/`switch_mode` live here; this lets the role brief Code mode as an *Analyst Intern* |
| `edit` (restricted) | writable extensions are `md`, `markdown`, `txt`, `csv`, `tsv`, `html`, `htm` only — source code, scripts, configuration and notebooks are unreachable |

Re-run just this step after editing the role, without a full setup:

```bat
cd scripts
python -m zoombie modes -Check     REM report the planned action only
python -m zoombie modes            REM dry run (the default)
python -m zoombie modes -Apply     REM write it
```

The target can be redirected with `ZOOMBIE_MODES_PATH`. If the file is not where
the extension keeps it, or a workspace has its own `.roomodes`, that override is
how to point the deploy at the right file.

### The MCP server (global)

The same install registers the **MCP server** — the toolchain's other transport —
**globally**. `python -m zoombie.mcp` speaks JSON-RPC over stdio and calls the SAME
command modules **in process**, so there is no `cmd.exe` and no console code page, and
a Cyrillic `-Output` never round-trips through a shell. Setup merges one entry,
`mcpServers.zoombie`, into the extension's global `mcp_settings.json`; every other
server in that file is preserved (the same shared-ownership rule as the role).

**Global is the target, and it is verified.** Setup writes only the global file and
then confirms the entry is present, reporting `data.manifest.mcp.registered: true`
and logging `mcp server 'zoombie' registered globally`. The **skills prefer the MCP
tools** with the `zoombie.cmd` CLI as the fallback, so a machine-wide registration is
what makes new projects work with no per-project setup. A warning —
`entry not confirmed in <path>` — means the global file could not be written; fix it
and re-run `python -m zoombie mcp -Apply`.

Re-run just this step after a manual edit, without a full setup:

```bat
cd scripts
python -m zoombie mcp -Check     REM report the planned action only
python -m zoombie mcp            REM dry run (the default)
python -m zoombie mcp -Apply     REM write it
```

The target can be redirected with `ZOOMBIE_MCP_SETTINGS_PATH`. Verify Zoo sees the
`zoombie` server in the MCP panel; if it is listed but not connected, check that
the entry's `command` is an existing Python and that the `-m zoombie.mcp` module
runs:

```bat
"%USERPROFILE%\zoombie-env\bin\zoombie\python.exe" -c "import zoombie.mcp"
```

**Optional: project scope.** The global merge above is all that is normally needed.
Only if a client insists on a project-scoped server, register the SAME entry in the
workspace's `.roo/mcp.json` (the file the extension reads for *project* servers) —
this is a manual fallback, not part of the install:

```json
{ "mcpServers": { "zoombie": {
  "command": "%USERPROFILE%\\zoombie-env\\bin\\zoombie\\python.exe",
  "args": ["-m", "zoombie.mcp"],
  "env": { "PYTHONPATH": "%USERPROFILE%\\zoombie-env\\bin\\zoombie", "PYTHONUTF8": "1" }
} } }
```

Ask the agent to fill the absolute interpreter path, since `%USERPROFILE%` is not
expanded inside the JSON.

### The item model

A produced `summary.md` lives in an **item**: a folder whose name is the user's own
choice and is never parsed by the toolchain.

```
<item>/                  any name
    summary.md           the document
    <source media>       the video/audio/PDF, when kept
    .data/               everything derived  (HIDDEN: dot-prefixed)
        img/             figures + manifest.json + README.md
        transcript.txt, transcript.srt, source.json, item.json
```

Two things are worth knowing before you go looking for files:

- **`.data/` is hidden.** Explorer and a default `Get-ChildItem` do not show it, so
  images and transcripts appear to be missing when they are not. Use
  `Get-ChildItem -Force` or `dir /a`, or let `zoombie items` report what is there.
- **The item's number and date are in `.data/item.json`**, not in the folder name.
  An older library that predates the item layout still works and can be moved onto
  it with `zoombie migrate` (dry run by default; `-Apply` to write). The migration
  never renames a folder and never renames a file inside `img/`.

To see what a workspace holds:

```bat
cd scripts
python -m zoombie items                       REM the current directory, one level
python -m zoombie items -Root "<folder>"      REM somewhere else
python -m zoombie items -Title "<title>"      REM also propose names for a new item
python -m zoombie items -Depth 2              REM also look one level further down
```

A scan reads one level below the root unless `-Depth` says otherwise; `-Recurse` is
an alias for `-Depth 2`. An item's `.data/` is never descended into, so image
directories never appear as folders the toolchain could not recognize.

---

## STEP 4 — SELF-TEST

Run it from the `scripts` directory, so `python -m zoombie...` resolves the
package:

```bat
cd scripts
set PYTHONPATH=%CD%
python -m zoombie.selftest
```

> The self-test locates the **deployed** launcher, so it exercises what the skills
> actually call. It can also be run from the deployed package directory
> `%USERPROFILE%\zoombie-env\bin\zoombie` (or `%PUBLIC%\zoombie-env\bin\zoombie`
> on a non-ASCII profile), where the package sits beside it.

The self-test:

1. locates the installed launcher and runs `doctor`,
2. asserts `backendConfigured` and `backendObserved` agree,
3. synthesizes *"The quick brown fox jumps over the lazy dog."* with the Windows
   speech engine, using an English voice,
4. writes the artifacts into a scratch folder whose name contains **Cyrillic**
   characters — this is the regression test for the whisper path bug,
5. runs `extract` and `transcribe` through the CLI,
6. verifies the transcript contains all seven key words, and that the run really
   used the configured backend (`deviceUsed`). On a CUDA machine a CPU run is a
   hard failure, because whisper.cpp exits 0 while quietly falling back,
7. re-runs the same audio with `-NoGpu` and asserts a deliberate CPU run succeeds
   and reports `deviceUsed: cpu` (proving the GPU policy does not break a
   legitimate CPU run),
8. simulates a CUDA build with no runtime and with a wrong-major runtime, and
   asserts each is reported as NOT ready — the exact state that used to look
   healthy,
9. checks device classification: a log showing only a *loaded* CUDA backend
   classifies as `cpu` (capability, not use), and a `-ng` run is never `cuda`,
10. reports the measured `realtimeFactor` for the run,
11. when the PDF toolchain is installed, generates a small PDF and runs `readpdf`
    into the same Cyrillic destination, asserting the Markdown is correct and that
    the image sidecar (`manifest.json` + `README.md` in the image directory) was
    written (otherwise this step is skipped),
12. runs `postprocess -Apply` twice on a small fixture Markdown and asserts the
    file is **byte-identical** after the second run — the idempotency guarantee
    that lets a skill re-run the summarizer safely,
13. cleans up.

A pass ends with `PASS: Cyrillic destination path worked end to end` and exit
code 0. If it fails, report the failing line; do not claim success.

**The installer now provisions the self-test's speech dependency.** `pyttsx3`
is not a runtime dependency of the pipeline — ffmpeg, whisper.cpp and yt-dlp all
work without it — but the self-test's TTS step needs it, and without it the
end-to-end chain SKIPS instead of running. So `setup` installs it (from
`requirements-selftest.txt`, reported at `data.manifest.selftest`), which is what
makes a green self-test mean the chain actually ran.

If it is still missing (no Python, or the install failed) the self-test prints
`SKIPPED: speech synthesis unavailable`. Treat that as an install defect to fix,
not a normal outcome:

```bat
python -m pip install --user -r requirements-selftest.txt
```

A skip is **not** a pipeline failure: the pure regression guards (steps 2, 3, 8
and 9) still run and still assert, so the CUDA-runtime and device-classification
regressions are verified even without a synthesized voice.

Optional: to exercise the download stage end to end, run the pipeline through the
deployed launcher against a URL the user approves (outputs go to user-approved
locations, not a temp dir):

```bat
REM the root is %PUBLIC%\zoombie-env on a non-ASCII profile; probe both
set "ZOOMBIE_BIN=%USERPROFILE%\zoombie-env\bin\zoombie\zoombie.cmd"
if not exist "%ZOOMBIE_BIN%" set "ZOOMBIE_BIN=%PUBLIC%\zoombie-env\bin\zoombie\zoombie.cmd"
"%ZOOMBIE_BIN%" pipeline -Source "<url>" -Output "<confirmed-item-folder>"
```

---

## STEP 5 — REPORT

Give the user a final table with:

- toolchain root and whether it is ASCII (`data.root`, `data.asciiRoot`),
- Python version/path (used by both yt-dlp and the PDF toolchain),
- ffmpeg and ffprobe versions and paths, and the yt-dlp version (via `python -m yt_dlp`),
- whisper.cpp release tag + asset, the backend **configured** and the backend
  **observed** (`data.manifest.whisper.backendObserved`) plus whether the CUDA
  runtime was provisioned (`data.manifest.whisper.cudaRuntimeReady`) and for
  which cuBLAS major (`data.manifest.whisper.cudaRuntime.cublasMajor`), and the
  binary path. Also report `backendDetected` (the current machine's hardware
  verdict) and `requestedBackend`. If `backendDetected` differs from the installed
  `backend`, say which case it is: `whisper.backendSubstituted` true means no asset
  shipped for the requested backend and the CPU build was used instead (the normal
  outcome on a machine whose GPU asset is not published); a substitution that is
  NOT recorded would mean the build is being replaced. If a CUDA machine reports no
  cuBLAS DLLs, say so plainly.
- GPU policy outcome (`data.gpuPolicy`). On a machine with a fitted GPU,
  `setup` FAILS (`ok:false`) when the CUDA backend cannot initialise, naming the
  reason and the preserved whisper log. Report that plainly; do not present it as
  a working GPU install. A CPU-only machine is never subject to this. Separately,
  when a GPU is present but the effective backend is `cpu`, `gpuPolicy.gpuIgnored`
  is true: the GPU is **unused**. Say so, and give the reason from
  `data.manifest.whisper.backendReason` (for example an integrated GPU sharing the
  system memory, or no `vulkan` asset shipped so the CPU build was substituted —
  `gpuPolicy.backendSubstituted`).
- model name and size,
- PDF toolchain: the Python used and whether the dependencies installed cleanly
  (`data.manifest.pdf.ok`),
- Tesseract: the provisioned engine path (`data.manifest.tesseract.engine`), its
  version (`data.manifest.tesseract.version`) and the language data actually on
  disk (`data.manifest.tesseract.languages`; the pinned set is
  `expectedLanguages`). A missing component is reported in `data.missing`, like
  ffmpeg and whisper,
- CLI path (`zoombie-env\bin\zoombie\zoombie.cmd`) and skill deployment results,
- self-test result: TTS voice used (or that the step was SKIPPED), the actual
  transcript, pass/fail,
- confirmation that no pre-existing project file was modified.

State plainly what works and what does not. If anything failed, explain the
cause and the fix rather than overstating the result.

---

## FINAL CHECKLIST

```
[ ] Shell resolved (cmd.exe, PowerShell, or any process spawn — batch needs no wrapper)
[ ] `bootstrap.cmd -Check` run and its findings reported
[ ] User confirmed the download, then `bootstrap.cmd` applied
[ ] Toolchain installed under the ASCII root %USERPROFILE%\zoombie-env (or %PUBLIC%\zoombie-env on a non-ASCII profile)
[ ] Backend selected from hardware AND explained in plain language (`whisper.backendReason`): cuda for NVIDIA; vulkan only for a discrete GPU with enough dedicated VRAM and a current driver; cpu otherwise (including integrated GPUs, which share the system memory bus)
[ ] Installed backend matches the CURRENT hardware (`backendDetected`), OR differs for a recorded reason: `whisper.backendSubstituted` is true when no asset exists for the detected backend and the CPU build was used instead
[ ] Model downloaded and recorded in env.json
[ ] CLI deployed to zoombie-env\bin\zoombie\zoombie.cmd, and the launcher works from ANY working directory
[ ] Six zoombie-* skills deployed to %USERPROFILE%\.roo\skills\ with cvrm-zoombie-version 4.9.0
[ ] The Zoombie role merged into the global custom_modes.yaml, only our entry replaced, and any hand-written mode in that file still intact
[ ] The `zoombie` MCP server merged into the global mcp_settings.json (`mcpServers.zoombie`), only our entry replaced, and any other MCP server in that file still intact
[ ] The MCP server is usable: `python -m zoombie.mcp --version` prints to stderr from the deployed package
[ ] A `.md` artifact can be written in the Zoombie role and a `.py` file is refused by its edit restriction
[ ] No stray .roo\skills directory outside %USERPROFILE%
[ ] A skill invocation routes through zoombie.cmd (not raw ffmpeg/whisper/python commands)
[ ] PDF dependencies installed into the existing Python; Tesseract engine provisioned into `zoombie-env\tesseract\` with `eng` and `rus` language data (`data.manifest.tesseract.ok`)
[ ] Backend verified, not assumed: `backendObserved` matches `backendConfigured`; on a CUDA machine the cuBLAS runtime for the ASSET's major (e.g. `cublas64_11.dll`, `cublasLt64_11.dll`) is present beside `whisper-cli.exe`
[ ] GPU policy holds: a machine with a fitted GPU reports `deviceUsed: cuda` on a real transcription, or `setup` FAILED and said why
[ ] On a GPU-accelerated install (cuda, or a discrete-GPU vulkan), a real transcription reports `deviceUsed: cuda`/`vulkan` and a `realtimeFactor` well below 1.0. **This line does not apply to a CPU install** — `deviceUsed: cpu` there is the intended outcome, not a failure
[ ] Non-ASCII (Cyrillic) paths handled: inputs isolated in ASCII work dirs
[ ] Path lengths sane: `doctor` reports `data.report.paths.whisperExeFits` and `modelPathFits` true, and the installer did not refuse the root as too deep
[ ] `python -m zoombie.selftest` passed (7/7 key words, Cyrillic destination, GPU assertion, deliberate `-NoGpu` run), or reported the TTS step SKIPPED with the pure regression guards still passing
[ ] `readpdf` (PDF -> Markdown + manifest.json/README.md image sidecar) verified or reported as skipped
[ ] `postprocess -Apply` idempotent: a second run leaves the file byte-identical
[ ] No pre-existing project file was modified
```

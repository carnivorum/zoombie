# zoombie

A deterministic Windows text-extraction toolchain: download video → extract
audio → transcribe with whisper.cpp (CUDA/Vulkan/CPU), plus PDF → Markdown.

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
- **The GPU is proved, not assumed — and required when it exists.** `env.json`
  records what was *intended* (`backend`). Every report also carries what
  whisper.cpp can *actually* initialise (`backendObserved`), because those
  diverge whenever the CUDA runtime is incomplete — and a CUDA build missing its
  cuBLAS DLLs exits 0 while transcribing on the CPU. A `transcribe` on a machine
  whose GPU fits the model therefore **fails** rather than quietly returning a
  slow CPU transcript. A CPU-only machine (no CUDA/Vulkan backend) runs on the
  CPU normally; `-NoGpu` forces the CPU deliberately.
- **Optional flags are probed, not assumed.** `whisper-cli --help` decides
  whether `-fa` (flash attention) and `-t` (threads) exist before they are
  passed, because an unknown flag aborts the run.

## Layout

```
setup.md                        thin setup prompt that drives the scripts
scripts/
  lib/ZoombieEnv.psm1           shared helpers (paths, ASCII guard, manifest, JSON result)
  pdf/extract_pdf.py            PDF -> Markdown extractor (PyMuPDF4LLM + optional Tesseract OCR)
  requirements-pdf.txt          Python dependencies for the PDF extractor
  setup.ps1                     thin entry point: fetch the latest setup-worker.ps1 and run it
  setup-worker.ps1              the installer/updater (install or update; -Check, -DryRun)
  zoombie.ps1                   runtime CLI (doctor/download/extract/transcribe/readpdf/pipeline/clean)
  selftest.ps1                  end-to-end test incl. Cyrillic-path regression
skills/
  zoombie-download-video/SKILL.md     thin wrapper -> zoombie.ps1 download
  zoombie-extract-audio/SKILL.md      thin wrapper -> zoombie.ps1 extract
  zoombie-transcribe-audio/SKILL.md   thin wrapper -> zoombie.ps1 transcribe
  zoombie-transcribe-video/SKILL.md   thin wrapper -> zoombie.ps1 pipeline
  zoombie-pdf-to-md/SKILL.md          thin wrapper -> zoombie.ps1 readpdf
```

The installed toolchain lives outside the repo, at an ASCII path:

```
%USERPROFILE%\zoombie-env\
  bin\ffmpeg.exe, ffprobe.exe
  bin\whisper\whisper-cli.exe (+ CUDA/Vulkan DLLs beside it, incl. the cuBLAS
               runtime cublas64_<major>.dll/cublasLt64_<major>.dll, provisioned
               separately because the whisper.cpp asset does not ship it; the
               required major is read from the asset name, not hard-coded)
  bin\zoombie\zoombie.ps1, lib\ZoombieEnv.psm1, pdf\extract_pdf.py   the deployed CLI
  models\ggml-*.bin
  work\<guid>\                                    ASCII scratch for each job
  tmp\                                            ASCII scratch for downloads/extraction
  env.json                                        resolved paths, versions, hardware
```

The root must be ASCII because whisper.cpp breaks on non-ASCII paths — not only
for the media, but for its own binary and model too. So the root is chosen
dynamically: `%USERPROFILE%\zoombie-env` when the profile path is ASCII, and
`%PUBLIC%\zoombie-env` when it is not (e.g. a user named `Мария`). `env.json`
records whichever was used, and the skills probe both locations.

Skills are deployed to the global root `%USERPROFILE%\.roo\skills\`.

## Quick start

```powershell
# LOCAL DEV: install/update from THIS working tree (no network)
# detect only (writes nothing)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-worker.ps1 -Check

# show the plan (writes nothing)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-worker.ps1 -DryRun

# install / update everything from the working tree (idempotent)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-worker.ps1

# verify end to end (includes a Cyrillic-path regression test)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\selftest.ps1
```

```powershell
# END USERS: any invocation of setup.ps1 installs or updates to the LATEST
# (it fetches the latest setup-worker.ps1 from GitHub and runs it)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -Check
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

## Distributing to other machines

[`scripts/setup.ps1`](scripts/setup.ps1) is the single entry point and is a
**thin bootstrap**: it always fetches the CURRENT
[`scripts/setup-worker.ps1`](scripts/setup-worker.ps1) from GitHub and runs it
with `-Refresh`. So any start of setup means *install or update to the latest* —
there is no cached copy to go stale and no gate that can skip the update.

That makes the distribution unit a single URL. On any machine, configured or
not:

```powershell
$src = "$env:USERPROFILE\zoombie-env\src"
New-Item -ItemType Directory -Force -Path $src | Out-Null
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/setup.ps1" -OutFile "$src\setup.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "$src\setup.ps1"
```

Or just paste [`setup.md`](setup.md) into a Zoo task — its Step 0 is exactly this.
Re-running the same command re-fetches the latest worker and updates in place
(the install is idempotent, so only what changed does work).

What travels in the repo vs. what each machine rebuilds:

| Thing | Travels? | Why |
|-------|----------|-----|
| `scripts/`, `skills/`, `setup.md` | yes | the implementation and the sources; `setup.ps1` fetches these itself |
| `%USERPROFILE%\zoombie-env\` | no | machine-local and large (the model alone can be ~1.5 GB); re-fetched so it matches each machine's GPU backend |
| `%USERPROFILE%\.roo\skills\` | no | deployed copies, written from `skills/` by `setup.ps1` |

On a machine that is **already configured**, nothing further is needed: the
deployed CLI at `%USERPROFILE%\zoombie-env\bin\zoombie\zoombie.ps1` is
self-contained (it carries its own `lib\` and `pdf\`), and the five `zoombie-*`
skills live in the global root, so "transcribe this video" and "convert this PDF"
work from any workspace.

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
& $zoombie transcribe -Source "<audio>" -Output "<basename>" [-Language auto] [-Srt] [-NoGpu] [-NoFlashAttn] [-Threads N] [-AllowCpuFallback] [-StrictGpu]
& $zoombie readpdf  -Source "<pdf>" -Output "<basename>" [-Ocr] [-Images] [-Pages "1-5,8"]
& $zoombie pipeline -Source "<url-or-file>" -Output "<basename>" [-DownloadDir "<dir>"] [-Srt]
& $zoombie clean                                          # remove scratch dirs
```

Every subcommand accepts `-DryRun` (plan only) and most accept `-Force`.
The input parameter is `-Source` (not `-Input`, which PowerShell reserves).

## Performance: proving the GPU is really used

`transcribe` and `pipeline` report how the run actually executed, so a silent
slowdown is impossible to miss:

| Field | Meaning |
|-------|---------|
| `deviceUsed` | the device whisper used for THIS run (`cuda`, `vulkan` or `cpu`) |
| `deviceSelected` | `true` only when a GPU backend was actually selected for decoding |
| `deviceVerified` | positive proof of GPU use; `false` when GPU use is only inferred from the backend banner (`-StrictGpu` turns that into a failure) |
| `backendInitialised` | `true` when the backend loaded — capability, not proof of use |
| `gpuCapable` / `gpuRequired` | whether the GPU can initialise, and whether this run was obliged to use it |
| `deviceName` | the backend whisper initialised, e.g. `CUDA0` |
| `backendConfigured` | what `env.json` intended (usually equals the above) |
| `flashAttention` / `threads` | the optional flags actually passed (capability-probed) |
| `loadMs` / `totalMs` / `encodeMs` / `decodeMs` | the `whisper_print_timings` block |
| `audioDurationSec` | input duration from `ffprobe` |
| `realtimeFactor` | `totalMs` / audio duration — **lower is faster** |
| `fallbackReason` | present when the run fell back (GPU error, or a silent CPU fallback) |
| `silentCpuFallback` | `true` when a GPU backend was configured but no GPU device was used |
| `gpuAttemptWallMs` | wall time of an abandoned GPU attempt before the CPU retry |
| `logPath` | a preserved whisper log, written on any fallback or policy violation |

`realtimeFactor` is the number to watch: roughly `0.03-0.10` is a working GPU
(a 90-minute file in a few minutes), while `~1.0` means the CPU is doing the
work (a 90-minute file takes about 90 minutes). The same lines are on stderr and
in `data.log`.

### GPU policy: a fitted GPU must be used

If the installed backend is `cuda` or `vulkan` and the GPU can actually
initialise, a `transcribe`/`pipeline` run that ends up on the CPU is a **failure**,
not a warning. The GPU-retry path is also narrower than it used to be:

- the CPU retry only fires when a non-zero exit *looks like* a GPU failure
  (`cuda`, `cublas`, `out of memory`, `driver`, …). Unrelated errors — a missing
  model, an unsupported codec — no longer trigger a full CPU re-run that hides
  the real cause;
- the abandoned GPU attempt's wall time is reported as `gpuAttemptWallMs`;
- the two attempts log to separate files, so the GPU error survives.

Opt out explicitly when a CPU run is what you actually want:

```powershell
& $zoombie transcribe -Source "<audio>" -Output "<base>" -NoGpu        # deliberate CPU run
& $zoombie transcribe -Source "<audio>" -Output "<base>" -AllowCpuFallback  # permit a CPU fallback
& $zoombie transcribe -Source "<audio>" -Output "<base>" -StrictGpu       # also require POSITIVE GPU proof
```

`deviceVerified` is that positive proof. By default, when the backend initialises
but no device-selection line appears in the log, the run warns and reports
`deviceVerified: false` with `silentCpuFallback: true` rather than failing, because
that shape also matches a log-format difference. `-StrictGpu` upgrades exactly
that ambiguous case to a failure; the reliable pre-run `--help` probe is unchanged.

A machine with no GPU is unaffected: its backend is neither `cuda` nor `vulkan`,
so nothing is ever required of it.

### CUDA runtime requirement (why the GPU can silently not be used)

The ggml-org `whisper-cublas-*` asset ships `ggml-cuda.dll` but **not the cuBLAS
runtime** that `ggml-cuda.dll` loads on first use. Without `cublas64_11.dll` and
`cublasLt64_11.dll` beside `whisper-cli.exe`, `ggml_cuda_init` cannot create a
device and whisper.cpp falls back to the CPU **while still exiting 0**. Nothing
in the pipeline sees a failure: `env.json` keeps saying `cuda`, and only the run
time reveals the problem.

`setup-worker.ps1` therefore provisions the matching cuBLAS runtime separately,
from NVIDIA's redist archives — one deterministic record with one published
sha256 per component, which is what makes the download reproducible without
installing the CUDA Toolkit. The required **major** is derived from the selected
asset name (`whisper-cublas-11.8.0-bin-x64.zip` → cuBLAS 11), so an asset built
against a different CUDA major needs a different runtime instead of silently
reusing the wrong one. A major with no pinned, hash-verified redist is **refused**
rather than downloaded unverified. The archive's sha256 is checked before
anything is copied next to the binary, and a CUDA install whose runtime is still
incomplete is reported as missing (by exact DLL name) and fails loudly rather
than being left silently CPU-only.

Asset selection is provision-aware rather than merely newest-first: releases are
walked newest-first and the first cuda asset whose major has a pinned redist is
chosen, so a future CUDA-12-only release cannot be installed on a machine that can
only provision CUDA 11. If only unprovisionable assets exist, the install refuses
BEFORE downloading, naming the asset, its major and the supported majors.

The install also stops trusting a sticky `env.json`: the backend recorded there
is compared with the **current** hardware probe, and a machine that gained or
lost a GPU gets the matching build reinstalled instead of keeping the old one.

To check the state at any time:

```powershell
# names the exact missing DLLs when a CUDA install cannot initialise
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-worker.ps1 -Check

# backendConfigured vs backendObserved, with a warning on mismatch
& "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.ps1" doctor
```

## Skills

The five skills in [`skills/`](skills/) are the canonical sources. They only
inspect the project, propose paths, collect the user's confirmation, and then
call `zoombie.ps1`. They are namespaced `zoombie-*` so their names cannot
collide with a foreign skill, and they carry `cvrm-zoombie-version: 3.3.0`,
which `setup.ps1` compares to decide `up to date` vs `updated`.

`zoombie-pdf-to-md` converts a PDF to Markdown. Text PDFs need nothing extra;
scanned PDFs use an opt-in Tesseract OCR fallback (`-Ocr`). It reuses the Python
this repo already requires (the `pymupdf4llm`/`pytesseract` dependencies are
installed into it with `pip --user`), and the source PDF is copied into an ASCII
scratch dir first, so the Cyrillic-path invariant holds for PyMuPDF exactly as it
does for whisper.cpp.

## Hacking

- Shared logic belongs in [`scripts/lib/ZoombieEnv.psm1`](scripts/lib/ZoombieEnv.psm1).
- Install/update logic belongs in [`scripts/setup-worker.ps1`](scripts/setup-worker.ps1).
  Keep [`scripts/setup.ps1`](scripts/setup.ps1) thin: it only fetches the worker
  and runs it, so there is nothing in it to update.
- When iterating locally, run `scripts\setup-worker.ps1` directly — it installs
  the working tree as-is and never hits the network. `scripts\setup.ps1` is the
  end-user path and always pulls the published worker.
- Bump `ZoombieSkillVersion` in that module when skill content changes, so the
  deployment step can tell an installed skill is out of date. It is currently
  `3.3.0`; every `SKILL.md` carries the same value in `cvrm-zoombie-version`.
- The PDF dependencies are installed with `pip install --user`. pip also writes
  console launchers into `%APPDATA%\Python\<ver>\Scripts`, which this toolchain
  never calls, so the installer snapshots that folder first and removes only the
  shims its own install created (anything pre-existing is never touched).
- `yt-dlp` is installed as a Python package and invoked as `python -m yt_dlp`. It
  is pure Python and therefore ASCII-path safe (unlike whisper.cpp), so it needs
  no local copy and no ASCII isolation, and running it as a module avoids PATH
  and launcher-shim issues entirely.
- Run `scripts\selftest.ps1` after any change that touches the pipeline.

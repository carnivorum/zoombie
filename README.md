 co# zoombie

A deterministic Windows text-extraction toolchain: download video → extract
audio → transcribe with whisper.cpp (CUDA/Vulkan/CPU), plus PDF → Markdown.

Everything fragile lives in tested Python, not in prose an agent re-interprets.
The skills are thin wrappers that call one CLI.

## Install on a fresh machine

**Fire-and-forget.** One file, no arguments, any working directory. Double-click
it (it holds the window open so you can read the output) or run it from a shell:

```bat
curl.exe -L -o "%TEMP%\zoombie-install.cmd" https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/zoombie-install.cmd
"%TEMP%\zoombie-install.cmd"
```

It ensures a Python interpreter, fetches the current repo, and installs or
updates everything. Re-running it is the update: it is idempotent, and it
**removes** what the source no longer ships (a retired skill, an obsolete module).

```bat
"%TEMP%\zoombie-install.cmd" -Check     REM detect only; write nothing
"%TEMP%\zoombie-install.cmd" -DryRun    REM plan only; write nothing
```

**Agentic fallback.** If the fire-and-forget run fails, paste this into a Zoo task
to *diagnose* it: Zoo fetches [`setup.md`](setup.md), reads it, and follows it —
detect, report, and only then apply. `setup.md` is the agent-facing diagnostic
procedure, not the primary path.

```text
Fetch the zoombie setup instructions and follow them exactly.

    curl.exe -L -o "%TEMP%\zoombie-setup.md" https://raw.githubusercontent.com/carnivorum/zoombie/main/setup.md

Then read %TEMP%\zoombie-setup.md and carry out every step in it, working step by
step with a todo checklist. Detect before installing. Ask me before the real
download. Never claim success without the self-test passing.
```

## Why it is built this way

- **Determinism.** Install and media commands live in [`scripts/`](scripts/), so
  the same prompt produces the same result instead of re-improvising `ffmpeg`
  and `whisper-cli` flags on every run.
- **One language.** The toolchain is Python end to end. The only non-Python files
  are the two install shims, split deliberately: `zoombie-install.cmd` is a frozen
  **getter** (fetch `install.ps1` from the repo and run it, nothing more), and
  `install.ps1` is the **core** (ensure Python, fetch the repo archive, run the
  Python installer). Because the getter holds no install logic, a stale copy can
  never install the wrong thing — it always fetches the current core. Nothing else
  in the pipeline is PowerShell.
- **Content-based updates.** The installer reconciles the deployed tree against
  the fetched one **by sha256** ([`lib/sync.py`](scripts/zoombie/lib/sync.py)):
  it writes what is missing or changed and **removes** what the source dropped.
  A version marker no longer decides "up to date" — the bytes do — so `-Check`
  reports a stale package file or skill instead of hiding it behind a matching
  version string.
- **One shared library, split by concern.** `zoombie/lib/` holds the shared
  helpers and `zoombie/commands/` holds one module per subcommand, so a skill's
  path is short and legible: `cli → commands/<name>.py → lib/<concern>.py`.
- **Two transports, one set of commands — MCP first.** Setup **globally** registers
  the `zoombie` MCP server (`python -m zoombie.mcp`, JSON-RPC over stdio) in the
  client's own `mcp_settings.json`, verifying the entry is present
  (`manifest.mcp.registered`) **and that the registered command really starts**
  (`manifest.mcp.available`, a spawn of `<command> -m zoombie.mcp --version` with the
  registered `PYTHONPATH`) — registration alone only means the entry is in the file,
  not that the tools will appear. The merge replaces only our entry, so other MCP
  servers are preserved. The entry is a plain stdio `command`/`args` server and needs
  no `"type"` field (checked against client 3.84.0); after a setup or a window reload,
  open a **new** task, because the client fixes its MCP tool list when a task starts.
  **MCP is the transport the skills use**: each skill calls
  the MCP tool of the same name with JSON arguments, and only falls back to the
  `zoombie.cmd` launcher when those tools are unavailable. The MCP path calls the
  commands **in process** — no `cmd.exe`, no console code page — so a Cyrillic
  `output` never round-trips through a shell. Every argument a tool's schema
  advertises is accepted by the facade (a parser-derived test pins that, so a
  future flag cannot silently become un-callable over MCP). Re-register any time
  with `python -m zoombie mcp -Apply`.
- **No install-path reliance.** All tools are fetched into one ASCII root and
  invoked by absolute path resolved from a manifest. PATH is only a fallback,
  refreshed from the registry, so "tool not found" false negatives are gone.
- **Cyrillic-path safe.** whisper.cpp misbehaves with non-ASCII paths. The CLI
  copies every input into an ASCII scratch dir (`zoombie-env\work\<guid>\`), runs
  whisper there, and copies artifacts back to the real, possibly Cyrillic,
  destination. Non-ASCII input names, output folders, and user profiles are safe.
- **Long-path aware (the 260-character limit).** Windows' legacy `MAX_PATH` needs
  a different remedy from the Cyrillic fix. Managed I/O goes through helpers that
  prepend the `\\?\` extended-length prefix; native tools (whisper.cpp, ffmpeg,
  PyMuPDF, Tesseract) open paths with plain C APIs and cannot use the prefix, so
  their paths are only length-*checked* and refused up front with the real cause.
  Downloads are additionally constrained at the source: `yt-dlp` gets
  `--windows-filenames --trim-filenames <n>`, because the output template embeds
  `%(title)s`, which is content-controlled. `doctor` reports the measured
  root/exe/model lengths and `longPathsEnabled`.
- **Clean output.** whisper runs with `-nt` (no timestamps) and its log banner is
  kept off stdout; UTF-8 is forced so non-ASCII transcript text survives.
- **The GPU is proved, not assumed — and required when it exists.** `env.json`
  records what was *intended* (`backend`). Every report also carries what
  whisper.cpp can *actually* initialise (`backendObserved`), because those
  diverge whenever the CUDA runtime is incomplete — and a CUDA build missing its
  cuBLAS DLLs exits 0 while transcribing on the CPU. A `transcribe` on a machine
  whose GPU fits the model therefore **fails** rather than quietly returning a
  slow CPU transcript. A CPU-only machine runs on the CPU normally; `-NoGpu`
  forces the CPU deliberately.
- **Optional flags are probed, not assumed.** `whisper-cli --help` decides
  whether `-fa` (flash attention) and `-t` (threads) exist before they are
  passed, because an unknown flag aborts the run.

## Layout

```
setup.md                        the agentic DIAGNOSTIC path (fallback when fire-and-forget fails)
scripts/
  zoombie-install.cmd           the ONE entry point: a frozen getter (fetch install.ps1, run it)
  install.ps1                   the core: ensure Python, fetch the repo archive, hand off
  requirements-pdf.txt          Python dependencies for the PDF extractor
  requirements-selftest.txt     extra self-test dependency (pyttsx3, for the TTS step)
  pdf/extract_pdf.py            standalone wrapper over zoombie.lib.pdf
  zoombie/
    __init__.py                 version constants (SKILL_VERSION, MARKER_KEY)
    __main__.py                 python -m zoombie
    cli.py                      argparse + dispatch + the one-JSON-line contract
    selftest.py                 end-to-end test incl. the Cyrillic-path regression
    mcp.py                      the MCP facade over the same command modules
    commands/
      doctor.py  download.py  extract.py  readpdf.py
      transcribe.py  pipeline.py  summarize.py (the step machine)  clean.py
      postprocess.py  verify.py  items.py
      slides.py (video -> slide frames)  readimages.py (images -> Markdown)
      unpack.py (installer/archive -> files)  modes.py  mcp.py (register tooling)
    lib/
      paths.py      env root, ASCII guard, path budget, extended prefix, safe-work copy
      manifest.py   env.json read/write
      tools.py      tool resolution, PATH refresh, Python discovery, pip shims
      process.py    subprocess helpers, logging, the JSON result, cpu threads
      env.py        the resolved runtime environment (absolute tool paths)
      whisper.py    capabilities, device proof, timings, hang-tolerant runner
      cublas.py     pinned cuBLAS redist records + runtime readiness
      stt.py        shared transcribe orchestration (transcribe + pipeline)
      pdf.py        PDF -> Markdown (PyMuPDF4LLM + optional Tesseract OCR / vision)
      slides.py     video -> slide frames (exact timestamps or perceptual dedup)
      ocr.py        Tesseract over a standalone image file
      textnorm.py   text normalization: norm/normalize_with_map, hhmmss, link encoding
      srt.py        SRT parsing + the word-window timing lookup for headings
      markdown.py   range-based, idempotent edits of a generated summary.md
      ytdlp.py      yt-dlp argument assembly
      download.py   HTTP download with resume
      archive.py    zip extraction, long-path aware
      skills.py     skill marker + deployment
      sync.py       content-based reconciliation: hash_tree + plan (writes nothing)
      modes.py      merge the Zoombie role into custom_modes.yaml
      mcpsettings.py  merge the MCP server into mcp_settings.json
      errors.py     user-facing exception types
    install/
      __main__.py   python -m zoombie.install
      main.py       the installer flow (the setup-worker replacement)
      hardware.py   hardware probe + model recommendation
      components.py ffmpeg, yt-dlp, whisper, cuBLAS, model, PDF deps, CLI, skills
      syncfiles.py  apply the hash plan: write changed/missing, remove remote-missing
tests/                          unit tests (python -m pytest tests)
skills/
  zoombie-summarize/SKILL.md          the ONE skill: drives the summarize step machine
  _shared/                            include bodies expanded at deploy time
```

The installed toolchain lives outside the repo, at an ASCII path:

```
%USERPROFILE%\zoombie-env\        (or %PUBLIC%\zoombie-env\ when the profile is not ASCII)
  bin\ffmpeg.exe, ffprobe.exe
  bin\whisper\whisper-cli.exe (+ CUDA/Vulkan DLLs beside it, incl. the cuBLAS
               runtime cublas64_<major>.dll/cublasLt64_<major>.dll, provisioned
               separately because the whisper.cpp asset does not ship it; the
               required major is read from the asset name, not hard-coded)
  bin\zoombie\zoombie.cmd            the stable launcher every skill calls
  bin\zoombie\zoombie\...            the deployed package (lib, commands, install)
  bin\zoombie\pdf\extract_pdf.py     the PDF helper
  models\ggml-*.bin
  work\<guid>\                       ASCII scratch for each job
  tmp\                               ASCII scratch for downloads/extraction
  env.json                           resolved paths, versions, hardware
```

The root must be ASCII because whisper.cpp breaks on non-ASCII paths — not only
for the media, but for its own binary and model too. So the root is chosen
dynamically: `%USERPROFILE%\zoombie-env` when the profile path is ASCII, and
`%PUBLIC%\zoombie-env` when it is not (e.g. a user named `Мария`). `env.json`
records whichever was used, and the skills probe both locations.

The root must also stay **shallow**. whisper-cli loads its sibling `ggml-*.dll` /
`cublas*.dll` through the loader search path and opens `models\ggml-*.bin` with a
plain `fopen`, so neither can carry the `\\?\` prefix the managed code uses. An
over-long root would install cleanly and then fail at the first transcription, so
the installer measures the assembled paths and **refuses to install** into a root
that cannot fit them (limit 240 characters, with margin), naming the numbers and
suggesting a shorter `-Root`. `%USERPROFILE%\zoombie-env` is short by
construction; this only bites when the root is overridden deep in the tree.

Skills are deployed to the global root `%USERPROFILE%\.roo\skills\`. That path
stays under the profile even when the toolchain root moves to `%PUBLIC%`: it is
owned by the editor and is never opened by a native tool, so the ASCII rule that
governs the toolchain root does not apply to it.

The **Zoombie role** is deployed as a Zoo Code custom mode, into the global
`custom_modes.yaml` under the extension's settings folder. It is a universal
analyst — a reasoning partner for finance, medicine, law, politics, science and
anything else — not a coding mode. Its edit permission is deliberately restricted
to human-readable documents and data (`md`, `markdown`, `txt`, `csv`, `tsv`,
`html`, `htm`), so it can author a report but can never touch source code,
configuration or a notebook. When a task needs tooling built or run, it briefs
Code mode as an *Analyst Intern* (facts, method and provenance in; judgement
out). See [`plans/zoombie-role.md`](plans/zoombie-role.md) for the reasoning.

## Quick start

```bat
REM LOCAL DEV: install/update from THIS working tree (no network)
REM detect only (writes nothing)
python -m zoombie.install -Check

REM show the plan (writes nothing)
python -m zoombie.install -DryRun

REM install / update everything from the working tree (idempotent)
python -m zoombie.install

REM verify end to end (includes a Cyrillic-path regression test)
python -m zoombie.selftest
```

Run those from the `scripts\` directory (or with `PYTHONPATH=scripts`), so
`python -m zoombie...` resolves the package.

**End users** do not clone the repo: they run the one file from
[Install on a fresh machine](#install-on-a-fresh-machine). When it fails, a Zoo
task can instead fetch [`setup.md`](setup.md) and follow it to diagnose the
cause. On a machine that already has this checkout, the local equivalents are:

```bat
REM LOCAL DEV: install/update from the working tree (no network)
python -m zoombie.install
python -m zoombie.install -Check
```

## Distributing to other machines

[`scripts/zoombie-install.cmd`](scripts/zoombie-install.cmd) is the single entry
point. It is a **getter**: it fetches [`install.ps1`](scripts/install.ps1) — the
core that ensures Python, fetches the current repository archive, and hands off to
the Python installer — and runs it. So any start of the install means *install or
update to the latest*: there is no cached copy to go stale, no gate that can skip
the update, and because the getter carries no install logic a stale getter still
runs the current core.

The distribution unit is that one file. Save it and double-click it, or fetch and
run it from cmd.exe (no PowerShell needed):

```bat
curl.exe -L -o "%TEMP%\zoombie-install.cmd" https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/zoombie-install.cmd
"%TEMP%\zoombie-install.cmd"
```

Double-clicked from Explorer it holds the window open at the end so the output is
readable; run from a shell, with an argument, or with `ZOOMBIE_NOPAUSE=1` it
returns immediately with the exit code. It works from any working directory.

**The agentic path is the fallback.** When the fire-and-forget run fails, have a
Zoo task fetch and follow [`setup.md`](setup.md): detect -> report -> confirm ->
apply -> self-test. `setup.md` explains each failure mode the entry point can
print, and the manual `python -m zoombie.install` route from a local checkout.

Every option has an environment-variable form so an unattended run can select it:
`ZOOMBIE_CHECK`, `ZOOMBIE_DRYRUN`, `ZOOMBIE_MODEL`, `ZOOMBIE_ROOT`,
`ZOOMBIE_FORCE`, `ZOOMBIE_NOPAUSE`, plus `ZOOMBIE_REPO_SLUG` / `ZOOMBIE_REPO_REF`
for a fork or branch. A switch wins over its variable.

What travels in the repo vs. what each machine rebuilds:

| Thing | Travels? | Why |
|-------|----------|-----|
| `scripts/`, `skills/`, `modes/`, `setup.md` | yes | the implementation and the sources; the installer fetches these itself |
| `%USERPROFILE%\zoombie-env\` (or `%PUBLIC%\zoombie-env\`) | no | machine-local and large (the model alone can be ~1.5 GB); re-fetched so it matches each machine's GPU backend, and re-homed to `%PUBLIC%` when the profile is not ASCII |
| `%USERPROFILE%\.roo\skills\` | no | deployed copies, written from `skills/` by the installer |
| `%APPDATA%\Code\User\globalStorage\zoocodeorganization.zoo-code\settings\custom_modes.yaml` | no | the Zoombie role is **merged** into it (only our entry is replaced; foreign modes are preserved), so a hand-written mode there is never lost |
| `%APPDATA%\Code\User\globalStorage\zoocodeorganization.zoo-code\settings\mcp_settings.json` | no | the `zoombie` MCP server is **merged** into it (only our entry is replaced; other MCP servers are preserved), so the server appears in the client without a manual config |

On a machine that is **already configured**, nothing further is needed: the
deployed launcher under the ASCII root (`%USERPROFILE%\zoombie-env\`, or
`%PUBLIC%\zoombie-env\` on a non-ASCII profile) is self-contained -- it carries
its own package and `pdf\` -- and the six `zoombie-*` skills live in the global
root, so "transcribe this video" and "convert this PDF" work from any workspace.

The entry point honors environment overrides, so a fork or branch can be used
without editing anything:

```bat
set ZOOMBIE_REPO_SLUG=someone/zoombie   REM default: carnivorum/zoombie
set ZOOMBIE_REPO_REF=dev                REM default: main
```

## The CLI

`zoombie` prints exactly one JSON result line per call:
`{ ok, action, data, error, timestamp }`. Human-readable progress goes to stderr.

```powershell
# the toolchain root moves to %PUBLIC% when the profile path is not ASCII
$zoombie = @(
    "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.cmd",
    "$env:PUBLIC\zoombie-env\bin\zoombie\zoombie.cmd"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

# The ADVERTISED MCP tools -- the small agent-facing surface.
& $zoombie summarize -Source "<url-or-file>"              # the front door; follow data.next
& $zoombie download -Source "<url>" [-DownloadDir "<dir>"] [-Name "<name>"] [-AudioOnly] [-Format wav]
                                                          # default dest: _unsorted/download/<name>
& $zoombie extract  -Source "<video>" -Output "<out>" [-Format wav|mp3|m4a|flac]
& $zoombie readpdf  -Source "<pdf>" -Output "<basename>" [-Ocr | -Vision "<dir>"] [-Images] [-ImagesOnly] [-ImageDir "<dir>"] [-MinPx N] [-MinPt N] [-Pages "1-5,8"]
& $zoombie readimages -Source "<image-or-dir>" -Output "<basename>" [-Ocr] [-Lang eng] [-ImageDir "<dir>"]
& $zoombie unpack   -Source "<archive>" -Output "<dir>" [-Check] [-Strip N] [-Include "*.dll"]
                                                          # any archive 7-Zip reads (7z/zip/rar/tar/gz/xz/iso/...), or an NSIS installer
& $zoombie items    [-Root "<dir>"] [-Depth N] [-Title "<title>"]    # read-only: what summaries exist
& $zoombie doctor                                         # read-only: is the toolchain usable

# CALLABLE but NOT advertised -- driven by summarize, or one-time/maintenance.
& $zoombie pipeline -Source "<url-or-file>" -Output "<item-folder>" [-DownloadDir "<dir>"] [-NoSrt]
& $zoombie transcribe -Source "<audio>" -Output "<item-folder>" [-Language auto] [-NoSrt] [-NoGpu] [-Threads N]
& $zoombie slides -Source "<video>" -Output "<item-dir>" [-Times "00:01:00,00:05:30"] [-Scale N]
& $zoombie postprocess -Md "<summary.md>" [-Srt "<file>"] [-ImageDir "<dir>"] [-Apply]   # anchors, timestamps, index, images
& $zoombie verify   -Dir "<library-root>" [-Recurse] [-Json]                               # exit 1 on problems
& $zoombie clean                                          # remove per-job scratch dirs
& $zoombie modes | mcp                                    # one-time setup (setup runs these)
```

`tools/list` advertises only the small agent-facing set; the rest are still
honoured by `tools/call` (a skill or a power-user can name one), but they are not
offered to the model, because they either duplicate the summarize flow or are
setup/maintenance verbs.

Every stage puts its per-run scratch under the toolchain root and removes it when
it finishes; `-KeepScratch` (alias `-KeepWork`) retains it and the result reports
it in `data.scratch`. `clean -CleanScratch` removes only dirs carrying our run
marker -- a folder of yours in a root is reported, never deleted.

Every subcommand accepts `-DryRun` (plan only) and most accept `-Force`.
Long options also work in their conventional spelling (`--source`, `--output`),
so the CLI is comfortable from a non-Windows-style invocation too.

Two commands are **dry run by default** and write ONLY with `-Apply`, because a
skill calls them and must never be able to corrupt a document by accident:
`postprocess`.

### Transcription output: into the destination folder

`-Output` on `transcribe` and `pipeline` names a **destination folder**, not a
basename; the artifacts (`transcript.txt`, `transcript.srt`, `source.json`) land
directly in it. In the summarize flow that folder is the run scratch, so the
artifacts are throwaway; a bare `transcribe` writes them where the user named.

`transcribe` and `pipeline` emit subtitles **by default**:

| Flag | Effect |
|------|--------|
| *(none)* | `transcript.srt` is written beside the transcript |
| `-NoSrt` | suppress the `.srt` (the timings are then lost) |
| `-Srt` | **legacy no-op alias**, kept so existing callers keep working — it can never remove the default |

Every run also writes a `source.json` origin sidecar recording where the input
came from. `pipeline` exposes no `-Format`: its audio is always a 16 kHz mono WAV,
which is the only thing whisper.cpp consumes. `-Format` lives on `download` and
`extract`, where it is honoured.

### `readpdf`: images and their sidecar

`readpdf` can also extract the embedded images:

| Flag | Effect |
|------|--------|
| `-Images` | extract images (and render the Markdown) |
| `-ImagesOnly` | extract images and the sidecar only; render no Markdown (and skip the `.md` guard) |
| `-ImageDir <dir>` | explicit image directory (default `<base>.images`); implies extraction |
| `-MinPx N` | drop images below this pixel size |
| `-MinPt N` | drop images below this on-page size in points |
| `-Vision <dir>` | render pages with **no text layer** to PNG in `<dir>` for a vision reader (text pages are never rendered; ignored when `-Ocr` is given) |
| `-Dpi N` | render dpi for `-Vision` (default 200) |

When images are extracted, `manifest.json` and `README.md` are written **into the
image directory** (`img\` when the summarize workflow relocates it). The manifest
records placement metadata; `postprocess` reads it to re-insert the images, and
`verify` reports `missing-manifest` / `missing-readme` when either file is absent.

**Reading policy: text first, vision/OCR only for a page with no text layer.** A
PDF with extractable text is read by PyMuPDF4LLM and never rasterized, so it costs
zero image tokens. A page with no text layer is escalated per page — `-Ocr` for a
deterministic Tesseract result, or `-Vision <dir>` to render it for the model. A
run that leaves scans unresolved reports them in `data.keptScannedPages`, so a
skipped scan is never silent.

### `slides`: a video's slide frames, with a placement manifest

`slides` extracts the slide images out of a video and writes the same kind of
sidecar `readpdf` does, so `postprocess` inlines them into `summary.md` block 6.
Each slide's `anchor_text` is the narration spoken during its interval, read from
the sibling SRT — that is the join between the audio and the video.

| Mode | Flag | Effect |
|------|------|--------|
| exact timestamps | `-Times "00:01:00,00:05:30"` / `-TimesFile <path>` | one frame per timestamp, no detection and no dedup — the minimal set |
| auto-detect | *(default)* | sample, dedup with a perceptual hash (dHash + Hamming), keep the last frame of each stable run |

`data` reports `mode`, `candidateFrames`, `keptFrames` and `intervals`, so the
dedup ratio is measurable per run rather than assumed. `-HashDistance` and
`-MinSlideSec` are the two thresholds; a higher distance and a longer minimum both
lower the kept count.

### `readimages`: an image or a folder of images

`readimages` is the image-input half of the same capability. Without `-Ocr` it
writes the Markdown image links plus the sidecar and leaves the reading to the
model (`data.visionOnly: true`); with `-Ocr` it runs Tesseract and renders the text
under each image. It never guesses text.

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
not a warning. The GPU-retry path is also narrow:

- the CPU retry only fires when a non-zero exit *looks like* a GPU failure
  (`cuda`, `cublas`, `out of memory`, `driver`, …). Unrelated errors — a missing
  model, an unsupported codec — no longer trigger a full CPU re-run that hides
  the real cause;
- the abandoned GPU attempt's wall time is reported as `gpuAttemptWallMs`;
- the two attempts log to separate files, so the GPU error survives.

Opt out explicitly when a CPU run is what you actually want:

```powershell
& $zoombie transcribe -Source "<audio>" -Output "<base>" -NoGpu            # deliberate CPU run
& $zoombie transcribe -Source "<audio>" -Output "<base>" -AllowCpuFallback # permit a CPU fallback
& $zoombie transcribe -Source "<audio>" -Output "<base>" -StrictGpu        # also require POSITIVE GPU proof
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

The installer therefore provisions the matching cuBLAS runtime separately, from
NVIDIA's redist archives — one deterministic record with one published sha256 per
component, which is what makes the download reproducible without installing the
CUDA Toolkit. The required **major** is derived from the selected asset name
(`whisper-cublas-11.8.0-bin-x64.zip` → cuBLAS 11), so an asset built against a
different CUDA major needs a different runtime instead of silently reusing the
wrong one. A major with no pinned, hash-verified redist is **refused** rather than
downloaded unverified. The archive's sha256 is checked before anything is copied
next to the binary, and a CUDA install whose runtime is still incomplete is
reported as missing (by exact DLL name) and fails loudly rather than being left
silently CPU-only.

Asset selection is provision-aware rather than merely newest-first: releases are
walked newest-first and the first cuda asset whose major has a pinned redist is
chosen, so a future CUDA-12-only release cannot be installed on a machine that can
only provision CUDA 11. If only unprovisionable assets exist, the install refuses
BEFORE downloading, naming the asset, its major and the supported majors.

The install also stops trusting a sticky `env.json`: the backend recorded there
is compared with the **current** hardware probe, and a machine that gained or
lost a GPU gets the matching build reinstalled instead of keeping the old one.

One exception, which is what keeps a non-NVIDIA machine from re-downloading on
every run: when no asset exists for the detected backend, the CPU build is a
recorded **substitution** (`whisper.backendSubstituted` + `whisper.requestedBackend`).
The next run sees the same substitution holds and keeps the build, rather than
treating "cpu installed, vulkan detected" as a mismatch and downloading again.

The chooser is capability **and benefit**: `cuda` for an NVIDIA GPU; `vulkan` only
for a discrete GPU with enough dedicated VRAM and a current driver; `cpu`
otherwise — including integrated GPUs (Iris/UHD, AMD iGPU), which share the system
memory bus and would contend with the CPU for bandwidth. The reason is recorded in
plain language at `whisper.backendReason`, and `gpuPolicy.gpuIgnored` is true when
a GPU is present but the CPU will actually run. Set `ZOOMBIE_ALLOW_IGPU_VULKAN=1`
to force Vulkan on an integrated GPU.

To check the state at any time:

```bat
REM names the exact missing DLLs when a CUDA install cannot initialise
python -m zoombie.install -Check

REM backendConfigured vs backendObserved, with a warning on mismatch
REM the launcher is under %PUBLIC% on a non-ASCII profile; probe both
set "ZOOMBIE_BIN=%USERPROFILE%\zoombie-env\bin\zoombie\zoombie.cmd"
if not exist "%ZOOMBIE_BIN%" set "ZOOMBIE_BIN=%PUBLIC%\zoombie-env\bin\zoombie\zoombie.cmd"
"%ZOOMBIE_BIN%" doctor
```

## Skills

The six skills in [`skills/`](skills/) are the canonical sources. They only
inspect the project, propose paths, collect the user's confirmation, and then
call the CLI. They are namespaced `zoombie-*` so their names cannot collide with a
foreign skill, and they carry `cvrm-zoombie-version: 5.2.0`. The marker is the
ownership proof used to prune a retired skill; the "up to date" decision itself is
made by comparing the **expanded bytes**, so a changed shared block redeploys
without a version bump and `-Check` can report a stale skill.

Five blocks repeat across all six - how to resolve the launcher, the JSON
contract, the repo fallback, the shell note and the scratch-dir rule - so they live once in
[`skills/_shared/`](skills/_shared/) and a source marks the include site with
`<!-- zoombie:include <name> -->`. **Deployment expands those markers**, so the
installed skill is self-contained and an agent never resolves an include at
runtime. The action is decided by comparing the expanded text, so editing a
shared block redeploys without a version bump.

There is exactly **one** skill, `zoombie-summarize`. The individual stages —
download, extract, transcribe, PDF/image → Markdown — survive as **MCP-only
tools**, and the summarize flow drives them for you, so an agent never chains
skills by hand.

| Skill | Produces |
|-------|----------|
| `zoombie-summarize` | a 6-block `summary.md` — the **front door** and the only skill: a step machine (`source` → `name` → `slides` → `prose` → `verify`) that downloads/transcribes/renders a raw source itself, splits the verbatim block-6 copy from the transcript, archives any existing summary, and verifies the result |

`zoombie-images-to-md` converts a PDF, a loose image, or a folder of images to
Markdown. **A text PDF is read directly and costs nothing** — only a page with no
text layer is escalated, to `-Ocr` (deterministic Tesseract) or to `-Vision` (a
rendered page the model reads). It reuses the Python this repo already requires
(the `pymupdf4llm`/`pytesseract` dependencies are installed into it with
`pip --user`), and the source is copied into an ASCII scratch dir first, so the
Cyrillic-path invariant holds for PyMuPDF exactly as it does for whisper.cpp.

`zoombie-summarize` is the **front door** for any document: given existing source
material (a transcript, a PDF-derived Markdown, or text) it turns it into a 6-block
`summary.md`, and given a RAW source (a video, an audio recording, a PDF, or images)
it produces that material first — one skill, no hand-rolled chain. An item is a
folder whose name is
the user's own choice — it is never parsed — laid out as:

```
<item>/                  any name
    summary.md           the document, at the root
    <source media>       the video/audio/PDF, when kept
    img/                 the figures the document inlines (VISIBLE)
```

Only those three things survive a run. The transcript, the `.srt`, the origin
sidecar and the OCR report are throwaway: they live in a run scratch under the
workspace `/.tmp/zoombie-summarize/<run>/` and are deleted at the verify step.
An existing `summary.md` is never destroyed — it is archived to
`summary_<yyyyMMdd_HHmm>.md` and the path reported. The title is the document's
own H1. `zoombie items` measures the directory's naming and reports the convention,
its confidence and the next number, so a new name follows the directory's own
convention and falls back to `<DD.MM.YYYY> - <title>` only with no evidence. Block
6 is a **verbatim copy** of the source with recognition artefacts cleaned out, not
a recap, and its heading says so.

It writes the prose itself; everything mechanical -- anchors, heading timestamps
from the SRT, the regenerated block-4 index, link repair and inline-image
re-insertion -- is done by `zoombie postprocess`, so a re-run cannot drift.

| Command | Does |
|---------|------|
| `zoombie items` | scan a workspace for items; one JSON line, read-only |
| `zoombie items -Depth N` | how many levels below the root to search; `1` is the default |
| `zoombie items -Recurse` | alias for `-Depth 2`: also look inside non-item subfolders |
| `zoombie postprocess` | the mechanical passes over a `summary.md` |
| `zoombie verify` | self-check a tree; advisories are reported but do not fail it |

**Depth.** A scan reads one level below the root by default, because a library is a
folder of items. `-Depth N` widens it: each level of non-item folder between the
root and an item costs one, so `a/b/item` needs `-Depth 3`. `-Recurse` is an alias
for `-Depth 2`. A directory that is itself an item is never descended into — its
`.data/` is its internals, not a nested workspace — and every item found below the
root carries a `relative` path, so a caller never has to rebuild one.

The confidence in the naming verdict is capped to `weak` unless the items are at
least half of the folders measured, because a directory of ordinary source folders
is "plainly named" too: without the cap, `items -Root .` on a repository with no
items at all would report a strong convention and propose names for a folder that
holds none.

## Hacking

- Shared logic belongs in `zoombie/lib/`. One module per concern; no `Zoombie`
  prefix and no Verb-Noun names, because the module path already namespaces them.
- **Never call the filesystem directly in the pipeline paths.** Use the
  long-path-aware helpers in [`paths.py`](scripts/zoombie/lib/paths.py) —
  `exists`, `is_file`, `is_dir`, `ensure_dir`, `remove`, `move`, `copy_file`,
  `copy_tree`, `file_size`, `list_dir` — which apply the `\\?\` prefix for you.
  Note that `list_dir` returns `Entry` objects with PREFIX-FREE paths, precisely
  so a `\\?\` path can never leak into JSON output or into a native tool call.
- Use `paths.absolute` when a path is about to be handed to a **native** tool
  (ffmpeg, whisper-cli, python): it returns a prefix-free absolute path. Pair it
  with `paths.assert_fits(p, what, slack=n)`. Always pass `slack` for a name that
  gets a suffix appended (`.txt`, `.images`, `.whisper.log`), because the suffix
  is what usually crosses the limit.
- Install/update logic belongs in `zoombie/install/`. The entry point is
  [`zoombie-install.cmd`](scripts/zoombie-install.cmd) — a frozen getter that
  fetches [`install.ps1`](scripts/install.ps1) (the core: ensure Python, fetch the
  repo, hand off) and runs it. The getter holds no install logic, so it cannot go
  stale; the core is always fetched fresh, so it can gain steps freely. There is
  no separate "fetch the latest worker" module.
- When iterating locally, run `python -m zoombie.install` — it installs the
  working tree as-is and never hits the network. The getter is the end-user path
  and always pulls the published repo.
- **Updates are content-based.** The installer hashes the fetched tree and the
  installed tree ([`lib/sync.py`](scripts/zoombie/lib/sync.py)) and writes the
  difference, removing files the source dropped. Skills are compared on their
  **expanded** bytes, so editing a shared block redeploys without a version bump.
- Bump `SKILL_VERSION` in [`__init__.py`](scripts/zoombie/__init__.py) when skill
  content changes. It is currently `5.2.0`; every `SKILL.md` carries the same value
  in `cvrm-zoombie-version`, inside the first 12 lines — the marker is the
  ownership proof used to prune a retired skill, and `read_marker` reads only the
  front matter, so a marker that drifts below it reads as unowned.
- **A comment must earn its place.** Keep one that prevents a realistic future
  regression or explains a non-obvious constraint (why a `\\?\` path must never
  reach JSON, why a bare `customModes:` is not an empty list). Delete history:
  what the code used to be, what it replaces, and any rationale that is already
  in this README. The reader is often an agent paying per token, and it cannot
  act on either.
- The PDF dependencies are installed with `pip install --user`, matching the
  comment in [`requirements-pdf.txt`](scripts/requirements-pdf.txt). pip also
  writes console launchers into `%APPDATA%\Python\<ver>\Scripts`, which this
  toolchain never calls, so the installer snapshots that folder first and removes
  only the shims its own install created (anything pre-existing is never touched).
- `yt-dlp` is installed as a Python package and invoked as `python -m yt_dlp`. It
  is pure Python and therefore ASCII-path safe (unlike whisper.cpp), so it needs
  no local copy and no ASCII isolation, and running it as a module avoids PATH
  and launcher-shim issues entirely.
- Run `python -m zoombie.selftest` and `python -m pytest tests` after any change
  that touches the pipeline.

# Subtask J — regressions from the other machine's report

Source: [`feedback/report (1).md`](report%20%281%29.md) (Windows 11 machine, zoombie 6.0.0).
Version: **6.1.0**, ROLE_VERSION **1.4.0**. Suite: **1062 passed** (was 1040 before
this change; +20 in [`tests/test_regressions_report1.py`](../tests/test_regressions_report1.py)
and +2 MCP-group guards in [`tests/test_modes.py`](../tests/test_modes.py:260)).
Self-test: **green end to end** (extract, transcribe on CUDA at `realtimeFactor`
0.85, `-NoGpu` on CPU, PDF->Markdown, postprocess idempotency, Cyrillic path).
`install -Check`: `registered: true`, `serverProbe.ok: true` (`zoombie-mcp 6.1.0`),
`available: true`, `missing: none`.

## The premise check first

The task arrived as "the installer does not install `mcp`; the same happened on
another PC". Two hypotheses were tested against primary sources and **rejected**:

| Hypothesis | Verdict | Evidence |
|---|---|---|
| the `mcp` Python package is missing | **rejected** | the facade is its own JSON-RPC server; [`mcp.py`](../scripts/zoombie/mcp.py:53) imports stdlib + `zoombie.lib` only, and a repo-wide search for `import mcp` / `mcp.server` / `stdio_server` / `Server(` returns **0** hits. Installing the package would change nothing. MCP tool calls work in this very session. |
| the entry needs `"type": "stdio"` (report §6.1) | **rejected** | the active client here is **3.84.0** ([`.obsolete`](../../../.vscode/extensions/.obsolete:1) drops 3.82.2), its [`mcp_settings.json`](../../../AppData/Roaming/Code/User/globalStorage/zoocodeorganization.zoo-code/settings/mcp_settings.json) has **no** `type`, and the tools connect. The 3.84.0 bundle contains no `streamableHttp` at all, so there is no transport to disambiguate. |

So there is no installer change "to install `mcp`". Two REAL defects were found and
fixed: **`registered: true` proved only that an entry is in a JSON file**, and — the
one that actually killed the flow on both machines — **the Zoombie role never granted
the `mcp` group**, so the client never offered the tools to that mode at all.

## What was changed

| Area | File | Change |
|---|---|---|
| **Role grants `mcp`** | [`modes/zoombie.yaml`](../modes/zoombie.yaml:82) | **THE root cause.** The role's `groups` listed `read`/`command`/`modes` only; without the `mcp` group the client never OFFERS the MCP tools to this mode, so the summarize flow was dead in Zoombie mode however well the server was registered. Added `- mcp`, with `allowedMcpServers` deliberately left OMITTED (the client reads an absent value as "any server"; a list would silently exclude a server the user adds later). Deployed live: the global `custom_modes.yaml` now carries `- mcp` and `modes -Check` reports `up to date`. |
| Deterministic scratch | [`commands/summarize.py`](../scripts/zoombie/commands/summarize.py:76) | `_new_run()` (uuid) replaced by `_run_key()` + `_run_for_source()`. The name is `sha256(canonical source)[:32]`, so a re-run **reuses** the directory instead of stacking a second one. Shape stays 32 lowercase hex because [`scratch.is_run_dir`](../scripts/zoombie/lib/scratch.py:59) recognises scratch by exactly that — any other shape would make the dir invisible to `clean -CleanScratch` and leak it forever. |
| Failure names its scratch | same | `_attach_scratch()` reports `data.run` + a `data.scratch` block on a failed step 0. The scratch is still KEPT (it holds the partial download and is what makes the failure diagnosable), but it is no longer invisible. The hint names RESUME or a manual delete, deliberately NOT `clean -CleanScratch`: that sweep covers the toolchain roots only, and a run dir under the workspace `/.tmp` is not its target (pinned by [`tests/test_scratch.py`](../tests/test_scratch.py:134)) — pointing at a verb that would spare the directory is worse than naming none. |
| yt-dlp diagnosis | [`lib/ytdlp.py`](../scripts/zoombie/lib/ytdlp.py:136), [`commands/pipeline.py`](../scripts/zoombie/commands/pipeline.py:179) | `failure_detail()` returns the last non-empty (clipped) output line; the pipeline error becomes `yt-dlp failed (exit 1): ERROR: ...` instead of a bare exit code. |
| One media file per item | [`commands/pipeline.py`](../scripts/zoombie/commands/pipeline.py:112) | When the download already sits in the item, `_retain_source` **renames it in place** to the sanitized name instead of copying, so the item holds one file rather than the observed two spellings of one title. |
| Startability probe | [`lib/mcpsettings.py`](../scripts/zoombie/lib/mcpsettings.py:233), [`install/components.py`](../scripts/zoombie/install/components.py:904), [`install/main.py`](../scripts/zoombie/install/main.py:222) | `probe_server()` spawns the REGISTERED argv (`<command> -m zoombie.mcp --version`) with the registered env. Recorded as `manifest.mcp.serverProbe` and `manifest.mcp.available`; a registered-but-dead server warns at install and `-Check` lists `mcp-server` in `missing`. Check/DryRun read the file and PROBE it too (spawning writes nothing), so `-Check` reports the real verdict rather than `registered: false, missing: none`. |
| Client-log noise | [`mcp.py`](../scripts/zoombie/mcp.py:1162) | the readiness banner is logged at `info`, not `step`, so a launch no longer looks like an error in the client log (report §6.2). stdout stays protocol-only. |

## Reproducibility of the name rule (report §4.3)

The two-variant finding is explained: `--windows-filenames` strips ASCII-illegal
characters but **not their fullwidth twins**, so a title ending in `？` (U+FF1F)
downloads as `...？.mp4`; [`_sanitize_media_name`](../scripts/zoombie/commands/pipeline.py:36)
folds it to ASCII `?` and removes it. Two spellings, one video. The fix makes the
item hold the sanitized name ONLY (in-place rename), so the outcome is one rule, not
two.

## yt-dlp version (report question)

`yt-dlp 2026.8.19` is a plain date version, not a `-dev`/nightly build. It is **not**
pinned in this repo, and that is deliberate: yt-dlp tracks site changes, so a pin goes
stale. The install path stays `pip install yt-dlp` (latest).

## What is NOT fixed here (and should not be)

**Defect 1 had TWO causes, and the second was OURS.**

1. **The role did not grant the `mcp` group.** [`modes/zoombie.yaml`](../modes/zoombie.yaml:79)
   listed `read`/`command`/`modes` only. Without the `mcp` group the client never
   OFFERS the MCP tools to this mode, so the summarize flow was dead in Zoombie mode
   no matter how perfectly the server was registered — which is exactly why the
   symptom looked like a broken MCP configuration on two machines. Fixed: the role
   now grants `mcp`, and `allowedMcpServers` is deliberately left OMITTED because the
   client reads an absent value as "any server" (an explicit list would silently
   exclude a server the user adds later). The 3.84.0 bundle confirms the flag: an
   `undefined` allow-list is allowed, otherwise membership is required.
   ROLE_VERSION **1.4.0**; the role is delivered by the modes merge, so a re-run of
   setup applies it.

2. **Task-time tool-list freezing (client-side, outside this repo).** The report's
   §3.3 mechanism — the client fixes its MCP tool list when a task STARTS — still
   stands for a task created while the server was not yet up.

The report's decisive step 5 ("create a NEW task — do the tools appear?") is therefore
now expected to PASS for the role reason alone. The install also proves the server
STARTS (`manifest.mcp.available`) and [`setup.md`](../setup.md:364) documents both.

## Remaining uncertainty

* The probe proves the server starts on the INSTALL machine with the registered env; it
  cannot prove the client will render the tools (that half is the client, as Subtask G
  already states).
* `_run_for_source` reduces the leak but does not sweep a HISTORICAL pile of uuid dirs
  already on disk from 6.0.0 runs; those are removed by `clean -CleanScratch`.
* The concurrency consequence is accepted deliberately: two `source` runs for the SAME
  input share one directory now. Two runs over DIFFERENT sources stay isolated. Both
  are interactive agent flows, so simultaneous same-source runs are not a supported
  case.

# Toolchain friction — what went wrong and what would make it stable

Below is everything that cost time or produced a wrong result in this run, grouped by root cause, with the evidence and a concrete fix for each. "Confirmed" means I saw it in output; "likely" means inferred from the symptom.

## A. The environment layer (the dominant cost): Cyrillic through `cmd.exe`

**A1 — Cyrillic arguments cannot be passed through the shell reliably. Confirmed.**
Chosen item folder `Могилко-телеграм-канал`. Passing the Cyrillic `-Output`/`-DownloadDir` inline would have been mangled by the console codepage, so I had to write the name to [`itemname.txt`](itemname.txt:1) and have PowerShell read it back with an explicit UTF-8 decoder. That is a workaround a caller should not need.
*Fix:* let the CLI accept an `@file` argument (a UTF-8 file containing the value) for `-Output`, `-DownloadDir`, `-Source`; and/or document the "stage the value in a UTF-8 file" pattern as first-class.

**A2 — Quoted absolute Windows paths get doubled by the shell shim. Confirmed.**
`"C:\Users\Public\zoombie-env\bin\yt-dlp.exe" …` arrived as `""C:\…\yt-dlp.exe""` and cmd answered «не является внутренней или внешней командой». Unquoted (space-free) paths worked.
*Fix:* make the `.cmd` shim robust to a wrapped argv0, or ship a `.ps1`/`.exe` launcher so quoting is not the caller's problem.

**A3 — Reading UTF-8 tool output is unreliable from the agent shell. Confirmed.**
yt-dlp's title came out as `╧юўхьє…` / `      …` no matter what I did: `[Console]::OutputEncoding = UTF8`, `chcp 65001`, `PYTHONIOENCODING`, `Out-File -Encoding utf8`, `WriteAllText(..., UTF8)`. The **only** reliable path was `yt-dlp --write-info-json` (the child writes the file) plus `ConvertFrom-Json`, then dumping to a file and reading it with the file tool. Two independent fallbacks (stdout capture and `WriteAllText`) both produced mojibake.
*Fix:* have every subcommand that reports a title write a UTF-8 sidecar (the pipeline already can, via the sidecar) and treat stdout as display-only. Document "never parse Cyrillic from stdout".

## B. Pipeline correctness (silent degradation of the origin record)

**B1 — `sourceKept:false` / `sourceFile:null` although the media WAS kept. Confirmed.**
[`transcript.source.json`](Могилко-телеграм-канал/transcript.source.json:15) says `sourceKept:false` and `sourceFile:null`, yet the `.mp4` sits in the item folder. The run logged:
`WARN source not retained beside the transcript: '<path>' and '<path>' are the same file`.
The retention check compares source vs destination and treats *identical* paths as "not retained". When `-DownloadDir` equals the item folder (the natural layout), `src == dst`, so it always warns and then records a false negative. This directly contradicts the sidecar's own purpose ("the origin survives the deleted media" — here the media was *not* deleted and the sidecar still denies it).
*Fix:* short-circuit when the fully-resolved `src` and `dst` are equal — that is retention (or a no-op to move), not a warning — and set `sourceKept:true`, `sourceFile:<name>`.

**B2 — `durationSec:null` although the duration was captured. Confirmed.**
The sidecar has `durationSec:null` and `realtimeFactor:null`, while the same invocation printed `duration=2021` from yt-dlp and `audioDurationSec:2020.79` from the pipeline. Known, in-hand values were dropped on the way to the sidecar.
*Fix:* persist `durationSec` from the `--print after_move:` metadata and `realtimeFactor` from the transcriber. A missing *metadata line* should degrade the field, but here the line existed.

**B3 — Spurious WARN noise. Confirmed.** B1's warning prints the *same path twice* and calls it "not retained". Even after the logic fix, the message should not fire on the normal same-folder layout.

## C. `verify` false positives — the biggest stability blocker

**C1 — `verify` rejects the library's own link style. Confirmed.**
`verify -Dir .` exited **1** with **74 problems**, *all* of them dead-links inside [`сравнение-трёх.md`](сравнение-трёх.md:1) — a file I did not touch. Every one is a GitHub-style line-suffixed link such as `[`Безруков/summary.md`](Безруков/summary.md:1)`; the resolver treats the `:1` as part of the filename and fails. The zoomie skills themselves *mandate* the `file.ext:LINE` form, so the checker flags the house style as broken. Net effect: `verify` cannot be used as a gate for this library at all, and a real breakage would drown in the noise.
*Fix:* strip a trailing `:<digits>` from the destination before resolving (and, if intended, verify the line number separately); accept relative paths with or without a leading `./`. Then re-baseline `сравнение-трёх.md` rather than living with 74 permanent warnings.

**C2 — New item was clean.** Confirmed: searching the verifier artifact for the new folder name returned no matches, so all 74 are pre-existing.

## D. Layout and documentation drift

**D1 — Written layout ≠ documented layout. Confirmed.**
The transcribe skill documents `.data/transcript.txt`, `.data/transcript.srt`, `.data/source.json`. The pipeline actually wrote them **directly in the item folder**. `postprocess` still found the SRT (it fell back to the sibling `transcript.srt` — `timestamped=17`), so it works, but the transcribe-skill diagram and the summarize-skill diagram disagree about where derived material lives.
*Fix:* pick one shape, write it, and align both SKILL.md diagrams.

**D2 — Legacy items vanish from the index. Confirmed.**
`index`/`items` report `skipped: Могилко -- no summary.md and no .data/ directory`. A pre-existing, human-curated item ([`Могилко/rutube-60c1d660.md`](Могилко/rutube-60c1d660.md:1)) is therefore invisible in [`README.md`](README.md:1).
*Fix:* include unsummarised folders in the index with a status column, or emit an explicit "not an item" report rather than silently skipping.

**D3 — `nextNumber` is misleading under a title-only convention. Confirmed.**
`items` printed `naming: <title> (confidence=strong, samples=4/4)` and `next number: 1` in the same breath. For a title-only convention the number is meaningless; printing it invites a caller to invent one.
*Fix:* suppress `nextNumber` when the measured convention is `title-only`.

**D4 — `items` display placeholder. Confirmed (cosmetic).** Lines render as `-: - «title»` because number and date are null; show the folder name instead.

## E. Quality / defaults

**E1 — Silent model downgrade. Confirmed.** This run used `ggml-small`; the sibling item [`психология/summary.md`](психология/summary.md:4) was produced with `ggml-large-v3-turbo`. Small is visibly worse here — the transcript is littered with mangled proper nouns («Кателин Гольд», «Голдон Чехуахуа», «Нейтеребидау»). Nothing told me a lower-quality default was in play.
*Fix:* if a previous run in the library used a larger model, say so; or surface the model choice in the confirmation step.

**E2 — Mangled, awkward media filename. Confirmed (cosmetic).**
The kept file is `Почему тебе стоит создать свой телеграм-канал уже сегодня？ [441eda3574ca43d0ee.mp4` — a **fullwidth** `？` (U+FF1F), no `.mp4` before the hash suffix, and a title truncated mid-hash. It forced a percent-encoded link in block 2.
*Fix:* sanitize to an ASCII-safe, NFC, quote-free name (and normalise fullwidth punctuation), capped at a clean boundary.

## F. What was actually fine

- `postprocess` was **byte-idempotent**: the second `-Apply` returned `changed:0` at an identical 38181-byte size.
- All 17 block-6 headings matched the SRT (`unmatched:0`), and the regenerated block-4 index resolved 17 `#s-N` anchors.
- No GPU is present (`gpuCapable:false`), so the CPU run is the correct path, not a silent fallback — the toolchain's GPU-guard behaviour was not exercised.

## Priority

1. **C1** — `verify` is unusable as a gate on this repo until line-suffixed links resolve. Highest value, smallest fix.
2. **B1 + B2** — the origin sidecar silently records false negatives; this is the exact failure the sidecar exists to prevent.
3. **A1–A3** — Cyrillic on Windows is the biggest time sink; an `@file` argument plus "never parse Cyrillic from stdout" would remove most of it.
4. **D1/D2** — align the layout diagram and index legacy items.
5. **E1/E2** — quality defaults and filename sanitisation.

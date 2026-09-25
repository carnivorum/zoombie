# Crimson item 4 — defect brief

Consolidates the six defects reported for the run on
`C:\Users\maxim\source\repos\kb\Crimson\4 - 09.04.2020 - Дивиденды во время чумы и после нее - принципы выбора акций`,
with the mechanism of each traced to source and the disagreement between the
reported symptoms and the artifacts resolved.

**Sources.** The user's bug report (first message of the thread); the item's own
artifacts (`summary.md`, `.data/`); the toolchain source in this repo; and file /
directory timestamps measured with `Get-Item` / `Get-ChildItem`. Nothing here is
reconstructed from memory. Where a conclusion is an inference rather than a
reading, it says so.

**Confidence labels.** *Confirmed* = read in source or measured on disk.
*Inferred* = follows from evidence but not directly observed.

---

## Corrections to the earlier analysis

Recorded first, because three of them changed the recommendation.

1. **`-nf` / `--no-fallback` is not a repetition mitigation.** An earlier pass
   listed it among the unused anti-loop knobs the capability probe already
   detects. In whisper.cpp `--no-fallback` **disables temperature fallback**,
   which is the built-in defence *against* repetition loops — enabling it would
   make this failure worse. Withdrawn.
2. **The timestamps corroborate the user's account; they do not establish it.**
   An earlier pass presented the directory mtimes as a discovery of the file
   move. The move was already stated in the bug report. The timestamps add only
   the *time* of the move (`.data` CreationTime `25.09 00:01:24`, ten minutes
   before `summary.md` at `00:11:22`).
3. **The timeline does settle the item's assembly.** An earlier pass said it did
   not, and speculated about a "pre-item-layout shape". The measured directory
   ages place the sequence precisely; see *Provenance* below.
4. **Duration is 1:44:18, not 1:43.** `source.json` records
   `durationSec: 6258.431688`. The summary's own last heading is `01:44:00`,
   consistent.
5. **The mis-stamps are a property of the document, not of the pipeline.** Any
   `summary.md` whose block-6 heading count differs from the manifest count
   mis-stamps under `postprocess`, regardless of how the transcript was produced.
   Fixing transcription does not repair this file.
6. **The loop is verbatim across 16 cues, not 21.** An earlier pass said
   `in the same.` repeated verbatim across cues 95–114. The file shows cues
   95–**110** verbatim (16 cues, `00:47:00`–`00:55:00`, 8 min); cues 111–114 are
   the *decay* — `in. in the same. in the same.` through `in. in. in.`. The whole
   affected window is cues 95–114 (`00:47:00`–`00:57:00`, 10 min ≈ 9.6%), of which
   8 min is verbatim repetition and the rest is corruption by mutating fragments.
   This matters for the detector: a verbatim rule bounds what it can see, because
   the tail mutates each cue. Caught by implementation, not by review.
7. **There was no timeline dispute, and framing one was an error.** This brief
   carried "which timeline is true — the SRT grid or the deck's stamps" as an open
   question across several rounds. A fresh `pipeline` run on the fixed build
   reproduced the existing transcript **byte-for-byte** (`transcript.txt` 86 791 B,
   `transcript.srt` 94 834 B, `audioDurationSec 6258.431688`), so the SRT grid and
   the video agree and the **only** wrong timeline in this story is the summary's
   block-6 stamps. The artifact in hand already contradicted the stamps; treating a
   defect as an ambiguity was the failure, not the missing measurement.
8. **There was no earlier, better transcript.** An earlier pass hypothesised that
   the summary was produced from a different transcript because its content looked
   inconsistent with the timings. With the timeline settled (7), the hypothesis is
   withdrawn: the summary was written from **this** transcript, whose narrative text
   does contain the Victorian/consols passage at `00:13:30`–`00:16:30` and Uber at
   `01:34`–`01:43`, while the loop occupies `00:47:00`–`00:57:00`. Nothing was
   discarded and recovered; the headings simply point at the wrong minutes.

---

## Provenance of this item

| Path | CreationTime | LastWriteTime |
|---|---|---|
| `<item>` | 20.09.2026 02:59:12 | 25.09.2026 00:09:20 |
| `video.mp4` | — | 20.09.2026 09:05:11 |
| `.data` | 25.09.2026 00:01:24 | 25.09.2026 00:07:06 |
| `.data\img` | 25.09.2026 00:07:06 | 25.09.2026 00:07:06 |
| `transcript.txt` / `.srt` / `source.json` | — | 25.09.2026 00:01:09 |
| `summary.md` | — | 25.09.2026 00:11:22 |

*Confirmed.* The item folder existed from 20.09 with the video already in it. The
transcription ran on 25.09 at `00:01:09`; the artifacts were placed beside the
folder (Defect 1) and moved by hand into `.data/` at `00:01:24`. `postprocess`
created `.data/img` at `00:07:06`, and `summary.md` was written at `00:11:22`.

`source.json` records `"kind": "audio"`, `"url"` = a deleted work-dir
`audio.wav`, `sourceKept: true`, `sourceFile: "video.mp4"`,
`backend/deviceUsed: "cuda"`, `deviceVerified: true`, `realtimeFactor: 0.0162`,
`model: ggml-large-v3-turbo`. *Inferred:* the run was a URL source with
`-DownloadDir` equal to the item root, because that is the only path that sets
`sourceKept: true` for a local-looking file — [`pipeline.py:90`](../scripts/zoombie/commands/pipeline.py:90).
For a local `-Source` the media is never copied and `sourceKept` stays the
default `False` ([`stt.py:74`](../scripts/zoombie/lib/stt.py:74)). The command
line was not observed.

---

## Defect 1 — artifacts written beside the item, not inside its `.data/`

*Confirmed.* Highest severity, because the defect is silent and self-consistent.

**Mechanism.** One inverted precedence expression:

```python
# scripts/zoombie/lib/stt.py:250
output_base = request.item_dir or request.output_base
```

`pipeline` and `transcribe` both pass a correctly resolved `output_base`
(`<item>/.data/transcript`) **and** a non-empty `item_dir`, so `.item_dir` wins
and every artifact is written flat off the item folder:

| Site | Consequence |
|---|---|
| [`stt.py:380`](../scripts/zoombie/lib/stt.py:380) | `<item>.txt`, `<item>.srt` |
| [`stt.py:575`](../scripts/zoombie/lib/stt.py:575) | `<item>.source.json` |
| [`stt.py:253`](../scripts/zoombie/lib/stt.py:253) | path budget validated against the shorter string |
| [`stt.py:257`](../scripts/zoombie/lib/stt.py:257) | overwrite guard checks `<item>.txt`, a file never written |
| [`stt.py:372`](../scripts/zoombie/lib/stt.py:372) | `ensure_dir` creates the parent, never `<item>/.data/` |

The docstring four lines above the expression describes the **correct** behaviour
("`output_base` IS `<item>/.data/transcript`"), which is why review did not catch
it. A comment asserting an invariant the code does not hold is worse than no
comment.

**Detection signature, and the reason it was accepted.** [`Report.to_data()`](../scripts/zoombie/lib/stt.py:113)
emits both `outputBase` and `itemDir`. On this run they were the **same string**,
which contradicts the contract — `outputBase` must end with `.data\transcript`
whenever `itemDir` is set. The transcribe-video skill tells the caller to read
both ([`SKILL.md:74`](../skills/zoombie-transcribe-video/SKILL.md:74)), and nobody
compared them. *Confirmed for the code path; the run's JSON was not retained.*

**Why `-DryRun` agreed with the contract.** The dry-run branch returns before
[`stt.transcribe`](../scripts/zoombie/lib/stt.py:229) is entered
([`pipeline.py:210`](../scripts/zoombie/commands/pipeline.py:210)), so its
`outputBase` came from `resolve_output_base` and was never overwritten. Dry run
and real run disagree because they never execute the same code.

**Repair.** `output_base = request.output_base or request.item_dir`. See
*Why the tests missed it* for the assertion that belongs beside it.

---

## Defect 2 — block-6 heading timestamps mis-assigned

*Confirmed.* Reader-visible and silent.

`postprocess.time_stamps` associates heading *k* with manifest image *k* **by
ordinal** ([`postprocess.py:283`](../scripts/zoombie/commands/postprocess.py:283)),
documented as valid because "the association is by ORDER, which is the documented
block-6 convention (one `###` per slide)". The convention holds only when the
heading count equals the image count. Here block 6 has **124 headings** and the
manifest has **96 images**, so the association shifts progressively.

The disagreement is measurable against the SRT:

| Summary's stamp | Text it stamps | SRT location of that text |
|---|---|---|
| `00:44:40` (`s-18`) | «меньше зависимости от рыночной моды» | `00:13:30` (cue at SRT line 111) |
| `00:51:59` (`s-19`) | «Викторианский критерий … консоли» | `00:15:00`–`00:16:30` |
| `01:08:21` (`s-59`) | «Прогноз на март 2021 года — это гадание» | `00:45:00` (cue 91) |

*Confirmed, and since settled.* Headings `s-2` … `s-30` or so are shifted;
headings past the point where the manifest's frames resume (`~00:57`) re-align
with the audio. The earlier framing of this as "which timeline is true" is
withdrawn (see Corrections 7): a fresh run reproduced the transcript byte-for-byte,
so the SRT grid is authoritative and the stamps are simply wrong. No part of the
recording is mis-timed; only the document is.

**Repair.** Refuse the ordinal association when the counts differ, fall back to
the SRT/fuzzy match, and report it. Silent mis-stamping is worse than an unstamped
heading, because a plausible timestamp is indistinguishable from a correct one.

---

## Defect 3 — "talking head" slides are downstream of a broken join

*Confirmed for the mechanism; the frame content itself was not inspected.*

A slide's `anchor_text` is the narration spoken during its interval
([`slides.py:305`](../scripts/zoombie/lib/slides.py:305)), and that text is the
**only** bridge between the deck and block 6
([`commands/slides.py:231`](../scripts/zoombie/commands/slides.py:231)). For the
loop window the anchors are literally `"in the same. in the same. in the same."`
(manifest rows 19–26), so `insert_images` cannot match them and falls through to
nearest-neighbour ([`postprocess.py:622`](../scripts/zoombie/commands/postprocess.py:622)).
The visible result is **eight figures (018–025, `00:44:40`–`00:54:38`) stacked
under the single heading `s-57`** (round line 424 of the item's `summary.md`,
`C:\Users\maxim\source\repos\kb\Crimson\4 - 09.04.2020 - ...\summary.md`), out of
deck order.

Frame-selection evidence for the same region: all 96 frames are 1280×720; images
`033`–`039` are **seven frames in 33 seconds**, `048`–`051` four in 20 s, `076`–`083`
eight in 44 s — all sharing one anchor because the SRT cursor did not advance.
Those sizes cluster at 590–960 KB, against 200–470 KB for the flat
slide-looking frames, so byte size is a usable cheap pre-filter.

**Resolved by viewing four frames.** An earlier pass called these clusters
"webcam churn" and marked the frame content *uncertain*. Viewing withdrew that:
frame `037` (`01:05:47`) is the **ФосАгро website** (`phosagro.ru`, farmland
hero, nav with «Инвесторам», a COVID-19 notice banner, carousel `01 / 03`), and
the audio at that minute introduces exactly that walkthrough. Other confirmed
content: `005` is the Ned Davis / Hartford Funds **Figure 8** chart with its
six-series colour legend (terminal values `$9,568` / `$6,607` / `$3,512` /
`$2,817` / `$388` / `$79`); `019` is the **Finviz screener** (20 rows of 7,690,
page 1/385, sorted by ticker ascending, page stamp `Wed Apr 08 2020`); `001` is a
**talking head** with no content.

So the dense clusters are **web-page captures**, not noise: they are informative
frames whose `anchor_text` the loop destroyed. That reframes this defect from
"useless frames" to "useful frames made unplaceable", and it confirms the
transcript's colour cue («салатовая линия — это S&P 500») against the chart.

**Content-level confirmation of the timeline (Corrections 7).** Slide `019` is a
screener at `00:51:59`, and the recovered audio at that minute *is* the screeners
section; slide `037` is the site walkthrough at `01:05:47`, matching the audio
there. The slide times are right to the second, and only the document's block-6
stamps are wrong.

**Repair.** OCR text-density gate on candidates using the existing
[`ocr.py`](../scripts/zoombie/lib/ocr.py:48), *before* the vision pass; byte size
as a pre-filter; and a hard report when a manifest entry's `anchor_text` is
degenerate or when several figures resolve to one paragraph.

---

## Defect 4 — the transcript loop, `00:47:00`–`00:57:00`

*Confirmed.* A genuine whisper.cpp repetition loop. `in the same.` repeats
**verbatim** across 16 consecutive 30-second cues (SRT cues 95–110,
`00:47:00`–`00:55:00`, 8 minutes), then decays through `in. in the same. in the
same.` to `in. in. in.` over cues 111–114, and resumes at cue 115 with real
speech. The whole affected window is cues 95–114 — 10 minutes, ≈ 9.6% of the
audio — of which the first 8 are clean verbatim repetition.

The decay matters for detection: a verbatim-only n-gram rule reports the 8-minute
core and misses the mutating tail, so the reported window is a lower bound on the
damage rather than the full extent of it.

The window in the original report (`00:44:40–00:56:00`) is the **summary's
mis-stamps** from Defect 2, not the transcript's own timeline. On the SRT grid
the loop runs `00:47:00`–`00:57:00`.

The run is deterministic, so a plain re-run cannot help. What can:

1. **Repetition detection** over the produced SRT — verbatim n-gram across
   consecutive cues. This window trips it at cue 95. Model-independent, and it
   converts a silent 10-minute hole into a reported failure.
2. **Windowed re-run** — slice with ffmpeg, transcribe, offset the SRT. Costs
   minutes, not a second full pass. On this hardware a full 1:44 decode took
   ~106 s (`realtimeFactor 0.0162`), so the experiment is cheap.
3. **Model choice for the retry.** The loop is a decoder failure, not a capacity
   failure: `ggml-large-v3-turbo` has a 4-layer distilled decoder, and the probe
   does not detect `-mc`/`--max-context` or `-et`/`--entropy-thold`, which are the
   genuinely relevant knobs ([`whisper.py:287`](../scripts/zoombie/lib/whisper.py:287)).
   Prefer a non-distilled decoder — `large-v3` per this codebase's naming
   ([`paths.py:295`](../scripts/zoombie/lib/paths.py:295)) — over simply a larger
   model. `-nf` must not be used (see *Corrections*).

---

## Defect 5 — block-6 subheading granularity

*Confirmed.* Block 6 carries **124 `###` headings** for a 96-slide deck over
1:44; block 4 lists all 124 as ToC entries. `postprocess` numbers every heading of
level ≥ 3 in the last `##` section and feeds all of them to the index
([`postprocess.py:273`](../scripts/zoombie/commands/postprocess.py:273),
[`postprocess.py:791`](../scripts/zoombie/commands/postprocess.py:791),
[`postprocess.py:813`](../scripts/zoombie/commands/postprocess.py:813)). No depth
limit, no count limit.

This is also what made Defect 2 bite: the 124-vs-96 mismatch is the input to the
ordinal association.

**Repair.** Cap block 4 at level-3 headings only, and state the prose policy in
the summarize skill: subsections on topic change, not per slide.

---

## Defect 6 — minor: sidecar and manifest fields that carry nothing

*Confirmed.*

- `img/manifest.json` records `"bytes": 0` for all 96 rows while the files are
  200 000–968 000 bytes. `build_rows` reads `record.get("bytes")`
  ([`slides.py:408`](../scripts/zoombie/lib/slides.py:408)) but `_extract_frames`
  never sets it ([`commands/slides.py:148`](../scripts/zoombie/commands/slides.py:148)),
  so the `Bytes` column in `.data/img/README.md` is permanently zero and the
  manifest cannot be used to reason about frame weight.
- For a bare `transcribe` of an already-extracted audio file, the sidecar records
  `kind: "audio"` with `url` pointing at a deleted scratch `audio.wav`
  ([`stt.py:539`](../scripts/zoombie/lib/stt.py:539)). The schema is satisfied and
  no origin is carried. A `null` with a reason would be more honest than a dead
  temp path.

---

## The ordering contradiction

*Confirmed.* Three skills document "transcribe → then summarize". A correct run
creates neither `summary.md` nor `.data/`, and
[`is_item`](../scripts/zoombie/item/paths.py:130) requires one of them, so the
item folder is unrecognised by `items`/`index` until the summary exists — and the
only `ensure_dir` that creates `.data/` is the one inside `replace_index` while
`postprocess` writes block 4.

Following the documented order therefore cannot produce a recognised item, which
is exactly why a by-hand `.data/` was the route taken here. Either
`transcribe`/`pipeline` should create `<item>/.data/` when `-Output` names an
item, or the three skills should document that `.data/` appears only after the
summary. Doing neither leaves the documentation contradicted by the mechanism.

---

## Why the tests missed Defect 1

*Confirmed.*

- [`tests/test_stt_args.py:258`](../tests/test_stt_args.py:258) asserts
  `resolve_output_base(...) == <item>/.data/transcript`, and
  [`tests/test_stt_args.py:276`](../tests/test_stt_args.py:276) asserts the guard
  and the writer "derive from one helper, so they cannot disagree" — true of
  `resolve_output_base`, silent about [`stt.py:250`](../scripts/zoombie/lib/stt.py:250).
- Every constructor that reaches `transcribe` passes `output_base=base` with **no**
  `item_dir` ([`tests/test_stt_args.py:218`](../tests/test_stt_args.py:218)).
- The one test that does pass `item_dir` asserts only that it is *echoed*
  ([`tests/test_stt_args.py:243`](../tests/test_stt_args.py:243)).
- The transcribe-command test monkeypatches `stt.transcribe`
  ([`tests/test_stt_args.py:335`](../tests/test_stt_args.py:335)), so the write
  path never executes.

No test asserts where artifacts land when `item_dir` is set.

`verify` cannot catch any of the content defects either: its checks are
`missing-image`, `missing-manifest`, `missing-readme`, `dead-anchor`, `dead-link`,
`missing-source` and an advisory `section6-not-declared`
([`verify.py:49`](../scripts/zoombie/commands/verify.py:49)). Nothing compares
heading count to image count, checks a timestamp, or compares block 6 with its
source.

---

## Ranked fixes

Ordered by value-to-risk, with the cheapest diagnostic first.

1. **Five-minute diagnostic before any fix.** Extract frame 95 (`-Times "00:47:00"`)
   and look at it. A slide with text ⇒ the OCR gate and the frame count are the
   cause and the anchors were already degenerate. A face with no slide ⇒ the
   frames do not exist and nothing downstream could place them. Either answer
   changes item 5.
2. **Stamp-integrity guard.** Refuse the ordinal association when heading count ≠
   image count; fall back with a warning. Highest value, because it repairs the
   reader-facing index that is otherwise wrong in a way nobody can detect.
3. **Make the anchor join fail loudly.** Degenerate `anchor_text`, or several
   figures resolving to one paragraph ⇒ report, do not place. An unplaced figure
   is a report line; a mis-placed one is a false claim.
4. **Fix [`stt.py:250`](../scripts/zoombie/lib/stt.py:250)** and add two tests: the
   `Request` with both fields set must write under `.data/`, and `outputBase` must
   resolve inside `<item>/.data/` whenever `itemDir` is set.
5. **Resolve the ordering contradiction** — create `.data/` at `-Output` time, or
   document the real order.
6. **Loop guards**, in order: extend the capability probe to `-mc` and `-et`; add
   the repetition detector; then the windowed non-distilled re-run. Never `-nf`.
7. **OCR text-density gate** on slide candidates, byte size as pre-filter, and
   raise `-MinSlideSec` / tighten `-HashDistance` so a seven-frames-in-33-seconds
   cluster cannot form.
8. **Cap the block-4 index at level 3** and state the prose policy.
9. **Sidecar honesty** for a local audio source: `null` with a reason rather than a
   dead scratch path.

Items 2–6 and 9 are code changes and belong to Code mode. A correct layout for
*this* item can be obtained meanwhile with `migrate -Apply`, which moves
root-level transcripts and the sidecar into `.data/` and rewrites the image links
([`migrate.py:113`](../scripts/zoombie/commands/migrate.py:113)); it is
idempotent and touches nothing already migrated. It does **not** repair the loop,
the stamps or the frame count.

---

## Implementation status (added by Code mode)

Each item below was implemented and tested; see the return for the diff summary.

1. **Artifact location** — fixed (precedence `output_base or item_dir`, one
   `effective_output_base` helper shared by the writer and the pipeline guard);
   2 new tests. **Verified end to end:** the deployed CLI wrote
   `<item>/.data/transcript.{txt,srt,source.json}`.
2. **Stamp integrity** — fixed (`time_stamps` refuses the ordinal association on a
   count mismatch and warns). **Verified on the Crimson item:** the warning fires
   "124 headings but the manifest has 96 times".
3. **Anchor join** — fixed (degenerate `anchor_text` skipped + reported; a shared
   paragraph reported). **Verified on the Crimson item:** `imagesSkippedDetail`
   names 4 `degenerate-anchor` rows (003/019/023/024 and 091) plus `same-paragraph`
   rows.
4. **Loop guards** — probe extended to `-mc`/`-et`; SRT repetition detector added
   and reported; `-nf` deliberately absent. **Measured discrepancy:** the detector
   reports cues **95–110** (16 cues, `00:47:00`–`00:55:00`), not 95–114 — 111–114
   are the *decay* (`in. in the same.`, `in. in. in.`) which this detector does not
   count as one verbatim run.
5. **Block-4 granularity** — capped at level 3 (numbering untouched).
   **Note:** the Crimson file's 124 headings are all `###`, so the cap does not
   reduce *this* file's index; it bounds future `####` growth.
6. **Vision + gates** — OCR text-density gate with byte prefilter and per-reason
   report; `bytes` now set; sidecar `url` honesty. **Verified:** sidecar carries
   `urlReason`.
7. **Ordering** — `.data/` is created at `-Output` time; the three skills document
   the same order. **Verified:** a bare `transcribe` produced a recognised item.

## Defects found during verification (not in the original report)

Both surfaced running the fixed build end to end on this item's own video. They
share a signature with Defect 1: **a success report that describes something other
than what happened.**

### Defect 7 — `transcribe` reports success when whisper cannot read the input

*Confirmed.* Running `transcribe` on the 1.8 GB `.mp4` returned:

```
artifacts: { "srt": null, "sidecar": {…, "size": 633} }
```

— no `txt` key at all, exit code 0, `ok: true`. The run's own log names the cause:

```
read_audio_data: failed to read audio data
error: failed to read audio file '…\input.mp4'
```

`transcribe` never converts: it copies the input into the work dir as
`input<ext>` ([`stt.py:281`](../scripts/zoombie/lib/stt.py:281)) and hands it to
whisper.cpp, which reads WAV and little else. `pipeline` is the command that
extracts audio first ([`pipeline.py:226`](../scripts/zoombie/commands/pipeline.py:226)).
Using `transcribe` on a video is a documented misuse — but the tool neither
refused it nor failed. It reported `realtimeFactor 0.0006` because the duration
came from `ffprobe`, which *can* read a video, while whisper could not.

Severity is bounded by the fact that this is not the documented path; it is
unbounded in the sense that a caller who trusts `ok` gets no transcript and no
error. Two cheap triggers: a missing `txt` in `artifacts`, and the
`read_audio_data: failed to read audio data` line in the captured log.
`artifacts.srt: null` alongside a non-null sidecar is already a detectable
contradiction.

### Defect 8 — the pipeline retains large media into a throwaway item

*Confirmed.* With `-DownloadDir` omitted — the documented normal case for a local
source — `pipeline` copies the 1.8 GB video into the item root. In this
verification the item was a scratch folder, so the run duplicated 1.8 GB
(`1 808 922 460` bytes removed afterwards, alongside the three artifacts). For a
local `-Source` the retention is semantically defensible; the cost is not, and
`_retain_source` had no size guard ([`pipeline.py:60`](../scripts/zoombie/commands/pipeline.py:60)).

### Implementation status of defects 7–8

*Implemented and verified.*

- `transcribe` refuses a known video container up front, naming `pipeline`
  (`VIDEO_CONTAINERS` + `refuse_video_input`, [`stt.py:45`](../scripts/zoombie/lib/stt.py:45));
  the WAV `pipeline` produces is unaffected.
- An exit-0 run with no `.txt` now raises `StepFailedError`
  ([`stt.py:475`](../scripts/zoombie/lib/stt.py:475)), surfacing the
  `audio_read_failure` log lines via `preserve_logs`. The sidecar write sits after
  that check, so `srt: null` alongside a non-null sidecar is now unreachable.
- `_retain_source` no longer duplicates a user-supplied local `-Source`
  ([`pipeline.py:77`](../scripts/zoombie/commands/pipeline.py:77)); the same-file
  short-circuit still records `sourceKept: true` for media already in the item, a
  deliberate non-copy records `sourceReason`, and an oversized retention is a
  logged skip (`MAX_RETAIN_BYTES`, [`pipeline.py:31`](../scripts/zoombie/commands/pipeline.py:31)).
- Re-verified independently: suite `678 passed in 5.93s`; `transcribe` on a dummy
  `.mp4` exits **1** with the refusal; the deployed copy carries both changes.

---

## Process note — the stale-result trap

The one *delivery* fault found in this session, recorded because it made a
subtask report a completed step that never ran.

A subtask issued two different commands; both outputs carried the **identical**
timestamp `2026-09-25T07:18:04.429Z` and the identical payload. The CLI cannot do
that — [`main()`](../scripts/zoombie/cli.py:527) writes one result line per
invocation via [`write_result`](../scripts/zoombie/lib/process.py:54), which
stamps a fresh `utc_now_iso()` on every call, and there is no result cache. So the
second command produced no result, and the first was re-served as if it were the
answer. The subtask read that stale payload as fresh evidence.

The rule this implies now lives in the mode
([`modes/zoombie.yaml:50`](../modes/zoombie.yaml:50)): **a timestamp older than
the run you just issued means the payload is stale — re-issue the command; two
results with an identical timestamp are the same result delivered twice.**
It is soft guidance and depends on the agent reading the `timestamp` field at all,
which is real but undocumented in
[`json-contract.md`](../skills/_shared/json-contract.md:1).

## Open questions

1. **What frames 033–039 and 076–083 actually show.** Decides whether the OCR gate
   is the fix or merely a filter. Unresolved: the PNGs have not been viewed.
2. **Is the audio in `00:47:00`–`00:55:00` intelligible speech? — ANSWERED: yes,
   and a windowed re-run recovers it.** *Confirmed by experiment.* The same
   installed model (`ggml-large-v3-turbo`) decoded the window `00:46:30`–`00:58:00`
   sliced out of the video (`-ss 00:46:30 -t 690`, 16 kHz mono WAV, 22 080 078 B)
   with **no loop at all** — three variants, all exit 0, `in the same` occurrences
   `0/0/0`, wall 12.6–15.7 s each:

   | Run | Flags | Bytes | Loops |
   |---|---|---|---|
   | A | baseline (same flags as the failing full-file run) | 11 255 | 0 |
   | B | `-mc 0` | 12 515 | 0 |
   | C | `-et 2.8` | 11 255 | 0 |

   Run C is byte-identical to run A, so `-et 2.8` changed nothing here; run B
   differs but also loops not at all. What mattered was **decoding the window as a
   short file**, not the parameters.

   The recovered text is real speech about flights and recovery forecasts, Altria
   and cigarette demand, McDonald's and junk food, the "здравый смысл" first
   filter, and the screeners section (Finviz, the 64 % extrapolated yield,
   Bloomberg Terminal, McDonald's negative equity) — i.e. material the summary's
   block 3 does discuss. The item's SRT content for `00:47:00`–`00:55:00` is
   therefore **recoverable, not lost**.

   *Hypothesis left untested:* why the full-file decode loops specifically there
   (long-context decoder instability, reset by a shorter input). One run per
   variant; determinism of the slice result was not re-checked.
3. **Why the manifest count and heading count diverged at all** — 96 detected
   against 124 authored. If the agent wrote headings from the transcript, the
   ordinal association was never going to hold, and item 2 is the only safe
   behaviour.

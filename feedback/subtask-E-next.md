# Subtask E — the `data.next` bridge, and its attach cap

Plan: [`plans/zoombie-image-pipeline-rework.md`](../plans/zoombie-image-pipeline-rework.md) §9 (§1.1 receipt
protocol, §3 the settled 413 root cause, §4 design foundations, §4.1 the two-stage filter, §14 non-goals).
Mode: implementation, **E only**. Subtasks F and G were **not** started.

* Subtask start (UTC): **2026-09-25T20:57Z**
* Receipt written (UTC): **2026-09-25T21:09Z** (newer than start)

Result: **GREEN.** `python -m pytest tests` → **801 passed** (floor was **770**, +31).
`python -m pytest tests -q` reproduces it below.

---

## 0. Baseline recorded before any change (§1.1 rule 3)

```
> python -m pytest tests -q            # BEFORE → 770 passed in 7.87s
> python -m pytest tests -q            # AFTER  → 801 passed in 8.14s
> python -m pytest tests --collect-only -q   # 801 tests collected; test_next.py = 31
> python -m pytest tests/test_next.py -q     # 31 passed in 0.51s
```

**770 + 31 = 801**, and every pre-existing test still passes — the floor is not just met, it is intact.

---

## 1. Artifacts (every path below exists on disk)

| Artifact | State | Role |
|----------|-------|------|
| [`scripts/zoombie/lib/next.py`](../scripts/zoombie/lib/next.py) | **new** | `DEFAULT_ATTACH_CAP`, `cap_of()`, `select()`, `describe()`, `build()` — the cap is applied here, not by the caller |
| [`scripts/zoombie/commands/slides.py`](../scripts/zoombie/commands/slides.py) | modified | `visionFrames` capped; `visionFrameCount`; `data.next` on both the apply and `-DryRun` paths |
| [`scripts/zoombie/commands/readimages.py`](../scripts/zoombie/commands/readimages.py) | modified | `data.next`; dry-run file list capped with `filesTruncated` |
| [`scripts/zoombie/commands/readpdf.py`](../scripts/zoombie/commands/readpdf.py) | modified | `visionPages` capped; `visionPageCount`; `data.next` on the apply and `-DryRun` paths |
| [`scripts/zoombie/commands/postprocess.py`](../scripts/zoombie/commands/postprocess.py) | modified | terminal `data.next` (shape-complete, empty attach) |
| [`scripts/zoombie/cli.py`](../scripts/zoombie/cli.py) | modified | `-AttachLimit` on `slides`/`readimages`/`readpdf`/`postprocess` |
| [`tests/test_next.py`](../tests/test_next.py) | **new** | 31 tests |
| [`skills/_shared/json-contract.md`](../skills/_shared/json-contract.md) | modified | documents `data.next` + the cap in the block every skill includes |
| `.tmp/subE/` (`deck12.mp4`, `item12*`, `scanned.pdf`, `ev_*.json`) | scratch (gitignored, §.gitignore:38) | the live evidence of §3 |

The change set is **7 files** (§10). Nothing was left under `scripts/`.

---

## 2. The `data.next` shape, as emitted (a real run, not a sketch)

`cd scripts && python -m zoombie slides -Source ..\.tmp\subE\deck12.mp4 -Output ..\.tmp\subE\item12c -MinFrameBytes 0 -NoTextGate`

```jsonc
"next": {
  "command": "postprocess",
  "args": { "-Md": "…\\.tmp\\subE\\item12c\\summary.md" },
  "why": "frames are kept and their OCR text is in data.ocr.artifact; read the attach list (the frames worth looking at) and write the block-6 prose, then run postprocess -Apply to inline the chosen figures and stamp the headings",
  "attach": [ { "file": "001 - 00-00-08.png", "path": "…\\001 - 00-00-08.png", "bytes": 5758 }, … 8 entries ],
  "attachCap": 8,
  "attachCount": 8,
  "count": 12,
  "truncated": true,
  "overAttach": [ { "file": "009 - 00-01-28.png", … }, { "file": "010 - 00-01-36.png", … },
                  { "file": "011 - 00-01-48.png", … }, { "file": "012 - 00-01-56.png", … } ],
  "budget": { "images": 8, "bytes": 46090 },
  "overBudgetReasons": [ "attach list truncated: 12 image(s) available, the inline cap is 8; 4 deferred to data.next.overAttach (re-run with a narrower request to read them)",
                         "more fits on disk than one result may attach, so the recommended next step is to NARROW the request for the deferred paths (for slides, -Times with their timestamps) rather than raise the cap: the cap is a transport guarantee and -Force does not bypass it" ],
  "writes": false,
  "reason": "…the two above, joined by '; '…"
}
```

The four required keys are exactly §9's: `command`, `args`, `why`, `attach`, `budget`. **The enforcement is
the point, not the field**, so the block carries the cap (`attachCap`), the honest total (`count`) and the
deferred remainder (`overAttach`). `-Md`/`-Source` keys are the house spelling (single-dash capitalised), not
a new convention.

---

## 3. The cap enforcement — proof

### 3.1 Live, on all four surfaces

A 12-slide synthetic deck (`deck12.mp4`, twelve distinct luma levels) and a 12-page image-only PDF
(`scanned.pdf`, no text layer) were built with the toolchain ffmpeg / PyMuPDF so the cap of 8 actually bites.

```
> cd scripts && python -m zoombie slides -Source ..\.tmp\subE\deck12.mp4 -Output ..\.tmp\subE\item12c -MinFrameBytes 0 -NoTextGate
  next: attachCap 8  attachCount 8  count 12  truncated true   overAttach 4   budget {images: 8, bytes: 46090}
> cd scripts && python -m zoombie readimages -Source ..\.tmp\subE\item12c\.data\img -Output ..\.tmp\subE\ri2
  next: attachCap 8  attachCount 8  count 12  truncated true   overAttach 4   budget {images: 8, bytes: 46090}
> cd scripts && python -m zoombie readpdf -Source ..\.tmp\subE\scanned.pdf -Output ..\.tmp\subE\scanout2 -Vision ..\.tmp\subE\vision2
  keptScannedPages [1..12];  visionPages returned 8 of visionPageCount 12
  next: attachCap 8  attachCount 8  count 12  truncated true   overAttach 4   budget {images: 8, bytes: 159200}
> cd scripts && python -m zoombie postprocess -Md ..\.tmp\subE\ri2.md -Apply
  next: command "verify"  attach []  count 0  truncated false
```

**12 kept → 8 attachable → 4 deferred, on three independent paths.** Nothing was deleted: the PNGs are all on
disk in `.data/img/`, and `data.images.count` / `keptFrames` / `visionPageCount` still report 12.

### 3.2 The "-AttachLimit 2" control, live

```
> cd scripts && python -m zoombie readpdf -Source ..\.tmp\subE\text.pdf -Output ..\.tmp\subE\pdfout -AttachLimit 2
  …"next":{ "command":null, "attachCap":2, "attachCount":0, "count":0, "truncated":false …}
```

The cap in force follows the flag and is reported as `attachCap` — the contract is auditable from the result,
not only from the code.

### 3.3 The tests that assert it

Concentrated in [`tests/test_next.py`](../tests/test_next.py) (31 tests):

* `TestCapEnforcement::test_build_applies_the_cap_itself_not_the_caller` — 96 entries in, and
  `len(attach) ≤ 8` with `len(attach) + len(overAttach) == 96`. **The caller cannot skip the cap.**
* `TestCapEnforcement::test_the_cap_truncates_and_names_the_excess` — 11 in → 8 + 3, and the three are
  *named*, not dropped.
* `TestCapEnforcement::test_build_reports_the_truncation_as_a_named_reason` — `truncated is True`,
  `attachCap == 8`, `count == 12`, and `reason` names both numbers.
* `TestCapEnforcement::test_the_cap_cannot_be_bypassed_by_force` — asserts **structurally** that neither
  `build` nor `select` has a `force` parameter, so no code path offers to switch the cap off.
* `TestCapEnforcement::test_args_capped_reports_a_narrowing_and_never_a_bypass` — the wording locks to the
  honest remedy (NARROW) and asserts the false promise `"inert"` is absent.
* `TestReadimagesNext::test_the_vision_hand_off_is_capped` — 12 images → 8 attached, 4 in `overAttach`,
  and **every advertised path exists on disk** (the block is executable, not decorative).
* `TestReadimagesNext::test_an_explicit_limit_narrows_the_attach_list` — `-AttachLimit 3` → 3 + 9.
* `TestEnvelopeUnchanged::test_data_next_is_an_addition_inside_data_not_a_new_envelope` — drives `cli.main`
  and asserts the envelope keys are **exactly** `["ok","action","error","data","timestamp"]` with `next`
  inside `data` (§14).
* `TestAttachLimitFlag` — the flag parses on all four pipeline commands and defaults to the library cap.

**No new `ZoombieError` was added.** The enforcement is a *hard truncation with a named reason*
(`truncated`, `reason`, `overAttach`), which is one of the two forms §9 permits. A refusal was rejected
because the terminal steps legitimately produce zero images, and because a refusal on a *successful* selection
run would throw away work the caller can still use; the block is instead *self-describing*, which is what
"reported reason" is for. The tests above pin that refusal to a reported truncation rather than a silent one.

---

## 4. How the cap interacts with `-DryRun` / `-Check`

`build()` writes nothing, so the cap composes with "`-DryRun` writes nothing" for free (§9 requires no new
I/O). The design rule taken: **a dry run is what the agent plans its reads from, so it must already be honest
about the cap.**

* `slides -DryRun` — no frames exist to extract, so `attach == []` and `truncated is False`; the block is
  emitted with the same 13 keys, `command` is still `postprocess`, and `attachCap` follows `-AttachLimit`.
  Live: `next keys = [command, args, why, attach, attachCap, attachCount, count, truncated, overAttach, budget, overBudgetReasons, writes, reason]`, `attach: []`, `cap: 8`.
* `readimages -DryRun` — the *planned* file list is capped too (`files` ≤ 8, `filesTruncated: true`) but
  `next.attach` is `[]` because nothing was written and there is nothing to read yet.
* `readpdf -DryRun` — `next.command` is the same call without `-DryRun`; `attach == []`.
* `postprocess` is dry-run by default and writes only with `-Apply`; its `next` is emitted in both modes.

`tests/test_next.py::TestReadimagesNext::test_dry_run_is_capped_but_writes_nothing` asserts both halves
(`filesTruncated is True`, `next.attach == []`, no `.md`/`.images` on disk) and
`TestSlidesNext::test_dry_run_emits_a_well_formed_next_and_writes_nothing` asserts the output folder stays
`[]`. `readpdf -Check` does not exist; `unpack -Check` is untouched by this subtask.

---

## 5. What the block recommends, per command (and one self-loop it avoids)

| Command | `command` | why |
|---------|-----------|-----|
| `slides` | `postprocess` | read the OCR text + attach list, write block-6 prose, then inline the chosen figures |
| `readimages` (vision-only) | **`null`** | the next step is an *agent* action (read, write prose, hand off), not a CLI verb |
| `readimages -Ocr` | `null` | the text is already in the document, so nothing is left to read |
| `readpdf` (text) | `null` | the text layer was read directly; nothing to look at |
| `readpdf -Vision` | `postprocess` | read the rendered scans, then place the figures |
| `postprocess` | `verify` (or `null`) | check the rewritten tree; `null` when the document already matched |

**A real bug the live run exposed and this subtask fixes.** `readimages` (vision-only) first recommended
`readimages` again — a **self-loop**: the verb had just written image links only, so re-running it under
`-Force` is a no-op, and `postprocess` (a 6-block pass) cannot place figures into a non-summary document
either. Both were wrong graph edges, so the recommendation is now `command: null` with the agent action in
`why`. `tests/test_next.py` asserts `command is None`; a wrong graph edge is worse than an honest hand-off,
because an agent follows it. Uniquely, this surfaced in the live run, not in the unit tests.

---

## 6. Reuse of D's blocks — nothing is recomputed

* `slides.data.next.attach` is built **from `visionFrames`**, which D already produced; the block reuses D's
  `data.ocr` and `data.images.skippedReasons` unchanged (I neither read nor wrote `.data/ocr.json`).
* No frame, no byte count and no OCR fact is re-derived: `budget.bytes` is a `paths.file_size` per attached
  file, i.e. the size of the artefact D already wrote (a `bytes` value already present on an entry is
  trusted, not re-stat'ed — `TestBudget::test_a_measured_byte_count_is_not_restatted`).
* `DEFAULT_ATTACH_CAP` is a **new** constant, distinct from and unrelated to D's `DEFAULT_MIN_TEXT_CHARS`.
  D's reporting floor is untouched.
* The image pipeline's own names (`visionFrames`, `visionPages`, `count`, `keptFrames`) keep their meaning;
  `data.next` is purely additive, so no producer needs to change.

---

## 7. Plan claims contradicted or corrected

1. **The `data.next` block carries six extra keys beyond §9's five** (`attachCap`, `attachCount`, `count`,
   `truncated`, `overAttach`, `overBudgetReasons`, `writes`, `reason` — thirteen keys in all, see §2).
   §9's shape is the *minimum*; a cap that is enforced but not
   *reported* would leave an agent unable to tell "8 of 8" from "8 of 96", which is the silent-shortening
   failure the item exists to remove. The four required keys are present and named exactly as written.
2. **"The CLI must refuse to emit more than N attachable images" (§9) is implemented as a hard truncation
   with a reported reason, not a refusal.** §9 allows either ("a named error or a hard truncation with a
   reported reason"). A refusal was rejected because the terminal steps legitimately attach zero images and
   because refusing a *successful* selection run would discard usable work. The truncation is not silent:
   `truncated: true`, `count`, `overAttach` and a `reason` naming the cap.
3. **§9 is silent on how a caller reaches the deferred frames.** Implementing `overAttach` exposed that a
   plain "read 8 at a time" rule deadlocks at the *next* invocation — `slides` cannot address frames 9–12 the
   same way it addressed 1–8. The block therefore recommends the *narrowing* edge (`slides -Times`), which is
   the one edge that makes the graph traversable.
4. **`readimages`'s recommendation is `null`, not a CLI verb** — correcting my own first implementation
   (see §5). §9 does not say what a non-pipeline producer should recommend; a self-loop is not a graph edge.
5. **`-Force` does not bypass the cap.** `-Force` is the overwrite flag throughout this codebase; extending
   it to the attach cap would collide with the no-overwrite refusal pattern E is asked to imitate. The cap is
   a transport guarantee, and that property is now pinned by a test on the function signatures.
6. **The cap is not applied to `data.images`, `intervals` or the D-produced blocks** — only to the
   agent-facing *attach* lists (`visionFrames`, `visionPages`, `next.attach`, dry-run `files`). Capping the
   reporting blocks would destroy the manifest D built; §9 caps what may be *read*, not what may be *known*.
7. The 413 root cause (§3) is untouched, as §9 requires: full-size PNGs are still produced, and the
   compressed-reading-copy work (§12) is still independent. This subtask changes what a result *advertises*.

### Advisories / honest scope

* **No `slides` apply-path live run could exercise OCR**, because the only pre-existing video on this machine
  is the synthetic one from Subtask D; the run above used `-NoTextGate`, so `data.ocr.artifact` was written
  with `"used": false`. The attach cap is independent of OCR, and D's live OCR evidence stands.
* **`scanned.pdf` in §3 is a synthetic image-only PDF** (twelve pages, an inserted PNG each, no text layer),
  not the Crimson document — the Crimson source video is not in the item tree (Subtask D's advisory). It
  exercises the real PyMuPDF render path and the real text-layer test, which is what the vision attach list
  depends on.
* **`skills/zoombie-summarize/SKILL.md` was NOT edited.** It is **11,454 bytes against an 11,500-byte budget**
  (`tests/test_skills.py::MAX_SKILL_BYTES`) — 46 bytes of headroom. Recording `data.next` there would fail
  `test_no_skill_exceeds_the_budget`, so the documentation went into
  [`skills/_shared/json-contract.md`](../skills/_shared/json-contract.md), the shared block every skill
  includes (14 insertions, `tests/test_skills.py` still green). A follow-up that trims the skill should also
  state `data.next` inline near the `slides`/`visionFrames` wording.
* **`readpdf`'s `visionPages` entries gained a `path` key** and lost their previous per-entry shape only in
  the sense that the list is now capped. Nothing in the repo asserted the old length; the full count moved to
  the new `visionPageCount`.

---

## 8. Commands run (raw, condensed)

```
> python -m pytest tests -q                       # BEFORE → 770 passed in 7.87s
> python -m pytest tests -q                       # AFTER  → 801 passed in 8.14s
> python -m pytest tests --collect-only -q        # 801 collected; test_next.py = 31
> python -m pytest tests/test_next.py -q          # 31 passed in 0.51s
> cd scripts && python -c "…cli.build_parser().parse_args(['slides','-Source','x','-DryRun'])…"
     cap 8    flag 3    imports ok
     build(... 11 attachable …) → attachCount 8 / count 11 / truncated True / overAttach 3
> ffmpeg -f lavfi -i color=… ×12 concat …         # build the 12-slide fixture (.tmp/subE/deck12.mp4)
> cd scripts && python -m zoombie slides -Source ..\.tmp\subE\deck12.mp4 -Output ..\.tmp\subE\item12c -MinFrameBytes 0 -NoTextGate
     keptFrames 12  runs 12  visionFrames 8  visionFrameCount 12  next.truncated true
> cd scripts && python -m zoombie slides -Source … -Output …\item12 -DryRun
     next keys [command,args,why,attach,attachCap,attachCount,count,truncated,overAttach,budget,overBudgetReasons,writes,reason]; attach []  cap 8
> cd scripts && python -m zoombie readimages -Source ..\.tmp\subE\item12c\.data\img -Output ..\.tmp\subE\ri2
     attach 8  count 12  truncated True  overAttach 4  bytes 46090
> cd scripts && python -c "…pymupdf: 12 pages, an inserted PNG each, no text layer…"     # scanned.pdf
> cd scripts && python -m zoombie readpdf -Source ..\.tmp\subE\scanned.pdf -Output ..\.tmp\subE\scanout2 -Vision ..\.tmp\subE\vision2
     keptScannedPages [1..12]  visionPages 8 of visionPageCount 12  attach 8  overAttach 4  bytes 159200
> cd scripts && python -m zoombie readpdf -Source ..\.tmp\subE\text.pdf -Output ..\.tmp\subE\pdfout -AttachLimit 2
     attachCap 2  command null  truncated false
> cd scripts && python -m zoombie postprocess -Md ..\.tmp\subE\ri2.md -Apply
     changed 1  next.command "verify"  attach []  writes false
> git status --porcelain ; git diff --stat        # change set = the 7 files in §1
```

Nothing under `scripts/` was left behind: the probes were one-line `python -c` invocations, and the scratch
is under the gitignored `.tmp/subE/`.

---

## 9. Checklist for the parent (§1.1 rule 3)

- [x] Receipt exists and is newer than the start (`…21:09Z` > `…20:57Z`).
- [x] Every artifact in §1 exists on disk.
- [x] The quoted output is plausible against the code and was produced by the commands shown.
- [x] **No receipt ⇒ not run** — this file is the deliverable.
- [x] Subtasks F and G were not started.

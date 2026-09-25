# Subtask F — CLI-owned scratch lifecycle

Plan: [`plans/zoombie-image-pipeline-rework.md`](../plans/zoombie-image-pipeline-rework.md) §10 (§1.1 receipt
protocol, §4.3 transport-in-code, §9 the `data.next` contract this sits beside, §11 why F precedes G).
Mode: implementation, **F only**. Subtask G was **not** started.

* Subtask start (UTC): **2026-09-25T21:10:57Z**
* Receipt written (UTC): **2026-09-25T21:30Z** (newer than start)

Result: **GREEN.** `python -m pytest tests` → **841 passed** (floor was **801**, +40).
`python -m pytest tests -q` reproduces it below.

---

## 0. Baseline recorded before any change (§1.1 rule 3)

```
> python -m pytest tests -q          # BEFORE → 801 passed in 8.09s   (the floor)
> python -m pytest tests -q          # AFTER  → 841 passed in 8.07s
> python -m pytest tests --collect-only -q   # 841 tests collected
> python -m pytest tests/test_scratch.py -q  # 40 passed in 0.63s  (all new)
```

**801 + 40 = 841**, and every pre-existing test still passes. The floor is not just met, it is intact:
`tests/test_next.py` (31) + `tests/test_skills.py` still green (`57 passed` together), `tests/test_unpack.py`
unchanged and green.

---

## 1. Artifacts (every path below exists on disk)

| Artifact | State | Role |
|----------|-------|------|
| [`scripts/zoombie/lib/scratch.py`](../scripts/zoombie/lib/scratch.py) | **new** | the two roots, `is_run_dir()`/`is_owned()`, `inventory()`, `clean()`, `report()`, `keep_requested()` |
| [`scripts/zoombie/commands/clean.py`](../scripts/zoombie/commands/clean.py) | modified | the `-CleanScratch` sweep; the legacy default is byte-for-byte preserved |
| [`scripts/zoombie/cli.py`](../scripts/zoombie/cli.py) | modified | `-KeepScratch` (alias of `-KeepWork`) on `_add_work` + `unpack`; `-CleanScratch` on `clean` |
| [`scripts/zoombie/commands/slides.py`](../scripts/zoombie/commands/slides.py) | modified | cleanup via `scratch.report`; `data.scratch` |
| [`scripts/zoombie/commands/readpdf.py`](../scripts/zoombie/commands/readpdf.py) | modified | **`-DryRun` now removes its own work dir** (it was created before the dry return); `data.scratch` on both paths |
| [`scripts/zoombie/commands/unpack.py`](../scripts/zoombie/commands/unpack.py) | modified | `scratch.report` in the `finally`; `data.scratch` + `data.cleanScratchVerb` |
| [`scripts/zoombie/lib/stt.py`](../scripts/zoombie/lib/stt.py) | modified | `Report.scratch`; `-DryRun`, the failure path and the success path all route through `scratch.report` |
| [`scripts/zoombie/commands/pipeline.py`](../scripts/zoombie/commands/pipeline.py) | modified | its work dir **and** download dir reported; the decision reaches the transcribe stage |
| [`scripts/zoombie/commands/transcribe.py`](../scripts/zoombie/commands/transcribe.py) | modified | `-KeepScratch` reaches the stage |
| [`tests/test_scratch.py`](../tests/test_scratch.py) | **new** | 40 tests |
| [`skills/_shared/scratch-note.md`](../skills/_shared/scratch-note.md) | modified | the prose obligation is replaced with the code contract |
| [`README.md`](../README.md) | modified | `clean -CleanScratch` documented |

The F change set is **11 files** (1 new module, 1 new test file, 9 edits). Nothing was left under `scripts/`
(one `scripts/no`, a 54-byte artefact of my own `echo` chaining, was found and deleted mid-session).

---

## 2. What "CLI-owned" means here, and the ownership rule

Per-run scratch already lived under the **(ASCII) toolchain root** — [`paths.new_temp_dir()`](../scripts/zoombie/lib/paths.py:513)
resolves `<env_root>\tmp\<prefix>-<guid>` and [`paths.new_ascii_dir()`](../scripts/zoombie/lib/paths.py:503) under
`<env_root>\work\<guid>`. F does **not** reinvent that; [`lib/scratch.py`](../scripts/zoombie/lib/scratch.py:37)
names the two roots so cleanup and reporting read from one place.

The ownership test is deliberately narrow (`is_owned`), and it is the whole safety of the verb:

* a real **directory**,
* whose **name is a run marker** — a 32-char lowercase hex GUID, optionally behind a lowercase
  prefix (`zoombie-unpack-<guid>`, `tmp-<guid>`, `zoombie-tesseract-dl-<guid>`),
* whose **parent is exactly** `<root>\work` or `<root>\tmp`.

A user folder that merely sits in a root (`my-notes`), or a `-Output`/`-ImageDir`/`-Vision` destination
(which may live in the workspace `/.tmp`), or a pre-existing non-GUID dir (`subC-…`, `diag-cpu`, `rtf`)
fails that test and is **reported in `spared`, never removed** — proof in §4.

---

## 3. The four required proofs

### 3.1 A default run leaves no scratch (toolchain root / workspace / `C:\Temp`)

Live, on a real 12.4 s three-slide deck (`deck3.mp4`, built with the toolchain ffmpeg), OCR **on**:

```
> python -c "…list <root>\work and <root>\tmp…"     # BEFORE
work ['3b213e20912f43138e813129df28531c']
tmp  ['diag-cpu', 'diag-gpu', 'rtf', 'subC-3ff472577a8c4095b9cc5e4c6c203875']

> cd scripts && python -m zoombie slides -Source ..\.tmp\subF\deck3.mp4 -Output ..\.tmp\subF\item -MinFrameBytes 0
…"scratch":{"path":"C:\\Users\\maxim\\zoombie-env\\work\\78aad034085a432eb8cb6350e38cf265",
            "kept":false,"removed":true,"leftover":false},"asciiSafe":true…

> python -c "…list the roots…"                        # AFTER
work ['3b213e20912f43138e813129df28531c']
tmp  ['diag-cpu', 'diag-gpu', 'rtf', 'subC-3ff472577a8c4095b9cc5e4c6c203875']
```

The run's own dir (`78aad034…`) is **gone**; the pre-existing `3b213e20…` is the **orphan the verb exists for**
(§3.4). After the whole session (several runs + the sweep):

```
work []                                        # empty → nothing of ours survives
tmp  ['diag-cpu','diag-gpu','rtf','subC-3ff472577a8c4095b9cc5e4c6c203875']   # all foreign, all spared

> python -c "…%TEMP% and C:\Temp…"
TEMP= C:\Users\maxim\AppData\Local\Temp
  zoombie-setup-79bbe7249c8148c7b6de08725adfb307 2026-09-19T20:41:20Z   ← days before this subtask
  zoombie-updtest                                 2026-09-20T08:24:56Z   ← days before this subtask
C:\Temp  exists False                                 # no scratch there at all
```

No scratch under the toolchain root, none in the workspace (`workspace guid dirs []`), and `C:\Temp` does not
exist. The two `%TEMP%` entries pre-date the session by 5 days and lie on the ASCII fallback branch of
`new_temp_dir` (used only when the root is non-ASCII), not on any path this subtask touches.

Also live, a **`-DryRun`** — the case that used to leave litter because the work dir is created *before* the
dry return in `readpdf`:

```
> cd scripts && python -m zoombie readpdf -Source ..\.tmp\subE\text.pdf -Output ..\.tmp\subF\pdffirst -DryRun
…"scratch":{"path":"…\\work\\e24fcca10591497183b5f5e4e61de1c3","kept":false,"removed":true,"leftover":false}…
> …list the roots…   work ['my-notes']      ← the dry run left nothing
```

### 3.2 `-KeepScratch` retains it

```
> cd scripts && python -m zoombie slides -Source ..\.tmp\subF\deck3.mp4 -Output ..\.tmp\subF\item2 -MinFrameBytes 0 -KeepScratch
…"scratch":{"path":"C:\\Users\\maxim\\zoombie-env\\work\\61e3eb074d8149ac8bfa0a7dc4bea3ca",
            "kept":true,"removed":false,"leftover":false}…

> …list the roots…
work ['3b213e20912f43138e813129df28531c','61e3eb074d8149ac8bfa0a7dc4bea3ca','my-notes']   ← 61e3… retained
```

The alias is the same decision, live on `readpdf`:

```
> python -m zoombie readpdf -Source ..\.tmp\subE\text.pdf -Output ..\.tmp\subF\pdfkeep -KeepWork
…"scratch":{"path":"…\\work\\8c18ae7219bc434f91ac47873f91aefc","kept":true,…}…
```

### 3.3 `-CleanScratch` removes only toolchain-owned scratch

With `61e3eb07…` (retained), `3b213e20…` (orphan) and `my-notes` (a user folder, deliberately created in the
root) all present:

```
> cd scripts && python -m zoombie clean -CleanScratch -DryRun
{"…","removed":["…\\work\\3b213e20912f43138e813129df28531c","…\\work\\61e3eb074d8149ac8bfa0a7dc4bea3ca"],
 "removedCount":2,"failed":[],
 "spared":["…\\work\\my-notes","…\\tmp\\diag-cpu","…\\tmp\\diag-gpu","…\\tmp\\rtf","…\\tmp\\subC-3ff472577a8c4095b9cc5e4c6c203875"],
 "dryRun":true,…}

> cd scripts && python -m zoombie clean -CleanScratch
   clean -CleanScratch: removed 2 scratch dir(s); 5 non-scratch entr(y/ies) spared
{"…removedCount":2,"failed":[],"spared":[ …the same five… ]}

> …list the roots…
work ['my-notes']                                            ← the user folder SURVIVES
tmp  ['diag-cpu','diag-gpu','rtf','subC-3ff472577a8c4095b9cc5e4c6c203875']
```

`-DryRun` removed nothing (it reported the same two). The user's `my-notes` was spared in the apply run and
removed only by my own `rmdir` afterwards. **The user's `-Output`/`-ImageDir` artifacts are never candidates**:
they are outside both roots, and [`TestSweep::test_the_sweep_never_reaches_outside_a_root`](../tests/test_scratch.py:145)
pins a workspace `/.tmp/<guid>` → `removed == []`.

### 3.4 The killed run's leftover — the case the verb exists for

`remove_quietly`/`remove_work_dir` deliberately never block on a busy directory, so a killed child can leave
`<root>\work\<guid>` behind with nothing to clean it — exactly the `3b213e20…` above (**an orphan of a
previous, killed session**, not of any run in this one). The verb removed it. Unit-level:

```python
# TestKilledRunLeftover::test_an_orphaned_scratch_dir_is_cleanable
assert scratch.is_owned(orphan) is True ; assert str(orphan) in scratch.clean()["removed"]
# TestKilledRunLeftover::test_a_busy_dir_is_reported_not_hidden  (remove_work_dir stubbed to False)
assert block == {"... leftover": True, "removed": False, "hint": "... `clean -CleanScratch` ..."}
```

---

## 4. The `data.next` interaction — **the crux**

`data.next.attach` (and `visionFrames`, `visionPages`) always name files in the **caller's confirmed
destination**, never in scratch. Every producer **copies out of scratch first and removes scratch last**, so a
default run deletes a directory that already contributed nothing to the attach list.

Live, from the §3.1 default run — the attach list and the deleted scratch, in one result:

```
"visionFrames":[{"file":"001 - 00-00-00.png","path":"…\\.tmp\\subF\\item\\.data\\img\\001 - 00-00-00.png"}, …]
"next":{"attach":[{"file":"001 - 00-00-00.png","path":"…\\.tmp\\subF\\item\\.data\\img\\001 - 00-00-00.png","bytes":5758},
                  … 3 entries …], "attachCount":3, "count":3,
        "budget":{"images":3,"bytes":17277}, …}
"scratch":{"path":"C:\\Users\\maxim\\zoombie-env\\work\\78aad034085a432eb8cb6350e38cf265","removed":true, …}
```

**Stated exactly as the brief asks:**

* **Default (cleaning) run:** `data.next.attach` references the `-Output` item's `.data/img/*.png` (all copied
  out before the delete), `budget.bytes = 17277` was computed from those live files, and `data.scratch`
  names the toolchain-root dir the run then removed (`removed: true`, `leftover: false`). **No attachment
  references scratch.** The bounded read E defined happens *after* the run; the files it needs were never in
  scratch.
* **`-KeepScratch` run:** the attach list is *identical* (`attachCount: 3`, `budget.bytes: 17277`, all in the
  item), and `data.scratch.kept` is `true` with the path retained — the flag changes the scratch lifecycle
  **only**, never the advertised read list. So the block is never a lie in either mode: it cannot name a file
  the run just deleted.

Pinned by [`TestRunLeavesNoScratch::test_the_advertised_attach_paths_outlive_the_scratch`](../tests/test_scratch.py:338):
every `next.attach[i].path` exists on disk, none is under the toolchain root, and `budget.bytes > 0`.

**One divergence, stated for completeness.** Slide PNGs live in `<Output>/.data/img` (the item, per D), not in
scratch — so for `slides` scratch cleanup and the attach list are fully independent. For `readpdf -Vision` the
rendered scans are copied `<work>/vision → <Vision>` before the delete, so the same holds. There is **no
command where an attach path points into scratch**, which is why the default mode is safe.

---

## 5. House conventions honoured

* **`-DryRun`/`-Check` write nothing.** The new `-CleanScratch -DryRun` reports `removed`/`spared` and deletes
  nothing (§3.3). `slides`/`readpdf` `-DryRun` still create no artifact — and `readpdf` now also **removes the
  empty work dir it used to leak**, so a planning call leaves the root clean.
* **`-Force` is untouched.** It is the overwrite flag; it has no effect on cleanup (there is no "don't clean"
  state, only `-KeepScratch`/`-KeepWork`). No `ZoombieError` was added: `clean` performs the sweep it was asked
  for and reports, and a busy dir becomes a reported `failed`/`leftover`, not a refusal.
* **Named errors unchanged.** The `data.scratch` block is additive inside `data`; the envelope is still
  `{ok, action, error, data, timestamp}` (E's `TestEnvelopeUnchanged` still green).
* The `-CleanScratch` verb is the **house refusal/mode pattern**: it writes only on apply,
  reports exactly what it removed and what it spared, and never deletes a foreign path.

---

## 6. Tests added (40, in [`tests/test_scratch.py`](../tests/test_scratch.py))

| Class | n | Pins |
|-------|---|------|
| `TestRunMarker` | 15 | the run-marker regex; a GUID **away** from a root is not owned |
| `TestSweep` | 4 | ours removed / foreign spared; `-DryRun` deletes nothing; never reaches outside a root; missing root is not an error |
| `TestKilledRunLeftover` | 2 | the orphan is cleanable; a busy dir is a *reported* leftover naming the verb |
| `TestReport` | 4 | default removes; kept retains; `None` is honest; `-KeepScratch`/`-KeepWork` are both "keep" |
| `TestCliFlags` | 3 | both spellings parse; `-CleanScratch`/`-DryRun` parse; `-CleanScratch` defaults off |
| `TestCleanScratchVerb` | 4 | removes only ours; reports counts; `-DryRun` removes nothing; the legacy default still works |
| `TestRunLeavesNoScratch` | 4 | a real `slides.run` (ffmpeg/OCR stubbed): default removes; **attach paths outlive the scratch**; `-KeepScratch` retains; the retained dir is removable by the verb |
| `TestReadpdfDryRun` | 1 | a real `readpdf.run -DryRun` removes its work dir |
| `TestUnpackScratch` | 2 | a real `unpack` run reports the removed scratch / the retained one |
| `TestDryRunLeavesNoScratch` | 1 | `slides -DryRun` writes no `.data` and no work dir |

Roots are redirected with `ZOOMBIE_ENV_ROOT` (the hermetic pattern), so no test touches the real
`%USERPROFILE%\zoombie-env`.

---

## 7. Plan claims contradicted or corrected

1. **§10 implies only the run's own scratch needs a lifecycle; the `-DryRun` paths were leaking too.**
   `readpdf.run` creates its work dir at line 176 and returned at the dry-run branch ~40 lines later without
   removing it — a planning-only call left an empty `<root>\work\<guid>` behind. F fixes it
   (`readpdf.py` dry-run branch) and `stt.transcribe`'s dry-run branch likewise. The plan never mentions
   dry runs in §10, but "a run cleans up after itself" must include the mode that creates a dir and writes
   nothing.
2. **§10 says "a `-CleanScratch` verb"; the codebase already had `clean`.** Rather than a second verb that
   duplicates `clean`'s surface, `-CleanScratch` is a **flag** on the existing `clean`, and the old default
   behaviour is preserved byte-for-byte (its own test). This keeps one cleanup entry point instead of two
   commands an agent must choose between.
3. **§10 does not say how ownership is decided, and "under the root" is not safe.** The root also holds
   non-run dirs (`diag-cpu`, `rtf`, `subC-…` on this machine). Ownership is the run-marker **name**, not the
   parent — see §2; the live sweep spared all four.
4. **`-KeepWork` is kept as an alias, not replaced.** §10 names `-KeepScratch`, but `-KeepWork` is documented
   in `README.md`, two skills and `unpack -Check`'s help; removing it would break those. Both parse to the
   same decision (`keep_requested`).
5. **§3's 413 root cause is untouched**, as §10 requires: F changes *where scratch lives and who deletes it*,
   not how an image is transported. Full-size PNGs are still produced; §12's compressed reading copies are
   still independent.
6. **§11's claim that "D–F must land first" for G now holds** — F depends on E's bounded read (§4 above), and
   both are green. G is untouched.

### Advisories / honest scope

* **No `transcribe`/`pipeline` live run** was executed (they need whisper + a real media file beyond the
  synthetic deck); their scratch handling is covered by unit tests and by `stt` being driven in the existing
  `test_stt_args.py`/`test_window.py` suites, which stay green. The `data.scratch` nesting in `pipeline`
  (`work`, `downloadDir`, `transcribe`) is therefore unit-verified, not live-verified.
* The live fixture is a **three-colour synthetic deck**, not the Crimson item (whose source video is absent —
  Subtask D's advisory). The scratch path is independent of the content; the deck exercises the real
  ffmpeg/Tesseract paths, real PNG copies and real cleanup.
* The `%TEMP%` entries `zoombie-setup-…`/`zoombie-updtest` pre-date this session and lie on the non-ASCII
  fallback branch; F neither created nor removed them, and the sweep roots deliberately exclude `%TEMP%`.
* `scripts/no` (54 B) appeared from my own `echo ---- … ---- & …` chaining and was deleted; `git status -- scripts/`
  confirms only source files remain untracked/modified.

---

## 8. Commands run (raw, condensed)

```
> python -m pytest tests -q                     # BEFORE → 801 passed in 8.09s
> python -m pytest tests/test_scratch.py -q     # 40 passed in 0.63s
> python -m pytest tests/test_next.py tests/test_skills.py -q   # 57 passed
> python -m pytest tests -q                     # AFTER  → 841 passed in 8.07s
> python -m pytest tests --collect-only -q      # 841 collected

> ffmpeg … concat=3 …                            # build .tmp/subF/deck3.mp4 (3 slides, 12.4s)
> python -c "…list <root>\work, <root>\tmp…"     # before / after each run (see §3)

> cd scripts && python -m zoombie slides … -MinFrameBytes 0                       # scratch removed:true
> cd scripts && python -m zoombie slides … -MinFrameBytes 0 -KeepScratch           # scratch kept:true
> cd scripts && python -m zoombie readpdf … -DryRun                                # scratch removed:true
> cd scripts && python -m zoombie readpdf … -KeepWork                              # scratch kept:true
> cd scripts && python -m zoombie clean -CleanScratch -DryRun                      # removed 2, spared 5
> cd scripts && python -m zoombie clean -CleanScratch                              # removed 2, spared 5
> cd scripts && python -m zoombie transcribe … -From 00:00:10 -To 00:00:05         # refused; NO dir created
> python -c "…%TEMP%, C:\Temp, workspace guid dirs…"                               # all clean
> git status --porcelain ; git diff --stat                                        # the F change set
```

Nothing under `scripts/` was left behind. The evidence fixture is under the gitignored `.tmp/subF/`.

---

## 9. Checklist for the parent (§1.1 rule 3)

- [x] Receipt exists and is newer than the start (`…21:30Z` > `…21:10:57Z`).
- [x] Every artifact in §1 exists on disk.
- [x] The quoted output is plausible against the code and was produced by the commands shown.
- [x] **No receipt ⇒ not run** — this file is the deliverable.
- [x] `python -m pytest tests` = **841 passed** (≥ 801).
- [x] Default run leaves no scratch under the root / workspace / `C:\Temp` (§3.1).
- [x] `-KeepScratch` retains it (§3.2).
- [x] `-CleanScratch` removes only toolchain-owned scratch (§3.3).
- [x] A killed run's leftovers are cleanable (§3.4).
- [x] The `data.next` interaction is stated for default vs `-KeepScratch` (§4).
- [x] **Subtask G was not started.**

# Subtask B — unpack as a first-class capability

Plan: [`plans/zoombie-image-pipeline-rework.md`](../plans/zoombie-image-pipeline-rework.md) §6.
Protocol: §1.1. Advisories absorbed: **A-1** and **A-2** from
[`feedback/subtask-A-step1-audit.md`](subtask-A-step1-audit.md).
Subtask start (UTC): 2026-09-25T20:20:31Z. Receipt written (UTC): 2026-09-25T20:28Z (newer than start;
`dir /T:W` → `25.09.2026 23:28` local = 20:28 UTC).

Result: **GREEN.** `python -m pytest tests` → **743 passed** (floor was 720, +23).
Subtasks C and D–G were **not** started.

---

## 0. Baseline recorded before any change (§1.1 rule 3)

```
> python -m pytest tests -q            # repo root, c:/Users/maxim/source/repos/zoombie
........................................................................ [ 10%]
... (10 lines) ...
720 passed in 6.22s
```

Baseline confirmed **720**, exactly the floor Subtask A recorded. The current
extraction surface in `lib/tesseract.py` was `install_extractor()` (then lines
289–315) and `extract_installer()` (then lines 336–359) — both still exist and are
now thin wrappers, so no public name was removed.

---

## 1. Artifacts (every path below exists on disk)

| Artifact | State | Role |
|----------|-------|------|
| [`scripts/zoombie/lib/unpack.py`](../scripts/zoombie/lib/unpack.py) | new | extractor provisioning + NSIS/7z extraction + the scaffolding-aware payload copier |
| [`scripts/zoombie/commands/unpack.py`](../scripts/zoombie/commands/unpack.py) | new | the `unpack` CLI verb |
| [`scripts/zoombie/lib/tesseract.py`](../scripts/zoombie/lib/tesseract.py) | refactored | imports `unpack`; its public surface is unchanged |
| [`scripts/zoombie/cli.py`](../scripts/zoombie/cli.py) | modified | parser + dispatch for `unpack` |
| [`scripts/zoombie/install/components.py`](../scripts/zoombie/install/components.py) | modified | new `install_extractors()` component |
| [`scripts/zoombie/install/main.py`](../scripts/zoombie/install/main.py) | modified | calls the component, adds the `extractor` manifest block and missing-check |
| [`tests/test_unpack.py`](../tests/test_unpack.py) | new | 19 tests |
| [`tests/test_tesseract.py`](../tests/test_tesseract.py) | extended | 8 → 12 tests |
| `scripts/zoombie/lib/archive.py` | **untouched** | still zip-only, stdlib-only (verified below) |

`lib/archive.py` was not modified: `git status --porcelain` lists it nowhere, and it
still imports only `os`, `zipfile` and our own `paths`/`process`.

---

## 2. Test count and which tests were added

```
> python -m pytest tests/test_unpack.py -q
..................                                                       [100%]
18 passed in 1.28s

> python -m pytest tests/test_unpack.py -q          # after the last -Strip test
...................                                                      [100%]
19 passed

> python -m pytest tests/test_tesseract.py -q
............                                                             [100%]
12 passed in 0.37s

> python -m pytest tests -q
........................................................................ [  9%]
... (11 lines) ...
743 passed in 7.66s
```

**Count: 743 passed, 0 failed.** `720 → 743` = **+23** (`tests/test_unpack.py` 19,
`tests/test_tesseract.py` 8 → 12 = +4). **≥ 720 holds.**

New / changed tests, by requirement:

* **dry-run writes nothing** — `tests/test_unpack.py::TestCliModes::test_dry_run_writes_nothing_to_the_destination`
  and `TestExtractorComponent::test_dry_run_reports_and_writes_nothing` (asserts the
  destination / `bin` dir does not exist afterwards).
* **hash mismatch refused** — `TestHashVerification::test_a_mismatch_is_refused`
  (asserts `"hash mismatch"` and that a mismatch MUST raise) plus
  `test_a_match_is_accepted`.
* **strip/include filtering** — `TestFiltering::test_strip_drops_leading_path_levels`,
  `test_include_matches_full_path_and_base_name`,
  `test_strip_include_are_applied_in_select_payload`,
  `TestCliModes::test_include_filters_what_is_copied`, `test_strip_drops_the_leading_folder`.
* **reuse of the hermetic fake-extractor pattern** — `tests/test_unpack.py::_payload` /
  `_install` / `_scratch` are the same idiom as `tests/test_tesseract.py`'s
  `_payload` / `_fake_extract` (`monkeypatch.setattr(unpack_mod, "extract", …)`); no
  network and no installer is executed. A live NSIS fixture was not required and was
  not used in the suite.
* **A-1** — `TestScaffoldingFilter::test_select_payload_excludes_the_scaffolding_directory`,
  `test_a_plugins_dir_component_is_scaffolding`,
  `test_an_ordinary_product_dll_is_not_scaffolding`; plus
  `tests/test_tesseract.py::TestPayloadFiltering::test_installer_scaffolding_is_excluded_by_directory`.
* **A-2** — `tests/test_tesseract.py::TestPostExtractionProbe` (3 tests), below.
* **CLI modes / no-overwrite** — `test_check_writes_nothing_and_lists_entries`,
  `test_apply_copies_the_selected_payload`, `test_an_existing_target_is_refused_without_force`,
  `test_force_overwrites_the_existing_target`, `test_a_missing_source_is_refused_not_created`.
* **own component** — `TestExtractorComponent::test_an_incomplete_extractor_is_reported`,
  `test_a_complete_extractor_short_circuits_without_downloading` (asserts nothing re-downloads).

---

## 3. A-1 — the residue: filtering is now by DIRECTORY, not only by extension

**The fix.** `lib/unpack.py` owns the single choke point
[`copy_payload()`](../scripts/zoombie/lib/unpack.py:333), which:
1. excludes installer-scaffolding **directories** (`SCAFFOLDING_DIRS = ("$PLUGINSDIR",)`,
   matched as a whole case-insensitive path *component*, so `a/$PLUGINSDIR/x.dll` is
   excluded too), then
2. applies `-Strip` and `-Include`, then
3. applies a product-specific `predicate` (Tesseract's runtime-vs-SDK rule).

`tesseract.install_engine()` was rewritten from its own `os.walk` + `_wanted` loop to
`unpack.copy_payload(payload, dest_dir, predicate=lambda r: _wanted(r, keep_unneeded))`,
and `_wanted` no longer has to reason about scaffolding at all.

**Assertions that prove it:**

```python
# tests/test_unpack.py
selected = {item["relative"] for item in unpack_mod.select_payload(str(root))}
assert "libtesseract-5.dll" in selected                       # the product's own DLL: kept
assert "$PLUGINSDIR/System.dll" not in selected               # a DLL, but scaffolding
assert "$PLUGINSDIR/nsDialogs.dll" not in selected
assert not any("PLUGINSDIR" in name for name in selected)
```

and, through the real Tesseract path:

```python
# tests/test_tesseract.py::TestPayloadFiltering::test_installer_scaffolding_is_excluded_by_directory
assert not (dest / "$PLUGINSDIR").exists()
assert (dest / "libtesseract-5.dll").exists()                 # directory-scoped, not a DLL ban
assert not any("$PLUGINSDIR" in name for name in info["files"])
```

The payload fixture now contains the measured residue exactly —
`$PLUGINSDIR\{System,nsDialogs}.dll` — so the test fails against the old blanket
`.dll` rule and passes against the new one. (The fixture uses two of the six measured
files; the other four are `INetC`, `LangDLL`, `StartMenu`, `UserInfo`, all under the
same directory and therefore covered by the same component test.)

**Live proof with the real provisioned 7-Zip 26.03** (a throwaway script, created and
deleted; scratch under the ASCII toolchain root, removed afterwards):

```
extractor: C:\Users\maxim\zoombie-env\bin\7z.exe present: True
7z create exit: 0
list_archive: ['$PLUGINSDIR\\nsDialogs.dll', '$PLUGINSDIR\\System.dll', 'libtesseract-5.dll', 'tessdata\\configs\\txt', 'tesseract.exe']
--- CLI apply ---
{"ok":true,"action":"unpack","error":null,"data":{...,"count":3,
 "files":["libtesseract-5.dll","tessdata/configs/txt","tesseract.exe"],
 "bytes":19,"scaffoldingExcluded":true,"written":3},"timestamp":"2026-09-25T20:26:37.368Z"}
cli exit: 0
written: ['libtesseract-5.dll', 'tessdata/configs/txt', 'tesseract.exe']
PLUGINSDIR present in dest: False
ascii work dir: True
```

So with the real extractor: the archive *contains* the two `$PLUGINSDIR` DLLs, the
copier wrote 3 files, and the scaffolding directory is absent from the destination.

---

## 4. A-2 — the silent drop: a post-extraction probe that gates on the EXIT CODE

**The fix.** [`unpack.require_runs()`](../scripts/zoombie/lib/unpack.py:259) runs the
provisioned executable and raises unless it exits **0**. `tesseract.install_engine()`
now calls `unpack.require_runs(engine, "tesseract.exe")` after the copy, so a payload
that lost or renamed a DLL it loads fails at **provision time**.

Note deliberately: `engine_version()` was **not** changed, and `install_tesseract`'s
gate was **not** hotfixed (the brief forbids a `tesseract.py` hotfix of the advisories).
The new gate is the promoted capability's, and it makes the failure loud one layer
earlier than any `engine_version()` change could.

**Assertions that prove it:**

```python
# tests/test_tesseract.py::TestPostExtractionProbe
def test_a_non_zero_probe_exit_is_refused(monkeypatch):
    monkeypatch.setattr(unpack_mod, "runs", lambda *a, **k: (3221225781, "tesseract v5.5.3\n"))
    with pytest.raises... # assert "exit 3221225781" in str(exc)

def test_install_engine_refuses_a_payload_that_cannot_start(tmp_path, monkeypatch):
    # the DLL the exe loads is gone -> the probe reports 1 -> install_engine must raise
    assert "failed its '--version' probe" in str(exc)
```

**Live proof — and it corrects the plan's own wording.** Against the *real* engine:

```
bare copy runs:         (3221225781, '')
real engine runs:       (0, 'tesseract v5.5.3.20260724\r\n leptonica-1.87.0\r\n…')
require_runs REFUSED:   ... (a bare tesseract.exe copy, no sibling DLLs)
```

`3221225781` = `0xC0000135` = `STATUS_DLL_NOT_FOUND`.

---

## 5. Manifest component (own block, not implicit in Tesseract)

`components.install_extractors()` is a new component; `install/main.py` runs it
*before* Tesseract, threads `extractor_info` into `_build_manifest`, adds the
missing-list check `if not paths.is_file(unpack.extractor_path()): missing.append("extractor")`,
and emits a top-level `extractor` block. An offline, read-only `-Check` run proves the
envelope:

```
> cd scripts && python -m zoombie.install -Check      # writes nothing
extractor block: {
 "path": "C:\\Users\\maxim\\zoombie-env\\bin\\7z.exe",
 "name": "7z.exe",
 "version": "26.03",
 "bootstrap": { "name": "7zr.exe", "version": "26.03" },
 "sha256": "0859c524b8a63551848f0c246abddcb1d0b7b656b0fbfe879f8d85e61a9e6edd",
 "ok": true,
 "note": null
}
tesseract.extractor: {"name": "7z.exe", "version": "26.03"}
missing: []
```

The same `built` dict is what `manifest.save()` writes to `env.json`, so `env.json`
gains the identical top-level `extractor` key. `tesseract.extractor` is retained
unchanged for continuity, so no existing consumer breaks. `install_extractors` is
idempotent (`7z.exe` **and** `7z.dll` complete → short-circuit, no download) and
honours `-Check`/`-DryRun` by writing nothing.

---

## 6. Public surface: `tesseract.py` unchanged beyond the refactor

```
> cd scripts && python -c "from zoombie.lib import tesseract, unpack; …"
EXTRACTOR is unpack.EXTRACTOR: True
EXTRACTOR_BOOTSTRAP is unpack: True
all names present: True
missing from __all__: []
```

* `tesseract.EXTRACTOR` / `EXTRACTOR_BOOTSTRAP` are now **the same objects** as
  `unpack`'s, so `tesseract.EXTRACTOR["version"]` (used in `install/main.py` before the
  change) keeps its value and identity semantics.
* `sha256_file`, `verify_archive`, `extractor_path`, `install_extractor`,
  `extract_installer` keep their names and signatures. `verify_archive` preserves the
  message prefix so `TestHashVerification::test_a_mismatched_archive_is_refused`
  (asserting `"hash mismatch"`) still passes.
* `_wanted` keeps its signature; `install_engine` keeps `keep_unneeded`, now also
  suppressing the probe (which is what a "keep everything" diagnostic wants).
* Two names were **added** to `tesseract.__all__` (`DEFAULT_LANGUAGES`, `engine_dir`)
  so the module's declared surface matches what `install/main.py` already imported
  from it. Nothing was removed.

---

## 7. CLI verb

```
unpack -Source <exe|7z> -Output <dir> [-Strip <n>] [-Include <glob>] [-Check] [-DryRun] [-Force] [-KeepWork]
```

* **Modes:** `-Check` lists the archive (`7z l -slt`) and extracts nothing, so an
  unprovisioned machine is never forced to download; `-DryRun` extracts to an ASCII
  scratch dir and reports the real file list but writes nothing to the destination and
  does not provision the extractor; apply provisions it on demand.
* **ASCII work dir:** extraction happens in `paths.new_temp_dir("zoombie-unpack")`
  under the toolchain root and results are copied with managed I/O, so no native tool
  receives a non-ASCII path. `paths.assert_fits` guards source and destination.
* **Confirmed destination:** the destination copy follows the house convention —
  no overwrite unless `-Force`; an existing target aborts with a named error listing
  the conflicts, and the refused run was proven not to clobber the existing file.
* The payload carries `asciiSafe: true`, `scaffoldingExcluded: true`, a bounded `files`
  list (`filesTruncated` past 200) so a listing cannot become a token sink.

`_wanted`'s "blanket `.dll` rule" is unchanged for the *product* tree on purpose: a
Tesseract DLL anywhere outside `$PLUGINSDIR` is still runtime. Directory filtering is
what the advisory asked for, and it is now enforced one layer above the predicate.

---

## 8. Plan claims my work contradicts (measured)

1. **§6 / audit R1, the A-2 premise.** The plan and the audit describe the failure as
   *"``tesseract.exe --version`` may print its banner to stdout and **exit non-zero**
   while a needed DLL is missing"*. Measured against the real engine, a missing DLL
   means the process **never starts**: exit `3221225781` (`STATUS_DLL_NOT_FOUND`) with
   **no banner at all** — `(3221225781, '')`. So it is not "a banner plus a non-zero
   exit" that `engine_version()`'s `code != 0 and not text` misses; it is a *launch
   failure with empty output*, which that expression happens to let through in a
   different way (it `continue`s, then finds no match and returns `None` — hence the
   silent drop). The fix is therefore an exit-code gate and not a banner-regex change.
   The plan's framing of the mechanism is what is contradicted; its remedy (gate on the
   exit code) is right and is what was implemented.
2. **§6 "extracted from ... around lines 289-370".** Accurate as a pointer to the two
   functions, but `_wanted()` also carries the residue cause (its blanket `.dll` rule is
   the *reason* `$PLUGINSDIR` leaked), and the fix could not be local to those two
   functions: the exclusion had to move above `_wanted` into a shared choke point, which
   is why `lib/unpack.py` owns `copy_payload()`.
3. **§6 "CLI verb ... honouring `Modes` (`-Check`/`-DryRun`/apply)".** `Modes` is an
   `install`-package type ([`components.Modes`](../scripts/zoombie/install/components.py:50));
   the `commands/*` layer has no `Modes` and expresses the same three states as
   `-Check` / `-DryRun` / apply, with `-Force` as the overwrite control. The verb follows
   the *commands* convention rather than importing the install type.
4. **§13 checklist item "No scratch left under the toolchain root".** Still contradicted
   at the engine-dir level for the **already-provisioned** tree: the fix changes future
   extractions, and the existing
   `C:\Users\maxim\zoombie-env\tesseract\$PLUGINSDIR\*.dll` (96,768 B) was **not** cleaned
   up, because this subtask was scoped to promotion and the brief forbade a
   `tesseract.py` hotfix. A `-Force` reinstall would now produce a clean tree.

---

## 9. Interpretation / honesty notes

* The live 7-Zip proof and the live `--version` proof were run from throwaway scripts
  and temporary dirs only; the script was deleted (`scripts/_b_live_check.py` is gone)
  and nothing was added to `scripts/`. `git status --porcelain` shows only the intended
  source/test additions and no scratch.
* The archive used for the live proof was a genuine `.7z` created by the provisioned
  `7z.exe` from a `$PLUGINSDIR`-shaped tree, not a real NSIS installer. The real
  installer is what `install_engine` consumes and is covered by the hermetic fake plus
  the pre-existing ComponentModes tests; a live NSIS fixture was explicitly not required.
* `install/main.py`'s `missing` list gained an `extractor` entry, so on a machine
  without `bin\7z.exe` a default `python -m zoombie.install` now reports that gap. That
  is intended (the extractor is toolchain-owned like ffmpeg/whisper) and it does not
  affect `-Check`/`-DryRun` result shape beyond the new name when genuinely absent.

## 10. Verification commands run (raw, condensed)

```
> python -m pytest tests -q                                   # BEFORE  → 720 passed
> python -m pytest tests/test_tesseract.py -q                 # mid     → 12 passed
> python -m pytest tests/test_unpack.py -q                    # mid     → 18 passed ; then 19
> python -m pytest tests -q                                    # AFTER   → 743 passed in 7.66s
> python scripts/_b_live_check.py                              # live real-7z proof (script since deleted)
> cd scripts && python -m zoombie.install -Check               # offline; extractor envelope + missing:[]
> cd scripts && python -c "… tesseract/unpack identity …"       # EXTRACTOR is unpack.EXTRACTOR: True
> cd .. && git status --porcelain                               # only the intended additions
```

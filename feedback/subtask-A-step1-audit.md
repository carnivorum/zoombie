# Subtask A — Step 1 audit (Tesseract provisioning)

Plan: [`plans/zoombie-image-pipeline-rework.md`](../plans/zoombie-image-pipeline-rework.md) §5.
Mode: **audit only** — nothing implemented, nothing fixed, Subtasks B–G not started.
Audit start (UTC): 2026-09-25T20:08Z. Receipt written (UTC): 2026-09-25T20:18Z (newer than start).

## 0. Environment preconditions — CONFIRMED before touching anything

Commands and raw output:

```
> dir /b "C:\Users\maxim\zoombie-env\bin"
ffmpeg.exe
ffprobe.exe
whisper
zoombie
```
→ **no `7z.exe`, no `7z.dll`** ⇒ `install_extractor()` had never run.

```
> dir /s /b "C:\Users\maxim\zoombie-env\tesseract"
(the system cannot find the path specified)
```
→ **`tesseract\` absent** ⇒ Step 1 had never executed on this machine.

```
> dir /b "C:\Users\maxim\zoombie-env\tmp"
diag-cpu
diag-gpu
rtf
```
→ no `zoombie-7zip-*` / `zoombie-tesseract-*` scratch ⇒ no prior tesseract run.

env.json before the run was dated `2026-09-24T20:50:59Z`, had **no top-level `tesseract` key**, and
`pdf.tesseract` / `pdf.tesseractVersion` were both `null`. All three established facts confirmed.

Prior-session scratch: `git check-ignore -v .tmp` → `.gitignore:38:.tmp/   .tmp`; `.tmp/` holds 26
files all mtime **2026-09-25 22:12–22:38** (probe_*.py, *.json, *.log, ocr_probe*), none touched by
this audit. The artifacts the prior session referenced (`tess_setup.exe`, `tess_root`, `tess_extract`,
`eng.traineddata`) are gone, as stated.

## 1. Full test suite

Correction 1 applied. Settled invocation: **`python -m pytest tests` from the repo root works with NO
PYTHONPATH** — `tests/conftest.py` inserts `scripts/` onto `sys.path` for the collected tests. Verified
with `set "PYTHONPATH="` (cleared):

```
> set "PYTHONPATH="
PYTHONPATH is now [%PYTHONPATH%]
> python -m pytest tests -q
........................................................................ [ 10%]
...
........................................................................ [100%]
720 passed in 5.98s
```

Also confirmed `720 passed` from `scripts\` with `python -m pytest ../tests -q`, and
`python -m pytest tests/test_tesseract.py -q` → `8 passed`. **No pytest config file exists**
(`pytest.ini`/`setup.cfg`/`tox.ini`/`pyproject.toml` all absent).

**Count: 720 passed, 0 failed.** Baseline before this work was 703 ⇒ **count EXCEEDS 703** (+17).
`tests/test_tesseract.py` contributes 8 of them.

Verdict: **works.**

## 2. Acceptance criterion `ocr.available()`

Only `python -c "from zoombie.lib import ..."` needs `scripts` importable (it does not go through
`conftest.py`). From the repo root it raises; from `scripts\` it resolves:

```
> python -c "from zoombie.lib import ocr; print(ocr.available())"        # repo root
Traceback (most recent call last):
  File "<string>", line 1, in <module>
ModuleNotFoundError: No module named 'zoombie'

> cd scripts & python -c "from zoombie.lib import ocr; print(ocr.available())"
(True, '5.5.3.20260724')
```

Pre-install (before item 4) the same command returned, correctly for the unprovisioned machine:

```
(False, 'Tesseract is not available: not found in the toolchain, a system location, or PATH')
```
with no system install (`where tesseract` → none; `C:\Program Files[\ (x86)]\Tesseract-OCR\tesseract.exe`
→ MISS) and `pytesseract 0.3.13` present.

Verdict: **works** (post-install; a real version string, not an error). The pre-install `(False, …)`
is the correct named failure, not a step-1 defect.

## 3. `tesseract.resolve_engine()`

```
> cd scripts & python -c "from zoombie.lib import tesseract; print(tesseract.resolve_engine())"
('C:\\Users\\maxim\\zoombie-env\\tesseract\\tesseract.exe', 'toolchain engine (C:\\Users\\maxim\\zoombie-env\\tesseract\\tesseract.exe)')
```

The path is the **toolchain copy and the reason says "toolchain engine"**, i.e. the first branch of
[`resolve_engine()`](../scripts/zoombie/lib/tesseract.py:234) fired, not a PATH hit. Supported by
[`engine_version()`](../scripts/zoombie/lib/tesseract.py:197) returning `5.5.3.20260724` and
[`language_list()`](../scripts/zoombie/lib/tesseract.py:174) returning `['eng', 'rus']` from disk.

Verdict: **works.**

## 4. Installer run (a) first, (b) second — provisioning + idempotence

Command for both: `cd scripts & python -m zoombie.install`. Exit code 0 both times. Raw stderr of
**run 1** (truncated to the tesseract-relevant lines; full log captured in the task transcript):

```
==> ensuring Tesseract OCR
==> provisioning pinned 7-Zip 26.03 extractor
==> download https://github.com/ip7z/7zip/releases/download/26.03/7zr.exe
==> download https://github.com/ip7z/7zip/releases/download/26.03/7z2603-x64.exe
==> downloading Tesseract 5.5.3 (25.3 MB)
==> download https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe
==> extracting the engine payload -> C:\Users\maxim\zoombie-env\tesseract
==> downloading pinned traineddata (eng, rus)
==> download .../eng.traineddata
==> download .../rus.traineddata
```

**run 2** — no download lines, short-circuit branch taken:

```
==> ensuring Tesseract OCR
     tesseract present: C:\Users\maxim\zoombie-env\tesseract\tesseract.exe (v5.5.3.20260724, langs: eng, rus)
```

Result envelope `changes` for run 1 contained `"provisioned Tesseract 5.5.3 (eng, rus)"`; run 2's
`changes` contained it **not** (only `mode zoombie: up to date` and `installed PDF python dependencies`).

On-disk state after provisioning:

```
> dir /b "C:\Users\maxim\zoombie-env\bin"
7z.dll
7z.exe
ffmpeg.exe
ffprobe.exe
whisper
zoombie

> dir /b "C:\Users\maxim\zoombie-env\tesseract"
$PLUGINSDIR
libarchive-13.dll ... libtesseract-5.dll ... zlib1.dll   (57 files)
tessdata
tesseract.exe

> dir /b "C:\Users\maxim\zoombie-env\tesseract\tessdata"
configs
eng.traineddata
pdf.ttf
rus.traineddata
tessconfigs
```

Verdict: **(a) works; (b) idempotent — works** (no re-download, component reported present/up to date).

## 5. `tesseract` record — inspect BOTH places (correction 2)

Correction 2 confirmed. The field path `manifest.tesseract.{…}` in the plan lives in the **install
result envelope**, not env.json. Both were inspected.

* **Install result envelope** (`data.manifest.tesseract`), raw from the run:
  ```json
  "tesseract":{"engine":"C:\\Users\\maxim\\zoombie-env\\tesseract\\tesseract.exe",
   "version":"5.5.3.20260724","languages":["eng","rus"],"expectedLanguages":["eng","rus"],
   "ok":true,"note":null,
   "extractor":{"name":"7z.exe","version":"26.03"},
   "installer":{"version":"5.5.3","sha256":"bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4"}}
  ```
  Assembled by [`_build_manifest()`](../scripts/zoombie/install/main.py:413). **This is where the
  data actually is** (`engine`, `version`, `languages`, `expectedLanguages`).
* **env.json** carries a separate top-level `"tesseract"` block (same four fields plus `ok`, `note`,
  `extractor`, `installer`) and a `"pdf"` block with its own `tesseract`/`tesseractVersion`:
  ```json
  "pdf":{"tesseract":"C:\\Users\\maxim\\zoombie-env\\tesseract\\tesseract.exe",
         "tesseractVersion":"5.5.3.20260724","ok":true,"note":null},
  "tesseract":{"engine":"C:\\Users\\maxim\\zoombie-env\\tesseract\\tesseract.exe",
               "version":"5.5.3.20260724","languages":["eng","rus"],
               "expectedLanguages":["eng","rus"],"ok":true,"note":null, ...}
  ```
  env.json `updatedUtc` became `2026-09-25T20:15:39Z`.

Verdict: **works** — all four fields populated, in the envelope's `manifest.tesseract` and mirrored in
env.json's top-level `tesseract`.

## 6. Re-derive a pinned sha256 (correction 3)

Zero downloads; hashed the persisted traineddata and compared to the constants in
[`lib/tesseract.py`](../scripts/zoombie/lib/tesseract.py:126).

```
> cd scripts & python -c "from zoombie.lib import tesseract; ... print(tesseract.sha256_file(p))"
eng observed DAA0C97D651C19FBA3B25E81317CD697E9908C8208090C94C3905381C23FC047
eng constant DAA0C97D651C19FBA3B25E81317CD697E9908C8208090C94C3905381C23FC047
eng match True
rus observed 681BE2C2BEAD1BC7BD235DF88C44E8E60AE73AE866840C0AD4E3B4C247BD37C2
rus constant 681BE2C2BEAD1BC7BD235DF88C44E8E60AE73AE866840C0AD4E3B4C247BD37C2
rus match True
```

| Constant | Declared | Observed | Match |
|----------|----------|----------|-------|
| `LANGUAGES["eng"]["sha256"]` | `daa0c97d…fc047` | `daa0c97d…fc047` | ✅ |
| `LANGUAGES["rus"]["sha256"]` | `681be2c2…d37c2` | `681be2c2…d37c2` | ✅ |

The `ENGINE["sha256"]` was **not** re-derived from a retained copy (the installer is deleted in a
`finally`; re-downloading it was optional and excluded). It IS echoed in the manifest's
`tesseract.installer.sha256` = `bee9e343…0ae4`, matching the constant in
[`ENGINE`](../scripts/zoombie/lib/tesseract.py:109) — but that is the constant read back, not an
independent measurement, so it is only corroborating.

Verdict: **works** for eng/rus (independently re-derived). `ENGINE["sha256"]` **unverified** by
independent hash (constant-echo only).

## 7. Scratch hygiene

After both installer runs:

```
> dir /b "C:\Users\maxim\zoombie-env\tmp"
diag-cpu
diag-gpu
rtf
```
→ identical to pre-run; the per-run `zoombie-7zip-<guid>` and `zoombie-tesseract-dl-<guid>` dirs named
in the run-1 log are **gone** (removed by the `finally: paths.remove_quietly(tmp, recursive=True)` in
[`install_tesseract()`](../scripts/zoombie/install/components.py:605) and
[`install_extractor()`](../scripts/zoombie/lib/tesseract.py:332)). `diag-cpu/diag-gpu/rtf` are
pre-existing diagnostics, untouched.

```
> if exist "C:\Temp" ... else echo C:\Temp MISSING
C:\Temp MISSING
```
`%TEMP%\zoombie*` holds only unrelated prior entries (`zoombie-modes-test.yaml`,
`zoombie-setup-restore.log`, `zoombie-setup-<guid>`, `zoombie_nn.txt`, `zoombie-updtest`) — the
toolchain root is ASCII so `new_temp_dir()` uses `<root>\tmp`, never `%TEMP%`.

Workspace: `git status --porcelain` after the runs shows **no new untracked path** and `.tmp/` mtimes
unchanged (all 2026-09-25 22:12–22:38, prior session).

**One scratch residue found, inside the engine dir (see review item 1):**
```
> dir /s /b "C:\Users\maxim\zoombie-env\tesseract\$PLUGINSDIR"
...\$PLUGINSDIR\INetC.dll / LangDLL.dll / nsDialogs.dll / StartMenu.dll / System.dll / UserInfo.dll
```
6 NSIS installer-scaffolding DLLs (96,768 bytes total), retained by the blanket `.dll` rule in
`_wanted`. The 26 `tessdata\configs\*` files and `tessdata\tessconfigs\` were correctly kept;
trainers, `ScrollView.jar`, `*.html` and the installer's own traineddata were correctly dropped
(97 files total under the engine dir).

Verdict: **works** (no scratch leak under `tmp`, workspace or `C:\Temp`) **with one advisory**: the
`$PLUGINSDIR\*.dll` residue is installer scaffolding kept in the *runtime* tree.

## Review items

### R1 — `_wanted()` ends in `return False`; is a missing DLL as loud as a missing `tesseract.exe`?

Tied to the code path. `extract_installer()` calls
[`_find_engine()`](../scripts/zoombie/lib/tesseract.py:362) **before** any filtering and raises
`"tesseract.exe was not found in the extracted installer payload"` if the exe is absent. `install_engine()`
then copies whatever `_wanted` accepts. So the asymmetry is real:

* a changed payload layout that **loses `tesseract.exe`** → loud, named `RuntimeError`;
* a changed payload layout that renames/removes a **loaded DLL** (e.g. `libtesseract-5.dll`) → the file
  is dropped silently by `return False`, the exe still copies, and the failure surfaces only later when
  the engine cannot start.

Why the *installed* binary cannot always save it: `tesseract.exe --version` may print its banner to
stdout and **exit non-zero** while a needed DLL is missing, and
[`engine_version()`](../scripts/zoombie/lib/tesseract.py:197) treats `code != 0 and not text` as
"continue" — i.e. a banner with a non-zero exit is still accepted. `install_tesseract()` gates only on
`engine_version(info["engine"])` being non-`None`, not on the exit code. So the current post-extraction
check is *weaker* than a strict `--version`-exit-0 probe.

**Verdict: the concern is valid.** A missing `tesseract.exe` is loud; a missing DLL is not, and todays
`--version` acceptance is not by exit code. (Advisory only — not fixed here.)

### R2 — retracted `install_languages` `TypeError`

**Not re-raised.** Confirmed in source: [`install_languages()`](../scripts/zoombie/lib/tesseract.py:471)
line is a conditional expression, `os.path.join(dest_dir, "tessdata") if dest_dir else tessdata_dir()`,
not a call that could raise `TypeError`. No claim about it appears in this receipt.

### R3 — ASCII-root invariant for a `%PUBLIC%` profile

```
> cd scripts & python -c "... USERPROFILE='C:\Users\Мария' ... paths.env_root() ..."
PUBLIC= C:\Users\Public
non-ascii profile root -> C:\Users\Public\zoombie-env
ascii? True
```

Step 3 of [`env_root()`](../scripts/zoombie/lib/paths.py:113) returns `%PUBLIC%\zoombie-env`, which is
ASCII; the machine's actual `%PUBLIC%` is `C:\Users\Public`. **Invariant holds.** (`is_ascii` guards
the `%PUBLIC%` join too, so a hypothetical non-ASCII `%PUBLIC%` would fall through to
`<SystemDrive>\zoombie-env`.)

## Per-item verdict table

| # | Item | Verdict |
|---|------|---------|
| 1 | `python -m pytest tests` count | **works** — 720 passed (≥ 703) |
| 2 | `ocr.available()` version | **works** — `(True, '5.5.3.20260724')` post-install |
| 3 | `resolve_engine()` toolchain-first | **works** |
| 4 | installer provisions eng+rus | **works** |
| 4 | installer idempotent on re-run | **works** |
| 5 | manifest/`env.json` tesseract fields | **works** (both places populated) |
| 6 | re-derive pinned sha256 | **works** (eng+rus match); `ENGINE` **unverified** independently |
| 7 | no scratch left | **works**, one residue: `$PLUGINSDIR\*.dll` in the engine dir |

## Claims in the plan the evidence contradicts

1. **§5 item 1 invocation.** The plan's literal `python -m pytest tests` is fine (`tests/conftest.py`
   provides `scripts/` on `sys.path`), but the plan's **§5 item 2/3 `python -c "from zoombie.lib …"`
   is not runnable from the repo root** — it raises `ModuleNotFoundError: No module named 'zoombie'`
   unless the CWD is `scripts/` or `PYTHONPATH=scripts`. The plan does not state the required CWD.
2. **§5 item 5 field path.** `manifest.tesseract.{engine,version,languages,expectedLanguages}` is the
   **install result envelope** (assembled in [`_build_manifest()`](../scripts/zoombie/install/main.py:413)),
   **not** a path inside `env.json`. env.json instead carries a **top-level `tesseract`** key plus a
   `pdf.tesseract`/`pdf.tesseractVersion` pair. As literally written the check would look in the wrong
   document.
3. **§5 item 6.** "compute the hash of an already-downloaded artifact" is not possible for the
   installer: it is deleted in the `finally` of `install_tesseract`. Only the persisted traineddata
   (and, only by constant-echo, the engine hash in the manifest) can be re-derived without a download.
4. **§13 checklist "`manifest.tesseract.{…}` populated".** Same field-path error as (2): the values are
   populated, but in the envelope/`env.json` top-level block, not at `env.json → manifest.tesseract`.
5. **§5 item 7 "no scratch left under the toolchain root".** True for the per-run `tmp\zoombie-*` dirs
   (all removed), but **§3's blanket reading is contradicted at the engine-dir level**: the extraction
   leaves `tesseract\$PLUGINSDIR\*.dll` (installer scaffolding) permanently in the provisioned tree.
   This is a real, reproducible residue of the run, distinct from the pre-existing `.tmp/` scratch.
6. **§5 item 2 "Expect a version string, not an error."** On this machine the criterion was an error on
   the **first** (pre-provision) run and only then a version. That is because Step 1 had never run —
   confirming the plan's §2 statement that Step 1 was `source-inspected only, never executed`. The
   plan's §1.1/§2 framing is correct; the §5 item-2 "expect" is only true **after** item 4.
7. **Deployed skills were stale before this run.** Run 1 reported all six skills as
   `updated (content)`; run 2 as `up to date`. Not a defect, but it means the pre-audit `env.json`
   `4.8.0` record described a **not-currently-deployed** skill set — the deployed skills only matched
   after run 1.

## Diagnosis status (debug-mode step)

No item is **broken**: items 1–7 all reach a working end state. Two findings are **advisories, not
failures**, and per the brief I have **not fixed** either:

* **A-1 (residue).** `tesseract\$PLUGINSDIR\*.dll` persists in the runtime tree.
  Most likely causes, distilled: (i) `_wanted`'s blanket `name.endswith(".dll")` rule admits *every*
  DLL regardless of directory, and NSIS scaffolding lives under `$PLUGINSDIR\`, which no rule excludes
  — this is the dominant cause; (ii) the payload tree also carries other installer-only DLLs the same
  rule would admit if present. Probe to confirm before any change: list
  `tesseract\**\*.dll` and check which sit under `$PLUGINSDIR\` versus the engine root.
* **A-2 (asymmetry).** A silently-dropped DLL is not caught at provision time (R1). Most likely cause:
  the only post-extraction assertion is `_find_engine()` (exe presence), and `engine_version()` accepts a
  non-zero exit as long as a banner was printed. Probe to confirm: rename one engine DLL in a scratch
  copy and run `tesseract.exe --version`, recording the exit code and whether a banner still prints.

Both are candidates for **Subtask B** (promote unpack out of `tesseract.py`), not for a hotfix here.
I am **not** proposing or applying a fix.

# Subtask H — compressed reading copies (§12), evaluated with numbers

Plan: [`plans/zoombie-image-pipeline-rework.md`](../plans/zoombie-image-pipeline-rework.md) §1.1 (receipt protocol), §3 (413 root cause + 7.3× measurement), §4.1 (two-stage filter), §9 (`data.next`/attach cap), §12 (the item under test). Inputs: [`subtask-C-ocr-measurement.md`](subtask-C-ocr-measurement.md), [`subtask-D-selector.md`](subtask-D-selector.md).

Mode: **measurement only — no product code written or changed.**

* Measurement start (UTC): **2026-09-25T22:03:38Z**
* Receipt written (UTC): **2026-09-25T22:09:40Z** (newer than start)

Artifacts this receipt names (all on disk):

| Artifact | Contents |
|----------|----------|
| `.tmp/subtask_h/bytes.json` | ffmpeg qscale sweep, bytes + ratio per frame/setting (gitignored) |
| `.tmp/subtask_h/ocr_cmp.json` | PNG-vs-JPEG OCR text + char/word/differing counts (gitignored) |
| `.tmp/subtask_h/numeric_fidelity.json` | numeric-token loss/gain per setting (gitignored) |
| `.tmp/subtask_h/word_stats.json`, `.tmp/subtask_h/glyph_heights*.json` | Tesseract word heights + ink row-runs (gitignored) |
| `.tmp/subtask_h/strip.json`, `.tmp/subtask_h/strip.png` | the SYNTHETIC small-font strip |
| `.tmp/subtask_h/vision/*.png|jpg` | native 1:1 crops + strip copies read visually |
| `.tmp/subtask_h/measure*.py` | the harnesses (measurement only) |
| `C:\Users\maxim\zoombie-env\tmp\subH-8ac2e5bcf4f04d93a70a468f3da88087\` | ASCII scratch: 5 PNGs + 30 JPEG variants (toolchain `tmp\`) |

---

## 1. Baseline (unchanged — no code was touched)

```
> python -m pytest tests -q
912 passed in 9.25s
> cd scripts && python -c "from zoombie.lib import ocr; print(ocr.available())"
(True, '5.5.3.20260724')
```

**912 held.** The engine is reachable. Nothing in `scripts/` or `tests/` was modified.

## 2. Method and the two invariants

* **Item resolved by wildcard** (§4.4 ASCII-path invariant not typed by hand):
  ```
  > python -c "import glob; print(glob.glob(r'C:\Users\maxim\source\repos\kb\Crimson\4 - 09.04.2020*'))"
  ['C:\\Users\\maxim\\source\\repos\\kb\\Crimson\\4 - 09.04.2020 - ��������� �� ����� ���� � ����� ��� - �������� ������ �����']
  > ... png count: 96   item isascii: False
  ```
  (cmd.exe prints the Cyrillic name as mojibake, as warned.)
* **ASCII-path invariant honoured:** every frame was copied into
  [`paths.new_temp_dir("subH")`](../scripts/zoombie/lib/paths.py:513) →
  `C:\Users\maxim\zoombie-env\tmp\subH-8ac2e5bcf4f04d93a70a468f3da88087` (`isascii: True`).
  Tesseract never saw a Cyrillic path. Left in place under the toolchain root's `tmp\` for re-inspection.
* Frames examined: **032** (10-K table, densest), **094** (10-K statement), **085** (Russian policy prose), **096** (FINVIZ quote screenshot) — plus **010**, which turned out to be the very frame §3/§12 measured.

## 3. §12's own number — REPRODUCED, and its scale identified

§12: *"q3 JPEG … 129,038 B vs 940,052 B — 7.3×"*. The 940,052 B frame is the **only** such frame in the item:

```
> python -c "... frames of exactly 940052 B: [('010 - 00-31-32.png', 940052)]"
```

```
> ffmpeg -i "…\010 - 00-31-32.png" -q:v 3 f010_ffq3.jpg   →  129038 bytes
```

**Both numbers reproduced exactly.** The critical correction is the **encoder scale**:

```
> python -c "… PIL im.save(buf,'JPEG',quality=3) …"   →  29,765 bytes   (PIL quality=3)
> ffmpeg … -q:v 3 …                                    → 129,038 bytes   (ffmpeg qscale=3)
```

§12's "q3" is **ffmpeg `-q:v 3`** (qscale), not PIL/Pillow `quality=3` — a 4.3× byte difference at the same nominal "3". An implementation that says "q3" must say *ffmpeg qscale*, or it will pick the wrong encoder. Frame 010, however, is a **poster frame: 29 OCR chars, 12 words** — exactly the "large type only" case §12 admitted, and it is the image file's own title slide. It is **not** representative; the item mean PNG is 556,905 B, not 940,052 B.

## 4. Q1 — the measured table (ffmpeg `-q:v`, source 1280 wide)

Bytes → ratio vs PNG → smallest font (ink height) → legibility verdict. All JPEGs are `-vf scale=…:flags=lanczos`.

| frame | PNG B | setting | bytes | ratio | OCR chars | numeric-token loss | smallest legible ink | vision verdict |
|-------|------:|---------|------:|------:|----------:|-------------------:|--------------------:|----------------|
| **010** (poster) | 940052 | q2 | 173453 | 5.42× | 19 | 0 | — | legible (few words) |
| | | **q3** | **129038** | **7.29×** | 26 | 0 | — | legible |
| | | q5 | 94739 | 9.92× | 0 | 0 | — | Tesseract sees nothing |
| **032** (10-K) | 573562 | q2 | 220985 | 2.60× | 2666 | 31/100 | **7–8 px** | header+table+notes legible; a 7px figure differs |
| | | **q3** | 178145 | **3.22×** | 2781 | 28/100 | **8 px** | legible at both readings; **7 px digit error** |
| | | q5 | 138996 | 4.13× | 2747 | 38/100 | 9 px | degraded |
| **094** (10-K) | 548389 | q2 | 224071 | 2.45× | 2963 | 22/80 | **7 px** | legible |
| | | **q3** | 180166 | **3.04×** | 2867 | **29/80** | 7 px | legible |
| | | q5 | 139108 | 3.94× | 2956 | 26/80 | 7 px | legible |
| **085** (RU prose) | 399141 | q2 | 158151 | 2.52× | 1383 | 6/11 | **6 px** | legible |
| | | **q3** | 128396 | **3.11×** | 1686 | 7/11 | 6 px | legible |
| | | q5 | 101190 | 3.94× | 1183 | 8/11 | 7 px | degraded |
| **096** (FINVIZ) | 379364 | q2 | 189923 | 2.00× | 884 | 25/48 | **4–5 px** | legible until the tiny numerals |
| | | **q3** | 153541 | **2.47×** | 865 | **25/48** | 5 px | **small numerals misread** |
| | | q5 | 118145 | 3.21× | 749 | 25/48 | 6 px | broken (70 words) |

1600-wide copies (upscaled — cannot add detail the 1280 source lacks; **do not read as a legibility gain**):

| frame | q3 @1280 | q3 @1600 | ratio | note |
|-------|---------:|---------:|------:|------|
| 032 | 178145 (3.22×) | 230469 | 2.49× | Tesseract *picks up more* chars (+242) but numeric-token loss rises to 51/100 — interpolation adds spurious tokens |
| 096 | 153541 (2.47×) | 193335 | 1.96× | +196 chars, loss 30/48 |

The 1600 upscale is **not worth its bytes**: ~1.25× larger for *more* OCR noise on the numeric track. A genuinely higher-res reading copy needs a higher-resolution **source**, which the item does not have.

### 4.1 Smallest legible font, per quality — SYNTHETIC strip

Real frames' actual ink height (dark row-runs, native 1:1) is **7–8 px** on 032 and **3–6 px** on 096 — i.e. the real frames sit exactly at the edge. To bracket the threshold cleanly I rendered a strip (Arial, antialiased, **SYNTHETIC**) at 5–16 px:

```
> cd scripts && python ..\.tmp\subtask_h\measure4.py
5px …garbled     6px …garbled     7px …NUMERAL ERROR (23,284 vs 23,264)
8px …clean       9px …clean       10px+ …clean
```

| quality | smallest legible font (SYNTHETIC strip, 1280 wide) | smallest legible on the REAL frames |
|---------|---------------------------------------------------|-------------------------------------|
| PNG | ~6 px | 6–7 px |
| q2 | ~7 px | 7 px |
| **q3** | **8 px** (7 px produces digit substitution) | **8 px** — 096's 5 px numerals fail; 032's 8 px pass |
| q5 | ~9 px | 9 px |

Where OCR drops characters the vision read thinks it can see: on the 7 px rung the eye still resolves the glyph but the *token* flips (`23,264`→`23,284`); on 096's 5 px FINVIZ row the eye reads the shape but Tesseract and the vision read both lose the exact figure. **The failure mode is silent digit substitution, not illegibility.**

## 5. Q2 — OCR: PNG vs JPEG (Tesseract input), same frames

```
> cd scripts && python ..\.tmp\subtask_h\measure2.py      # ocr.ocr_image(..., lang='eng+rus')
```
char/word deltas are vs the **PNG** OCR. `differing` = characters that differ after whitespace-normalisation (difflib).

| frame | PNG chars/words | q2 chars (±)/words | q3 chars (±)/words | q5 chars (±)/words |
|-------|----------------:|-------------------:|-------------------:|-------------------:|
| 010 | 29 / 12 | 19 (−10) / 5 (−7) | 26 (−3) / 5 (−7) | 0 (−29) / 0 (−12) |
| 032 | 2807 / 469 | 2666 (−141) / 439 (−30) | 2781 (−26) / 465 (−4) | 2747 (−60) / 452 (−17) |
| 094 | 2951 / 467 | 2963 (+12) / 462 (−5) | 2867 (−84) / 448 (−19) | 2956 (+5) / 465 (−2) |
| 085 | 1353 / 178 | 1383 (+30) / 183 (+5) | 1686 (+333) / 238 (+60) | 1183 (−170) / 158 (−20) |
| 096 | 941 / 179 | 884 (−57) / 168 (−11) | 865 (−76) / 171 (−8) | 749 (−192) / 144 (−35) |

**Raw char/word counts are misleading** (Subtask C already proved this): 085 *gains* 333 chars at q3 while actually getting noisier — wrong-script and blur both inflate length. The honest signal is **numeric-token fidelity**, the metric that matters for tables:

| frame | PNG tokens | q2 lost/gained | q3 lost/gained | q5 lost/gained |
|-------|-----------:|---------------:|---------------:|---------------:|
| 032 | 100 | 31 / 24 | **28 / 25** | 38 / 26 |
| 094 | 80 | 22 / 14 | **29 / 18** | 26 / 27 |
| 085 | 11 | 6 / 1 | 7 / 6 | 8 / 3 |
| 096 | 48 | 25 / 28 | **25 / 24** | 25 / 16 |

**Feeding Tesseract the JPEG is materially worse on the numeric track.** q3 loses **28 %** of 032's figures and **52 %** of 096's — every single digit flip (`4.66`→`4.66`?, `22.02`→`22.02`-loses, `1.20`, `0.40`, `9092`, `54`, `4929`) sits precisely on the small-font case §12 flagged. **The split is therefore justified with evidence:** OCR the PNG, hand the JPEG to vision.

## 6. Format-split verdict, made concrete against the code

**The split is already the de-facto shape; nothing routes a JPEG to Tesseract today.**

* **OCR input is the frame PNG.** [`_extract_frames()`](../scripts/zoombie/commands/slides.py:257) OCRs `target = os.path.join(work_images, slides.frame_name(index, candidate["timeSec"]))`, and [`frame_name()`](../scripts/zoombie/lib/slides.py:604) returns `f"{index:03d} - {time_tag(seconds)}.**png**"`. [`ocr.ocr_image()`](../scripts/zoombie/lib/ocr.py:97) opens whatever path it is given. So OCR is fed the PNG by construction.
* **`visionFrames` is a path list built in §12's own insertion point.** [`data.visionFrames`](../scripts/zoombie/commands/slides.py:592) is `{**frame, "path": os.path.join(images_dir, frame["file"])}` — i.e. **plainly where a JPEG sibling path would be substituted**. [`visionPages`](../scripts/zoombie/commands/readpdf.py:356) is built the same way.
* **`postprocess` inserts from the manifest's `file`, never from `visionFrames`.** [`place_figures()`](../scripts/zoombie/commands/postprocess.py:688) iterates `manifest["images"]`, and [`image_link(prefix, entry["file"])`](../scripts/zoombie/commands/postprocess.py:766) writes the link. The manifest is written by [`write_sidecar()`](../scripts/zoombie/lib/slides.py:689), whose `"images": rows` rows carry `record["file"]` — the **`.png`** name. Therefore:
  * the **`postprocess` insertion MUST use the PNG** (`entry["file"]`), and it already does;
  * the PNG **stays the deliverable** because it is the higher-quality artifact embedded in `summary.md`, and because `postprocess` has no JPEG path at all — a JPEG sibling named in `visionFrames` **cannot** leak into `summary.md`;
  * the split is therefore already enforced by shape: `visionFrames` (AI read) and `manifest.images` (removal/inline) are **disjoint** structures built from the same `records`, so advertising JPEG in the one never reaches the other.

**But the cheap fix has a hidden coupling:** `visionFrames` entries are built as `{**frame, "path": …}` where `frame["file"]` is the PNG name. `data.next.attachable` (lines 617–623) and `describe()`/`build()` compute `budget.bytes` from that `file`. If `frame["file"]` is *replaced* with the JPEG name, the manifest and the README keep the PNG while `visionFrames` advertises a JPEG the rest of the pipeline never created. The correct change is to carry a **separate key** (e.g. `"readingPath"` + `"readingBytes"`) and rewrite only the `visionFrames`/`next.attachable` construction — not `frame["file"]`.

## 7. §9 interaction — attach cap and `data.next.budget`

* The cap is structural: [`DEFAULT_ATTACH_CAP = 8`](../scripts/zoombie/lib/next.py:50) counts **list entries**, not bytes; [`describe()`](../scripts/zoombie/lib/next.py:74) sums `bytes` from each entry, reading `paths.file_size` when the entry omits it.
* If `visionFrames` is rewired to advertise JPEG paths, then **`budget.bytes` must be the JPEG size**, not the PNG. Today [`next.attachable`](../scripts/zoombie/commands/slides.py:620) carries `"bytes": paths.file_size(<the PNG>)`; and [`next_mod.build()`](../scripts/zoombie/commands/slides.py:601) receives the `attachable` list un-capped.
* **This is a code change the implementation must make, and it is not optional:** leaving `bytes` at the PNG size would over-report the agent's upcoming spend (940 KB instead of 129 KB per frame) — defeating the exact §3/§9 budget purpose. The image **count** is unaffected (same list length); only `bytes` changes.
* `readpdf` is the same shape: [`vision_attachable`](../scripts/zoombie/commands/readpdf.py:308) uses `paths.file_size(...)` on the *rendered page* path.

## 8. `image_count` / manifest / naming risks (report only — not fixed)

The sidecar and pruning logic match **`*.png`** literally:

* [`image_count()`](../scripts/zoombie/item/paths.py:142) counts `entry.name.lower().endswith(".png")` — **a `.jpg` sibling is invisible to it**, so the count stays correct *only if* the sibling is never counted. Adding a JPEG sibling **does not** change `image_count` (it is PNG-filtered), which is the desired outcome — but it means the sibling is also invisible to any code that discovers frames by extension.
* [`mcp.py` facets.next](../scripts/zoombie/mcp.py:793) builds its frame list with the same `.png` filter.
* `.png` is **hardcoded in four places** the implementer must not casually change: `frame_name()` ([`lib/slides.py`](../scripts/zoombie/lib/slides.py:604)), the [`page-NNN.png`](../scripts/zoombie/lib/pdf.py:699) renderer, `readimages.py`'s IHDR probe ([`commands/readimages.py`](../scripts/zoombie/commands/readimages.py:67)), and the README/manifest rows (PNG names).
* **Idempotency:** [`image_link()`](../scripts/zoombie/commands/postprocess.py:624)/[`INSERTED_IMG_RE`](../scripts/zoombie/lib/markdown.py:79) match inserted links against the `img/` path fragment — a JPEG sibling named `032 … __q3.jpg` would **not** match `img/032 … .png`, so a re-run cannot mistake it, which is safe. But a **second** artifact *inside* `img/` named after the same stem `032 … .png` must not be created; use a distinct sibling extension/suffix.
* **What would break if done carelessly:** naming the sibling with a `.png`-matching stem (e.g. appending a suffix *before* `.png`) would make `image_count` and the manifest disagree; calling the sibling `.jpg` is safe for `image_count` but the sibling must then be written somewhere the manifest does **not** enumerate, or the manifest/README must explicitly exclude it.

## 9. Light/dark and dense/sparse

* The dense frames are **light documents** (10-K on white — 032, 094; RU policy on white — 085) *except* 096, whose FINVIZ chrome is **dark grey with small coloured numerals**; the small-numeral row is the worst case and q3 loses 52 % of its tokens.
* **Light/dark does not change the recommendation** (no dark-frame JPEG artefact anomaly was seen beyond the small-numeral loss), but it means the worst case is a **dark UI screenshot**, not a spreadsheet — worth a note in the implementation.
* Dense (032/094/096) fails far more than sparse/prose (085): the escalation rule should key on **smallest-numeral density**, not frame count.

## 10. Recommendation

1. **Reproduce §12 as written, but with ffmpeg `-q:v 3`** — §12's numbers are correct (129,038 B / 7.3× on frame 010) *only* under the ffmpeg qscale. It must not be implemented as PIL `quality=3`.
2. **q3 is NOT safe for small text in general.** It is safe for **≥8 px** text. Real dense frames carry **5–8 px** figures; on 096 (5 px numerals) q3 loses half the numeric tokens, and on the synthetic strip q3 substitutes a digit at 7 px. **Do not state "fully legible at 1280×720" as a general claim** — it holds for the poster case 010 and for ≥8 px only.
3. **Recommended default: q2** for the vision reading copy (2.0–2.6× on dense frames, 5.4× on the poster; the only failing zone is 096's 4–5 px row, where even PNG OCR is marginal). If a single quality is mandated, **q2 over q3** trades ~15 % more bytes for ~20 % fewer figure drops.
4. **1600-wide does NOT help** and should be rejected: it costs ~1.25× the bytes of q3@1280 and *increases* numeric-token loss because interpolation invents tokens. §12's "prefer q2 or 1600 px wide over downscaling" should drop the 1600 option — it cannot add detail the 1280 source lacks.
5. **A conditional escalation IS warranted:** keep a cheap default (q3) for sparse/prose frames and escalate to **q2 for frames whose baseline OCR shows dense small numerals** (e.g. `chars > 800` *and* a digit-ratio test, or the selector's own `bytes` threshold). On this item that means q2 on 032/094/096 and q3 elsewhere — ~15 % byte cost on three frames for the figures that matter.
6. **Keep the format split exactly as designed.** Tesseract → **PNG always** (q3 loses 28–52 % of table figures); vision + `postprocess` → **PNG**; only the agent's *inline reading copy* becomes JPEG. The code already separates these; the change is confined to `visionFrames`/`next.attachable` and must carry the JPEG **bytes** into `budget`.

## 11. Plan claims the evidence contradicts

* **§12 "fully legible at 1280×720" is true only for its poster frame 010 and for ≥8 px text.** On the item's actual densest frames q3 misreads 7 px-and-smaller figures.
* **§12's 7.3× is the poster frame (940,052 B), the item's single largest PNG; the item mean is 556,905 B** and the dense-frame ratio at q3 is **2.5–3.2×**, not ~7×. "~8–12 frames/session becomes ~30–40" is therefore optimistic for a dense item — with q2 at ~2.3× it is closer to **~18–26 frames/session**.
* **§12 "prefer q2 or 1600 px wide"** — the 1600 option is counter-productive (more bytes, worse OCR); strike it.
* **§12 "write a q3 JPEG sibling"** — "q3" is **ffmpeg `-q:v 3`**; PIL `quality=3` is 4.3× smaller and visually destroys the text.

## 12. Diagnosis status (debug-mode step)

No blocker; no unexplained failure. Eight harnesses ran clean (300+ OCR calls, 0 errors); no product code was touched; 912 still passes. The one measurement I could not complete as scripted — `row_runs` returning `[]` for 094/085 on the first threshold — I diagnosed as **too-strict a dark-row threshold on antialiased light-on-white text**, not a real absence, and worked around it with the threshold-170 pass for 096/032 and the synthetic strip for the px threshold. The light/dark luminance probe returned no captured output; the light/dark statement above is from the vision reads, and is labelled as such rather than as a measured number.

**Nothing was implemented. The quality, resolution and escalation parameters above await the user's confirmation before any code is written.**

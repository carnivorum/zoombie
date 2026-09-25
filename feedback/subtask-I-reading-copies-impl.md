# Subtask I — compressed reading copies (§12), implemented

Plan: [`plans/zoombie-image-pipeline-rework.md`](../plans/zoombie-image-pipeline-rework.md)
§1.1 (receipt protocol), §3 (413 root cause), §4.1 (two-stage filter), §9
(`data.next`/attach cap), §12 (the item). Parameters: **Subtask H's measurement**
([`feedback/subtask-H-reading-copies.md`](subtask-H-reading-copies.md)) — read, not
re-derived. Inputs: [`subtask-D-selector.md`](subtask-D-selector.md),
[`subtask-E-next.md`](subtask-E-next.md), [`subtask-F-scratch.md`](subtask-F-scratch.md).

* Subtask start (UTC): **2026-09-25T22:12:58Z**
* Receipt written (UTC): **2026-09-25T22:26Z** (newer than start)

Result: **GREEN.** `python -m pytest tests` → **950 passed** (floor was **912**, +38).
§12 is the plan's last item; nothing beyond it was started.

---

## 0. Baseline recorded before any change (§1.1 rule 3)

```
> python -m pytest tests -q        # BEFORE → 912 passed in 9.38s   (the floor)
> python -m pytest tests -q        # AFTER  → 950 passed in 9.75s
> python -m pytest tests/test_reading.py -q   # 37 passed
```

**912 + 38 = 950**, and every pre-existing test still passes. The floor is intact.

---

## 1. Artifacts (every path below exists on disk)

| Artifact | State | Role |
|----------|-------|------|
| [`scripts/zoombie/lib/reading.py`](../scripts/zoombie/lib/reading.py) | **new** | the constants, `count_numeric_tokens()`, `quality_for()`, `plan()`, `make_copies()`, `prune()`, `index_copies()` |
| [`scripts/zoombie/commands/slides.py`](../scripts/zoombie/commands/slides.py) | modified | encodes a copy per frame; `visionFrames` + `next.attach` advertise it; `data.readingCopy` |
| [`scripts/zoombie/commands/readpdf.py`](../scripts/zoombie/commands/readpdf.py) | modified | a copy per rendered scan; `visionPages` + `next.attach` advertise it |
| [`scripts/zoombie/cli.py`](../scripts/zoombie/cli.py) | modified | `-NoReadingCopy` on `slides` + `readpdf` |
| [`scripts/zoombie/mcp.py`](../scripts/zoombie/mcp.py) | modified | `facet_next` advertises copies when they exist (frame list still PNG-only) |
| [`tests/test_reading.py`](../tests/test_reading.py) | **new** | 37 tests |
| [`tests/test_mcp.py`](../tests/test_mcp.py) | extended | +1 test (copies invisible to the PNG count, advertised in attach) |
| [`tests/test_slides.py`](../tests/test_slides.py) | modified | 3 `_extract_frames` call sites unpacks the new 5-tuple |
| [`skills/_shared/json-contract.md`](../skills/_shared/json-contract.md) | modified | an `attach` path is a JPEG copy, not a PNG; `budget.bytes` is the copy size |
| [`skills/zoombie-images-to-md/SKILL.md`](../skills/zoombie-images-to-md/SKILL.md) | modified | `visionPages[i].path` is a copy; `readingCopy` documented |
| `plans/zoombie-image-pipeline-rework.md` §12 | modified | §12 rewritten to the implemented policy; status/floor/snapshot updated |
| `.tmp/subtask_i/{measure.py,verify.py,live.json}` | scratch (gitignored) | the live harnesses |
| `C:\Users\maxim\zoombie-env\tmp\subI-62da2def425d4719903232d83a4f1896\` | ASCII scratch | the 5 real frames + their copies |

The change set is **6 source modules** (1 new, 5 edits) + **3 test files** + **2 skill
files** + the plan. Nothing was left under `scripts/`.

---

## 2. The chosen heuristic, with H's numbers cited and the constant

H's measured numeric-token counts on the real Crimson frames:

| frame | H tokens | H verdict | this rule |
|-------|---------:|-----------|-----------|
| 032 | **100** | dense, must escalate | **q2** |
| 094 | **80** | dense, must escalate | **q2** |
| 096 | **48** | dense, must escalate | **q2** |
| 085 | **11** | prose | **q3** |
| 010 | **0** | poster | **q3** |

**The constant: `DENSE_NUMERIC_TOKENS = 20`** — inside H's wide 11..48 gap, not on a
cliff edge. The other two constants are `DEFAULT_QUALITY = 3` and
`DENSE_QUALITY = 2` (ffmpeg `-q:v`).

```
[reading.py] DEFAULT_QUALITY = 3   DENSE_QUALITY = 2   DENSE_NUMERIC_TOKENS = 20
[reading.py] count_numeric_tokens()  -> re.compile(r"\d[\d,\.]*\d|\d")
[reading.py] quality_for(text) -> (quality, reason)   # pure; testable without ffmpeg
```

**Why numeric tokens and not chars.** H proved raw char/word counts are misleading
(frame 085 *gains* 333 chars at q3 while getting noisier), and H's own fidelity
metric — the one its table is written in — is the numeric-token count. The regex is
copied verbatim from H's harness (`.tmp/subtask_h/measure6.py:15`) so the threshold is
expressed in the same units as H's numbers. A char threshold (H's alternative
"`chars > 800`") would fire on 085 (1686 chars at q3) — a false escalation — because
085 is prose, which is exactly the discriminator H said not to use.

**Default q3 when the OCR text is unavailable.** `quality_for(None)` returns
`(3, "…unavailable…")`; an empty string is a *real* zero-numeral measurement and is
not conflated with unavailability. So `-NoTextGate`, an absent engine, and an
OCR-skipped frame all default to q3 **and say so**.

---

## 3. Live before/after on the real dense frames

`cd scripts && python ..\.tmp\subtask_i\measure.py` (the real Crimson item, ASCII
scratch, `ocr.available() -> (True, '5.5.3.20260724')`):

```
work=C:\Users\maxim\zoombie-env\tmp\subI-62da2def425d4719903232d83a4f1896 ascii=True
ocr available=True detail=5.5.3.20260724

frame      PNG B  tokens quality
010       940052       0       3  0 numeric tokens (< 20), so the reading copy stays at the default -q:v 3
032       573562     100       2  100 numeric tokens (>= 20) means dense small numerals, so the reading copy escalates to -q:v 2
085       399141      11       3  11 numeric tokens (< 20), so the reading copy stays at the default -q:v 3
094       548389      80       2  80 numeric tokens (>= 20) means dense small numerals, so the reading copy escalates to -q:v 2
096       379364      48       2  48 numeric tokens (>= 20) means dense small numerals, so the reading copy escalates to -q:v 2

plan summary: {'qualities': {'3': 2, '2': 3}, 'escalated': 3, 'routed': 5}

make_copies available=True count=5 bytes=892413 failed=[]

frame      PNG B    JPEG B  ratio  q  esc
010       940052    129038   7.29  3 False -> 010 - 00-31-32.q3.jpg
032       573562    220985   2.60  2 True  -> 032 - 01-01-34.q2.jpg
085       399141    128396   3.11  3 False -> 085 - 01-20-10.q3.jpg
094       548389    224071   2.45  2 True  -> 094 - 01-38-25.q2.jpg
096       379364    189923   2.00  2 True  -> 096 - 01-43-25.q2.jpg
```

**These bytes reproduce H's table exactly** — 032/094/096 escalate and 085/010 do not;
010 q3 = 129,038 B (H's §12 number, and the only frame H's 7.3× came from), 032 q2 =
220,985, 085 q3 = 128,396, 094 q2 = 224,071, 096 q2 = 189,923. All five match H's §4.1
table to the byte, which confirms the implementation is ffmpeg `-q:v N` at the source
resolution and not PIL `quality=N`.

---

## 4. Proof `budget.bytes` changed and the manifest / `image_count` did not

### 4.1 Live `slides` run — the copies are what is advertised

`cd scripts && python -m zoombie slides -Source ..\.tmp\subD\deck4.mp4 -Output ..\.tmp\subI\item -MinFrameBytes 0`

```
"visionFrames":[{"file":"001 - 00-00-36.png","timeSec":36.0,"representative":true,
  "readingPath":"…\\.data\\img\\readings\\001 - 00-00-36.q3.jpg","readingBytes":11021,
  "path":"…\\.data\\img\\readings\\001 - 00-00-36.q3.jpg"}, … 4 frames …],
"next":{"attach":[{"file":"001 - 00-00-36.png",
                   "path":"…\\readings\\001 - 00-00-36.q3.jpg","bytes":11021}, …],
        "budget":{"images":4,"bytes":44085}, …},
"readingCopy":{"dir":"…\\.data\\img\\readings","available":true,"count":4,"bytes":44085,
               "pruned":0,"qualities":{"3":4},"escalated":0,
               "reason":"0 frame(s) escalated to -q:v 2 for dense small numerals; the rest use -q:v 3"}
```

`budget.bytes = 44085` = the sum of the four JPEGs, and **`file` is still the PNG**
(`001 - 00-00-36.png`) while `readingPath`/`path` are the JPEG — the separate-key
constraint H identified. Before this change `budget.bytes` was the PNG sum.

### 4.2 PNG-only enumeration, proven live

`cd scripts && python ..\.tmp\subtask_i\verify.py`

```
=== manifest ===
rows: 4
all .png: True
any .jpg mentioned: False
=== README ===
contains '.jpg': False
=== image_count ===
image_count: 4 | png files: 4
=== dirs ===
loose .jpg in img/: []
readings/: ['001 - 00-00-36.q3.jpg', '002 - 00-01-16.q3.jpg',
            '003 - 00-01-56.q3.jpg', '004 - 00-02-36.q3.jpg']
```

The manifest, the README and `image_count()` see **only PNGs**; no `.jpg` is loose in
`img/`; all copies are inside `img/readings/`. `postprocess` reads
`manifest.images[].file`, which is the PNG by construction, so a copy cannot reach
`summary.md` — asserted in
`TestSlidesReadingCopies::test_postprocess_inlines_the_png_not_the_copy`.

### 4.3 Live `readpdf -Vision` — same shape on the second surface

`cd scripts && python -m zoombie readpdf -Source ..\.tmp\subE\scanned.pdf -Output ..\.tmp\subI\scanout -Vision ..\.tmp\subI\vision -AttachLimit 3`

```
"visionPages":[{"page":1,"file":"page-001.png",
  "path":"…\\vision\\readings\\page-001.q3.jpg","bytes":49188}, …3…],
"visionPageCount":12,
"readingCopy":{"dir":"…\\vision\\readings","available":true,"count":12,"bytes":590256,
               "qualities":{"3":12}},
"next":{…,"budget":{"images":3,"bytes":147564}, …}
```

12 rendered scans → 12 q3 copies (a rendered page has no OCR text to escalate from),
`budget.bytes = 147564 = 3 × 49188` **JPEG** bytes, `file` still `page-NNN.png`.

### 4.4 Idempotency and stale pruning, live

```
> python -c "…list img/readings…"     # BEFORE
['001 - 00-00-36.q3.jpg', '002 - 00-01-16.q3.jpg', '003 - 00-01-56.q3.jpg', '004 - 00-02-36.q3.jpg']
> cd scripts && python -m zoombie slides -Source …\deck4.mp4 -Output …\item -MinFrameBytes 0 -Force
> python -c "…list img/readings…"     # AFTER an identical re-run → converging, count 4
['001 - 00-00-36.q3.jpg', '002 - 00-01-16.q3.jpg', '003 - 00-01-56.q3.jpg', '004 - 00-02-36.q3.jpg']

> cd scripts && python -m zoombie slides -Source …\deck4.mp4 -Output …\item -Times "00:00:36" -Force
> python -c "…list img/readings…"     # AFTER a NARROWER re-run → stale siblings pruned
['001 - 00-00-36.q3.jpg']
```

No accumulation. `prune()` only ever removes names matching `\.q\d+\.jpe?g$`, so a
hand-placed file in a reading dir is never touched (unit-tested).

### 4.5 `-DryRun` writes nothing, live

```
> cd scripts && python -m zoombie slides -Source …\deck4.mp4 -Output ..\.tmp\subI\dryitem -DryRun
  → {"ok":true,…,"next":{"attach":[],"budget":{"images":0,"bytes":0},…},…}
> python -c "print('dry item exists:', os.path.exists(r'..\.tmp\subI\dryitem'))"
dry item exists: False
```

### 4.6 The envelope is unchanged

```
envelope keys: ['ok', 'action', 'error', 'data', 'timestamp']
```

(§14 held; `readingCopy` is additive inside `data`.) `-NoReadingCopy` was exercised
live: `readingCopy.available: False` and `visionFrames[0].path` ends in
`img\001 - 00-00-36.png` — the PNG is advertised, no copies written.

---

## 5. Tests added (38)

**[`tests/test_reading.py`](../tests/test_reading.py) — 37:**

| Class | n | Pins |
|-------|---|------|
| `TestNumericTokenCount` | 3 | H's token definition, verbatim |
| `TestEscalationRule` | 7 | 032/094/096 escalate to q2, 085/010 stay q3; the threshold is **strictly inside** H's 11..48 gap; the boundary is exactly the constant; unavailable OCR → q3 and says so |
| `TestPlan` | 3 | per-frame routing; missing OCR text → q3; the summary counts |
| `TestNaming` | 3 | `.q2.jpg` naming never looks like a PNG; `is_reading_copy`; the dir is a subdirectory |
| `TestMakeCopies` | 5 | one copy per frame at the planned quality; **no `-vf scale`** in the argv (no upscale); unresolvable ffmpeg is reported, not raised |
| `TestPrune` | 2 | only our stale copies removed; foreign files spared |
| `TestIndexCopies` | 2 | stem → copy mapping |
| `TestSlidesReadingCopies` | 10 | dense→q2 / prose→q3; **`file` never overwritten**; **`budget.bytes` is the JPEG size, not the PNG**; copies live in their own subdir with no loose `.jpg`; **manifest/README/`image_count` PNG-only**; **re-run idempotent**; `-DryRun` writes nothing; `-NoReadingCopy` advertises PNGs; **postprocess inlines the PNG** |
| `TestReadpdfReadingCopies` | 2 | every rendered page gets q3 and `budget` is JPEG; `-NoReadingCopy` advertises the PNG scans |

**[`tests/test_mcp.py`](../tests/test_mcp.py) — +1:**
`test_reading_copies_are_advertised_but_do_not_change_the_frame_count` — a copy in
`readings/` is advertised with its own bytes, while `facet_next.images.count` stays the
PNG count.

The 3 `tests/test_slides.py` `_extract_frames` call sites were updated for the 5-tuple
return (they assert the same D-2 behaviour; no test was weakened).

---

## 6. Plan claims my work contradicts / clarifies

1. **§12's "fully legible at 1280×720"** is true only for the poster frame 010 and for
   ≥ 8 px text. On the item's real dense frames q3 misreads 7 px-and-smaller figures —
   which is exactly why the escalation exists. The plan now states this (H's §11).
2. **§12's 7.3× is the poster frame** (940,052 B, the item's single largest PNG; the
   item mean is 556,905 B). The dense-frame ratio at q3 is 2.5–3.2×, so
   "~8–12 frames/session becomes ~30–40" was optimistic; the plan now says so.
3. **§12's "prefer q2 or 1600 px wide over downscaling" — the 1600 option is struck**,
   as H measured (more bytes, worse numeric fidelity). There is no `-vf` in the encoder.
4. **§12's "write a q3 JPEG sibling" — "q3" is ffmpeg `-q:v 3`**, and the plan now says
   so explicitly, because PIL `quality=3` is 4.3× smaller and destroys the text.
5. **§12's "write a sibling" is implemented as a dedicated `readings/` subdirectory**,
   not a loose sibling: H's §8 shows a loose `.jpg` in `img/` is invisible to
   `image_count`/the manifest but would need every reader to remember the filter. A
   subdirectory makes the exclusion structural and keeps all our copy logic in one
   place; verified live (§4.2).
6. **§9's `budget.bytes` was the PNG size** before this change; §9 exists so the agent
   knows what it will spend, so leaving it at the PNG size would over-report ~3× and
   defeat the purpose. It now reports the JPEG size (H's §7 called this "not optional").
7. **A defect in my own first draft, fixed before testing:** `readpdf`'s
   `readingCopy.reason` initially reported "the rendered page scans were advertised as
   PNGs" *while the copies were being written* (an inverted branch). Corrected to state
   the q3-per-page rule.

### Advisories / honest scope

* **The `slides` live runs used a synthetic solid-colour deck** (`.tmp/subD/deck4.mp4`,
  the only pre-existing video on this machine — Subtask D's advisory), for which the
  11 KB JPEG is *larger* than its 4 KB PNG. That is a true property of flat synthetic
  colour and not a defect of the parameter choice, which H fixed on real frames; the
  byte-reduction claim is evidenced by §3's real-frame table, not by this deck.
* **The OCR call count changed on the default path** from D-2's one-per-run to
  one-per-frame, because the escalation score needs every frame's numeric-token count.
  On the real item this is 96 calls for 96 frames (`eng+rus`, ~1.55× `rus`). This is a
  deliberate trade the brief's policy implies ("derive the tokens from the frame's OCR
  text"), and it is confined to the reading-copy default; `-NoReadingCopy` restores the
  one-per-run cost. Recorded rather than hidden because it moves the §4 cost curve.
* **`readimages` does not make copies.** Its inputs are a user's loose images (its
  attach entries point at the user's own files), so rewriting them into a `readings/`
  subdirectory of the sidecar dir would surprise; it is out of §12's frame/adoptedPage
  scope and was left alone.
* **`mcp.facets.next` reads copies but never writes them** — it advertises existing
  copies so the facet over-reports nothing; generating on demand from a read-only tool
  would violate "the facet is read-only".

---

## 7. Commands run (raw, condensed)

```
> python -m pytest tests -q                          # BEFORE → 912 passed in 9.38s
> python -m pytest tests -q                          # AFTER  → 950 passed in 9.75s
> python -m pytest tests/test_reading.py -q          # 37 passed in 0.57s
> cd scripts && python ..\.tmp\subtask_i\measure.py  # the real-frame routing table (§3)
> cd scripts && python ..\.tmp\subtask_i\verify.py   # manifest/README/count PNG-only (§4.2)
> cd scripts && python -m zoombie slides -Source ..\.tmp\subD\deck4.mp4 -Output ..\.tmp\subI\item -MinFrameBytes 0
     visionFrames → readings/*.q3.jpg  budget {images:4,bytes:44085}
> cd scripts && python -m zoombie slides … -MinFrameBytes 0 -Force     # re-run: converging, 4 copies
> cd scripts && python -m zoombie slides … -Times "00:00:36" -Force    # narrower: 1 copy, 3 pruned
> cd scripts && python -m zoombie slides … -DryRun                     # writes nothing, item absent
> cd scripts && python -m zoombie slides … -NoReadingCopy -Force       # advertises PNGs
> cd scripts && python -m zoombie readpdf -Source ..\.tmp\subE\scanned.pdf -Output ..\.tmp\subI\scanout \
      -Vision ..\.tmp\subI\vision -AttachLimit 3
     visionPages → readings/*.q3.jpg  budget {images:3,bytes:147564}  readingCopy count 12
> git status --porcelain                              # the change set in §1
```

Nothing was left under `scripts/`; the live scratch is under the gitignored
`.tmp/subtask_i/` and the toolchain root's `tmp\subI-…`.

---

## 8. Checklist for the parent (§1.1 rule 3)

- [x] Receipt exists and is newer than the start (`…22:26Z` > `…22:12:58Z`).
- [x] Every artifact in §1 exists on disk.
- [x] The quoted output is plausible and was produced by the commands shown.
- [x] **No receipt ⇒ not run** — this file is the deliverable.
- [x] `python -m pytest tests` = **950 passed** (≥ 912).
- [x] The escalation is deterministic: dense → q2, prose/poster → q3 (§2, §3).
- [x] `data.next.budget.bytes` reflects the reading-copy bytes, not the PNGs (§4.1, §4.3).
- [x] The manifest, README and `image_count` enumerate only PNGs; postprocess inlines the PNG (§4.2).
- [x] `-DryRun` writes nothing (§4.5).
- [x] A re-run is idempotent with no accumulating stale siblings (§4.4).
- [x] `frame["file"]` is never overwritten; the copy rides separate keys (§4.1).
- [x] The JSON envelope is unchanged (§4.6).
- [x] The plan §12 and the shared skill wording match the implementation (§6).
- [x] **Nothing beyond §12 was started.**

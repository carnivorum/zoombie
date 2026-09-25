# Subtask C — the OCR measurement

Plan: [`plans/zoombie-image-pipeline-rework.md`](../plans/zoombie-image-pipeline-rework.md) §7 (§4.1 two-stage filter, §1.1 receipt protocol).
Mode: **measurement only** — no product code written or changed; Subtasks B and D–G not started.

* Measurement start (UTC): **2026-09-25T20:31:02Z**
* Receipt written (UTC): **2026-09-25T20:36:52Z** (newer than start)

Artifacts this receipt names (all on disk):

| Artifact | Contents |
|----------|----------|
| [`feedback/subtask-C-ocr-chars.csv`](subtask-C-ocr-chars.csv) | **full raw per-frame table**, 96 rows — `eng`/`rus`/`eng+rus` chars + seconds |
| `.tmp/subtask_c/results.json` | full raw output incl. **the OCR text of every frame × config** (gitignored) |
| `.tmp/subtask_c/chars.csv` | flat `frame,lang,chars,seconds,error` version |
| `.tmp/subtask_c/dump.txt` | the raw text of 8 sample frames × 3 configs |
| `.tmp/subtask_c_measure.py`, `_analyze.py`, `_text.py`, `_classify.py`, `_pixels.py`, `_csv.py`, `_dump.py` | the harnesses (measurement scripts; no product code touched) |

## 0. Method, and the two invariants

* **Item** resolved **by wildcard** (Cyrillic folder name never typed):
  `glob(r"C:\Users\maxim\source\repos\kb\Crimson\4 - 09.04.2020*")` →
  `…\4 - 09.04.2020 - Дивиденды во время чумы и после нее - принципы выбора акций`
  (printed as mojibake by cmd.exe, as warned). Frames in `.data\img\` → **96 PNGs**, `001 - 00-00-00.png` … `096 - 01-43-25.png`.
* **ASCII-path invariant honoured (§4.4):** every frame was **copied into an ASCII scratch dir first** —
  [`paths.new_temp_dir("subC")`](../scripts/zoombie/lib/paths.py:513) →
  `C:\Users\maxim\zoombie-env\tmp\subC-3ff472577a8c4095b9cc5e4c6c203875` (`isascii() == True`).
  Tesseract never saw a Cyrillic path. The scratch is **left in place** deliberately so the parent can
  re-inspect it; it is under the toolchain root `tmp\`, not the workspace.
* **Engine:** [`ocr.available()`](../scripts/zoombie/lib/ocr.py:71) → `(True, '5.5.3.20260724')`.
* **Calls:** 96 frames × 3 configs = **288** [`ocr.ocr_image()`](../scripts/zoombie/lib/ocr.py:97) invocations.
  Results were saved to JSON **after every frame**, so a timeout could not lose finished work. All 288 returned
  exit 0 — **0 OCR errors in any config** (no frame had a raised exception).

## 1. chars-per-frame distributions (min / median / max)

Measured length of the OCR text for each config over the 96 frames (chars after `.strip()`):

| config | n | **min** | **median** | mean | **max** | errors |
|--------|---|--------:|-----------:|-----:|--------:|-------:|
| `eng`     | 96 | **0** | **123.0** | 364.8 | **2958** | 0 |
| `rus`     | 96 | **0** | **129.0** | 388.5 | **2703** | 0 |
| `eng+rus` | 96 | **0** | **133.0** | 382.1 | **2951** | 0 |

Totals over the whole item: `eng` 35 023 chars · `rus` 37 293 · `eng+rus` 36 681.

> **The distribution is not the answer.** The three columns are indistinguishable by count (medians 123/129/133),
> because Tesseract running the *wrong* script still emits ~the same number of characters of **garbage**.
> The character counts therefore **cannot** be the selector's usefulness signal — only the text can. That is the
> central finding of this measurement, and it is what §4.1's "score usefulness from cheap text" actually requires.

## 2. Wall-clock (per-config totals)

| config | total | mean/frame |
|--------|------:|-----------:|
| `eng`     | 50.0 s | 0.521 s |
| `rus`     | 53.0 s | 0.552 s |
| `eng+rus` | **82.3 s** | 0.857 s |

**`eng+rus` costs 1.55× `rus` alone** (82.3 / 53.0). Idle-load was negligible — the run was dominated by child
`tesseract.exe` processes, so the ratio is a real CPU/startup cost, not wall-clock noise.

## 3. Which config is *correct* — from the text (raw evidence)

Script composition of the whole item, weighted by characters:

| config | Cyrillic | Latin | digits |
|--------|---------:|------:|-------:|
| `eng`     | **0.0 %** | 61.5 % | 14.5 % |
| `rus`     | 62.6 % | **0.0 %** | 12.5 % |
| `eng+rus` | 37.8 % | 25.2 % | 13.4 % |

Decisive raw evidence (full text in `.tmp/subtask_c/dump.txt`):

* **English chart, frame 005.** `eng` → correct:
  `FIGURE 8 / Returns of S&P 500 Index Stocks by Dividend Policy: Growth of $100 (1972-2019) / Dividend Growers…`.
  `rus` on the same frame → **`АСЧЕЕ 8`** and **`Вейшгп$ о? $&Р 500 1пйех ${осК$ Бу Отмепа Ройсу…`** — i.e. it renders
  "FIGURE"/"Returns" as Cyrillic homoglyphs. **156 of 461 chars are wrong-script garbling**, not translation.
* **Russian slide, frame 072.** `eng` → **garbage**: `AuBugenabl`, `OnibTk pe AT S P …`.
  `rus` → correct: `Дивиденды`, `Собрания акционеров`, `Календарь инвестора`, `Облигации`, `Аналитики`.

So on a **mixed** deck each single-language config is only half-right. Item composition by a word-hit classifier:
**12 frames English-dominant, 52 Russian-dominant, 13 zero-text, 19 mixed/ambiguous.**

## 4. Was `eng+rus` worth it vs `rus`? (the `-Lang` decision)

* Per-frame winner (strict char count): `eng` 38 · `rus` 36 · `eng+rus` 7 · ties 15.
* `eng+rus` **beats `max(eng,rus)` on only 7 frames, is beaten on 74, ties 15.**
* **Honest read:** the **character-count** case for `eng+rus` over `rus` is weak — it gains ~nothing in *volume*.
  Its real value is **script coverage**: it is the only single run that returns the correct script for both the
  12 English frames and the 52 Russian frames (frames 032/094 show `eng+rus` fusing the correct English
  10-K/10-K text with the correct Russian it also transcribes). On this deck the choice is therefore:

  * `rus` alone — right for 52 frames, **wrong-script for 12 English frames**, at the lowest cost;
  * `eng+rus` — right for both scripts, at **1.55× the time**.

  For a **default** `-Lang` on a Russian-deck toolchain, `eng+rus` is defensible *only because the deck is mixed*;
  `rus`-only would silently garble every English chart/10-K frame (12/96 here, and frame 032/094 are the two
  **most text-dense frames in the item** — 2788 and 2958 chars of exactly that English). The plan's §4.1
  "read both scripts rather than silently drop Russian" reasoning holds symmetrically for English here.
  **Recommendation: keep `eng+rus` as the default; the 1.55× is real but small in absolute terms (82 s for the
  whole 96-frame item) and it buys the two densest frames.** This is a judgement for the selector's author, not a
  change made here.

## 5. Near-zero frames — and the covered-run case

* `eng+rus` (and both single configs) return **exactly 0 chars on 13 frames**:
  `001, 002, 003, 004, 007, 009, 011, 012, 013, 014, 015, 017, 018`.
* **0 frames** fall in the ambiguous `1–11` band — a frame either has real text (≥ 29 chars) or exactly nothing.
  The old `DEFAULT_MIN_TEXT_CHARS = 12` floor would drop **the same 13 frames** and no others.
* **Are they image-only slides?** I verified the zero class visually from the ASCII scratch copy (the one place an
  image check is warranted — *confirming* the class, not scoring usefulness):
  * **Frame 002** is a **talking-head shot of Jim Chanos with a microphone — genuinely no text anywhere.** Its
    `OCR = 0` is *correct output*, not an OCR failure. Same shape for the other frames with a host on camera.
  * **Frame 014/015** are bright frames (`bgfrac 0.83`, near-white) — a person/lower-third with no readable text.
  * No frame with visible slide text returned zero.
* **Consequence for §4.1:** dropping a frame because `text == 0` would **delete exactly the frames a presenter
  is on camera** — the image-only case the **covered-run rule** ("every stable run contributes ≥1 frame to the AI
  set regardless of text and diff") exists to protect. This measurement **confirms that rule is necessary**, not
  merely tidy: 13/96 frames (13.5 %) would otherwise be lost. It also confirms the floor should become a
  **reporting** threshold, never a drop rule (the plan's §4.1 / D-3).

## 6. Usefulness judgement from the TEXT (sample spanning the range)

Scored **from the OCR text alone**, per §4.1. "Useful" = the text alone tells the selector what the frame is
about (enough to decide whether to spend an image read); "not useful" = the frame carries no readable text.

| frame | `eng+rus` chars | what the TEXT shows | verdict from text |
|-------|------:|---------------------|-------------------|
| 002 | 0 | (nothing — camera on host) | **not useful** — image-only; must be kept by the covered-run rule, not by text |
| 016 | 53 | `#SALT2015 / @ JIM CHANOS / KYNIKOS ASSOCIATES FOUNDER` | **useful** — identifies the speaker/branding |
| 072 | 218 | `Дивиденды / …принципах… / Собрания акционеров / Облигации / Аналитики` | **useful** — a dividend-policy slide, on-topic |
| 080 | 821 | (Russian policy prose) | **useful** — dense text |
| 085 | 1353 | `…распределение… совета директоров… дивидендной политики…` | **useful** — dense policy document |
| 032 | 2807 | `Issuer Purchases of Equity Securities… Selected Financial Data… 2019 2018 2017…` | **useful (high)** — a financial table; text alone is self-describing |
| 094 | 2951 | `Consolidated Statements of Operations Data: Revenue … Uber Technologies…` | **useful (high)** — 10-K statement; the densest frame in the item |
| 096 | 941 | `Real-time stock quotes! … UBER … Uber Technologies, Inc. … Dividend …` | **useful** — a FINVIZ quote screenshot |

Usefulness **from text** is unambiguous for every non-zero frame sampled: the text names the subject
(dividends, the speaker, the financial statement), which is exactly what the cheap first stage needs to rank
frames before an image is read. The zero-text frames are the only ones where text cannot decide — and for those
the covered-run rule, not a text floor, is the correct mechanism.

## 7. Summary answers to §7

1. **Distributions** (min / median / max chars/frame): `eng` 0 / 123 / 2958 · `rus` 0 / 129 / 2703 ·
   `eng+rus` 0 / 133 / 2951. No config errored on any frame.
2. **Does `rus` beat `eng`?** Not by char count (they are within noise of each other). It **is** correct where
   `eng` is garbage (49 frames Russian-dominant vs 12 English-dominant), but `eng` is correct on the 12 English
   frames where `rus` garbles. **The deck is mixed, so neither single config is sufficient.**
3. **Cost of `eng+rus` over `rus` alone:** **1.55×** wall-clock (82.3 s vs 53.0 s for the whole item).
4. **Near-nothing frames:** **13**, all **exactly 0**, and they are **image-only (camera on the host)** — the
   covered-run case. No frame with legible slide text returned near-zero.
5. **Char counts are not a usefulness signal** — wrong-script OCR returns comparable volume of garbage. The
   selector must score from **text content/script**, and treat a zero **as "keep via covered-run", never "drop"**.

## 8. Diagnosis status (debug-mode step)

No blocker. The measurement ran clean: 288/288 invocations, 0 errors, no unexplained OCR failure. Nothing was
fixed, and no product code was touched. The one decision this measurement informs (default `-Lang`) is stated in
§4 as a recommendation for the selector's author, not applied here.

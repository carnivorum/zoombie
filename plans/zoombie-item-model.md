# The item model: source-anchored, name-free items

Status: **plan** (nothing implemented). Supersedes the naming-convention discussion in
[`README.md:418`](../README.md:418) and [`skills/zoombie-summarize/SKILL.md:141`](../skills/zoombie-summarize/SKILL.md:141).

**Revision note.** An earlier draft of this plan put images *flat* in `.data/` and renamed the
image sidecar `README.md` → `images.md`. Both are withdrawn: images live in `.data/img/`, and
`README.md` keeps its name. §5 documents the two defects the nesting retires.

## Why this exists

The current model makes the **folder name** the item's identity and its metadata:

[`FOLDER_RE = re.compile(r"^(\d+)\s*-\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(.*)$")`](../scripts/zoombie/commands/library.py:45)

That single regex is both the recognition test and the field extractor, called from exactly
one place ([`scan_library_detail()`](../scripts/zoombie/commands/library.py:137)). Three
consequences followed, all observed:

1. **Any other naming is rejected.** A user convention of `a <title>`, `b <title>`, `c <title>`
   — the case that prompted this — parses as *no fields*, so every item lands in `## Skipped`
   ([`library.py:139`](../scripts/zoombie/commands/library.py:139)). So does ISO `2020-05-06`,
   an em-dash separator, or an unpadded day.
2. **An agent with no library to read invents a placeholder.** Told only
   "`<number> - <DD.MM.YYYY> - <title>`" and "never invent the number"
   ([`SKILL.md:142`](../skills/zoombie-summarize/SKILL.md:142)), an agent on another machine
   emitted a generic `NN` glyph: it had no way to learn its number or its date.
3. **The number has no defined provenance.** Nothing computes a successor; `index` sorts by
   `(number, date)` ([`library.py:164`](../scripts/zoombie/commands/library.py:164)) but never
   reports a duplicate. Two machines working one library pick the same number independently.

The fix is not a looser regex. A looser regex keeps the coupling and trades a visible skip for
a silently mis-parsed row. The fix is to **stop encoding identity in the name**.

## 1. The item contract

An **item** is a folder with *any name the user likes* — the name is presentation and is never
parsed. Its layout is:

```
<item>/                         ← any name; user's choice; never parsed
├── summary.md                  ← the 6-block document (the deliverable)
├── <source media>              ← the original video / audio / PDF, when kept
└── .data/                      ← everything mechanical and derived
    ├── img/                    ← extracted images, in reading order
    │   ├── 001 - p01.png
    │   ├── 002 - p02.png
    │   ├── manifest.json       ← image placement metadata (postprocess reads this)
    │   └── README.md           ← the same table, for a human
    ├── transcript.txt          ← transcribe output: the wording
    ├── transcript.srt          ← transcribe output: the timing
    ├── source.json             ← origin sidecar: url, kind, sourceKept, duration, model…
    └── item.json               ← number, date, title, source — the item's metadata
```

Rules:

- **`summary.md` sits at the item root, not in `.data/`.** It is the artifact a human opens.
- **The source media sits at the item root too**, beside `summary.md`, because requirement 3
  says so; `.data/` is for *derived* material only.
- **Images live in `.data/img/`, not flat in `.data/`.** The nesting is not cosmetic: keeping the
  image directory holding *only* image material preserves two invariants that the flat layout
  broke (§5). Images stay named `001 - p01.png` in reading order
  ([`readpdf.py:14`](../scripts/zoombie/commands/readpdf.py:14)).
- **The image sidecar keeps the name `README.md`.** An earlier draft renamed it to `images.md` to
  suit a flat directory; with the nesting restored that rename has no justification, so
  [`pdf.SIDECAR_README`](../scripts/zoombie/lib/pdf.py:48) and `verify`'s `missing-readme` check
  ([`verify.py:56`](../scripts/zoombie/commands/verify.py:56)) are left alone.
- **`.data/` is dot-prefixed and therefore hidden** in Explorer and from a default
  `Get-ChildItem`. Accepted deliberately: it keeps the item folder clean. Document it, because
  a user who "cannot find their images" will otherwise report it as a bug.

### Recognition: by evidence, not by name

An entry is an item **iff it contains a `.data/` directory or a `summary.md`**. Both are our own
markers; neither requires parsing anything. This is what makes any folder name work.

Used as: instead of `FOLDER_RE.match(entry.name) is not None` at
[`library.py:137`](../scripts/zoombie/commands/library.py:137), the predicate becomes a two-stat
test on the entry's contents.

### Metadata: `item.json`

```json
{
  "number": 13,
  "date": "2020-05-06",
  "title": "Заметки",
  "source": { "file": "video.mp4", "kind": "video", "url": "https://…" },
  "createdAt": "2026-09-20T18:00:00Z",
  "toolchainVersion": "4.4.0"
}
```

- **`date` is stored ISO, with no display format in the file.** The index renders `DD.MM.YYYY`
  for a human. This also fixes a real bug: the current sort compares `DD.MM.YYYY` strings, so
  within a repeated number `01.06.2020` sorts before `31.12.2019`
  ([`library.py:164`](../scripts/zoombie/commands/library.py:164)).
- **`number` is computed, not invented.** The scan returns `nextNumber = max(existing) + 1` in
  its result, so the agent copies a fact instead of guessing. Duplicates are reported as a
  finding, not sorted through.
- **Field fallback order** when any field is absent: `item.json` → a legacy folder-name parse →
  the summary's H1 via [`title_of()`](../scripts/zoombie/commands/library.py:65).

## 2. Section 6 must declare that it is a copy

The incident: a task on another machine read block 6 as a *recap* and treated it as a summary,
when it is the **formatted source** with recognition artifacts cleaned out. The current heading
invites that misreading — `## 6. Полное содержание транскрипта`
([`SKILL.md:49`](../skills/zoombie-summarize/SKILL.md:49)) literally says "the full content of
the transcript".

New convention — the heading must state what block 6 is:

```markdown
## 6. Полный текст источника (копия, очищенная от артефактов распознавания)
```

or in English:

```markdown
## 6. Source text - verbatim copy, cleaned of recognition artifacts
```

Prose that must accompany it in block 6's opening: block 6 is the source text itself, with
filler, duplicate cues and machine noise removed and punctuation restored; nothing in it is
condensed, paraphrased or summarised.

Note the passes are unaffected: block 6 is located by number, `SECTION6_RE = r"^##\s*6\."`
([`postprocess.py:53`](../scripts/zoombie/commands/postprocess.py:53)), so the title text is
free and any language works.

**Enforcement.** This is exactly the failure the file format cannot see, so `verify` should
carry a lint: a `summary.md` whose block-6 heading does not declare the copy (match on a
declaration phrase, per language) is reported as e.g. `section6-not-declared`. Precedent: the
verifier already reports structural problems the writer cannot see, such as a `img/` folder
missing a sidecar ([`verify.py:12`](../scripts/zoombie/commands/verify.py:12)).

## 3. The workspace scan handle

A new subcommand that enumerates items in a root — the *current workspace* by default — and
returns a compact structured result, so an agent learns what exists in one call instead of
listing directories and reading N documents.

```
zoombie items                       # scan $PWD, compact, one JSON line
zoombie items -Root <dir>           # explicit root
zoombie items -Recurse -Depth 2     # bounded descent
zoombie items -Detail               # fuller per-item records
```

Result shape (one JSON line, matching the CLI's existing contract):

```json
{
  "ok": true,
  "data": {
    "root": "C:\\ws",
    "count": 3,
    "nextNumber": 14,
    "items": [
      { "name": "a Заметки", "path": "…", "number": 12, "date": "2020-05-06",
        "title": "Заметки", "summary": true,
        "source": { "file": "video.mp4", "kind": "video" },
        "transcript": { "txt": true, "srt": true },
        "images": 7, "warnings": [] }
    ],
    "skipped": [ { "name": "notes", "reason": "no .data/ and no summary.md" } ]
  }
}
```

Token economy is the point: `items` is the **only** sanctioned way to enumerate, and it reports
what each item actually holds — so a caller knows whether a transcript, a source or images are
present without opening anything.

This replaces the direct `scan_library()` call that
[`library.py:27`](../scripts/zoombie/commands/library.py:27) documents and
[`SKILL.md:131`](../skills/zoombie-summarize/SKILL.md:131) forbids — an inconsistency in the
current inputs that this command resolves by existing.

The library `README.md` index ([`library.py:201`](../scripts/zoombie/cli.py:201)) becomes a
*renderer over the same scan*: its `#` / `Date` / `Title` columns come from `item.json`, and the
folder name appears only as the link target. `index` keeps its dry-run/`-Apply` discipline.

## 4. Source retention reverses today's default

Requirement 3 conflicts with deliberate current behaviour, so it is a behaviour change, not a
tweak: `pipeline` **deletes the downloaded media** after transcription
([`pipeline.py:163`](../scripts/zoombie/commands/pipeline.py:163)) and records
`sourceKept: false` ([`stt.py:519`](../scripts/zoombie/lib/stt.py:519)), which is what the
sidecar exists to survive ([`stt.py:497`](../scripts/zoombie/lib/stt.py:497)).

New rule:

- The media is **kept and placed at the item root** beside `summary.md`.
- `sourceKept: true` becomes the normal case; `false` remains valid and meaningful (a URL-only
  item, or a run where the media could not be fetched).
- The contract says the source *may* be present, not that it must be, so an item whose media was
  never obtainable is still well-formed. `item.json.source.kind` records which case it is.
- **Path budget:** the media file name is content-controlled, so the destination path must go
  through [`paths.assert_fits()`](../scripts/zoombie/lib/paths.py:262) before the copy, the same
  way [`pipeline.py:107`](../scripts/zoombie/commands/pipeline.py:107) sizes the output base.
- **Git:** [`.gitignore`](../.gitignore:1) ignores `*.wav`, `*.mp3`, `*.m4a`, `*.flac`, `*.srt`
  but **not** `*.mp4`, `*.mkv`, `*.webm` or `*.pdf`. Keeping sources in a workspace will start
  committing hundreds of megabytes unless the patterns are extended. Add the video and document
  extensions, and ignore `**/.data/`.

## 5. What breaks, file by file

The new layout moves the image directory from `<item>/img/` to `<item>/.data/img/`, and **`img/`
is currently hard-coded in eight places.** The list, with the risk of each:

| Site | Current | Change | Risk |
|------|---------|--------|------|
| [`markdown.py:65`](../scripts/zoombie/lib/markdown.py:65) `INSERTED_IMG_RE` | matches links whose path contains the literal `img/` | survives **unchanged** — `.data/img/` still contains `img/`; harden to be directory-derived | watch — see below |
| [`markdown.py:243`](../scripts/zoombie/lib/markdown.py:243) `strip_inserted_images()` | strips a previous run's figures | same source of truth as above | watch |
| [`postprocess.py:766`](../scripts/zoombie/commands/postprocess.py:766) `_image_dir_for()` | defaults to `<dir>/img` | defaults to `<item>/.data/img` | high |
| [`postprocess.py:476`](../scripts/zoombie/commands/postprocess.py:476) `load_manifest()` | reads `img/manifest.json` | reads `.data/img/manifest.json` | high |
| [`postprocess.py:489`](../scripts/zoombie/commands/postprocess.py:489) `image_url_prefix()` | link prefix to the image dir | prefix becomes `.data/img/` | high |
| [`verify.py:163`](../scripts/zoombie/commands/verify.py:163), [`:170`](../scripts/zoombie/commands/verify.py:170) | discovers dirs named `img` | the *name* test still works, but non-recursive discovery must descend into `.data/` to reach `.data/img` | medium |
| [`verify.py:55-56`](../scripts/zoombie/commands/verify.py:55) | requires `manifest.json` **and** `README.md` | **no change** — the rename is withdrawn | none |
| [`library.py:49`](../scripts/zoombie/commands/library.py:49), [`:82`](../scripts/zoombie/commands/library.py:82), [`:103`](../scripts/zoombie/commands/library.py:103) | `IMAGE_DIR_NAME = "img"`, image count, manifest candidates | add `DATA_DIR_NAME = ".data"` beside the existing `IMAGE_DIR_NAME = "img"` and join them; the two-location manifest check simplifies to one | medium |
| [`readpdf.py:131`](../scripts/zoombie/commands/readpdf.py:131) | default dir `f"{base}.images"` | default dir `<item>/.data/img` | medium |
| [`cli.py:177`](../scripts/zoombie/cli.py:177) | `-ImageDir` help says "sibling img/" | update the help text | low |
| [`selftest.py:127`](../scripts/zoombie/selftest.py:127), and the `img` fixture at [`:474`](../scripts/zoombie/selftest.py:474) | builds an `img` dir | build `.data/img` | low |
| [`test_postprocess.py:444`](../tests/test_postprocess.py:444), [`test_verify.py:57`](../tests/test_verify.py:57), [`test_markdown.py`](../tests/test_markdown.py) | `img/…` fixtures throughout | `.data/img/…` | low, mechanical |

### Byte-idempotency: preserved by nesting, but for the wrong reason

`postprocess` guarantees that a second `-Apply` on an unchanged document leaves it
**byte-identical** ([`postprocess.py:29`](../scripts/zoombie/commands/postprocess.py:29)). The
whole toolchain leans on that: it is why re-running is always safe and why the skill may call
the CLI without diffing first.

The guarantee is implemented partly by regex on the *link path*:

```python
INSERTED_IMG_RE = re.compile(
    r"^[ \t]*(?:!\[[^\]]*\]\(<?[^)\n]*img/[^)\n]*>?\))+[ \t]*(?P<keep>[^\n]*)",
```

A **flat** `.data/` would have broken this. Links would read `.data/001%20-%20p01.png`, the
substring `img/` would be absent, `strip_inserted_images()` would remove nothing, and the insert
pass would add a second copy of every figure on every run — silently, growing the document on
each pass.

**Nesting `img/` inside `.data/` retires that defect.** The link becomes
`.data/img/001%20-%20p01.png`, which still contains `img/`, so the existing pattern keeps
matching and idempotency holds. This is the first reason the flat layout was wrong.

The dependence survives, though — it is now a **coincidence of the directory's name rather than a
guarantee.** Rename that directory, or pass an `-ImageDir` pointing elsewhere
([`cli.py:177`](../scripts/zoombie/cli.py:177)), and the same failure returns. The hardening —
deriving both the strip pattern in [`markdown.py:65`](../scripts/zoombie/lib/markdown.py:65) and
the insert prefix in [`image_url_prefix()`](../scripts/zoombie/commands/postprocess.py:489) from
the one configured directory instead of a literal — is therefore still recommended, but it is
**no longer a prerequisite** and schedules as ordinary work.

### Directory ownership: unchanged

[`_prepare_images_dir()`](../scripts/zoombie/commands/readpdf.py:64) refuses a directory that has
no readable `manifest.json`, treating it as foreign and hand-curated. The nesting is what keeps
this guard correct: `.data/img/` holds only image material, exactly as `<item>/img/` did, so the
"is this our own previous output?" test needs **no change** and cannot fire on a sibling
`transcript.txt` or `item.json`. Under the flat layout it would have had to be rewritten. This is
the second reason the flat layout was wrong.

The pruning rule is likewise unchanged and still correct: prune only the files the manifest lists,
so a stray file is never deleted by an image re-run
([`readpdf.py:71`](../scripts/zoombie/commands/readpdf.py:71)).

## 6. Migration

Existing libraries use `<num> - <DD.MM.YYYY> - <title>` with `img/` and the sidecars at the item
root. A one-way migration pass (`zoombie items -Migrate`, dry run by default, `-Apply` to write):

1. create `.data/`;
2. move `img/` → `.data/img/`, intact — no renames;
3. move `<base>.txt`, `<base>.srt`, `<base>.source.json` → `.data/transcript.txt`,
   `.data/transcript.srt`, `.data/source.json`;
4. write `.data/item.json`, deriving `number`, `date` and `title` **from the folder name**;
5. rewrite the image links inside `summary.md` from `img/…` to `.data/img/…`, through
   [`percent_encode_dest()`](../scripts/zoombie/lib/textnorm.py:125) so the encoding stays
   correct. This is the same pattern the `postprocess` strip pass matches
   ([`markdown.py:65`](../scripts/zoombie/lib/markdown.py:65)), which is why the rewrite must
   happen **before** any `postprocess -Apply` runs on a migrated document.

Step 4 is where [`FOLDER_RE`](../scripts/zoombie/commands/library.py:45) earns its keep: it is
**demoted from runtime gate to legacy-name reader**. After migration it is used only here, which
is the right home for a format that exists solely to describe the past.

## 7. Role and skill awareness

- [`modes/zoombie.yaml`](../modes/zoombie.yaml) gains an item-model section: what an item is;
  that the **folder name is the user's to choose** and is never parsed; that metadata lives in
  `.data/item.json`; that **block 6 is a verbatim copy of the source, not a recap**; and that
  `zoombie items` is the way to enumerate a workspace.
- Bump [`ROLE_VERSION`](../scripts/zoombie/__init__.py:36) `1.0.0` → `1.1.0`. The role is
  versioned by content comparison and carries no marker key, deliberately
  ([`__init__.py:28`](../scripts/zoombie/__init__.py:28)), so this is the only edit needed.
- Bump [`SKILL_VERSION`](../scripts/zoombie/__init__.py:23) `4.3.0` → `4.4.0` and update the
  `cvrm-zoombie-version` line in **every** `SKILL.md`. The marker must stay within the **first
  12 lines** or the skill silently reads as unowned
  ([`__init__.py:20`](../scripts/zoombie/__init__.py:20)); tests already guard this
  ([`tests/test_skills.py:111`](../tests/test_skills.py:111)).
- Per skill:
  - `zoombie-summarize` — the item contract, `.data/` paths, the free folder name, the block-6
    declaration, `items` as the enumerator, and the removal of the "never invent the number"
    dead end in favour of "read `nextNumber` from the scan".
  - `zoombie-pdf-to-md` — image destination becomes `<item>/.data/img`; the sidecar keeps its
    `README.md` name.
  - `zoombie-transcribe-audio`, `zoombie-transcribe-video` — outputs land in `<item>/.data/`,
    and the kept source lands at the item root.
  - `zoombie-download-video` — media lands at the item root when the run is part of item
    production.
- [`README.md`](../README.md) and [`setup.md`](../setup.md) — the naming paragraph
  ([`README.md:418`](../README.md:418)), the skill→CLI table, and any `img/` reference.

## 8. Open decisions

1. **Does the library `README.md` index survive as a distinct concept?** Recommended: yes, but
   as a pure renderer over the `items` scan, so there is one definition of an item and no second
   parser.
2. **Is source retention mandatory or opt-in?** Recommended: mandatory placement when the media
   exists, with `sourceKept: false` still valid when it never existed. The contract already
   tolerates absence via `source.json`.
3. **Does `.data/` hide break anyone's workflow?** Accepted as the price of a clean item folder;
   worth one line in `setup.md` since "my images are missing" is a predictable report.
4. **What is the declaration check's exact phrase set?** Needs Russian and English forms, and a
   decision on whether a missing declaration is a `verify` failure (exit 1) or a warning.

## 9. Acceptance criteria

- A folder named `a Заметки`, `2020-05-06 Заметки`, or `Заметки` is recognized as an item when
  it holds `.data/` or `summary.md`, and its `number`, `date` and `title` come from
  `.data/item.json`.
- `zoombie items` on a workspace returns the documented shape in one JSON line, with
  `nextNumber`, per-item contents, and a `skipped` list carrying reasons.
- A second `postprocess -Apply` on a document whose figures live in `.data/img/` is
  **byte-identical to the first** — pinned by a regression test, since the current guarantee
  rests on the `img/` substring happening to survive inside the new path.
- `verify` reports a non-item folder and a block-6 heading that does not declare itself a copy.
- A migrated library renders the same index rows it rendered before migration.
- New tests: `tests/test_items.py` (recognition, `nextNumber`, duplicates, the documented JSON
  shape), `tests/test_markdown.py` additions for the directory-derived strip pattern, and
  `tests/test_library.py` pinning the accepted and rejected name forms — the file that does not
  exist today.

## 10. Handoff

Everything above that touches `.py` or `tests/` is Code mode work, not an analytical artifact.
The recommended order, smallest-risk first, is deliberate:

1. **The `.data/` path constants** in one shared place — `DATA_DIR_NAME = ".data"` beside the
   existing `IMAGE_DIR_NAME = "img"`, joined where the item-relative image path is needed — so
   the eight hard-coded sites read one value instead of spelling a path each.
2. **A byte-idempotency regression test** over `.data/img/`, then the directory-derived
   strip/insert hardening ([`markdown.py:65`](../scripts/zoombie/lib/markdown.py:65),
   [`:243`](../scripts/zoombie/lib/markdown.py:243)). No longer a prerequisite, but the test
   should exist before the paths move, because it is the check that proves the nesting worked.
3. **`zoombie items`** and the evidence-based recognition predicate.
4. **`item.json`** write/read, and `index` rendered from the scan.
5. **Source retention** in the pipeline, plus the `.gitignore` patterns.
6. **Migration** pass, with the dry run.
7. **Section-6 declaration** and the `verify` lint.
8. **Role and skill text**, with both version bumps.

Decisions 1 and 2 of section 8 are the only ones that change the data model; the rest of the
plan is mechanical once those are fixed.

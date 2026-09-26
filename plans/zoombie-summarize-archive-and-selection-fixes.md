# Plan: summarize overwrite-safety and the silent selection no-op

Origin: an end-to-end summarize run over a 75-minute Crimson/Ford video
(`C:\...\kb\Crimson\10 - 01.05.2020 - ...`) that produced a correct document but
exposed four defects. This plan fixes them and revises the contract they violate.

## What happened

1. A prune was issued as `summarize -Step slides -Run X -Drop "f001,f006,f007"`,
   omitting `-Slides`. The step returned `ok: true`, exit 0, `"applied": false`,
   `"extracted": 0`, and the frame set was unchanged. All 20 frames were published.
2. Re-running the flow to fix it did **not** archive the existing `summary.md`
   (the contract says it must) but created a sibling folder `... (2)`.
3. Every `data.next.args` for the `slides` and `name` steps omits `-Slides`, so the
   recommended invocation is not replayable — it is the same no-op as 1.
4. The prose step warned that the ordinal slide-time association was refused
   (13 headings vs 17 manifest times) and fell back to SRT search, which matched
   all 13 headings. Correct behaviour, but it fires on every legitimate prune.

## Root causes

- `-Keep`/`-Drop` are honoured **only inside** the `if want:` branch of
  [`_step_slides()`](../scripts/zoombie/commands/summarize.py:526). Without
  `-Slides` they are parsed and discarded, with no error.
- [`_step_name()`](../scripts/zoombie/commands/summarize.py:436) uniquifies the
  item folder, then calls
  [`_archive_existing()`](../scripts/zoombie/commands/summarize.py:605) on that
  brand-new folder, which cannot contain a `summary.md`. Archiving worked for an
  **in-place** source (line 426, no uniquify) and failed for an **external** source.
- The ordinal refusal at
  [`postprocess.py`](../scripts/zoombie/commands/postprocess.py:325) is deliberate
  and sound; the alarm level is the only problem.

## Decisions taken

- **Overwriting is an explicit, warned choice — never silent and never automatic.**
  When the target folder already holds a document, the `name` step does not
  proceed: it reports exactly what would be lost and refuses until the caller
  chooses. The agent asks the user, who may back down and archive by hand.
- **No backup by default.** The user opted out of an automatic archive: a rewrite
  replaces `summary.md` and `img/` outright. Archiving stays available as an opt-in
  (`-Archive`), which moves the predecessor into `summary_<stamp>/`.
- **The archive is a folder, not a file.** `summary_<stamp>/` holds `summary.md`
  **and** its `img/`. The figures are index-numbered (`005 - 00-26-08.png`), so a
  20-frame and a 17-frame run disagree about what index 005 means — a flat backup
  would repoint the archived document at the wrong pictures or at none. Every link
  is relative (`img/...`), so moving the pair keeps them valid with **no rewriting**.
- **The source is never collateral.** The overwrite surface is exactly two
  artifacts: `summary.md` and `img/`. The media and every other file survive
  untouched. This is an invariant, not a caution: for an in-place source the item
  folder *is* the source folder, so a folder-level wipe would destroy the user's
  own recording. A source inside the workspace must still be there afterwards.
- **Reuse the target folder** when it exists and is empty or item-like; fall back to
  `unique_name()` only for a foreign folder.
- **Infer `want`** from a keep/drop selection rather than refusing it, so the
  documented "re-run -Step slides with -Keep/-Drop" wording keeps working.

## Code changes

1. [`summarize.py`](../scripts/zoombie/commands/summarize.py:526) — in
   `_step_slides`, `want = want or bool(keep or drop)`, with a log line naming the
   inference.
2. [`summarize.py`](../scripts/zoombie/commands/summarize.py:436) — in
   `_step_name`: decide the target first (reuse an empty or item-like folder, else
   `unique_name`), then inspect it. If it holds a `summary.md` and neither
   `-Overwrite` nor `-Archive` was given, raise a `ZoombieError` that names the
   document at risk and how to proceed. Require no new flag for an empty target, so
   the ordinary first run is unchanged.
3. [`cli.py`](../scripts/zoombie/cli.py:590) — add `-Overwrite` and `-Archive`
   booleans to `summarize`, alongside `-ConfirmMove` and following its shape.
   `-Archive` implies the overwrite; `-Overwrite` alone means a clean rewrite.
4. [`summarize.py`](../scripts/zoombie/commands/summarize.py:605) — rewrite
   `_archive_existing` to create `summary_<stamp>/` and move BOTH `summary.md` and
   `img/` into it, keeping the collision loop (`summary_<stamp> (2)`) at folder
   level. Report `{from, to, timestamp}` where `to` is the folder.
5. [`summarize.py`](../scripts/zoombie/commands/summarize.py:500) — the `name`
   result gains an `existing` block (`{summary, images, media, others}`) and an
   `overwrite` block (`{required, archived}`), and `why` tells the agent to ask the
   user, naming `summary_<stamp>/` when an archive was taken.
6. [`summarize.py`](../scripts/zoombie/commands/summarize.py:698) — confirm
   `_publish_figures` rebuilds only `img/` and touches nothing else; this is what
   makes a no-backup rewrite safe. Add an assertion or comment pinning the
   invariant so a future edit cannot widen the blast radius to the media.
7. [`summarize.py`](../scripts/zoombie/commands/summarize.py:510) — the `next.args`
   for `slides` carries `-Slides: "true"`; the `name` step's `why` states that
   `-Slides` true or false is mandatory (a placeholder would exit argparse, so it
   cannot ride in `args`).
8. [`postprocess.py`](../scripts/zoombie/commands/postprocess.py:346) — demote the
   refusal to a note when the manifest was narrowed by a selection; keep it a
   warning otherwise. Fix the stale advisory docstring at
   [`verify.py`](../scripts/zoombie/commands/verify.py:17).
9. [`verify.py`](../scripts/zoombie/commands/verify.py:270) — `_is_archived_summary`
   must recognise archive **directories**, and the tree walk must not descend into
   them, so an archived document is never checked against the live tree.
10. [`item/scan.py`](../scripts/zoombie/item/scan.py:1) — confirm a nested archive
    folder is not counted as a second item.

## Instruction and contract changes

11. [`SKILL.md`](../skills/zoombie-summarize/SKILL.md:76) — the prune instruction
    must show `slides:"true"`, e.g.
    `{"step":"slides","run":R,"slides":"true","drop":"f001,f003"}`, plus `keep` and
    `drop` rows in the flow table, and an instruction to check
    `data.slides.selection.applied` — `false` means the selection was ignored.
12. [`SKILL.md`](../skills/zoombie-summarize/SKILL.md:1) — replace the
    archive para with the new rule: an existing summary makes the `name` step
    STOP and report what is at risk; the agent must ask the user, who may back down
    and archive by hand, choose `-Archive` to snapshot the predecessor and its
    figures into `summary_<stamp>/`, or `-Overwrite` for a clean rewrite. State that
    the media is never removed. This changes the documented "never overwrite
    silently" line from an automatic action to an explicit question.
13. [`mcp.py`](../scripts/zoombie/mcp.py:729) — the tool description's arg list
    omits `keep`/`drop`; add them plus `overwrite` and `archive`, and state the
    `slides:true` requirement and the overwrite confirmation.
14. [`cli.py`](../scripts/zoombie/cli.py:566) — `-Keep`/`-Drop` help must state that
    `-Slides true` is implied; `-Overwrite`/`-Archive` help must state that the
    media is preserved.
15. [`plans/zoombie-summarize-redesign.md`](../plans/zoombie-summarize-redesign.md:98)
    — revise Overwrite-safety: the flat `summary_<stamp>.md` rename becomes the
    opt-in folder archive; archiving is no longer automatic; and correct the claim
    that it happens at the prose step (the code archives at name, deliberately).

## Tests

16. A regression for the no-op: `-Drop` without `-Slides` must prune, and the
    selection must report `applied: true`.
17. A contract test that replays each step's `data.next.args` verbatim and asserts
    the next step acted — the guard that catches defects 1 and 3 together.
18. An external-source `name` step over an existing item: with neither flag it
    refuses and names the document; with `-Archive` the predecessor and its figures
    land in `summary_<stamp>/` and the target is reused; with `-Overwrite` the item
    is rewritten with no archive folder.
19. The source-preservation invariant: after a no-backup overwrite, the item still
    holds its `video.mp4` (and, for an in-place source, the source folder's original
    file), and an archive folder's document still resolves its own figures.
20. [`test_summarize.py`](../tests/test_summarize.py:143) — `TestArchive` currently
    asserts automatic archiving at the name step; rewrite it for the opt-in flow.
21. [`test_skills.py`](../tests/test_skills.py:543) — extend the archive assertion
    to require the prune example to carry `slides:true` and the overwrite question
    to be described.

## Risks and open questions

- `img/` is rebuilt by `_publish_figures`; the archive move must happen before it,
  or the predecessor's figures are already gone. Ordering is the point of item 4.
- Widening the overwrite beyond `summary.md` and `img/` would delete the media and
  is the single most dangerous edit here; item 6 exists to prevent it.
- An item folder named `summary_<stamp>/` nested inside an item must not be taken
  for an item by `is_item` (which keys on `summary.md`). Verify the nested case and
  an `items -Recurse` run.
- The existing regression
  [`test_verify.py`](../tests/test_verify.py:347) is labelled for a real Ford
  re-run archive, so archiving on this corpus has worked at least once; the
  external-source path is either a regression or was never exercised.

## Out of scope

- The two Ford items on disk. The fix must land and be confirmed first, then the
  superseded 20-figure item is retired and the corrected one given the canonical
  name. An agent must not hand-move those folders.
- Frame classification (which images are "slides") — a judgement call, not a defect.

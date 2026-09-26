# Live test: the summarize flow end to end

A **live test** drives the real `summarize` flow against real media through the real
installed toolchain. It is not part of the unit suite: it transcribes audio on the
GPU, downloads from the network and takes minutes, so it runs **only when the user
explicitly agrees**. Never start it off your own bat, and never as part of a retest
sweep.

If you are the agent and you want to run it, **ask first**, then follow this file
top to bottom.

## The rule for the agent

1. The global test run may print a one-line hint that a live test was not run. That
   hint is a REMINDER, not an instruction: ask the user before acting on it.
2. On approval, run the tasks below in order, one subtask each, and keep the run
   scratch until you have reviewed the results.
3. Afterwards, do the review pass (bottom of this file) and report hiccups.

## What is in this folder

| Path | What it is | Tracked? |
|------|------------|----------|
| `readme.md` | this file; the instructions | yes |
| `run_livetest.py` | creates the harness and prints the task commands | yes |
| `23 - 05.06.2020  - От Страха к Богатству - стратегия Citi Private Bank для клиентов-миллионеров.pdf` | the PDF source. Note the **double space** before the dash and the Cyrillic name: both are deliberate stressors for path handling | **no** - personal, stays on this machine |
| `some whatever name user picked for a file.mp4` | the video source, named as a user would name it: spaces, lower case, no extension hint of its own | **no** - see the caution below |

The two media files are **deliberately untracked**. `.gitignore` already excludes
`*.pdf` and `*.mp4` globally, and this folder carries its own local rules for them
as well, so they exist only in your working copy. That is intended, not an
oversight: do not "fix" it by force-adding them.

The folder itself is NOT ignored - only `ws/`, `out/` and the samples are - so the
instructions and the runner travel with the repository while the media stays put.

`ws/` and the outputs under it are created by `run_livetest.py` and are disposable.
Outputs land in `_unsorted/`, which is gitignored too.

## The matrix

Three sources by three placements. The link case has no placement question.

| # | Source | Placement | Destination the flow must choose |
|---|--------|-----------|----------------------------------|
| 1 | video file | inside the workspace | beside the media, in its own folder, name unchanged |
| 2 | video file | outside the workspace | `<workspace>/_unsorted/summaries/<name>/` |
| 3 | PDF | inside the workspace | beside the PDF |
| 4 | PDF | outside the workspace | `<workspace>/_unsorted/summaries/<name>/` |
| 5 | remote video | a link, no placement | `<workspace>/_unsorted/summaries/<name>/` |

### What "the workspace" means here

The workspace root is **the working directory of the process that runs the flow**.
With the MCP tool that is the folder the client opened - this repository - so
`tests/livetest/ws/...` counts as *inside* it. A file under `%TEMP%` is *outside* it.
That is the whole distinction, and it is worth re-checking on every run: a
mis-detected root silently sends the document to the wrong place.

### One case that is deliberately absent

A source *the workspace contains* whose output lands *outside* it is not reachable:
by the destination rule, any source inside the workspace is summarized in place.
Only an outside source routes to `_unsorted`. This is a known shape of the design,
not a gap in the test.

## Setup

```powershell
cd tests\livetest
python run_livetest.py            # creates ws/ and the outside copies, writes ws/TASKS.md
python run_livetest.py --reset    # clears the harness instead of building it
```

The script is idempotent; `--reset` clears `ws/` and the outside copies first.

It writes the task list to **`ws/TASKS.md`** rather than printing the commands, and
prints only the pointer to it. That is deliberate: the resolved paths carry
Cyrillic and a doubled space, and echoing them through the console code page would
corrupt exactly the values this test exists to stress. Read `ws/TASKS.md` for the
commands with the real paths in them; this file remains the human-facing form.

If it exits 1 naming a missing sample, that is the untracked media being absent -
see "What is in this folder" above. It never invokes the flow itself.

## The subtasks

Run **one subtask each**, so a hiccup is attributable. Substitutes: with the MCP
`summarize` tool preferred; the CLI form is given once in the "Running" section.

### Subtask 1 - video, inside

Source: `tests/livetest/ws/media/some whatever name user picked for a file.mp4`

1. `summarize {source: "<that path>"}`
2. Expect `data.internal: true` and **no** media question.
3. `name` step: the folder is fixed, so confirm it as-is.
4. `slides: "true"`.
5. Review the frames, drop the presenter's face, and **check
   `data.slides.selection.applied`**:
   `{"step":"slides","run":R,"slides":"true","drop":"f001"}`.
6. `prose`, then `verify`.
7. Assert: `summary.md` sits **beside the mp4**, the folder name is **unchanged**,
   and `img/` holds only the kept figures.

### Subtask 2 - video, outside

Source: `%TEMP%\zoombie-livetest\outside\` - same file, copied out of the workspace.
The script prints the absolute path; it differs per machine, so always take it from
the script rather than guessing.

1. `summarize {source: "<that path>"}` - expect `data.media.choiceRequired: true`.
2. `name` step: ask the media question and answer `copy`.
3. Same slides/prose/verify sequence as subtask 1.
4. Assert: the item is at `<workspace>/_unsorted/summaries/<name>/`, holds
   `summary.md`, the copied `video.mp4` **and** `img/`, and the original in
   `%TEMP%` is **still there**.

### Subtask 3 - PDF, inside

Source: `tests/livetest/ws/media/23 - 05.06.2020  - ... .pdf`

1. `summarize {source: "<that path>"}` - a PDF is rendered, not transcribed, so
   expect the render path rather than whisper.
2. `slides: "false"` - pages are not slide frames; keeping this false is itself the
   assertion that a PDF never silently yields frames.
3. `prose`, then `verify`.
4. Assert: `summary.md` beside the PDF, and block 6 is the rendered text.

### Subtask 4 - PDF, outside

Source: `%TEMP%\zoombie-livetest\outside\` - the same PDF, copied out.

1. As subtask 3, but expect the media question and the `_unsorted` destination.
2. Assert the same as subtask 2, with the PDF in place of the video.

### Subtask 5 - remote video

Source: `https://rutube.ru/video/ee7e9f4af68a1b34df85219d147bfe99/`

It resolves to a short film trailer, and that is INTENTIONAL, not a stale link: it
is the chosen fixture precisely because its title carries U+29F8, the codepoint that
once crashed the result line on a cp1251 console. Leave it as it is - a more
conventional video would quietly stop testing that path.

1. `summarize {source: "<that url>"}` - expect a download, then transcription.
2. No media question: a downloaded source is ours. `data.media.choiceRequired` must
   be **false**.
3. Slides, prose, verify as subtask 1.
4. Assert: `_unsorted/summaries/<name>/`, the downloaded media retained, `img/`
   present, and no `(2)` sibling anywhere.

### Subtask 5b - overwrite safety

Only after subtask 5 has produced an item: re-run it against **its own** name.

1. `name` step with the same name - the target already holds a summary, so the step
   must **refuse**, name the document at risk, and touch nothing.
2. Re-run with `archive: true`. Assert `data.archived` is a **folder** containing
   both `summary.md` and its `img/`, that the live item still holds its media, and
   that a `(2)` folder was **not** created.
3. Re-run once more with `overwrite: true` and assert no archive was added and the
   media is still there.

## Running it as CLI instead of MCP

Only if the MCP tool is unavailable. The launcher lives under the user profile; on a
machine whose user name is not ASCII it is installed under `%PUBLIC%`:

```powershell
$cli = @(
    "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.cmd",
    "$env:PUBLIC\zoombie-env\bin\zoombie\zoombie.cmd"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
& $cli summarize -Source "<path-or-url>"
& $cli summarize -Step name -Run "<run>" -Name "<name>" -Media copy
& $cli summarize -Step slides -Run "<run>" -Slides true -Drop "f001,f003"
& $cli summarize -Step prose  -Run "<run>" -Title "<title>" -SummaryText "<text>" -Sections "<json>"
& $cli summarize -Step verify -Run "<run>"
```

A Cyrillic or spaced argument that arrives mangled is the console code page, not the
tool: stage the value in a UTF-8 file and pass `@path` instead.

## Review pass

After the subtasks, review the results and report every hiccup. A live test that
passes silently is worthless; this section is the point of it. Look for:

- **A prune that did not prune.** `data.slides.selection.applied` must be `true` and
  `droppedIds` must name what you dropped. `false` means the whole frame set was
  published - the exact defect this test was written to catch.
- **A `(2)` sibling folder.** A re-run must reuse the item, never accrete copies.
- **An archive that is a flat file.** `summary_<stamp>` must be a **folder** with its
  own `img/`; a lone `.md` means the document's figures were left to be overwritten.
- **A vanished source.** After any overwrite or archive, the media must still be
  there. This is the invariant that matters most: for an in-place source the item
  folder *is* the source folder.
- **Wrong destination for the placement.** Inside must be in place; outside and link
  must be under `_unsorted`.
- **A folder name that changed.** An in-place item keeps the source folder's name.
- **Warning noise.** A legitimate prune should log the slide-time fallback as a
  note, not as a `WARN`.
- **Cyrillic and doubled spaces.** Any mangled path, missing figure or failed lookup
  caused by the PDF's name is a real bug and the reason that name was chosen.

Then clean up:

```powershell
python run_livetest.py --reset
```

## What the live test has actually found

A live test is worth its time only if it has caught something. These were all
invisible to the unit suite and were found the first time it ran end to end. Keep
them in mind when reviewing, because each one was a silent failure rather than an
error.

**A result line that could not be encoded (crash, fixed).** The remote video's title
contains U+29F8, which the console code page here (cp1251) cannot represent. Download
and transcription both succeeded and then the command died emitting its JSON result:
`UnicodeEncodeError`, exit 1, **no result line at all**. The one-line-per-invocation
contract was broken with a stack trace. The result is now written as UTF-8 bytes
rather than through the console's text layer. Look for this whenever a source title
carries an exotic codepoint.

**An archived item that stopped being recognised (fixed).** After `-Archive` the item
root holds the `summary_<stamp>/` folder and the media, but **no** `summary.md`. The
next run therefore treated its own item as a stranger's folder and created `... (2)`
instead of reusing it. An archive folder is now recognised as evidence of our own
item. Watch for any `(2)` sibling appearing where a reuse was expected.

**A warning that cried wolf on every legitimate prune (fixed).** The slide-time
association warns when block-6 heading count and manifest figure count differ. After
any prune they always differ, so a correct run logged a `WARN` saying timestamps
"shift silently". The mismatch is now a note: the direction of the difference cannot
distinguish a defect from ordinary use, and the real safety is that the ordinal
stamp is REFUSED and the SRT path is used instead.

**One case, one folder (harness fix).** The first version put both samples in a
single directory. An in-place source IS its item, so the video item became the folder
the PDF run had to write into and the overwrite gate fired on what should have been a
clean first run. The inside cases now get separate folders.

The engine itself, notably the `-Drop` prune, worked on the very first attempt in
every video subtask: `selection.applied` was `true` with the right `droppedIds`. If it
ever comes back `false`, that is a regression of the defect this harness exists to
guard, and it should be the first thing you report.

## Caution: this folder is on the edge of .gitignore

The folder itself is tracked, but its **media is not**: `*.pdf` and `*.mp4` are
ignored globally, so the two samples never leave this machine. Before committing,
run `git status tests/livetest` and confirm you are staging **only** `readme.md`,
`run_livetest.py` and `.gitignore` - never a sample, never an output.

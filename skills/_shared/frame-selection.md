### YOU make the final keep/drop — the detector only proposes

Auto-detect **cannot** tell a slide from a talking head: a webcam close-up is a
stable run too, so a talking-head video comes back with one frame per run (the
Crimson run got **40 frames, ~33 of them the same face**). The tool will not guess
this for you, and you must not silently accept 40 faces — but you also must not
touch the files.

The loop is: **run `slides`, look, decide, re-run `slides` with your verdict.**

1. Run `slides`. Read `data.images.imageIds` (`f001`, `f002`, … the proposed
   frames) and `data.visionFrames[].id`. The attached image blocks are your look at
   the frames; the deferred ones are listed by id in the result text.
2. Decide which frames are worth keeping — a content frame (chart, screenshot, PDF
   page, table) stays; a duplicate face or a transition goes. Use the OCR text in
   `data.ocr.artifact` and your own eyes, not char count.
3. Re-run `slides` with that verdict:
   * `keep: "f005, f006, f014, f017, f027, f031"` — an **allow-list**. Non-empty
     REPLACES the default set, so 40 proposals become your 7 content frames.
   * `drop: "f002, f009"` — a **deny-list**, for stripping known noise without
     enumerating everything you want.
   * Frames are named by **id** (`f005`) or by their **timestamp** (`00:04:04`,
     `00-04-04` or `244`) — the handles read in the first result. Ids are stable
     across runs: `f005` is the 5th *proposed* frame, whatever you keep.
   * A long list goes in a file: `keep_file: "<path>"` (one id per line), for when
     the shell would mangle a long inline value.
   * An id the run did not propose is **refused by name** — fix the id, do not
     retry blindly.
4. `data.images.selection` echoes what was kept and dropped.

### The tool owns every file — you only name frames

**Never delete, move, rename, copy or hand-edit anything under the run scratch
`img/`, and never edit `manifest.json` or the frames yourself.** Naming a frame in
`keep`/`drop` is the ONLY way you may influence which frames exist: the tool
applies the selection, prunes the dropped frames and rewrites the manifest. A
frame path is never a valid `keep`/`drop` value. If a previous run confuses you,
re-run `slides` with a corrected selection — do not "clean up" the directory.

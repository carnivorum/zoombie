**Scratch is the CLI's job, not yours.** The `summarize` flow keeps a whole run's
throwaway - the transcript, the `.srt`, the origin sidecar, the OCR report, the
image manifest and the reading copies - in a run scratch dir under the workspace
`/.tmp/zoombie-summarize/<run>/`, and deletes it at the `verify` step on success.
Do not create, move or delete those files yourself.

A run puts its own other intermediate files
under the toolchain's ASCII scratch root (`zoombie-env\work\<guid>` for $job work
dirs, `zoombie-env\tmp\<prefix>-<guid>` for downloads/extraction) and **removes
them as it finishes** — including after a failure. You do not delete toolchain
scratch by hand, and a `vision`/`image_dir` destination you named is YOUR
artifact, not scratch: it is never removed. To inspect a run's scratch, pass
`keep_scratch: true` (alias `keep_work: true`) and the result reports it in
`data.scratch`. A killed run can leave a busy dir behind; clean it with the
`clean` tool and `{"clean_scratch": true}` (add `dry_run: true` to preview). That
verb removes only dirs carrying our run-marker name under a scratch root — a
folder of yours that merely sits there is reported, never deleted.

Everything else you write that is not a deliverable — a value staged for an
`@file` argument, an intermediate, a debug dump — goes under the workspace's own
`/.tmp/` folder (`<workspace>\.tmp\`): never the workspace root, never beside the
source, and never a hard-coded `C:\Temp`. Create it on first use and remove what
you created when done. `/.tmp/` is git-ignored, so a file a failed run leaves
behind stays out of the way instead of polluting the repository.

**Scratch is the CLI's job, not yours.** A run puts its own intermediate files
under the toolchain's ASCII scratch root (`zoombie-env\work\<guid>` for $job work
dirs, `zoombie-env\tmp\<prefix>-<guid>` for downloads/extraction) and **removes
them as it finishes** — including after a failure. You do not delete toolchain
scratch by hand, and a `-Vision`/`-ImageDir` destination you named is YOUR
artifact, not scratch: it is never removed. To inspect a run's scratch, pass
`-KeepScratch` (alias `-KeepWork`) and the result reports it in `data.scratch`.
A killed run can leave a busy dir behind; clean it with
`$cli clean -CleanScratch` (add `-DryRun` to preview). That verb removes only
dirs carrying our run-marker name under a scratch root — a folder of yours that
merely sits there is reported, never deleted.

Everything else you write that is not a deliverable — a value staged for an
`@file` argument, an intermediate, a debug dump — goes under the workspace's own
`/.tmp/` folder (`<workspace>\.tmp\`): never the workspace root, never beside the
source, and never a hard-coded `C:\Temp`. Create it on first use and remove what
you created when done. `/.tmp/` is git-ignored, so a file a failed run leaves
behind stays out of the way instead of polluting the repository.

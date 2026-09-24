**Keep scratch out of the workspace.** The CLI already puts its own intermediate
files in the toolchain's ASCII scratch root, so nothing of its leaks into your
project. Everything YOU write that is not a deliverable — a `-Vision` page
render, a value staged for an `@file` argument, an intermediate, or a debug dump
— goes under the workspace's own `/.tmp/` folder (`<workspace>\.tmp\`): never the
workspace root, never beside the source, and never a hard-coded `C:\Temp`. Create
it on first use and remove what you created when done. `/.tmp/` is git-ignored, so
a file a failed run leaves behind stays out of the way instead of polluting the
repository.

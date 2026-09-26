# Plan: live-test harness (runner, hint, gitignore)

Companion to [`tests/livetest/readme.md`](../tests/livetest/readme.md), which is the
instruction file. This plan covers the code and config the README refers to.

## Context

The user wants the **global** test run to print a hint that a live test exists and
was not run, so the AGENT can ask for permission. The live test itself must never
run automatically - it transcribes on the GPU, downloads from the network and takes
minutes.

Two facts establish the ground:

- [`workspace_root()`](../scripts/zoombie/lib/workspace.py:66) is always the working
  directory, and the registered MCP server pins no `cwd`, so the workspace is the
  repository the client opened. "Inside" therefore means
  `tests/livetest/ws/...`; "outside" must be a temp directory outside the repo.
- `.gitignore` already ignores `*.pdf` (line 25) and `*.mp4` (line 16) globally, so
  the two sample files stay local whatever the folder line says. `tests/livetest`
  (line 47) must go so the instructions are committable.

## 1. `.gitignore`

- **Remove** the bare `tests/livetest` line. A bare entry ignores the directory and
  everything in it, so without this neither `readme.md` nor the runner can be
  committed.
- **Add** local, explicit rules so the samples stay on this machine even if the
  global media patterns are ever narrowed:

```
# Live test: instructions and runner are TRACKED, samples and harness are not.
tests/livetest/ws/
tests/livetest/out/
tests/livetest/*.pdf
tests/livetest/*.mp4
```

- The two media lines are redundant against the global rules and are kept anyway:
  they document the intent and they make the guarantee local, which is what "the
  PDF is personal" actually requires.
- Consequence to state in the commit message: with the folder un-ignored, any stray
  file left in `tests/livetest/` becomes committable. `ws/` and `out/` are covered,
  and run output goes to `_unsorted/` (already ignored), so the only thing a human
  could accidentally stage is a loose sample - which now has its own rule.

## 2. `tests/livetest/run_livetest.py`

Creates the harness and prints the tasks. Idempotent; `--reset` clears first.

- **Paths, derived not hardcoded.** `REPO = Path(__file__).resolve().parents[2]`,
  `HERE = Path(__file__).resolve().parent`, `WS = HERE / "ws"`,
  `MEDIA_DIR = WS / "media"`, and the outside copy under
  `tempfile.gettempdir() / "zoombie-livetest" / "outside"`.
- **Discover the media, do not name it.** Glob `*.mp4` and `*.pdf` in `HERE` rather
  than hardcoding the long Cyrillic filename. If either is missing, exit 1 with a
  message naming what it looked for, because the samples are untracked and a fresh
  clone will not have them.
- **Copy, never move.**
  `here/<sample>` -> `ws/media/<name>` for the inside case, and
  `here/<sample>` -> `<outside>/<name>` for the outside case.
- **Write the tasks to a UTF-8 file and print only a pointer.** The PDF name carries
  Cyrillic and a doubled space, so printing the commands would round-trip through the
  console code page and mangle them - the same hazard the CLI's `@file` convention
  exists to avoid. Write `ws/TASKS.md` (UTF-8) with the five subtask commands
  templated with the real resolved paths plus the rutube URL, and print its path.
  The README's subtask sections stay the human-facing form.
- `--reset` removes `ws/` and the outside directory, nothing else.
- Keep it dependency-free (stdlib only) and ASCII-safe in its own source.

## 3. The global-run hint - `tests/conftest.py`

Add a `pytest_terminal_summary` hook. It must be a no-op unless the run is genuinely
the global one, which is the distinction the user drew ("dont run every retest"):

```python
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if exitstatus != 0:            # a broken suite has a real problem to fix first
        return
    if config.option.file_or_dir or config.option.keyword:
        return                     # a partial run or retest: stay quiet
    if os.environ.get("ZOOMBIE_NO_LIVETEST_HINT"):
        return
    terminalreporter.write_line(livetest_hint())
```

- The condition `not file_or_dir and not keyword` is what makes it fire on a bare
  `pytest` (the global run) and never on `pytest tests/test_x.py` or `-k ...`.
- Factor the text into `livetest_hint()` returning one line, so it is unit-testable:
  it names `tests/livetest/readme.md` and states that the agent must **ask the user**
  before running, and must not run it during a retest.
- The hook runs no command and touches no file. It is a hint, nothing more.

## 4. Tests

- `tests/test_livetest_harness.py`:
  - `.gitignore` no longer carries a bare `tests/livetest` line, and does carry the
    `ws/` and local media rules. Reading the file and asserting on it matches the
    existing style ([`test_installer_scripts.py`](../tests/test_installer_scripts.py:1)
    asserts on file contents the same way).
  - `livetest_hint()` names the readme and the ask-first rule.
  - `run_livetest.py` imports and its path helpers resolve under the repo without
    creating anything (call the pure helpers, not `main`).
- Guard the runner's media discovery with a monkeypatched empty directory, asserting
  the clear failure rather than a `FileNotFoundError`.

## 5. Documentation touchpoints

- [`readme.md`](../tests/livetest/readme.md) is written and already covers the
  matrix, the cwd rule, the subtasks, the review pass and the gitignore caution.
- Decide whether the "ask before a live test" rule also belongs in
  [`modes/zoombie.yaml`](../modes/zoombie.yaml:1). It is an agent-behaviour rule, not
  a summarize rule, so the mode's instructions are its natural home; the README alone
  only helps an agent that already opened it. Recommend adding one line there.
- Optional: a pointer from the repo [`README.md`](../README.md:1) so a human knows
  the live test exists.

## Open questions

- `run_livetest.py` writes `ws/TASKS.md`; the README currently says the script "prints
  the tasks". Update the README wording to "writes the task list and prints its path"
  when the runner lands, so the two agree.
- The video sample is named `some whatever name user picked for a file.mp4`, which
  reads like a placeholder. Confirm it is the intended permanent sample and not a
  stand-in someone forgot to rename.

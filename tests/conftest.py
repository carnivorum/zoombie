"""Make the ``zoombie`` package importable from a plain checkout.

The package lives in ``scripts/zoombie/`` (it has to sit beside ``lib/`` and
``pdf/`` so the installed layout mirrors the repo layout), so ``scripts/`` is what
belongs on ``sys.path``, not the repository root.

This file also carries the LIVE-TEST HINT: a one-line reminder, printed only at the
end of a CLEAN GLOBAL run, that a real end-to-end test exists and was not run. It
is a reminder for the agent to ASK the user -- never an instruction to run anything,
and never an action. The live test transcribes on the GPU, downloads from the
network and takes minutes; see ``tests/livetest/readme.md``.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Suppress the hint entirely (for a scripted or CI run that has already decided).
NO_HINT_ENV = "ZOOMBIE_NO_LIVETEST_HINT"

LIVETEST_README = os.path.join("tests", "livetest", "readme.md")


def livetest_hint() -> str:
    """The one-line reminder. Factored out so it is testable without a pytest run."""
    return (
        "live test available but NOT run: tests/livetest/ (see "
        f"{LIVETEST_README}). It transcribes on the GPU and downloads from the "
        "network, so ASK THE USER before running it -- do not start it as part of "
        "a retest."
    )


def _is_global_run(config) -> bool:
    """True only for a bare ``pytest`` over everything.

    The user's rule is "hint on the global test run, not every retest". A partial
    run -- an explicit path, or a ``-k`` expression -- is a retest, so it stays
    quiet; a bare run is the whole suite and gets the reminder.
    """
    option = getattr(config, "option", None)
    if option is None:
        return False
    if getattr(option, "file_or_dir", None):
        return False
    if getattr(option, "keyword", None):
        return False
    return True


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Print the live-test reminder at the end of a clean global run.

    Silent when the suite is RED (a real failure is the thing to fix first) and on
    any partial run. Prints; never executes anything.
    """
    if exitstatus != 0:
        return
    if os.environ.get(NO_HINT_ENV):
        return
    if not _is_global_run(config):
        return
    terminalreporter.write_sep("-", "livetest")
    terminalreporter.write_line(livetest_hint())

"""Repo-level invariants for the unified installer entry points.

The distribution story is one instruction: run ``scripts\\zoombie-install.cmd``.
It is a GETTER — it fetches ``scripts/install.ps1`` from the repo and runs it — and
that split is what the tests here protect:

* **The getter holds no install logic.** If it grew Python discovery, a ``winget``
  call or archive handling, a saved copy would go stale the moment the flow
  changed. The forbidden-token checks below fail the suite if that happens.
* **Double-clickability with a bounded pause.** The getter must hold the console
  open on an Explorer double-click, but never on a shell/argument/automated run,
  so an agentic invocation cannot hang.
* **Restraint.** Neither script may mutate anything global (execution policy,
  ``setx``, the registry, ``$PROFILE``).
* **Non-ASCII safety.** Neither script may stage relative to its own location or
  the launch directory (``%~dp0`` / ``%CD%`` / ``$PSScriptRoot``), and the core
  must force the child's ``TEMP``/``TMP`` to an ASCII dir.
* **No dangling references.** The deleted bootstraps must not be named by any live
  file (plans may mention them as history).
"""

from __future__ import annotations

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
GETTER = os.path.join(SCRIPTS_DIR, "zoombie-install.cmd")
CORE = os.path.join(SCRIPTS_DIR, "install.ps1")

# Tokens that belong to the CORE only. Their presence in the getter means the
# getter absorbed install logic and can now go stale.
CORE_ONLY_TOKENS = ["winget", "codeload", "Expand-Archive", "zoombie.install"]

# The pre-Port implementation, gone from the tree. No live file may reference
# these as a path; plans may mention them as history.
DEAD_PATHS = ["bootstrap.cmd", "bootstrap.ps1", "scripts/setup-worker.ps1"]


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class TestGetterContract:
    def test_runs_from_a_file(self):
        assert os.path.isfile(GETTER)

    def test_embeds_the_default_repo_slug_and_ref(self):
        text = _read(GETTER)
        assert re.search(r'set "REPO_SLUG=carnivorum/zoombie"', text)
        assert re.search(r'set "REPO_REF=main"', text)

    def test_fetches_the_core(self):
        text = _read(GETTER)
        assert "scripts/install.ps1" in text, (
            "the getter must fetch scripts/install.ps1: it holds no install logic"
        )

    def test_holds_no_install_logic(self):
        text = _read(GETTER)
        for token in CORE_ONLY_TOKENS:
            assert token not in text, (
                f"the getter must not contain '{token}': that is core logic, and a "
                "saved getter carrying it would go stale"
            )

    def test_pause_is_bounded(self):
        """Pause only for a double-click; never for an automated run."""
        text = _read(GETTER)
        assert "pause" in text, "the getter must hold the window open on a double-click"
        assert "%cmdcmdline%" in text, "the double-click test must use %cmdcmdline%"
        assert "ZOOMBIE_NOPAUSE" in text, "ZOOMBIE_NOPAUSE must be able to suppress the pause"
        assert "-NoPause" in text
        # The pause must sit behind a guard that also requires no forwarded args.
        assert re.search(r'if not defined NOPAUSE if "%FWD%"==""', text), (
            "the pause must be suppressed when arguments are forwarded"
        )

    def test_stages_ascii_first(self):
        text = _read(GETTER)
        assert "%PUBLIC%" in text, "an ASCII staging fallback (%PUBLIC%) is required"
        assert "%~dp0" not in text, "the getter must not stage relative to its own location"
        assert "%CD%" not in text, "the getter must not stage relative to the launch directory"

    def test_no_global_mutation(self):
        text = _read(GETTER)
        assert "Set-ExecutionPolicy" not in text
        assert not re.search(r"(?mi)^\s*setx\b", text)
        assert "reg add" not in text.lower()


class TestCoreContract:
    def test_ensures_python_and_runs_the_installer(self):
        text = _read(CORE)
        assert "Python.Python.3.12" in text
        assert "-m zoombie.install" in text
        assert "PYTHONPATH" in text
        assert "PYTHONUTF8" in text

    def test_elevates_only_around_the_python_step(self):
        text = _read(CORE)
        assert "-Verb RunAs" in text
        # The elevation is a one-shot, waited-on child, not a self-relaunch.
        assert "Start-Process" in text
        assert "-Wait" in text
        assert text.count("-Verb RunAs") == 1, (
            "the only elevation must be the Python install; a second one means "
            "something else started requesting privilege"
        )

    def test_ascii_staging_and_child_temp(self):
        text = _read(CORE)
        assert "$env:PUBLIC" in text, "an ASCII staging fallback is required"
        assert "$env:TEMP" in text and "$env:TMP" in text
        assert re.search(r"\$env:TEMP\s*=\s*\$asciiTmp", text), (
            "the child TEMP must be forced to the ASCII stage"
        )
        assert "$PSScriptRoot" not in text, "the core must not stage relative to its own file"

    def test_forwards_the_expected_options(self):
        text = _read(CORE)
        for option in ("-Check", "-DryRun", "-Model", "-Root", "-Force"):
            assert option in text, f"the core must forward {option}"

    def test_no_global_mutation(self):
        text = _read(CORE)
        assert "Set-ExecutionPolicy" not in text
        assert not re.search(r"(?mi)^\s*setx\b", text)
        assert not re.search(r"SetEnvironmentVariable\s*\([^)]*'(User|Machine)'", text, re.IGNORECASE)
        assert not re.search(r"\$PROFILE\b", text)


class TestNoDanglingBootstrapReferences:
    def test_live_files_do_not_name_the_deleted_bootstraps(self):
        """Only plans and feedback may mention the old bootstraps, as history."""
        offenders: list[str] = []
        for base, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [
                name for name in dirs
                if name not in {".git", "__pycache__", ".pytest_cache", ".tmp"}
            ]
            relative_base = os.path.relpath(base, REPO_ROOT).replace("\\", "/")
            if relative_base.startswith(("plans", "feedback")):
                continue
            for name in files:
                if not name.endswith((".md", ".py", ".cmd", ".ps1")):
                    continue
                path = os.path.join(base, name)
                # The getter/core are what replaced them; this test names them as
                # DEAD_PATHS on purpose.
                if os.path.abspath(path) in (
                    os.path.abspath(GETTER),
                    os.path.abspath(CORE),
                    os.path.abspath(__file__),
                ):
                    continue
                text = _read(path)
                for dead in DEAD_PATHS:
                    if dead in text:
                        offenders.append(f"{os.path.relpath(path, REPO_ROOT)} names {dead}")
        assert not offenders, "live references to deleted bootstraps: " + "; ".join(offenders)

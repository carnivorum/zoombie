"""Repo-level invariants for the setup entry points.

The distribution story is a one-liner that downloads ``scripts/bootstrap.ps1``
and runs it (or ``scripts/bootstrap.cmd`` directly). Two properties make that
safe, and neither is visible from reading a single file:

* **Agreement.** ``bootstrap.ps1`` is a shim over ``bootstrap.cmd``: it must
  advertise the same options and embed the same default repo slug and ref. If
  they drift, the one-liner silently installs from the wrong place.
* **Restraint.** The shim must not mutate anything global (execution policy,
  ``setx``, ``$PROFILE``) and must not depend on its own file location, because
  it is normally run from a string via ``iex``.

This is the same class of check as ``tests/test_skills.py``: a reference that
can dangle. The docs name the entry points, so those references are checked too.
"""

from __future__ import annotations

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
PS1 = os.path.join(SCRIPTS_DIR, "bootstrap.ps1")
CMD = os.path.join(SCRIPTS_DIR, "bootstrap.cmd")

# Options bootstrap.cmd accepts and forwards to the Python installer.
EXPECTED_OPTIONS = ["-Check", "-DryRun", "-Model", "-Root", "-Force"]

# The pre-Port PowerShell implementation. It is gone from the tree; a live file
# must not reference it as a path. Plans may mention it only as history.
DEAD_PATHS = ["scripts/setup-worker.ps1", "scripts/zoombie.ps1", "scripts/lib/ZoombieEnv.psm1"]


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _ps1() -> str:
    return _read(PS1)


def _cmd() -> str:
    return _read(CMD)


# ---------------------------------------------------------------------------
# Agreement between the two entry points
# ---------------------------------------------------------------------------

class TestAgreement:
    def test_default_repo_slug_matches(self):
        ps1 = re.search(r"\$defaultSlug\s*=\s*'([^']+)'", _ps1())
        # Exclude '%' so the ZOOMBIE_REPO_SLUG indirection line is not matched;
        # only the literal default is under test.
        cmd = re.search(r'set "REPO_SLUG=([^"%]+)"', _cmd())
        assert ps1 and cmd, "both files must embed a default repo slug"
        assert ps1.group(1) == cmd.group(1), (
            f"bootstrap.ps1 slug '{ps1.group(1)}' != bootstrap.cmd slug "
            f"'{cmd.group(1)}': the one-liner would install from a different repo"
        )

    def test_default_repo_ref_matches(self):
        ps1 = re.search(r"\$defaultRef\s*=\s*'([^']+)'", _ps1())
        cmd = re.search(r'set "REPO_REF=([^"%]+)"', _cmd())
        assert ps1 and cmd, "both files must embed a default repo ref"
        assert ps1.group(1) == cmd.group(1), (
            f"bootstrap.ps1 ref '{ps1.group(1)}' != bootstrap.cmd ref "
            f"'{cmd.group(1)}'"
        )

    def test_both_advertise_the_same_options(self):
        ps1, cmd = _ps1(), _cmd()
        for option in EXPECTED_OPTIONS:
            assert option in cmd, f"bootstrap.cmd does not mention {option}"
            # The switch name appears either as the param itself or as the
            # argv passed to bootstrap.cmd.
            bare = option.lstrip("-")
            assert (option in ps1) or (bare in ps1), (
                f"bootstrap.ps1 does not forward {option}"
            )

    def test_ps1_never_invokes_python_directly(self):
        """The shim delegates; it must not grow a second install path."""
        text = _ps1()
        assert "zoombie.install" not in text, (
            "bootstrap.ps1 must not run the Python installer itself; "
            "bootstrap.cmd owns that step"
        )
        assert "-m zoombie" not in text


# ---------------------------------------------------------------------------
# Restraint: nothing global is mutated
# ---------------------------------------------------------------------------

class TestRestraint:
    def test_no_execution_policy_change(self):
        assert "Set-ExecutionPolicy" not in _ps1()

    def test_no_setx_command(self):
        # Match command usage at the start of a line, not a mention in prose.
        assert not re.search(r"(?m)^\s*setx\b", _ps1(), re.IGNORECASE)

    def test_no_environment_variable_is_persisted(self):
        text = _ps1()
        assert not re.search(r"SetEnvironmentVariable\s*\([^)]*'(User|Machine)'", text, re.IGNORECASE)

    def test_no_profile_edit(self):
        assert not re.search(r"\$PROFILE\b", _ps1())

    def test_no_author_of_the_script_blocks(self):
        assert "Unblock-File" not in _ps1()


# ---------------------------------------------------------------------------
# Location independence: it is run from a string, not a file
# ---------------------------------------------------------------------------

class TestLocationIndependence:
    def test_pscriptroot_is_not_used_to_resolve_paths(self):
        text = _ps1()
        assert "Join-Path $PSScriptRoot" not in text
        assert "Split-Path" not in text
        assert "$MyInvocation" not in text

    def test_pscriptroot_is_referenced_at_most_once(self):
        """Its only job is to tell file mode from inline (iex) mode."""
        # Count in CODE only: a comment may name it (as this one does).
        occurrences = sum(
            len(re.findall(r"\$PSScriptRoot\b", line)) for line in _code_lines()
        )
        assert occurrences == 1, (
            "$PSScriptRoot must appear exactly once, deciding file vs inline mode; "
            f"found {occurrences} references"
        )

    def test_no_relative_paths_are_assumed(self):
        """A one-liner has no cwd of its own, so relative paths must not appear."""
        text = _ps1()
        assert ".." not in text.replace("...", ""), (
            "bootstrap.ps1 must not walk relative paths; it runs from a string"
        )

    def test_ascii_staging_falls_back_to_public(self):
        text = _ps1()
        assert "PUBLIC" in text, (
            "the staged bootstrap.cmd must fall back to %PUBLIC% when %TEMP% is "
            "not ASCII, matching the toolchain root rule"
        )


# ---------------------------------------------------------------------------
# Failure handling: never close the caller's shell
# ---------------------------------------------------------------------------

def _code_lines() -> list[str]:
    """bootstrap.ps1 lines with the help block and comments removed.

    Only real code can close a shell; a comment that mentions `exit` cannot.
    """
    text = re.sub(r"(?s)<#.*?#>", "", _ps1())
    return [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _without_string_literals(line: str) -> str:
    """Drop double-quoted substrings, so 'exit code' in a message is not a match."""
    return re.sub(r'"[^"]*"', '""', line)


class TestFailureHandling:
    def test_exit_is_guarded_by_file_mode(self):
        """`exit` under iex would terminate the user's session."""
        text = _ps1()
        assert "$runAsFile" in text, "file mode must be detected"
        guarded = 0
        for line in _code_lines():
            if re.search(r"\bexit\b", _without_string_literals(line)):
                guarded += 1
                assert "$runAsFile" in line, (
                    f"unguarded exit: '{line.strip()}'. An inline run must throw "
                    "instead, because exit would close the caller's shell."
                )
        assert guarded, "expected at least one guarded exit for file mode"


# ---------------------------------------------------------------------------
# Docs and dead references
# ---------------------------------------------------------------------------

class TestReferences:
    def test_entry_points_exist(self):
        for path in (CMD, PS1):
            assert os.path.isfile(path), f"documented entry point missing: {path}"

    def test_readme_names_both_entry_points(self):
        readme = _read(os.path.join(REPO_ROOT, "README.md"))
        assert "bootstrap.ps1" in readme, "README must name the PowerShell entry point"
        assert "bootstrap.cmd" in readme

    def test_setup_prompt_names_the_powershell_entry_point(self):
        setup = _read(os.path.join(REPO_ROOT, "setup.md"))
        assert "bootstrap.ps1" in setup, (
            "setup.md Step 0 should offer the one-line PowerShell entry point"
        )

    def test_no_live_reference_to_the_removed_powershell_implementation(self):
        """Plans may keep it as history; source, docs and tests must not."""
        skip_dirs = {".git", "plans", ".pytest_cache", "__pycache__", "node_modules"}
        # This file names the dead paths as data, so it is not a reference.
        skip_files = {os.path.abspath(__file__)}
        offenders: list[str] = []
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for name in files:
                path = os.path.join(root, name)
                if os.path.abspath(path) in skip_files:
                    continue
                if os.path.getsize(path) > 2_000_000:
                    continue
                try:
                    text = _read(path)
                except (UnicodeDecodeError, OSError):
                    continue
                for dead in DEAD_PATHS:
                    if dead in text:
                        offenders.append(f"{os.path.relpath(path, REPO_ROOT)} -> {dead}")
        assert not offenders, (
            "live files still reference the removed PowerShell implementation: "
            + "; ".join(offenders)
        )

"""Repo-level invariants for ``skills/``.

These exist because of a real bug: ``zoombie-summarize`` was referenced by
`README.md`, `setup.md` and the hand-off step of three SKILL.md files, but the
skill itself had never been created in any commit. Nothing caught it, because no
test cross-checked the skill inventory against the CLI and against the docs.

The lesson is that a skill file is not standalone: it names other skills and it
invokes CLI subcommands. Both are references that can dangle, so both are
checked here.
"""

from __future__ import annotations

import os
import re

import pytest

from zoombie import SKILL_VERSION
from zoombie.cli import build_parser
from zoombie.lib import skills as skills_mod

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(REPO_ROOT, "skills")

# Every skill handed off to, excluding the five whose absence is the point of
# this file. Kept explicit so a rename is a deliberate edit here too.
EXPECTED_SKILLS = {
    "zoombie-download-video",
    "zoombie-extract-audio",
    "zoombie-transcribe-audio",
    "zoombie-transcribe-video",
    "zoombie-pdf-to-md",
    "zoombie-summarize",
}


def _skill_paths() -> list[str]:
    return [
        os.path.join(SKILLS_DIR, entry.name, "SKILL.md")
        for entry in sorted(os.scandir(SKILLS_DIR), key=lambda e: e.name)
        if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "SKILL.md"))
    ]


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _subcommands() -> dict[str, set[str]]:
    """Map each CLI subcommand to the option strings it accepts."""
    parser = build_parser()
    result: dict[str, set[str]] = {}
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public API
        if not hasattr(action, "choices") or not action.choices:
            continue
        for name, sub in action.choices.items():
            options: set[str] = set()
            for sub_action in sub._actions:  # noqa: SLF001
                options.update(sub_action.option_strings)
            result[name] = options
    return result


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class TestInventory:
    def test_every_expected_skill_exists(self):
        found = {os.path.basename(os.path.dirname(p)) for p in _skill_paths()}
        missing = EXPECTED_SKILLS - found
        assert not missing, (
            f"skills referenced across the repo but absent: {sorted(missing)}. "
            "A hand-off to a skill that does not exist is a dead end."
        )

    def test_no_unexpected_skill_appears(self):
        found = {os.path.basename(os.path.dirname(p)) for p in _skill_paths()}
        extra = found - EXPECTED_SKILLS
        assert not extra, (
            f"new skills not listed in EXPECTED_SKILLS: {sorted(extra)}. "
            "Add them here so the hand-off cross-check covers them."
        )

    def test_name_matches_the_directory(self):
        for path in _skill_paths():
            directory = os.path.basename(os.path.dirname(path))
            match = re.search(r"(?m)^name:\s*(\S+)\s*$", _read(path))
            assert match, f"{directory}: no 'name:' in the front matter"
            assert match.group(1) == directory, (
                f"{directory}: 'name: {match.group(1)}' must match the directory"
            )

    def test_every_skill_is_namespaced(self):
        for path in _skill_paths():
            assert os.path.basename(os.path.dirname(path)).startswith("zoombie-")


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

class TestVersionMarkers:
    def test_every_skill_carries_a_marker_in_the_front_matter(self):
        for path in _skill_paths():
            marker = skills_mod.read_marker(path)
            assert marker.owned, (
                f"{path}: no {skills_mod.MARKER_KEY} within the first 12 lines, so "
                "deployment would read this skill as unowned and never update it"
            )

    def test_every_marker_matches_the_constant(self):
        """A drifted marker reports 'updated' on every setup, forever."""
        for path in _skill_paths():
            marker = skills_mod.read_marker(path)
            assert marker.version == SKILL_VERSION, (
                f"{path}: marker {marker.version} != SKILL_VERSION {SKILL_VERSION}"
            )


# ---------------------------------------------------------------------------
# Cross-references - the checks that would have caught the missing skill
# ---------------------------------------------------------------------------

class TestCrossReferences:
    def test_every_skill_handed_off_to_exists(self):
        """Any `zoombie-<name>` a skill tells the agent to hand off to must exist."""
        known = {os.path.basename(os.path.dirname(p)) for p in _skill_paths()}
        pattern = re.compile(r"`(zoombie-[a-z0-9-]+)`")
        for path in _skill_paths():
            for referenced in pattern.findall(_read(path)):
                assert referenced in known, (
                    f"{os.path.basename(os.path.dirname(path))} hands off to "
                    f"'{referenced}', which does not exist under skills/"
                )

    def test_every_referenced_cli_subcommand_exists(self):
        """A skill that calls a subcommand the CLI does not have is a dead end."""
        subcommands = _subcommands()
        pattern = re.compile(r"(?:\$cli|-m zoombie)\s+([a-z]+)")
        for path in _skill_paths():
            for name in pattern.findall(_read(path)):
                assert name in subcommands, (
                    f"{os.path.basename(os.path.dirname(path))} calls "
                    f"'zoombie {name}', which is not a subcommand"
                )

    def test_every_referenced_cli_flag_exists(self):
        """Catch a flag that was renamed on the CLI but not in the skill."""
        subcommands = _subcommands()
        call = re.compile(r"(?:\$cli|-m zoombie)\s+([a-z]+)([^\r\n]*)")
        for path in _skill_paths():
            skill = os.path.basename(os.path.dirname(path))
            for name, tail in call.findall(_read(path)):
                if name not in subcommands:
                    continue  # already reported by the subcommand test
                for flag in re.findall(r"(?<![\w-])(-[A-Za-z][A-Za-z0-9-]*)", tail):
                    # A capitalised PowerShell-style flag is the contract; the
                    # lowercase alias is accepted by argparse too.
                    assert flag in subcommands[name], (
                        f"{skill}: 'zoombie {name} {flag}' is not a known option "
                        f"of that subcommand"
                    )

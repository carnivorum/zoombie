"""Repo-level invariants for ``skills/``.

These exist because of a real bug: ``zoombie-summarize`` was referenced by
`README.md`, `setup.md` and the hand-off step of three SKILL.md files, but the
skill itself had never been created in any commit. Nothing caught it, because no
test cross-checked the skill inventory against the CLI and against the docs.

The lesson is that a skill file is not standalone: it names other skills and it
invokes CLI subcommands. Both are references that can dangle, so both are
checked here.

A second class of guard keeps the token cost down: the shared blocks and the
per-skill budget are asserted, so the next edit that re-pastes boilerplate fails
the suite instead of being paid for by every agent that loads a skill.
"""

from __future__ import annotations

import os
import re

import pytest

from zoombie import SKILL_VERSION
from zoombie.cli import build_parser
from zoombie.lib import skills as skills_mod
from zoombie.lib.errors import ZoombieError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(REPO_ROOT, "skills")

# Exactly ONE skill. The download/extract/transcribe/readpdf/readimages stages
# survive as MCP-only tools, and the summarize flow drives them; there is no
# second skill to hand off to. Kept explicit so re-adding one is a deliberate edit.
EXPECTED_SKILLS = {
    "zoombie-summarize",
}


# The largest acceptable skill source. Exceeding it means boilerplate crept back
# in or the skill is doing two jobs; split it or move the shared text into
# skills/_shared/ rather than raise the bound.
#
# The current maximum is zoombie-summarize, by design: it owns the 6-block
# contract, which no other skill should carry. The budget sits just above it, so
# another skill growing to this size - or any skill re-pasting a shared block,
# which costs roughly 1.5 KB - fails rather than being paid for by every agent
# that loads it.
# Raised from 11000 when the scratch-note block was shared: every skill now carries
# its include marker, which is a few hundred bytes, and zoombie-summarize sits just
# under the bound. The guard still catches a re-pasted BLOCK (roughly 1.5 KB) or a
# skill that took on a second job.
# A REPO-LOCAL heuristic, NOT a client limit: the extension imposes no file-size
# cap on SKILL.md (it reads the whole file and only checks name/description), so
# this bound exists purely for token economy -- a loaded skill is injected into
# the agent's context, so its size is its cost. It is kept tight enough that a
# re-pasted shared BLOCK (roughly 1.5 KB) still trips it.
# Raised from 11500 when the shared cli-resolve block gained the MCP-first note
# (every skill expands it, so the bound moves with the shared text).
# Raised from 12000 when zoombie-summarize became the FRONT DOOR: it now also
# produces its own source material (video/audio/PDF/images) rather than only
# consuming it -- a deliberate scope addition, not re-pasted boilerplate.
MAX_SKILL_BYTES = 13000

# A GENUINE CLIENT LIMIT, and a silent one. The Zoo Code extension validates the
# skill description as 1..1024 characters and, on a violation, logs to the console
# and RETURNS -- the skill is simply not loaded, with no visible error in the UI.
# Verified in the installed bundle (zoocodeorganization.zoo-code 3.82.2):
#   `f.length<1||f.length>1024` -> "invalid description length" -> return.
# Every description must stay comfortably inside it.
MAX_DESCRIPTION_CHARS = 1024

# The canonical include blocks. Kept explicit so a rename is a deliberate edit
# here, and so a deleted block is a failure rather than a silently missing include.
EXPECTED_INCLUDES = {
    "cli-resolve", "json-contract", "repo-fallback", "shell-note", "scratch-note",
    "frame-selection",
}

_OPEN_RE = re.compile(r"<!--\s*zoombie:include\s+([A-Za-z0-9._-]+)\s*-->")
_CLOSE_RE = re.compile(r"<!--\s*/zoombie:include\s*-->")


def _skill_paths() -> list[str]:
    return [
        os.path.join(SKILLS_DIR, entry.name, "SKILL.md")
        for entry in sorted(os.scandir(SKILLS_DIR), key=lambda e: e.name)
        if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "SKILL.md"))
    ]


def _shared_dir() -> str:
    return skills_mod.shared_dir(SKILLS_DIR)


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

    def test_description_is_a_valid_unquoted_yaml_scalar(self):
        """The front matter must PARSE, or the client silently drops the skill.

        This is the guard for a real outage: four skills stopped loading because a
        description contained ``: `` (colon-space), which is invalid inside a plain
        (unquoted) YAML scalar. The client's front-matter parser threw, its catch
        logged to the console, and the skill was simply never registered -- the
        suite stayed green because nothing parsed the front matter the way the
        client does.

        The client reads the description as a plain scalar, so two sequences are
        forbidden in it: ``: `` (a mapping indicator) and `` #`` (a comment
        indicator). A description needing either must be quoted instead.
        """
        for path in _skill_paths():
            skill = os.path.basename(os.path.dirname(path))
            match = re.search(r"(?m)^description:\s*(.+)$", _read(path))
            assert match, f"{skill}: no 'description:' in the front matter"
            description = match.group(1).strip()
            # A quoted scalar is always safe; only a plain one is constrained.
            if description[:1] in ("'", '"'):
                continue
            assert ": " not in description and not description.endswith(":"), (
                f"{skill}: the description contains ': ' (colon-space), which is "
                "invalid in an unquoted YAML scalar -- the client would fail to "
                "parse the front matter and DROP this skill. Rephrase, or quote it."
            )
            assert " #" not in description, (
                f"{skill}: the description contains ' #', which starts a YAML "
                "comment -- the client would truncate or drop it."
            )

    def test_every_description_fits_the_client_limit(self):
        """The extension silently drops a skill whose description exceeds 1024 chars.

        Verified in the installed bundle: the validator logs "invalid description
        length" and returns WITHOUT registering the skill, so an over-long
        description is not a warning but a skill that never appears. This is the
        real, client-enforced bound -- distinct from the repo-local byte budget.
        """
        for path in _skill_paths():
            skill = os.path.basename(os.path.dirname(path))
            match = re.search(r"(?m)^description:\s*(.+)$", _read(path))
            assert match, f"{skill}: no 'description:' in the front matter"
            length = len(match.group(1).strip())
            assert length <= MAX_DESCRIPTION_CHARS, (
                f"{skill}: description is {length} chars, over the client's "
                f"{MAX_DESCRIPTION_CHARS}-char limit -- the extension would REFUSE "
                "to load this skill entirely"
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


# ---------------------------------------------------------------------------
# Shared blocks - token economy
# ---------------------------------------------------------------------------

class TestSharedIncludes:
    """The repo shares boilerplate; the deployed skill must not carry a marker.

    These are the guards that keep the de-duplication from either breaking the
    deploy (an unknown name) or quietly undoing itself (a re-pasted block).
    """

    def test_every_expected_block_exists(self):
        for name in EXPECTED_INCLUDES:
            path = os.path.join(_shared_dir(), f"{name}.md")
            assert os.path.isfile(path), f"missing shared block: {path}"

    def test_shared_folder_is_not_a_skill(self):
        """A folder with no SKILL.md is skipped by both inventory and deploy."""
        assert not os.path.isfile(os.path.join(_shared_dir(), "SKILL.md"))
        found = {os.path.basename(os.path.dirname(p)) for p in _skill_paths()}
        assert skills_mod.SHARED_FOLDER not in found

    def test_every_used_include_exists(self):
        for path in _skill_paths():
            for name in _OPEN_RE.findall(_read(path)):
                assert name in EXPECTED_INCLUDES, (
                    f"{os.path.basename(os.path.dirname(path))} includes "
                    f"'{name}', which is not a known block"
                )

    def test_markers_are_balanced(self):
        for path in _skill_paths():
            text = _read(path)
            opens = len(_OPEN_RE.findall(text))
            closes = len(_CLOSE_RE.findall(text))
            assert opens == closes, (
                f"{os.path.basename(os.path.dirname(path))}: {opens} open vs "
                f"{closes} close marker(s)"
            )

    def test_each_include_appears_at_most_once(self):
        for path in _skill_paths():
            names = _OPEN_RE.findall(_read(path))
            duplicates = {n for n in names if names.count(n) > 1}
            assert not duplicates, (
                f"{os.path.basename(os.path.dirname(path))}: duplicated include "
                f"marker(s) {sorted(duplicates)}"
            )

    def test_no_shared_body_is_re_pasted(self):
        """The check that catches a block pasted back in instead of included."""
        for name in EXPECTED_INCLUDES:
            body = _read(os.path.join(_shared_dir(), f"{name}.md")).strip()
            # A short body would make this check meaningless.
            assert len(body.splitlines()) >= 2, f"{name} block is too small"
            for path in _skill_paths():
                assert body not in _read(path), (
                    f"{os.path.basename(os.path.dirname(path))} contains the "
                    f"'{name}' block verbatim; use the include marker instead"
                )

    def test_expansion_removes_every_marker(self):
        for path in _skill_paths():
            expanded = skills_mod.expand_includes(_read(path), _shared_dir())
            assert "zoombie:include" not in expanded, (
                f"{os.path.basename(os.path.dirname(path))}: a marker survived "
                "expansion, so the deployed skill would carry it"
            )

    def test_expansion_inlines_the_body(self):
        for path in _skill_paths():
            expanded = skills_mod.expand_includes(_read(path), _shared_dir())
            for name in _OPEN_RE.findall(_read(path)):
                body = _read(os.path.join(_shared_dir(), f"{name}.md")).strip()
                assert body in expanded

    def test_expansion_is_idempotent(self):
        for path in _skill_paths():
            once = skills_mod.expand_includes(_read(path), _shared_dir())
            twice = skills_mod.expand_includes(once, _shared_dir())
            assert once == twice

    def test_unknown_include_is_a_failure(self):
        with pytest.raises(ZoombieError):
            skills_mod.expand_includes(
                "<!-- zoombie:include not-a-block -->\n<!-- /zoombie:include -->\n",
                _shared_dir(),
            )

    def test_unclosed_include_is_a_failure(self):
        with pytest.raises(ZoombieError):
            skills_mod.expand_includes(
                "<!-- zoombie:include cli-resolve -->\ntext with no close\n",
                _shared_dir(),
            )


class TestPrune:
    """A rename must not leave two skills advertising the same job."""

    def _deploy(self, root, name, version=SKILL_VERSION, owned=True):
        directory = root / name
        directory.mkdir()
        marker = f"{skills_mod.MARKER_KEY}: {version}\n" if owned else ""
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\n{marker}---\nbody\n", encoding="utf-8"
        )
        return directory

    def test_a_stale_owned_zoombie_skill_is_removed(self, tmp_path):
        stale = self._deploy(tmp_path, "zoombie-pdf-to-md")
        removed = skills_mod.prune(str(tmp_path), {"zoombie-images-to-md"})
        assert [entry["skill"] for entry in removed] == ["zoombie-pdf-to-md"]
        assert not stale.exists()

    def test_a_kept_skill_is_left_alone(self, tmp_path):
        kept = self._deploy(tmp_path, "zoombie-summarize")
        skills_mod.prune(str(tmp_path), {"zoombie-summarize"})
        assert kept.exists()

    def test_a_foreign_folder_is_never_removed(self, tmp_path):
        # Starts with 'zoombie-' but carries no marker: not ours.
        foreign = self._deploy(tmp_path, "zoombie-custom", owned=False)
        assert skills_mod.prune(str(tmp_path), set()) == []
        assert foreign.exists()

    def test_a_non_zoombie_folder_is_never_removed(self, tmp_path):
        other = self._deploy(tmp_path, "someone-else")
        assert skills_mod.prune(str(tmp_path), set()) == []
        assert other.exists()

    def test_a_missing_root_is_not_an_error(self, tmp_path):
        assert skills_mod.prune(str(tmp_path / "absent"), set()) == []


class TestMcpFirst:
    """The shared transport block must make MCP the first choice, CLI the fallback.

    Every skill includes this block, so the ordering here is the policy: an agent
    that reads it should reach for the MCP tools and only fall back to the launcher.
    """

    def _block(self) -> str:
        return _read(os.path.join(_shared_dir(), "cli-resolve.md"))

    def test_every_skill_includes_the_transport_block(self):
        for path in _skill_paths():
            assert "zoombie:include cli-resolve" in _read(path), (
                f"{os.path.basename(os.path.dirname(path))} does not include the "
                "transport-resolution block"
            )

    def test_the_block_recommends_mcp_before_the_cli(self):
        body = self._block()
        assert "MCP" in body, "the transport block must mention MCP"
        # The MCP recommendation must come BEFORE the CLI resolution snippet.
        assert body.index("MCP") < body.index("zoombie.cmd"), (
            "MCP must be presented first, with the CLI as the fallback"
        )

    def test_the_block_names_the_cli_as_the_fallback(self):
        assert "fallback" in self._block().lower()

    def test_the_block_does_not_teach_flag_translation(self):
        """The MCP schemas carry the argument names; the block must not re-teach it.

        The removed sentence mapped ``-Source -> source`` etc. Keeping it invited
        an agent to double-guess the tool schema, so its absence is asserted.
        """
        body = self._block()
        assert "->" not in body, "the shared block must not translate flags"
        assert "flags as arguments" not in body.lower()
        for flag in ("`-Source`", "`-Output`", "`-DownloadDir`", "`-AudioOnly`"):
            assert flag not in body, (
                f"the shared block still names the CLI flag {flag}; the MCP "
                "argument name is the transport-facing spelling"
            )

    def test_the_expanded_skill_carries_the_mcp_first_rule(self):
        """What is DEPLOYED must carry the rule, not only the repo source."""
        for path in _skill_paths():
            expanded = skills_mod.expand_includes(_read(path), _shared_dir())
            assert "MCP" in expanded, (
                f"{os.path.basename(os.path.dirname(path))} does not reach the MCP "
                "tools after expansion"
            )

    def test_no_skill_orders_the_agent_to_use_the_shell(self):
        """No skill may shout 'Run this, nothing else' over a SHELL snippet.

        A heading that commands a shell run, or a line calling the CLI "the only
        command", contradicts the MCP-first rule in the same file. Each skill's run
        section must name the MCP tool first.
        """
        banned = (
            "Run this, nothing else",
            "the only command this skill needs",
            "these are the only commands this skill needs",
        )
        for path in _skill_paths():
            skill = os.path.basename(os.path.dirname(path))
            body = _read(path)
            for phrase in banned:
                assert phrase not in body, (
                    f"{skill} still contains '{phrase}', which presents the shell as "
                    "the primary transport"
                )

    def _run_section(self, path: str) -> str:
        body = _read(path)
        assert "## Run this" in body, f"{os.path.dirname(path)} has no run section"
        run = body[body.index("## Run this"):]
        return run[:run.index("\n## ") if "\n## " in run else len(run)]

    def test_the_run_section_names_the_mcp_tool_first(self):
        for path in _skill_paths():
            skill = os.path.basename(os.path.dirname(path))
            run = self._run_section(path)
            assert "MCP" in run, (
                f"{skill}: the run section must present the MCP tool, not only the CLI"
            )
            assert "fallback" in run.lower(), (
                f"{skill}: the run section must label the CLI as the fallback"
            )

    def test_the_run_section_shows_an_mcp_json_example(self):
        """Naming MCP is not enough: the run section must SHOW a JSON tool call.

        An example is what an agent copies. A prose mention ("prefer the MCP
        tool") without one leaves the CLI snippet as the only runnable thing,
        which is exactly the drift this guard exists to stop.
        """
        for path in _skill_paths():
            skill = os.path.basename(os.path.dirname(path))
            run = self._run_section(path)
            assert "```json" in run, (
                f"{skill}: the run section must carry a ```json MCP tool example, "
                "not only a CLI snippet"
            )
            # The MCP example precedes the CLI fallback line, so the JSON block is
            # the first code fence an agent meets.
            assert run.index("```json") < run.lower().index("cli fallback"), (
                f"{skill}: the MCP JSON example must come before the CLI fallback line"
            )


class TestPickerRouting:
    """The one skill must route a request and describe the step flow.

    With a single skill there is no hand-off to mis-route, so the property under
    test is narrower: the description must admit a raw source (video, audio, PDF,
    images) and the body must state the step machine and the overwrite safety.
    """

    def _description(self, name: str) -> str:
        path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        match = re.search(r"(?m)^description:\s*(.+?)(?=\n[a-z_]+:|\n---)", _read(path), re.S)
        assert match, f"{name} has no description in the front matter"
        return " ".join(match.group(1).split())

    def test_summarize_is_the_front_door_for_a_raw_source(self):
        description = self._description("zoombie-summarize")
        assert "FRONT DOOR" in description
        for source in ("video", "audio", "PDF", "image"):
            assert source.lower() in description.lower(), (
                f"summarize's description must admit a raw {source} source"
            )

    def test_the_body_names_the_step_flow(self):
        body = _read(os.path.join(SKILLS_DIR, "zoombie-summarize", "SKILL.md")).lower()
        for step in ("source", "name", "slides", "prose", "verify"):
            assert step in body, f"the summarize step {step!r} must be documented"

    def test_the_body_states_the_overwrite_safety(self):
        body = _read(os.path.join(SKILLS_DIR, "zoombie-summarize", "SKILL.md"))
        assert "archived" in body.lower(), (
            "the skill must tell the agent an existing summary is archived, not destroyed"
        )
        # The flow is now: the step REFUSES, the agent ASKS, the user chooses. The
        # skill must name both answers and the invariant that the media survives.
        for token in ("archive", "overwrite", "media is never removed"):
            assert token in body.lower(), (
                f"the skill must document {token!r} for an existing summary"
            )

    def test_the_prune_example_carries_slides_true(self):
        """The documented prune must not reproduce the silent no-op.

        ``-Drop`` without ``-Slides`` was parsed and discarded, so a skill that shows
        the prune without the flag teaches the agent the exact broken call.
        """
        body = _read(os.path.join(SKILLS_DIR, "zoombie-summarize", "SKILL.md"))
        assert '"slides": "true"' in body or "slides:\"true\"" in body, (
            "the prune example must carry slides:true"
        )
        assert "selection.applied" in body, (
            "the skill must tell the agent to verify the selection was applied"
        )

    def test_the_body_describes_the_visible_img_folder(self):
        body = _read(os.path.join(SKILLS_DIR, "zoombie-summarize", "SKILL.md"))
        assert "img/" in body
        assert "no `.data`" in body.lower() or "no `.data/`" in body.lower() or "throwaway" in body.lower()


class TestSkillBudget:
    """A per-skill byte budget, so a re-inflated skill fails the suite."""

    def test_no_skill_exceeds_the_budget(self):
        for path in _skill_paths():
            size = os.path.getsize(path)
            assert size <= MAX_SKILL_BYTES, (
                f"{os.path.basename(os.path.dirname(path))} is {size} bytes "
                f"(budget {MAX_SKILL_BYTES}). Move shared text into "
                f"skills/_shared/ or split the skill."
            )

"""The Zoombie role deployment: merge safety, idempotency and the write boundary.

The property under test is that a user-owned ``custom_modes.yaml`` is never
clobbered. Most of these cases exist to prove that some *foreign* content survives
a deploy, because that is the failure that would be expensive and silent.
"""

from __future__ import annotations

import os
import re

import pytest

from zoombie.lib import modes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FOREIGN = """\
customModes:
  - slug: my-own-mode
    name: My Own Mode
    roleDefinition: |
      You are a hand-written mode. Do not delete me.
    groups:
      - read
"""

OUR_ITEMS = """\
  - slug: zoombie
    name: Zoombie
    description: Universal cross-domain analyst and reasoning partner.
    roleDefinition: |
      You are Zoombie, a universal analyst.
    whenToUse: |
      Use this mode to understand something.
    groups:
      - read
      - command
      - modes
      - - edit
        - fileRegex: \\.(md|markdown|txt|csv|tsv|html|htm)$
          description: Analytical artifacts only
"""


@pytest.fixture
def target(tmp_path) -> str:
    return str(tmp_path / "custom_modes.yaml")


@pytest.fixture
def source_file(tmp_path) -> str:
    path = tmp_path / "zoombie.yaml"
    path.write_text("customModes:\n" + OUR_ITEMS, encoding="utf-8", newline="")
    return str(path)


# ---------------------------------------------------------------------------
# read_source
# ---------------------------------------------------------------------------

class TestReadSource:
    def test_extracts_the_item_text_verbatim(self, source_file):
        source = modes.read_source(source_file)
        assert source.slugs == ["zoombie"]
        assert "- slug: zoombie" in source.items_text
        # The root key is not part of the item text: it is re-added by the merge.
        assert "customModes:" not in source.items_text

    def test_comments_above_the_key_are_not_treated_as_items(self, source_file):
        with open(source_file, "r", encoding="utf-8") as handle:
            text = handle.read()
        path = source_file
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("# a leading comment\n#   - slug: not-a-mode\n" + text)
        source = modes.read_source(path)
        assert source.slugs == ["zoombie"]

    def test_empty_source_is_refused(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        with pytest.raises(modes.ZoombieError):
            modes.read_source(str(path))

    def test_missing_root_key_is_refused(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("modes:\n  - slug: x\n", encoding="utf-8")
        with pytest.raises(modes.ZoombieError):
            modes.read_source(str(path))


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

class TestMerge:
    def test_creates_the_file_when_it_is_missing(self):
        result = modes.merge("", OUR_ITEMS, "zoombie")
        assert result.action == "created"
        assert result.text.startswith("customModes:\n")
        assert "- slug: zoombie" in result.text

    def test_created_into_an_empty_list(self):
        result = modes.merge("customModes: []\n", OUR_ITEMS, "zoombie")
        assert result.action == "created"
        assert "customModes:\n" in result.text
        assert "[]" not in result.text
        assert "- slug: zoombie" in result.text

    def test_preserves_a_foreign_mode_exactly(self):
        result = modes.merge(FOREIGN, OUR_ITEMS, "zoombie")
        assert result.action == "created"
        # Every foreign line, in order, byte-for-byte.
        for line in FOREIGN.splitlines():
            assert line in result.text.splitlines()
        assert result.text.index("- slug: my-own-mode") < result.text.index("- slug: zoombie")

    def test_replaces_only_our_entry_on_update(self):
        first = modes.merge(FOREIGN, OUR_ITEMS, "zoombie")
        changed = OUR_ITEMS.replace("universal analyst", "a changed analyst")
        second = modes.merge(first.text, changed, "zoombie")
        assert second.action == "updated"
        assert "a changed analyst" in second.text
        # The foreign mode is still intact and still first.
        assert "- slug: my-own-mode" in second.text

    def test_second_identical_merge_is_up_to_date(self):
        first = modes.merge(FOREIGN, OUR_ITEMS, "zoombie")
        second = modes.merge(first.text, OUR_ITEMS, "zoombie")
        assert second.action == "up to date"
        # Crucial: no write is needed, so the caller can skip the write entirely.
        assert second.text == first.text

    def test_ignores_a_slug_inside_a_block_scalar(self):
        """A foreign roleDefinition mentioning our slug must not fool the matcher."""
        hostile = (
            "customModes:\n"
            "  - slug: sneaky\n"
            "    name: Sneaky\n"
            "    roleDefinition: |\n"
            "      Some prose.\n"
            "        - slug: zoombie\n"
            "      More prose.\n"
            "    groups:\n"
            "      - read\n"
        )
        result = modes.merge(hostile, OUR_ITEMS, "zoombie")
        # The sneaky block was NOT matched, so this is an append, not a replace.
        assert result.action == "created"
        assert "- slug: sneaky" in result.text
        assert "Do not delete me" not in result.text
        # Exactly two real list items: the foreign one and ours. The text inside
        # the scalar also reads "- slug: zoombie", which is precisely the decoy;
        # counting item-shaped lines AT THE LIST INDENT excludes it.
        item_lines = [
            line for line in result.text.splitlines()
            if re.match(r"^  - slug: ", line)
        ]
        assert len(item_lines) == 2
        assert "- slug: sneaky" in item_lines[0]
        assert "- slug: zoombie" in item_lines[1]

    def test_malformed_file_is_refused_not_repaired(self):
        with pytest.raises(modes.ZoombieError):
            modes.merge("modes:\n  - slug: x\n", OUR_ITEMS, "zoombie")

    def test_crlf_input_is_normalised_without_losing_content(self):
        result = modes.merge(FOREIGN.replace("\n", "\r\n"), OUR_ITEMS, "zoombie")
        assert "\r" not in result.text
        assert "- slug: my-own-mode" in result.text


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------

class TestDeploy:
    def test_deploy_writes_and_reports_created(self, source_file, target):
        record = modes.deploy(source_file, target)
        assert record["action"] == "created"
        assert record["slug"] == "zoombie"
        assert os.path.isfile(target)

    def test_deploy_is_idempotent(self, source_file, target):
        modes.deploy(source_file, target)
        before = open(target, "r", encoding="utf-8").read()
        record = modes.deploy(source_file, target)
        after = open(target, "r", encoding="utf-8").read()
        assert record["action"] == "up to date"
        assert before == after

    def test_dry_run_writes_nothing(self, source_file, target):
        record = modes.deploy(source_file, target, dry_run=True)
        assert record["action"] == "created"
        assert not os.path.exists(target)

    def test_force_rewrites_even_when_up_to_date(self, source_file, target):
        modes.deploy(source_file, target)
        record = modes.deploy(source_file, target, force=True)
        assert record["action"] == "up to date"
        assert os.path.isfile(target)

    def test_foreign_mode_survives_a_real_deploy(self, source_file, target):
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(FOREIGN)
        modes.deploy(source_file, target)
        text = open(target, "r", encoding="utf-8").read()
        assert "You are a hand-written mode. Do not delete me." in text
        assert "- slug: zoombie" in text


# ---------------------------------------------------------------------------
# The write boundary
# ---------------------------------------------------------------------------

class TestWriteBoundary:
    """The edit restriction is the security story; test it, not just the prose."""

    @pytest.fixture
    def file_regex(self, source_file) -> str:
        source = modes.read_source(source_file)
        match = re.search(r"fileRegex:\s*(\S+)", source.items_text)
        assert match, "the edit group must carry a fileRegex"
        return match.group(1)

    @pytest.mark.parametrize(
        "name",
        ["report.md", "notes.markdown", "brief.txt", "data.csv", "data.tsv",
         "report.html", "report.htm"],
    )
    def test_allows_human_readable_artifacts(self, file_regex, name):
        assert re.search(file_regex, name), f"{name} should be writable"

    @pytest.mark.parametrize(
        "name",
        ["script.py", "app.js", "config.json", "settings.yaml", "style.css",
         "notebook.ipynb", "run.cmd", "data.xml", "doc.docx", "sheet.xlsx"],
    )
    def test_refuses_code_and_config(self, file_regex, name):
        assert not re.search(file_regex, name), f"{name} must NOT be writable"

    def test_end_anchor_blocks_a_double_extension(self, file_regex):
        """`report.md.exe` would pass a naive substring match; the anchor stops it."""
        assert not re.search(file_regex, "report.md.exe")

    def test_the_generated_entry_has_only_schema_fields(self, source_file):
        """An unknown key would fail the extension's schema validation."""
        allowed = {"slug", "name", "description", "roleDefinition", "whenToUse",
                   "customInstructions", "groups"}
        source = modes.read_source(source_file)
        keys = re.findall(r"^\s{4}([a-zA-Z]+):", source.items_text, re.MULTILINE)
        # `fileRegex`/`description` inside the group entry are indented deeper, so
        # this top-level scan only sees the mode's own keys.
        assert set(keys) <= allowed, f"unexpected keys: {set(keys) - allowed}"

    def test_the_groups_are_least_privilege(self, source_file):
        source = modes.read_source(source_file)
        assert "      - read" in source.items_text
        assert "      - command" in source.items_text
        assert "      - modes" in source.items_text
        # edit must be the STRUCTURED form, never an unrestricted string.
        assert "\n      - edit\n" not in source.items_text


# ---------------------------------------------------------------------------
# The behavioural contract
# ---------------------------------------------------------------------------

class TestRoleBehaviour:
    """These assert the role's stance, not its formatting.

    Losing one of these clauses would silently change how the role reasons, and
    nothing else in the suite would notice.
    """

    @pytest.fixture
    def items(self) -> str:
        here = os.path.dirname(os.path.abspath(__file__))
        repo = os.path.dirname(here)
        source = modes.read_source(os.path.join(repo, "modes", "zoombie.yaml"))
        return source.items_text

    def test_does_not_take_input_for_granted(self, items):
        # Every input class must be named, not just "the user's premise":
        # a premise, a supplied file/document, a fetched source, and its own
        # earlier output are different things that fail in different ways.
        lowered = items.lower()
        assert "scrutinise the input" in lowered
        assert "not the user's premise" in lowered
        assert "not a document or file" in lowered
        assert "not a source you fetched" in lowered
        assert "not your own earlier answer" in lowered
        # An unrepresentative or stale source is the quiet failure mode.
        assert "representative" in lowered
        assert "stale" in lowered

    def test_criticises_without_being_contrarian(self, items):
        lowered = items.lower()
        assert "criticism, not contrarianism" in lowered
        assert "manufacture objections" in lowered
        assert "push back when the evidence warrants it" in lowered
        # Being right quietly must be allowed, or the role becomes noise.
        assert "agree when it does not" in lowered

    def test_criticism_is_stated_once_and_unpadded(self, items):
        """Stated in one place, and explicitly not padded.

        The stance appears in roleDefinition; the skeleton step in
        customInstructions must not restate it at length, which is what made the
        role prompt expensive without adding a rule.
        """
        lowered = items.lower()
        assert "criticism, not contrarianism" in lowered
        assert "give them once" in lowered
        assert "state each once" in lowered
        assert "never pad either section" in lowered
        # The long-form restatement was removed; a short cross-reference stays.
        assert "one line each and stated once" not in lowered

    def test_criticism_and_uncertainty_stay_distinct(self, items):
        # They answer different questions; collapsing them into one hedge is the
        # failure mode this clause exists to prevent.
        assert "Criticism, Uncertainty" in items
        assert "different questions" in items.lower()

    def test_skeleton_carries_all_six_sections(self, items):
        assert "Answer, Evidence, Reasoning, Criticism, Uncertainty, Next steps" in items

    def test_delegation_brief_is_intact(self, items):
        for field in ("Question:", "Deliverable:", "Inputs:", "Method constraints:",
                      "Evidence discipline:", "Return format:", "Scope guard:"):
            assert field in items, f"the intern brief lost '{field}'"
        assert "Analyst Intern" in items

    def test_points_at_the_ingest_and_summary_skills(self, items):
        """The role must name the skills, not just say 'use the tools'."""
        for name in ("zoombie-transcribe-video", "zoombie-transcribe-audio",
                     "zoombie-download-video", "zoombie-extract-audio",
                     "zoombie-images-to-md", "zoombie-summarize", "zoombie postprocess"):
            assert name in items, f"the role no longer points at {name}"

    def test_separates_the_two_document_shapes(self, items):
        """The analysis skeleton and the library 6-block format must not blend.

        Without this the role will happily pour an analysis into summary.md, or
        a library digest into the analysis skeleton, and both read wrong.
        """
        assert "do not blend them" in items
        assert "6-block" in items
        assert "Answer / Evidence / Reasoning / Criticism / Uncertainty / Next steps" in items

    def test_forbids_hand_editing_cli_managed_parts(self, items):
        assert "never hand-edit what the CLI owns" in items


# ---------------------------------------------------------------------------
# Prompt size - the always-on, per-turn cost
# ---------------------------------------------------------------------------

def _role_items() -> str:
    """The deployed role's item text, read from the canonical source."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    return modes.read_source(os.path.join(repo, "modes", "zoombie.yaml")).items_text


class TestPromptSize:
    """The role prompt is billed on every turn in the mode.

    A budget, not a target: it exists so the next edit that restates what a skill
    already owns fails the suite instead of quietly doubling what every Zoombie
    turn costs. Raise it deliberately, with a reason.
    """

    # Character ceilings for the two fields the model reads on every turn.
    ROLE_DEFINITION_MAX = 4500
    CUSTOM_INSTRUCTIONS_MAX = 5400

    def test_role_definition_stays_within_budget(self):
        items = _role_items()
        match = re.search(
            r"roleDefinition:\s*\|(?P<body>.*?)(?=\n    [a-zA-Z]+:)", items, re.DOTALL
        )
        assert match, "roleDefinition block not found"
        body = match.group("body")
        assert len(body) <= self.ROLE_DEFINITION_MAX, (
            f"roleDefinition is {len(body)} characters "
            f"(budget {self.ROLE_DEFINITION_MAX})"
        )

    def test_custom_instructions_stay_within_budget(self):
        items = _role_items()
        match = re.search(
            r"customInstructions:\s*\|(?P<body>.*?)(?=\n    [a-zA-Z]+:)", items, re.DOTALL
        )
        assert match, "customInstructions block not found"
        body = match.group("body")
        assert len(body) <= self.CUSTOM_INSTRUCTIONS_MAX, (
            f"customInstructions is {len(body)} characters "
            f"(budget {self.CUSTOM_INSTRUCTIONS_MAX})"
        )

    def test_delegates_the_item_model_to_the_skill(self):
        """The item layout and the block-6 rule are owned by zoombie-summarize."""
        items = _role_items().lower()
        assert "zoombie-summarize owns" in items
        # The restated shape must be gone, while the correctness guards remain.
        assert "summary.md sits at the item root" not in items
        assert "nextnumber" in items
        assert "read-only" in items

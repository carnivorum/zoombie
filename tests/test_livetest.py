"""The live-test harness: the hint, the gitignore rules and the runner's paths.

The live test itself is NOT run here -- it transcribes on the GPU and downloads
from the network. What is tested is the machinery AROUND it, which is where the
mistakes live: a hint that fires when it should not, a gitignore rule that lets
the user's own media become committable, or a runner that hardcodes a path and
breaks on the next machine.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

import conftest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITIGNORE = os.path.join(REPO_ROOT, ".gitignore")
LIVETEST_DIR = os.path.join(REPO_ROOT, "tests", "livetest")
RUNNER = os.path.join(LIVETEST_DIR, "run_livetest.py")


def _load_runner():
    """Import ``run_livetest`` by path; it is not on ``sys.path`` by design."""
    spec = importlib.util.spec_from_file_location("livetest_runner", RUNNER)
    assert spec is not None and spec.loader is not None, f"cannot load {RUNNER}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Options:
    def __init__(self, file_or_dir=None, keyword=None):
        self.file_or_dir = file_or_dir or []
        self.keyword = keyword


class _Config:
    def __init__(self, file_or_dir=None, keyword=None):
        self.option = _Options(file_or_dir, keyword)


class _Reporter:
    """Captures what the hook would print."""

    def __init__(self):
        self.lines: list[str] = []

    def write_sep(self, *_args, **_kwargs):
        pass

    def write_line(self, text):
        self.lines.append(text)


class TestTheHint:
    def test_the_hint_names_the_readme_and_the_ask_first_rule(self):
        text = conftest.livetest_hint()
        assert "tests/livetest" in text
        assert conftest.LIVETEST_README in text
        # The rule the user cares about: the AGENT asks, and does not self-start.
        assert "ASK THE USER" in text
        assert "retest" in text

    def test_a_bare_run_is_the_global_run(self):
        assert conftest._is_global_run(_Config()) is True

    def test_a_partial_run_is_quiet(self):
        """The whole point: a retest must not re-nag."""
        assert conftest._is_global_run(_Config(file_or_dir=["tests/test_cli.py"])) is False
        assert conftest._is_global_run(_Config(keyword="summarize")) is False

    def test_the_hook_prints_on_a_clean_global_run(self):
        reporter = _Reporter()
        conftest.pytest_terminal_summary(reporter, 0, _Config())
        assert reporter.lines and "live test available" in reporter.lines[0]

    def test_the_hook_is_silent_when_the_suite_is_red(self):
        reporter = _Reporter()
        conftest.pytest_terminal_summary(reporter, 1, _Config())
        assert reporter.lines == []

    def test_the_hook_is_silent_on_a_partial_run(self):
        reporter = _Reporter()
        conftest.pytest_terminal_summary(reporter, 0, _Config(file_or_dir=["tests"]))
        assert reporter.lines == []

    def test_the_hook_honours_the_suppression_variable(self, monkeypatch):
        monkeypatch.setenv(conftest.NO_HINT_ENV, "1")
        reporter = _Reporter()
        conftest.pytest_terminal_summary(reporter, 0, _Config())
        assert reporter.lines == []


class TestGitignore:
    """The user's media must stay local; the instructions must be committable."""

    @staticmethod
    def _lines() -> list[str]:
        with open(GITIGNORE, encoding="utf-8") as handle:
            return [
                line.strip() for line in handle
                if line.strip() and not line.startswith("#")
            ]

    def test_the_livetest_folder_is_not_blanket_ignored(self):
        lines = self._lines()
        assert "tests/livetest" not in lines, (
            "a bare tests/livetest rule ignores readme.md and the runner too, so the "
            "instructions could never be committed"
        )
        assert not any(line.rstrip("/") == "tests/livetest" for line in lines)

    def test_the_user_media_and_the_harness_stay_local(self):
        lines = self._lines()
        for rule in ("tests/livetest/ws/", "tests/livetest/*.pdf", "tests/livetest/*.mp4"):
            assert rule in lines, f"{rule} must be ignored so it stays on this machine"


class TestTheRunner:
    def test_it_derives_its_paths_from_the_repository(self):
        runner = _load_runner()
        assert runner.HERE == runner.REPO / "tests" / "livetest"
        assert runner.WORKSPACE == runner.HERE / "ws"
        # The outside copies are OFF the repository, so the flow routes them to
        # _unsorted. Deriving both lets the inside/outside split hold on any machine.
        assert runner.REPO not in runner.OUTSIDE.parents

    def test_each_inside_case_gets_its_own_folder(self):
        """A shared folder would make the second case overwrite the first.

        An in-place source IS its item, so two samples in one directory means the
        video item is the folder the PDF run must write into -- and the live run
        showed exactly that, tripping the overwrite gate on a first run.
        """
        runner = _load_runner()
        directories = list(runner.INSIDE_DIRS.values())
        assert len(directories) == 2
        assert len(set(directories)) == 2, "the inside cases must not share a folder"
        for directory in directories:
            assert runner.WORKSPACE in directory.parents

    def test_the_remote_source_is_the_reviewed_url(self):
        assert _load_runner().REMOTE_URL == (
            "https://rutube.ru/video/ee7e9f4af68a1b34df85219d147bfe99/"
        )

    def test_a_missing_sample_is_named_not_misreported(self, tmp_path, monkeypatch):
        """A fresh clone has no samples; the error must say so and say why."""
        runner = _load_runner()
        monkeypatch.setattr(runner, "HERE", tmp_path)
        with pytest.raises(runner.HarnessError) as caught:
            runner.find_sample(".mp4")
        message = str(caught.value)
        assert ".mp4" in message
        assert "NOT tracked by git" in message

    def test_two_samples_of_a_kind_are_refused(self, tmp_path, monkeypatch):
        """Two candidates would make the inside/outside cases ambiguous."""
        runner = _load_runner()
        monkeypatch.setattr(runner, "HERE", tmp_path)
        (tmp_path / "a.mp4").write_bytes(b"x")
        (tmp_path / "b.mp4").write_bytes(b"x")
        with pytest.raises(runner.HarnessError, match="2 .mp4 samples"):
            runner.find_sample(".mp4")

    def test_reset_clears_and_builds_nothing(self, tmp_path, monkeypatch, capsys):
        """The review pass ends with this command, so it must not leave a harness."""
        runner = _load_runner()
        workspace = tmp_path / "ws"
        outside = tmp_path / "outside"
        monkeypatch.setattr(runner, "WORKSPACE", workspace)
        monkeypatch.setattr(runner, "OUTSIDE", outside)
        workspace.mkdir()
        outside.mkdir()
        (workspace / "TASKS.md").write_text("stale", encoding="utf-8")

        assert runner.main(["--reset"]) == 0
        assert not workspace.exists()
        assert not outside.exists()
        assert "cleared" in capsys.readouterr().out

    def test_the_task_list_carries_the_resolved_paths(self):
        runner = _load_runner()
        paths = {
            "workspace": runner.WORKSPACE,
            "videoInside": runner.WORKSPACE / "media" / "v.mp4",
            "pdfInside": runner.WORKSPACE / "media" / "p.pdf",
            "videoOutside": runner.OUTSIDE / "v.mp4",
            "pdfOutside": runner.OUTSIDE / "p.pdf",
        }
        text = runner.task_list(paths)
        for value in paths.values():
            assert str(value) in text
        assert runner.REMOTE_URL in text
        # The prune check is the defect this whole test exists to catch.
        assert "selection.applied" in text

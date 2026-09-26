"""Regressions from ``feedback/report (1).md`` (the other machine's report).

Each test pins one defect that report surfaced and that was fixed here, so the
fix cannot quietly regress:

* the run scratch is DETERMINISTIC per source, so a re-run never leaves a second
  directory behind (defect 3);
* the scratch SHAPE stays 32-hex, or ``clean -CleanScratch`` would stop owning it;
* a failed step-0 NAMES the scratch it deliberately kept (defect 3's hidden path);
* a download failure reports yt-dlp's own ERROR line, not just ``exit 1`` (defect 2);
* the item holds ONE media file, not two spellings of one title (defect 2's
  two-variant finding);
* ``registered`` gains a STARTABILITY sibling -- the spawn probe (defect 1).
"""

from __future__ import annotations

import json

import pytest

from zoombie.cli import Outcome
from zoombie.commands import pipeline, summarize as sz
from zoombie.lib import mcpsettings, scratch
from zoombie.lib.errors import StepFailedError


def _run_state(run, version: int) -> None:
    run.mkdir(parents=True, exist_ok=True)
    (run / sz.RUN_MARKER).write_text(
        json.dumps({"runVersion": version}), encoding="utf-8"
    )


class TestScratchIsDeterministic:
    """One source -> one scratch dir, and the name is one ``clean`` recognises."""

    def test_the_key_is_a_run_dir_name(self):
        """A different shape would make the run dir invisible to the sweep."""
        key = sz._run_key("https://example.test/v/1")
        assert scratch.is_run_dir(key), key
        assert len(key) == 32 and key == key.lower()

    def test_the_same_source_maps_to_the_same_key(self):
        assert sz._run_key("https://example.test/v/1") == sz._run_key(
            "https://example.test/v/1"
        )

    def test_different_sources_differ(self):
        assert sz._run_key("https://example.test/v/1") != sz._run_key(
            "https://example.test/v/2"
        )

    def test_a_local_source_is_canonicalised(self, tmp_path):
        """The same file spelled two ways must not yield two directories."""
        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")
        mine = str(media)
        other = str(tmp_path / "." / "clip.mp4")
        assert sz._run_key(mine) == sz._run_key(other)

    def test_a_resumable_run_is_reused(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        source = "https://example.test/v/1"
        first = sz._run_for_source(source)
        _run_state(__import__("pathlib").Path(first), sz.RUN_VERSION)
        second = sz._run_for_source(source)
        assert second == first

    def test_a_rerun_never_adds_a_second_directory(self, tmp_path, monkeypatch):
        """THE defect: a repeat run used to leave the previous dir beside the new."""
        monkeypatch.chdir(tmp_path)
        source = "https://example.test/v/1"
        first = sz._run_for_source(source)
        _run_state(__import__("pathlib").Path(first), sz.RUN_VERSION)
        sz._run_for_source(source)
        root = sz._scratch_root()
        assert len([e for e in __import__("os").listdir(root)]) == 1

    def test_a_stale_or_unknown_run_is_recreated_not_stacked(self, tmp_path, monkeypatch):
        """A pre-change ``run.json`` holds nothing resumable: same dir, fresh start."""
        monkeypatch.chdir(tmp_path)
        source = "https://example.test/v/1"
        run = sz._run_for_source(source)
        stale = __import__("pathlib").Path(run)
        _run_state(stale, sz.RUN_VERSION - 1)
        (stale / "leftover.bin").write_bytes(b"old")
        again = sz._run_for_source(source)
        assert again == run
        assert not (stale / "leftover.bin").exists()
        assert len(__import__("os").listdir(sz._scratch_root())) == 1

    def test_a_killed_run_with_no_state_is_recreated(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        source = "https://example.test/v/1"
        run = sz._run_for_source(source)
        # No run.json at all: the run died before step 0 wrote anything.
        assert sz._run_for_source(source) == run


class TestFailureNamesTheScratch:
    def test_attach_scratch_reports_the_path(self):
        outcome = Outcome(ok=False, error="yt-dlp failed (exit 1)")
        shaped = sz._attach_scratch(outcome, r"C:\ws\.tmp\zoombie-summarize\abc")
        assert shaped.ok is False
        assert shaped.data["run"] == r"C:\ws\.tmp\zoombie-summarize\abc"
        block = shaped.data["scratch"]
        assert block["kept"] is True and block["leftover"] is True
        assert shaped.error == outcome.error

    def test_the_hint_does_not_name_a_verb_that_would_spare_it(self):
        """``clean -CleanScratch`` sweeps the toolchain roots, NOT the workspace .tmp.

        Naming a verb that would ignore this directory is worse than naming none, so
        the hint must offer the two things that really work: resume, or delete it.
        """
        block = sz._attach_scratch(
            Outcome(ok=False, error="x"), r"C:\ws\.tmp\zoombie-summarize\abc"
        ).data["scratch"]
        assert "CleanScratch" not in block["hint"]
        assert "resume" in block["hint"]


class TestDownloadFailureIsDiagnosable:
    """``exit 1`` alone was unactionable; the yt-dlp ERROR line must survive."""

    def test_failure_detail_takes_the_last_non_empty_line(self):
        text = "progress\n\nERROR: [rutube] Unable to download video JSON\n"
        assert "Unable to download video JSON" in pipeline.ytdlp.failure_detail(text)

    def test_failure_detail_is_empty_for_no_output(self):
        assert pipeline.ytdlp.failure_detail("") == ""
        assert pipeline.ytdlp.failure_detail("\n\n") == ""

    def test_failure_detail_is_clipped(self):
        detail = pipeline.ytdlp.failure_detail("x" * 5000)
        assert len(detail) <= 403  # ": " + limit

    def test_the_pipeline_error_carries_the_cause(self, tmp_path, monkeypatch):
        class _Env:
            def require(self, *_args):
                return "python.exe"

        class _Args:
            download_dir = str(tmp_path)
            force = False
            source = "https://example.test/v/1"

        monkeypatch.setattr(
            pipeline.process,
            "run_text",
            lambda *_a, **_k: (1, "ERROR: [rutube] Unable to download video JSON"),
        )
        with pytest.raises(StepFailedError) as caught:
            pipeline._download(_Env(), _Args(), str(tmp_path))
        assert "Unable to download video JSON" in str(caught.value)


class TestOneMediaFilePerItem:
    """yt-dlp's fullwidth name and the sanitized name must not BOTH survive."""

    def test_a_download_in_the_item_is_renamed_not_duplicated(self, tmp_path):
        item = tmp_path / "item"
        item.mkdir()
        # The observed shape: a title ending in fullwidth ？, which
        # --windows-filenames does not strip, is not ASCII-illegal either.
        downloaded = item / "Clip ？ [441eda35].mp4"
        downloaded.write_bytes(b"media")

        kept = pipeline._retain_source(str(downloaded), str(item))
        assert kept is not None
        files = [p.name for p in item.iterdir() if p.is_file()]
        assert len(files) == 1, files
        assert "？" not in files[0]
        assert not downloaded.exists()

    def test_a_local_source_is_still_never_duplicated(self, tmp_path):
        source = tmp_path / "elsewhere.mp4"
        source.write_bytes(b"media")
        item = tmp_path / "item"
        item.mkdir()
        assert pipeline._retain_source(str(source), str(item), local_source=True) is None
        assert source.exists()
        assert list(item.iterdir()) == []


class TestMcpProbe:
    """``registered`` proves the file; the probe proves the server STARTS."""

    def test_a_working_server_is_reported_available(self, monkeypatch):
        monkeypatch.setattr(
            mcpsettings.process, "run_text", lambda *_a, **_k: (0, "zoombie-mcp 6.0.0\n")
        )
        probe = mcpsettings.probe_server(interpreter_path="C:/python/python.exe")
        assert probe["ok"] is True
        assert probe["version"] == "zoombie-mcp 6.0.0"

    def test_a_server_that_exits_non_zero_is_not_available(self, monkeypatch):
        monkeypatch.setattr(mcpsettings.process, "run_text", lambda *_a, **_k: (1, ""))
        probe = mcpsettings.probe_server(interpreter_path="C:/python/python.exe")
        assert probe["ok"] is False
        assert probe["error"]

    def test_a_silent_zero_exit_is_not_available(self, monkeypatch):
        """No version banner means the server did not really answer."""
        monkeypatch.setattr(mcpsettings.process, "run_text", lambda *_a, **_k: (0, ""))
        assert mcpsettings.probe_server(interpreter_path="C:/python/python.exe")["ok"] is False

    def test_no_interpreter_is_a_reported_failure_not_a_raise(self, monkeypatch):
        probe = mcpsettings.probe_server(interpreter_path=None)
        # interpreter() may resolve one on the dev machine; only the shape is pinned.
        assert "ok" in probe and "version" in probe

    def test_the_probe_uses_the_registered_module(self, monkeypatch):
        seen = {}

        def _capture(argv, **_kwargs):
            seen["argv"] = argv
            return 0, "zoombie-mcp 6.1.0"

        monkeypatch.setattr(mcpsettings.process, "run_text", _capture)
        mcpsettings.probe_server(interpreter_path="C:/python/python.exe")
        assert seen["argv"][1:] == ["-m", mcpsettings.SERVER_MODULE, "--version"]


class TestCheckModeReportsTheTruth:
    """``-Check`` writes nothing, but must not claim "missing: none" over a dead server."""

    def _stub(self, monkeypatch, *, registered: bool, probe: dict | None):
        from zoombie.install import components

        monkeypatch.setattr(
            components.mcpsettings, "deploy",
            lambda **_k: {"name": "zoombie", "action": "up to date", "path": "C:/m.json"},
        )
        monkeypatch.setattr(components.mcpsettings, "is_registered", lambda _p: registered)
        monkeypatch.setattr(components.mcpsettings, "probe_server", lambda **_k: probe)
        return components

    def test_a_registered_server_is_probed_in_check_mode(self, monkeypatch):
        components = self._stub(
            monkeypatch, registered=True, probe={"ok": True, "version": "zoombie-mcp 6.1.0"}
        )
        record = components.deploy_mcp(components.Modes(check=True, dry_run=True))
        assert record["registered"] is True
        assert record["probe"]["ok"] is True

    def test_an_absent_entry_is_not_probed(self, monkeypatch):
        components = self._stub(monkeypatch, registered=False, probe=None)
        called = {"probe": False}

        def _boom(**_k):
            called["probe"] = True
            raise AssertionError("must not probe when nothing is registered")

        monkeypatch.setattr(components.mcpsettings, "probe_server", _boom)
        record = components.deploy_mcp(components.Modes(check=True, dry_run=True))
        assert record.get("probe") is None
        assert called["probe"] is False

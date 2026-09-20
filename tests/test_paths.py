"""Tests for the path rules: ASCII guard, budget, extended prefix, safe work copy.

``conftest.py`` puts ``scripts/`` on ``sys.path``, so the package imports directly.
"""

from __future__ import annotations

import os

import pytest

from zoombie.lib import paths

CYRILLIC_DIR = "\u0442\u0435\u0441\u0442"  # "тест"


class TestAscii:
    def test_plain_ascii_is_ascii(self):
        assert paths.is_ascii(r"C:\Users\maxim\zoombie-env")

    def test_cyrillic_is_not_ascii(self):
        assert not paths.is_ascii(rf"C:\Users\{CYRILLIC_DIR}\zoombie-env")

    def test_empty_is_ascii(self):
        assert paths.is_ascii("")
        assert paths.is_ascii(None)


class TestExtendedPrefix:
    def test_adds_prefix(self):
        assert paths.to_extended(r"C:\x\y").startswith("\\\\?\\")

    def test_is_idempotent(self):
        once = paths.to_extended(r"C:\x\y")
        assert paths.to_extended(once) == once

    def test_unc_uses_unc_form(self):
        assert paths.to_extended(r"\\server\share\x") == r"\\?\UNC\server\share\x"

    def test_round_trip(self):
        original = r"C:\x\y.txt"
        assert paths.from_extended(paths.to_extended(original)) == original

    def test_round_trip_unc(self):
        original = r"\\server\share\x"
        assert paths.from_extended(paths.to_extended(original)) == original


class TestPathLength:
    def test_prefix_does_not_count(self):
        plain = r"C:\x\y"
        assert paths.path_length(paths.to_extended(plain)) == paths.path_length(plain)

    def test_empty_is_zero(self):
        assert paths.path_length("") == 0
        assert paths.path_length(None) == 0

    def test_absolute_length(self):
        assert paths.path_length(r"C:\abcd") == len(r"C:\abcd")


class TestAssertFits:
    def test_short_path_passes(self):
        assert paths.assert_fits(r"C:\short\file.txt", "The path") == r"C:\short\file.txt"

    def test_long_path_raises(self):
        long_path = "C:\\" + ("x" * (paths.PATH_BUDGET + 10))
        with pytest.raises(paths.PathTooDeepError) as excinfo:
            paths.assert_fits(long_path, "The output path")
        message = str(excinfo.value)
        assert "The output path" in message
        # The message must name the real cause, not just the number.
        assert "\\\\?\\" in message or "escape hatch" in message

    def test_slack_pushes_over_the_budget(self):
        # A base merely UNDER the budget must still fail once a suffix is added.
        base = "C:\\" + ("y" * (paths.PATH_BUDGET - 8))
        assert paths.path_length(base) <= paths.PATH_BUDGET
        with pytest.raises(paths.PathTooDeepError):
            paths.assert_fits(base, "The output path", slack=12)

    def test_empty_passes(self):
        assert paths.assert_fits("", "The path") == ""
        assert paths.assert_fits(None, "The path") is None


class TestCopyIntoSafeWork:
    def test_copies_under_an_ascii_name(self, tmp_path):
        source = tmp_path / "audio.wav"
        source.write_bytes(b"RIFFdata")
        work = tmp_path / "work"
        work.mkdir()

        result = paths.copy_into_safe_work(str(source), str(work))

        assert result["work_dir"] == str(work)
        assert os.path.basename(result["input_path"]) == "input.wav"
        assert paths.is_ascii(result["input_path"])
        assert paths.is_file(result["input_path"])
        assert result["original_path"] == str(source)

    def test_cyrillic_source_name_is_isolated(self, tmp_path):
        source = tmp_path / f"{CYRILLIC_DIR}.mp4"
        source.write_bytes(b"video")
        work = tmp_path / "work"
        work.mkdir()

        result = paths.copy_into_safe_work(str(source), str(work))

        assert paths.is_ascii(result["input_path"])
        assert paths.is_file(result["input_path"])

    def test_missing_extension_falls_back_to_bin(self, tmp_path):
        source = tmp_path / "noextension"
        source.write_bytes(b"x")
        work = tmp_path / "work"
        work.mkdir()

        result = paths.copy_into_safe_work(str(source), str(work))
        assert os.path.basename(result["input_path"]) == "input.bin"

    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            paths.copy_into_safe_work(str(tmp_path / "nope.wav"), str(tmp_path))

    def test_non_ascii_work_dir_refused(self, tmp_path):
        source = tmp_path / "a.wav"
        source.write_bytes(b"x")
        with pytest.raises(ValueError):
            paths.copy_into_safe_work(str(source), str(tmp_path / CYRILLIC_DIR))


class TestFileHelpers:
    def test_ensure_and_remove(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        paths.ensure_dir(str(target))
        assert paths.is_dir(str(target))
        paths.remove(str(target), recursive=True)
        assert not paths.exists(str(target))

    def test_remove_missing_is_silent(self, tmp_path):
        paths.remove(str(tmp_path / "nope"))

    def test_remove_quietly_reports_success_when_gone(self, tmp_path):
        target = tmp_path / "work"
        target.mkdir()
        (target / "f.txt").write_text("x")
        assert paths.remove_quietly(str(target), recursive=True) is True
        assert not paths.exists(str(target))

    def test_newest_file(self, tmp_path):
        older = tmp_path / "old.txt"
        newer = tmp_path / "new.txt"
        older.write_text("a")
        newer.write_text("b")
        os.utime(older, (1, 1))

        assert paths.newest_file(str(tmp_path)) == str(newer)

    def test_newest_file_empty_dir(self, tmp_path):
        assert paths.newest_file(str(tmp_path)) is None

    def test_copy_file_creates_parent(self, tmp_path):
        source = tmp_path / "src.txt"
        source.write_text("hello")
        destination = tmp_path / "deep" / "nested" / "dst.txt"
        paths.copy_file(str(source), str(destination))
        assert destination.read_text() == "hello"

    def test_extension_helpers(self):
        assert paths.extension_of(r"C:\a\b.wav") == ".wav"
        assert paths.without_extension(r"C:\a\b.wav") == r"C:\a\b"


class TestEnvRoot:
    def test_override_is_used_verbatim(self, monkeypatch):
        monkeypatch.setenv("ZOOMBIE_ENV_ROOT", r"C:\custom-root")
        assert paths.env_root() == r"C:\custom-root"
        assert not paths.root_is_default()

    def test_default_is_used_when_no_override(self, monkeypatch):
        monkeypatch.delenv("ZOOMBIE_ENV_ROOT", raising=False)
        monkeypatch.setenv("USERPROFILE", r"C:\Users\maxim")
        assert paths.env_root() == os.path.join(r"C:\Users\maxim", "zoombie-env")
        assert paths.root_is_default()

    def test_falls_back_to_public_for_non_ascii_profile(self, monkeypatch):
        monkeypatch.delenv("ZOOMBIE_ENV_ROOT", raising=False)
        monkeypatch.setenv("USERPROFILE", rf"C:\Users\{CYRILLIC_DIR}")
        monkeypatch.setenv("PUBLIC", r"C:\Users\Public")
        assert paths.env_root() == os.path.join(r"C:\Users\Public", "zoombie-env")

    def test_root_candidates_include_both_locations(self, monkeypatch):
        monkeypatch.delenv("ZOOMBIE_ENV_ROOT", raising=False)
        monkeypatch.setenv("USERPROFILE", r"C:\Users\maxim")
        monkeypatch.setenv("PUBLIC", r"C:\Users\Public")
        candidates = paths.root_candidates()
        assert os.path.join(r"C:\Users\maxim", "zoombie-env") in candidates
        assert os.path.join(r"C:\Users\Public", "zoombie-env") in candidates

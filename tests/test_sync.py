"""Tests for the content-based reconciler: the pure plan and the tree hasher.

These pin the property the whole content-based install rests on: a diff is
derived from bytes, not from a version marker, so "up to date" cannot be claimed
while the content differs.
"""

from __future__ import annotations

from zoombie.install import syncfiles
from zoombie.lib import sync


class _Modes:
    """Minimal stand-in for components.Modes: the write decision only."""

    def __init__(self, may_write: bool = True):
        self.may_write = may_write


class TestWriteIfChangedIdempotence:
    """The launcher and the getter carry CRLF; a re-run must report `unchanged`.

    Reading with text-mode newline translation (`\\n`) while writing `\\r\\n` made a
    byte-identical file report `updated` on every run — a non-idempotent install.
    """

    def test_crlf_file_is_unchanged_on_second_write(self, tmp_path):
        target = tmp_path / "zoombie.cmd"
        text = "@echo off\r\necho hi\r\n"
        first = syncfiles._write_if_changed(_Modes(), str(target), text)
        assert first == "added"
        second = syncfiles._write_if_changed(_Modes(), str(target), text)
        assert second == "unchanged"
        # The bytes on disk are exactly what was asked for, CRLF included.
        assert target.read_bytes() == text.encode("utf-8")

    def test_real_change_is_detected(self, tmp_path):
        target = tmp_path / "x.cmd"
        syncfiles._write_if_changed(_Modes(), str(target), "one\r\n")
        assert syncfiles._write_if_changed(_Modes(), str(target), "two\r\n") == "updated"

    def test_dry_run_writes_nothing(self, tmp_path):
        target = tmp_path / "x.cmd"
        action = syncfiles._write_if_changed(_Modes(may_write=False), str(target), "content\r\n")
        assert action == "added"
        assert not target.exists()


class TestPlan:
    def test_added_when_desired_only(self):
        result = sync.plan({"a.py": "1"}, {})
        assert result["added"] == ["a.py"]
        assert result["updated"] == []
        assert result["unchanged"] == []
        assert result["removed"] == []

    def test_updated_when_hash_differs(self):
        result = sync.plan({"a.py": "new"}, {"a.py": "old"})
        assert result["updated"] == ["a.py"]
        assert result["added"] == []
        assert result["removed"] == []

    def test_unchanged_when_equal(self):
        result = sync.plan({"a.py": "same"}, {"a.py": "same"})
        assert result["unchanged"] == ["a.py"]
        assert sync.is_noop(result)

    def test_removed_when_installed_only(self):
        result = sync.plan({}, {"gone.py": "1"})
        assert result["removed"] == ["gone.py"]
        assert not sync.is_noop(result)

    def test_paths_are_sorted(self):
        result = sync.plan({"b": "1", "a": "1", "c": "1"}, {})
        assert result["added"] == ["a", "b", "c"]

    def test_is_noop_only_when_nothing_changes(self):
        assert sync.is_noop({"added": [], "updated": [], "unchanged": ["x"], "removed": []})
        assert not sync.is_noop({"added": ["x"], "updated": [], "unchanged": [], "removed": []})
        assert not sync.is_noop({"added": [], "updated": [], "unchanged": [], "removed": ["x"]})


class TestPlanText:
    def test_absent_is_added(self):
        assert sync.plan_text("body", None) == "added"

    def test_equal_is_unchanged(self):
        assert sync.plan_text("body", "body") == "unchanged"

    def test_different_is_updated(self):
        assert sync.plan_text("new", "old") == "updated"


class TestHashTree:
    def test_missing_root_is_empty(self, tmp_path):
        assert sync.hash_tree(str(tmp_path / "nope")) == {}

    def test_hashes_relative_posix_paths(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.py").write_text("a", encoding="utf-8")
        (tmp_path / "sub" / "b.py").write_text("b", encoding="utf-8")
        tree = sync.hash_tree(str(tmp_path))
        assert set(tree) == {"a.py", "sub/b.py"}

    def test_same_content_hashes_equal_across_roots(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        left.mkdir()
        right.mkdir()
        (left / "x.py").write_text("same bytes", encoding="utf-8")
        (right / "x.py").write_text("same bytes", encoding="utf-8")
        assert sync.hash_tree(str(left)) == sync.hash_tree(str(right))

    def test_pycache_is_excluded(self, tmp_path):
        (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
        (tmp_path / "pkg" / "mod.py").write_text("m", encoding="utf-8")
        (tmp_path / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"x")
        tree = sync.hash_tree(str(tmp_path))
        assert set(tree) == {"pkg/mod.py"}

    def test_hash_text_matches_hash_file(self, tmp_path):
        target = tmp_path / "t.txt"
        target.write_text("hello", encoding="utf-8")
        assert sync.hash_file(str(target)) == sync.hash_text("hello")

    def test_change_detected_by_hash(self, tmp_path):
        (tmp_path / "a.py").write_text("one", encoding="utf-8")
        before = sync.hash_tree(str(tmp_path))
        (tmp_path / "a.py").write_text("two", encoding="utf-8")
        after = sync.hash_tree(str(tmp_path))
        result = sync.plan(after, before)
        assert result["updated"] == ["a.py"]

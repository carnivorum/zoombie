"""Tests for the ``verify`` command.

Two properties are load-bearing and get a test each:

* a clean tree exits 0 (so the command is usable as a gate), and
* a percent-encoded relative link is NOT false-flagged -- a verifier that flags
  its own valid output is worse than no verifier at all.

Everything is synthesized into ``tmp_path``; no PDF and no whisper is involved.
"""

from __future__ import annotations

import json

from zoombie import cli
from zoombie.commands import verify as vf

EM = "\u2014"

# A clean article: block 2 points at an existing transcript, block 4 links to
# anchors that exist in block 6, block 5 points at an existing summary, and the
# single image link resolves.
CLEAN = """# Статья

## 2. Источник
- [Транскрипт](transcript.srt)

## 3. Краткое содержание
Кратко.

## 4. Содержание
- [00:00:02 {em} Вступление](#s-1)
- [Финал](#s-2)

## 5. Связанные статьи
- [Другая статья](./other-summary.md)

## 6. Полное содержание транскрипта
### <a id="s-1"></a>00:00:02 {em} Вступление
Первый абзац.

![001 - p01.png](img/001%20-%20p01.png)

### <a id="s-2"></a>Финал
Второй абзац.
""".replace("{em}", EM)


def build_tree(tmp_path, summary: str = CLEAN, *, img: bool = True, siblings: bool = True):
    """Write a complete, clean article tree and return the root path."""
    if siblings:
        (tmp_path / "transcript.srt").write_text("1\n00:00:02,000 --> 00:00:04,000\nx\n", encoding="utf-8")
        (tmp_path / "other-summary.md").write_text("# Другая\n", encoding="utf-8")
        (tmp_path / "summary.md").write_text(summary, encoding="utf-8", newline="\n")
        (tmp_path / "img").mkdir()
        (tmp_path / "img" / "001 - p01.png").write_bytes(b"\x89PNG")
        (tmp_path / "img" / "manifest.json").write_text(
            json.dumps({"source": "x.pdf", "count": 1, "images": []}), encoding="utf-8"
        )
        (tmp_path / "img" / "README.md").write_text("# Extracted images\n", encoding="utf-8")
    return tmp_path


def kinds(report: dict) -> set[str]:
    return {problem["kind"] for problem in report["problems"]}


class TestCleanTree:
    def test_a_clean_fixture_passes(self, tmp_path):
        root = build_tree(tmp_path)
        report = vf.verify_tree(str(root))
        assert report["ok"] is True
        assert report["problems"] == []
        # summary.md + other-summary.md; img/README.md is a sidecar, not a
        # document, and lives in a subfolder the non-recursive walk skips.
        assert report["filesChecked"] == 2

    def test_cli_exits_zero_on_a_clean_tree(self, tmp_path):
        root = build_tree(tmp_path)
        assert cli.main(["verify", "-Dir", str(root)]) == 0

    def test_the_report_has_the_documented_shape(self, tmp_path):
        root = build_tree(tmp_path)
        report = vf.verify_tree(str(root))
        # ``advisories`` carries the checks that inform without failing a tree --
        # the block-6 "this is a copy" declaration is one, because every document
        # written before that convention existed would otherwise fail.
        assert set(report) == {"root", "filesChecked", "problems", "advisories", "ok"}
        assert report["root"] == str(root)

    def test_json_flag_still_exits_zero_and_keeps_the_shape(self, tmp_path):
        root = build_tree(tmp_path)
        assert cli.main(["verify", "-Dir", str(root), "-Json"]) == 0

    def test_a_missing_directory_is_a_clean_failure(self, tmp_path):
        assert cli.main(["verify", "-Dir", str(tmp_path / "nope")]) == 1


class TestMissingImage:
    def test_a_dangling_image_link_is_reported(self, tmp_path):
        root = build_tree(tmp_path)
        (root / "img" / "001 - p01.png").unlink()
        report = vf.verify_tree(str(root))
        assert report["ok"] is False
        assert vf.KIND_MISSING_IMAGE in kinds(report)
        problem = next(p for p in report["problems"] if p["kind"] == vf.KIND_MISSING_IMAGE)
        assert problem["line"] > 0
        assert problem["file"].endswith("summary.md")

    def test_cli_exits_one(self, tmp_path):
        root = build_tree(tmp_path)
        (root / "img" / "001 - p01.png").unlink()
        assert cli.main(["verify", "-Dir", str(root)]) == 1


class TestImageFolderSidecars:
    def test_a_missing_manifest_is_reported(self, tmp_path):
        root = build_tree(tmp_path)
        (root / "img" / "manifest.json").unlink()
        report = vf.verify_tree(str(root))
        assert vf.KIND_MISSING_MANIFEST in kinds(report)

    def test_a_missing_readme_is_reported(self, tmp_path):
        root = build_tree(tmp_path)
        (root / "img" / "README.md").unlink()
        report = vf.verify_tree(str(root))
        assert vf.KIND_MISSING_README in kinds(report)

    def test_an_image_dir_not_referenced_by_any_link_is_still_checked(self, tmp_path):
        """A link-only walk is blind to an img/ no Markdown happens to point at."""
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("![001 - p01.png](img/001%20-%20p01.png)", ""), encoding="utf-8"
        )
        (root / "img" / "manifest.json").unlink()
        report = vf.verify_tree(str(root))
        assert vf.KIND_MISSING_MANIFEST in kinds(report)


class TestDeadAnchor:
    def test_an_index_link_with_no_matching_anchor_is_reported(self, tmp_path):
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("[Финал](#s-2)", "[Финал](#s-99)"), encoding="utf-8"
        )
        report = vf.verify_tree(str(root))
        assert report["ok"] is False
        assert vf.KIND_DEAD_ANCHOR in kinds(report)
        problem = next(p for p in report["problems"] if p["kind"] == vf.KIND_DEAD_ANCHOR)
        assert "s-99" in problem["detail"]

    def test_an_anchor_that_exists_passes(self, tmp_path):
        root = build_tree(tmp_path)
        assert vf.verify_tree(str(root))["ok"] is True

    def test_a_foreign_fragment_is_not_our_problem(self, tmp_path):
        """Only ``#s-N`` ids are ours; any other fragment is a viewer concern."""
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary + "\n[см.](#footnote-3)\n", encoding="utf-8"
        )
        assert vf.verify_tree(str(root))["ok"] is True


class TestDeadLink:
    def test_a_dead_related_link_is_reported(self, tmp_path):
        root = build_tree(tmp_path)
        (root / "other-summary.md").unlink()
        report = vf.verify_tree(str(root))
        assert report["ok"] is False
        assert vf.KIND_DEAD_LINK in kinds(report)

    def test_a_percent_encoded_link_is_not_false_flagged(self, tmp_path):
        """A space encoded as %20 must resolve, not be reported as dead."""
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "other - summary.md").write_text("# Ещё\n", encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("./other-summary.md", "./other%20-%20summary.md"),
            encoding="utf-8",
        )
        report = vf.verify_tree(str(root))
        assert report["ok"] is True, report["problems"]

    def test_a_percent_encoded_cyrillic_link_is_not_false_flagged(self, tmp_path):
        root = build_tree(tmp_path)
        (root / "статья - обзор.md").write_text("# Обзор\n", encoding="utf-8")
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("./other-summary.md", "статья%20-%20обзор.md"),
            encoding="utf-8",
        )
        assert vf.verify_tree(str(root))["ok"] is True

    def test_a_paren_in_the_destination_is_read_whole(self, tmp_path):
        """A destination containing parens must resolve, not be truncated."""
        root = build_tree(tmp_path)
        (root / "отчёт (итог).md").write_text("# Итог\n", encoding="utf-8")
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("./other-summary.md", "отчёт%20%28итог%29.md"),
            encoding="utf-8",
        )
        assert vf.verify_tree(str(root))["ok"] is True

    def test_an_absolute_url_is_never_checked(self, tmp_path):
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary + "\n[пример](https://example.com/missing)\n", encoding="utf-8"
        )
        assert vf.verify_tree(str(root))["ok"] is True


class TestLineSuffixedLinks:
    """The ``file.ext:LINE`` form is what the skills themselves mandate.

    A destination like ``Безруков/summary.md:1`` must resolve the path and ignore
    the line number. The old resolver kept ``:1`` as part of the filename, so
    every line-suffixed link in the library was a dead link -- the checker flagged
    the house style as broken and could not be used as a gate.
    """

    def test_a_line_suffixed_existing_link_resolves(self, tmp_path):
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("./other-summary.md", "./other-summary.md:5"),
            encoding="utf-8",
        )
        report = vf.verify_tree(str(root))
        assert report["ok"] is True, report["problems"]

    def test_a_line_suffixed_bare_existing_link_resolves(self, tmp_path):
        """No leading ``./`` either, as the GitHub-style house form appears."""
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("./other-summary.md", "other-summary.md:12"),
            encoding="utf-8",
        )
        assert vf.verify_tree(str(root))["ok"] is True

    def test_a_line_suffixed_MISSING_link_is_still_a_dead_link(self, tmp_path):
        """The suffix must not blind the check to a genuinely missing file."""
        root = build_tree(tmp_path)
        (root / "other-summary.md").unlink()
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("./other-summary.md", "./other-summary.md:3"),
            encoding="utf-8",
        )
        assert vf.KIND_DEAD_LINK in kinds(vf.verify_tree(str(root)))


class TestMissingSource:
    def test_a_missing_source_document_is_reported(self, tmp_path):
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        # Block 2 is the source/provenance block; its ``<base>.md`` is the
        # document this summary was derived from.
        (root / "summary.md").write_text(
            summary.replace("(transcript.srt)", "(source-article.md)"),
            encoding="utf-8",
        )
        report = vf.verify_tree(str(root))
        assert report["ok"] is False
        assert vf.KIND_MISSING_SOURCE in kinds(report)

    def test_the_source_block_is_distinguished_from_block_5(self, tmp_path):
        """A missing .md in block 2 is a ``missing-source``; elsewhere a ``dead-link``."""
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("(transcript.srt)", "(gone.md)"), encoding="utf-8"
        )
        report = vf.verify_tree(str(root))
        assert vf.KIND_MISSING_SOURCE in kinds(report)
        assert vf.KIND_DEAD_LINK not in kinds(report)

    def test_block_5_is_reported_as_a_dead_link_not_a_source(self, tmp_path):
        root = build_tree(tmp_path)
        (root / "other-summary.md").unlink()
        report = vf.verify_tree(str(root))
        assert vf.KIND_DEAD_LINK in kinds(report)
        assert vf.KIND_MISSING_SOURCE not in kinds(report)


class TestRecursion:
    def test_a_subfolder_is_checked_with_recurse(self, tmp_path):
        root = build_tree(tmp_path)
        nested = root / "sub"
        nested.mkdir()
        (nested / "summary.md").write_text(
            CLEAN.replace("(transcript.srt)", "(gone.md)"), encoding="utf-8"
        )
        (nested / "img").mkdir()
        (nested / "img" / "001 - p01.png").write_bytes(b"\x89PNG")
        (nested / "img" / "manifest.json").write_text("{}", encoding="utf-8")
        (nested / "img" / "README.md").write_text("# x\n", encoding="utf-8")

        assert vf.verify_tree(str(root), recurse=False)["ok"] is True
        report = vf.verify_tree(str(root), recurse=True)
        assert report["ok"] is False
        assert vf.KIND_MISSING_SOURCE in kinds(report)
        assert any(p["file"].startswith(str(nested)) for p in report["problems"])

    def test_cli_recursive_flag(self, tmp_path):
        root = build_tree(tmp_path)
        nested = root / "sub"
        nested.mkdir()
        (nested / "broken.md").write_text("[x](missing.md)\n", encoding="utf-8")
        assert cli.main(["verify", "-Dir", str(root)]) == 0
        assert cli.main(["verify", "-Dir", str(root), "-Recurse"]) == 1


class TestReportOrdering:
    def test_the_report_is_sorted_and_deduplicated(self, tmp_path):
        root = build_tree(tmp_path)
        summary = (root / "summary.md").read_text(encoding="utf-8")
        (root / "summary.md").write_text(
            summary.replace("[x](#s-1)", "").replace("[Финал](#s-2)", "[Финал](#s-2) [тоже](#s-2)"),
            encoding="utf-8",
        )
        report = vf.verify_tree(str(root))
        keys = [(p["file"], p["line"], p["kind"]) for p in report["problems"]]
        assert keys == sorted(keys)
        # The same dead anchor referenced twice on one line is reported once.
        assert len(keys) == len(set(keys))

    def test_two_runs_agree(self, tmp_path):
        root = build_tree(tmp_path)
        first = vf.verify_tree(str(root))
        second = vf.verify_tree(str(root))
        assert first == second


class TestSection6ImageCount:
    """The advisory check: block-6 heading count vs the slide manifest count.

    Named for what it detects -- a shifted stamp association -- and advisory
    because a document written before the write-time guard is exactly what it
    catches, so failing it would reject every pre-guard library.
    """

    def _slide_manifest(self, root, count, *, images=None, kind="slides"):
        payload = {"count": count, "kind": kind}
        if images is not None:
            payload["images"] = images
        (root / "img" / "manifest.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_a_mismatch_is_reported_as_an_advisory(self, tmp_path):
        # CLEAN has two anchored block-6 headings (s-1, s-2); declare three images.
        root = build_tree(tmp_path)
        self._slide_manifest(root, 3)
        report = vf.verify_tree(str(root))

        advisories = [a for a in report["advisories"]
                      if a["kind"] == vf.KIND_SECTION6_IMAGE_COUNT]
        assert len(advisories) == 1
        advisory = advisories[0]
        assert advisory["severity"] == vf.SEVERITY_WARNING
        assert advisory["file"].endswith("summary.md")
        assert "2 anchored headings" in advisory["detail"]
        assert "3 images" in advisory["detail"]
        # Advisory, so the tree still passes: a pre-guard document must not fail.
        assert report["ok"] is True
        assert vf.KIND_SECTION6_IMAGE_COUNT not in kinds(report)

    def test_a_match_is_not_reported(self, tmp_path):
        root = build_tree(tmp_path)
        self._slide_manifest(root, 2)
        report = vf.verify_tree(str(root))
        assert all(a["kind"] != vf.KIND_SECTION6_IMAGE_COUNT
                   for a in report["advisories"])

    def test_the_images_length_is_the_fallback(self, tmp_path):
        """A manifest carrying only ``images`` is still compared."""
        root = build_tree(tmp_path)
        self._slide_manifest(root, 0, images=[{"file": "a.png"}, {"file": "b.png"},
                                              {"file": "c.png"}])
        report = vf.verify_tree(str(root))
        assert any(a["kind"] == vf.KIND_SECTION6_IMAGE_COUNT
                   for a in report["advisories"])

    def test_a_pdf_manifest_is_not_compared(self, tmp_path):
        """A PDF summary has no one-heading-per-image convention, so it is skipped.

        The stock ``build_tree`` writes a manifest WITHOUT ``kind: slides`` (the
        PDF shape); comparing counts there would flag every PDF item.
        """
        root = build_tree(tmp_path)
        report = vf.verify_tree(str(root))
        assert all(a["kind"] != vf.KIND_SECTION6_IMAGE_COUNT
                   for a in report["advisories"])

    def test_no_manifest_is_not_reported(self, tmp_path):
        root = build_tree(tmp_path)
        (root / "img" / "manifest.json").unlink()
        report = vf.verify_tree(str(root))
        assert all(a["kind"] != vf.KIND_SECTION6_IMAGE_COUNT
                   for a in report["advisories"])

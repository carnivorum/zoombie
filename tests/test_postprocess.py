"""Tests for the ``postprocess`` command.

The headline property is **idempotency**: a second ``-Apply`` on an unchanged
document must leave the file byte-identical. Every other test here exists to pin
down one pass in isolation so that property cannot be satisfied by a pass that
simply does nothing.

Everything runs against synthesized Markdown and SRT written into ``tmp_path``;
no PDF, no whisper, no third-party dependency is involved.
"""

from __future__ import annotations

import json
import re

from zoombie import cli
from zoombie.commands import postprocess as pp

EM = "\u2014"

# A 6-block document, complete except that block 4 holds stale prose (which the
# index pass must replace) and the block-6 headings carry no anchors/timestamps.
SUMMARY = """# Заметки о рынке

## 2. Источник
- [Исходный транскрипт](transcript.srt)

## 3. Краткое содержание
Краткое содержание первое.

Краткое содержание второе.

## 4. Содержание
старое содержание без анкеров

## 5. Связанные статьи
- [Другая статья](./other-summary.md)

## 6. Полное содержание транскрипта
### Вступление
Мы начинаем разговор о портфеле клиента и его структуре.

### Часть 2
### Дополнительные материалы
Продолжаем с дополнительными материалами по инструментам рынка.

### Финал
Подводим итоги обсуждения портфеля и дальнейших шагов.
"""

SRT = """1
00:00:02,000 --> 00:00:04,500
Мы начинаем разговор о портфеле клиента и его структуре

2
00:01:00,000 --> 00:01:05,000
Продолжаем с дополнительными материалами по инструментам рынка

3
00:02:30,000 --> 00:02:35,000
Подводим итоги обсуждения портфеля и дальнейших шагов
"""

MANIFEST = {
    "source": "deck.pdf",
    "count": 1,
    "images": [
        {
            "file": "001 - p01.png",
            "page": 1,
            "xref": 12,
            "width": 800,
            "height": 600,
            "bbox": [10.0, 20.0, 200.0, 300.0],
            "anchor_text": "Продолжаем с дополнительными материалами по инструментам рынка.",
            "page_title": "Дополнительные материалы",
            "digest": "abc",
            "bytes": 1234,
        }
    ],
}


# Block 3 carrying a criticism sub-block. This is the shape the zoombie-summarize
# skill documents, and it must not disturb block 6's numbering.
SUMMARY_WITH_CRITICISM = SUMMARY.replace(
    "Краткое содержание второе.",
    "Краткое содержание второе.\n\n***Критика***\nОдин источник устарел.\nИтог подан односторонне.",
)


def write_doc(tmp_path, name: str = "summary.md", text: str = SUMMARY, srt: str | None = SRT):
    """Write a document (and its sibling SRT) and return the document path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    if srt is not None:
        (tmp_path / "transcript.srt").write_text(srt, encoding="utf-8", newline="\n")
    return path


def read_text(path) -> str:
    return path.read_text(encoding="utf-8")


class TestDryRunIsTheDefault:
    def test_dry_run_writes_nothing_and_apply_writes(self, tmp_path):
        path = write_doc(tmp_path)
        before = path.read_bytes()

        assert cli.main(["postprocess", "-Md", str(path)]) == 0
        assert path.read_bytes() == before  # dry run touched nothing

        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        after = path.read_bytes()
        assert after != before
        assert b'<a id="s-1"></a>' in after
        assert b"00:00:02" in after

    def test_apply_is_byte_identical_on_the_second_run(self, tmp_path):
        """The acceptance criterion: double ``-Apply`` leaves the file unchanged."""
        path = write_doc(tmp_path)

        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        first = path.read_bytes()

        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        second = path.read_bytes()
        assert second == first

        # A third run, to catch a two-cycle oscillation.
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        assert path.read_bytes() == first

    def test_report_has_the_documented_shape(self, tmp_path):
        path = write_doc(tmp_path)
        report_path = tmp_path / "report.json"
        assert cli.main(
            ["postprocess", "-Md", str(path), "-Apply", "-Report", str(report_path)]
        ) == 0

        payload = json.loads(read_text(report_path))
        assert payload["apply"] is True
        assert payload["changed"] == 1
        entry = payload["files"][0]
        for key in (
            "md", "applied", "headings", "timestamped", "unmatched", "indexEntries",
            "linksRewritten", "imagesPlaced", "imagesSkipped", "charsBefore", "charsAfter",
        ):
            assert key in entry, key
        assert entry["applied"] is True
        assert entry["md"] == str(path)
        assert entry["charsBefore"] != entry["charsAfter"]
        assert entry["headings"] == 4
        assert entry["indexEntries"] == 4

    def test_report_records_no_change_on_a_second_run(self, tmp_path):
        path = write_doc(tmp_path)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        report_path = tmp_path / "report2.json"
        assert cli.main(
            ["postprocess", "-Md", str(path), "-Apply", "-Report", str(report_path)]
        ) == 0
        payload = json.loads(read_text(report_path))
        assert payload["changed"] == 0
        assert payload["files"][0]["applied"] is False

    def test_dir_mode_walks_the_folder(self, tmp_path):
        write_doc(tmp_path, "a.md")
        write_doc(tmp_path, "b.md", srt=None)
        assert cli.main(["postprocess", "-Dir", str(tmp_path), "-Apply"]) == 0
        assert '<a id="s-1"></a>' in read_text(tmp_path / "a.md")
        assert '<a id="s-1"></a>' in read_text(tmp_path / "b.md")

    def test_dir_mode_without_recurse_skips_subfolders(self, tmp_path):
        write_doc(tmp_path, "top.md")
        nested = tmp_path / "sub"
        nested.mkdir()
        write_doc(nested, "c.md")

        assert cli.main(["postprocess", "-Dir", str(tmp_path), "-Apply"]) == 0
        assert '<a id="s-1"></a>' in read_text(tmp_path / "top.md")
        assert '<a id="s-1"></a>' not in read_text(nested / "c.md")

        assert cli.main(["postprocess", "-Dir", str(tmp_path), "-Recurse", "-Apply"]) == 0
        assert '<a id="s-1"></a>' in read_text(nested / "c.md")

    def test_md_and_dir_together_is_refused(self, tmp_path):
        path = write_doc(tmp_path)
        assert cli.main(["postprocess", "-Md", str(path), "-Dir", str(tmp_path)]) == 1

    def test_a_missing_input_is_a_clean_failure(self, tmp_path):
        assert cli.main(["postprocess", "-Md", str(tmp_path / "nope.md")]) == 1


class TestAnchors:
    def test_every_heading_gets_a_sequential_anchor(self):
        out, stats = pp.process_document(SUMMARY, None, "img")
        assert '### <a id="s-1"></a>Вступление' in out
        assert '### <a id="s-4"></a>Финал' in out
        assert stats["headings"] == 4

    def test_anchors_are_idempotent(self):
        once, _ = pp.process_document(SUMMARY, None, "img")
        twice, _ = pp.process_document(once, None, "img")
        assert twice == once


class TestTimestamps:
    def test_headings_get_the_srt_start_time(self, tmp_path):
        path = write_doc(tmp_path)
        out, stats = pp.process_document(
            read_text(path), str(tmp_path / "transcript.srt"), "img"
        )
        assert f'### <a id="s-1"></a>00:00:02 {EM} Вступление' in out
        assert f'### <a id="s-4"></a>00:02:30 {EM} Финал' in out
        assert stats["timestamped"] == 3
        assert stats["unmatched"] == []

    def test_an_unmatched_divider_inherits_the_next_stamped_time(self, tmp_path):
        """A divider between two stamped blocks takes the NEXT one's time."""
        path = write_doc(tmp_path)
        out, stats = pp.process_document(
            read_text(path), str(tmp_path / "transcript.srt"), "img"
        )
        # "Часть 2" has no paragraph of its own, so it matched nothing and
        # inherits from "Дополнительные материалы".
        assert f'### <a id="s-2"></a>00:01:00 {EM} Часть 2' in out
        assert f'### <a id="s-3"></a>00:01:00 {EM} Дополнительные материалы' in out
        assert stats["unmatched"] == []
        assert stats["timestamped"] == 3  # the divider is stamped but has no prose

    def test_a_heading_that_never_matches_is_reported_and_unstamped(self, tmp_path):
        text = SUMMARY.replace(
            "Подводим итоги обсуждения портфеля и дальнейших шагов.",
            "Совсем другой текст которого нет в субтитрах.",
        )
        path = write_doc(tmp_path, text=text)
        out, stats = pp.process_document(
            read_text(path), str(tmp_path / "transcript.srt"), "img"
        )
        assert stats["unmatched"] == ["Финал"]
        # No timestamp was invented for it, and the anchor is still there.
        assert '### <a id="s-4"></a>Финал' in out

    def test_a_stale_timestamp_is_replaced_not_stacked(self, tmp_path):
        text = SUMMARY.replace("### Вступление", f"### 00:09:59 {EM} Вступление")
        path = write_doc(tmp_path, text=text)
        out, stats = pp.process_document(
            read_text(path), str(tmp_path / "transcript.srt"), "img"
        )
        assert "00:09:59" not in out
        section_6 = out.split("## 6.")[1]
        assert section_6.count(f'### <a id="s-1"></a>00:00:02 {EM} Вступление') == 1
        assert stats["timestamped"] == 3

    def test_no_srt_means_no_timestamps_but_anchors_still_land(self):
        out, stats = pp.process_document(SUMMARY, None, "img")
        assert stats["timestamped"] == 0
        assert EM not in out.split("## 6.")[1]
        assert '### <a id="s-1"></a>Вступление' in out

    def test_a_sibling_summary_srt_is_found_by_default(self, tmp_path):
        path = write_doc(tmp_path, srt=None)
        (tmp_path / "summary.srt").write_text(SRT, encoding="utf-8", newline="\n")
        assert pp._srt_for(str(path), None).endswith("summary.srt")

    def test_summary_md_falls_back_to_transcript_srt(self, tmp_path):
        path = write_doc(tmp_path)
        assert pp._srt_for(str(path), None).endswith("transcript.srt")

    def test_an_explicit_srt_always_wins(self, tmp_path):
        path = write_doc(tmp_path)
        other = tmp_path / "other.srt"
        other.write_text(SRT, encoding="utf-8", newline="\n")
        assert pp._srt_for(str(path), str(other)) == str(other)


class TestSectionIsolation:
    def test_section_4_is_regenerated_and_3_and_5_are_untouched(self, tmp_path):
        path = write_doc(tmp_path)
        before = read_text(path)
        before_3 = before.split("## 3.")[1].split("## 4.")[0]
        before_5 = before.split("## 5.")[1].split("## 6.")[0]

        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        after = read_text(path)

        # Section 3 and section 5 are byte-identical: neither the index pass nor
        # the image pass may touch them.
        assert after.split("## 3.")[1].split("## 4.")[0] == before_3
        assert after.split("## 5.")[1].split("## 6.")[0] == before_5

        # Block 4 is exactly the freshly rendered index.
        assert "старое содержание без анкеров" not in after
        index_body = after.split("## 4.")[1].split("## 5.")[0]
        assert index_body.count("- [") == 4
        assert f"- [00:00:02 {EM} Вступление](#s-1)" in index_body
        assert f"- [00:01:00 {EM} Часть 2](#s-2)" in index_body
        assert f"- [00:02:30 {EM} Финал](#s-4)" in index_body

    def test_an_asterisk_bullet_index_is_replaced_too(self, tmp_path):
        text = SUMMARY.replace("старое содержание без анкеров", "* старый пункт")
        path = write_doc(tmp_path, text=text)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        after = read_text(path)
        assert "* старый пункт" not in after
        assert "- [" in after.split("## 4.")[1].split("## 5.")[0]

    def test_link_repair_rewrites_the_destination_but_not_the_label(self, tmp_path):
        # A destination with a space, a paren and an inner "v2" is the case that
        # truncating scanners get wrong.
        text = SUMMARY.replace("./other-summary.md", "other summary (v2).md")
        path = write_doc(tmp_path, text=text)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        after = read_text(path)
        section_5 = after.split("## 5.")[1].split("## 6.")[0]
        assert "- [Другая статья](other%20summary%20%28v2%29.md)" in section_5
        # Only the destination changed: the label is byte-identical.
        assert "[Другая статья]" in section_5


class TestLinkRepair:
    def test_a_space_in_a_destination_is_encoded(self):
        out, count = pp.rewrite_links(f"- [Другая статья](other summary.md)\n")
        assert out == "- [Другая статья](other%20summary.md)\n"
        assert count == 1

    def test_parens_in_a_destination_are_not_truncated(self):
        text = "- [Отчёт](отчёт (итог).md)\n"
        out, _ = pp.rewrite_links(text)
        # The balanced scanner must read the WHOLE destination, inner parens
        # included, and then encode both of them.
        assert out == "- [Отчёт](отчёт%20%28итог%29.md)\n"
        assert out.count("%29") == 1

    def test_nested_brackets_in_a_label_become_parens(self):
        out, _ = pp.rewrite_links("- [см. [1] и прим.](a b.md)\n")
        assert out == "- [см. (1) и прим.](a%20b.md)\n"

    def test_cyrillic_is_left_readable(self):
        assert pp.rewrite_links("[x](статья.md)\n") == ("[x](статья.md)\n", 0)

    def test_an_absolute_url_is_never_touched(self):
        text = "[x](https://example.com/a_b?q=1)\n"
        assert pp.rewrite_links(text) == (text, 0)

    def test_repair_is_idempotent(self):
        once, _ = pp.rewrite_links("- [A](a b.md) [B](c (d).md)\n")
        twice, count = pp.rewrite_links(once)
        assert twice == once
        assert count == 0

    def test_an_unterminated_link_is_left_alone(self):
        text = "[broken](no closing\n"
        assert pp.rewrite_links(text) == (text, 0)

    def test_a_multi_line_destination_is_not_collapsed(self):
        text = "[A](a\n b.md)\n"
        out, _ = pp.rewrite_links(text)
        # The newline survives (it is not in the encode table) and the space is
        # encoded -- the link is not collapsed onto one line.
        assert out == "[A](a\n%20b.md)\n"


class TestCriticismSubBlock:
    """A non-heading sub-block in block 3 must be invisible to every pass.

    The skill documents the criticism sub-block as bold-italic precisely because
    a heading there would be numbered by `assign_anchors`, and the block-4 index
    is narrowed to block 6 -- leaving a dangling `s-N` that `verify` rejects.
    These tests pin that reasoning down.
    """

    def test_block_6_numbering_is_unaffected(self, tmp_path):
        path = write_doc(tmp_path, text=SUMMARY_WITH_CRITICISM)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        after = read_text(path)

        section_6 = after.split("## 6.")[1]
        anchors = re.findall(r'### <a id="(s-\d+)"></a>', section_6)
        # Dense and starting at s-1: the block-3 sub-block consumed nothing.
        assert anchors == ["s-1", "s-2", "s-3", "s-4"]

    def test_the_criticism_text_survives_untouched(self, tmp_path):
        path = write_doc(tmp_path, text=SUMMARY_WITH_CRITICISM)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        after = read_text(path)

        block_3 = after.split("## 3.")[1].split("## 4.")[0]
        assert "***Критика***" in block_3
        assert "Один источник устарел." in block_3
        assert "Итог подан односторонне." in block_3
        # No anchor may be attached to it.
        assert "<a id=" not in block_3

    def test_the_index_has_one_entry_per_block_6_heading(self, tmp_path):
        """The count must be block-6 headings only, never that plus the sub-block."""
        path = write_doc(tmp_path, text=SUMMARY_WITH_CRITICISM)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        after = read_text(path)

        index_body = after.split("## 4.")[1].split("## 5.")[0]
        assert index_body.count("- [") == 4
        assert "Критика" not in index_body

    def test_a_criticism_sub_block_keeps_the_run_idempotent(self, tmp_path):
        path = write_doc(tmp_path, text=SUMMARY_WITH_CRITICISM)
        args = ["postprocess", "-Md", str(path), "-Apply"]
        assert cli.main(args) == 0
        first = path.read_bytes()
        assert cli.main(args) == 0
        assert path.read_bytes() == first

    def test_a_stale_anchor_on_the_sub_block_self_heals(self, tmp_path):
        """A document mangled by the older whole-file numbering must converge.

        The old pass numbered every `###` anywhere, so a document written with a
        `### Criticism` heading in block 3 carries an `s-1` that belongs to it.
        One run must move that numbering into block 6.
        """
        stale = SUMMARY.replace(
            "Краткое содержание второе.",
            'Краткое содержание второе.\n\n### <a id="s-1"></a>Критика\nЗамечание.',
        )
        path = write_doc(tmp_path, text=stale)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        after = read_text(path)

        block_3 = after.split("## 3.")[1].split("## 4.")[0]
        assert "<a id=" not in block_3, "the stale anchor must be stripped"
        assert "Критика" in block_3
        section_6 = after.split("## 6.")[1]
        assert re.findall(r'### <a id="(s-\d+)"></a>', section_6) == [
            "s-1", "s-2", "s-3", "s-4"
        ]


class TestImages:
    def _with_manifest(self, tmp_path, name="summary.md", text=SUMMARY):
        path = write_doc(tmp_path, name=name, text=text)
        image_dir = tmp_path / "img"
        image_dir.mkdir()
        (image_dir / "001 - p01.png").write_bytes(b"\x89PNG")
        (image_dir / "manifest.json").write_text(
            json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8"
        )
        return path, image_dir

    def test_strip_then_insert_is_idempotent_across_two_runs(self, tmp_path):
        path, image_dir = self._with_manifest(tmp_path)

        args = ["postprocess", "-Md", str(path), "-Apply", "-ImageDir", str(image_dir)]
        assert cli.main(args) == 0
        first = path.read_bytes()
        assert b"![001 - p01.png](img/001%20-%20p01.png)" in first

        assert cli.main(args) == 0
        assert path.read_bytes() == first  # not duplicated, not moved

    def test_a_missing_image_dir_is_not_invented(self, tmp_path):
        path, image_dir = self._with_manifest(tmp_path)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        assert "img/001" in read_text(path)
        assert pp.image_url_prefix(str(image_dir)) == "img/"

    def test_the_image_lands_after_its_anchor_paragraph(self, tmp_path):
        path, image_dir = self._with_manifest(tmp_path)
        assert cli.main(
            ["postprocess", "-Md", str(path), "-Apply", "-ImageDir", str(image_dir)]
        ) == 0
        text = read_text(path)
        paragraph = "Продолжаем с дополнительными материалами по инструментам рынка."
        assert f"{paragraph}\n\n![001 - p01.png](img/001%20-%20p01.png)\n" in text
        # ... and it is never glued to the following prose.
        assert "![001 - p01.png](img/001%20-%20p01.png)Подводим" not in text

    def test_a_manifest_without_a_match_is_skipped_not_dumped(self, tmp_path):
        path, image_dir = self._with_manifest(tmp_path)
        manifest = json.loads(read_text(image_dir / "manifest.json"))
        manifest["images"][0]["anchor_text"] = "текст которого нет в документе"
        manifest["images"][0]["page_title"] = "тоже нет"
        (image_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

        assert cli.main(
            ["postprocess", "-Md", str(path), "-Apply", "-ImageDir", str(image_dir)]
        ) == 0
        text = read_text(path)
        assert "img/001" not in text
        # Nothing was dumped at the top of the document as a last resort.
        assert text.lstrip().startswith("# Заметки о рынке"), text[:80]

    def test_the_page_title_is_the_fallback_anchor(self, tmp_path):
        path, image_dir = self._with_manifest(tmp_path)
        manifest = json.loads(read_text(image_dir / "manifest.json"))
        # The primary anchor no longer matches; the page title does.
        manifest["images"][0]["anchor_text"] = "нет такого текста"
        manifest["images"][0]["page_title"] = (
            "Продолжаем с дополнительными материалами по инструментам рынка."
        )
        (image_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        assert cli.main(
            ["postprocess", "-Md", str(path), "-Apply", "-ImageDir", str(image_dir)]
        ) == 0
        assert "img/001" in read_text(path)

    def test_nearest_neighbour_keeps_reading_order(self, tmp_path):
        """Two images with no anchor of their own still land in manifest order."""
        path, image_dir = self._with_manifest(tmp_path)
        manifest = json.loads(read_text(image_dir / "manifest.json"))
        plain = {
            "file": "002 - p02.png",
            "page": 2,
            "xref": 13,
            "width": 800,
            "height": 600,
            "bbox": [10.0, 20.0, 200.0, 300.0],
            "anchor_text": "текст которого нет",
            "page_title": "и такого нет",
            "digest": "def",
            "bytes": 99,
        }
        # Both entries are anchorless, so both are skipped rather than dumped.
        manifest["images"] = [plain]
        (image_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        assert cli.main(
            ["postprocess", "-Md", str(path), "-Apply", "-ImageDir", str(image_dir)]
        ) == 0
        assert "img/" not in read_text(path).split("## 6.")[1]

        # Now give the first a real anchor: the anchorless second rides along.
        manifest["images"] = [dict(MANIFEST["images"][0]), plain]
        (image_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        assert cli.main(
            ["postprocess", "-Md", str(path), "-Apply", "-ImageDir", str(image_dir)]
        ) == 0
        text = read_text(path)
        assert "img/001%20-%20p01.png" in text
        assert "img/002%20-%20p02.png" in text
        assert text.index("img/001") < text.index("img/002")

    def test_a_missing_manifest_is_a_silent_no_op(self, tmp_path):
        path = write_doc(tmp_path)
        assert cli.main(["postprocess", "-Md", str(path), "-Apply"]) == 0
        assert "img/" not in read_text(path).split("## 6.")[1]

    def test_previous_images_are_removed_when_the_manifest_drops_them(self, tmp_path):
        path, image_dir = self._with_manifest(tmp_path)
        args = ["postprocess", "-Md", str(path), "-Apply", "-ImageDir", str(image_dir)]
        assert cli.main(args) == 0
        assert "img/001" in read_text(path)

        (image_dir / "manifest.json").write_text(
            json.dumps({"source": "x.pdf", "count": 0, "images": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        assert cli.main(args) == 0
        assert "img/001" not in read_text(path)

    def test_a_corrupt_manifest_is_ignored(self, tmp_path):
        path, image_dir = self._with_manifest(tmp_path)
        (image_dir / "manifest.json").write_text("{not json", encoding="utf-8")
        assert cli.main(
            ["postprocess", "-Md", str(path), "-Apply", "-ImageDir", str(image_dir)]
        ) == 0
        assert "img/001" not in read_text(path)


class TestSlideTimes:
    """A slide manifest's ``timeSec`` prefers over the fuzzy SRT text search.

    The association is by ORDER (one ``###`` per slide, in reading order), so the
    test proves the exact time lands on the exact heading without any of the
    narration needing to match the cue text.
    """

    def _doc(self) -> str:
        return (
            "# T\n\n## 4. Содержание\n\n## 6. Полный текст (копия)\n"
            "### Slide one\nМы начинаем разговор о портфеле клиента.\n\n"
            "### Slide two\nПродолжаем с дополнительными материалами.\n"
        )

    def _manifest(self, image_dir, times):
        image_dir.mkdir(exist_ok=True)
        rows = [
            {"file": f"{i:03d} - x.png", "timeSec": t, "anchor_text": ""}
            for i, t in enumerate(times, start=1)
        ]
        (image_dir / "manifest.json").write_text(
            json.dumps({"source": "v.mp4", "count": len(rows), "images": rows},
                       ensure_ascii=False),
            encoding="utf-8",
        )

    def test_time_sec_stamps_headings_by_order(self, tmp_path):
        image_dir = tmp_path / "img"
        self._manifest(image_dir, [83.0, 3661.0])
        out, stats = pp.process_document(
            self._doc(), None, str(image_dir), str(tmp_path / "summary.md")
        )
        # 83s -> 00:01:23, 3661s -> 01:01:01, with no SRT at all.
        assert f"00:01:23 {EM} Slide one" in out
        assert f"01:01:01 {EM} Slide two" in out
        assert stats["unmatched"] == []

    def test_more_times_than_headings_is_truncated(self, tmp_path):
        image_dir = tmp_path / "img"
        self._manifest(image_dir, [1.0, 2.0, 3.0, 4.0])
        out, _ = pp.process_document(
            self._doc(), None, str(image_dir), str(tmp_path / "summary.md")
        )
        # The surplus times must not invent a heading or wrap onto another one:
        # only the two real headings are stamped, in block 4 and block 6 each.
        assert out.count(EM) == 4

    def test_without_time_sec_the_srt_path_is_unchanged(self, tmp_path):
        image_dir = tmp_path / "img"
        image_dir.mkdir()
        (image_dir / "manifest.json").write_text(
            json.dumps({"source": "v.mp4", "count": 0, "images": []},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        srt = tmp_path / "t.srt"
        srt.write_text(
            "1\n00:00:05,000 --> 00:00:06,000\nМы начинаем разговор о портфеле клиента\n",
            encoding="utf-8",
        )
        out, _ = pp.process_document(
            self._doc(), str(srt), str(image_dir), str(tmp_path / "summary.md")
        )
        # 5s -> 00:00:05. A manifest with no timeSec must not disable the SRT
        # path for the heading it DOES match.
        assert f"00:00:05 {EM} Slide one" in out

    def test_is_idempotent(self, tmp_path):
        image_dir = tmp_path / "img"
        self._manifest(image_dir, [10.0, 20.0])
        doc = self._doc()
        once, _ = pp.process_document(doc, None, str(image_dir), str(tmp_path / "s.md"))
        twice, _ = pp.process_document(once, None, str(image_dir), str(tmp_path / "s.md"))
        assert once == twice


class TestWhitespace:
    def test_blank_runs_collapse_and_one_trailing_newline_is_kept(self):
        text = "# T\n\n\n\n### A\nПервый.\n\n\n\n### B\nВторой.\n\n\n"
        out, _ = pp.process_document(text, None, "img")
        assert "\n\n\n" not in out
        assert out.endswith("\n")
        assert not out.endswith("\n\n")

    def test_a_document_without_a_final_newline_gets_exactly_one(self):
        out, _ = pp.process_document("# T\n\n### A\nПервый.", None, "img")
        assert out.endswith("Первый.\n")

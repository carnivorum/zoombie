"""Tests for range-based, idempotent Markdown editing.

``conftest.py`` puts ``scripts/`` on ``sys.path``, so the package imports directly.
"""

from __future__ import annotations

import re

from zoombie.lib import markdown as md

EM_DASH = "\u2014"

# ``HH:MM:SS — `` in front of a section-6 heading, as written by the timestamp
# pass. The agent splits this back out before building the index.
TS_PREFIX_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+\u2014\s+")

SUMMARY_TEMPLATE = """# \u0417\u0430\u043c\u0435\u0442\u043a\u0438

## 1. \u0422\u0438\u0442\u0443\u043b
\u0422\u0435\u043a\u0441\u0442 \u0442\u0438\u0442\u0443\u043b\u0430.

## 2. \u041a\u0440\u0430\u0442\u043a\u043e\u0435 \u0441\u043e\u0434\u0435\u0440\u0436\u0430\u043d\u0438\u0435
\u041a\u0440\u0430\u0442\u043a\u043e.

## 3. \u041a\u043b\u044e\u0447\u0435\u0432\u044b\u0435 \u0442\u0435\u0437\u0438\u0441\u044b
- \u0422\u0435\u0437\u0438\u0441 \u043e\u0434\u0438\u043d.

## 4. \u0421\u043e\u0434\u0435\u0440\u0436\u0430\u043d\u0438\u0435
\u0441\u0442\u0430\u0440\u043e\u0435 \u0441\u043e\u0434\u0435\u0440\u0436\u0430\u043d\u0438\u0435 \u0431\u0435\u0437 \u0430\u043d\u043a\u0435\u0440\u043e\u0432

## 5. \u0421\u0432\u044f\u0437\u0430\u043d\u043d\u044b\u0435 \u0441\u0442\u0430\u0442\u044c\u0438
- [\u0421\u0442\u0430\u0442\u044c\u044f](../other/summary.md)

## 6. \u0422\u0440\u0430\u043d\u0441\u043a\u0440\u0438\u043f\u0442
### 00:00:00 \u2014 \u0412\u0441\u0442\u0443\u043f\u043b\u0435\u043d\u0438\u0435
\u041f\u0435\u0440\u0432\u044b\u0439 \u0430\u0431\u0437\u0430\u0446.

### 00:05:00 \u2014 \u0413\u043b\u0430\u0432\u043d\u0430\u044f \u0438\u0434\u0435\u044f
\u0412\u0442\u043e\u0440\u043e\u0439 \u0430\u0431\u0437\u0430\u0446.

### \u041f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0440\u0435\u043c\u0430\u0440\u043a\u0430
\u0422\u0440\u0435\u0442\u0438\u0439 \u0430\u0431\u0437\u0430\u0446.
"""

SECTION_4_RE = r"^##\s*4\."
SECTION_6_RE = r"^##\s*6\."


def _run_pipeline(text: str) -> str:
    """assign_anchors -> render_index -> replace_range into section 4.

    This is the sequence the ``zoombie-summarize`` skill runs, and it must be a
    no-op on its second invocation. The heading timestamp is read back off the
    heading text (the way the reference pass does) so the index entry and the
    heading it links to stay in sync by construction.
    """
    numbered, headings = md.assign_anchors(text)
    for heading in headings:
        match = TS_PREFIX_RE.match(heading["title"])
        heading["timestamp"] = match.group(1) if match else ""
        if match:
            heading["title"] = heading["title"][match.end() :]
    body = md.render_index(headings)

    bounds = md.block_range(numbered, SECTION_4_RE)
    assert bounds is not None
    return md.replace_range(numbered, bounds[0], bounds[1], body + "\n")


class TestBlockRange:
    def test_returns_heading_end_to_next_block_heading(self):
        text = "## 4. A\nbody\n\n## 5. B\nrest\n"
        start, end = md.block_range(text, r"^##\s*4\.")
        assert text[start:end] == "body\n\n"
        assert text[end:].startswith("## 5.")

    def test_last_section_runs_to_eof(self):
        text = "## 1. A\nbody\n## 9. Last\ntail\n"
        start, end = md.block_range(text, r"^##\s*9\.")
        assert end == len(text)
        assert text[start:end] == "tail\n"

    def test_single_hash_headings_do_not_terminate_the_range(self):
        text = "## 6. T\n### sub\nbody\n"
        start, end = md.block_range(text, SECTION_6_RE)
        assert text[start:end] == "### sub\nbody\n"

    def test_missing_heading_is_none(self):
        assert md.block_range("no headings here\n", r"^##\s*4\.") is None


class TestReplaceRange:
    def test_replaces_a_slice(self):
        assert md.replace_range("abcdef", 1, 3, "XY") == "aXYdef"

    def test_clamps_out_of_range_bounds(self):
        assert md.replace_range("abc", -5, 99, "Z") == "Z"
        assert md.replace_range("abc", 2, 1, "Z") == "abZc"

    def test_insertion_at_eof(self):
        assert md.replace_range("abc", 3, 3, "Z") == "abcZ"


class TestHeadingList:
    def test_collects_level_three_and_deeper_with_offsets(self):
        text = "# top\n### A\nbody\n#### B\n### <a id=\"s-2\"></a>C\n"
        headings = md.heading_list(text)
        assert [(h["level"], h["title"]) for h in headings] == [(3, "A"), (4, "B"), (3, "C")]
        assert all(text[h["start"] : h["start"] + 3].startswith("###") for h in headings)
        assert [h["anchor"] for h in headings] == ["", "", "s-2"]

    def test_ignores_hashes_inside_a_line(self):
        assert md.heading_list("text ### not a heading\n") == []


class TestAssignAnchors:
    def test_numbers_headings_in_reading_order(self):
        text = "## 6. T\n### A\n### B\n#### C\n"
        numbered, headings = md.assign_anchors(text)
        assert [h["anchor"] for h in headings] == ["s-1", "s-2", "s-3"]
        assert '### <a id="s-1"></a>A' in numbered
        assert '#### <a id="s-3"></a>C' in numbered
        assert md.anchor_id(4) == "s-4"
        assert md.ANCHOR_PREFIX == "s"

    def test_is_idempotent(self):
        text = "## 6. T\n### A\n### B\n"
        once, _ = md.assign_anchors(text)
        twice, _ = md.assign_anchors(once)
        assert twice == once

    def test_replaces_an_existing_anchor_rather_than_stacking(self):
        text = "## 6. T\n### <a id=\"s-9\"></a>B\n### A\n"
        numbered, headings = md.assign_anchors(text)
        assert numbered.count('id="s-1"') == 1
        assert 'id="s-9"' not in numbered
        assert headings[0]["title"] == "B"
        assert '### <a id="s-1"></a>B' in numbered
        assert '### <a id="s-2"></a>A' in numbered

    def test_title_is_reported_without_the_anchor(self):
        numbered, headings = md.assign_anchors("### 00:05:00 \u2014 \u0418\u0434\u0435\u044f\n")
        assert headings[0]["title"] == f"00:05:00 {EM_DASH} \u0418\u0434\u0435\u044f"
        assert numbered == '### <a id="s-1"></a>00:05:00 \u2014 \u0418\u0434\u0435\u044f\n'

    def test_text_without_headings_is_unchanged(self):
        assert md.assign_anchors("just prose\n") == ("just prose\n", [])


class TestRenderIndex:
    def test_mixed_timestamped_and_untimestamped(self):
        headings = [
            {"level": 3, "title": "\u0412\u0441\u0442\u0443\u043f\u043b\u0435\u043d\u0438\u0435", "anchor": "s-1", "timestamp": "00:00:00"},
            {"level": 3, "title": "\u0413\u043b\u0430\u0432\u043d\u0430\u044f \u0438\u0434\u0435\u044f", "anchor": "s-2", "timestamp": "00:05:00"},
            {"level": 4, "title": "\u041f\u043e\u0434\u0440\u043e\u0431\u043d\u043e\u0441\u0442\u044c", "anchor": "s-3"},
            {"level": 3, "title": "\u0424\u0438\u043d\u0430\u043b", "anchor": "s-4", "timestamp": None},
        ]
        expected = (
            "- [00:00:00 \u2014 \u0412\u0441\u0442\u0443\u043f\u043b\u0435\u043d\u0438\u0435](#s-1)\n"
            "- [00:05:00 \u2014 \u0413\u043b\u0430\u0432\u043d\u0430\u044f \u0438\u0434\u0435\u044f](#s-2)\n"
            "  - [\u041f\u043e\u0434\u0440\u043e\u0431\u043d\u043e\u0441\u0442\u044c](#s-3)\n"
            "- [\u0424\u0438\u043d\u0430\u043b](#s-4)\n"
        )
        assert md.render_index(headings) == expected

    def test_uses_a_real_em_dash_with_spaces(self):
        rendered = md.render_index(
            [{"level": 3, "title": "T", "anchor": "s-1", "timestamp": "00:00:01"}]
        )
        assert rendered == "- [00:00:01 \u2014 T](#s-1)\n"
        assert " \u2014 " in rendered

    def test_empty_list_still_produces_a_body(self):
        assert md.render_index([]) == "_(\u0441\u043e\u0434\u0435\u0440\u0436\u0430\u043d\u0438\u0435 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e)_\n"


class TestStripInsertedImages:
    def test_removes_a_two_image_glued_run_and_keeps_trailing_prose(self):
        body = (
            "\u041f\u0435\u0440\u0432\u044b\u0439 \u0430\u0431\u0437\u0430\u0446.\n"
            "![001 - p01.png](img/001%20-%20p01.png)![002 - p02.png](img/002%20-%20p02.png)\u0418\u0434\u0435\u043c \u0434\u0430\u043b\u044c\u0448\u0435.\n"
            "\u0412\u0442\u043e\u0440\u043e\u0439 \u0430\u0431\u0437\u0430\u0446.\n"
        )
        cleaned = md.strip_inserted_images(body)
        assert "img/" not in cleaned
        assert "\u0418\u0434\u0435\u043c \u0434\u0430\u043b\u044c\u0448\u0435." in cleaned
        assert "\u041f\u0435\u0440\u0432\u044b\u0439 \u0430\u0431\u0437\u0430\u0446." in cleaned
        assert "\u0412\u0442\u043e\u0440\u043e\u0439 \u0430\u0431\u0437\u0430\u0446." in cleaned
        assert "\n\n\n" not in cleaned

    def test_removes_a_single_image_line_but_keeps_the_paragraph(self):
        body = "\u0422\u0435\u043a\u0441\u0442.\n![a.png](img/a.png)\n\n\u0414\u0430\u043b\u044c\u0448\u0435.\n"
        assert md.strip_inserted_images(body) == "\u0422\u0435\u043a\u0441\u0442.\n\n\u0414\u0430\u043b\u044c\u0448\u0435.\n"

    def test_is_idempotent(self):
        body = "![a](img/a.png)![b](img/b.png)tail\n"
        once = md.strip_inserted_images(body)
        assert md.strip_inserted_images(once) == once

    def test_leaves_foreign_links_alone(self):
        body = "[summary.md](./summary.md)\n![x](photo.png)\n"
        assert md.strip_inserted_images(body) == body


class TestInsertAfterParagraph:
    LINKS = ["![001 - p01.png](img/001%20-%20p01.png)"]

    def test_interior_offset_is_blank_line_separated(self):
        body = "\u041f\u0435\u0440\u0432\u044b\u0439.\n\n\u0412\u0442\u043e\u0440\u043e\u0439.\n"
        offset = body.index("\u0412\u0442\u043e\u0440\u043e\u0439")
        out = md.insert_after_paragraph(body, offset, self.LINKS)
        assert out == (
            "\u041f\u0435\u0440\u0432\u044b\u0439.\n\n"
            "\n![001 - p01.png](img/001%20-%20p01.png)\n"
            "\u0412\u0442\u043e\u0440\u043e\u0439.\n"
        )
        assert "![001" in out.split("\n")[3]

    def test_at_eof_has_no_trailing_blank_or_garbage(self):
        body = "\u041f\u0435\u0440\u0432\u044b\u0439 \u0430\u0431\u0437\u0430\u0446.\n"
        out = md.insert_after_paragraph(body, len(body), self.LINKS)
        assert out == "\u041f\u0435\u0440\u0432\u044b\u0439 \u0430\u0431\u0437\u0430\u0446.\n\n![001 - p01.png](img/001%20-%20p01.png)"
        assert not out.endswith("\n\n")

    def test_does_not_double_the_trailing_newline(self):
        # Inserting right before the blank line's newline must not add a second
        # one: the "next char is already a newline" branch has to fire.
        body = "tail\n\nnext\n"
        out = md.insert_after_paragraph(body, 5, self.LINKS)
        assert out == "tail\n\n![001 - p01.png](img/001%20-%20p01.png)\nnext\n"
        assert "\n\n\n" not in out

    def test_insert_strip_insert_returns_the_original(self):
        body = "\u041f\u0435\u0440\u0432\u044b\u0439 \u0430\u0431\u0437\u0430\u0446.\n\n\u0412\u0442\u043e\u0440\u043e\u0439 \u0430\u0431\u0437\u0430\u0446.\n"
        offset = body.index("\u0412\u0442\u043e\u0440\u043e\u0439")
        inserted = md.insert_after_paragraph(body, offset, self.LINKS)
        stripped = md.strip_inserted_images(inserted)
        assert stripped == body
        again = md.insert_after_paragraph(stripped, offset, self.LINKS)
        assert again == inserted

    def test_foreign_offsets_are_clamped(self):
        body = "abc"
        assert md.insert_after_paragraph(body, 99, self.LINKS) == "abc\n![001 - p01.png](img/001%20-%20p01.png)"
        assert md.insert_after_paragraph(body, -99, self.LINKS) == "\n![001 - p01.png](img/001%20-%20p01.png)\nabc"

    def test_no_links_is_a_no_op(self):
        assert md.insert_after_paragraph("abc", 1, []) == "abc"


class TestSmallHelpers:
    def test_ensure_trailing_newline_adds_exactly_one(self):
        assert md.ensure_trailing_newline("abc") == "abc\n"
        assert md.ensure_trailing_newline("abc\n\n\n") == "abc\n"
        assert md.ensure_trailing_newline("") == ""

    def test_collapse_blank_runs(self):
        assert md.collapse_blank_runs("a\n\n\n\nb") == "a\n\nb"
        assert md.collapse_blank_runs("a\n\nb") == "a\n\nb"


class TestFullPipeline:
    def test_second_run_is_a_byte_identical_no_op(self):
        first = _run_pipeline(SUMMARY_TEMPLATE)

        # The first run must have done the work it is supposed to do.
        assert '### <a id="s-1"></a>00:00:00 \u2014 \u0412\u0441\u0442\u0443\u043f\u043b\u0435\u043d\u0438\u0435' in first
        assert '### <a id="s-3"></a>\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0440\u0435\u043c\u0430\u0440\u043a\u0430' in first
        assert f"- [00:00:00 {EM_DASH} \u0412\u0441\u0442\u0443\u043f\u043b\u0435\u043d\u0438\u0435](#s-1)" in first
        assert f"  - [\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0440\u0435\u043c\u0430\u0440\u043a\u0430]" not in first
        assert "\u0441\u0442\u0430\u0440\u043e\u0435 \u0441\u043e\u0434\u0435\u0440\u0436\u0430\u043d\u0438\u0435 \u0431\u0435\u0437 \u0430\u043d\u043a\u0435\u0440\u043e\u0432" not in first
        index_start, index_end = md.block_range(first, SECTION_4_RE)
        assert first[index_start:index_end].count("- [") == 3  # 3 entries, not 6

        second = _run_pipeline(first)
        assert second == first

    def test_pipeline_edits_only_section_4_and_6(self):
        first = _run_pipeline(SUMMARY_TEMPLATE)
        assert "## 1. \u0422\u0438\u0442\u0443\u043b\n\u0422\u0435\u043a\u0441\u0442 \u0442\u0438\u0442\u0443\u043b\u0430." in first
        assert "## 5. \u0421\u0432\u044f\u0437\u0430\u043d\u043d\u044b\u0435 \u0441\u0442\u0430\u0442\u044c\u0438" in first

    def test_block_4_body_is_only_the_index(self):
        first = _run_pipeline(SUMMARY_TEMPLATE)
        start, end = md.block_range(first, SECTION_4_RE)
        body = first[start:end]
        assert "- [" in body
        assert md.block_range(first, SECTION_6_RE) is not None

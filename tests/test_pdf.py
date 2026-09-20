"""Tests for the PDF page-spec parser and result shape (no PyMuPDF needed)."""

from __future__ import annotations

import pytest

from zoombie.lib import pdf


class TestParsePages:
    def test_none_means_all(self):
        assert pdf.parse_pages(None) is None
        assert pdf.parse_pages("") is None

    def test_single_page_is_zero_based(self):
        assert pdf.parse_pages("3") == [2]

    def test_range(self):
        assert pdf.parse_pages("1-5") == [0, 1, 2, 3, 4]

    def test_mixed_spec(self):
        assert pdf.parse_pages("1-3,8") == [0, 1, 2, 7]

    def test_whitespace_is_tolerated(self):
        assert pdf.parse_pages(" 1-2 , 5 ") == [0, 1, 4]

    def test_reversed_range_is_normalized(self):
        assert pdf.parse_pages("5-1") == [0, 1, 2, 3, 4]

    def test_duplicates_are_deduplicated(self):
        assert pdf.parse_pages("1,1,2") == [0, 1]

    def test_invalid_spec_raises(self):
        with pytest.raises(ValueError):
            pdf.parse_pages("abc")

    def test_zero_page_is_dropped(self):
        # Page 0 is not a page; the 0-based index would be -1.
        assert pdf.parse_pages("0") == []


class TestResultShape:
    def test_to_data_uses_the_documented_keys(self):
        result = pdf.Result(
            output=r"C:\docs\book.md",
            pages=3,
            ocr_used=True,
            kept_scanned_pages=[4, 5],
        )
        data = result.to_data()
        assert data["output"] == r"C:\docs\book.md"
        assert data["pages"] == 3
        assert data["ocrUsed"] is True
        assert data["keptScannedPages"] == [4, 5]
        assert data["engine"] == "pymupdf4llm"

    def test_defaults(self):
        data = pdf.Result(output="x.md").to_data()
        assert data["pages"] == 0
        assert data["ocrUsed"] is False
        assert data["keptScannedPages"] == []


class TestArgumentContract:
    def test_parser_accepts_the_documented_flags(self):
        parser = pdf.build_parser()
        args = parser.parse_args(
            [
                "--input", "in.pdf",
                "--output", "out.md",
                "--ocr",
                "--images", "img",
                "--pages", "1-5,8",
                "--json",
            ]
        )
        assert args.input == "in.pdf"
        assert args.output == "out.md"
        assert args.ocr is True
        assert args.images == "img"
        assert args.pages == "1-5,8"
        assert args.json is True

    def test_lang_defaults_to_eng(self):
        parser = pdf.build_parser()
        args = parser.parse_args(["--input", "a.pdf", "--output", "b.md"])
        assert args.lang == "eng"

    def test_input_and_output_are_required(self):
        parser = pdf.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


class TestAsText:
    def test_string_passes_through(self):
        assert pdf._as_text("hello") == "hello"

    def test_list_of_dicts_is_joined(self):
        value = [{"text": "one"}, {"text": "two"}]
        assert pdf._as_text(value) == "one\n\ntwo"

    def test_unexpected_shape_is_empty(self):
        assert pdf._as_text(42) == ""
        assert pdf._as_text(None) == ""

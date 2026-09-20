"""Tests for the text normalisation primitives.

``conftest.py`` puts ``scripts/`` on ``sys.path``, so the package imports directly.
"""

from __future__ import annotations

import os

from zoombie.lib import textnorm

CYRILLIC_SAMPLE = "  \u0401\u043b\u043a\u0430,  \u0442\u0430\u043c \u2014 \u00ab\u0434\u043e\u043c\u00bb!  "


def _fold(ch: str) -> str:
    """What :func:`normalize_with_map` turns a single source character into."""
    low = ch.lower().replace("\u0451", "\u0435")
    return low if low.isalnum() else " "


class TestNorm:
    def test_folds_case_yo_and_punctuation(self):
        assert textnorm.norm(CYRILLIC_SAMPLE) == "\u0435\u043b\u043a\u0430 \u0442\u0430\u043c \u0434\u043e\u043c"

    def test_keeps_yo_folded_but_leaves_j(self):
        # ё folds to е; й is a distinct letter and must survive untouched.
        assert textnorm.norm("\u0451\u0436\u0438\u043a") == "\u0435\u0436\u0438\u043a"
        assert textnorm.norm("\u0439\u043e\u0433\u0430") == "\u0439\u043e\u0433\u0430"

    def test_empty(self):
        assert textnorm.norm("") == ""
        assert textnorm.norm("   \t\n ") == ""


class TestNormalizeWithMap:
    def test_lengths_match(self):
        stream, index = textnorm.normalize_with_map(CYRILLIC_SAMPLE)
        assert len(stream) == len(index)

    def test_stream_equals_norm(self):
        stream, _ = textnorm.normalize_with_map(CYRILLIC_SAMPLE)
        assert stream == textnorm.norm(CYRILLIC_SAMPLE)

    def test_every_offset_produced_its_character(self):
        stream, index = textnorm.normalize_with_map(CYRILLIC_SAMPLE)
        assert stream
        for k, source_offset in enumerate(index):
            assert _fold(CYRILLIC_SAMPLE[source_offset]) == stream[k], (
                f"offset {source_offset} -> {stream[k]!r}"
            )

    def test_offsets_point_into_the_original(self):
        stream, index = textnorm.normalize_with_map(CYRILLIC_SAMPLE)
        for source_offset in index:
            assert 0 <= source_offset < len(CYRILLIC_SAMPLE)

    def test_yo_offset_points_at_the_yo_itself(self):
        # The first letter is Ё at offset 2 (two leading spaces).
        stream, index = textnorm.normalize_with_map(CYRILLIC_SAMPLE)
        assert stream[0] == "\u0435"
        assert index[0] == 2
        assert CYRILLIC_SAMPLE[index[0]] == "\u0401"

    def test_double_space_collapses_to_one_mapped_space(self):
        stream, index = textnorm.normalize_with_map("ab  cd")
        assert stream == "ab cd"
        assert len(index) == 5
        assert _fold("ab  cd"[index[2]]) == " "

    def test_plain_ascii_round_trip(self):
        stream, index = textnorm.normalize_with_map("Hello, World!")
        assert stream == "hello world"
        # The collapsed space is attributed to the first separator of the run,
        # which here is the comma (not the space after it, and not the W).
        assert ["Hello, World!"[i] for i in index] == [
            "H", "e", "l", "l", "o", ",", "W", "o", "r", "l", "d",
        ]

    def test_empty(self):
        assert textnorm.normalize_with_map("") == ("", [])
        assert textnorm.normalize_with_map(" ... ") == ("", [])


class TestHhmmss:
    def test_zero(self):
        assert textnorm.hhmmss(0) == "00:00:00"

    def test_fifty_nine(self):
        assert textnorm.hhmmss(59) == "00:00:59"

    def test_just_under_an_hour(self):
        assert textnorm.hhmmss(3599) == "00:59:59"

    def test_exactly_one_hour(self):
        assert textnorm.hhmmss(3600) == "01:00:00"

    def test_exactly_one_day(self):
        # Hours are unbounded, so a day is 24 hours, not a wrap to 00.
        assert textnorm.hhmmss(86400) == "24:00:00"

    def test_more_than_99_hours(self):
        assert textnorm.hhmmss(360000) == "100:00:00"

    def test_negative_clamps_to_zero(self):
        assert textnorm.hhmmss(-5) == "00:00:00"


class TestPercentEncodeDest:
    def test_encodes_space_and_parens(self):
        assert textnorm.percent_encode_dest("img/001 - p01(1).png") == (
            "img/001%20-%20p01%281%29.png"
        )

    def test_encodes_brackets_and_angle_brackets(self):
        assert textnorm.percent_encode_dest("[a]<b>") == "%5Ba%5D%3Cb%3E"

    def test_leaves_cyrillic_raw(self):
        assert textnorm.percent_encode_dest("\u043f\u0430\u043f\u043a\u0430/\u0444\u0430\u0439\u043b.md") == (
            "\u043f\u0430\u043f\u043a\u0430/\u0444\u0430\u0439\u043b.md"
        )

    def test_does_not_touch_the_fragment(self):
        assert textnorm.percent_encode_dest("a b#c d") == "a%20b#c d"

    def test_empty(self):
        assert textnorm.percent_encode_dest("") == ""


class TestSlugForFiles:
    DATED = "12 - 06.05.2020 - \u0417\u0430\u043c\u0435\u0442\u043a\u0438"

    def test_date_survives(self):
        slug = textnorm.slug_for_files(self.DATED)
        assert "06.05.2020" in slug
        assert slug == "06.05.2020-\u0417\u0430\u043c\u0435\u0442\u043a\u0438"

    def test_does_not_use_path_stem_semantics(self):
        # The two ways a folder-ish name gets mangled: Path().stem and
        # os.path.splitext. Both eat the date; slug_for_files must not.
        from pathlib import Path

        assert Path(self.DATED).stem == "12 - 06.05"
        assert os.path.splitext(self.DATED) == ("12 - 06.05", ".2020 - \u0417\u0430\u043c\u0435\u0442\u043a\u0438")
        assert "06.05.2020" in textnorm.slug_for_files(self.DATED)

    def test_keeps_allowed_characters(self):
        assert textnorm.slug_for_files("RBC-BidenPlaybook_2020.v2") == "RBC-BidenPlaybook_2020.v2"

    def test_collapses_bad_runs_to_one_dash(self):
        assert textnorm.slug_for_files("a  ,,  b") == "a-b"

    def test_truncates(self):
        assert textnorm.slug_for_files("x" * 50, max_len=10) == "x" * 10

    def test_empty_falls_back_to_src(self):
        assert textnorm.slug_for_files("") == "src"
        assert textnorm.slug_for_files(" , , ") == "src"

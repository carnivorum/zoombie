"""Tests for the SRT parser, the offset index and the anomaly reporter.

``conftest.py`` puts ``scripts/`` on ``sys.path``, so the package imports directly.
"""

from __future__ import annotations

from zoombie.lib import textnorm
from zoombie.lib import srt as srt_lib
from zoombie.lib.srt import Cue, SrtIndex

# BOM + CRLF + a multi-line cue + blank lines between cues + no final newline.
SRT_BOM_CRLF = (
    "\ufeff"
    "1\r\n"
    "00:00:01,000 --> 00:00:04,500\r\n"
    "\u041f\u0435\u0440\u0432\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430\r\n"
    "\u0438 \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435.\r\n"
    "\r\n"
    "2\r\n"
    "00:00:05,000 --> 00:00:08,000\r\n"
    "\u0412\u0442\u043e\u0440\u043e\u0439 \u0431\u043b\u043e\u043a.\r\n"
    "\r\n"
    "3\r\n"
    "00:00:09,250 --> 00:00:12,000\r\n"
    "\u0422\u0440\u0435\u0442\u0438\u0439 \u0431\u043b\u043e\u043a \u0431\u0435\u0437 \u0444\u0438\u043d\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u043f\u0435\u0440\u0435\u0432\u043e\u0434\u0430"
)

CUES = [
    Cue(1, 0.0, 5.0, "\u0414\u043e\u0431\u0440\u044b\u0439 \u0432\u0435\u0447\u0435\u0440, \u0441 \u0432\u0430\u043c\u0438 Crimson Alter"),
    Cue(2, 12.5, 18.0, "\u0438 \u044f \u0441\u043d\u043e\u0432\u0430 \u0440\u0430\u0434 \u0432\u0430\u0441 \u0432\u0438\u0434\u0435\u0442\u044c \u0443 \u0441\u0435\u0431\u044f \u0432 \u0433\u043e\u0441\u0442\u044f\u0445"),
    Cue(3, 25.0, 30.0, "\u0441\u0435\u0433\u043e\u0434\u043d\u044f \u0443 \u043d\u0430\u0441 \u0438\u0441\u0441\u043b\u0435\u0434\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0438\u0439 \u0441\u0442\u043e\u043b"),
]

CLEAN_CUES = [
    Cue(1, 0.0, 5.0, "\u0414\u043e\u0431\u0440\u044b\u0439 \u0432\u0435\u0447\u0435\u0440 \u0441 \u0432\u0430\u043c\u0438 Crimson Alter"),
    Cue(2, 5.0, 9.0, "\u0421\u0435\u0433\u043e\u0434\u043d\u044f \u043c\u044b \u043f\u043e\u0433\u043e\u0432\u043e\u0440\u0438\u043c \u043f\u0440\u043e \u0434\u043e\u043b\u043b\u0430\u0440"),
    Cue(3, 9.0, 14.0, "\u0421\u0438\u0441\u0442\u0435\u043c\u0430 \u043c\u0435\u0436\u0434\u0443\u043d\u0430\u0440\u043e\u0434\u043d\u043e\u0433\u043e \u043a\u0440\u0435\u0434\u0438\u0442\u043e\u0432\u0430\u043d\u0438\u044f \u0443\u0441\u0442\u0440\u043e\u0435\u043d\u0430 \u0441\u043b\u043e\u0436\u043d\u043e"),
    Cue(4, 14.0, 18.0, "\u0418\u043d\u0432\u0435\u0441\u0442\u043e\u0440\u044b \u0433\u043e\u0442\u043e\u0432\u044b \u0434\u0430\u0432\u0430\u0442\u044c \u043a\u0440\u0435\u0434\u0438\u0442\u044b \u0432 \u0434\u043e\u043b\u043b\u0430\u0440\u0430\u0445"),
    Cue(5, 18.0, 22.0, "\u0424\u0435\u0434\u0440\u0435\u0437\u0435\u0440\u0432 \u0437\u0430\u043f\u0443\u0441\u043a\u0430\u0435\u0442 \u0432\u0430\u043b\u044e\u0442\u043d\u044b\u0435 \u0441\u0432\u043e\u043f\u044b"),
    Cue(6, 22.0, 26.0, "\u041d\u0430 \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u0432\u0441\u0451 \u0434\u043e \u043d\u043e\u0432\u044b\u0445 \u0432\u0441\u0442\u0440\u0435\u0447"),
]


class TestParse:
    def test_bom_crlf_multiline_and_missing_final_newline(self, tmp_path):
        path = _write(tmp_path, "transcript.srt", SRT_BOM_CRLF)
        cues = srt_lib.parse(path)

        assert len(cues) == 3
        assert [c.index for c in cues] == [1, 2, 3]
        assert cues[0].start == 1.0
        assert cues[0].end == 4.5
        assert cues[0].text == (
            "\u041f\u0435\u0440\u0432\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430 \u0438 \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435."
        )
        assert cues[1].start == 5.0
        assert cues[2].start == 9.25
        assert cues[2].end == 12.0
        assert cues[2].text.endswith("\u043f\u0435\u0440\u0435\u0432\u043e\u0434\u0430")

    def test_lf_without_bom_parses_identically(self, tmp_path):
        def signature(path):
            return [(c.index, c.start, c.end, c.text) for c in srt_lib.parse(path)]

        with_bom = _write(tmp_path, "bom.srt", SRT_BOM_CRLF)
        plain_lf = _write(tmp_path, "lf.srt", SRT_BOM_CRLF.lstrip("\ufeff").replace("\r\n", "\n"))
        assert signature(plain_lf) == signature(with_bom)
        assert len(signature(plain_lf)) == 3

    def test_malformed_cue_is_skipped_not_fatal(self, tmp_path):
        text = (
            "1\n00:00:01,000 --> 00:00:02,000\n\u043f\u0435\u0440\u0432\u044b\u0439\n\n"
            "\u043d\u0435 \u043d\u043e\u043c\u0435\u0440 \u0438 \u043d\u0435 \u0442\u0430\u0439\u043c\u043a\u043e\u0434\n\n"
            "3\n00:00:03,000 --> 00:00:04,000\n\u0442\u0440\u0435\u0442\u0438\u0439\n"
        )
        cues = srt_lib.parse(_write(tmp_path, "broken.srt", text))
        assert [c.index for c in cues] == [1, 3]
        assert [c.start for c in cues] == [1.0, 3.0]

    def test_missing_file_is_empty(self, tmp_path):
        assert srt_lib.parse(tmp_path / "nope.srt") == []

    def test_blank_and_empty_are_empty(self, tmp_path):
        assert srt_lib.parse(_write(tmp_path, "blank.srt", "")) == []
        assert srt_lib.parse(_write(tmp_path, "lines.srt", "\n\n\n")) == []

    def test_hhmmss_properties(self):
        cue = Cue(1, 3661.0, 3723.0, "x")
        assert cue.start_hhmmss == "01:01:01"
        assert cue.end_hhmmss == "01:02:03"


def _write(tmp_path, name: str, text: str):
    """Write a fixture byte-for-byte.

    ``newline=""`` disables newline translation, which matters here: with the
    default, ``path.write_text("a\\r\\nb")`` would store ``a\\r\\r\\nb`` on
    Windows and the fixture would no longer be the CRLF (or LF) file it claims
    to be.
    """
    path = tmp_path / name
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


class TestParseLineEndings:
    def test_doubled_crlf_does_not_split_a_cue(self, tmp_path):
        # A file whose writer added CRLF to an existing CR: \r\r\n.
        doubled = SRT_BOM_CRLF.replace("\r\n", "\r\r\n").lstrip("\ufeff")
        cues = srt_lib.parse(_write(tmp_path, "doubled.srt", doubled))
        assert len(cues) == 3
        assert cues[0].start == 1.0
        assert cues[0].text.startswith("\u041f\u0435\u0440\u0432\u0430\u044f")

    def test_lone_cr_is_a_line_break(self, tmp_path):
        lone = SRT_BOM_CRLF.lstrip("\ufeff").replace("\r\n", "\r")
        cues = srt_lib.parse(_write(tmp_path, "cr.srt", lone))
        assert len(cues) == 3
        assert cues[2].start == 9.25


class TestSrtIndex:
    def test_time_at_at_a_cue_transition(self):
        index = SrtIndex(CUES)
        boundary = index.offsets[1]
        assert boundary > 0, "the fixture must exercise a real cue boundary"

        # The offset exactly at the boundary belongs to the cue that starts
        # there; one before it still belongs to the previous cue.
        assert index.time_at(boundary) == 12.5
        assert index.time_at(boundary - 1) == 0.0
        assert index.time_at(0) == 0.0
        assert index.time_at(len(index.full) - 1) == 25.0

    def test_text_for_tracks_the_same_boundary(self):
        index = SrtIndex(CUES)
        boundary = index.offsets[1]
        assert index.text_for(boundary) == CUES[1].text
        assert index.text_for(boundary - 1) == CUES[0].text

    def test_locate_maps_back_to_the_original_text(self):
        index = SrtIndex(CUES)
        word = "\u0438\u0441\u0441\u043b\u0435\u0434\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0438\u0439"
        position = index.full.find(textnorm.norm(word))
        assert position >= 0

        cue_index, original_offset = index.locate(position)
        assert cue_index == 2
        assert CUES[cue_index].text[original_offset : original_offset + len(word)] == word

    def test_find_hits_a_phrase_present_in_a_cue(self):
        index = SrtIndex(CUES)
        words = textnorm.norm(CUES[1].text).split()
        found = index.find(words, 0)
        assert found is not None
        assert index.time_at(found) == 12.5
        assert index.text_for(found) == CUES[1].text

    def test_find_returns_none_for_an_absent_phrase(self):
        index = SrtIndex(CUES)
        absent = textnorm.norm(
            "\u044d\u0442\u043e\u0439 \u0444\u0440\u0430\u0437\u044b \u0432 \u0442\u0440\u0430\u043d\u0441\u043a\u0440\u0438\u043f\u0442\u0435 \u043d\u0435\u0442"
        ).split()
        assert index.find(absent, 0) is None

    def test_find_retries_from_zero_when_the_cursor_passed_the_cue(self):
        index = SrtIndex(CUES)
        words = textnorm.norm(CUES[0].text).split()
        assert index.find(words, 0) == index.offsets[0]
        # A cursor beyond the match still finds it via the one retry from 0.
        assert index.find(words, len(index.full) - 1) == index.offsets[0]

    def test_find_is_monotonic_within_the_window(self):
        index = SrtIndex(CUES)
        first = textnorm.norm(CUES[0].text).split()
        second = textnorm.norm(CUES[1].text).split()
        cursor = index.find(first, 0)
        assert cursor is not None
        later = index.find(second, cursor)
        assert later is not None
        assert later >= cursor

    def test_empty_index_is_safe(self):
        index = SrtIndex([])
        assert index.full == ""
        assert index.time_at(5) == 0.0
        assert index.text_for(5) == ""
        assert index.locate(5) is None
        assert index.find(["anything"], 0) is None


class TestAnomalies:
    def test_clean_file_reports_nothing(self):
        assert srt_lib.anomalies(CLEAN_CUES) == []

    def test_duplicate_run_of_three(self):
        repeated = "\u0432 \u043e\u0431\u0449\u0435\u043c \u044d\u0442\u043e \u043f\u043e\u0432\u0442\u043e\u0440 \u0444\u0440\u0430\u0437\u044b"
        cues = [
            CLEAN_CUES[0],
            Cue(2, 5.0, 8.0, repeated),
            Cue(3, 8.0, 11.0, repeated),
            Cue(4, 11.0, 14.0, repeated),
            CLEAN_CUES[5],
        ]
        entries = srt_lib.anomalies(cues)
        assert [e["kind"] for e in entries] == ["duplicate-run"]
        entry = entries[0]
        assert entry["cue_indexes"] == [2, 3, 4]
        assert entry["start"] == "00:00:05"
        assert entry["end"] == "00:00:14"  # the end of the last cue in the run
        assert entry["text"] == repeated
        assert "3" in entry["detail"]

    def test_two_cues_are_not_a_run(self):
        repeated = "\u043a\u043e\u0440\u043e\u0442\u043a\u0438\u0439 \u043f\u043e\u0432\u0442\u043e\u0440"
        cues = [Cue(1, 0.0, 3.0, repeated), Cue(2, 3.0, 6.0, repeated)]
        assert srt_lib.anomalies(cues) == []

    def test_noise_only_run_of_three(self):
        cues = [
            CLEAN_CUES[0],
            Cue(2, 5.0, 7.0, "\u0432 \u043e\u0431\u0449\u0435\u043c,\u043d\u0443"),
            Cue(3, 7.0, 9.0, "\u0432\u043e\u0442 \u043a\u0430\u043a \u0431\u044b,"),
            Cue(4, 9.0, 11.0, "\u0442\u0430\u043a \u0441\u043a\u0430\u0437\u0430\u0442\u044c \u0432 \u0431\u043e\u043b\u044c\u0448\u0438\u043d\u0441\u0442\u0432\u0435"),
            CLEAN_CUES[5],
        ]
        entries = srt_lib.anomalies(cues)
        assert [e["kind"] for e in entries] == ["noise-only-run"]
        assert entries[0]["cue_indexes"] == [2, 3, 4]

    def test_noise_run_with_a_content_word_is_not_reported(self):
        cues = [Cue(1, 0.0, 2.0, "\u043d\u0443 \u0432\u043e\u0442 \u0434\u043e\u043b\u043b\u0430\u0440")]
        assert srt_lib.anomalies(cues) == []

    def test_timing_end_before_start(self):
        cues = [Cue(1, 10.0, 5.0, "\u0441\u043b\u043e\u043c\u0430\u043d\u043d\u043e\u0435 \u0432\u0440\u0435\u043c\u044f \u043a\u0443\u0435")]
        entries = srt_lib.anomalies(cues)
        assert [e["kind"] for e in entries] == ["timing"]
        assert entries[0]["cue_indexes"] == [1]
        assert entries[0]["start"] == "00:00:10"
        assert entries[0]["end"] == "00:00:05"
        assert "end" in entries[0]["detail"]

    def test_timing_long_cue_with_few_words(self):
        cues = [Cue(1, 0.0, 45.0, "\u043d\u0443")]
        entries = srt_lib.anomalies(cues)
        assert [e["kind"] for e in entries] == ["timing"]
        assert "45" in entries[0]["detail"]

    def test_long_cue_with_words_is_not_reported(self):
        text = " ".join(["\u0441\u043b\u043e\u0432\u043e"] * 20)
        assert srt_lib.anomalies([Cue(1, 0.0, 45.0, text)]) == []

    def test_reporter_is_pure_and_repeatable(self):
        cues = [
            Cue(1, 5.0, 3.0, "\u043d\u0443 \u0432\u043e\u0442"),
            Cue(2, 5.0, 3.0, "\u043d\u0443 \u0432\u043e\u0442"),
            Cue(3, 5.0, 3.0, "\u043d\u0443 \u0432\u043e\u0442"),
        ]
        before = [(c.index, c.start, c.end, c.text) for c in cues]
        first = srt_lib.anomalies(cues)
        second = srt_lib.anomalies(cues)

        assert first == second
        assert [(c.index, c.start, c.end, c.text) for c in cues] == before
        kinds = sorted({e["kind"] for e in first})
        assert kinds == ["duplicate-run", "noise-only-run", "timing"]

    def test_entries_expose_the_documented_keys(self):
        cues = [Cue(1, 10.0, 5.0, "\u0441\u043b\u043e\u043c\u0430\u043d\u043d\u043e\u0435 \u0432\u0440\u0435\u043c\u044f")]
        assert set(srt_lib.anomalies(cues)[0]) == {
            "kind",
            "cue_indexes",
            "start",
            "end",
            "text",
            "detail",
        }

    def test_fillers_can_be_overridden(self):
        texts = ["\u0431\u043b\u0430 \u043d\u0443", "\u043d\u0443 \u0431\u043b\u0430", "\u0431\u043b\u0430 \u0431\u043b\u0430 \u043d\u0443"]
        cues = [Cue(i, float(i), float(i) + 1, t) for i, t in enumerate(texts, start=1)]
        assert [e["kind"] for e in srt_lib.anomalies(
            cues, fillers=["\u0431\u043b\u0430", "\u043d\u0443"]
        )] == ["noise-only-run"]
        # Without the override the same cues carry content words, so nothing fires.
        assert srt_lib.anomalies(cues) == []

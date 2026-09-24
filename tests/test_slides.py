"""Unit tests for ``zoombie.lib.slides``.

Everything here is deterministic and needs no ffmpeg: timestamp parsing, the
perceptual-hash dedup, the interval/run maths, the narration join and the
manifest rendering. The ffmpeg decode itself is native and lives in the
self-test, exactly as ``test_pdf.py`` avoids opening a real PDF.
"""

from __future__ import annotations

import json
import os

import pytest

from zoombie.lib import slides
from zoombie.lib.errors import ZoombieError
from zoombie.lib.srt import Cue


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #

class TestParseTime:
    def test_hhmmss(self):
        assert slides.parse_time("01:02:03") == 3723.0

    def test_mmss(self):
        assert slides.parse_time("02:03") == 123.0

    def test_plain_seconds(self):
        assert slides.parse_time("90") == 90.0

    def test_comma_fraction(self):
        assert slides.parse_time("00:00:01,500") == pytest.approx(1.5)

    def test_dot_fraction(self):
        assert slides.parse_time("1.25") == pytest.approx(1.25)

    def test_invalid_is_a_named_failure(self):
        with pytest.raises(ZoombieError):
            slides.parse_time("not-a-time")

    def test_empty_is_a_named_failure(self):
        with pytest.raises(ZoombieError):
            slides.parse_time("   ")


class TestParseTimes:
    def test_sorted_and_deduplicated(self):
        assert slides.parse_times("00:20,00:05\n00:20") == [5.0, 20.0]

    def test_semicolon_and_comma_mix(self):
        assert slides.parse_times("10; 20,30") == [10.0, 20.0, 30.0]

    def test_empty_is_empty(self):
        assert slides.parse_times("") == []


def test_load_times_file(tmp_path):
    path = tmp_path / "times.txt"
    path.write_text("00:10\n00:05\n\n00:20\n", encoding="utf-8")
    assert slides.load_times_file(str(path)) == [5.0, 10.0, 20.0]


def test_load_times_file_missing_is_a_named_failure(tmp_path):
    with pytest.raises(ZoombieError):
        slides.load_times_file(str(tmp_path / "absent.txt"))


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #

def _frame(value: int) -> bytes:
    """One 9x8 grayscale frame filled with ``value``."""
    return bytes([value]) * slides.HASH_FRAME_BYTES


def _gradient_frame() -> bytes:
    """A 9x8 frame that increases left to right, so the hash is all ones."""
    row = bytes(range(slides.HASH_FRAME_BYTES // 8))
    return row * 8


class TestHash:
    def test_hamming(self):
        assert slides.hamming(0b1011, 0b1001) == 1
        assert slides.hamming(7, 7) == 0

    def test_flat_frame_has_no_gradient(self):
        # Every neighbour is equal, so no "brighter than the right neighbour"
        # bit is ever set.
        assert slides.dhash_bits(_frame(100)) == 0

    def test_rising_frame_sets_every_bit(self):
        assert slides.dhash_bits(_gradient_frame()) == (1 << 64) - 1

    def test_too_short_is_none(self):
        assert slides.dhash_bits(b"\x01\x02") is None

    def test_hash_sequence_slices_the_stream(self):
        raw = _frame(0) + _frame(200) + _frame(50)
        assert slides.hash_sequence(raw) == [0, 0, 0]

    def test_hash_sequence_drops_a_partial_frame(self):
        # A trailing partial frame (ffmpeg killed mid-write) must not invent a
        # frame, or it becomes a spurious slide boundary.
        raw = _frame(0) + b"\x00" * 10
        assert len(slides.hash_sequence(raw)) == 1


class TestStableRuns:
    def test_identical_frames_are_one_run(self):
        # 4 frames at 1 fps, all the same slide, above the 2s floor.
        runs = slides.stable_runs([5, 5, 5, 5], rate=1.0, distance=0, min_seconds=2.0)
        assert runs == [(0, 3)]

    def test_a_change_starts_a_new_run(self):
        hashes = [0] * 3 + [(1 << 64) - 1] * 3
        runs = slides.stable_runs(hashes, rate=1.0, distance=0, min_seconds=2.0)
        assert runs == [(0, 2), (3, 5)]

    def test_a_short_flap_is_merged_away(self):
        # Two long runs with a single odd frame between them: the odd frame is
        # too short to be its own slide, so it disappears.
        hashes = [0, 0, 0, (1 << 64) - 1, 0, 0, 0]
        runs = slides.stable_runs(hashes, rate=1.0, distance=0, min_seconds=2.0)
        # The odd frame breaks the chain into 3 runs, the middle one dropped.
        assert (3, 3) not in runs

    def test_a_chain_not_a_comparison_to_the_first(self):
        # Each step is within `distance` of the PREVIOUS frame but far from the
        # first: a slow drift must stay one run.
        hashes = [0b0000, 0b0001, 0b0011, 0b0111]
        runs = slides.stable_runs(hashes, rate=1.0, distance=1, min_seconds=1.0)
        assert runs == [(0, 3)]

    def test_none_breaks_the_chain(self):
        # A single frame at 1 fps is exactly at the min_seconds floor, so the
        # leading run survives; the None still splits it from the trailing run.
        runs = slides.stable_runs([0, None, 0, 0], rate=1.0, distance=0, min_seconds=1.0)
        assert runs == [(0, 0), (2, 3)]

    def test_all_none_is_no_runs(self):
        assert slides.stable_runs([None, None], rate=1.0, min_seconds=1.0) == []


class TestIntervals:
    def test_from_runs_keeps_the_last_frame(self):
        intervals = slides.intervals_from_runs([(0, 3)], rate=1.0, duration=10.0)
        assert intervals[0]["frameIndex"] == 3
        assert intervals[0]["timeSec"] == 0.0
        assert intervals[0]["endTimeSec"] == 4.0

    def test_from_runs_clamps_to_duration(self):
        intervals = slides.intervals_from_runs([(0, 9)], rate=1.0, duration=5.0)
        assert intervals[0]["endTimeSec"] == 5.0

    def test_from_times_chains_each_to_the_next(self):
        intervals = slides.intervals_from_times([10.0, 20.0, 30.0], duration=40.0)
        assert [i["timeSec"] for i in intervals] == [10.0, 20.0, 30.0]
        assert intervals[0]["endTimeSec"] == 20.0
        assert intervals[1]["endTimeSec"] == 30.0
        assert intervals[2]["endTimeSec"] == 40.0

    def test_from_times_without_duration_leaves_the_last_open(self):
        intervals = slides.intervals_from_times([10.0])
        assert "endTimeSec" not in intervals[0]


# --------------------------------------------------------------------------- #
# the narration join
# --------------------------------------------------------------------------- #

class TestCuesInWindow:
    def _cues(self):
        return [
            Cue(index=1, start=0.0, end=4.0, text="first"),
            Cue(index=2, start=4.0, end=8.0, text="second"),
            Cue(index=3, start=8.0, end=12.0, text="third"),
        ]

    def test_window_collects_overlapping_cues(self):
        assert slides.cues_in_window(self._cues(), 0.0, 8.0) == "first second"

    def test_a_straddling_cue_belongs_to_the_earlier_slide(self):
        # The cue starting at 4.0 and ending at 8.0 overlaps [0, 8): the speaker
        # was still on the earlier slide.
        assert "second" in slides.cues_in_window(self._cues(), 0.0, 8.0)

    def test_an_open_window_runs_to_the_end(self):
        assert slides.cues_in_window(self._cues(), 8.0, None) == "third"

    def test_no_cues_is_empty(self):
        assert slides.cues_in_window([], 0.0, 10.0) == ""

    def test_max_chars_bounds_the_anchor(self):
        cues = [Cue(index=1, start=0.0, end=100.0, text="x" * 1000)]
        assert len(slides.cues_in_window(cues, 0.0, 100.0, max_chars=50)) == 50


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #

class TestNaming:
    def test_time_tag(self):
        assert slides.time_tag(3723.0) == "01-02-03"

    def test_frame_name(self):
        assert slides.frame_name(1, 83.0) == "001 - 00-01-23.png"


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

class TestManifest:
    def test_rows_carry_the_time_keys_and_the_anchor(self):
        rows = slides.build_rows([
            {"file": "001 - 00-01-23.png", "width": 1280, "height": 720,
             "timeSec": 83.0, "endTimeSec": 120.0, "anchor_text": "hello",
             "digest": "abc", "bytes": 10},
        ])
        row = rows[0]
        assert row["file"] == "001 - 00-01-23.png"
        assert row["timeSec"] == 83.0
        assert row["timecode"] == "00:01:23"
        assert row["endTimeSec"] == 120.0
        # The image pass reads this key, so the name must match the PDF manifest.
        assert row["anchor_text"] == "hello"
        # A slide has no page or bbox.
        assert "page" not in row and "bbox" not in row

    def test_write_sidecar_shape(self, tmp_path):
        rows = slides.build_rows([
            {"file": "001 - 00-00-00.png", "timeSec": 0.0, "anchor_text": "intro",
             "width": 10, "height": 10, "bytes": 1},
        ])
        manifest = slides.write_sidecar(str(tmp_path), rows, "video.mp4")
        payload = json.loads(open(manifest, encoding="utf-8").read())
        assert payload["count"] == 1
        assert payload["kind"] == "slides"
        assert os.path.basename(payload["source"]) == "video.mp4"
        assert payload["images"][0]["anchor_text"] == "intro"
        # The image pass and verify both require this sibling README.
        assert os.path.isfile(os.path.join(str(tmp_path), slides.SIDECAR_README))


def test_png_size_reads_the_ihdr(tmp_path):
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 7, 9, 8, 0, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IEND", b""))
    path = tmp_path / "x.png"
    path.write_bytes(png)
    assert slides.png_size(str(path)) == (7, 9)


def test_png_size_on_a_non_png_is_zero(tmp_path):
    path = tmp_path / "x.png"
    path.write_bytes(b"not a png at all")
    assert slides.png_size(str(path)) == (0, 0)

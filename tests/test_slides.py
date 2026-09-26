"""Unit tests for ``zoombie.lib.slides``.

Everything here is deterministic and needs no ffmpeg: timestamp parsing, the
grayscale-diff boundary detection, the perceptual-hash dedup, the interval/run
maths, the narration join and the manifest rendering. The ffmpeg decode itself is
native and lives in the self-test, exactly as ``test_pdf.py`` avoids opening a
real PDF.
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


# --------------------------------------------------------------------------- #
# the byte prefilter (a cost gate) -- it never drops a covered run
# --------------------------------------------------------------------------- #

class TestBytePrefilter:
    def test_a_near_empty_frame_is_dropped(self):
        reason = slides.flat_frame_reason(100_000)
        assert reason is not None and "below" in reason

    def test_a_text_slide_survives(self):
        # 590-960 KB is the measured text-slide band on the Crimson item.
        assert slides.flat_frame_reason(800_000) is None

    def test_the_floor_sits_below_the_measured_flat_band(self):
        """The default floor only decides where the OCR budget goes -- it drops nothing.

        Honest scope: the Crimson item's flat frames measured 200-470 KB and its
        suspect loop frames 590-960 KB, so the default 150 KB floor would not by
        itself separate them. Since D-3 the byte value is a cost gate only: a frame
        under it is KEPT and its OCR is skipped, never removed. A user who wants the
        measured band to cost nothing passes a higher -MinFrameBytes.
        """
        assert slides.DEFAULT_MIN_FRAME_BYTES < 200_000
        assert slides.flat_frame_reason(240_000) is None  # in the flat band, kept
        assert slides.flat_frame_reason(240_000, min_bytes=500_000) is not None

    def test_the_threshold_is_inclusive_at_the_floor(self):
        assert slides.flat_frame_reason(slides.DEFAULT_MIN_FRAME_BYTES) is None
        assert slides.flat_frame_reason(slides.DEFAULT_MIN_FRAME_BYTES - 1) is not None


class TestTextGate:
    def test_empty_ocr_text_is_dropped(self):
        reason = slides.low_text_reason("")
        assert reason is not None and "below" in reason

    def test_a_real_slide_title_passes(self):
        assert slides.low_text_reason("Дивиденды во время чумы") is None

    def test_none_means_the_gate_could_not_run_and_does_not_drop(self):
        # Tesseract absent: keep the frame rather than drop every candidate.
        assert slides.low_text_reason(None) is None

    def test_whitespace_only_is_dropped(self):
        assert slides.low_text_reason("   \n \t ") is not None


class TestManifestBytes:
    """Defect 6: ``bytes`` was never set, so the manifest column was permanently 0."""

    def test_rows_carry_the_byte_size(self):
        rows = slides.build_rows([
            {"file": "001 - x.png", "timeSec": 0.0, "anchor_text": "a",
             "width": 10, "height": 10, "bytes": 909_000},
        ])
        assert rows[0]["bytes"] == 909_000

    def test_a_record_without_bytes_is_zero_not_missing(self):
        rows = slides.build_rows([{"file": "001 - x.png", "timeSec": 0.0}])
        assert rows[0]["bytes"] == 0


# --------------------------------------------------------------------------- #
# D-1: the grayscale diff is the boundary signal; the dHash cannot see a fade
# --------------------------------------------------------------------------- #

class TestGrayscaleDiff:
    def test_identical_frames_have_no_diff(self):
        frame = bytes([7]) * slides.HASH_FRAME_BYTES
        assert slides.mean_abs_diff(frame, frame) == 0.0

    def test_a_uniform_fade_is_visible_to_the_diff(self):
        def frame(level: int) -> bytes:
            # A horizontal ramp offset by ``level``: a fade of the whole picture.
            base = bytes((x * 10 + level) % 256
                         for _ in range(8) for x in range(slides.HASH_FRAME_BYTES // 8))
            return base

        assert slides.mean_abs_diff(frame(0), frame(5)) == pytest.approx(5.0)

    def test_the_dhash_is_blind_to_a_uniform_fade(self):
        """This is WHY the boundary signal is a diff and not the dHash.

        A uniform level change leaves every neighbour comparison unchanged, so the
        two dHashes are identical while the images are measurably different. A
        boundary detector built on the dHash would merge the fade into one run.
        """
        def frame(level: int) -> bytes:
            return bytes((x * 10 + level) % 256
                         for _ in range(8) for x in range(slides.HASH_FRAME_BYTES // 8))

        assert slides.dhash_bits(frame(0)) == slides.dhash_bits(frame(9))
        assert slides.mean_abs_diff(frame(0), frame(9)) == pytest.approx(9.0)

    def test_mismatched_sizes_are_none_not_same(self):
        assert slides.mean_abs_diff(b"\x00" * 72, b"\x00" * 10) is None

    def test_diff_sequence_has_one_fewer_entry_than_samples(self):
        raw = b"".join(bytes([v]) * slides.HASH_FRAME_BYTES for v in (0, 0, 50))
        assert len(slides.diff_sequence(raw)) == 2

    def test_boundary_indices_find_the_change(self):
        # steady, steady, big jump, steady
        diffs = [0.0, 0.0, 40.0, 0.5]
        assert slides.boundary_indices(diffs, threshold=3.0) == [3]

    def test_an_unreadable_diff_is_a_boundary(self):
        # None means "cannot tell"; treating it as "same" would join two slides.
        assert slides.boundary_indices([0.0, None, 0.0], threshold=3.0) == [2]

    def test_runs_from_boundaries_cuts_and_filters_short_runs(self):
        # 8 samples, a boundary at 4 -> two 4-frame runs (4/0.25 = 16 s >= 2 s).
        assert slides.runs_from_boundaries(8, [4], 0.25, 2.0) == [(0, 3), (4, 7)]

    def test_a_flap_shorter_than_the_floor_is_dropped(self):
        # At 4 fps (rate=4) the floor is 8 gates, so a 2-frame run between two
        # boundaries is a flap and disappears; the long runs survive.
        runs = slides.runs_from_boundaries(30, [10, 12], 4.0, 2.0)
        assert (10, 11) not in runs
        assert (0, 9) in runs


class TestDefaultCadence:
    def test_the_boundary_cadence_is_quarter_fps(self):
        # Plan D-1: 0.25 fps, not the old 1.0 fps.
        assert slides.DEFAULT_SAMPLE_RATE == 0.25


# --------------------------------------------------------------------------- #
# D-4: the covered-run rule -- every run gets at least one frame
# --------------------------------------------------------------------------- #

class TestCoveredRuns:
    def test_every_short_run_contributes_its_last_frame(self):
        picks = slides.sample_indices([(0, 3), (4, 7)], rate=0.25, interval_seconds=30.0)
        assert picks == [(0, 3), (1, 7)]

    def test_one_run_always_yields_a_frame_as_a_pair(self):
        (run_index, frame_index), = slides.sample_indices([(0, 0)], 0.25)
        assert (run_index, frame_index) == (0, 0)

    def test_a_long_run_gets_interior_samples_plus_the_last(self):
        # A 41-frame run at 0.25 fps is 164 s; with a 30 s interval, one interior
        # sample every round(30 * 0.25) = 8 frames, plus the complete final frame.
        picks = slides.sample_indices([(0, 40)], rate=0.25, interval_seconds=30.0)
        indices = [frame for _run, frame in picks]
        assert indices[-1] == 40
        assert len(indices) > 1
        assert 40 in indices

    def test_no_index_is_repeated(self):
        picks = slides.sample_indices([(0, 80)], rate=0.25, interval_seconds=30.0)
        indices = [frame for _run, frame in picks]
        assert len(indices) == len(set(indices))


# --------------------------------------------------------------------------- #
# D-3: text is a REPORT, never a drop rule
# --------------------------------------------------------------------------- #

class TestTextReporting:
    def test_low_text_reason_is_worded_as_a_report_not_a_floor(self):
        reason = slides.low_text_reason("")
        assert reason is not None
        assert "reporting threshold" in reason
        assert "image-only" in reason

    def test_an_image_only_frame_is_flagged_not_dropped(self):
        # Subtask C: 13/96 frames are exactly 0 chars. The report says so and
        # nothing else -- there is no verdict a caller could use to drop it.
        report = slides.text_report("", min_chars=12, lang="eng+rus", promoted=True)
        assert report["likelyImageOnly"] is True
        assert report["chars"] == 0
        assert report["promoted"] is True

    def test_a_real_frame_is_not_flagged(self):
        report = slides.text_report("Дивиденды во время чумы", min_chars=12)
        assert report["likelyImageOnly"] is False
        assert report["script"] == "cyrillic"

    def test_no_ocr_text_is_unknown_not_dropped(self):
        report = slides.text_report(None, min_chars=12)
        # OCR could not run: unknown, not "image-only".
        assert report["likelyImageOnly"] is False
        assert report["reason"] is None

    def test_script_of_separates_the_scripts(self):
        assert slides.script_of("FIGURE 8") == "latin"
        assert slides.script_of("АСЧЕЕ 8") == "cyrillic"
        assert slides.script_of("АСЧЕЕ Figure") == "mixed"
        assert slides.script_of("") == "none"


class TestOcrArtifact:
    def test_write_ocr_artifact_shape(self, tmp_path):
        entries = [
            {"file": "001 - 00-00-00.png", "timeSec": 0.0, "runIndex": 0,
             "text": "", **slides.text_report("", min_chars=12, lang="eng+rus")},
            {"file": "002 - 00-00-04.png", "timeSec": 4.0, "runIndex": 1,
             "text": "a real slide with text",
             **slides.text_report("a real slide with text", min_chars=12)},
        ]
        path = slides.write_ocr_artifact(str(tmp_path), entries, source="v.mp4", lang="eng+rus")
        payload = json.loads(open(path, encoding="utf-8").read())
        # The text is a FILE, not the result payload (D-6).
        assert payload["kind"] == "slides-ocr"
        assert payload["count"] == 2
        assert payload["imagesOnly"] == 1
        assert payload["frames"][0]["text"] == ""


# --------------------------------------------------------------------------- #
# the selector command: covered-run, one OCR per run, dedup, dry-run
# --------------------------------------------------------------------------- #

from types import SimpleNamespace  # noqa: E402  (kept next to its tests)

from zoombie.commands import slides as slides_cmd  # noqa: E402


def _png(width: int = 160, height: int = 160) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def _candidate(run: int, frame: int, representative: bool, digest, time_sec: float | None = None) -> dict:
    return {"runIndex": run, "frameIndex": frame, "representative": representative,
            "timeSec": round(frame / 0.25, 3) if time_sec is None else time_sec,
            "anchor_text": "", "sourceHash": digest}


class TestDedupAcrossTheUnion:
    def test_a_representative_is_never_dropped_as_a_duplicate(self):
        # Two runs whose pictures hash the same: BOTH are covered, so both stay.
        kept, skipped = slides_cmd._dedup_candidates(
            [_candidate(0, 3, True, 0), _candidate(1, 7, True, 0)], distance=0
        )
        assert len(kept) == 2
        assert skipped == []

    def test_an_interior_sample_that_matches_its_run_is_dropped_with_reason(self):
        candidates = [_candidate(0, 3, True, 0), _candidate(0, 2, False, 0)]
        kept, skipped = slides_cmd._dedup_candidates(candidates, distance=0)
        assert [c["frameIndex"] for c in kept] == [3]
        assert skipped[0]["reason"] == "dedup"

    def test_a_distinct_interior_sample_is_kept(self):
        candidates = [_candidate(0, 3, True, 0), _candidate(0, 2, False, (1 << 64) - 1)]
        kept, _skipped = slides_cmd._dedup_candidates(candidates, distance=0)
        assert [c["frameIndex"] for c in kept] == [3, 2]


class TestGlobalDedup:
    """The OPT-IN pass that collapses non-sequential duplicates (talking heads)."""

    def test_a_repeated_representative_is_dropped(self):
        # Two runs, SAME picture: the within-run pass keeps both (covered run),
        # but the global pass keeps only the first.
        kept, skipped = slides_cmd._dedup_global(
            [_candidate(0, 3, True, 0), _candidate(1, 7, True, 0)], distance=0
        )
        assert [c["runIndex"] for c in kept] == [0]
        assert skipped[0]["reason"] == "global-dedup"

    def test_distinct_representatives_both_survive(self):
        kept, skipped = slides_cmd._dedup_global(
            [_candidate(0, 3, True, 0), _candidate(1, 7, True, (1 << 64) - 1)], distance=0
        )
        assert len(kept) == 2
        assert skipped == []

    def test_a_frame_with_no_hash_is_kept(self):
        """"Cannot tell" is never a duplicate -- an unreadable frame survives."""
        kept, _skipped = slides_cmd._dedup_global(
            [_candidate(0, 3, True, 0), _candidate(1, 7, True, None)], distance=0
        )
        assert len(kept) == 2


class TestExtractFrames:
    """Drive the real selector with ffmpeg/OCR stubbed out."""

    def _patch(self, monkeypatch, ocr_text: str):
        calls = {"write": 0, "ocr": 0}

        def fake_argv(_ffmpeg, _source, _seconds, output, _width):
            return ["ffmpeg", output]

        def fake_run_text(argv, **_kwargs):
            # The last argv entry is the output path; write a real PNG there so
            # slides.png_size reads a genuine IHDR.
            calls["write"] += 1
            with open(argv[-1], "wb") as handle:
                handle.write(_png())
            return 0, ""

        def fake_ocr(_path, _lang):
            calls["ocr"] += 1
            return ocr_text

        monkeypatch.setattr(slides_cmd.slides, "single_frame_argv", fake_argv)
        monkeypatch.setattr(slides_cmd.process, "run_text", fake_run_text)
        monkeypatch.setattr(slides_cmd.ocr, "ocr_image", fake_ocr)
        return calls

    def test_an_image_only_run_is_never_discarded_by_the_text_floor(self, tmp_path, monkeypatch):
        calls = self._patch(monkeypatch, ocr_text="")  # exactly what frame 002 returns
        records, skipped, entries, ocr_calls, _texts = slides_cmd._extract_frames(
            "ffmpeg", "v.mp4", str(tmp_path / "img"),
            [_candidate(0, 3, True, 0)], 1280, 64,
            min_frame_bytes=0, min_text_chars=12, ocr_usable=True, ocr_lang="eng+rus",
        )
        assert len(records) == 1                      # KEPT, despite zero text
        assert records[0]["representative"] is True
        assert not any(s["reason"] in ("low-text", "flat") for s in skipped)
        assert entries[0]["likelyImageOnly"] is True   # reported, not dropped
        assert ocr_calls == 1

    def test_one_ocr_call_per_run_not_per_sample(self, tmp_path, monkeypatch):
        calls = self._patch(monkeypatch, ocr_text="Дивиденды")
        # Five candidate frames of ONE run: distinct times, so distinct files.
        candidates = [
            _candidate(0, frame, frame == 4, None, time_sec=frame * 4.0)
            for frame in range(5)
        ]
        records, _skipped, _entries, ocr_calls, _texts = slides_cmd._extract_frames(
            "ffmpeg", "v.mp4", str(tmp_path / "img"), candidates, 1280, 64,
            min_frame_bytes=0, min_text_chars=12, ocr_usable=True, ocr_lang="eng+rus",
        )
        assert len(records) == 5        # every candidate is a frame
        assert ocr_calls == 1           # ... but ONE OCR call for the whole run
        assert calls["ocr"] == 1
        # Before the rework this same 5-sample run paid 5 OCR calls.

    def test_no_frame_is_analysed_twice(self, tmp_path, monkeypatch):
        calls = self._patch(monkeypatch, ocr_text="x")
        # Three candidates that share ONE sample index (frame 3), so they resolve
        # to the same frame: it must be extracted and analysed once, not three times.
        duplicate = _candidate(0, 3, True, 0)
        candidates = [dict(duplicate), dict(duplicate), dict(duplicate)]
        records, skipped, _entries, _ocr_calls, _texts = slides_cmd._extract_frames(
            "ffmpeg", "v.mp4", str(tmp_path / "img"), candidates, 1280, 64,
            min_frame_bytes=0, min_text_chars=12, ocr_usable=True, ocr_lang="eng+rus",
        )
        # The same second is extracted ONCE; the repeats are named, not re-analysed.
        assert len(records) == 1
        assert calls["write"] == 1
        assert all(entry["reason"] == "dedup" for entry in skipped)

    def test_reading_copy_does_not_add_ocr_calls(self, tmp_path, monkeypatch):
        """Plan §12: the q2/q3 score inherits the run's ONE OCR call (D-2 holds).

        Regression guard. Scoring the escalation from each frame's own text once
        relaxed the representative guard, so a default ``slides`` run (reading copies
        ON) paid one OCR call per FRAME -- 96 instead of 4 on the real Crimson item.
        The fix scores from the run representative's text instead, so this test
        asserts the call count is unchanged by the reading copy.
        """
        calls = self._patch(monkeypatch, ocr_text="Revenue 1,234 5,678")
        candidates = [
            _candidate(0, frame, frame == 4, None, time_sec=frame * 4.0)
            for frame in range(5)
        ]
        records, _skipped, _entries, ocr_calls, _texts = slides_cmd._extract_frames(
            "ffmpeg", "v.mp4", str(tmp_path / "img"), candidates, 1280, 64,
            min_frame_bytes=0, min_text_chars=12, ocr_usable=True, ocr_lang="eng+rus",
        )
        assert len(records) == 5
        assert ocr_calls == 1 and calls["ocr"] == 1     # one RUN, not one per frame
        # Every frame still carries a text, inherited from the run, so the §12
        # escalation is scored for all of them at no extra OCR cost.
        assert all(record["ocrText"] == "Revenue 1,234 5,678" for record in records)


class TestDryRun:
    def _args(self, source: str, output: str, **overrides) -> SimpleNamespace:
        values = {
            "source": source, "output": output, "image_dir": None, "times": None,
            "times_file": None, "srt": None, "scale": 1280, "sample_rate": 0.25,
            "diff_threshold": 3.0, "hash_distance": 8, "min_slide_seconds": 2.0,
            "sample_interval": 30.0, "min_px": 64, "min_frame_bytes": 150_000,
            "min_text_chars": 12, "no_text_gate": False, "lang": None,
            "dry_run": True, "force": False, "keep_work": False, "work_root": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        source = tmp_path / "media.mp4"
        source.write_bytes(b"\x00" * 32)
        output = tmp_path / "item"
        output.mkdir()

        class _Env:
            ffprobe = None

            def require(self, *_args, **_kwargs):
                return "ffmpeg"

        monkeypatch.setattr(slides_cmd.env_mod, "resolve", lambda: _Env())

        outcome = slides_cmd.run(self._args(str(source), str(output)))

        assert outcome.data["dryRun"] is True
        # No scratch, no sidecar, no OCR artifact -- nothing at all was written.
        assert not (output / "img").exists()
        assert list(output.iterdir()) == []


# --------------------------------------------------------------------------- #
# the agent's final keep/drop: the detector proposes, the agent decides
# --------------------------------------------------------------------------- #

class TestSelectionHelpers:
    """The pure half -- no ffmpeg -- so the id/timestamp handles are pinned."""

    def _records(self):
        return [
            {"timeSec": 10.0, "file": "001 - 00-00-10.png"},
            {"timeSec": 20.0, "file": "002 - 00-00-20.png"},
            {"timeSec": 30.0, "file": "003 - 00-00-30.png"},
        ]

    def test_frame_id_is_stable_and_zero_padded(self):
        assert slides.frame_id(1) == "f001"
        assert slides.frame_id(12) == "f012"

    def test_no_selection_keeps_everything(self):
        records = self._records()
        kept, dropped, unmatched = slides.apply_selection(records)
        assert kept == records and dropped == [] and unmatched == []

    def test_keep_is_an_allow_list_by_id(self):
        kept, dropped, unmatched = slides.apply_selection(self._records(), ["f002"])
        assert [r["timeSec"] for r in kept] == [20.0]
        assert [r["timeSec"] for r in dropped] == [10.0, 30.0]
        assert unmatched == []

    def test_a_frame_may_be_named_by_its_timestamp(self):
        # 00:00:20 -- the same handle the result's ``intervals`` reports.
        kept, _dropped, unmatched = slides.apply_selection(self._records(), ["00:00:20"])
        assert [r["timeSec"] for r in kept] == [20.0]
        assert unmatched == []

    def test_drop_removes_from_the_full_set(self):
        kept, dropped, unmatched = slides.apply_selection(self._records(), None, ["f001"])
        assert [r["timeSec"] for r in kept] == [20.0, 30.0]
        assert [r["timeSec"] for r in dropped] == [10.0]
        assert unmatched == []

    def test_an_unknown_handle_is_reported_not_silently_ignored(self):
        kept, _dropped, unmatched = slides.apply_selection(self._records(), ["f099"])
        assert kept == []            # an allow-list that matches nothing keeps nothing
        assert unmatched == ["f099"]

    def test_keep_also_sequences_the_survivors(self):
        kept, _dropped, _unmatched = slides.apply_selection(
            self._records(), ["f003", "f001"]
        )
        assert [r["timeSec"] for r in kept] == [30.0, 10.0]

    def test_parse_selection_splits_and_collapses(self):
        assert slides.parse_selection("f001, f002 f001\nf003") == ["f001", "f002", "f003"]
        assert slides.parse_selection("") == []


class TestSelectionCommand:
    """Drive ``slides`` in timestamps mode with ffmpeg/OCR stubbed out."""

    def _args(self, source: str, output: str, work_root: str, **overrides):
        values = {
            "source": source, "output": output, "image_dir": None,
            "times": "00:10,00:20,00:30", "times_file": None, "srt": None,
            "scale": 1280, "sample_rate": 0.25, "diff_threshold": 3.0,
            "hash_distance": 8, "min_slide_seconds": 2.0, "sample_interval": 30.0,
            "min_px": 0, "min_frame_bytes": 0, "min_text_chars": 12,
            "no_text_gate": False, "lang": None, "dry_run": False, "force": True,
            "keep_work": False, "keep_scratch": False, "work_root": work_root,
            "attach_limit": None, "no_reading_copy": True,
            "keep": None, "drop": None, "keep_file": None, "drop_file": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _patch(self, monkeypatch):
        class Env:
            ffprobe = None

            def require(self, *_a, **_k):
                return "ffmpeg"

        def fake_argv(_ffmpeg, _source, _seconds, output, _width):
            return ["ffmpeg", output]

        def fake_run_text(argv, **_kwargs):
            with open(argv[-1], "wb") as handle:
                handle.write(_png())
            return 0, ""

        monkeypatch.setattr(slides_cmd.env_mod, "resolve", lambda: Env())
        monkeypatch.setattr(slides_cmd.slides, "single_frame_argv", fake_argv)
        monkeypatch.setattr(slides_cmd.process, "run_text", fake_run_text)
        monkeypatch.setattr(slides_cmd.ocr, "ocr_image", lambda *_a, **_k: "text")

    def _run(self, tmp_path, monkeypatch, **overrides):
        self._patch(monkeypatch)
        source = tmp_path / "media.mp4"
        source.write_bytes(b"\x00" * 32)
        output = tmp_path / "item"
        output.mkdir()
        outcome = slides_cmd.run(
            self._args(str(source), str(output), str(tmp_path / "work"), **overrides)
        )
        return outcome, output

    def test_without_a_selection_every_proposed_frame_is_kept(self, tmp_path, monkeypatch):
        outcome, _out = self._run(tmp_path, monkeypatch)
        assert outcome.data["images"]["proposed"] == 3
        assert outcome.data["images"]["count"] == 3
        assert outcome.data["images"]["imageIds"] == ["f001", "f002", "f003"]
        assert outcome.data["images"]["selection"]["applied"] is False

    def test_keep_narrows_the_frames_written_and_the_manifest(self, tmp_path, monkeypatch):
        outcome, output = self._run(tmp_path, monkeypatch, keep="f002")
        assert outcome.data["images"]["count"] == 1
        assert [f["id"] for f in outcome.data["visionFrames"]] == ["f002"]
        selection = outcome.data["images"]["selection"]
        assert selection["applied"] is True
        assert selection["keep"] == ["f002"]
        assert selection["dropped"] == 2
        assert selection["droppedIds"] == ["f001", "f003"]
        # The manifest on disk carries ONLY the kept frame -- one figure, not three.
        import json

        manifest = json.loads(
            (output / "img" / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["count"] == 1
        # ... and the dropped PNGs were never written to the published directory.
        pngs = sorted(p.name for p in (output / "img").glob("*.png"))
        assert len(pngs) == 1

    def test_the_proposed_ids_are_reported_even_when_the_agent_kept_one(self, tmp_path, monkeypatch):
        outcome, _out = self._run(tmp_path, monkeypatch, keep="f003")
        # The ids are stable across runs: the id read in the FIRST result is the
        # handle selectable in the SECOND, whichever frames were kept.
        assert outcome.data["images"]["imageIds"] == ["f001", "f002", "f003"]
        assert [f["id"] for f in outcome.data["visionFrames"]] == ["f003"]

    def test_drop_removes_named_noise(self, tmp_path, monkeypatch):
        outcome, _out = self._run(tmp_path, monkeypatch, drop="f002")
        assert outcome.data["images"]["count"] == 2
        assert [f["id"] for f in outcome.data["visionFrames"]] == ["f001", "f003"]

    def test_an_unknown_id_is_refused_by_name(self, tmp_path, monkeypatch):
        from zoombie.lib.errors import ZoombieError

        with pytest.raises(ZoombieError) as excinfo:
            self._run(tmp_path, monkeypatch, keep="f099")
        assert "f099" in str(excinfo.value)

    def test_a_path_is_not_a_handle(self, tmp_path, monkeypatch):
        """The agent names frames by id, never by path: a path is an unknown handle."""
        from zoombie.lib.errors import ZoombieError

        with pytest.raises(ZoombieError):
            self._run(tmp_path, monkeypatch, drop="img/002 - 00-00-20.png")

    def test_the_attach_list_carries_the_id(self, tmp_path, monkeypatch):
        outcome, _out = self._run(tmp_path, monkeypatch)
        ids = [entry["id"] for entry in outcome.data["next"]["attach"]]
        assert ids == ["f001", "f002", "f003"]

"""Tests for the compressed reading copies (plan §12).

The parameters are Subtask H's measured ones and must not drift: the default is
ffmpeg ``-q:v 3`` (NOT PIL ``quality=3``), the source resolution is kept (no
upscale), the PNG stays the deliverable, and the quality escalates to ``-q:v 2``
only for a frame whose OCR text carries dense small numerals.

H's measured numeric-token counts are the anchor: 032 = 100, 094 = 80, 096 = 48
(escalate) against 085 = 11 and 010 = 0 (stay). The threshold therefore sits inside
the 11..48 gap at :data:`zoombie.lib.reading.DENSE_NUMERIC_TOKENS` = 20.
"""

from __future__ import annotations

import os
import struct
import zlib

import pytest

from zoombie.lib import next as next_mod, paths, reading


# H's own numbers, so the threshold is asserted against the measurement, not a guess.
H_DENSE = {"032": 100, "094": 80, "096": 48}
H_SPARSE = {"085": 11, "010": 0}


def _png() -> bytes:
    """A minimal but genuine 160x160 PNG, so ``png_size`` reads a real IHDR."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 160, 160, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


class TestNumericTokenCount:
    """The metric is H's own (measure6.py), so the threshold is in H's units."""

    def test_it_matches_hs_definition(self):
        # Runs of digits with inner ,/. separators; a bare digit is a token.
        assert reading.count_numeric_tokens("23,264 and 4.66 and 8") == 3
        assert reading.count_numeric_tokens("9092 54 4929") == 3

    def test_empty_and_none_are_zero(self):
        assert reading.count_numeric_tokens("") == 0
        assert reading.count_numeric_tokens(None) == 0

    def test_prose_with_a_few_figures(self):
        # The 085 case: prose that mentions a couple of numbers.
        assert reading.count_numeric_tokens("Дивиденды выросли на 7% в 2020 году") == 2


class TestEscalationRule:
    """The rule is deterministic and anchored on H's measured separation."""

    @pytest.mark.parametrize("stem,tokens", list(H_DENSE.items()))
    def test_a_dense_frame_matching_hs_count_escalates(self, stem, tokens):
        text = " ".join(str(n) for n in range(tokens))
        quality, reason = reading.quality_for(text)
        assert reading.count_numeric_tokens(text) == tokens
        assert quality == reading.DENSE_QUALITY == 2, stem
        assert "escalates" in reason and "numeric tokens" in reason

    @pytest.mark.parametrize("stem,tokens", list(H_SPARSE.items()))
    def test_a_prose_or_poster_frame_stays_at_the_default(self, stem, tokens):
        text = " ".join(str(n) for n in range(tokens))
        quality, reason = reading.quality_for(text)
        assert quality == reading.DEFAULT_QUALITY == 3, stem
        assert "stays at the default" in reason

    def test_the_threshold_is_inside_hs_gap(self):
        # The whole point: 20 is strictly between H's 11 (prose) and 48 (dense).
        assert max(H_SPARSE.values()) < reading.DENSE_NUMERIC_TOKENS < min(H_DENSE.values())

    def test_the_boundary_is_exactly_the_constant(self):
        below = " ".join(str(n) for n in range(reading.DENSE_NUMERIC_TOKENS - 1))
        at = " ".join(str(n) for n in range(reading.DENSE_NUMERIC_TOKENS))
        assert reading.quality_for(below)[0] == reading.DEFAULT_QUALITY
        assert reading.quality_for(at)[0] == reading.DENSE_QUALITY

    def test_unavailable_ocr_text_defaults_to_q3_and_says_so(self):
        quality, reason = reading.quality_for(None)
        assert quality == reading.DEFAULT_QUALITY
        assert "unavailable" in reason
        # An empty string is a REAL zero-numeral measurement, not "unavailable".
        assert "unavailable" not in reading.quality_for("")[1]


class TestPlan:
    """``plan`` is the pure half -- no file is touched, no tool is run."""

    def test_it_routes_each_frame_by_its_own_text(self):
        plan = reading.plan([
            {"file": "032.png", "ocrText": " ".join(str(n) for n in range(100))},
            {"file": "085.png", "ocrText": " ".join(str(n) for n in range(11))},
            {"file": "010.png", "ocrText": ""},
        ])
        assert plan["032.png"]["quality"] == reading.DENSE_QUALITY
        assert plan["032.png"]["escalated"] is True
        assert plan["085.png"]["quality"] == reading.DEFAULT_QUALITY
        assert plan["010.png"]["quality"] == reading.DEFAULT_QUALITY
        assert plan["010.png"]["tokens"] == 0

    def test_a_frame_without_ocr_text_defaults_to_q3(self):
        plan = reading.plan([{"file": "x.png"}])
        assert plan["x.png"]["quality"] == reading.DEFAULT_QUALITY
        assert plan["x.png"]["tokens"] is None

    def test_summary_counts_by_quality(self):
        plan = reading.plan([
            {"file": "a.png", "ocrText": " ".join(str(n) for n in range(50))},
            {"file": "b.png", "ocrText": "one two"},
            {"file": "c.png", "ocrText": ""},
        ])
        summary = reading.summary(plan)
        assert summary["qualities"]["2"] == 1
        assert summary["qualities"]["3"] == 2
        assert summary["escalated"] == 1
        assert summary["routed"] == 3


class TestNaming:
    def test_the_reading_name_keeps_the_quality_and_never_looks_like_a_png(self):
        name = reading.reading_name("032 - 01-01-34.png", 2)
        assert name == "032 - 01-01-34.q2.jpg"
        assert not name.lower().endswith(".png")

    def test_is_reading_copy_recognises_our_names_only(self):
        assert reading.is_reading_copy("032 - 01-01-34.q2.jpg")
        assert reading.is_reading_copy("page-001.q3.jpeg")
        assert not reading.is_reading_copy("032 - 01-01-34.png")
        assert not reading.is_reading_copy("manifest.json")
        assert not reading.is_reading_copy("holiday.jpg")

    def test_the_reading_dir_is_a_subdirectory_of_the_image_dir(self):
        assert reading.reading_dir("C:/item/.data/img") == os.path.join(
            "C:/item/.data/img", "readings"
        )


class TestMakeCopies:
    """The encoding half, with ffmpeg's runner stubbed so no binary is needed."""

    def _stub_ffmpeg(self, tmp_path) -> str:
        fake = tmp_path / "ffmpeg.exe"
        fake.write_bytes(b"")
        return str(fake)

    def test_it_writes_one_copy_per_frame_at_the_planned_quality(self, tmp_path):
        source = tmp_path / "img"
        source.mkdir()
        (source / "001 - 00-00-00.png").write_bytes(_png())
        (source / "002 - 00-00-04.png").write_bytes(_png())
        seen: list[tuple[str, str]] = []

        def runner(argv, **_kwargs):
            # The last argv entry is the destination; -q:v precedes its value.
            dest = argv[-1]
            quality = argv[argv.index("-q:v") + 1]
            seen.append((os.path.basename(dest), quality))
            with open(dest, "wb") as handle:
                handle.write(b"\xff\xd8" + bytes.fromhex("11") * 200 + b"\xff\xd9")
            return 0, ""

        plan = reading.plan([
            {"file": "001 - 00-00-00.png", "ocrText": " ".join(str(n) for n in range(60))},
            {"file": "002 - 00-00-04.png", "ocrText": "plain prose"},
        ])
        result = reading.make_copies(
            str(source), str(tmp_path / "out"), plan,
            ffmpeg=self._stub_ffmpeg(tmp_path), runner=runner,
        )
        assert result["available"] is True
        assert result["count"] == 2
        assert set(result["byFrame"]) == {"001 - 00-00-00.png", "002 - 00-00-04.png"}
        assert result["byFrame"]["001 - 00-00-00.png"]["quality"] == reading.DENSE_QUALITY
        assert result["byFrame"]["002 - 00-00-04.png"]["quality"] == reading.DEFAULT_QUALITY
        assert sorted(seen) == [("001 - 00-00-00.q2.jpg", "2"), ("002 - 00-00-04.q3.jpg", "3")]
        # ``bytes`` is the copy's own size and the total sums them.
        assert result["bytes"] == sum(
            entry["bytes"] for entry in result["byFrame"].values()
        )

    def test_no_upscale_appears_in_the_argv(self):
        argv = reading._encode_argv("ffmpeg", "in.png", "out.jpg", 2)
        assert "-vf" not in argv and "scale" not in " ".join(argv)
        assert argv[argv.index("-q:v") + 1] == "2"
        assert argv[-1] == "out.jpg"

    def test_an_unresolvable_ffmpeg_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reading.tools, "resolve", lambda _name: None)
        result = reading.make_copies(
            str(tmp_path), str(tmp_path / "out"),
            reading.plan([{"file": "x.png", "ocrText": "1"}]),
        )
        assert result["available"] is False
        assert result["byFrame"] == {}
        assert result["failed"][0]["reason"] == "ffmpeg-unavailable"

    def test_an_empty_plan_does_nothing(self, tmp_path):
        result = reading.make_copies(str(tmp_path), str(tmp_path / "out"), {})
        assert result["count"] == 0 and result["available"] is False


class TestPrune:
    def test_it_removes_only_our_stale_copies(self, tmp_path):
        d = tmp_path / "readings"
        d.mkdir()
        (d / "001.q3.jpg").write_bytes(b"a")
        (d / "002.q2.jpg").write_bytes(b"b")
        (d / "hand-curated.jpg").write_bytes(b"c")
        (d / "notes.txt").write_bytes(b"d")

        removed = reading.prune(str(d), keep={"001.q3.jpg"})
        assert removed == 1
        assert (d / "001.q3.jpg").exists()
        assert not (d / "002.q2.jpg").exists()
        # Nothing foreign is ever touched.
        assert (d / "hand-curated.jpg").exists()
        assert (d / "notes.txt").exists()

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert reading.prune(str(tmp_path / "absent"), keep=set()) == 0


class TestIndexCopies:
    def test_it_maps_a_png_stem_to_its_copy(self, tmp_path):
        d = tmp_path / "readings"
        d.mkdir()
        (d / "001 - 00-00-00.q2.jpg").write_bytes(b"a")
        (d / "002 - 00-00-04.q3.jpg").write_bytes(b"b")
        index = reading.index_copies(str(d))
        assert index["001 - 00-00-00"] == str(d / "001 - 00-00-00.q2.jpg")
        assert index["002 - 00-00-04"] == str(d / "002 - 00-00-04.q3.jpg")

    def test_a_missing_directory_yields_nothing(self, tmp_path):
        assert reading.index_copies(str(tmp_path / "absent")) == {}


# --------------------------------------------------------------------------- #
# the command wire-up
# --------------------------------------------------------------------------- #

def _slide_args(source: str, output: str, work_root: str, **overrides):
    from types import SimpleNamespace

    values = {
        "source": source, "output": output, "image_dir": None, "times": "00:01,00:02",
        "times_file": None, "srt": None, "scale": 1280, "sample_rate": 0.25,
        "diff_threshold": 3.0, "hash_distance": 8, "min_slide_seconds": 2.0,
        "sample_interval": 30.0, "min_px": 0, "min_frame_bytes": 0,
        "min_text_chars": 12, "no_text_gate": False, "lang": None,
        "dry_run": False, "force": True, "keep_work": False, "keep_scratch": False,
        "work_root": work_root, "attach_limit": None, "no_reading_copy": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_slides(monkeypatch, tmp_path, ocr_text: str, jpeg_bytes: int = 512):
    """Stub ffmpeg frame extraction, OCR, and the reading-copy encoder."""
    from zoombie.commands import slides as slides_cmd

    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"")

    class Env:
        ffprobe = None

        def require(self, *_a, **_k):
            return str(fake_ffmpeg)

    def fake_argv(_ffmpeg, _source, _seconds, output, _width):
        return ["ffmpeg", output]

    def fake_run_text(argv, **_kwargs):
        with open(argv[-1], "wb") as handle:
            handle.write(_png())
        return 0, ""

    real_make = reading.make_copies

    def stub_make(source_dir, dest_dir, planned, *, ffmpeg=None, runner=None, timeout=300):
        def run(argv, **_kwargs):
            dest = argv[-1]
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(b"\xff\xd8" + b"J" * jpeg_bytes + b"\xff\xd9")
            return 0, ""

        return real_make(source_dir, dest_dir, planned, ffmpeg=str(fake_ffmpeg), runner=run)

    monkeypatch.setattr(slides_cmd.env_mod, "resolve", lambda: Env())
    monkeypatch.setattr(slides_cmd.slides, "single_frame_argv", fake_argv)
    monkeypatch.setattr(slides_cmd.process, "run_text", fake_run_text)
    monkeypatch.setattr(slides_cmd.ocr, "ocr_image", lambda *_a, **_k: ocr_text)
    monkeypatch.setattr(slides_cmd.reading, "make_copies", stub_make)


class TestSlidesReadingCopies:
    def _run(self, tmp_path, monkeypatch, ocr_text, **overrides):
        from zoombie.commands import slides as slides_cmd

        _patch_slides(monkeypatch, tmp_path, ocr_text)
        source = tmp_path / "media.mp4"
        source.write_bytes(b"\x00" * 32)
        output = tmp_path / "item"
        output.mkdir()
        outcome = slides_cmd.run(_slide_args(
            str(source), str(output), str(tmp_path / "work"), **overrides
        ))
        return outcome, output

    def test_dense_frames_escalate_and_prose_frames_do_not(self, tmp_path, monkeypatch):
        dense = " ".join(str(n) for n in range(60))
        outcome, _out = self._run(tmp_path, monkeypatch, ocr_text=dense)
        # Every frame is dense here, so every copy is q2.
        for frame in outcome.data["visionFrames"]:
            assert frame["path"].endswith(".q2.jpg")
        assert outcome.data["readingCopy"]["qualities"] == {"2": 2}

    def test_prose_text_stays_at_the_default_quality(self, tmp_path, monkeypatch):
        outcome, _out = self._run(tmp_path, monkeypatch, ocr_text="plain Russian prose")
        for frame in outcome.data["visionFrames"]:
            assert frame["path"].endswith(".q3.jpg")
        assert outcome.data["readingCopy"]["qualities"] == {"3": 2}

    def test_file_is_never_overwritten_by_the_reading_copy(self, tmp_path, monkeypatch):
        """H's structural constraint: ``frame["file"]`` stays the PNG name."""
        outcome, _out = self._run(tmp_path, monkeypatch, ocr_text="prose")
        for frame in outcome.data["visionFrames"]:
            # ``file`` is the PNG identity (the manifest/README/postprocess key)...
            assert frame["file"].lower().endswith(".png")
            # ...and the read path is the copy, carried on separate keys.
            assert frame["readingPath"].endswith(".jpg")
            assert frame["path"] == frame["readingPath"]
            assert frame["path"] != frame["file"]

    def test_budget_bytes_is_the_reading_copy_size_not_the_png(self, tmp_path, monkeypatch):
        outcome, output = self._run(tmp_path, monkeypatch, ocr_text="prose",
                                    jpeg_bytes=512)
        budget = outcome.data["next"]["budget"]
        png_total = sum(
            paths.file_size(os.path.join(output, ".data", "img", entry["file"]))
            for entry in outcome.data["next"]["attach"]
        )
        # The stub copies are 2 header bytes + 512 + 2 trailer = 516 B each.
        assert budget["bytes"] == 516 * budget["images"]
        assert budget["bytes"] != png_total
        # And the advertised path really is the JPEG that exists.
        for entry in outcome.data["next"]["attach"]:
            assert entry["path"].endswith(".jpg")
            assert paths.is_file(entry["path"])
            assert paths.file_size(entry["path"]) == 516

    def test_the_copies_live_beside_the_frames_in_their_own_subdirectory(
        self, tmp_path, monkeypatch
    ):
        outcome, output = self._run(tmp_path, monkeypatch, ocr_text="prose")
        img_dir = os.path.join(str(output), ".data", "img")
        reading_dir = reading.reading_dir(img_dir)
        assert paths.is_dir(reading_dir)
        assert outcome.data["readingCopy"]["dir"] == reading_dir
        # The copies are NOT among the frames.
        loose_jpgs = [
            name for name in os.listdir(img_dir) if name.lower().endswith(".jpg")
        ]
        assert loose_jpgs == []

    def test_the_manifest_readme_and_image_count_stay_png_only(self, tmp_path, monkeypatch):
        import json

        from zoombie.item import paths as item_paths

        outcome, output = self._run(tmp_path, monkeypatch, ocr_text="prose")
        img_dir = os.path.join(str(output), ".data", "img")
        manifest = json.loads(
            open(os.path.join(img_dir, "manifest.json"), encoding="utf-8").read()
        )
        assert all(row["file"].lower().endswith(".png") for row in manifest["images"])
        readme = open(os.path.join(img_dir, "README.md"), encoding="utf-8").read()
        assert ".jpg" not in readme
        assert item_paths.image_count(str(output)) == len(manifest["images"]) == 2

    def test_a_rerun_is_idempotent_and_leaves_no_stale_copies(self, tmp_path, monkeypatch):
        outcome, output = self._run(tmp_path, monkeypatch, ocr_text="prose")
        reading_dir = reading.reading_dir(
            os.path.join(str(output), ".data", "img")
        )
        first = sorted(os.listdir(reading_dir))
        # A second run over the same frame set must converge, not accumulate.
        from zoombie.commands import slides as slides_cmd

        second_outcome = slides_cmd.run(_slide_args(
            str(tmp_path / "media.mp4"), str(output), str(tmp_path / "work"),
        ))
        second = sorted(os.listdir(reading_dir))
        assert second == first
        assert second_outcome.data["readingCopy"]["count"] == 2

    def test_dry_run_writes_no_reading_directory(self, tmp_path, monkeypatch):
        from zoombie.commands import slides as slides_cmd

        _patch_slides(monkeypatch, tmp_path, ocr_text="prose")
        source = tmp_path / "media.mp4"
        source.write_bytes(b"\x00" * 32)
        output = tmp_path / "item"
        output.mkdir()
        slides_cmd.run(_slide_args(
            str(source), str(output), str(tmp_path / "work"), dry_run=True
        ))
        assert list(output.iterdir()) == []

    def test_no_reading_copy_advertises_the_pngs(self, tmp_path, monkeypatch):
        outcome, output = self._run(tmp_path, monkeypatch, ocr_text="prose",
                                    no_reading_copy=True)
        assert outcome.data["readingCopy"]["available"] is False
        assert "NoReadingCopy" in outcome.data["readingCopy"]["reason"]
        for frame in outcome.data["visionFrames"]:
            assert frame["path"].lower().endswith(".png")
            assert "readingPath" not in frame
        assert not paths.exists(
            reading.reading_dir(os.path.join(str(output), ".data", "img"))
        )

    def test_postprocess_inlines_the_png_not_the_copy(self, tmp_path, monkeypatch):
        """The deliverable stays the PNG; a copy can never reach summary.md."""
        import json

        outcome, output = self._run(tmp_path, monkeypatch, ocr_text="prose")
        img_dir = os.path.join(str(output), ".data", "img")
        manifest = json.loads(
            open(os.path.join(img_dir, "manifest.json"), encoding="utf-8").read()
        )
        # postprocess place_figures iterates manifest["images"] and links entry["file"].
        for row in manifest["images"]:
            assert row["file"].lower().endswith(".png")
            assert paths.is_file(os.path.join(img_dir, row["file"]))


class TestReadpdfReadingCopies:
    """``readpdf``'s rendered scans are pages with no OCR text, so q3 by default."""

    def _patch(self, tmp_path, monkeypatch):
        from zoombie.commands import readpdf

        scanned = tmp_path / "scanned.pdf"
        scanned.write_bytes(b"%PDF-1.4\n")
        helper = tmp_path / "extract_pdf.py"
        helper.write_text("# helper\n", encoding="utf-8")
        fake_ffmpeg = tmp_path / "ffmpeg.exe"
        fake_ffmpeg.write_bytes(b"")

        class Env:
            pdf_script = str(helper)
            ffmpeg = str(fake_ffmpeg)

        class Page:
            def __init__(self, n):
                self.page = n
                self.file = f"page-{n:03d}.png"
                self.width, self.height = 100, 140

        class Result:
            output = str(tmp_path / "converted.md")
            pages = 3
            ocr_used = False
            kept_scanned_pages = [1, 2, 3]
            image_count = 0
            images_manifest = None
            images_skipped: list = []
            vision_dir = str(tmp_path / "vision")
            vision_pages = [Page(1).__dict__, Page(2).__dict__, Page(3).__dict__]

        def fake_convert(_options):
            (tmp_path / "converted.md").write_text("# t\n", encoding="utf-8")
            (tmp_path / "vision").mkdir(exist_ok=True)
            for n in (1, 2, 3):
                (tmp_path / "vision" / f"page-{n:03d}.png").write_bytes(_png())
            return Result()

        real_make = reading.make_copies

        def stub_make(source_dir, dest_dir, planned, *, ffmpeg=None, runner=None, timeout=300):
            def run(argv, **_kwargs):
                dest = argv[-1]
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as handle:
                    handle.write(b"\xff\xd8" + b"J" * 400 + b"\xff\xd9")
                return 0, ""

            return real_make(source_dir, dest_dir, planned, ffmpeg=str(fake_ffmpeg), runner=run)

        monkeypatch.setattr(readpdf.env_mod, "resolve", lambda *_a, **_k: Env())
        monkeypatch.setattr(readpdf.pdf, "convert", fake_convert)
        monkeypatch.setattr(readpdf.reading, "make_copies", stub_make)
        return readpdf, scanned

    def _args(self, tmp_path, scanned, **overrides):
        import argparse

        values = dict(
            source=str(scanned), output=str(tmp_path / "out"), ocr=False, images=False,
            images_only=False, image_dir=None, min_px=64, min_pt=30, pages=None,
            lang="eng", work_root=str(tmp_path / "work"), keep_work=False,
            force=True, dry_run=False, attach_limit=2, dpi=200,
            vision_dir=str(tmp_path / "vision"), no_reading_copy=False,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_every_rendered_page_gets_a_q3_copy_and_the_budget_is_jpeg(self, tmp_path, monkeypatch):
        readpdf, scanned = self._patch(tmp_path, monkeypatch)
        outcome = readpdf.run(self._args(tmp_path, scanned))
        block = outcome.data["readingCopy"]
        assert block["available"] is True
        assert block["qualities"] == {"3": 3}
        assert block["count"] == 3
        # visionPages advertise the copy and its bytes.
        for entry in outcome.data["visionPages"]:
            assert entry["path"].endswith(".q3.jpg")
            assert paths.is_file(entry["path"])
        budget = outcome.data["next"]["budget"]
        assert budget["bytes"] == 404 * budget["images"]  # 2 + 400 + 2

    def test_no_reading_copy_advertises_the_png_scans(self, tmp_path, monkeypatch):
        readpdf, scanned = self._patch(tmp_path, monkeypatch)
        outcome = readpdf.run(self._args(tmp_path, scanned, no_reading_copy=True))
        assert outcome.data["readingCopy"]["available"] is False
        for entry in outcome.data["visionPages"]:
            assert entry["path"].endswith(".png")
        assert not paths.exists(
            reading.reading_dir(str(tmp_path / "vision"))
        )

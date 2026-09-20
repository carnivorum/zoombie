"""Tests for the PDF page-spec parser, image pipeline and result shape."""

from __future__ import annotations

import json
import os
import re

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

    def test_to_data_exposes_the_image_placement_keys(self):
        result = pdf.Result(
            output="x.md",
            images=r"C:\docs\img",
            images_manifest=os.path.join("C:\\docs\\img", pdf.SIDECAR_MANIFEST),
            image_count=7,
            images_placed=6,
            images_skipped=[{"reason": "glyph", "page": 2}, {"reason": "scanned-page", "page": 9}],
        )
        data = result.to_data()
        assert data["images"] == r"C:\docs\img"
        assert data["imagesManifest"].endswith(pdf.SIDECAR_MANIFEST)
        assert data["imageCount"] == 7
        assert data["imagesPlaced"] == 6
        assert [(e["reason"], e["page"]) for e in data["imagesSkipped"]] == [
            ("glyph", 2),
            ("scanned-page", 9),
        ]

    def test_image_fields_default_to_empty(self):
        data = pdf.Result(output="x.md").to_data()
        assert data["imagesManifest"] is None
        assert data["imageCount"] == 0
        assert data["imagesPlaced"] == 0
        assert data["imagesSkipped"] == []

    def test_images_skipped_is_copied_not_shared(self):
        skipped = [{"reason": "xref0", "page": 1}]
        result = pdf.Result(output="x.md", images_skipped=skipped)
        result.to_data()["imagesSkipped"].append({"reason": "bogus"})
        assert len(skipped) == 1


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

    def test_parser_accepts_the_image_placement_flags(self):
        parser = pdf.build_parser()
        args = parser.parse_args(
            [
                "--input", "in.pdf",
                "--output", "out.md",
                "--images-only",
                "--image-dir", "img",
                "--min-px", "80",
                "--min-pt", "40",
            ]
        )
        assert args.images_only is True
        assert args.image_dir == "img"
        assert args.min_px == 80
        assert args.min_pt == 40

    def test_image_thresholds_default_to_the_kb_constants(self):
        parser = pdf.build_parser()
        args = parser.parse_args(["--input", "a.pdf", "--output", "b.md"])
        assert args.images_only is False
        assert args.image_dir is None
        assert args.min_px == pdf.DEFAULT_MIN_PX == 64
        assert args.min_pt == pdf.DEFAULT_MIN_PT == 30

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


class FakePage:
    """Minimal stand-in for a PyMuPDF page (no PDF needed)."""

    def __init__(self, number: int, infos: list[dict] | None = None,
                 blocks: list[tuple] | None = None, text: str = "body text"):
        self.number = number
        self._infos = infos or []
        self._blocks = blocks or []
        self._text = text

    def get_image_info(self, xrefs: bool = False) -> list[dict]:
        return [dict(info) for info in self._infos]

    def get_text(self, kind: str = "text"):
        if kind == "blocks":
            return [
                (b[0], b[1], b[2], b[3], b[4], index, b[5])
                for index, b in enumerate(self._blocks)
            ]
        return self._text


class TestKeepImage:
    def test_glyph_tile_is_rejected(self):
        # 4x4 px: the rectangle is in the bbox, only the tuple went missing. This is
        # the 1081-byte font-atlas shape that filled the old -Images directory.
        assert pdf.keep_image({"width": 4, "height": 4, "bbox": (10, 10, 14, 14)}) is False

    def test_large_image_with_a_tiny_on_page_box_is_rejected(self):
        # A wide but flat rule: plenty of pixels, no on-page area.
        assert pdf.keep_image({"width": 900, "height": 3, "bbox": (10, 10, 590, 11)}) is False
        assert pdf.keep_image({"width": 900, "height": 600, "bbox": (10, 10, 20, 20)}) is False

    def test_sixty_px_image_is_kept_above_its_threshold(self):
        info = {"width": 60, "height": 60, "bbox": (10, 10, 210, 210)}
        assert pdf.keep_image(info, min_px=60, min_pt=30) is True

    def test_default_pixel_threshold_rejects_a_sixty_px_image(self):
        # The default threshold is 64 px, so 60x60 px sits just BELOW it and is
        # dropped; -MinPx 60 is how a caller keeps it.
        info = {"width": 60, "height": 60, "bbox": (10, 10, 210, 210)}
        assert pdf.keep_image(info) is False

    def test_pixel_threshold_boundary(self):
        box = (10, 10, 210, 210)
        assert pdf.keep_image({"width": 63, "height": 63, "bbox": box}) is False
        assert pdf.keep_image({"width": 64, "height": 64, "bbox": box}) is True

    def test_a_mapping_and_an_imageinfo_agree(self):
        record = pdf.ImageInfo(
            file="", page=1, xref=5, width=300, height=200,
            bbox=(10.0, 10.0, 310.0, 210.0),
        )
        assert pdf.keep_image(record) is True
        assert pdf.keep_image({"width": 300, "height": 200, "bbox": record.bbox}) is True

    def test_missing_size_is_rejected(self):
        assert pdf.keep_image({}) is False
        assert pdf.keep_image({"width": 0, "height": 0}) is False

    def test_no_bbox_still_uses_the_pixel_test(self):
        assert pdf.keep_image({"width": 200, "height": 200}) is True


class TestTextBlocks:
    def test_only_text_blocks_are_kept(self):
        page = FakePage(0, blocks=[
            (10.0, 10.0, 100.0, 20.0, "  Heading  ", 0),
            (0.0, 0.0, 500.0, 500.0, "an image block", 1),
            (10.0, 30.0, 100.0, 40.0, "   ", 0),
        ])
        blocks = pdf.text_blocks(page)
        assert [block["text"] for block in blocks] == ["Heading"]

    def test_block_geometry_is_normalized_to_floats(self):
        page = FakePage(0, blocks=[(1, 2, 3, 4, "x", 0)])
        assert pdf.text_blocks(page) == [
            {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0, "text": "x"}
        ]


class TestAnchorFor:
    BLOCKS = [
        {"x0": 0.0, "y0": 0.0, "x1": 200.0, "y1": 20.0, "text": "top block"},
        {"x0": 0.0, "y0": 40.0, "x1": 200.0, "y1": 60.0, "text": "closer"},
    ]

    def test_closest_block_above_wins(self):
        assert pdf.anchor_for((0.0, 100.0, 200.0, 300.0), self.BLOCKS) == "closer"

    def test_tie_is_broken_by_horizontal_overlap(self):
        blocks = [
            {"x0": 0.0, "y0": 10.0, "x1": 40.0, "y1": 60.0, "text": "left column"},
            {"x0": 300.0, "y0": 10.0, "x1": 500.0, "y1": 60.0, "text": "right column"},
        ]
        image = (310.0, 100.0, 490.0, 200.0)
        assert pdf.anchor_for(image, blocks) == "right column"

    def test_no_block_above_falls_back_to_the_carry(self):
        image = (0.0, 5.0, 200.0, 100.0)
        assert pdf.anchor_for(image, self.BLOCKS, carry="previous page tail") == "previous page tail"

    def test_no_blocks_and_no_carry_is_empty(self):
        assert pdf.anchor_for((0.0, 5.0, 200.0, 100.0), [], carry="") == ""


class TestCollectImages:
    SMALL_BOX = (10.0, 10.0, 210.0, 210.0)

    def info(self, xref: int, digest: str, box=SMALL_BOX, width: int = 200, height: int = 200):
        return {
            "xref": xref, "digest": digest, "width": width, "height": height, "bbox": box,
        }

    def test_records_are_sorted_top_to_bottom_then_left_to_right(self):
        page = FakePage(0, infos=[
            self.info(7, "b", box=(300.0, 400.0, 400.0, 500.0)),
            self.info(8, "a", box=(10.0, 20.0, 210.0, 220.0)),
            self.info(9, "c", box=(20.0, 20.0, 220.0, 220.0)),
        ])
        records = pdf.collect_images(page)
        # (20,20) sorts before (10,20) is false: sorted by y0 then x0, so the
        # left-hand (10,20) image comes first and (300,400) last.
        assert [record.xref for record in records] == [8, 9, 7]

    def test_xref_zero_is_skipped_with_a_reason(self):
        # get_image_info can report drawn images with xref == 0; extract_image(0)
        # raises "bad xref", so they must never receive a file name.
        page = FakePage(0, infos=[self.info(0, "inline"), self.info(11, "real")])
        skipped: list[dict] = []
        records = pdf.collect_images(page, skipped=skipped)
        assert [record.xref for record in records] == [11]
        assert [entry["reason"] for entry in skipped] == ["xref0"]
        assert skipped[0]["page"] == 1

    def test_duplicates_are_dropped_by_digest(self):
        page = FakePage(0, infos=[
            self.info(11, "cover", box=(10.0, 10.0, 210.0, 210.0)),
            self.info(11, "cover", box=(10.0, 300.0, 210.0, 500.0)),
        ])
        skipped: list[dict] = []
        records = pdf.collect_images(page, seen=set(), skipped=skipped)
        assert len(records) == 1
        assert [entry["reason"] for entry in skipped] == ["duplicate"]

    def test_dedupe_falls_back_to_xref_and_bbox_when_the_digest_is_missing(self):
        page = FakePage(0, infos=[
            self.info(11, "", box=(10.0, 10.0, 210.0, 210.0)),
            self.info(11, "", box=(10.0, 10.0, 210.0, 210.0)),
            self.info(11, "", box=(10.0, 300.0, 210.0, 500.0)),
        ])
        records = pdf.collect_images(page, seen=set())
        assert len(records) == 2

    def test_glyph_tiles_are_skipped_with_a_reason(self):
        page = FakePage(0, infos=[
            {"xref": 4, "digest": "tile", "width": 4, "height": 4, "bbox": (1.0, 1.0, 5.0, 5.0)},
        ])
        skipped: list[dict] = []
        assert pdf.collect_images(page, skipped=skipped) == []
        assert [entry["reason"] for entry in skipped] == ["glyph"]

    def test_anchor_and_page_title_come_from_the_text_blocks(self):
        page = FakePage(0,
            infos=[self.info(11, "d", box=(10.0, 300.0, 210.0, 500.0))],
            blocks=[
                (10.0, 10.0, 200.0, 20.0, "Slide title", 0),
                (10.0, 40.0, 200.0, 60.0, "Look at this chart:", 0),
            ])
        records = pdf.collect_images(page)
        assert records[0].anchor_text == "Look at this chart:"
        assert records[0].page_title == "Slide title"
        assert records[0].page == 1
        assert records[0].digest == "d"

    def test_carry_is_used_for_a_figure_at_the_top_of_a_page(self):
        page = FakePage(0,
            infos=[self.info(11, "d", box=(10.0, 5.0, 210.0, 200.0))],
            blocks=[(10.0, 300.0, 200.0, 320.0, "below the figure", 0)])
        records = pdf.collect_images(page, carry="last text of page 1")
        assert records[0].anchor_text == "last text of page 1"

    def test_a_broken_page_reports_an_error_instead_of_raising(self):
        class Broken(FakePage):
            def get_image_info(self, xrefs: bool = False):
                raise RuntimeError("bad page")

        skipped: list[dict] = []
        assert pdf.collect_images(Broken(2), skipped=skipped) == []
        assert skipped[0]["reason"] == "error"
        assert skipped[0]["page"] == 3


class TestCollectDocument:
    def test_digest_dedupe_spans_pages_and_carry_advances(self):
        class Doc:
            def __init__(self, pages):
                self.pages = pages

            def load_page(self, number):
                return self.pages[number]

        doc = Doc([
            FakePage(0, infos=[{
                "xref": 11, "digest": "cover", "width": 200, "height": 200,
                "bbox": (10.0, 100.0, 210.0, 300.0),
            }], blocks=[(10.0, 10.0, 200.0, 20.0, "page one title", 0)]),
            FakePage(1, infos=[{
                "xref": 11, "digest": "cover", "width": 200, "height": 200,
                "bbox": (10.0, 100.0, 210.0, 300.0),
            }, {
                "xref": 12, "digest": "chart", "width": 200, "height": 200,
                "bbox": (10.0, 5.0, 210.0, 200.0),
            }], blocks=[(10.0, 300.0, 200.0, 320.0, "page two footer", 0)]),
        ])
        records, skipped = pdf.collect_document(doc, [0, 1])
        assert [(record.page, record.xref) for record in records] == [(1, 11), (2, 12)]
        assert records[0].anchor_text == "page one title"
        # The second page's figure is above every block on its own page, so it
        # inherits page one's last text block.
        assert records[1].anchor_text == "page one title"
        assert [entry["reason"] for entry in skipped] == ["duplicate"]

    def test_a_scan_with_no_text_and_no_images_is_reported(self):
        class Doc:
            def load_page(self, number):
                return FakePage(number, infos=[], text="")

        records, skipped = pdf.collect_document(Doc(), [0])
        assert records == []
        assert skipped == [{"reason": "scanned-page", "page": 1}]


class TestWriteImages:
    class Doc:
        """Doc that hands back a PNG payload for one xref."""

        def __init__(self, payload: bytes, ext: str = "png", fail: bool = False):
            self.payload = payload
            self.ext = ext
            self.fail = fail

        def extract_image(self, xref: int) -> dict:
            if self.fail:
                raise RuntimeError(f"bad xref: {xref}")
            return {"image": self.payload, "ext": self.ext}

    def _pixmap(self):
        pymupdf = pytest.importorskip("pymupdf")
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64))
        pix.set_rect(pix.irect, (10, 20, 30))
        return pix

    def test_file_name_is_zero_padded_sequence_and_page(self, tmp_path):
        pix = self._pixmap()
        doc = type("D", (), {"extract_image": lambda self, xref: {
            "image": pix.tobytes("png"), "ext": "png",
        }, "__getitem__": lambda self, key: None})()
        records = [pdf.ImageInfo(file="", page=3, xref=11, width=64, height=64,
                                 bbox=(1.0, 2.0, 65.0, 66.0), anchor_text="a")]
        entries, skipped = pdf.write_images(doc, records, str(tmp_path))
        assert skipped == []
        assert entries[0]["file"] == "001 - p03.png"
        assert (tmp_path / entries[0]["file"]).is_file()
        assert entries[0]["bytes"] > 0

    def test_a_failed_image_is_reported_and_gets_no_manifest_entry(self, tmp_path):
        pix = self._pixmap()
        payload = pix.tobytes("png")

        class Mixed:
            def extract_image(self, xref: int) -> dict:
                if xref == 99:
                    raise RuntimeError("bad xref: 99")
                return {"image": payload, "ext": "png"}

        records = [
            pdf.ImageInfo(file="", page=1, xref=99, width=80, height=80, bbox=(0.0, 0.0, 80.0, 80.0)),
            pdf.ImageInfo(file="", page=1, xref=11, width=80, height=80, bbox=(0.0, 0.0, 80.0, 80.0)),
        ]
        entries, skipped = pdf.write_images(Mixed(), records, str(tmp_path))
        # Only the image that was actually written is in the manifest, and the
        # survivor keeps its reading-order number rather than being renumbered.
        assert [entry["file"] for entry in entries] == ["002 - p01.png"]
        assert [entry["reason"] for entry in skipped] == ["error"]
        assert skipped[0]["xref"] == 99
        assert os.listdir(str(tmp_path)) == ["002 - p01.png"]

    def test_name_collisions_do_not_overwrite(self, tmp_path):
        pix = self._pixmap()
        payload = pix.tobytes("png")
        doc = type("D", (), {"extract_image": lambda self, xref: {
            "image": payload, "ext": "png"}})()
        (tmp_path / "001 - p01.png").write_bytes(payload)
        records = [pdf.ImageInfo(file="", page=1, xref=11, width=64, height=64,
                                 bbox=(0.0, 0.0, 64.0, 64.0))]
        entries, _skipped = pdf.write_images(doc, records, str(tmp_path))
        assert entries[0]["file"] == "001 - p01 (2).png"


class TestImagesSidecar:
    ENTRIES = [
        pdf.ImageInfo(
            file="001 - p01.png", page=1, xref=11, width=300, height=200,
            bbox=(10.0, 20.0, 310.0, 220.0), anchor_text="See the figure below:",
            page_title="Intro", digest="abc", bytes=4096,
        ).to_manifest()
    ]

    def test_manifest_schema_and_readme_table(self, tmp_path):
        manifest = pdf.images_sidecar(str(tmp_path), self.ENTRIES, "source.pdf")
        assert os.path.basename(manifest) == "manifest.json"
        payload = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert payload["source"] == "source.pdf"
        assert payload["count"] == 1
        entry = payload["images"][0]
        assert sorted(entry) == sorted([
            "file", "page", "xref", "width", "height", "bbox",
            "anchor_text", "page_title", "digest", "bytes",
        ])
        assert entry["bbox"] == [10.0, 20.0, 310.0, 220.0]
        readme = (tmp_path / "README.md").read_text(encoding="utf-8")
        assert "001 - p01.png" in readme
        assert "See the figure below:" in readme

    def test_table_cells_cannot_break_the_table(self, tmp_path):
        entry = pdf.ImageInfo(
            file="001 - p01.png", page=1, xref=11, width=64, height=64,
            bbox=(0.0, 0.0, 64.0, 64.0), anchor_text="a | b" * 60, bytes=1,
        ).to_manifest()
        pdf.images_sidecar(str(tmp_path), [entry], "s.pdf")
        readme = (tmp_path / "README.md").read_text(encoding="utf-8")
        rows = [line for line in readme.splitlines() if line.startswith("| `")]
        assert len(rows) == 1
        # Counting only UNescaped pipes: 6 cells means 7 delimiters, so the pipe
        # inside the anchor text can never be mistaken for a cell boundary.
        assert len(re.findall(r"(?<!\\)\|", rows[0])) == 7
        assert "\\|" in rows[0]


class TestImagePipelineEndToEnd:
    """Build a real PDF with a drawn image and run the whole image path."""

    def _make_pdf(self, path: str) -> None:
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        try:
            page = doc.new_page()  # 595 x 842 pt
            page.insert_text((72, 72), "Look at the figure below:")
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 90))
            pix.set_rect(pix.irect, (200, 30, 40))
            page.insert_image(pymupdf.Rect(72, 90, 272, 240), pixmap=pix)
            doc.save(path)
        finally:
            doc.close()

    def test_images_only_writes_manifest_readme_and_files(self, tmp_path):
        source = str(tmp_path / "doc.pdf")
        self._make_pdf(source)
        images_dir = str(tmp_path / "img")

        result = pdf.convert(pdf.Options(
            input=source, output=str(tmp_path / "out.md"),
            images_only=True, images_dir=images_dir,
        ))

        assert result.image_count >= 1
        assert result.images_placed == result.image_count
        assert result.bytes == 0  # images-only renders no Markdown
        assert not (tmp_path / "out.md").exists()
        assert os.path.isfile(os.path.join(images_dir, pdf.SIDECAR_MANIFEST))
        assert os.path.isfile(os.path.join(images_dir, pdf.SIDECAR_README))

        payload = json.loads(
            (tmp_path / "img" / pdf.SIDECAR_MANIFEST).read_text(encoding="utf-8")
        )
        assert payload["count"] == result.image_count
        for entry in payload["images"]:
            assert os.path.isfile(os.path.join(images_dir, entry["file"]))
            assert entry["bytes"] > 0
            assert entry["page"] == 1
            assert entry["bbox"][2] > entry["bbox"][0]
        assert result.images_manifest == os.path.join(images_dir, pdf.SIDECAR_MANIFEST)

    def test_markdown_is_still_written_when_not_images_only(self, tmp_path):
        source = str(tmp_path / "doc.pdf")
        self._make_pdf(source)
        result = pdf.convert(pdf.Options(
            input=source, output=str(tmp_path / "out.md"),
            images=True, images_dir=str(tmp_path / "img"),
        ))
        assert os.path.isfile(str(tmp_path / "out.md"))
        assert result.bytes > 0
        assert "Look at the figure below" in (tmp_path / "out.md").read_text(encoding="utf-8")
        assert result.image_count >= 1

    def test_images_only_requires_a_directory(self, tmp_path):
        source = str(tmp_path / "doc.pdf")
        self._make_pdf(source)
        with pytest.raises(ValueError):
            pdf.convert(pdf.Options(
                input=source, output=str(tmp_path / "out.md"), images_only=True,
            ))

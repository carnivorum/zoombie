"""Tests for ``readimages``: the loose-image Markdown producer.

The vision path (no ``-Ocr``) is the important one to pin down: it must write the
image links and the sidecar and NO guessed text, and it must be usable on a
directory as well as a single file.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import zlib

import pytest

from zoombie.commands import readimages
from zoombie.lib.errors import ZoombieError


def _png(width: int = 4, height: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def _args(**overrides) -> argparse.Namespace:
    values = {
        "source": None,
        "output": None,
        "ocr": False,
        "lang": "eng",
        "image_dir": None,
        "dry_run": False,
        "force": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class TestCollectImages:
    def test_a_file_is_returned_as_is(self, tmp_path):
        image = tmp_path / "a.png"
        image.write_bytes(_png())
        assert readimages.collect_images(str(image)) == [str(image)]

    def test_a_directory_is_scanned_in_name_order(self, tmp_path):
        for name in ("b.png", "a.png", "c.jpg"):
            (tmp_path / name).write_bytes(_png())
        (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
        found = readimages.collect_images(str(tmp_path))
        assert [os.path.basename(p) for p in found] == ["a.png", "b.png", "c.jpg"]

    def test_a_missing_source_is_a_named_failure(self, tmp_path):
        with pytest.raises(ZoombieError):
            readimages.collect_images(str(tmp_path / "nope"))

    def test_an_empty_directory_is_a_named_failure(self, tmp_path):
        with pytest.raises(ZoombieError):
            readimages.run(_args(source=str(tmp_path), output=str(tmp_path / "out")))


class TestVisionOnly:
    def test_writes_links_sidecar_and_no_text(self, tmp_path):
        images = tmp_path / "in"
        images.mkdir()
        (images / "001.png").write_bytes(_png(7, 9))
        base = tmp_path / "doc"

        outcome = readimages.run(_args(source=str(images), output=str(base)))

        assert outcome.ok
        assert outcome.data["visionOnly"] is True
        md = (tmp_path / "doc.md").read_text(encoding="utf-8")
        # The image is linked and nothing is invented as its text.
        assert "![001.png]" in md
        assert "001.png" in md

        sidecar = os.path.join(str(base) + ".images", "manifest.json")
        payload = json.loads(open(sidecar, encoding="utf-8").read())
        assert payload["count"] == 1
        assert payload["images"][0]["width"] == 7
        assert payload["images"][0]["height"] == 9


class TestGuards:
    def test_refuses_to_overwrite_without_force(self, tmp_path):
        image = tmp_path / "a.png"
        image.write_bytes(_png())
        base = tmp_path / "doc"
        readimages.run(_args(source=str(image), output=str(base)))
        with pytest.raises(ZoombieError):
            readimages.run(_args(source=str(image), output=str(base)))

    def test_dry_run_writes_nothing(self, tmp_path):
        image = tmp_path / "a.png"
        image.write_bytes(_png())
        base = tmp_path / "doc"
        outcome = readimages.run(_args(source=str(image), output=str(base), dry_run=True))
        assert outcome.data["dryRun"] is True
        assert not (tmp_path / "doc.md").exists()

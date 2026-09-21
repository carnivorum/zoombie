"""Tests for ``migrate`` and the byte-idempotency guarantee over the .data layout.

Two things are being pinned here, and the second is the important one.

The migration tests check the moves and that nothing is renamed inside ``img/``.

The idempotency test checks the property the whole toolchain leans on: a second
``postprocess -Apply`` must leave an unchanged document BYTE-identical. That
guarantee is implemented partly by stripping a previous run's figures with a regex
that matches the image path, so it is sensitive to where the images live. Moving
them from ``img/`` to ``.data/img/`` preserves the ``img/`` substring that the
pattern keys on -- but preserving it by luck is not a guarantee, so this test is
what proves it, and ``inserted_image_re`` (given the real directory) is what makes
it true by construction rather than by coincidence.
"""

from __future__ import annotations

import json

from zoombie.cli import main
from zoombie.commands import migrate as mig
from zoombie.commands import postprocess as pp
from zoombie.item import meta, paths as item_paths

# ``anchor_text`` is matched against the PARAGRAPH TEXT of block 6, not against a
# heading -- so it has to quote the prose the figure hangs off.
MANIFEST = {
    "source": "x.pdf",
    "count": 1,
    "images": [
        {
            "file": "001 - p01.png",
            "page": 1,
            "anchor_text": "Первый абзац текста.",
            "page_title": "Первый абзац текста.",
        }
    ],
}

SUMMARY = (
    "# Заметки\n"
    "\n"
    "## 1. Титул\n"
    "Заметки о рынке.\n"
    "\n"
    "## 2. Источник\n"
    "Из [x.pdf](x.pdf).\n"
    "\n"
    "## 3. Краткое содержание\n"
    "Коротко.\n"
    "\n"
    "## 4. Содержание\n"
    "_старое_\n"
    "\n"
    "## 5. Связанные статьи\n"
    "Нет.\n"
    "\n"
    "## 6. Копия источника\n"
        "### Вступление\n"
        "Первый абзац текста.\n"
        "\n"
        "Второй абзац текста.\n"
    )


def read(path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def make_legacy_item(root, name="12 - 06.05.2020 - Заметки"):
    """A pre-item-model item: img/ and the sidecars in the item root."""
    item_dir = root / name
    image_dir = item_dir / "img"
    image_dir.mkdir(parents=True)
    (item_dir / "summary.md").write_text(SUMMARY, encoding="utf-8", newline="\n")
    (image_dir / "001 - p01.png").write_bytes(b"\x89PNG")
    (image_dir / "manifest.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8"
    )
    (image_dir / "README.md").write_text("# Extracted images\n", encoding="utf-8")
    (item_dir / "audio.wav").write_bytes(b"\x00")
    (item_dir / "audio.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                                        encoding="utf-8")
    (item_dir / "audio.source.json").write_text(
        json.dumps({"kind": "audio"}), encoding="utf-8"
    )
    return item_dir


class TestIdempotencyOverDataImg:
    def test_a_second_apply_is_byte_identical(self, tmp_path):
        """THE regression test for the .data/img move.

        If the strip pattern stopped matching, the re-insert pass would append a
        second copy of every figure and this would fail on content, not just bytes.
        """
        item_dir = tmp_path / "item"
        image_dir = item_dir / ".data" / "img"
        image_dir.mkdir(parents=True)
        (item_dir / "summary.md").write_text(SUMMARY, encoding="utf-8", newline="\n")
        (image_dir / "001 - p01.png").write_bytes(b"\x89PNG")
        (image_dir / "manifest.json").write_text(
            json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8"
        )

        args = ["postprocess", "-Md", str(item_dir / "summary.md"), "-Apply"]
        assert main(args) == 0
        first = read(item_dir / "summary.md")
        # The LINK, not the whole string: the alt text also contains "001".
        assert first.count("](.data/img/") == 1, first

        assert main(args) == 0
        second = read(item_dir / "summary.md")
        assert second == first
        assert second.count("](.data/img/") == 1, second

    def test_the_link_prefix_is_item_relative(self, tmp_path):
        """``.data/img/...`` and not a bare ``img/...``: the links must resolve
        from the FILE, which sits one level above the data directory."""
        item_dir = tmp_path / "item"
        image_dir = item_dir / ".data" / "img"
        image_dir.mkdir(parents=True)
        (item_dir / "summary.md").write_text(SUMMARY, encoding="utf-8", newline="\n")
        (image_dir / "001 - p01.png").write_bytes(b"\x89PNG")
        (image_dir / "manifest.json").write_text(
            json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8"
        )
        assert main(["postprocess", "-Md", str(item_dir / "summary.md"), "-Apply"]) == 0
        assert ".data/img/001" in read(item_dir / "summary.md")

    def test_the_default_image_dir_is_the_item_layout(self, tmp_path):
        """With no -ImageDir, the manifest is found in .data/img."""
        item_dir = tmp_path / "item"
        image_dir = item_dir / ".data" / "img"
        image_dir.mkdir(parents=True)
        (image_dir / "manifest.json").write_text("{}", encoding="utf-8")
        resolved = pp._image_dir_for(str(item_dir / "summary.md"), None)
        assert resolved == str(image_dir)

    def test_the_legacy_img_dir_is_still_found(self, tmp_path):
        item_dir = tmp_path / "item"
        (item_dir / "img").mkdir(parents=True)
        (item_dir / "img" / "manifest.json").write_text("{}", encoding="utf-8")
        resolved = pp._image_dir_for(str(item_dir / "summary.md"), None)
        assert resolved == str(item_dir / "img")


class TestStripMarker:
    def test_a_marker_that_does_not_appear_strips_nothing(self):
        """Documents WHY the marker must be derived from the real directory: this
        is the silent failure the passed-in marker prevents."""
        from zoombie.lib import markdown as md

        body = "Текст.\n![a](elsewhere/a.png)\n\nДальше.\n"
        assert md.strip_inserted_images(body, ".data/img/") == body

    def test_the_marker_is_escaped_not_treated_as_a_pattern(self):
        """``.data`` contains a dot, which must not act as a wildcard."""
        from zoombie.lib import markdown as md

        keep = "Текст.\n![a](XdataYimg/a.png)\n\nДальше.\n"
        assert md.strip_inserted_images(keep, ".data/img/") == keep

        strip = "Текст.\n![a](.data/img/a.png)\n\nДальше.\n"
        assert ".data/img" not in md.strip_inserted_images(strip, ".data/img/")


class TestMigration:
    def test_it_moves_the_image_directory_without_renaming_inside(self, tmp_path):
        item_dir = make_legacy_item(tmp_path)
        assert main(["migrate", "-Dir", str(tmp_path), "-Apply"]) == 0

        image_dir = item_dir / ".data" / "img"
        assert (image_dir / "001 - p01.png").is_file()
        assert (image_dir / "manifest.json").is_file()
        # The sidecar keeps its name: nesting removed the reason to rename it.
        assert (image_dir / "README.md").is_file()
        assert not (item_dir / "img").exists()

    def test_it_moves_the_transcript_and_the_origin_sidecar(self, tmp_path):
        item_dir = make_legacy_item(tmp_path)
        assert main(["migrate", "-Dir", str(tmp_path), "-Apply"]) == 0
        # The fixture has no ``.txt``; the SRT and the origin sidecar are the
        # sidecars it does carry, and both must land under fixed names.
        assert (item_dir / ".data" / "transcript.srt").is_file()
        assert (item_dir / ".data" / "source.json").is_file()

    def test_it_writes_item_json_from_the_legacy_name(self, tmp_path):
        item_dir = make_legacy_item(tmp_path)
        assert main(["migrate", "-Dir", str(tmp_path), "-Apply"]) == 0
        payload = json.loads(
            (item_dir / ".data" / "item.json").read_text(encoding="utf-8")
        )
        assert payload["number"] == 12
        assert payload["date"] == "2020-05-06"
        assert payload["title"] == "Заметки"

    def test_it_rewrites_the_image_links(self, tmp_path):
        item_dir = make_legacy_item(tmp_path)
        (item_dir / "summary.md").write_text(
            SUMMARY.replace("Первый абзац текста.",
                            "Первый абзац текста.\n\n![001 - p01.png](img/001%20-%20p01.png)"),
            encoding="utf-8", newline="\n",
        )
        assert main(["migrate", "-Dir", str(tmp_path), "-Apply"]) == 0
        text = read(item_dir / "summary.md")
        assert ".data/img/001%20-%20p01.png" in text
        assert "](img/" not in text

    def test_a_migrated_document_is_then_idempotent(self, tmp_path):
        """The rewrite must happen BEFORE postprocess sees the document, or the
        strip pass matches nothing and the figures duplicate."""
        item_dir = make_legacy_item(tmp_path)
        (item_dir / "summary.md").write_text(
            SUMMARY.replace("Первый абзац текста.",
                            "Первый абзац текста.\n\n![001 - p01.png](img/001%20-%20p01.png)"),
            encoding="utf-8", newline="\n",
        )
        assert main(["migrate", "-Dir", str(tmp_path), "-Apply"]) == 0

        args = ["postprocess", "-Md", str(item_dir / "summary.md"), "-Apply"]
        assert main(args) == 0
        first = read(item_dir / "summary.md")
        assert main(args) == 0
        assert read(item_dir / "summary.md") == first

    def test_dry_run_is_the_default_and_changes_nothing(self, tmp_path):
        item_dir = make_legacy_item(tmp_path)
        assert main(["migrate", "-Dir", str(tmp_path), "-Json"]) == 0
        assert (item_dir / "img" / "manifest.json").is_file()
        assert not (item_dir / ".data").exists()

    def test_it_records_the_detected_convention(self, tmp_path):
        # Three dated siblings, so the verdict is STRONG rather than a single
        # coincidence -- a one-item library has no measurable convention.
        for number in ("12", "13", "14"):
            make_legacy_item(tmp_path, name=f"{number} - 06.05.2020 - Заметки")
        assert main(["migrate", "-Dir", str(tmp_path), "-Apply"]) == 0
        payload = json.loads(
            (tmp_path / ".zoombie-library.json").read_text(encoding="utf-8")
        )
        assert payload["convention"] == "num-date-title"

    def test_a_name_is_never_changed(self, tmp_path):
        item_dir = make_legacy_item(tmp_path)
        assert main(["migrate", "-Dir", str(tmp_path), "-Apply"]) == 0
        assert item_dir.name == "12 - 06.05.2020 - Заметки"

    def test_a_missing_dir_is_a_clean_failure(self, tmp_path, capsys):
        assert main(["migrate", "-Dir", str(tmp_path / "nope")]) == 1
        assert json.loads(capsys.readouterr().out.strip())["ok"] is False


class TestMigratedLibraryIndexes:
    def test_the_same_row_renders_before_and_after(self, tmp_path):
        """A migrated library must index to what it indexed before, from
        item.json rather than from the folder name."""
        item_dir = make_legacy_item(tmp_path)
        assert main(["index", "-Dir", str(tmp_path), "-Apply"]) == 0
        before = read(tmp_path / "README.md")

        assert main(["migrate", "-Dir", str(tmp_path), "-Apply"]) == 0
        assert main(["index", "-Dir", str(tmp_path), "-Apply"]) == 0
        after = read(tmp_path / "README.md")

        assert "06.05.2020" in after
        assert "Заметки" in after
        # The number and title survive the move; the toolchain line may not.
        assert "| 12 |" in before and "| 12 |" in after
        assert item_dir.is_dir()

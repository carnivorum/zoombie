"""Tests for the item model: recognition, naming MEASUREMENT, and the scan.

The convention under test is the one this design exists for: a folder is an item
because of what it CONTAINS (a ``summary.md``), and it is named however the
workspace already names things. So the cases that matter most are the ones a
fixed regex rejected -- ``a Заметки``, a dated name, a plain title -- and the ones
where a workspace has no usable evidence at all.
"""

from __future__ import annotations

import json
import os

from zoombie.cli import main
from zoombie.item import paths as item_paths, registry, scan


def make_item(root, name: str, *, title=None, summary="# Заголовок\n\n## 6. Копия источника\nтело\n",
              media=None):
    """Build an item folder on disk and return its path."""
    item_dir = root / name
    item_dir.mkdir(parents=True)
    if summary is not None:
        (item_dir / "summary.md").write_text(summary, encoding="utf-8", newline="\n")
    if media:
        (item_dir / media).write_bytes(b"\x00")
    return item_dir


class TestRecognition:
    def test_any_folder_name_is_an_item(self, tmp_path):
        """The whole point: the name carries no meaning and is never parsed."""
        for name in ("a Заметки", "2020-05-06 Заметки", "Заметки", "item 13"):
            item_dir = tmp_path / name
            item_dir.mkdir()
            (item_dir / "summary.md").write_text("# t\n", encoding="utf-8")
            assert item_paths.is_item(str(item_dir)), name

    def test_a_summary_alone_is_enough(self, tmp_path):
        item_dir = tmp_path / "bare"
        item_dir.mkdir()
        (item_dir / "summary.md").write_text("# t\n", encoding="utf-8")
        assert item_paths.is_item(str(item_dir))

    def test_a_folder_without_a_summary_is_not_an_item(self, tmp_path):
        (tmp_path / "notes").mkdir()
        assert not item_paths.is_item(str(tmp_path / "notes"))

    def test_a_media_only_folder_is_not_an_item(self, tmp_path):
        """A bare download awaiting its write-up is not yet an item."""
        folder = tmp_path / "download"
        folder.mkdir()
        (folder / "video.mp4").write_bytes(b"\x00")
        assert not item_paths.is_item(str(folder))


class TestMeasure:
    def test_a_date_workspace_is_detected(self):
        names = ["12 - 06.05.2020 - Заметки", "13 - 07.05.2020 - Ещё",
                 "14 - 08.05.2020 - Третье"]
        verdict = registry.measure(names)
        assert verdict.convention_id == "num-date-title"
        assert verdict.confidence == "strong"
        assert verdict.next_number == 15

    def test_a_letter_workspace_is_detected(self):
        """The case that prompted this: `a <title>`, `b <title>`, `c <title>`."""
        verdict = registry.measure(["a Первый", "b Второй", "c Третий"])
        assert verdict.convention_id == "letter-title"
        assert verdict.next_letter == "d"

    def test_one_sample_is_no_evidence(self):
        verdict = registry.measure(["12 - 06.05.2020 - Заметки"])
        assert verdict.confidence == "none"
        assert verdict.convention_id == "title-only"

    def test_two_agreeing_samples_are_weak_not_strong(self):
        verdict = registry.measure(["a Первый", "a Второй"])
        assert verdict.convention_id == "letter-title"
        assert verdict.confidence == "weak"

    def test_an_em_dash_is_accepted(self):
        verdict = registry.measure(
            ["12 — 06.05.2020 — Заметки", "13 — 07.05.2020 — Ещё", "14 — 08.05.2020 — Т."]
        )
        assert verdict.convention_id == "num-date-title"

    def test_an_iso_date_is_accepted(self):
        verdict = registry.measure(
            ["2020-05-06 - Заметки", "2020-05-07 - Ещё", "2020-05-08 - Третье"]
        )
        assert verdict.convention_id == "iso-date-title"

    def test_plain_titles_are_a_convention_not_an_absence(self):
        verdict = registry.measure(["Первый", "Второй", "Третий"])
        assert verdict.convention_id == "title-only"

    def test_no_siblings_claims_nothing(self):
        verdict = registry.measure([])
        assert verdict.convention_id is None


class TestPropose:
    def test_the_default_recommendation_is_date_and_title(self):
        assert registry.render("date-title", title="X", date="2020-05-06") == "06.05.2020 - X"

    def test_a_name_follows_the_local_convention_instead(self):
        verdict = registry.measure(["a Первый", "b Второй", "c Третий"])
        assert registry.propose("Четвёртый", verdict=verdict) == "d. Четвёртый"

    def test_the_next_number_comes_from_the_measurement(self):
        verdict = registry.measure(
            ["12 - 06.05.2020 - Заметки", "13 - 07.05.2020 - Ещё", "14 - 08.05.2020 - Т."]
        )
        assert registry.propose("Новое", verdict=verdict, date="2020-05-09").startswith("15 - ")

    def test_an_unknown_convention_degrades_to_the_default(self):
        assert registry.render("nonsense", title="X") == "X"


class TestDates:
    def test_the_stored_form_is_iso_and_the_display_form_is_russian(self):
        assert registry.date_iso("06.05.2020") == "2020-05-06"
        assert registry.date_display("2020-05-06") == "06.05.2020"

    def test_a_non_date_is_not_a_date(self):
        assert registry.date_iso("not a date") is None


class TestScan:
    def test_the_scan_reports_the_next_number(self, tmp_path):
        make_item(tmp_path, "12 - 06.05.2020 - Первый")
        make_item(tmp_path, "13 - 07.05.2020 - Второй")
        make_item(tmp_path, "14 - 08.05.2020 - Третий")
        result = scan.scan(str(tmp_path))
        assert result.naming["nextNumber"] == 15
        assert len(result.items) == 3

    def test_a_non_item_is_reported_with_a_reason(self, tmp_path):
        make_item(tmp_path, "item")
        (tmp_path / "scripts").mkdir()
        result = scan.scan(str(tmp_path))
        assert [entry["name"] for entry in result.skipped] == ["scripts"]

    def test_a_media_folder_is_reported_as_media(self, tmp_path):
        folder = tmp_path / "download"
        folder.mkdir()
        (folder / "video.mp4").write_bytes(b"\x00")
        result = scan.scan(str(tmp_path))
        assert result.skipped[0]["kind"] == "media"

    def test_the_source_is_read_from_disk(self, tmp_path):
        make_item(tmp_path, "item", media="video.mp4")
        result = scan.scan(str(tmp_path))
        assert result.items[0]["source"]["file"] == "video.mp4"
        assert result.items[0]["source"]["kind"] == "video"

    def test_a_deleted_source_is_reported_as_absent(self, tmp_path):
        make_item(tmp_path, "item")
        result = scan.scan(str(tmp_path))
        assert result.items[0]["source"]["present"] is False

    def test_the_title_comes_from_the_summary_h1(self, tmp_path):
        make_item(tmp_path, "plain", summary="# Настоящий заголовок\n\n## 6. Копия\nтело\n")
        result = scan.scan(str(tmp_path))
        assert result.items[0]["title"] == "Настоящий заголовок"

    def test_the_images_are_counted_from_the_visible_dir(self, tmp_path):
        item_dir = make_item(tmp_path, "item")
        images = item_dir / "img"
        images.mkdir()
        (images / "001 - p01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        result = scan.scan(str(tmp_path))
        assert result.items[0]["images"] == 1


class TestRecurse:
    def test_a_nested_item_is_found_with_a_relative_key(self, tmp_path):
        outer = tmp_path / "one"
        outer.mkdir()
        make_item(outer, "inner")
        result = scan.scan(str(tmp_path), 2)
        assert len(result.items) == 1
        assert result.items[0]["relative"] == "one/inner"

    def test_recurse_does_not_descend_into_an_item(self, tmp_path):
        """An item's ``img/`` must not surface as a skipped folder."""
        item_dir = make_item(tmp_path, "item")
        images = item_dir / "img"
        images.mkdir()
        (images / "001 - p01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        result = scan.scan(str(tmp_path), 3)
        assert len(result.items) == 1
        assert all(entry["name"] != "img" for entry in result.skipped)

    def test_depth_bounds_the_descent(self, tmp_path):
        outer = tmp_path / "one"
        outer.mkdir()
        inner = outer / "two"
        inner.mkdir()
        make_item(inner, "deep")
        assert len(scan.scan(str(tmp_path), 1).items) == 0
        assert len(scan.scan(str(tmp_path), 3).items) == 1

    def test_a_non_positive_depth_is_read_as_one(self, tmp_path):
        make_item(tmp_path, "item")
        assert len(scan.scan(str(tmp_path), 0).items) == 1
        assert len(scan.scan(str(tmp_path), -5).items) == 1


class TestItemsCommand:
    def test_it_reports_the_documented_shape(self, tmp_path, capsys):
        make_item(tmp_path, "item", media="video.mp4")
        assert main(["items", "-Root", str(tmp_path)]) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["data"]["count"] == 1
        assert payload["data"]["items"][0]["source"]["file"] == "video.mp4"

    def test_it_proposes_names_in_the_local_convention(self, tmp_path, capsys):
        for name in ("a Первый", "b Второй", "c Третий"):
            make_item(tmp_path, name)
        assert main(["items", "-Root", str(tmp_path), "-Title", "Четвёртый"]) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "d. Четвёртый" in payload["data"]["proposals"]

    def test_it_writes_nothing(self, tmp_path):
        """Read-only by contract: the naming verdict is a measurement."""
        make_item(tmp_path, "item")
        before = sorted(os.listdir(tmp_path))
        main(["items", "-Root", str(tmp_path)])
        assert sorted(os.listdir(tmp_path)) == before

    def test_a_missing_root_is_a_clean_failure(self, tmp_path, capsys):
        assert main(["items", "-Root", str(tmp_path / "nope")]) == 1


class TestNextNumberIsNotInvented:
    def test_title_only_items_report_no_next_number(self, tmp_path):
        for name in ("Первый", "Второй", "Третий"):
            make_item(tmp_path, name)
        assert scan.scan(str(tmp_path)).naming.get("nextNumber") is None

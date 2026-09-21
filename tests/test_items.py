"""Tests for the item model: recognition, naming MEASUREMENT, and item.json.

The convention under test is the one this redesign exists for: a folder is an item
because of what it CONTAINS, and it is named however the workspace already names
things. So the cases that matter most are the ones the old single regex rejected --
``a Заметки``, a dated name, a plain title -- and the ones where a workspace has no
usable evidence at all.
"""

from __future__ import annotations

import json

from zoombie.cli import main
from zoombie.commands import items as items_cmd
from zoombie.item import meta, paths as item_paths, registry, scan


def make_item(root, name: str, *, number=None, date=None, title=None,
              summary="# Заголовок\n\n## 6. Копия источника\nтело\n", media=None):
    """Build an item folder on disk and return its path."""
    item_dir = root / name
    (item_dir / ".data").mkdir(parents=True)
    if summary is not None:
        (item_dir / "summary.md").write_text(summary, encoding="utf-8", newline="\n")
    if number is not None or date is not None or title is not None:
        meta.write(item_dir, number=number, date=date, title=title or name)
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

    def test_a_data_dir_alone_is_enough(self, tmp_path):
        item_dir = tmp_path / "bare"
        (item_dir / ".data").mkdir(parents=True)
        assert item_paths.is_item(str(item_dir))

    def test_a_plain_folder_is_not_an_item(self, tmp_path):
        (tmp_path / "notes").mkdir()
        assert not item_paths.is_item(str(tmp_path / "notes"))

    def test_dot_data_is_not_counted_as_a_sibling(self, tmp_path):
        """A detector that counted our own internals would see a convention in
        every single item folder."""
        make_item(tmp_path, "первый")
        assert registry.child_names(str(tmp_path)) == ["первый"]


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
        """The separator the project's own prose uses, which the old pattern
        rejected -- so a 'tidied' name silently stopped being an item."""
        verdict = registry.measure(
            ["12 — 06.05.2020 — Заметки", "13 — 07.05.2020 — Ещё",
             "14 — 08.05.2020 — Третье"]
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
        """With no evidence the toolchain recommends a date, as asked -- but it
        does not IMPOSE it, because nothing is written."""
        name = registry.propose("Заметки", date="2020-05-06")
        assert name == "06.05.2020 - Заметки"

    def test_a_name_follows_the_local_convention_instead(self):
        verdict = registry.measure(["a Первый", "b Второй", "c Третий"])
        assert registry.propose("Четвёртый", verdict=verdict) == "d. Четвёртый"

    def test_the_next_number_comes_from_the_measurement(self):
        verdict = registry.measure(
            ["12 - 06.05.2020 - Заметки", "13 - 07.05.2020 - Ещё",
             "18 - 08.05.2020 - Третье"]
        )
        assert verdict.next_number == 19
        assert registry.propose("Новое", verdict=verdict, date="2020-05-09") == \
            "19 - 09.05.2020 - Новое"

    def test_an_unknown_convention_degrades_to_the_default(self):
        assert registry.render("nonsense", title="X") == "X"


class TestDates:
    def test_the_stored_form_is_iso_and_the_display_form_is_russian(self):
        assert registry.date_iso("06.05.2020") == "2020-05-06"
        assert registry.date_display("2020-05-06") == "06.05.2020"

    def test_iso_sorts_chronologically_where_the_display_form_did_not(self):
        """The old code sorted DD.MM.YYYY strings, so 01.06 sorted before 31.12."""
        stored = sorted(["31.12.2019", "01.06.2020"], key=registry.date_iso)
        assert stored == ["31.12.2019", "01.06.2020"]

    def test_a_non_date_is_not_a_date(self):
        assert registry.date_iso("not a date") is None


class TestItemJson:
    def test_written_metadata_wins_over_the_folder_name(self, tmp_path):
        make_item(tmp_path, "any name", number=7, date="2020-05-06", title="Заметки")
        item_dir = tmp_path / "any name"
        resolved = meta.fields(str(item_dir), "any name")
        assert (resolved["number"], resolved["date"], resolved["title"]) == \
            (7, "2020-05-06", "Заметки")
        assert resolved["stored"]

    def test_the_legacy_name_backfills_metadata(self, tmp_path):
        """A pre-item.json library must still index, and migrate."""
        item_dir = tmp_path / "12 - 06.05.2020 - Заметки"
        item_dir.mkdir()
        (item_dir / "summary.md").write_text("# Заметки\n", encoding="utf-8")
        resolved = meta.fields(str(item_dir), item_dir.name)
        assert resolved["number"] == 12
        assert resolved["date"] == "2020-05-06"
        assert resolved["title"] == "Заметки"
        assert not resolved["stored"]

    def test_the_summary_h1_is_the_last_resort_for_a_title(self, tmp_path):
        item_dir = make_item(tmp_path, "plain", summary="# Настоящий заголовок\n")
        resolved = meta.fields(str(item_dir), "plain")
        assert resolved["title"] == "Настоящий заголовок"

    def test_a_corrupt_sidecar_is_ignored_not_trusted(self, tmp_path):
        item_dir = make_item(tmp_path, "broken", number=1, date="2020-05-06", title="T")
        (item_dir / ".data" / "item.json").write_text("{not json", encoding="utf-8")
        assert meta.read(str(item_dir)) is None


class TestScan:
    def test_the_scan_reports_the_next_number(self, tmp_path):
        make_item(tmp_path, "первый", number=1, date="2020-05-06", title="Первый")
        make_item(tmp_path, "второй", number=2, date="2020-05-07", title="Второй")
        result = scan.scan(str(tmp_path))
        assert result.next_number == 3
        assert len(result.items) == 2

    def test_confidence_is_capped_when_nothing_found_is_an_item(self, tmp_path):
        """A directory of ordinary source folders is 'plainly named' too.

        Reporting that as STRONG would have the toolchain confidently proposing
        names for a folder that holds no items -- which is exactly what `zoombie
        items -Root .` did on this repository until this cap existed.
        """
        for name in ("scripts", "tests", "plans"):
            (tmp_path / name).mkdir()
        naming = scan.scan(str(tmp_path)).naming
        assert naming["convention"] == "title-only"
        assert naming["confidence"] == "weak"
        assert naming["itemCount"] == 0
        assert naming["cappedBecauseNoItems"] is True

    def test_real_items_keep_full_confidence(self, tmp_path):
        for name in ("Первый", "Второй", "Третий"):
            make_item(tmp_path, name, number=None, date=None, title=name)
        naming = scan.scan(str(tmp_path)).naming
        assert naming["confidence"] == "strong"
        assert naming["itemCount"] == 3
        assert "cappedBecauseNoItems" not in naming

    def test_a_non_item_is_reported_with_a_reason(self, tmp_path):
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        (tmp_path / "notes").mkdir()
        result = scan.scan(str(tmp_path))
        assert [entry["name"] for entry in result.skipped] == ["notes"]
        assert result.skipped[0]["reason"]

    def test_a_stray_high_number_does_not_drive_the_successor(self, tmp_path):
        """Siblings that are not items must not push the next number to 100."""
        make_item(tmp_path, "item", number=3, date="2020-05-06", title="A")
        (tmp_path / "99 - not an item").mkdir()
        result = scan.scan(str(tmp_path))
        assert result.next_number == 4

    def test_the_source_is_read_from_disk_not_from_metadata(self, tmp_path):
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A",
                  media="video.mp4")
        item = scan.scan(str(tmp_path)).items[0]
        assert item["source"]["file"] == "video.mp4"
        assert item["source"]["kind"] == "video"
        assert item["source"]["present"] is True

    def test_a_deleted_source_is_reported_as_absent(self, tmp_path):
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        assert scan.scan(str(tmp_path)).items[0]["source"]["present"] is False

    def test_an_unnumbered_item_sorts_last(self, tmp_path):
        make_item(tmp_path, "with number", number=1, date="2020-05-06", title="A")
        make_item(tmp_path, "without number", number=None, date=None, title="B")
        items = scan.scan(str(tmp_path)).items
        assert items[-1]["name"] == "without number"


class TestConfidenceIsAShare:
    """Strong confidence needs the items to be a real share of the samples.

    The repository case that prompted this: ``zoombie items -Root .`` on this repo
    reports a ``title-only`` convention for ``scripts``/``tests``/``plans`` -- and
    the same "convention" would be read from nine source folders beside one real
    item, where a mere "zero items" cap could not help. Below half the samples the
    verdict describes our own directories, so it is downgraded and the reason is
    recorded the way ``cappedBecauseNoItems`` is.
    """

    def test_majority_items_keep_full_confidence(self, tmp_path):
        for index, name in enumerate(("Первый", "Второй"), start=1):
            make_item(tmp_path, name, number=None, date=None, title=name)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "tests").mkdir()
        naming = scan.scan(str(tmp_path)).naming
        assert naming["itemCount"] == 2
        assert naming["confidence"] == "strong"
        assert "cappedBecauseFewItems" not in naming

    def test_the_boundary_is_inclusive_at_half(self, tmp_path):
        """Exactly half the samples being items is enough -- the rule is >= half."""
        make_item(tmp_path, "item one", number=None, date=None, title="One")
        make_item(tmp_path, "item two", number=None, date=None, title="Two")
        (tmp_path / "scripts").mkdir()
        (tmp_path / "tests").mkdir()
        naming = scan.scan(str(tmp_path)).naming
        assert naming["confidence"] == "strong"

    def test_a_minority_of_items_is_downgraded_with_a_reason(self, tmp_path):
        """Nine source folders plus one real item: the convention is the folders'."""
        make_item(tmp_path, "Единственное", number=None, date=None, title="Одно")
        for name in ("scripts", "tests", "plans", "modes", "docs",
                     "assets", "tools", "vendor", "extra"):
            (tmp_path / name).mkdir()
        naming = scan.scan(str(tmp_path)).naming
        assert naming["confidence"] == "weak"
        assert naming["itemCount"] == 1
        assert naming["cappedBecauseFewItems"] is True

    def test_no_items_still_caps_with_its_own_reason(self, tmp_path):
        """The older cap is unchanged and keeps its distinct marker."""
        for name in ("scripts", "tests", "plans"):
            (tmp_path / name).mkdir()
        naming = scan.scan(str(tmp_path)).naming
        assert naming["confidence"] == "weak"
        assert naming["cappedBecauseNoItems"] is True
        assert "cappedBecauseFewItems" not in naming

    def test_the_repository_case_stays_weak(self, tmp_path):
        """``scripts``/``tests``/``plans`` -- this repo's own shape -- must not
        read as a strong convention."""
        for name in ("scripts", "tests", "plans"):
            (tmp_path / name).mkdir()
        assert scan.scan(str(tmp_path)).naming["confidence"] == "weak"


class TestRecurse:
    """``scan(root, depth=N)``: the newest, least-exercised path.

    The rule under test is that a directory which is ITSELF an item is reported and
    not descended into -- an item's ``.data/`` is its internals, not a nested
    workspace -- which is also what stops a recursive scan from reporting every
    image directory as a skippable folder.
    """

    def test_a_nested_item_is_found_with_a_relative_key(self, tmp_path):
        make_item(tmp_path, "top", number=1, date="2020-05-06", title="Top")
        (tmp_path / "collection").mkdir()
        make_item(tmp_path / "collection", "inner", number=2,
                  date="2020-05-07", title="Inner")

        result = scan.scan(str(tmp_path), depth=2)
        names = [item["name"] for item in result.items]
        assert "top" in names and "inner" in names
        nested = next(item for item in result.items if item["name"] == "inner")
        # Forward slashes, whatever the platform: the value is printed.
        assert nested["relative"] == "collection/inner"

        # The nested item must count toward the totals. The one-level pass computed
        # them over the immediate children, so a deeper scan that forgot to
        # recompute would report count=1 and nextNumber=2 here -- and would hand
        # out a number it had already given to a nested item.
        assert result.naming["itemCount"] == 2
        assert result.next_number == 3

    def test_recurse_does_not_descend_into_an_item(self, tmp_path):
        """An item's ``.data/img`` must not surface as a skipped folder."""
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        image_dir = tmp_path / "item" / ".data" / "img"
        image_dir.mkdir(parents=True)
        (image_dir / "001 - p01.png").write_bytes(b"\x89PNG")

        result = scan.scan(str(tmp_path), depth=3)
        assert [entry["name"] for entry in result.skipped] == []
        assert [item["name"] for item in result.items] == ["item"]

    def test_depth_bounds_the_descent(self, tmp_path):
        outer = tmp_path / "one"
        inner = outer / "two"
        inner.mkdir(parents=True)
        make_item(tmp_path, "shallow", number=1, date="2020-05-06", title="S")
        make_item(inner, "deep", number=2, date="2020-05-07", title="D")

        # depth=2 reaches one level down, not two: the item two folders deep is
        # missed, and stopping there is the point of the bound.
        shallow_scan = scan.scan(str(tmp_path), depth=2)
        assert "deep" not in [item["name"] for item in shallow_scan.items]

        deeper = scan.scan(str(tmp_path), depth=3)
        assert "deep" in [item["name"] for item in deeper.items]

    def test_a_recursive_scan_reports_no_image_directory_as_skippable(self, tmp_path):
        """The same invariant across several items: no ``.data``/``img`` leaks in."""
        for index, name in enumerate(("Первый", "Второй"), start=1):
            item_dir = make_item(tmp_path, name, number=index,
                                 date="2020-05-06", title=name)
            (item_dir / ".data" / "img").mkdir(parents=True, exist_ok=True)

        result = scan.scan(str(tmp_path), depth=3)
        assert result.skipped == []
        assert len(result.items) == 2

    def test_a_three_level_nesting_is_reached_exactly_at_its_depth(self, tmp_path):
        """A non-item folder between the root and the item costs one level each.

        ``a/b/item`` is three levels below the root, so ``depth=3`` finds it and
        ``depth=2`` does not -- the boundary is the point, because the depth is a
        user-supplied flag and an off-by-one silently loses items.
        """
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        make_item(deep, "item", number=1, date="2020-05-06", title="Deep")

        assert [i["name"] for i in scan.scan(str(tmp_path), depth=2).items] == []
        assert [i["name"] for i in scan.scan(str(tmp_path), depth=3).items] == ["item"]

    def test_depth_counts_levels_below_the_root(self, tmp_path):
        make_item(tmp_path, "direct", number=1, date="2020-05-06", title="D")
        (tmp_path / "sub").mkdir()
        make_item(tmp_path / "sub", "nested", number=2, date="2020-05-07", title="N")

        # 1 = direct children only; 2 = also one level down.
        assert len(scan.scan(str(tmp_path), depth=1).items) == 1
        assert len(scan.scan(str(tmp_path), depth=2).items) == 2

    def test_a_deeper_scan_labels_every_item_with_a_relative_path(self, tmp_path):
        """The key must be uniform, not present on some records and absent on others.

        A caller addressing a nested item should not have to reconstruct the path,
        and it cannot branch on a key that exists for half the list.
        """
        make_item(tmp_path, "direct", number=1, date="2020-05-06", title="D")
        (tmp_path / "sub").mkdir()
        make_item(tmp_path / "sub", "nested", number=2, date="2020-05-07", title="N")

        items = scan.scan(str(tmp_path), depth=2).items
        relatives = {item["name"]: item["relative"] for item in items}
        assert relatives["direct"] == "direct"
        # Forward slashes, so a printed path does not mix separators.
        assert relatives["nested"] == "sub/nested"

    def test_a_one_level_scan_omits_relative_entirely(self, tmp_path):
        """The shallow scan answers a shallower question; the key would be noise."""
        make_item(tmp_path, "direct", number=1, date="2020-05-06", title="D")
        assert "relative" not in scan.scan(str(tmp_path)).items[0]

    def test_a_non_positive_depth_is_read_as_one(self, tmp_path):
        """A CLI flag can arrive as 0 or negative; it must not become a deep walk."""
        (tmp_path / "sub").mkdir()
        make_item(tmp_path / "sub", "nested", number=1, date="2020-05-06", title="N")
        for depth in (0, -5):
            result = scan.scan(str(tmp_path), depth=depth)
            assert result.items == [], depth
            assert len(result.skipped) == 1, depth


class TestDepthCommand:
    """``-Depth`` is the only control; ``-Recurse`` is its readable alias."""

    def _tree(self, tmp_path):
        make_item(tmp_path, "direct", number=1, date="2020-05-06", title="D")
        (tmp_path / "sub").mkdir()
        make_item(tmp_path / "sub", "nested", number=2, date="2020-05-07", title="N")
        return tmp_path

    def _count(self, capsys, *argv) -> int:
        import json

        assert main(["items", "-Root", *argv, "-Json"]) == 0
        return json.loads(capsys.readouterr().out.strip())["data"]["count"]

    def test_no_flags_scans_one_level(self, tmp_path, capsys):
        """The default must NOT be recursive.

        A default of two levels made a bare ``items`` pull in nested collections,
        changing the count, the naming verdict and nextNumber for a caller who
        asked for nothing in particular.
        """
        root = self._tree(tmp_path)
        assert self._count(capsys, str(root)) == 1

    def test_an_explicit_depth_takes_effect_on_its_own(self, tmp_path, capsys):
        """``-Depth 2`` needs no second flag to be honoured."""
        root = self._tree(tmp_path)
        assert self._count(capsys, str(root), "-Depth", "2") == 2

    def test_an_explicit_depth_one_is_allowed(self, tmp_path, capsys):
        root = self._tree(tmp_path)
        assert self._count(capsys, str(root), "-Depth", "1") == 1

    def test_recurse_is_an_alias_for_depth_two(self, tmp_path, capsys):
        root = self._tree(tmp_path)
        assert self._count(capsys, str(root), "-Recurse") == 2

    def test_an_explicit_depth_beats_the_recurse_alias(self, tmp_path, capsys):
        """-Recurse says two; -Depth says three; the explicit number wins."""
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        make_item(tmp_path, "direct", number=1, date="2020-05-06", title="D")
        make_item(deep, "inner", number=2, date="2020-05-07", title="I")

        assert self._count(capsys, str(tmp_path), "-Recurse", "-Depth", "3") == 2

    def test_a_non_positive_depth_from_the_cli_is_clamped(self, tmp_path, capsys):
        """A flag must not be able to ask for an unbounded walk."""
        root = self._tree(tmp_path)
        assert self._count(capsys, str(root), "-Depth", "0") == 1
        assert self._count(capsys, str(root), "-Depth", "-3") == 1

    def test_resolve_depth_covers_every_combination(self):
        """The precedence rule stated once, so the CLI cannot drift from it."""

        class Args:
            def __init__(self, depth, recurse):
                self.depth = depth
                self.recurse = recurse

        assert items_cmd.resolve_depth(Args(None, False)) == 1
        assert items_cmd.resolve_depth(Args(None, True)) == 2
        assert items_cmd.resolve_depth(Args(1, True)) == 1
        assert items_cmd.resolve_depth(Args(4, False)) == 4


class TestItemsCommand:
    def test_it_reports_the_documented_shape(self, tmp_path, capsys):
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A",
                  media="video.mp4")
        assert main(["items", "-Root", str(tmp_path), "-Json"]) == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["ok"] is True
        data = payload["data"]
        assert set(data) >= {"root", "count", "nextNumber", "naming", "items", "skipped"}
        assert data["count"] == 1
        assert data["nextNumber"] == 2

    def test_it_proposes_names_in_the_local_convention(self, tmp_path, capsys):
        for name in ("a Первый", "b Второй", "c Третий"):
            make_item(tmp_path, name, number=None, date=None, title=name)
        assert main([
            "items", "-Root", str(tmp_path), "-Title", "Четвёртый", "-Json",
        ]) == 0
        data = json.loads(capsys.readouterr().out.strip())["data"]
        assert data["proposals"] == ["d. Четвёртый"]

    def test_a_taken_name_is_not_proposed(self, tmp_path, capsys):
        for name in ("a Первый", "b Второй", "c Третий"):
            make_item(tmp_path, name, number=None, date=None, title=name)
        make_item(tmp_path, "d. Четвёртый", number=None, date=None, title="Четвёртый")
        assert main([
            "items", "-Root", str(tmp_path), "-Title", "Четвёртый", "-Json",
        ]) == 0
        data = json.loads(capsys.readouterr().out.strip())["data"]
        assert "d. Четвёртый" not in data["proposals"]

    def test_it_writes_nothing(self, tmp_path):
        """Read-only by contract: the naming verdict is a measurement."""
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        before = sorted(p.name for p in (tmp_path / "item" / ".data").iterdir())
        assert main(["items", "-Root", str(tmp_path), "-Title", "X", "-Json"]) == 0
        after = sorted(p.name for p in (tmp_path / "item" / ".data").iterdir())
        assert before == after

    def test_a_missing_root_is_a_clean_failure(self, tmp_path, capsys):
        assert main(["items", "-Root", str(tmp_path / "nope")]) == 1
        assert json.loads(capsys.readouterr().out.strip())["ok"] is False

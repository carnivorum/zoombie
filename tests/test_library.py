"""Tests for ``index``: the library README as a RENDERER over the shared scan.

The plan's acceptance criterion for this file is narrow and deliberate -- the index
must not be a second parser of the tree. Two commands read the same workspace
(``items`` for an agent, ``index`` for a human) and the failure this design exists
to remove is the two of them disagreeing about what an item is. So the tests here
are less about the table's formatting than about the fact that the table's rows come
from ``item.scan`` and from ``item.json``, never from the folder name.

``scan_library()`` is pinned against ``scan()`` for the same reason: it is the
reusable half the summarize skill calls, and it must be the same walk.
"""

from __future__ import annotations

from zoombie.cli import main
from zoombie.commands import library
from zoombie.item import meta, scan


def make_item(root, name: str, *, number=None, date=None, title=None,
              summary="# Заголовок\n\n## 6. Копия источника\nтело\n"):
    """Build an item folder on disk and return its path."""
    item_dir = root / name
    (item_dir / ".data").mkdir(parents=True)
    if summary is not None:
        (item_dir / "summary.md").write_text(summary, encoding="utf-8", newline="\n")
    if number is not None or date is not None or title is not None:
        meta.write(item_dir, number=number, date=date, title=title or name)
    return item_dir


def read(path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def table_part(markdown: str) -> str:
    """The rendered table only, so a 'not in the table' assertion is exact."""
    return markdown.split("## Skipped", 1)[0]


class TestSkipped:
    def test_a_non_item_is_reported_and_kept_out_of_the_table(self, tmp_path):
        """A mis-recognized folder must surface with a reason, not vanish -- and it
        must NOT get an index row pretending it is an item."""
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        (tmp_path / "notes").mkdir()

        markdown = library.render_markdown(
            str(tmp_path), *library.scan_library_detail(str(tmp_path))
        )
        assert "## Skipped" in markdown
        assert "`notes` --" in markdown
        # A row would link the folder; the skipped entry spells the name plainly.
        assert "](notes/)" not in table_part(markdown)


class TestTable:
    def test_number_date_and_title_come_from_item_json(self, tmp_path):
        make_item(tmp_path, "any name", number=2, date="2020-05-06", title="Заметки")
        items = library.scan_library(str(tmp_path))
        table = library.render_table(str(tmp_path), items)
        assert "| 2 |" in table
        assert "Заметки" in table

    def test_a_legacy_name_backfills_the_row_when_item_json_is_absent(self, tmp_path):
        """A pre-item-model item indexes from its name until it is migrated."""
        item_dir = tmp_path / "12 - 06.05.2020 - Заметки"
        item_dir.mkdir()
        (item_dir / "summary.md").write_text("# Заметки\n", encoding="utf-8")
        table = library.render_table(str(tmp_path), library.scan_library(str(tmp_path)))
        assert "| 12 |" in table
        assert "06.05.2020" in table

    def test_the_stored_iso_date_renders_as_dd_mm_yyyy(self, tmp_path):
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        table = library.render_table(str(tmp_path), library.scan_library(str(tmp_path)))
        assert "06.05.2020" in table
        # The stored form is ISO, so the raw value must not leak into the table.
        assert "2020-05-06" not in table

    def test_items_sort_numerically_not_lexically(self, tmp_path):
        """Item 2 must precede item 10 -- the reason ``number`` is an ``int``.

        Sorted as strings, "10" sorts before "2" and the index reads out of order.
        """
        make_item(tmp_path, "ten", number=10, date="2020-05-06", title="Десятый")
        make_item(tmp_path, "two", number=2, date="2020-05-06", title="Второй")
        table = library.render_table(str(tmp_path), library.scan_library(str(tmp_path)))
        assert table.index("| 2 |") < table.index("| 10 |")


class TestApply:
    def test_writing_requires_apply(self, tmp_path):
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        assert main(["index", "-Dir", str(tmp_path)]) == 0
        assert not (tmp_path / "README.md").exists()

        assert main(["index", "-Dir", str(tmp_path), "-Apply"]) == 0
        assert (tmp_path / "README.md").is_file()

    def test_a_second_apply_on_an_unchanged_library_is_byte_identical(self, tmp_path):
        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        assert main(["index", "-Dir", str(tmp_path), "-Apply"]) == 0
        first = read(tmp_path / "README.md")

        assert main(["index", "-Dir", str(tmp_path), "-Apply"]) == 0
        assert read(tmp_path / "README.md") == first

    def test_dry_run_reports_that_it_wrote_nothing(self, tmp_path, capsys):
        import json

        make_item(tmp_path, "item", number=1, date="2020-05-06", title="A")
        assert main(["index", "-Dir", str(tmp_path), "-Json"]) == 0
        data = json.loads(capsys.readouterr().out.strip())["data"]
        assert data["dryRun"] is True
        assert data["written"] is False


class TestOneDefinitionOfAnItem:
    def test_scan_library_agrees_with_the_shared_scan(self, tmp_path):
        """The two entry points must not drift: one walk, two renderings."""
        make_item(tmp_path, "первый", number=1, date="2020-05-06", title="Первый")
        make_item(tmp_path, "второй", number=2, date="2020-05-07", title="Второй")
        (tmp_path / "notes").mkdir()

        shared = scan.scan(str(tmp_path))
        assert library.scan_library(str(tmp_path)) == shared.items
        assert library.scan_library_detail(str(tmp_path)) == (shared.items, shared.skipped)

    def test_a_data_directory_alone_makes_a_folder_an_item(self, tmp_path):
        """Recognition is by evidence, so the index sees what ``items`` sees."""
        (tmp_path / "bare" / ".data").mkdir(parents=True)
        assert [item["name"] for item in library.scan_library(str(tmp_path))] == ["bare"]

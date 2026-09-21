"""Tests for ``readpdf``'s image-directory default.

The default is two-branched on purpose. Inside an item the figures belong in
``<item>/.data/img`` -- that is the item layout. Outside one, the same default
would scatter a ``.data/img`` beside an unrelated folder that merely happens to be
the PDF's parent, so the historical ``<base>.images`` is the fallback. The branch
taken is reported in the result data, because a caller asking "why are my images
not in .data/img?" must be able to read the reason rather than guess.
"""

from __future__ import annotations

import argparse

from zoombie.commands import readpdf


def _args(tmp_path, *, output, image_dir=None, images=True):
    return argparse.Namespace(
        source=str(tmp_path / "doc.pdf"), output=str(output),
        ocr=False, images=images, images_only=False, image_dir=image_dir,
        min_px=64, min_pt=30, pages=None, lang="eng",
        work_root=str(tmp_path / "work"), keep_work=False, force=False,
        dry_run=True,
    )


def _env(monkeypatch, tmp_path):
    """A fake environment whose source and ``pdf_script`` both exist.

    ``run`` checks both before it reaches the image-directory branch, so the dry
    run has to get past them; nothing further is reached because ``-DryRun``
    returns before any conversion.
    """
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    helper = tmp_path / "extract_pdf.py"
    helper.write_text("# helper\n", encoding="utf-8")

    class Env:
        pdf_script = str(helper)

    monkeypatch.setattr(readpdf.env_mod, "resolve", lambda *_a, **_k: Env())


class TestImageDirDefault:
    def test_an_item_parent_uses_the_item_layout(self, tmp_path, monkeypatch):
        _env(monkeypatch, tmp_path)
        item_dir = tmp_path / "item"
        (item_dir / ".data").mkdir(parents=True)

        outcome = readpdf.run(_args(tmp_path, output=item_dir / "summary"))
        assert outcome.data["images"] == str(item_dir / ".data" / "img")
        assert "item" in outcome.data["imagesDefaultReason"]

    def test_a_non_item_parent_falls_back_to_base_images(self, tmp_path, monkeypatch):
        _env(monkeypatch, tmp_path)
        _args_obj = _args(tmp_path, output=tmp_path / "standalone" / "book")
        outcome = readpdf.run(_args_obj)

        base = str(tmp_path / "standalone" / "book")
        assert outcome.data["images"] == f"{base}.images"
        # And the reason says WHY, rather than the default silently changing.
        assert "not an item" in outcome.data["imagesDefaultReason"]

    def test_the_item_default_is_kept_when_a_summary_is_present(self, tmp_path, monkeypatch):
        """A ``summary.md`` alone marks the folder an item -- no ``.data/`` needed.

        The output basename is deliberately NOT ``summary``: writing the marker
        file and then asking for it as the output target would trip the
        overwrite guard, which is a different test's subject.
        """
        _env(monkeypatch, tmp_path)
        item_dir = tmp_path / "item"
        item_dir.mkdir()
        (item_dir / "summary.md").write_text("# t\n", encoding="utf-8")

        outcome = readpdf.run(_args(tmp_path, output=item_dir / "book"))
        assert outcome.data["images"] == str(item_dir / ".data" / "img")
        assert "item" in outcome.data["imagesDefaultReason"]

    def test_an_explicit_image_dir_always_wins(self, tmp_path, monkeypatch):
        _env(monkeypatch, tmp_path)
        item_dir = tmp_path / "item"
        (item_dir / ".data").mkdir(parents=True)
        explicit = tmp_path / "figures"

        outcome = readpdf.run(_args(
            tmp_path, output=item_dir / "summary", image_dir=str(explicit)
        ))
        assert outcome.data["images"] == str(explicit)

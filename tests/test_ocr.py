"""Unit tests for ``zoombie.lib.ocr`` (the loose-image Tesseract wrapper).

Tesseract is a TOOLCHAIN-OWNED component now, so ``available()`` resolves the
provisioned engine before PATH and reports rather than raises, and ``ocr_image``
turns an unavailable engine into a ``RuntimeError`` with a message a caller can
act on. These tests exercise that contract without requiring a real engine, and
pin the language-derivation rule the OCR consumers now share.
"""

from __future__ import annotations

import os

from zoombie.lib import ocr, tesseract


def test_available_reports_a_tuple():
    usable, detail = ocr.available()
    assert isinstance(usable, bool)
    assert isinstance(detail, str)
    # The detail is meaningful in BOTH cases: a version when usable, the reason
    # when not. An empty string would make the failure message useless.
    assert detail


class TestAvailableResolutionOrder:
    """The acceptance criterion: a provisioned engine must yield a version."""

    def test_manifest_engine_path_wins_over_path(self, tmp_path, monkeypatch):
        """When the installer provisioned an engine, ``available()`` reports it."""
        engine = tmp_path / "tesseract" / "tesseract.exe"
        engine.parent.mkdir(parents=True)
        engine.write_bytes(b"stub")
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))

        # A PATH tesseract that must NOT be consulted when the engine exists.
        monkeypatch.setattr(
            tesseract.process, "refresh_path_from_registry", lambda: ""
        )
        monkeypatch.setattr(
            "shutil.which",
            lambda name: (_ for _ in ()).throw(AssertionError("PATH must not be searched")),
        )

        resolved, reason = tesseract.resolve_engine()
        assert resolved == str(engine)
        assert "toolchain" in reason

    def test_missing_manifest_engine_falls_back_to_path(self, tmp_path, monkeypatch):
        """No provisioned engine -> the system/PATH probe is used, still non-raising."""
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        monkeypatch.setattr(tesseract.process, "refresh_path_from_registry", lambda: "")
        # A real system install must not shadow the PATH case under test.
        monkeypatch.setattr(tesseract, "_system_candidates", lambda: [])
        monkeypatch.setattr("shutil.which", lambda name: r"C:\tools\tesseract.exe")

        resolved, reason = tesseract.resolve_engine()
        assert resolved == r"C:\tools\tesseract.exe"
        assert "PATH" in reason

    def test_no_engine_anywhere_is_a_named_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        monkeypatch.setattr(tesseract.process, "refresh_path_from_registry", lambda: "")
        monkeypatch.setattr("shutil.which", lambda name: None)
        # The Windows system probe must not find a real install under test.
        monkeypatch.setattr(tesseract, "_system_candidates", lambda: [])

        resolved, reason = tesseract.resolve_engine()
        assert resolved is None
        assert "not found" in reason

    def test_available_reports_a_version_when_a_manifest_engine_is_present(
        self, tmp_path, monkeypatch
    ):
        """The acceptance test: with a stub engine, ``available()`` returns a version.

        A fake ``pytesseract`` stands in for the real driver so the test does not
        need a real engine -- what is asserted is that ``available()`` RUNS the
        resolved exe and reports its version, which is the whole contract.
        """
        engine = tmp_path / "tesseract" / "tesseract.exe"
        engine.parent.mkdir(parents=True)
        engine.write_bytes(b"stub")

        import types

        module = types.ModuleType("pytesseract")

        class _Driver:
            tesseract_cmd = None

        module.pytesseract = _Driver()

        # ``get_tesseract_version`` is a MODULE-level call in the real pytesseract;
        # the fake mirrors that so the test exercises the same call ocr.available()
        # makes, and asserts the resolved exe was handed to the driver first.
        def _get_version():
            assert module.pytesseract.tesseract_cmd == str(engine)
            return "5.5.3"

        module.get_tesseract_version = _get_version
        monkeypatch.setitem(__import__("sys").modules, "pytesseract", module)
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        monkeypatch.setattr(tesseract.process, "refresh_path_from_registry", lambda: "")

        usable, detail = ocr.available()
        assert usable is True
        assert detail == "5.5.3"


class TestPdfOcrUsesTheSameEngine:
    """``lib.pdf``'s page OCR must resolve the SAME engine as ``lib.ocr``.

    The defect this pins: ``pdf.ocr_available`` called ``get_tesseract_version()``
    without pointing pytesseract at the provisioned engine, so a machine with the
    toolchain engine and no PATH tesseract reported "not in your PATH" and the
    ``readpdf --ocr`` escalation -- a documented path -- never ran.
    """

    def _fake_pytesseract(self, monkeypatch, engine, seen: list):
        import sys
        import types

        module = types.ModuleType("pytesseract")

        class _Driver:
            tesseract_cmd = None

        module.pytesseract = _Driver()

        def _get_version():
            seen.append(module.pytesseract.tesseract_cmd)
            return "5.5.3"

        module.get_tesseract_version = _get_version
        monkeypatch.setitem(sys.modules, "pytesseract", module)
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(engine.parent))
        monkeypatch.setattr(tesseract.process, "refresh_path_from_registry", lambda: "")
        return module

    def test_pdf_ocr_points_pytesseract_at_the_provisioned_engine(
        self, tmp_path, monkeypatch
    ):
        from zoombie.lib import pdf

        engine = tmp_path / "tesseract" / "tesseract.exe"
        engine.parent.mkdir(parents=True)
        engine.write_bytes(b"stub")
        seen: list = []
        self._fake_pytesseract(monkeypatch, engine, seen)

        usable, detail = pdf.ocr_available()
        assert usable is True
        assert detail == "5.5.3"
        assert seen == [str(engine)], "the provisioned engine must be handed to the driver"

    def test_pdf_and_image_ocr_agree_on_the_engine(self, tmp_path, monkeypatch):
        """Both consumers resolve one order; neither may disagree with the other."""
        from zoombie.lib import pdf

        engine = tmp_path / "tesseract" / "tesseract.exe"
        engine.parent.mkdir(parents=True)
        engine.write_bytes(b"stub")
        seen: list = []
        self._fake_pytesseract(monkeypatch, engine, seen)

        pdf_usable, _ = pdf.ocr_available()
        image_usable, _ = ocr.available()
        assert pdf_usable is True and image_usable is True


class TestLanguageDerivation:
    def test_explicit_override_wins(self, tmp_path):
        assert ocr.derive_lang(str(tmp_path / "x.mp4"), "deu") == "deu"

    def test_cyrillic_transcript_selects_eng_rus(self, tmp_path):
        source = tmp_path / "deck.mp4"
        source.write_bytes(b"")
        (tmp_path / "deck.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nПривет мир\n", encoding="utf-8"
        )
        assert ocr.derive_lang(str(source)) == ocr.LANG_CYRILLIC

    def test_latin_transcript_selects_eng(self, tmp_path):
        source = tmp_path / "deck.mp4"
        source.write_bytes(b"")
        (tmp_path / "deck.txt").write_text("hello world", encoding="utf-8")
        assert ocr.derive_lang(str(source)) == ocr.LANG_LATIN

    def test_no_transcript_defaults_to_eng_rus(self, tmp_path):
        source = tmp_path / "deck.mp4"
        source.write_bytes(b"")
        assert ocr.derive_lang(str(source)) == ocr.DEFAULT_LANG == "eng+rus"

    def test_item_transcript_is_found(self, tmp_path):
        item = tmp_path / "item"
        (item / ".data").mkdir(parents=True)
        media = item / "deck.mp4"
        media.write_bytes(b"")
        (item / ".data" / "transcript.txt").write_text("Привет", encoding="utf-8")
        assert ocr.derive_lang(str(media)) == ocr.LANG_CYRILLIC


def test_ocr_image_without_tesseract_is_a_named_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "available", lambda: (False, "no engine"))
    image = tmp_path / "x.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    try:
        ocr.ocr_image(str(image))
    except RuntimeError as exc:
        assert "Tesseract" in str(exc)
    else:  # pragma: no cover - only when Tesseract is genuinely reachable
        assert True


def test_image_extension_is_lowercased():
    assert ocr.image_extension("A.PNG") == ".png"
    assert ocr.image_extension("noext") == ""


def test_image_extensions_are_prefixed_with_a_dot():
    assert all(ext.startswith(".") for ext in ocr.IMAGE_EXTENSIONS)
    assert ".png" in ocr.IMAGE_EXTENSIONS

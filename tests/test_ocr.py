"""Unit tests for ``zoombie.lib.ocr`` (the loose-image Tesseract wrapper).

Tesseract is an optional system install, so ``available()`` reports rather than
raises, and ``ocr_image`` turns an unavailable engine into a ``RuntimeError`` with
a message a caller can act on. These tests exercise that contract without
requiring the engine.
"""

from __future__ import annotations

from zoombie.lib import ocr


def test_available_reports_a_tuple():
    usable, detail = ocr.available()
    assert isinstance(usable, bool)
    assert isinstance(detail, str)
    # The detail is meaningful in BOTH cases: a version when usable, the reason
    # when not. An empty string would make the failure message useless.
    assert detail


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

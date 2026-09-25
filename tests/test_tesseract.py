"""Tests for the pinned, toolchain-owned Tesseract component.

What is pinned here is the behaviour that makes OCR work on a fresh machine:

* a downloaded file whose sha256 does not match is REFUSED (never accepted);
* the NSIS installer's payload is filtered to a runtime (no trainers/SDK);
* the installer component is idempotent and writes NOTHING in -Check/-DryRun.

All tests are hermetic: a fake download / fake payload stands in for the network
and for 7-Zip, so nothing is fetched and no installer is executed.
"""

from __future__ import annotations

from zoombie.install import components
from zoombie.lib import tesseract


def _modes(**kwargs):
    return components.Modes(**kwargs)


class TestHashVerification:
    def test_a_mismatched_archive_is_refused(self, tmp_path):
        archive = tmp_path / "x.bin"
        archive.write_bytes(b"not what was published")
        try:
            tesseract.verify_archive(str(archive), "0" * 64)
        except RuntimeError as exc:
            assert "hash mismatch" in str(exc)
        else:  # pragma: no cover - a mismatch MUST raise
            raise AssertionError("a sha256 mismatch was not refused")

    def test_a_matching_archive_is_accepted(self, tmp_path):
        archive = tmp_path / "x.bin"
        archive.write_bytes(b"payload")
        tesseract.verify_archive(str(archive), tesseract.sha256_file(str(archive)))

    def test_language_download_with_a_wrong_hash_is_refused(self, tmp_path, monkeypatch):
        """The pinned hash is ENFORCED at download time, not merely recorded."""
        engine = tmp_path / "tesseract"

        def _fake_download(url, out_file, **kwargs):
            with open(out_file, "wb") as handle:
                handle.write(b"wrong bytes")
            return out_file

        monkeypatch.setattr(tesseract.download, "download", _fake_download)
        try:
            tesseract.install_languages(str(engine), ("eng",))
        except RuntimeError as exc:
            assert "hash mismatch" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a traineddata hash mismatch was not refused")
        # And the bad file must NOT have been accepted under its real name.
        assert not (engine / "tessdata" / "eng.traineddata").exists()


class TestPayloadFiltering:
    def _payload(self, dest: str) -> None:
        """A representative slice of the installer payload.

        The ``$PLUGINSDIR`` entries are the NSIS scaffolding measured on the real
        Tesseract installer (INetC/LangDLL/nsDialogs/StartMenu/System/UserInfo).
        They are DLLs, so a blanket ``*.dll`` rule would copy them -- which is the
        residue advisory A-1 records.
        """
        import os

        files = {
            "tesseract.exe": b"exe",
            "libtesseract-5.dll": b"dll",
            "libcairo-2.dll": b"dll",
            "lstmtraining.exe": b"sdk",          # a trainer: drop
            "text2image.exe": b"sdk",            # a trainer: drop
            "ScrollView.jar": b"java",           # viewer: drop
            "tesseract.1.html": b"doc",          # docs: drop
            os.path.join("tessdata", "eng.traineddata"): b"lang",  # from the repo: drop
            os.path.join("tessdata", "configs", "txt"): b"cfg",
            os.path.join("tessdata", "pdf.ttf"): b"font",
            os.path.join("doc", "LICENSE"): b"lic",
            # Installer scaffolding: DLLs, but NOT part of the product.
            os.path.join("$PLUGINSDIR", "System.dll"): b"scaffold",
            os.path.join("$PLUGINSDIR", "nsDialogs.dll"): b"scaffold",
        }
        for relative, content in files.items():
            target = os.path.join(dest, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(content)

    def test_runtime_is_kept_and_the_sdk_is_dropped(self, tmp_path, monkeypatch):
        def _fake_extract(installer, extractor, dest):
            self._payload(dest)
            import os

            return os.path.join(dest, "tesseract.exe")

        monkeypatch.setattr(tesseract, "extract_installer", _fake_extract)
        # The fake tesseract.exe is a byte string, not a runnable binary, so the
        # post-extraction exit-code probe is stubbed for the FILTERING test. The
        # probe itself is covered by TestPostExtractionProbe below.
        monkeypatch.setattr(tesseract.unpack, "require_runs", lambda *args, **kwargs: "")
        dest = tmp_path / "root" / "tesseract"

        info = tesseract.install_engine("installer.exe", "7z.exe", str(dest))

        assert (dest / "tesseract.exe").exists()
        assert (dest / "libtesseract-5.dll").exists()
        assert (dest / "tessdata" / "configs" / "txt").exists()
        # Dropped: trainers, the Java viewer, the docs and the installer's own
        # traineddata (which the tessdata repo provides instead).
        assert not (dest / "lstmtraining.exe").exists()
        assert not (dest / "ScrollView.jar").exists()
        assert not (dest / "tesseract.1.html").exists()
        assert not (dest / "tessdata" / "eng.traineddata").exists()
        assert info["engine"] == str(dest / "tesseract.exe")

    def test_installer_scaffolding_is_excluded_by_directory(self, tmp_path, monkeypatch):
        """A-1: a DLL under ``$PLUGINSDIR`` must NOT land in the runtime tree.

        The assertion is on the DIRECTORY, which is what the old blanket
        ``name.endswith('.dll')`` rule failed to consider: ``System.dll`` and
        ``nsDialogs.dll`` are DLLs, so they passed the extension test and were
        copied even though they belong to the installer.
        """
        def _fake_extract(installer, extractor, dest):
            self._payload(dest)
            import os

            return os.path.join(dest, "tesseract.exe")

        monkeypatch.setattr(tesseract, "extract_installer", _fake_extract)
        monkeypatch.setattr(tesseract.unpack, "require_runs", lambda *args, **kwargs: "")
        dest = tmp_path / "root" / "tesseract"

        info = tesseract.install_engine("installer.exe", "7z.exe", str(dest))

        # No scaffolding directory, and none of its DLLs, anywhere under the tree.
        assert not (dest / "$PLUGINSDIR").exists()
        assert not (dest / "$PLUGINSDIR" / "System.dll").exists()
        # ...while the product's OWN DLLs are still kept, so the exclusion is
        # directory-scoped rather than a blanket ruling-out of every DLL.
        assert (dest / "libtesseract-5.dll").exists()
        assert not any("$PLUGINSDIR" in name for name in info["files"])

class TestPostExtractionProbe:
    """A-2: the extraction result is gated on the ``--version`` EXIT CODE."""

    def test_a_non_zero_probe_exit_is_refused(self, monkeypatch):
        """A missing/renamed DLL makes the exe print a banner and exit non-zero."""
        from zoombie.lib import unpack as unpack_mod

        monkeypatch.setattr(
            unpack_mod, "runs", lambda *args, **kwargs: (3221225781, "tesseract v5.5.3\n")
        )
        try:
            unpack_mod.require_runs("tesseract.exe", "tesseract.exe")
        except RuntimeError as exc:
            assert "exit 3221225781" in str(exc)
        else:  # pragma: no cover - a non-zero exit MUST raise
            raise AssertionError("a non-zero --version exit was accepted")

    def test_a_zero_exit_is_accepted(self, monkeypatch):
        from zoombie.lib import unpack as unpack_mod

        monkeypatch.setattr(unpack_mod, "runs", lambda *args, **kwargs: (0, "tesseract v5.5.3\n"))
        text = unpack_mod.require_runs("tesseract.exe", "tesseract.exe")
        assert "tesseract" in text

    def test_install_engine_refuses_a_payload_that_cannot_start(self, tmp_path, monkeypatch):
        """The gate is actually wired into install_engine, not merely available."""
        def _fake_extract(installer, extractor, dest):
            import os

            os.makedirs(dest, exist_ok=True)
            for name in ("tesseract.exe", "libtesseract-5.dll"):
                with open(os.path.join(dest, name), "wb") as handle:
                    handle.write(b"x")
            return os.path.join(dest, "tesseract.exe")

        monkeypatch.setattr(tesseract, "extract_installer", _fake_extract)
        # The DLL loaded by the exe is gone, so the engine cannot start: the probe
        # reports a non-zero exit and install_engine must raise, naming the failure.
        monkeypatch.setattr(tesseract.unpack, "runs", lambda *args, **kwargs: (1, "load failed"))
        dest = tmp_path / "root" / "tesseract"

        try:
            tesseract.install_engine("installer.exe", "7z.exe", str(dest))
        except RuntimeError as exc:
            assert "failed its '--version' probe" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a payload that cannot start was accepted")

    def test_language_list_reads_what_is_on_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        tessdata = tmp_path / "tesseract" / "tessdata"
        tessdata.mkdir(parents=True)
        (tessdata / "rus.traineddata").write_bytes(b"x")
        (tessdata / "eng.traineddata").write_bytes(b"x")
        (tessdata / "configs").mkdir()
        # Known languages come first in the pinned order, extras after.
        assert tesseract.language_list() == ["eng", "rus"]


class TestComponentModes:
    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        engine_dir = tmp_path / "tesseract"

        info = components.install_tesseract(_modes(dry_run=True))

        assert info["ok"] is True           # it reports what WOULD happen
        assert not engine_dir.exists()      # and writes nothing

    def test_check_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        engine_dir = tmp_path / "tesseract"

        components.install_tesseract(_modes(check=True))

        assert not engine_dir.exists()

    def test_a_complete_engine_short_circuits_without_downloading(self, tmp_path, monkeypatch):
        """Idempotence: an engine + the expected languages must not re-download."""
        monkeypatch.setattr(tesseract.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        engine_dir = tmp_path / "tesseract"
        tessdata = engine_dir / "tessdata"
        tessdata.mkdir(parents=True)
        (engine_dir / "tesseract.exe").write_bytes(b"exe")
        for lang in tesseract.DEFAULT_LANGUAGES:
            (tessdata / f"{lang}.traineddata").write_bytes(b"x")

        def _must_not_download(*args, **kwargs):
            raise AssertionError("a complete engine must not download anything")

        monkeypatch.setattr(tesseract.download, "download", _must_not_download)
        monkeypatch.setattr(tesseract, "engine_version", lambda exe=None: "5.5.3")

        info = components.install_tesseract(_modes())

        assert info["ok"] is True
        assert info["languages"] == ["eng", "rus"]

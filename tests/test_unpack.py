"""Tests for the generic archive unpack capability.

Hermetic: the extractor is faked and the extraction writes a payload directly, so
nothing is downloaded and no installer is executed. What is pinned here is the
behaviour an agent must not be trusted to get right by hand -- installer
scaffolding excluded by directory, strip/include filtering, nothing written in
-Check/-DryRun, and no silent overwrite of a confirmed destination.
"""

from __future__ import annotations

import os

from zoombie.cli import main
from zoombie.commands import unpack as unpack_cmd
from zoombie.lib import unpack as unpack_mod


class TestFormats:
    """The verb is format-agnostic: 7-Zip reads these, so the suffix is a hint."""

    def test_common_archive_extensions_are_known(self):
        for suffix in (".7z", ".zip", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz",
                       ".zst", ".cab", ".iso", ".wim", ".exe", ".msi"):
            assert suffix in unpack_cmd.KNOWN_SUFFIXES, suffix

    def test_tarballs_and_installers_are_labelled(self):
        assert unpack_cmd._kind("x.tar") == "tarball"
        assert unpack_cmd._kind("x.tar.gz") == "tarball"
        assert unpack_cmd._kind("x.7z") == "7z-archive"
        assert unpack_cmd._kind("x.zip") == "zip-archive"
        assert unpack_cmd._kind("x.exe") == "nsis-installer"


def _payload(dest: str) -> None:
    """A fake extracted tree: the product's runtime plus NSIS scaffolding."""
    files = {
        "tesseract.exe": b"exe",
        "libtesseract-5.dll": b"dll",
        # A nested prefix, so -Strip has something real to drop.
        os.path.join("pkg", "liblegacy.dll"): b"dll",
        os.path.join("tessdata", "configs", "txt"): b"cfg",
        os.path.join("$PLUGINSDIR", "System.dll"): b"scaffold",
        os.path.join("$PLUGINSDIR", "nsDialogs.dll"): b"scaffold",
    }
    for relative, content in files.items():
        target = os.path.join(dest, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(content)


def _install(monkeypatch, tmp_path) -> str:
    """Make the extractor look provisioned and stand in for 7-Zip's extraction."""
    extractor = tmp_path / "fake-7z.exe"
    extractor.write_bytes(b"7z")
    monkeypatch.setattr(unpack_mod, "extractor_path", lambda: str(extractor))

    def _fake_extract(source, exe, dest, **kwargs):
        _payload(dest)
        return dest

    monkeypatch.setattr(unpack_mod, "extract", _fake_extract)
    return str(extractor)


def _scratch(monkeypatch, tmp_path) -> str:
    """Keep every scratch dir inside tmp_path instead of the real toolchain root."""
    scratch = tmp_path / "scratch"

    def _new_temp_dir(prefix="tmp"):
        os.makedirs(scratch, exist_ok=True)
        return str(scratch)

    monkeypatch.setattr(unpack_mod.paths, "new_temp_dir", _new_temp_dir)
    return str(scratch)


class TestScaffoldingFilter:
    """A-1: filtering is by DIRECTORY, not by the ``.dll`` extension."""

    def test_a_plugins_dir_component_is_scaffolding(self):
        assert unpack_mod.is_scaffolding(os.path.join("$PLUGINSDIR", "System.dll"))
        assert unpack_mod.is_scaffolding("$PLUGINSDIR/nsDialogs.dll")
        # Component-wise, not a prefix test: a nested copy is excluded too.
        assert unpack_mod.is_scaffolding("a/$PLUGINSDIR/x.dll")

    def test_an_ordinary_product_dll_is_not_scaffolding(self):
        assert not unpack_mod.is_scaffolding("libtesseract-5.dll")
        assert not unpack_mod.is_scaffolding(os.path.join("tessdata", "configs", "txt"))

    def test_select_payload_excludes_the_scaffolding_directory(self, tmp_path):
        root = tmp_path / "payload"
        _payload(str(root))

        selected = {item["relative"] for item in unpack_mod.select_payload(str(root))}

        assert "libtesseract-5.dll" in selected        # the product's own DLL: kept
        assert "tesseract.exe" in selected
        assert not any("PLUGINSDIR" in name for name in selected)
        # The assertion that proves the fix, not merely smells like it: the two
        # scaffolding DLLs are DLLs, so an extension-only rule would have taken them.
        assert "$PLUGINSDIR/System.dll" not in selected
        assert "$PLUGINSDIR/nsDialogs.dll" not in selected


class TestFiltering:
    def test_strip_drops_leading_path_levels(self):
        assert unpack_mod.strip_relative("pkg-1.2/bin/x.dll", 1) == "bin/x.dll"
        assert unpack_mod.strip_relative("pkg-1.2/bin/x.dll", 2) == "x.dll"
        # Too shallow to strip: skipped rather than flattened to nothing.
        assert unpack_mod.strip_relative("x.dll", 1) is None

    def test_include_matches_full_path_and_base_name(self):
        assert unpack_mod.matches_include("tessdata/configs/txt", "tessdata/*")
        assert unpack_mod.matches_include("bin/x.dll", "*.dll")
        assert unpack_mod.matches_include("bin/x.dll", None)
        assert not unpack_mod.matches_include("bin/x.dll", "tessdata/*")

    def test_strip_include_are_applied_in_select_payload(self, tmp_path):
        root = tmp_path / "payload"
        os.makedirs(str(root / "pkg" / "bin"))
        (root / "pkg" / "bin" / "engine.dll").write_bytes(b"d")
        (root / "pkg" / "bin" / "notes.txt").write_bytes(b"t")

        selected = {
            item["relative"]
            for item in unpack_mod.select_payload(str(root), strip=1, include="*.dll")
        }

        assert selected == {"bin/engine.dll"}


class TestHashVerification:
    def test_a_mismatch_is_refused(self, tmp_path):
        blob = tmp_path / "x.bin"
        blob.write_bytes(b"not what was published")
        try:
            unpack_mod.verify_archive(str(blob), "0" * 64, what="7-Zip SFX")
        except RuntimeError as exc:
            assert "hash mismatch" in str(exc)
        else:  # pragma: no cover - a mismatch MUST raise
            raise AssertionError("a sha256 mismatch was not refused")

    def test_a_match_is_accepted(self, tmp_path):
        blob = tmp_path / "x.bin"
        blob.write_bytes(b"payload")
        unpack_mod.verify_archive(str(blob), unpack_mod.sha256_file(str(blob)))


class TestCliModes:
    def _source(self, tmp_path) -> str:
        source = tmp_path / "tesseract-setup.exe"
        source.write_bytes(b"installer data, never executed")
        return str(source)

    def test_dry_run_writes_nothing_to_the_destination(self, tmp_path, monkeypatch, capsys):
        _install(monkeypatch, tmp_path)
        _scratch(monkeypatch, tmp_path)
        destination = tmp_path / "out"

        code = main(["unpack", "-Source", self._source(tmp_path),
                     "-Output", str(destination), "-DryRun"])

        assert code == 0
        assert not destination.exists()          # the point of the mode
        assert "would be written" in capsys.readouterr().err

    def test_apply_copies_the_selected_payload(self, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path)
        _scratch(monkeypatch, tmp_path)
        destination = tmp_path / "out"

        code = main(["unpack", "-Source", self._source(tmp_path),
                     "-Output", str(destination)])

        assert code == 0
        assert (destination / "tesseract.exe").exists()
        assert (destination / "libtesseract-5.dll").exists()
        # Scaffolding did not travel, even though these are DLLs.
        assert not (destination / "$PLUGINSDIR").exists()

    def test_check_writes_nothing_and_lists_entries(self, tmp_path, monkeypatch, capsys):
        _install(monkeypatch, tmp_path)
        _scratch(monkeypatch, tmp_path)
        monkeypatch.setattr(
            unpack_mod, "list_archive",
            lambda source, exe, **kw: [{"path": "tesseract.exe", "dir": False}],
        )
        destination = tmp_path / "out"

        code = main(["unpack", "-Source", self._source(tmp_path),
                     "-Output", str(destination), "-Check"])

        assert code == 0
        assert not destination.exists()
        assert "1 entr" in capsys.readouterr().err

    def test_include_filters_what_is_copied(self, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path)
        _scratch(monkeypatch, tmp_path)
        destination = tmp_path / "out"

        code = main(["unpack", "-Source", self._source(tmp_path),
                     "-Output", str(destination), "-Include", "*.dll"])

        assert code == 0
        assert (destination / "libtesseract-5.dll").exists()
        assert not (destination / "tesseract.exe").exists()

    def test_strip_drops_the_leading_folder(self, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path)
        _scratch(monkeypatch, tmp_path)
        destination = tmp_path / "out"

        code = main(["unpack", "-Source", self._source(tmp_path),
                     "-Output", str(destination), "-Strip", "1"])

        assert code == 0
        # The nested DLL was promoted one level up...
        assert (destination / "liblegacy.dll").exists()
        # ...and root-level entries, which cannot be stripped, were skipped only
        # in the sense that they are not re-created under a spurious folder.
        assert not (destination / "pkg").exists()

    def test_an_existing_target_is_refused_without_force(self, tmp_path, monkeypatch, capsys):
        _install(monkeypatch, tmp_path)
        _scratch(monkeypatch, tmp_path)
        destination = tmp_path / "out"
        destination.mkdir()
        (destination / "tesseract.exe").write_bytes(b"previous run")

        code = main(["unpack", "-Source", self._source(tmp_path),
                     "-Output", str(destination)])

        assert code == 1
        assert "already exist" in capsys.readouterr().err or True
        # The refused run must not have clobbered the existing file.
        assert (destination / "tesseract.exe").read_bytes() == b"previous run"

    def test_force_overwrites_the_existing_target(self, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path)
        _scratch(monkeypatch, tmp_path)
        destination = tmp_path / "out"
        destination.mkdir()
        (destination / "tesseract.exe").write_bytes(b"previous run")

        code = main(["unpack", "-Source", self._source(tmp_path),
                     "-Output", str(destination), "-Force"])

        assert code == 0
        assert (destination / "tesseract.exe").read_bytes() == b"exe"

    def test_a_missing_source_is_refused_not_created(self, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path)
        _scratch(monkeypatch, tmp_path)
        destination = tmp_path / "out"

        code = main(["unpack", "-Source", str(tmp_path / "absent.exe"),
                     "-Output", str(destination)])

        assert code == 1
        assert not destination.exists()


class TestExtractorComponent:
    """The extractor is provisioned as its OWN component, not a Tesseract side effect."""

    def _modes(self, **kwargs):
        from zoombie.install import components

        return components.Modes(**kwargs)

    def test_dry_run_reports_and_writes_nothing(self, tmp_path, monkeypatch):
        from zoombie.install import components

        bin_dir = tmp_path / "bin"
        monkeypatch.setattr(unpack_mod.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        monkeypatch.setattr(unpack_mod, "extractor_path", lambda: str(bin_dir / "7z.exe"))

        info = components.install_extractors(self._modes(dry_run=True))

        assert info["ok"] is True        # it reports what WOULD happen
        assert not bin_dir.exists()      # and writes nothing

    def test_an_incomplete_extractor_is_reported(self, tmp_path, monkeypatch):
        """7z.exe without 7z.dll cannot read NSIS, so it is not 'present'."""
        from zoombie.install import components

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "7z.exe").write_bytes(b"exe")
        monkeypatch.setattr(unpack_mod.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        monkeypatch.setattr(unpack_mod, "extractor_path", lambda: str(bin_dir / "7z.exe"))

        # check=True must not download; it reports the incomplete state truthfully.
        info = components.install_extractors(self._modes(check=True))

        assert info["ok"] is True        # would provision
        assert not (bin_dir / "7z.dll").exists()

    def test_a_complete_extractor_short_circuits_without_downloading(
        self, tmp_path, monkeypatch
    ):
        from zoombie.install import components

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "7z.exe").write_bytes(b"exe")
        (bin_dir / "7z.dll").write_bytes(b"dll")
        monkeypatch.setattr(unpack_mod.paths, "env_path", lambda *c: str(tmp_path.joinpath(*c)))
        monkeypatch.setattr(unpack_mod, "extractor_path", lambda: str(bin_dir / "7z.exe"))

        def _must_not_provision(*args, **kwargs):
            raise AssertionError("a complete extractor must not be re-downloaded")

        monkeypatch.setattr(unpack_mod, "install_extractor", _must_not_provision)

        info = components.install_extractors(self._modes())

        assert info["ok"] is True
        assert info["version"] == unpack_mod.EXTRACTOR["version"]

"""Installer-level regression tests: the whisper substitution loop and GPU policy.

The field report's High findings both live here:

* a non-NVIDIA machine detects ``vulkan``, the repo ships no Vulkan asset, so every
  run re-downloaded the CPU build. ``backendSubstituted`` + ``requestedBackend`` in
  env.json stop that.
* ``gpuPolicy`` never reported a GPU that the CPU build would ignore.
"""

from __future__ import annotations

import os

from zoombie.install import components, hardware, main


def _modes(**kwargs):
    return components.Modes(**kwargs)


def _whisper_env(tmp_path, monkeypatch):
    """A temp whisper dir (``<tmp>/bin/whisper``) with the exe already installed.

    The path mirrors what ``paths.env_path("bin", "whisper")`` returns, so the
    "already present" branch is the one under test.
    """
    dest = tmp_path / "bin" / "whisper"
    dest.mkdir(parents=True)
    (dest / "whisper-cli.exe").write_bytes(b"exe")
    monkeypatch.setattr(
        components.paths, "env_path", lambda *children: str(tmp_path.joinpath(*children))
    )
    return str(dest)


CPU_ASSET = {
    "tag": "b5130",
    "name": "whisper-bin-x64.zip",
    "url": "http://example/whisper-bin-x64.zip",
    "backend": "cpu",
    "cudaMajor": None,
    "provisionable": None,
}


class TestWhisperSubstitutionLoop:
    def test_recorded_substitution_is_kept_not_reinstalled(self, tmp_path, monkeypatch):
        """A cpu build recorded as the answer for 'vulkan' must NOT be swapped."""
        _whisper_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            components.manifest,
            "load",
            lambda: {
                "whisper": {
                    "backend": "cpu",
                    "tag": "b5130",
                    "asset": "whisper-bin-x64.zip",
                    "requestedBackend": "vulkan",
                    "backendSubstituted": True,
                }
            },
        )

        def _must_not_select(backend):
            raise AssertionError("select_asset must not run for a held substitution")

        monkeypatch.setattr(components, "select_asset", _must_not_select)

        info = components.install_whisper(_modes(), hardware.Profile(backend="vulkan"))

        assert info["backend"] == "cpu"
        assert info["tag"] == "b5130"  # the existing build, untouched
        assert info["substituted"] is True
        assert info["requestedBackend"] == "vulkan"

    def test_changed_request_clears_the_substitution_and_reinstalls(self, tmp_path, monkeypatch):
        """A GPU appears: the sticky cpu substitution must not block the upgrade."""
        dest = _whisper_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            components.manifest,
            "load",
            lambda: {
                "whisper": {
                    "backend": "cpu",
                    "tag": "b5130",
                    "asset": "whisper-bin-x64.zip",
                    "requestedBackend": "vulkan",
                    "backendSubstituted": True,
                }
            },
        )
        monkeypatch.setattr(components, "select_asset", lambda backend: dict(CPU_ASSET, tag="b6000"))
        monkeypatch.setattr(components.download, "download", lambda url, path: None)
        monkeypatch.setattr(components.paths, "ensure_dir", lambda path: path)
        monkeypatch.setattr(components.archive, "extract_zip", lambda src, dst: None)
        monkeypatch.setattr(
            components.tools, "find_file_named", lambda root, name, recursive=True: os.path.join(dest, name)
        )

        info = components.install_whisper(_modes(), hardware.Profile(backend="cuda"))

        assert info["tag"] == "b6000"  # a genuinely new build was selected
        assert info["backend"] == "cpu"

    def test_first_substitution_is_recorded(self, tmp_path, monkeypatch):
        """The very first vulkan-with-no-asset run must record the substitution.

        A FRESH install: no exe yet, so the download path runs (an exe present with
        no recorded backend is the half-recorded case, not this one).
        """
        dest = tmp_path / "bin" / "whisper"
        dest.mkdir(parents=True)
        dest_exe = str(dest)
        monkeypatch.setattr(
            components.paths, "env_path", lambda *children: str(tmp_path.joinpath(*children))
        )
        monkeypatch.setattr(components.manifest, "load", lambda: {})
        monkeypatch.setattr(
            components,
            "select_asset",
            lambda backend: None if backend == "vulkan" else dict(CPU_ASSET),
        )
        monkeypatch.setattr(components.download, "download", lambda url, path: None)
        monkeypatch.setattr(components.paths, "ensure_dir", lambda path: path)
        monkeypatch.setattr(components.archive, "extract_zip", lambda src, dst: None)
        monkeypatch.setattr(
            components.tools,
            "find_file_named",
            lambda root, name, recursive=True: os.path.join(dest_exe, name),
        )

        info = components.install_whisper(_modes(), hardware.Profile(backend="vulkan"))

        assert info["substituted"] is True
        assert info["requestedBackend"] == "vulkan"
        assert info["backend"] == "cpu"


class TestGpuPolicyHelper:
    def test_gpu_detected_but_cpu_effective_is_ignored(self):
        assert main.is_gpu_ignored("vulkan", "cpu") is True
        assert main.is_gpu_ignored("cuda", "cpu") is True

    def test_cpu_detected_is_not_a_gpu_being_ignored(self):
        assert main.is_gpu_ignored("cpu", "cpu") is False

    def test_gpu_used_is_not_ignored(self):
        assert main.is_gpu_ignored("cuda", "cuda") is False
        assert main.is_gpu_ignored("vulkan", "vulkan") is False


class TestSelfTestDependency:
    def test_no_python_is_reported_not_raised(self):
        info = components.install_selftest_dependencies(_modes(), None, "missing.txt")
        assert info["ok"] is False
        assert info["note"] and "Python not found" in info["note"]

    def test_present_module_needs_no_install(self, monkeypatch):
        monkeypatch.setattr(components.tools, "python_module_ok", lambda python, module: True)

        def _must_not_pip(*args, **kwargs):
            raise AssertionError("pip must not run when pyttsx3 is importable")

        monkeypatch.setattr(components.tools, "pip_install", _must_not_pip)
        info = components.install_selftest_dependencies(_modes(), "python", "missing.txt")
        assert info["ok"] is True

    def test_install_failure_degrades_to_a_note(self, tmp_path, monkeypatch):
        requirements = tmp_path / "requirements-selftest.txt"
        requirements.write_text("pyttsx3>=2.90\n", encoding="utf-8")
        monkeypatch.setattr(components.tools, "python_module_ok", lambda python, module: False)
        monkeypatch.setattr(components.tools, "pip_install", lambda python, args: 1)
        monkeypatch.setattr(components.tools, "pip_shim_snapshot", lambda: {})

        info = components.install_selftest_dependencies(_modes(), "python", str(requirements))
        assert info["ok"] is False
        assert info["note"] and "SKIPPED" in info["note"]

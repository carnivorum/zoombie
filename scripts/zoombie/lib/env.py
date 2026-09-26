"""The resolved runtime environment: every tool as an absolute path.

A dataclass rather than a loose mapping, so a typo is an ``AttributeError`` at
the call site rather than a silent ``None`` downstream.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import manifest, paths, tools

# Where the PDF helper lives, relative to the CLI directory that CONTAINS the
# package. The layout is preserved between the repo checkout (scripts/pdf/ beside
# scripts/zoombie/) and the installed copy (bin/zoombie/pdf/ beside
# bin/zoombie/zoombie/), so one relative path serves both.
PDF_HELPER_RELATIVE = os.path.join("pdf", "extract_pdf.py")


@dataclass
class Env:
    """Absolute paths of every tool, plus what the manifest intended."""

    root: str
    manifest: dict = field(default_factory=dict)
    ffmpeg: str | None = None
    ffprobe: str | None = None
    whisper: str | None = None
    model: str | None = None
    backend: str | None = None
    python: str | None = None
    pdf_script: str | None = None

    @property
    def ascii_root(self) -> bool:
        return paths.is_ascii(self.root)

    @property
    def gpu_backend_configured(self) -> bool:
        return self.backend in ("cuda", "vulkan")

    def require(self, attr: str, name: str) -> str:
        """Return a tool path, or raise a setup hint naming the tool."""
        from .errors import SetupRequiredError

        value = getattr(self, attr, None)
        if not value or not paths.is_file(value):
            raise SetupRequiredError(
                f"{name} is not available. Run the zoombie setup first "
                "(zoombie-install.cmd, or python -m zoombie.install)."
            )
        return value


def package_dir() -> str:
    """Directory of the ``zoombie`` package itself (``.../zoombie``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli_dir() -> str:
    """The directory containing the package: ``scripts/`` or ``bin/zoombie/``.

    This is where the deployed launcher, ``lib/``-style siblings and the ``pdf/``
    helper live.
    """
    return os.path.dirname(package_dir())


def pdf_helper() -> str:
    """Absolute path of the PDF -> Markdown helper script."""
    return os.path.join(cli_dir(), PDF_HELPER_RELATIVE)


def resolve(model_override: str | None = None) -> Env:
    """Resolve every tool to an absolute path.

    Order is manifest first (what setup installed), then the toolchain bin dirs
    (no PATH needed at all), then PATH. Nothing relies on where a tool was
    installed.
    """
    data = manifest.load()
    m = lambda dotted: manifest.dig(data, dotted)  # noqa: E731 - terse on purpose

    env = Env(
        root=paths.env_root(),
        manifest=data,
        ffmpeg=tools.resolve("ffmpeg", candidates=[m("ffmpeg.path")]),
        ffprobe=tools.resolve("ffprobe", candidates=[m("ffprobe.path")]),
        # yt-dlp is a Python package invoked as `python -m yt_dlp`, not an exe.
        whisper=tools.resolve("whisper-cli", candidates=[m("whisper.path")]),
        backend=m("whisper.backend"),
        python=tools.find_python(),
        pdf_script=pdf_helper(),
    )

    env.model = _resolve_model(model_override, data)
    return env


def _resolve_model(override: str | None, data: dict) -> str | None:
    """Model path: an explicit override, else the manifest's recorded model."""
    if override:
        if paths.is_file(override):
            # Absolute WITHOUT an extended prefix: this path is handed to
            # whisper-cli, which opens it with plain fopen.
            return paths.absolute(override)
        name = override if override.startswith("ggml-") else f"ggml-{override}.bin"
        return os.path.join(paths.env_path("models"), name)
    return manifest.dig(data, "model.path")

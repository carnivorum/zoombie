"""The zoombie toolchain: download, extract, transcribe, PDF -> Markdown.

Everything fragile lives here, in tested Python, rather than in prose an agent
re-reads on every run. The package is deliberately split by concern:

    zoombie.cli            the single entry point (one JSON line on stdout)
    zoombie.commands.*     one module per subcommand
    zoombie.lib.*          shared helpers (paths, tools, whisper, cublas, ...)
    zoombie.install.*      the installer/updater

Naming rules for this package (the PowerShell original was far too verbose):
no ``Zoombie`` in any symbol, no ``Verb-Noun`` pairs, short imperative function
names, and one module per concern so the module name carries the context.
"""

from __future__ import annotations

# Bumped whenever skill content changes, so deployment can tell an installed
# skill is out of date. Every SKILL.md carries the same value in MARKER_KEY.
SKILL_VERSION = "4.0.0"

# Front-matter key written into every SKILL.md we own, to prove ownership.
MARKER_KEY = "cvrm-zoombie-version"

__all__ = ["SKILL_VERSION", "MARKER_KEY"]

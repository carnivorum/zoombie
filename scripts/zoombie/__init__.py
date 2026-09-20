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

# Bumped whenever SKILL content changes, so deployment can tell an installed
# skill is out of date. Every SKILL.md carries the same value in MARKER_KEY.
# NOTE: read_marker() reads this from the FIRST 12 LINES of each SKILL.md, so a
# skill file whose marker line drifted below the front matter silently reads as
# unowned. Keep the marker inside the front matter.
SKILL_VERSION = "4.3.0"

# The version marker written into every SKILL.md we own, to prove ownership.
MARKER_KEY = "cvrm-zoombie-version"

# The deployed revision of the Zoo Code ROLE (see ``modes/``).
#
# Deliberately independent of SKILL_VERSION. A mode entry cannot carry a marker
# key -- the extension validates the mode schema and an unknown field would be
# rejected -- so the role is versioned by CONTENT comparison in
# ``lib/modes.py``, and this value is only ever written to env.json. Editing the
# prompts therefore does NOT churn the five SKILL.md markers the way sharing one
# number would, which is why the two are kept apart.
ROLE_VERSION = "1.0.0"

__all__ = ["SKILL_VERSION", "MARKER_KEY", "ROLE_VERSION"]

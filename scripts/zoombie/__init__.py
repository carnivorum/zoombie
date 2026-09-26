"""The zoombie toolchain: download, extract, transcribe, PDF -> Markdown.

Everything fragile lives here, in tested Python, rather than in prose an agent
re-reads on every run. The package is deliberately split by concern:

    zoombie.cli            the single entry point (one JSON line on stdout)
    zoombie.commands.*     one module per subcommand
    zoombie.lib.*          shared helpers (paths, tools, whisper, cublas, ...)
    zoombie.install.*      the installer/updater

Naming rules: no ``Zoombie`` in any symbol, no ``Verb-Noun`` pairs, short
imperative function names, and one module per concern so the module name carries
the context.
"""

from __future__ import annotations

# Bumped whenever SKILL content changes, so deployment can tell an installed
# skill is out of date. Every SKILL.md carries the same value in MARKER_KEY.
# NOTE: read_marker() reads this from the FIRST 12 LINES of each SKILL.md, so a
# skill file whose marker line drifted below the front matter silently reads as
# unowned. Keep the marker inside the front matter.
#
# 4.5.0: skills share their boilerplate through skills/_shared/, expanded at
# deploy time, and the role prompt is compressed.
# 4.6.0: the cli-resolve block points a user without a launcher at the setup.md
# flow (fetch and follow) rather than the raw bootstrap one-liner.
# 4.7.0: zoombie-pdf-to-md becomes zoombie-images-to-md (text-first; vision/OCR
# only for a page or file with no text layer), zoombie-summarize asks before
# extracting slides, and the new `slides`/`readimages` subcommands ship.
# 4.8.0: every skill carries the shared scratch-note block: temporary files an
# agent writes go under the workspace /.tmp/, never C:\Temp or the workspace root.
# 4.9.0: the MCP facade is registered in the client's own MCP settings at setup
# (lib/mcpsettings + the `mcp` command), so the servers the skills already call are
# reachable in process; the shared cli-resolve block points at the MCP tools first.
# 5.0.0: MCP-first is the whole transport. The shared cli-resolve block drops the
# flag-translation sentence (the tool schemas carry the argument names), each run
# section shows an MCP JSON example with a single CLI-fallback line, and the
# transport-facing prose uses the MCP argument names. Download/extract state that a
# TRANSCRIPT goal hands off to zoombie-transcribe-video, whose pipeline already runs
# download -> extract -> transcribe. Paired with the mcp.py fix that accepts every
# argument its schemas advertise (apply, ocr, vision, audio_only, ...), so the
# WRITE and scan-escalation paths work over MCP at all.
# 5.1.0: zoombie-summarize becomes the FRONT DOOR: it produces its own source
# material (pipeline/transcribe/readpdf/readimages) when the item has none, and
# every producer's description points at it, so one skill answers "make me a
# document" without a hand-rolled two-skill chain.
SKILL_VERSION = "5.1.0"

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
# 1.2.0: the prompt drops what zoombie-summarize already owns and states the
# not-a-coder rule once instead of three times.
ROLE_VERSION = "1.2.0"

__all__ = ["SKILL_VERSION", "MARKER_KEY", "ROLE_VERSION"]

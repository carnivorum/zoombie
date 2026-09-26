"""Where a produced item lands, and what its folder is called.

Two decisions live here, and they must have ONE definition because both the
``summarize`` flow and the ``download`` tool depend on them:

* **Routing.** A source that already sits INSIDE the workspace becomes its own
  item in place -- ``summary.md`` lands literally beside the media, and the
  media is never copied or renamed. A source that is EXTERNAL (a URL, or a path
  outside the workspace) becomes ``<workspace>/_unsorted/<kind>/<name>/``.
* **Naming.** A name is extrapolated from what the source reports (a yt-dlp
  title, a file stem) and reduced to a Windows-safe, human-readable folder name
  -- no special symbols, percent-encoding normalized -- with a fallback when the
  source carries no usable name at all.

The workspace root is the directory the **MCP server was started in** (the
client spawns it with the project as its working directory); ``ZOOMBIE_WORKSPACE_ROOT``
overrides it, which is what a test or a non-standard host uses. Nothing here
reads a name to decide meaning -- the folder name is presentation, exactly as
:mod:`zoombie.item.paths` documents -- so this module never parses one.
"""

from __future__ import annotations

import os
import re
import unicodedata
from urllib.parse import unquote

from . import paths

__all__ = [
    "WORKSPACE_ENV_VAR",
    "UNSORTED_DIR_NAME",
    "KIND_SUMMARIES",
    "KIND_DOWNLOAD",
    "workspace_root",
    "inside_workspace",
    "unsorted_dir",
    "destination_dir",
    "sanitize_name",
    "unique_name",
]

# Override for the workspace root. Unset means "the process working directory",
# which is where the MCP client starts the server.
WORKSPACE_ENV_VAR = "ZOOMBIE_WORKSPACE_ROOT"

# The single folder under the workspace root that collects output for sources
# that are NOT already part of the workspace. Deliberately visible (no dot): a
# user moving a summary must be able to see and move the folder with it.
UNSORTED_DIR_NAME = "_unsorted"

# The two sub-folders of ``_unsorted``. ``summaries`` holds a produced document,
# ``download`` holds a bare media download. Kept as constants so the two callers
# cannot spell them differently.
KIND_SUMMARIES = "summaries"
KIND_DOWNLOAD = "download"

# Characters Windows forbids in a file/folder name, plus the path separators.
_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'

# A percent-escape (``%20``), normalized away before sanitizing so a name that
# arrived URL-encoded is not double-encoded and does not keep the escapes.
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")

# Default cap for a produced folder name. Chosen to leave room for the item
# layout (``summary.md``, ``img``, a media extension) inside the Windows budget.
DEFAULT_NAME_LENGTH = 120


def workspace_root() -> str:
    """Absolute path of the workspace root.

    ``ZOOMBIE_WORKSPACE_ROOT`` wins when set (and non-empty); otherwise the
    process working directory is used, because the MCP client spawns the server
    with the project as its cwd. The path is returned absolute and prefix-free,
    which is the form every other helper and the JSON output expect.
    """
    override = os.environ.get(WORKSPACE_ENV_VAR, "").strip()
    if override:
        return paths.absolute(override)
    return paths.absolute(os.getcwd())


def inside_workspace(path: str, root: str | None = None) -> bool:
    """True when ``path`` is inside the workspace (the root itself counts).

    Both sides are made absolute and case-normalized before the prefix test, so
    ``C:/../repos/kb/Crimson/x.mp4`` and ``<ws>\\Crimson\\x.mp4`` compare equal
    on Windows. The test is on WHOLE path segments -- ``<ws>\\foo`` must not be
    read as inside ``<ws>\\foobar`` -- which is what the trailing-separator
    comparison below guarantees.
    """
    if not path:
        return False
    base = os.path.normcase(paths.absolute(root if root is not None else workspace_root()))
    target = os.path.normcase(paths.absolute(path))
    if base == target:
        return True
    return target.startswith(base + os.sep)


def unsorted_dir(kind: str, root: str | None = None) -> str:
    """``<workspace>/_unsorted/<kind>`` (not created here)."""
    if kind not in (KIND_SUMMARIES, KIND_DOWNLOAD):
        raise ValueError(f"unknown unsorted kind: {kind!r}")
    return os.path.join(root if root is not None else workspace_root(),
                        UNSORTED_DIR_NAME, kind)


def destination_dir(
    source: str,
    kind: str,
    *,
    root: str | None = None,
    name: str | None = None,
) -> tuple[str, bool]:
    """The folder a produced item (or download) goes into.

    Returns ``(path, internal)``:

    * ``internal`` is ``True`` when ``source`` is a real path INSIDE the
      workspace: the destination is the source's own folder (item) or the file
      itself is left where it is (download), and ``name`` is ignored -- the
      media keeps its own name, which is the documented rule.
    * ``internal`` is ``False`` for a URL, or a path outside the workspace: the
      destination is ``<workspace>/_unsorted/<kind>/<sanitized-name>/``, with
      the name extrapolated by :func:`sanitize_name` when the caller gives none.

    ``source`` being a URL is detected by a scheme, so a download that is not a
    local file never falls into the in-place branch.
    """
    is_url = bool(re.match(r"^[a-z][a-z0-9+.-]*://", source or "", re.IGNORECASE))
    if not is_url and inside_workspace(source, root):
        # In place: the item IS the source's folder. For a bare download the
        # caller wants the containing folder of the file, which is the same
        # answer -- the media is never moved.
        return os.path.dirname(paths.absolute(source)), True

    chosen = sanitize_name(name or _stem_of(source))
    return os.path.join(unsorted_dir(kind, root), chosen), False


def _stem_of(source: str) -> str:
    """A best-effort name from a URL or a path.

    For a path it is the file stem; for a URL it is the last non-empty path
    segment, percent-decoded. Empty when neither yields anything, which the
    caller's :func:`sanitize_name` then turns into the ``"item"`` fallback.
    """
    text = (source or "").strip()
    if not text:
        return ""
    # A URL: take the final path segment (before any query/fragment).
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        from urllib.parse import urlsplit

        tail = urlsplit(text).path.rstrip("/").rsplit("/", 1)[-1]
        return unquote(tail) if tail else ""
    name = os.path.basename(os.path.normpath(text))
    stem, dot, _extension = name.rpartition(".")
    return stem if dot else name


def sanitize_name(name: str | None, *, max_len: int = DEFAULT_NAME_LENGTH) -> str:
    """A safe, human-readable folder name from arbitrary source text.

    The rules, in order: percent-escapes are decoded first (so a URL-encoded
    name is normalized rather than double-encoded), the string is NFC-normalized
    and fullwidth punctuation is folded to its ASCII form, every character
    Windows forbids is dropped, runs of whitespace collapse to one space, and a
    trailing dot or space is trimmed (NTFS silently strips both). The stem is
    capped at ``max_len``. An empty result becomes ``"item"`` so a caller always
    receives a usable name and never has to branch on emptiness.
    """
    text = unquote(name or "") if _PERCENT_ESCAPE_RE.search(name or "") else (name or "")
    text = unicodedata.normalize("NFC", text)

    # Fullwidth forms (U+FF01..U+FF5E) fold to ASCII by a fixed offset; an
    # ideographic space becomes a normal space. A ``？`` (U+FF1F) renders like
    # ``?`` but is a different code point, which is exactly the case that once
    # produced ``... сегодня？`` and forced a percent-encoded link.
    folded = "".join(
        chr(ord(ch) - 0xFEE0) if "\uff01" <= ch <= "\uff5e"
        else " " if ch == "\u3000"
        else ch
        for ch in text
    )

    cleaned = "".join(
        ch for ch in folded if ch not in _ILLEGAL_NAME_CHARS and ord(ch) >= 32
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or "item"


def unique_name(directory: str, name: str) -> str:
    """``name``, or ``name (2)``, ``name (3)`` ... until it is free in ``directory``.

    Used when creating a folder so an extrapolated name can never silently
    collide with existing work. Comparison is case-insensitive, matching NTFS.
    """
    candidate = name
    index = 1
    while paths.exists(os.path.join(directory, candidate)):
        index += 1
        candidate = f"{name} ({index})"
    return candidate

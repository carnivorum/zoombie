"""Range-based, idempotent editing of a generated ``summary.md``.

The one rule that makes every pass here safe to re-run: **the unit of work is a
range, never a heading string**. A pass locates a section with
:func:`block_range` (``[end of the heading line, start of the next ``##``
heading)``), strips whatever a previous run wrote inside that range, and writes
the freshly generated text back. Matching on a header *string* instead is what
produces duplicated blocks on the second run, so no function here does it.

Two conventions are shared with the skills and must not drift:

* every heading in block 6 is prefixed with ``<a id="s-N"></a>``, where the
  prefix and the id come from :data:`ANCHOR_PREFIX` / :func:`anchor_id` -- the
  index in block 4 links to exactly those ids;
* inserted images carry their own storage prefix, and
  :func:`strip_inserted_images` removes a whole *run* of them at once so a
  previously corrupted document (images glued to each other and to the next
  paragraph) is repaired rather than duplicated.

The insertion newline discipline is strict on purpose: every insertion emits a
leading ``\\n`` and, unless the next character is already a newline, a trailing
``\\n``. Omitting the trailing one glues the image to the following paragraph
(``![..]Идем дальше.``), which is both ugly and unrecoverable by the strip pass.

Nothing in this module reads or writes files -- callers pass and receive text.
"""

from __future__ import annotations

import os
import re

__all__ = [
    "ANCHOR_PREFIX",
    "anchor_id",
    "block_range",
    "replace_range",
    "heading_list",
    "assign_anchors",
    "render_index",
    "strip_inserted_images",
    "insert_after_paragraph",
    "ensure_trailing_newline",
    "collapse_blank_runs",
]

# Single source of truth for the anchor convention: block 4 links to ``#s-N``
# and block 6 headings carry ``<a id="s-N"></a>``. Both are built from this
# constant so the two halves of the document cannot drift apart.
ANCHOR_PREFIX = "s"

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", re.MULTILINE)
_BLOCK_HEADING_RE = re.compile(r"^##[ \t]+", re.MULTILINE)
# Only tags whose id matches our own prefix are recognized, so a foreign anchor
# in the surrounding prose is neither adopted nor mistaken for ours.
_ANCHOR_RE = re.compile(rf"<a id=\"({ANCHOR_PREFIX}-\d+)\"></a>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
# One or more Markdown images pointing into an ``img/`` folder, anchored to a
# line start. Matching a *run* collapses the historical bug where images were
# glued both to each other and to the following paragraph
# (``![a]![b]Идем дальше.``) into a single match, so a re-run strips them
# cleanly instead of stacking duplicates. Whatever text follows the run on the
# same line is preserved by the ``keep`` group.
INSERTED_IMG_RE = re.compile(
    r"^[ \t]*(?:!\[[^\]]*\]\(<?[^)\n]*img/[^)\n]*>?\))+[ \t]*(?P<keep>[^\n]*)",
    re.MULTILINE,
)


def anchor_id(n: int) -> str:
    """The anchor id for the ``n``-th heading in reading order."""
    return f"{ANCHOR_PREFIX}-{n}"


# ---------------------------------------------------------------------------
# ranges
# ---------------------------------------------------------------------------

def block_range(text: str, heading_pattern: str) -> tuple[int, int] | None:
    """Bounds of a ``## ...`` section: ``(body_start, body_end)``.

    ``body_start`` is the offset just past the newline that ends the matched
    heading line, and ``body_end`` is the offset of the next ``##`` heading at
    line start, or ``len(text)`` when the section is last. Both offsets are
    valid clamp targets for :func:`replace_range`.

    The section is found by regex on its heading *line*; all subsequent work
    stays inside the returned range, which is what keeps the pass idempotent.
    Returns ``None`` when nothing matches.
    """
    match = re.search(heading_pattern, text, re.MULTILINE)
    if match is None:
        return None
    start = text.find("\n", match.end())
    body_start = len(text) if start < 0 else start + 1
    nxt = _BLOCK_HEADING_RE.search(text, body_start)
    body_end = nxt.start() if nxt else len(text)
    return body_start, body_end


def replace_range(text: str, start: int, end: int, new: str) -> str:
    """Replace ``text[start:end]`` with ``new``, clamping both bounds.

    Clamping matters because callers compute offsets from one version of the
    text and then apply several edits; an out-of-range pair must degrade to
    ``text + new``, never raise or silently drop a slice.
    """
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    return text[:start] + new + text[end:]


# ---------------------------------------------------------------------------
# headings
# ---------------------------------------------------------------------------

def _strip_anchor(rest: str) -> str:
    """Drop a leading ``<a id="s-N"></a>`` from a heading's text, if present."""
    match = _ANCHOR_RE.match(rest)
    if match is None:
        return rest
    return rest[match.end() :].lstrip()


def heading_list(text: str) -> list[dict]:
    """Ordered ``###`` and deeper headings with ``level``, ``title``, offsets.

    ``start`` is the offset of the heading line itself (useful for range work)
    and ``anchor`` is the bare id the heading already carries (``"s-2"``), or
    ``""`` when it has none. Bare ids are what :func:`render_index` needs for
    ``(#s-2)``, which is why the tag is unwrapped here rather than returned
    verbatim.
    """
    headings: list[dict] = []
    for match in _HEADING_RE.finditer(text):
        level = len(match.group(1))
        if level < 3:
            continue
        rest = match.group(2)
        anchor_match = _ANCHOR_RE.match(rest)
        headings.append(
            {
                "level": level,
                "title": _strip_anchor(rest).strip(),
                "start": match.start(),
                "anchor": anchor_match.group(1) if anchor_match else "",
            }
        )
    return headings


def assign_anchors(text: str) -> tuple[str, list[dict]]:
    """Number every ``###`` (and deeper) heading and attach its anchor tag.

    Each heading becomes ``### <a id="s-N"></a>Title`` with ``N`` assigned in
    reading order. The pass is **idempotent**: an existing tag is replaced
    rather than stacked, so running it twice yields byte-identical output. It
    also normalizes the tag form and position, which means a document written by
    hand (or by the reference project) converges to the same bytes after one
    run.

    Returns the rewritten text and the heading list describing the *new*
    numbering, ready to hand to :func:`render_index`.
    """
    out: list[str] = []
    headings: list[dict] = []
    cursor = 0
    for match in _HEADING_RE.finditer(text):
        level = len(match.group(1))
        if level < 3:
            continue
        title = _strip_anchor(match.group(2)).strip()
        anchor = anchor_id(len(headings) + 1)

        out.append(text[cursor : match.start()])
        out.append(f"{'#' * level} <a id=\"{anchor}\"></a>{title}")
        cursor = match.end()
        headings.append(
            {"level": level, "title": title, "start": match.start(), "anchor": anchor}
        )
    out.append(text[cursor:])
    return "".join(out), headings


# ---------------------------------------------------------------------------
# the block-4 index
# ---------------------------------------------------------------------------

def render_index(headings: list[dict]) -> str:
    """Render the block-4 body: one bullet per heading, indented by level.

    ``- [HH:MM:SS — Title](#s-N)`` when the heading carries a timestamp, else
    ``- [Title](#s-N)``. Levels deeper than 3 are indented by two spaces each
    (the ``###`` level itself is not indented). The separator is an em dash
    U+2014 with a space on either side, matching the heading text produced by
    the timestamp pass.

    An empty list still produces a body -- a section 4 that silently keeps
    stale entries would be worse than one that says so.
    """
    if not headings:
        return "_(содержание недоступно)_\n"

    lines: list[str] = []
    for heading in headings:
        indent = "  " * max(0, int(heading.get("level", 3)) - 3)
        stamp = heading.get("timestamp")
        title = str(heading.get("title", "")).strip()
        anchor = heading.get("anchor") or ""
        label = f"{stamp} \u2014 {title}" if stamp else title
        lines.append(f"{indent}- [{label}](#{anchor})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# inline images
# ---------------------------------------------------------------------------

def strip_inserted_images(body: str) -> str:
    """Remove the image links a previous run inserted, keeping other text.

    The regex matches a whole *run* of consecutive ``![...](...img/...)`` links
    and preserves whatever follows on the last matched line, so a document that
    an older broken run corrupted (images glued to each other, and to the next
    paragraph) is repaired losslessly instead of losing prose. The pass is
    repeated until it stops changing the text, then blank runs are collapsed.
    """
    def _keep(match: re.Match[str]) -> str:
        return match.group("keep")

    cleaned = body
    for _ in range(5):
        updated = INSERTED_IMG_RE.sub(_keep, cleaned)
        if updated == cleaned:
            break
        cleaned = updated
    return collapse_blank_runs(cleaned)


def insert_after_paragraph(body: str, offset: int, links: list[str]) -> str:
    """Insert image links at ``offset``, with a blank line on either side.

    ``links`` are complete Markdown images, one per line. The leading ``\\n`` is
    unconditional; the trailing one is emitted unless the next character is
    already a newline or the insertion is at end-of-text. Missing it glues the
    image to the next paragraph, which also defeats
    :func:`strip_inserted_images`. ``offset`` is clamped into ``[0, len(body)]``
    so a stale offset from an earlier edit cannot corrupt the string.
    """
    if not links:
        return body
    offset = max(0, min(offset, len(body)))
    nxt = body[offset : offset + 1]
    trailer = "\n" if nxt and nxt != "\n" else ""
    return body[:offset] + "\n" + "\n".join(links) + trailer + body[offset:]


# ---------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------

def ensure_trailing_newline(text: str) -> str:
    """Return ``text`` with exactly one trailing newline (empty stays empty)."""
    if not text:
        return text
    return text.rstrip("\n") + "\n"


def collapse_blank_runs(text: str) -> str:
    """Collapse runs of three or more newlines to a single blank line."""
    return _BLANK_RUN_RE.sub("\n\n", text)

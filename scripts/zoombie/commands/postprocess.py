"""``postprocess``: the mechanical half of ``summary.md`` production.

The ``zoombie-summarize`` skill writes the PROSE. Everything in a ``summary.md``
that is *mechanical* -- anchors, timestamps, the regenerated index, link repair,
inline-image re-insertion -- lives here, in tested Python, so it is not
re-improvised on every run and cannot silently drift between runs.

The orchestration is driven by these primitives, with the shared helpers in
:mod:`zoombie.lib.markdown`, :mod:`zoombie.lib.srt` and
:mod:`zoombie.lib.textnorm` doing the actual work.

Order of operations (each one is idempotent, and the whole run is):

1. :func:`zoombie.lib.markdown.assign_anchors` numbers every block-6 ``###``
   heading and attaches ``<a id="s-N"></a>``.
2. Heading timestamps come from the sibling SRT (:class:`zoombie.lib.srt.SrtIndex`).
3. Block 4 is regenerated with :func:`zoombie.lib.markdown.render_index`.
4. Link destinations are percent-encoded and link labels de-bracketed.
5. When the document is PDF-derived, previously inserted images are stripped and
   re-inserted from ``img/manifest.json``.
6. Blank runs are collapsed and exactly one trailing newline is ensured.

**Dry run by default.** The document is never touched unless ``-Apply`` is
given, and even then the new text is built entirely in memory and written once,
so a failure halfway through can never leave a half-rewritten file behind. This
is deliberate: a skill calls this command, and a skill must never be able to
corrupt a document by accident.

The acceptance criterion is byte-level idempotency: a second ``-Apply`` on an
unchanged document must leave the file byte-identical. Every pass therefore
works on a *range*, never on a heading string, and every pass rebuilds its
output from the document rather than appending to it.
"""

from __future__ import annotations

import json
import os
import re

from ..cli import Outcome
from ..lib import markdown as md, paths, process
from ..lib.errors import ZoombieError
from ..lib.srt import SrtIndex, parse
from ..lib.textnorm import hhmmss, norm, percent_encode_dest

# Section heading locators. They match the ``## N. Title`` line, so the range a
# pass edits is always delimited by structure, not by the exact title text --
# an agent that rephrases "4. Содержание" must not silently break the pass.
SECTION2_RE = r"^##\s*2\."
SECTION4_RE = r"^##\s*4\."
SECTION5_RE = r"^##\s*5\."
SECTION6_RE = r"^##\s*6\."

# ``<base>.md`` / ``<base>.txt`` / ``<base>.srt`` reference inside a block-5
# "related articles" link. Recognized to route the link to the right resolver.
MD_REF_RE = re.compile(r"\.md$", re.IGNORECASE)

# A stamped heading is ``HH:MM:SS — Title``. Only the heading *text* after the
# anchor tag is inspected, and the stamp is stripped before re-stamping, which is
# what keeps a second run from producing ``00:01:00 — 00:01:00 — T``.
STAMP_RE = re.compile(r"^(\d{2}:(\d{2}):(\d{2}))\s+\u2014\s+")

# A URL scheme or protocol-relative prefix. Such a destination is never touched:
# it is not a path and has nothing on disk to resolve.
ABSOLUTE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)

# ``![alt](dest)`` -- used to enumerate image links for the report. The
# destination is re-extracted with the balanced scanner, never from this match.
IMG_ALT_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<dest>[^)\n]*)\)")

TIMESTAMP_WINDOWS: tuple[int, ...] = (14, 8, 6, 5, 4, 3)


# --------------------------------------------------------------------------- #
# low-level link handling
# --------------------------------------------------------------------------- #

def scan_destination(text: str, start: int) -> tuple[str, int] | None:
    """Read a Markdown destination starting at ``start`` using balanced parens.

    ``start`` points just past the ``](`` of a link. The destination ends at the
    first ``)`` that is not matched by an opening ``(`` *inside* the destination,
    which is what keeps ``(стр. 5)`` inside a file name from truncating the link.
    An optional ``<...>`` wrapper is unwrapped. Returns
    ``(destination, index_of_closing_paren)``, or ``None`` when the destination
    is unterminated.

    This is a single shared primitive so the rewriter and the verifier can never
    disagree about where a destination ends.
    """
    if text.startswith("<", start):
        close = text.find(">", start + 1)
        if close < 0:
            return None
        return text[start + 1 : close], close + 1

    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return text[start:index], index
            depth -= 1
        index += 1
    return None


def _label_start(text: str, closing: int) -> int:
    """Offset of the ``[`` opening the label that closes at ``closing``.

    Scanning backwards with bracket balance -- rather than ``rfind("[")`` -- is
    what makes a label that itself contains brackets (``[см. [1]]``) resolve to
    the OUTER bracket. ``rfind`` finds the inner ``[`` and reports the label as
    ``"1]"``, which is the bug this replaces. An unbalanced label falls back to
    ``rfind`` so a malformed link is still repaired rather than skipped.
    """
    depth = 1
    position = closing - 1
    while position >= 0:
        char = text[position]
        if char == "]":
            depth += 1
        elif char == "[":
            depth -= 1
            if depth == 0:
                return position
        position -= 1
    return text.rfind("[", 0, closing)


def iter_link_spans(text: str):
    """Yield ``(label_start, label, dest_start, dest_end)`` for every inline link.

    ``label_start`` is the offset of the ``[`` itself; ``label`` is the text
    without the brackets. Offsets point into ``text`` and are what makes the
    rewriting pass a set of range replacements: editing by offset rather than by
    re-joining the string means a destination that contains a newline (a
    multi-line link) survives the pass instead of collapsing into one line.
    """
    index = 0
    while True:
        closing = text.find("](", index)
        if closing < 0:
            return
        label_start = _label_start(text, closing)
        if label_start < 0:
            index = closing + 2
            continue
        scanned = scan_destination(text, closing + 2)
        if scanned is None:
            return
        dest, dest_end = scanned
        yield label_start, text[label_start + 1 : closing], closing + 2, dest_end
        index = dest_end + 1


def rewrite_links(text: str) -> tuple[str, int]:
    """Repair link labels and destinations; return ``(text, links_rewritten)``.

    Two fixes:

    * a destination is run through :func:`percent_encode_dest`, which encodes
      space/tab/parens/brackets/angle/quote and leaves Cyrillic (and anything
      already encoded) byte-for-byte intact;
    * square brackets inside a LABEL are turned into parentheses, because
      ``[см. [1]]`` is a nested link reference to every Markdown parser.
      (An earlier implementation additionally skipped absolute URLs; that check
      is dropped because :func:`percent_encode_dest` is already a no-op on them --
      a URL contains none of the characters it encodes.)

    The edits are applied as ordered range replacements from the end backwards,
    so offsets computed on the original text stay valid throughout.
    """
    edits: list[tuple[int, int, str]] = []
    changed_links = 0
    for label_start, label, dest_start, dest_end in iter_link_spans(text):
        new_label = label.translate(_LABEL_FIX) if ("[" in label or "]" in label) else label
        new_dest = percent_encode_dest(text[dest_start:dest_end])
        touched = False
        if new_label != label:
            # ``label_start`` is the offset of ``[``; its contents start one later.
            edits.append((label_start + 1, label_start + 1 + len(label), new_label))
            touched = True
        if new_dest != text[dest_start:dest_end]:
            edits.append((dest_start, dest_end, new_dest))
            touched = True
        if touched:
            changed_links += 1

    if not edits:
        return text, 0

    # Applied end-first, so every offset -- computed against the original text --
    # is still valid at the moment it is used.
    edits.sort(key=lambda edit: edit[0], reverse=True)
    out = text
    for start, end, replacement in edits:
        out = out[:start] + replacement + out[end:]
    return out, changed_links


_LABEL_FIX = str.maketrans({"[": "(", "]": ")"})


# --------------------------------------------------------------------------- #
# block 6: headings and their first content paragraph
# --------------------------------------------------------------------------- #

def _split_heading(title: str) -> tuple[str, str | None]:
    """Split a heading's text into ``(title, optional_stamp)``.

    Stripping the existing stamp is what makes the timestamp pass idempotent: the
    timestamp comes from the SRT on every run, so the heading is rebuilt from its
    real title and never accumulates ``00:01:00 — 00:01:00 — …``.
    """
    match = STAMP_RE.match(title)
    if match is None:
        return title, None
    return title[match.end() :].strip(), match.group(1)


def _first_paragraph(text: str, body_start: int, body_end: int) -> str | None:
    """First non-blank, non-``<!-- -->`` line of a block, or ``None``.

    Blank lines and HTML comments are skipped.
    Lines starting with ``>`` are unwrapped before the check, because a heading
    followed only by a blockquote note is still content worth anchoring.

    ``body_start`` must already be past the heading's own line and ``body_end``
    must be the NEXT heading (see :func:`heading_bodies`); feeding it a whole
    section would return the heading line itself as the "paragraph", which is the
    bug that would silently stamp every heading with its own title's search miss.
    """
    for line in text[body_start:body_end].split("\n"):
        candidate = line.strip().lstrip(">").strip()
        if not candidate or candidate.startswith("<!--"):
            continue
        return candidate
    return None


def heading_bodies(
    text: str, headings: list[dict], block_end: int
) -> list[tuple[dict, str | None]]:
    """Pair each heading with the first content paragraph *of its own block*.

    The stop offset is the next heading's start, not the end of the section --
    exactly the heading-block boundary. Without it, a divider heading
    immediately followed by another heading would adopt that next heading's line
    as its "first paragraph" and match a cue it never spoke.
    """
    pairs: list[tuple[dict, str | None]] = []
    for position, heading in enumerate(headings):
        line_end = text.find("\n", heading["start"])
        start = len(text) if line_end < 0 else line_end + 1
        stop = headings[position + 1]["start"] if position + 1 < len(headings) else block_end
        if stop < start:
            stop = start
        pairs.append((heading, _first_paragraph(text, start, stop)))
    return pairs


def _headings_in_range(text: str, start: int, end: int) -> list[dict]:
    """``###``-and-deeper headings inside ``[start, end)``, in reading order.

    :func:`zoombie.lib.markdown.assign_anchors` numbered the headings across the
    whole document; this narrows that list to block 6 by offset, which is
    range-based and therefore immune to a heading being renamed.
    """
    return [heading for heading in md.heading_list(text) if start <= heading["start"] < end]


def stamp_headings(
    text: str,
    index: SrtIndex | None,
    heading_start: int,
    heading_end: int,
) -> tuple[dict[str, str], list[str], int]:
    """Resolve a timestamp for every heading in block 6 from the SRT.

    Returns ``(stamps, unmatched_titles, timestamped_count)`` where ``stamps`` maps
    a heading's bare anchor id to its ``HH:MM:SS`` stamp.

    The matching itself is delegated to :meth:`SrtIndex.find`, which already
    implements the reference rules: word windows of length 14 down to 5, longest
    match wins, a monotonic cursor so headings keep reading order, and one retry
    from offset 0 when a heading points *backwards* (a summary written out of
    speech order). Only the window *set* is passed in as a parameter, so the rule
    is visible here rather than buried.

    Two post-passes, both ported:

    * a **backwards pass** lets a heading that matched nothing inherit the next
      stamped heading's time -- this is what timestamps section dividers such as
      ``Часть 2`` / ``Дополнительные материалы``, which have no speech of their
      own;
    * a heading still without a time gets NO timestamp and is reported as
      ``unmatched``, never guessed.
    """
    headings = _headings_in_range(text, heading_start, heading_end)
    bodies = heading_bodies(text, headings, heading_end)
    stamps: dict[str, str] = {}
    seconds: list[float | None] = []

    cursor = 0
    for _heading, paragraph in bodies:
        if index is None:
            seconds.append(None)
            continue
        words = norm(paragraph).split() if paragraph else []
        found = index.find(words, cursor) if words else None
        if found is None:
            # ``SrtIndex.find`` already retried from offset 0 on a miss; a second
            # explicit retry here would repeat the identical search.
            seconds.append(None)
            continue
        seconds.append(index.time_at(found))
        cursor = max(cursor, found)

    # Backwards inheritance: an unstamped divider takes the NEXT stamped time.
    for position in range(len(seconds) - 1, -1, -1):
        if seconds[position] is None and position + 1 < len(seconds):
            seconds[position] = seconds[position + 1]

    timestamped = 0
    unmatched: list[str] = []
    for (heading, paragraph), value in zip(bodies, seconds):
        if value is None:
            # Still unstamped after inheritance: reported, never guessed.
            unmatched.append(heading["title"])
            continue
        stamps[heading["anchor"]] = hhmmss(value)
        if paragraph:
            timestamped += 1
    return stamps, unmatched, timestamped


def apply_stamps(text: str, stamps: dict[str, str], heading_start: int, heading_end: int) -> str:
    """Rewrite block-6 heading lines with their anchor and timestamp.

    ``## ``, not ``### ``: :func:`zoombie.lib.markdown.assign_anchors` re-emits
    each heading as ``<hashes> <a id="s-N"></a><title>``, so the anchor tag sits
    between the hashes and the title. This pass strips both the tag and any
    previously written stamp and writes exactly one of each, in that order --
    converging a hand-written heading onto the same bytes as a generated one.

    Edits are applied from the end backwards so the offsets collected before any
    edit stay valid.
    """
    headings = _headings_in_range(text, heading_start, heading_end)
    edits: list[tuple[int, int, str]] = []
    for heading in headings:
        line_end = text.find("\n", heading["start"])
        if line_end < 0:
            line_end = len(text)
        title, _ = _split_heading(heading["title"])
        stamp = stamps.get(heading["anchor"])
        prefix = f"{stamp} \u2014 " if stamp else ""
        edits.append(
            (
                heading["start"],
                line_end,
                f"{'#' * heading['level']} {title_tag(heading['anchor'])}{prefix}{title}",
            )
        )

    out = text
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
        out = out[:start] + replacement + out[end:]
    return out


def title_tag(anchor: str) -> str:
    """The anchor tag emitted in front of a block-6 heading title."""
    return f'<a id="{anchor}"></a>'


def heading_start(text: str) -> int | None:
    """Offset of the first block-6 heading line, or ``None``.

    Deliberately based on the first ``###`` heading rather than on the ``## 6.``
    line: a document is block-6-shaped as soon as it has sub-headings, and an
    agent renaming the section must not disable the timestamp pass.
    """
    headings = md.heading_list(text)
    return headings[0]["start"] if headings else None


# --------------------------------------------------------------------------- #
# block 4: the index
# --------------------------------------------------------------------------- #

def build_index_entries(text: str, heading_start: int, heading_end: int, stamps: dict[str, str]) -> list[dict]:
    """The heading list :func:`zoombie.lib.markdown.render_index` consumes.

    Titles are read back off the *rewritten* heading lines, so the index entry and
    the heading it links to cannot drift: one source of truth, two renderings.
    """
    entries: list[dict] = []
    for heading in _headings_in_range(text, heading_start, heading_end):
        title, stamp = _split_heading(heading["title"])
        entries.append(
            {
                "level": heading["level"],
                "title": title,
                "anchor": heading["anchor"],
                "timestamp": stamps.get(heading["anchor"]) or stamp or "",
            }
        )
    return entries


def replace_index(text: str, entries: list[dict]) -> tuple[str, bool]:
    """Replace the block-4 body with a freshly rendered index.

    Range-based, never header-string-based: the body between the ``## 4.`` line
    and the next ``## `` heading is replaced wholesale, so a re-run rewrites the
    same bytes and a stale index (or a stray paragraph an agent left there) is
    removed rather than kept.
    """
    bounds = md.block_range(text, SECTION4_RE)
    if bounds is None:
        return text, False
    body = md.render_index(entries) + "\n"
    return md.replace_range(text, bounds[0], bounds[1], body), True


# --------------------------------------------------------------------------- #
# inline images
# --------------------------------------------------------------------------- #

def load_manifest(image_dir: str) -> dict | None:
    """Read ``img/manifest.json``; ``None`` when missing or unreadable.

    An unreadable manifest is treated as "not ours": silently inserting nothing is
    safer than guessing anchors from a corrupt file.
    """
    path = os.path.join(image_dir, "manifest.json")
    if not paths.is_file(path):
        return None
    try:
        with open(paths.to_extended(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("images"), list):
        return None
    return payload


def image_url_prefix(image_dir: str) -> str:
    """The destination prefix for images, relative to the Markdown file.

    The layout that matters is the skill's: ``summary.md`` sits beside ``img/``, so
    the links are written as exactly ``img/<file>``. A directory named anything
    else falls back to its own basename so the links still resolve.
    """
    name = os.path.basename(os.path.normpath(image_dir))
    return f"{name}/" if name else "img/"


def image_link(prefix: str, file_name: str) -> str:
    """One Markdown image line, with the destination correctly encoded."""
    return f"![{file_name}]({percent_encode_dest(prefix + file_name)})"


def insert_images(text: str, manifest: dict | None, prefix: str) -> tuple[str, int, int]:
    """Strip a previous run's images, then re-insert them from the manifest.

    ``strip_inserted_images`` runs first so a re-run removes the previous run's
    images before adding them again -- that ordering is what makes the pass
    idempotent instead of stacking duplicates.

    Anchoring is a two-pass rule, in order:

    1. match ``anchor_text`` (normalized) against the paragraphs of block 6;
    2. fall back to ``page_title``;
    3. fall back to the nearest *previous* matched image, preserving reading
       order for a figure whose anchor prose the agent paraphrased away.

    Returns ``(text, placed, skipped)``; ``skipped`` counts manifest entries that
    had no anchor at all (they are NOT dumped at the top of the document -- a
    missing figure is a report line, not a reason to corrupt the layout).

    Offsets are re-sliced after every insertion, because an insertion shifts
    everything after it; inserting into a stale offset list is the bug this
    guards against.
    """
    if not manifest:
        return text, 0, 0

    text = md.strip_inserted_images(text)

    bounds = md.block_range(text, SECTION6_RE)
    if bounds is None:
        return text, 0, len(manifest.get("images", []))

    body_start, body_end = bounds
    images = [entry for entry in manifest["images"] if isinstance(entry, dict) and entry.get("file")]

    # Paragraph offsets are collected once and then kept *relative* to the body
    # range, which is re-sliced on every edit below.
    paragraphs = _paragraph_offsets(text, body_start, body_end)
    paragraphs_norm = [norm(text[start:end]) for start, end in paragraphs]

    placed = 0
    skipped = 0
    cursor = 0
    last_index: int | None = None
    # Paragraph index -> absolute offset just past the image run already inserted
    # for it. Two figures that share an anchor must end up one after the other in
    # manifest order; inserting both "after the paragraph" would instead push the
    # later image in front of the earlier one, because each insertion happens at
    # the same spot.
    tails: dict[int, int] = {}

    for entry in images:
        offset = _match_anchor(paragraphs_norm, entry.get("anchor_text"), cursor)
        if offset is None:
            offset = _match_anchor(paragraphs_norm, entry.get("page_title"), 0)
        if offset is None:
            # Nearest-neighbour: hang off the previous matched image.
            offset = last_index
        if offset is None:
            skipped += 1
            continue

        # Clamp: an insertion changes the paragraph list, so an offset carried
        # over by the nearest-neighbour fallback can now be one past the end.
        # Clamping keeps the slice well-formed; skipping would silently drop the
        # figure instead.
        if not paragraphs:
            skipped += 1
            continue
        offset = max(0, min(offset, len(paragraphs) - 1))

        link = image_link(prefix, entry["file"])
        if offset in tails:
            # Continue the run already open for this paragraph.
            insert_at = tails[offset]
        else:
            # ``insert_after_paragraph`` emits the leading newline that creates
            # the blank line above the image, so the offset handed to it is the
            # START of the line following the paragraph -- not the end of the
            # paragraph's text, which would glue the image onto that line.
            _paragraph_start, paragraph_stop = paragraphs[offset]
            insert_at = paragraph_stop
            if text[insert_at:insert_at + 1] == "\n":
                insert_at += 1

        body = text[body_start:body_end]
        body = md.insert_after_paragraph(body, insert_at - body_start, [link])
        text = text[:body_start] + body + text[body_end:]
        body_end = body_start + len(body)

        placed += 1
        cursor = offset
        last_index = offset
        tails[offset] = insert_at + 1 + len(link)
        # Every offset after the insertion has moved; rebuild both lists.
        paragraphs = _paragraph_offsets(text, body_start, body_end)
        paragraphs_norm = [norm(text[start:end]) for start, end in paragraphs]
    return text, placed, skipped


def _paragraph_offsets(text: str, body_start: int, body_end: int) -> list[tuple[int, int]]:
    """Start/text-end offsets of the paragraphs inside ``[body_start, body_end)``.

    A paragraph is a maximal run of non-blank lines; a heading or an already
    inserted image line starts a fresh one. The second offset is the end of the
    paragraph's last line of TEXT -- its trailing newline is deliberately not
    included, so inserting "after the paragraph" lands on the same bytes whether
    or not the document ends with a final newline (the insertion's trailing
    newline is suppressed when the next character is already one).
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    line_end = body_start
    position = body_start
    for line in text[body_start:body_end].split("\n"):
        stripped = line.strip()
        hashes = len(stripped) - len(stripped.lstrip("#"))
        is_break = (
            not stripped
            or stripped.startswith("![")
            or (0 < hashes <= 6 and stripped[hashes:hashes + 1] in (" ", "\t"))
        )
        if is_break:
            if start is not None:
                spans.append((start, line_end))
                start = None
        else:
            if start is None:
                start = position
            line_end = position + len(line)
        position += len(line) + 1
    if start is not None:
        spans.append((start, line_end))
    return spans


def _match_anchor(paragraphs_norm: list[str], anchor: str | None, cursor: int) -> int | None:
    """Index of the paragraph matching ``anchor``, longest window first.

    The anchor is normalized the same way the paragraph is (``ё`` folded, case
    dropped, punctuation to spaces -- :func:`zoombie.lib.textnorm.norm`), then
    searched as word windows from longest to shortest. Matching a *window* rather
    than a prefix is what tolerates the small edits an agent makes when it
    transcribes the PDF's sentence into prose.
    """
    if not anchor:
        return None
    words = norm(anchor).split()
    if not words:
        return None

    for length in TIMESTAMP_WINDOWS:
        if length > len(words):
            continue
        for start in range(0, min(len(words) - length, 30) + 1):
            phrase = " ".join(words[start : start + length])
            for index in range(cursor, len(paragraphs_norm)):
                if phrase in paragraphs_norm[index]:
                    return index
    return None


# --------------------------------------------------------------------------- #
# the whole document
# --------------------------------------------------------------------------- #

def process_document(text: str, srt_path: str | None, image_dir: str) -> tuple[str, dict]:
    """Run every pass over one document; return ``(new_text, stats)``.

    Pure with respect to the filesystem except for *reading* the SRT and the image
    manifest, which makes it directly unit-testable and keeps the dry-run path
    free of any write.
    """
    original = text
    stats: dict = {
        "headings": 0,
        "timestamped": 0,
        "unmatched": [],
        "indexEntries": 0,
        "linksRewritten": 0,
        "imagesPlaced": 0,
        "imagesSkipped": 0,
    }

    # 1. anchors (also normalizes any hand-written ones into our form)
    text, headings = md.assign_anchors(text)

    # 2./3. timestamps + index, both scoped to the sub-heading region.
    start = heading_start(text)
    if start is not None:
        end = len(text)
        cues = parse(srt_path) if srt_path else []
        index = SrtIndex(cues) if cues else None
        stamps, unmatched, timestamped = stamp_headings(text, index, start, end)
        text = apply_stamps(text, stamps, start, end)
        entries = build_index_entries(text, start, end, stamps)
        stats["headings"] = len(entries)
        stats["timestamped"] = timestamped
        stats["unmatched"] = unmatched
        text, replaced = replace_index(text, entries)
        if replaced:
            stats["indexEntries"] = len(entries)

    # 4. link repair
    text, links = rewrite_links(text)
    stats["linksRewritten"] = links

    # 5. images (PDF-derived documents only; a missing manifest is a no-op)
    manifest = load_manifest(image_dir)
    if manifest is not None:
        text, placed, skipped = insert_images(text, manifest, image_url_prefix(image_dir))
        stats["imagesPlaced"] = placed
        stats["imagesSkipped"] = skipped

    # 6. whitespace discipline
    text = md.collapse_blank_runs(text)
    text = md.ensure_trailing_newline(text)

    stats["charsBefore"] = len(original)
    stats["charsAfter"] = len(text)
    return text, stats


def _srt_for(md_path: str, srt_arg: str | None) -> str | None:
    """Resolve the SRT to read timings from.

    An explicit ``-Srt`` always wins. Otherwise the sibling ``<base>.srt`` is
    tried, and -- because a summary sits next to a transcript rather than next to
    an audio file -- ``transcript.srt`` in the same folder is tried second. The
    fallback is decided with :func:`os.path.splitext` on the *file* name only,
    never ``Path().stem``, which would mangle a folder that contains dots.
    """
    if srt_arg:
        return srt_arg
    stem = os.path.splitext(md_path)[0]
    candidates = [os.path.join(stem + ".srt")]
    if os.path.basename(md_path).lower() == "summary.md":
        candidates.append(os.path.join(os.path.dirname(md_path), "transcript.srt"))
    for candidate in candidates:
        if paths.is_file(candidate):
            return candidate
    return None


def _image_dir_for(md_path: str, explicit: str | None) -> str:
    """The image directory to read a manifest from for this document."""
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(md_path), "img")


def _collect_markdown(target: str, recurse: bool) -> list[str]:
    """Every ``*.md`` under ``target`` (a file is returned as-is)."""
    if paths.is_file(target):
        return [target]
    if not paths.is_dir(target):
        raise ZoombieError(f"Input not found: {target}")

    found: list[str] = []
    if recurse:
        for root, _dirs, files in os.walk(paths.to_extended(target)):
            for name in files:
                if name.lower().endswith(".md"):
                    found.append(_de_extend(os.path.join(root, name), paths.to_extended(target), target))
    else:
        for entry in paths.list_dir(target, files=True):
            if entry.name.lower().endswith(".md"):
                found.append(entry.path)
    return sorted(found)


def _de_extend(path: str, extended_root: str, real_root: str) -> str:
    """Strip a ``\\\\?\\`` prefix inherited from an ``os.walk`` over it.

    ``os.walk`` yields the prefixed root's children *with* the prefix, and that
    prefix must never leak into a report path or a native call.
    """
    if not path.startswith("\\\\?\\"):
        return path
    relative = os.path.relpath(path, extended_root)
    return os.path.join(real_root, relative)


def run(args) -> Outcome:
    """Entry point behind ``zoombie postprocess``."""
    if args.md and args.dir:
        raise ZoombieError("Use either -Md or -Dir, not both.")

    target = args.md or args.dir
    if not target:
        raise ZoombieError("Pass -Md <file> or -Dir <folder>.")
    target = paths.absolute(target)

    documents = _collect_markdown(target, bool(args.recurse))
    if not documents:
        raise ZoombieError(f"No .md files found under: {target}")

    reports: list[dict] = []
    changed_files = 0
    for md_path in documents:
        paths.assert_fits(md_path, "The postprocess input path")
        with open(paths.to_extended(md_path), "r", encoding="utf-8-sig", newline="") as handle:
            original = handle.read()

        srt_path = _srt_for(md_path, args.srt)
        image_dir = _image_dir_for(md_path, args.image_dir)
        updated, stats = process_document(original, srt_path, image_dir)

        entry = {"md": md_path, "applied": False, **stats}
        if updated != original:
            changed_files += 1
            if args.apply:
                # Single write, of a fully built string: a failure inside
                # process_document can never leave a half-rewritten document.
                with open(paths.to_extended(md_path), "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(updated)
                entry["applied"] = True
        reports.append(entry)
        process.log(
            f"  {md_path}: headings={stats['headings']} timestamped={stats['timestamped']} "
            f"unmatched={len(stats['unmatched'])} images={stats['imagesPlaced']} "
            f"{'changed' if updated != original else 'unchanged'}"
            f"{'' if updated == original or args.apply else ' (dry run)'}"
        )

    report = {
        "target": target,
        "apply": bool(args.apply),
        "dryRun": not args.apply,
        "changed": changed_files,
        "files": reports,
    }
    if args.report:
        report_path = paths.absolute(args.report)
        paths.assert_fits(report_path, "The postprocess report path")
        with open(paths.to_extended(report_path), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    return Outcome(
        ok=True,
        data={
            "target": target,
            "dryRun": not args.apply,
            "changed": changed_files,
            "files": reports,
        },
    )

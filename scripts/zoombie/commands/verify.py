"""``verify``: the self-check for a produced ``summary.md`` tree.

``postprocess`` makes a document *deterministic*; ``verify`` makes it
*self-checking*. It is the gate a skill runs after producing a summary, and the
one a human runs before trusting a re-run: exit code 1 on any problem, 0 on a
clean tree.

Checks, per Markdown file:

* ``missing-image``  -- an ``![](…)`` whose destination points into an ``img/``
  folder that has no such file on disk.
* ``dead-anchor``    -- a block-4 ``#s-N`` link with no matching
  ``<a id="s-N">`` in the same file.
* ``dead-link``      -- a block-5 relative link that resolves to nothing.
* ``missing-source`` -- a ``<base>.md`` referenced by the block-2 source block
  that does not exist.

A finished item keeps no image manifest, so there is no ordinal heading-to-image
convention left to compare: that advisory was removed with the ``.data/`` layout
(see the note at the image checks below). A figure the document references is
verified by its presence on disk, which is the check that survives.

Two properties matter more than the checks themselves:

* the link scanner is the **balanced-paren** one shared (by import) with
  ``postprocess``, so a destination containing ``(стр. 5)`` is read whole rather
  than truncated -- the verifier and the rewriter can never disagree;
* destinations are ``unquote``-ed before they are resolved, so a correctly
  percent-encoded link (Cyrillic, or a space written ``%20``) is resolved rather
  than false-flagged. A verifier that flags its own valid output is worse than
  none at all.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote

from ..cli import Outcome
from ..item import paths as item_paths
from ..lib import markdown as md, paths, process
from ..lib.errors import ZoombieError
from .postprocess import (
    ABSOLUTE_RE,
    SECTION2_RE,
    SECTION5_RE,
    SECTION6_RE,
    iter_link_spans,
)

# Problem kinds, in the order they are reported.
KIND_MISSING_IMAGE = "missing-image"
KIND_DEAD_ANCHOR = "dead-anchor"
KIND_DEAD_LINK = "dead-link"
KIND_MISSING_SOURCE = "missing-source"
KIND_SECTION6_UNDECLARED = "section6-not-declared"

# Problems are fatal by default: `verify` exit code 1 is a contract, and most of
# what it reports (a dead anchor, a missing manifest) is a broken document. A check
# that flags documents written BEFORE a convention existed is marked advisory, so
# it informs without failing a tree that is otherwise sound.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

IMAGE_DIR_NAME = item_paths.IMAGE_DIR_NAME

# Block 6 is a VERBATIM COPY of the source with recognition artefacts cleaned out.
# It is not a recap, a digest or a summary of a summary -- and a task on another
# machine read it as exactly that, because the only thing distinguishing the two
# was prose. So the heading must say which it is.
#
# Matched over the heading LINE only, and by a token that survives rephrasing, so
# a natural title in either language passes. This is a lint, not a schema: it
# cannot prove the body is a copy, only that the heading does not claim otherwise.
DECLARATION_TOKENS = (
    "копия",
    "copy",
    "verbatim",
    "дословно",
    "полный текст",
    "full text",
    "source text",
    "текст источника",
)

# ``<a id="s-N"></a>`` written by the anchor pass; only our own prefix counts.
ANCHOR_RE = re.compile(r'<a\s+id="(s-\d+)"\s*></a>')
# ``#s-N`` at the end of a link destination: the block-4 index target.
FRAGMENT_ANCHOR_RE = re.compile(r"^#(s-\d+)$")
# A GitHub-style line suffix on an otherwise valid destination, e.g.
# ``Безруков/summary.md:1``. Our OWN skill docs mandate the ``file.ext:LINE``
# link form, so the verifier must resolve the path and ignore the line number --
# otherwise it flags the house style as 74 dead links and is unusable as a gate.
LINE_SUFFIX_RE = re.compile(r":\d+$")


def _line_of(text: str, offset: int) -> int:
    """1-based line number of a character offset (for the report)."""
    return text.count("\n", 0, offset) + 1


def iter_links(text: str):
    """Yield ``(is_image, label, dest, dest_offset)`` for every inline link.

    Delegates the span discovery to ``postprocess.iter_link_spans`` so the
    balanced-paren rules are shared: an image is recognized by the ``!`` that
    immediately precedes its opening bracket.
    """
    for label_start, label, dest_start, dest_end in iter_link_spans(text):
        is_image = label_start >= 1 and text[label_start - 1] == "!"
        yield is_image, label, text[dest_start:dest_end], dest_start


def _resolve(base_dir: str, dest: str) -> str:
    """Absolute path a relative destination points at (fragment stripped).

    ``unquote`` is what makes the check correct on our own output: the rewriter
    encodes a space as ``%20`` and leaves Cyrillic raw, so the on-disk name is
    the *decoded* form. Fragments (``#s-1``) are dropped -- they are not paths --
    and a trailing ``:<digits>`` line suffix (``summary.md:1``) is dropped from
    the PATH too. The line-suffixed form is what our own skill docs mandate, so
    treating it as part of the filename flags the house style as broken; the
    path is resolved and, when it exists, the link is valid.
    """
    path = unquote(dest.split("#", 1)[0]).replace("<", "").replace(">", "").strip()
    path = LINE_SUFFIX_RE.sub("", path)
    if not path:
        return ""
    return os.path.normpath(os.path.join(base_dir, path))


def _check_links(md_path: str, text: str, problems: list[dict]) -> None:
    """Run every link/anchor check over one document."""
    base_dir = os.path.dirname(md_path)
    anchors = set(ANCHOR_RE.findall(text))
    source_range = md.block_range(text, SECTION2_RE)
    related_range = md.block_range(text, SECTION5_RE)

    def in_block(bounds: tuple[int, int] | None, offset: int) -> bool:
        return bounds is not None and bounds[0] <= offset < bounds[1]

    def add(kind: str, offset: int, detail: str) -> None:
        problems.append(
            {
                "file": md_path,
                "line": _line_of(text, offset),
                "kind": kind,
                "detail": detail,
            }
        )

    for is_image, label, dest, dest_offset in iter_links(text):
        if not dest:
            continue

        # An in-file anchor (block 4). Only our own ``#s-N`` ids are checked;
        # any other fragment is a viewer concern, not a document defect.
        fragment = FRAGMENT_ANCHOR_RE.match(dest)
        if fragment is not None:
            if fragment.group(1) not in anchors:
                add(KIND_DEAD_ANCHOR, dest_offset, f"[{label}]({dest}) -> no <a id=\"{fragment.group(1)}\">")
            continue

        if dest.startswith("#") or ABSOLUTE_RE.match(dest):
            continue

        resolved = _resolve(base_dir, dest)
        exists = bool(resolved) and paths.exists(resolved)

        if is_image:
            # An image link is one that addresses an image DIRECTORY. The marker is
            # deliberately ``img/`` and not a full path, so both the item layout
            # (``.data/img/``) and a hand-made ``img/`` are recognized.
            if (IMAGE_DIR_NAME + "/") in dest.replace("\\", "/") and not exists:
                add(KIND_MISSING_IMAGE, dest_offset, f"[{label}]({dest}) -> {resolved}")
            continue

        if exists:
            continue

        # A ``<base>.md`` named inside block 2 is a *source* reference: it is
        # reported as such, because the fix (regenerate the source block) is
        # different from the fix for a dead related-articles link. Any other
        # relative link -- block 5 in particular -- is a plain dead link.
        if in_block(source_range, dest_offset) and dest.lower().endswith(".md"):
            add(KIND_MISSING_SOURCE, dest_offset, f"[{label}]({dest}) -> {resolved}")
        elif in_block(related_range, dest_offset):
            add(KIND_DEAD_LINK, dest_offset, f"related article [{label}]({dest}) -> {resolved}")
        else:
            add(KIND_DEAD_LINK, dest_offset, f"[{label}]({dest}) -> {resolved}")


# The manifest/README presence checks and the slide/manifest count check were
# REMOVED with the ``.data/`` layout: a finished item keeps only the figures its
# document actualy inlines, so there is no manifest to require and no ordinal
# heading-to-image convention to compare. A figure the document references is
# checked by ``missing-image``; a figure it does not reference is simply not there.


def _check_section6(md_path: str, text: str, problems: list[dict]) -> None:
    """Report a block-6 heading that does not declare itself a copy of the source.

    The failure this catches is not structural -- the document is perfectly valid
    Markdown -- but it is the one that has already happened: a reader (in that case
    an agent on another machine) took block 6 for a recap and treated a verbatim
    copy as a second-hand summary. Nothing in the file said otherwise.

    An ABSENT block-6 heading is reported too: a document whose content is simply
    prose has no copy, and silently passing it would hide the case where an agent
    dropped the source entirely.
    """
    bounds = md.block_range(text, SECTION6_RE)
    if bounds is None:
        problems.append(
            {
                "file": md_path,
                "line": 0,
                "kind": KIND_SECTION6_UNDECLARED,
                "severity": SEVERITY_WARNING,
                "detail": "no '## 6.' section; block 6 is the verbatim copy of the source",
            }
        )
        return

    # The heading LINE, not the body: the declaration belongs in the title, where a
    # reader meets it before the text. ``bounds[0]`` is the offset JUST PAST the
    # heading's newline, so the heading line ENDS at ``body_start - 1`` and its
    # start is the previous newline. Searching from ``body_start`` itself would begin
    # on the body's first line, so a document whose heading DOES declare the copy was
    # reported as undeclared (the real end-to-end run hit this).
    body_start = bounds[0]
    heading_end = body_start - 1 if body_start > 0 else 0
    heading_start = text.rfind("\n", 0, heading_end) + 1
    heading = text[heading_start:max(heading_start, heading_end)]
    heading = heading.split("\n", 1)[0].strip().lower()
    if not any(token in heading for token in DECLARATION_TOKENS):
        problems.append(
            {
                "file": md_path,
                "line": _line_of(text, heading_start),
                "kind": KIND_SECTION6_UNDECLARED,
                # ADVISORY, deliberately. Every document produced before this
                # convention existed reads as undeclared, so making it fatal would
                # fail every library in existence and make `verify` useless as a
                # gate. The signal is worth surfacing; it is not worth a migration
                # tax. This is the "warning, not failure" option from the plan.
                "severity": SEVERITY_WARNING,
                "detail": (
                    "block 6 heading does not state that it is a copy of the source "
                    f"(expected one of: {', '.join(DECLARATION_TOKENS)})"
                ),
            }
        )


def _de_extend(path: str, extended_root: str, real_root: str) -> str:
    """Strip a ``\\\\?\\`` prefix inherited from an ``os.walk`` over it."""
    if not path.startswith("\\\\?\\"):
        return path
    return os.path.join(real_root, os.path.relpath(path, extended_root))


# An ARCHIVED summary. It is a historical snapshot the summarize flow kept when the
# user chose -Archive, and its figure links point at whatever ``img/`` held at the
# time. It must NOT be checked: a live check would flag every historical link as a
# missing image and fail a tree that is actually sound.
#
# The name patterns live in ``item.paths`` -- the module that owns "what an item
# is" -- because an archive folder holds a ``summary.md``, so the producer and this
# checker must agree on ONE definition or the snapshot is counted as an item.
def _is_archived_summary(name: str) -> bool:
    """True for a ``summary_<stamp>.md`` snapshot a re-summarize left behind."""
    return item_paths.is_archived_summary(name)


def _is_archive_dir(name: str) -> bool:
    """True for a ``summary_<stamp>/`` archive folder."""
    return item_paths.is_archive_dir(name)


def _collect_markdown(root: str, recurse: bool) -> list[str]:
    """Every LIVE ``*.md`` in the scanned scope, sorted for a stable report.

    Archived summaries are skipped, in BOTH forms: a ``summary_<stamp>.md`` file from
    an older run, and a ``summary_<stamp>/`` folder from the current one. They are
    frozen snapshots, and their links legitimately resolve to pictures that moved
    with them and no longer sit in the live ``img/``. The folder walk prunes those
    directories outright, so no document inside one is ever reached.
    """
    found: list[str] = []
    if recurse:
        for walk_root, dirs, files in os.walk(paths.to_extended(root)):
            dirs[:] = [name for name in dirs if not _is_archive_dir(name)]
            for name in files:
                if name.lower().endswith(".md") and not _is_archived_summary(name):
                    found.append(_de_extend(os.path.join(walk_root, name), paths.to_extended(root), root))
    else:
        for entry in paths.list_dir(root, files=True):
            if entry.name.lower().endswith(".md") and not _is_archived_summary(entry.name):
                found.append(entry.path)
    return sorted(found)


def verify_tree(root: str, recurse: bool = False) -> dict:
    """Check every Markdown file under ``root``.

    Returns the report shape the CLI emits: ``{root, filesChecked, problems,
    ok}``. Each problem is ``{file, line, kind, detail}``.
    """
    root = paths.absolute(root)
    problems: list[dict] = []

    files = _collect_markdown(root, recurse)
    for md_path in files:
        try:
            with open(paths.to_extended(md_path), "r", encoding="utf-8-sig") as handle:
                text = handle.read()
        except OSError as exc:
            problems.append(
                {
                    "file": md_path,
                    "line": 0,
                    "kind": KIND_DEAD_LINK,
                    "detail": f"unreadable: {exc}",
                }
            )
            continue
        _check_links(md_path, text, problems)
        # Only a summary is a 6-block document. A transcript or a README has no
        # block 6 to declare anything about.
        if os.path.basename(md_path).lower() == item_paths.SUMMARY_NAME:
            _check_section6(md_path, text, problems)

    # A deduplicated report, sorted so two runs are byte-comparable.
    unique: list[dict] = []
    seen: set[tuple] = set()
    for problem in problems:
        key = (problem["file"], problem["line"], problem["kind"], problem["detail"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(problem)
    unique.sort(key=lambda problem: (problem["file"], problem["line"], problem["kind"]))

    fatal = [p for p in unique if p.get("severity") != SEVERITY_WARNING]
    advisories = [p for p in unique if p.get("severity") == SEVERITY_WARNING]

    return {
        "root": root,
        "filesChecked": len(files),
        # ``problems`` keeps its original meaning: things that are WRONG. An
        # advisory is reported in its own list so a caller that counts problems is
        # not silently inflated by a document that merely predates a convention.
        "problems": fatal,
        "advisories": advisories,
        "ok": not fatal,
    }


# The link reader is re-exported so a caller can inspect a tree without reaching
# into the postprocess module.
__all__ = ["run", "verify_tree", "iter_links"]


def run(args) -> Outcome:
    """Entry point behind ``zoombie verify``."""
    root = args.dir or os.getcwd()
    root = paths.absolute(root)
    if not paths.is_dir(root):
        raise ZoombieError(f"Directory not found: {root}")
    paths.assert_fits(root, "The verify path")

    report = verify_tree(root, bool(args.recurse))

    if not args.json:
        for problem in report["problems"]:
            location = f"{problem['file']}:{problem['line']}"
            process.log(f"{problem['kind']}: {location} -- {problem['detail']}", "warn")
        for advisory in report["advisories"]:
            location = f"{advisory['file']}:{advisory['line']}"
            process.log(f"{advisory['kind']}: {location} -- {advisory['detail']}", "info")
    process.log(
        f"verify: {report['filesChecked']} file(s) checked, "
        f"{len(report['problems'])} problem(s), "
        f"{len(report['advisories'])} advisory"
    )

    # ``ok=False`` is what makes the CLI exit 1; the report still travels in
    # ``data`` so a caller gets the detail, not just the verdict.
    return Outcome(
        ok=report["ok"],
        data=report,
        error=None if report["ok"] else f"{len(report['problems'])} problem(s) found",
    )

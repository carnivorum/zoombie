"""Text normalisation primitives shared by the SRT and Markdown passes.

Everything here is stdlib-only and has no project dependencies, because these
helpers are the bottom of the stack: :mod:`zoombie.lib.srt` and
:mod:`zoombie.lib.markdown` both build on them, and nothing here may import
either of those back.

Three separate concerns live in this one module:

* :func:`norm` / :func:`normalize_with_map` -- the tolerant word stream used to
  match ASR text against prose. The second one keeps the reverse map
  (``normalised index -> original index``) which is the primitive the SRT pass
  in the reference project lacked: without it a match offset cannot be turned
  back into a position in the untouched original text.
* :func:`hhmmss` -- the single timestamp format used by every heading, index
  entry and anomaly report.
* :func:`percent_encode_dest` / :func:`slug_for_files` -- the two file-name and
  link helpers. They are deliberately *not* interchangeable: the first is for
  Markdown destinations and never touches non-ASCII (Cyrillic paths must stay
  readable), the second is for *file prefixes* only (see its docstring).

The behaviour matches the reference implementation these helpers were extracted
from, so documents produced by either the original pass or this one compare
equal.
"""

from __future__ import annotations

import re

__all__ = [
    "DEST_FIX",
    "norm",
    "normalize_with_map",
    "hhmmss",
    "percent_encode_dest",
    "slug_for_files",
]


# Characters that break a bare CommonMark link destination. Percent-encoded one
# by one; everything else (including Cyrillic) is passed through untouched.
DEST_FIX = {
    " ": "%20",
    "\t": "%09",
    "(": "%28",
    ")": "%29",
    "[": "%5B",
    "]": "%5D",
    "<": "%3C",
    ">": "%3E",
    '"': "%22",
}

# ``[^\w\s]`` with ``re.UNICODE`` keeps Cyrillic letters and digits, drops
# everything else to a separator.
_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_SLUG_BAD_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё._-]+")
_SLUG_LEADING_NUMBER_RE = re.compile(r"^\d+\s*-\s*")
_SLUG_DASH_RUN_RE = re.compile(r"-{2,}")


def norm(text: str) -> str:
    """Normalize text for tolerant lookups against ASR/PDF-derived prose.

    Lower-cases, folds ``ё`` to ``е``, turns every non-word character into a
    single space and collapses whitespace. ``й`` is left as-is: it is a distinct
    Russian letter and the ASR engine emits it correctly, so folding it to ``и``
    would only create false matches.
    """
    text = text.lower().replace("ё", "е").replace("\u2011", "-")
    text = _NON_WORD_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Lower-cased word stream plus a norm-index -> original-index map.

    Mirrors :func:`norm` (lower, ``ё`` -> ``е``, non-word -> space) but keeps the
    reverse map, so a match offset in the returned string can be turned into an
    index into the untouched original text. ``len(stream) == len(index)`` always
    holds; ``index[i]`` is the offset in ``text`` that produced ``stream[i]``.

    A run of separators collapses to at most one space, attributed to the
    *first* character of that run, so the invariant ``stream[k]`` was produced
    by ``text[index[k]]`` holds for spaces as well as for letters. (The
    reference implementation attributed that space to the character *after* the
    run; see the module notes -- the difference is one offset and only ever
    affects a match that begins on whitespace.) Leading separators emit nothing
    at all (no leading space), so the stream is stripped exactly like
    :func:`norm`'s output.
    """
    chars: list[str] = []
    index: list[int] = []
    pending_index: int | None = None
    for i, ch in enumerate(text):
        low = ch.lower()
        if low == "ё":
            low = "е"
        if low.isalnum():
            if pending_index is not None and chars:
                chars.append(" ")
                index.append(pending_index)
            pending_index = None
            chars.append(low)
            index.append(i)
        elif pending_index is None:
            pending_index = i
    return "".join(chars), index


def hhmmss(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS``; hours are unbounded.

    Zero-padded to two digits for minutes and seconds and to *at least* two for
    hours, so 100 hours renders as ``100:00:00`` rather than wrapping modulo 24.
    """
    total = int(seconds)
    if total < 0:
        total = 0
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def percent_encode_dest(dest: str) -> str:
    """Percent-encode only the characters that break a bare Markdown destination.

    Space, tab, parentheses, square brackets, angle brackets and the double
    quote are encoded; everything else -- including Cyrillic and already-encoded
    sequences -- is left byte-for-byte intact. A fragment (``#...``) is never
    touched, because an anchor target is not a path.
    """
    if not dest:
        return dest
    path, sep, frag = dest.partition("#")
    path = "".join(DEST_FIX.get(ch, ch) for ch in path)
    return path + sep + frag


def slug_for_files(text: str, max_len: int = 32) -> str:
    """Short, filesystem-safe tag for a source document name.

    **For FILE prefixes only, never for folder names.** Folder names routinely
    contain a date (``12 - 06.05.2020 - Заметки``), and ``pathlib.Path().stem``
    treats the ``.2020 - Заметки`` tail as an extension and returns
    ``12 - 06.05`` -- the date is mangled away. This helper strips only the
    leading article number, keeps ``[0-9A-Za-zА-Яа-яЁё._-]``, replaces every
    other run with a single ``-`` and truncates to ``max_len``. Callers deciding
    "is this a file or a folder" must use :func:`os.path.splitext`, never
    ``Path().stem``.

    ``"12 - 06.05.2020 - Заметки"`` becomes ``"06.05.2020-Заметки"``: the date
    is *kept*, which is the point of the helper -- it both distinguishes two
    same-titled drops from one day and proves the name never went through
    ``Path().stem``. An empty result falls back to ``"src"`` so a prefix is
    never blank.
    """
    stem = _SLUG_LEADING_NUMBER_RE.sub("", text)
    slug = _SLUG_DASH_RUN_RE.sub("-", _SLUG_BAD_RE.sub("-", stem)).strip("-._ ")
    return slug[:max_len].strip("-._ ") or "src"

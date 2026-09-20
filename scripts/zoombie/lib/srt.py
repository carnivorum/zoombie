"""SRT parsing, offset lookup and anomaly reporting.

The companion skills turn a transcript into headings whose timestamps come from
``transcript.srt``. That lookup is fuzzy by nature: the summarising model never
reproduces a cue verbatim, so the match is done on a normalized word stream, not
on the raw text. This module provides the two pieces that make it work:

* :class:`SrtIndex` -- a normalized concatenation of every cue plus a map from a
  normalized offset back to the cue (and, via
  :meth:`SrtIndex.locate`, to a character offset in that cue's original text).
  The reverse map comes from :func:`zoombie.lib.textnorm.normalize_with_map`;
  without it a match offset is lost the moment the text is normalized.
* :func:`anomalies` -- a **pure reporter** for damaged ASR output (looped
  duplicate cues, runs of pure filler, impossible timing). It never edits
  anything: it returns candidates and the agent decides what to remove and logs
  it. Two runs over the same file therefore always agree.

Parsing is deliberately forgiving -- files come from several ASR paths and
several platforms:

* UTF-8 with or without BOM (``utf-8-sig``), CRLF or LF, and a missing final
  newline are all accepted;
* cue text may span several lines and is joined with single spaces;
* blank lines between cues are the block separator;
* a malformed cue is *skipped*, never fatal, so one bad block cannot cost the
  whole transcript.
"""

from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass

from .textnorm import hhmmss, norm, normalize_with_map

__all__ = [
    "DEFAULT_FILLERS",
    "Cue",
    "SrtIndex",
    "parse",
    "anomalies",
]

# ``00:01:02,345 --> 00:01:05,000``; the comma/dot fraction is optional because
# hand-edited files and some exporters drop the milliseconds.
SRT_TS_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})(?:[,.](\d{1,3}))?\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})(?:[,.](\d{1,3}))?"
)

# Filler that carries no content of its own. These are normalized (so
# ``в общем-то`` becomes ``в общем то``) before they are compared token by
# token; keeping the raw spellings here makes the list readable and reviewable.
DEFAULT_FILLERS = (
    "в общем-то",
    "в общем",
    "в большинстве",
    "э-э",
    "ээ",
    "ну",
    "вот",
    "как бы",
    "так сказать",
)

# A cue longer than this with almost no words is a timing/alignment artefact.
LONG_CUE_SECONDS = 30.0
LONG_CUE_MAX_WORDS = 3
ANOMALY_RUN_MIN = 3


@dataclass
class Cue:
    """One subtitle cue. ``index`` is the file's own number (1-based)."""

    index: int
    start: float
    end: float
    text: str

    @property
    def start_hhmmss(self) -> str:
        """``start`` as ``HH:MM:SS`` (see :func:`zoombie.lib.textnorm.hhmmss`)."""
        return hhmmss(self.start)

    @property
    def end_hhmmss(self) -> str:
        """``end`` as ``HH:MM:SS``."""
        return hhmmss(self.end)

    @property
    def duration(self) -> float:
        """``end - start`` in seconds; may be negative on a malformed cue."""
        return self.end - self.start


def _seconds(hours: str, minutes: str, seconds: str, millis: str | None) -> float:
    """Sum an SRT timestamp into seconds, keeping the millisecond fraction."""
    total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    if millis:
        total += int(millis.ljust(3, "0")) / 1000.0
    return float(total)


def parse(path: str | os.PathLike[str]) -> list[Cue]:
    """Read an SRT file into cues, in file order.

    Returns an empty list for a missing file, an empty file or a file whose
    blocks are all malformed. ``utf-8-sig`` strips a BOM if present and is a
    no-op otherwise; ``errors="replace"`` means a broken byte degrades one
    character instead of aborting the parse.
    """
    try:
        # ``newline=""`` disables universal-newline translation so the raw CRLF
        # pairs survive to the explicit normalization below. Letting Python
        # translate instead would turn a stray ``\r\r\n`` into a blank line and
        # silently split one cue in two.
        with open(
            os.fspath(path),
            "r",
            encoding="utf-8-sig",
            errors="replace",
            newline="",
        ) as handle:
            raw = handle.read()
    except OSError:
        return []

    # Collapse CRLF, a doubled CRLF (``\r\r\n`` -- seen in files written by a
    # tool that added CRLF to an already-CR line) and a lone CR (old-Mac line
    # endings) to a bare LF. The doubled form is unwrapped *first*: doing it the
    # other way round would turn ``\r\r\n`` into a blank line and split one cue
    # into two blocks, losing it.
    raw = raw.lstrip("\ufeff")
    raw = raw.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    next_index = 1
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.strip() for line in block.split("\n")]
        lines = [line for line in lines if line]
        if not lines:
            continue

        stamp_at = next(
            (i for i, line in enumerate(lines[:2]) if SRT_TS_RE.search(line)), None
        )
        if stamp_at is None:
            continue
        match = SRT_TS_RE.search(lines[stamp_at])
        assert match is not None  # guaranteed by the ``next`` above

        index = next_index
        if stamp_at == 1 and lines[0].isdigit():
            index = int(lines[0])
        next_index = max(next_index, index) + 1

        body = " ".join(
            line for i, line in enumerate(lines) if i != stamp_at and not line.isdigit()
        ).strip()
        if not body:
            continue

        cues.append(
            Cue(
                index=index,
                start=_seconds(match.group(1), match.group(2), match.group(3), match.group(4)),
                end=_seconds(match.group(5), match.group(6), match.group(7), match.group(8)),
                text=body,
            )
        )
    return cues


class SrtIndex:
    """Normalized concatenation of the cues plus offset lookups.

    ``full`` is every cue's normalized text joined by single spaces; the
    ``offsets``/``cue_of``/``maps`` lists are aligned, so entry ``k`` records
    where cue ``cue_of[k]`` starts in ``full`` and how to map each of its
    normalized characters back into that cue's original text.
    """

    def __init__(self, cues: list[Cue]) -> None:
        self.cues: list[Cue] = list(cues)
        self.offsets: list[int] = []
        self.cue_of: list[int] = []
        self.maps: list[list[int]] = []
        parts: list[str] = []
        pos = 0
        for i, cue in enumerate(self.cues):
            stream, index_map = normalize_with_map(cue.text)
            if not stream:
                continue
            if parts:
                parts.append(" ")
                pos += 1
            self.offsets.append(pos)
            self.cue_of.append(i)
            self.maps.append(index_map)
            parts.append(stream)
            pos += len(stream)
        self.full: str = "".join(parts)

    # -- offset lookups -----------------------------------------------------

    def _entry(self, offset: int) -> int | None:
        """Index into ``cue_of``/``maps`` for a normalized ``offset``."""
        if not self.cue_of:
            return None
        k = bisect.bisect_right(self.offsets, offset) - 1
        return max(0, min(k, len(self.cue_of) - 1))

    def time_at(self, offset: int) -> float:
        """Cue start (seconds) for a normalized offset in :attr:`full`.

        The lookup is a ``bisect_right`` over the cue start offsets, so an
        offset that lands *exactly* on a cue boundary belongs to the cue that
        starts there, not to the one before it.
        """
        k = self._entry(offset)
        if k is None:
            return 0.0
        return self.cues[self.cue_of[k]].start

    def text_for(self, offset: int) -> str:
        """The original cue text covering a normalized offset (diagnostics)."""
        k = self._entry(offset)
        if k is None:
            return ""
        return self.cues[self.cue_of[k]].text

    def locate(self, offset: int) -> tuple[int, int] | None:
        """Map a normalized offset to ``(cue_index, offset in original text)``.

        This is what makes the reverse map worthwhile: the caller gets a real
        character position inside the untouched cue, not just the cue number.
        """
        k = self._entry(offset)
        if k is None:
            return None
        within = offset - self.offsets[k]
        index_map = self.maps[k]
        if not index_map:
            return None
        return self.cue_of[k], index_map[max(0, min(within, len(index_map) - 1))]

    # -- search -------------------------------------------------------------

    def _search(self, words: list[str], start_offset: int) -> int | None:
        """Longest window that occurs at or after ``start_offset``."""
        max_len = min(len(words), 14)
        for length in range(max_len, 4, -1):  # 14 ... 5
            best: int | None = None
            for start in range(0, min(len(words) - length, 30) + 1):
                phrase = " ".join(words[start : start + length])
                found = self.full.find(phrase, start_offset)
                if found >= 0 and (best is None or found < best):
                    best = found
            if best is not None:
                return best  # longest window wins, earliest occurrence inside it
        return None

    def find(self, words: list[str], cursor: int = 0) -> int | None:
        """Locate a heading's leading words in the cue stream.

        Windows are tried longest-first (14 down to 5 words) so a long verbatim
        run beats a short coincidental one. The search is monotonic: it starts
        at ``cursor`` so headings keep their reading order and an earlier cue is
        never matched twice. If nothing is found at or after the cursor the
        search is retried once from offset 0 -- the fallback that lets a
        backwards-pointing heading (a summary written out of speech order) still
        get a timestamp. Returns ``None`` when neither attempt matches.
        """
        if not words or not self.full:
            return None
        found = self._search(words, max(0, cursor))
        if found is None and cursor > 0:
            found = self._search(words, 0)
        return found


def _filler_phrases(fillers: tuple[str, ...] | list[str]) -> list[tuple[str, ...]]:
    """Normalized filler phrases as token tuples, longest first."""
    phrases = []
    for filler in fillers:
        tokens = tuple(norm(filler).split())
        if tokens:
            phrases.append(tokens)
    phrases.sort(key=len, reverse=True)
    return phrases


def _is_noise_only(text: str, phrases: list[tuple[str, ...]]) -> bool:
    """True when every token of ``text`` is consumed by a filler phrase."""
    tokens = norm(text).split()
    if not tokens:
        return False  # an empty cue is a different problem, not a filler run
    position = 0
    while position < len(tokens):
        for phrase in phrases:
            if tokens[position : position + len(phrase)] == list(phrase):
                position += len(phrase)
                break
        else:
            return False
    return True


def anomalies(
    cues: list[Cue],
    fillers: tuple[str, ...] | list[str] = DEFAULT_FILLERS,
) -> list[dict]:
    """Report suspicious cues without changing anything.

    **Pure reporter.** Nothing is removed, merged or renumbered here; the
    returned entries are candidates for the agent to judge, and the agent logs
    whatever it decides to act on. Because the function only reads, calling it
    twice on the same file yields the same list.

    Kinds, in report order:

    * ``duplicate-run`` -- at least :data:`ANOMALY_RUN_MIN` consecutive cues
      whose :func:`zoombie.lib.textnorm.norm` text is identical (the classic
      whisper loop).
    * ``noise-only-run`` -- at least :data:`ANOMALY_RUN_MIN` consecutive cues
      made up entirely of filler words, with no content token at all.
    * ``timing`` -- ``end <= start``, or a cue longer than
      :data:`LONG_CUE_SECONDS` carrying at most :data:`LONG_CUE_MAX_WORDS` words.

    Each entry is ``{"kind", "cue_indexes", "start", "end", "text", "detail"}``
    with ``start``/``end`` already formatted as ``HH:MM:SS``.
    """
    entries: list[dict] = []
    phrases = _filler_phrases(fillers)

    def _entry(kind: str, group: list[Cue], detail: str) -> dict:
        return {
            "kind": kind,
            "cue_indexes": [cue.index for cue in group],
            "start": group[0].start_hhmmss,
            "end": group[-1].end_hhmmss,
            "text": group[0].text,
            "detail": detail,
        }

    # duplicate-run: maximal runs of consecutive cues with identical norm text.
    start = 0
    while start < len(cues):
        key = norm(cues[start].text)
        end = start + 1
        while end < len(cues) and norm(cues[end].text) == key:
            end += 1
        run = cues[start:end]
        if key and len(run) >= ANOMALY_RUN_MIN:
            entries.append(
                _entry(
                    "duplicate-run",
                    run,
                    f"{len(run)} consecutive cues with identical text: {key[:60]!r}",
                )
            )
        start = end

    # noise-only-run: maximal runs of consecutive cues that are all filler.
    # The run must *begin* on filler too, otherwise the leading non-filler cue
    # before a run would be swept into it (and the run then discarded).
    start = 0
    while start < len(cues):
        if not _is_noise_only(cues[start].text, phrases):
            start += 1
            continue
        end = start + 1
        while end < len(cues) and _is_noise_only(cues[end].text, phrases):
            end += 1
        run = cues[start:end]
        if len(run) >= ANOMALY_RUN_MIN:
            filler_key = norm(cues[start].text)
            entries.append(
                _entry(
                    "noise-only-run",
                    run,
                    f"{len(run)} consecutive filler-only cues: {filler_key[:60]!r}",
                )
            )
        start = end

    # timing: reported per cue, since each one is independently malformed.
    for cue in cues:
        if cue.end <= cue.start:
            detail = f"end ({cue.end_hhmmss}) <= start ({cue.start_hhmmss})"
        elif cue.duration > LONG_CUE_SECONDS and len(norm(cue.text).split()) <= LONG_CUE_MAX_WORDS:
            detail = (
                f"{cue.duration:.1f}s for "
                f"{len(norm(cue.text).split())} words"
            )
        else:
            continue
        entries.append(_entry("timing", [cue], detail))

    order = {"duplicate-run": 0, "noise-only-run": 1, "timing": 2}
    entries.sort(key=lambda e: (e["cue_indexes"][0], order[e["kind"]]))
    return entries

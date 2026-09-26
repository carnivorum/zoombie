"""Video -> slide frames, with a manifest shaped for the image pass.

The job this module does for a slide video is exactly what
:mod:`zoombie.lib.pdf` does for a PDF: turn a visual source into PNG files plus a
placement sidecar (``manifest.json`` + ``README.md``) that ``postprocess`` can
inline. The difference is the placement axis -- a PDF figure is anchored by a
*bbox on a page*, a slide is anchored by a *time window in the narration* -- so
a slide row carries ``timeSec``/``endTimeSec`` and its ``anchor_text`` is the
transcript spoken while the slide was on screen. Everything downstream
(``postprocess.insert_images``, ``verify``) then works unchanged.

Frame selection has two modes, and the difference is the whole point:

* **exact timestamps** (``-Times``/``-TimesFile``): one frame per timestamp, no
  sampling and no dedup. The kept count equals the number of timestamps, which is
  the minimal-image path a user who already knows where the slides change wants.
* **auto-detect**: sample at a low cadence (0.25 fps), find boundaries from a
  **grayscale diff** between consecutive samples, then keep **one frame per stable
  run** -- the covered-run rule -- plus samples inside a long run. Dedup by
  perceptual hash then collapses the extras that are the same picture, so a static
  long run costs exactly one OCR call.

The boundary signal is a grayscale diff and NOT the dHash, because a dHash cannot
see a fade: it compares each pixel with its right neighbour, so a uniform change
of the whole frame's level leaves every comparison unchanged. The dHash is still
computed, but for **reporting only** -- it becomes each kept frame's ``digest``.

Both the diff and the dHash are read from ffmpeg's own ``rawvideo`` grayscale
output rather than through PIL, so this adds no Python dependency beyond what the
toolchain already installs: ffmpeg is already required by ``extract``.

Unlike :mod:`zoombie.lib.pdf` -- which is deliberately import-free so the
standalone ``scripts/pdf/extract_pdf.py`` wrapper can use it -- this module may
import its sibling helpers, because nothing outside the package needs it.
"""

from __future__ import annotations

import json
import os
import re

from . import textnorm
from .errors import ZoombieError

__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_SCALE_WIDTH",
    "DEFAULT_HASH_DISTANCE",
    "DEFAULT_DIFF_THRESHOLD",
    "DEFAULT_MIN_SLIDE_SECONDS",
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "DEFAULT_MIN_PX",
    "DEFAULT_MIN_FRAME_BYTES",
    "DEFAULT_MIN_TEXT_CHARS",
    "OCR_ARTIFACT_NAME",
    "SIDECAR_MANIFEST",
    "SIDECAR_README",
    "HASH_FRAME_BYTES",
    "parse_time",
    "parse_times",
    "load_times_file",
    "frame_id",
    "parse_selection",
    "load_selection_file",
    "apply_selection",
    "hamming",
    "dhash_bits",
    "hash_sequence",
    "mean_abs_diff",
    "diff_sequence",
    "boundary_indices",
    "runs_from_boundaries",
    "sample_indices",
    "png_size",
    "flat_frame_reason",
    "low_text_reason",
    "script_of",
    "text_report",
    "stable_runs",
    "intervals_from_runs",
    "intervals_from_times",
    "cues_in_window",
    "time_tag",
    "frame_name",
    "hash_argv",
    "single_frame_argv",
    "build_rows",
    "write_sidecar",
    "write_ocr_artifact",
]

# Sample four times per second (0.25 fps). This is the *boundary-detection*
# cadence, not the image count: the run segmentation and the covered-run rule
# below mean a longer slide deck is not paid for in images, and one OCR call per
# run means it is not paid for in OCR either. A 1.0 fps cadence produced ~300
# samples for a single five-minute slide, each of which the old per-candidate OCR
# gate then read -- the cost this rework removes.
DEFAULT_SAMPLE_RATE = 0.25

# 1280 is wide enough for slide text to stay legible to the model while keeping
# the per-image token cost down; the hash pass is tiny and independent of it.
DEFAULT_SCALE_WIDTH = 1280

# Max Hamming distance between two 64-bit dHashes for them to count as the same
# slide. This is now the DEDUP distance (two sampled frames are the same picture),
# not the boundary test -- boundaries come from the grayscale diff below. It is
# generous on purpose: a cursor moving or a video-codec artifact shifts a few
# bits, and treating those as new pictures is the failure that inflates the count.
DEFAULT_HASH_DISTANCE = 8

# Mean absolute per-pixel difference (0-255) above which two consecutive samples
# are treated as different pictures, i.e. a slide boundary. Small on purpose: a
# fade or a dimmed frame can move every pixel by a few levels, and that -- not a
# large abrupt change -- is what a dHash is blind to. A camera pan moves far more
# than this, but the covered-run rule keeps the run regardless, so the cost of a
# false boundary is one extra OCR call and no lost slide.
DEFAULT_DIFF_THRESHOLD = 3.0

# A stable run shorter than this is a transition/flap, not a slide worth keeping.
DEFAULT_MIN_SLIDE_SECONDS = 2.0

# Inside a run longer than this, one extra representative is added every
# ``DEFAULT_SAMPLE_INTERVAL_SECONDS`` so a long hold still gets interior coverage
# (an animated build, a second slide shown within the same static run). The run's
# first and last frames are ALWAYS representatives, whatever this is.
DEFAULT_SAMPLE_INTERVAL_SECONDS = 30.0

# Same idea as pdf.DEFAULT_MIN_PX: drop frames too small to be a real slide.
DEFAULT_MIN_PX = 64

# A frame whose PNG is smaller than this is not dispatched to OCR **only** when it
# is not already a covered run's representative. It is a pure cost optimization:
# the Crimson item measured flat talking-head frames at 200-470 KB against 590-960 KB
# for text slides, so a 150 KB frame is almost never worth an OCR call. It NEVER
# decides that a slide is dropped -- a covered run's representative is always
# extracted, and if it is under this size the OCR is simply skipped and reported.
DEFAULT_MIN_FRAME_BYTES = 150_000

# The REPORTING threshold. It is no longer a drop rule (plan D-3): char count is
# not a usefulness signal -- Subtask C measured ``eng``/``rus``/``eng+rus`` medians
# at 123/129/133 chars because wrong-script OCR emits comparable garbage -- so a
# frame below this is still KEPT and still in ``visionFrames``; it is only flagged
# in the artifact as "likely image-only". 12 is the old floor, kept so the flag
# matches what the old gate would have dropped (Subtask C: the same 13 frames).
DEFAULT_MIN_TEXT_CHARS = 12

SIDECAR_MANIFEST = "manifest.json"
SIDECAR_README = "README.md"

# The OCR text artifact: written beside ``manifest.json`` under ``.data/``, never
# inlined into the result payload, so the text is readable by a caller without
# every invocation paying for it in the transport (plan D-6).
OCR_ARTIFACT_NAME = "ocr.json"

# The tiny frame the perceptual hash is computed from: 9x8 gives 8x8 horizontal
# gradient comparisons = 64 bits.
_HASH_WIDTH = 9
_HASH_HEIGHT = 8
# Bytes one tiny frame occupies on the rawvideo pipe; the caller slices the
# captured stream with this so it never has to know the hash geometry.
HASH_FRAME_BYTES = _HASH_WIDTH * _HASH_HEIGHT

# Seconds, MM:SS or HH:MM:SS, with an optional fraction on the last field. The
# part COUNT decides the meaning, so ``02:03`` is 2 min 3 s and never 2 h 3 min.
_TIME_RE = re.compile(r"^\d+(?:\.\d+)?(?::\d+(?:\.\d+)?){0,2}$")


def _log(message: str) -> None:
    """Progress on stderr, so stdout stays a single JSON line."""
    import sys

    sys.stderr.write("    " + message + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #

def parse_time(value: str) -> float:
    """Parse ``HH:MM:SS`` / ``MM:SS`` / ``SS`` (fractional allowed) to seconds.

    The number of colon-separated fields decides the meaning -- 1 field is
    seconds, 2 are minutes and seconds, 3 are hours, minutes and seconds -- so
    ``02:03`` can never be read as two hours. Accepts a comma or a dot as the
    fractional separator, because a subtitle or spreadsheet export uses the comma
    while a hand-typed timestamp usually uses the dot, and both are correct input.
    """
    text = str(value).strip().replace(",", ".")
    if not text:
        raise ZoombieError("Empty timestamp.")
    if _TIME_RE.match(text) is None:
        raise ZoombieError(f"Invalid timestamp: {value!r} (expected HH:MM:SS or SS).")
    parts = text.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def parse_times(spec: str) -> list[float]:
    """Parse a timestamp list into sorted, de-duplicated seconds.

    Commas and newlines are both separators, so one flag accepts a one-line list
    and the same value pasted from a file. Order is not trusted: the result is
    sorted, because the extraction and the manifest both assume reading order.
    """
    if not spec:
        return []
    chunks = re.split(r"[,\n;]+", spec)
    values: list[float] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if chunk:
            values.append(parse_time(chunk))
    # Sorted and deduplicated: two timestamps that resolve to the same second
    # would otherwise extract two frames for one slide.
    return sorted(set(values))


def load_times_file(path: str) -> list[float]:
    """Read timestamps from a file, one per line (or comma separated)."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return parse_times(handle.read())
    except OSError as exc:
        raise ZoombieError(f"Could not read the timestamps file: {exc}") from exc


# --------------------------------------------------------------------------- #
# the agent's final keep/drop over the detector's proposals
# --------------------------------------------------------------------------- #

def frame_id(index: int) -> str:
    """``f001`` -- the stable id of the ``index``-th kept frame (1-based).

    The id is what an agent NAMES in ``-Keep``/``-Drop``. It is deliberately not a
    file path: the agent's judgement is expressed as ids and timestamps, and the
    command performs every file operation. An id the agent never sees is a file it
    cannot touch, which is the point -- selection can never damage the directory.
    """
    return f"f{int(index):03d}"


def _selection_key(token: str) -> str:
    """Normalise one selection token (an id or a timestamp) for matching."""
    return str(token or "").strip().lower()


def _frame_matches(index: int, record: dict, key: str) -> bool:
    """True when ``key`` names the frame at ``index`` of ``records``.

    A key may be the frame id (``f005``) or the frame's timestamp in any spelling
    the toolchain already accepts -- ``00:04:04``, ``00-04-04``, or raw seconds
    (``244``) -- because all three are read off the same reported ``intervals``.
    """
    if not key:
        return False
    if key == frame_id(index):
        return True
    seconds = record.get("timeSec")
    if seconds is None:
        return False
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return False
    return key in (time_tag(value).lower(), textnorm.hhmmss(value).lower(), f"{value:g}")


def parse_selection(spec: str | None) -> list[str]:
    """Parse a ``-Keep``/``-Drop`` value into de-duplicated, ordered tokens.

    Commas, whitespace and newlines all separate, so one flag accepts a one-line
    list and the same value pasted from a file. Order is not trusted and duplicates
    collapse, because the selection is a SET of frames and how it was typed must not
    matter. Ids are lower-cased; a timestamp is kept as typed and matched by
    :func:`_frame_matches`.
    """
    if not spec:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,\s;]+", str(spec)):
        key = _selection_key(chunk)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def load_selection_file(path: str) -> list[str]:
    """Read a selection from a UTF-8 file, one id/timestamp per line.

    A file exists for the same reason ``-TimesFile`` does: a talking-head run can
    mean naming dozens of frames, and a long inline value is exactly what a shell
    mangles. It is parsed by :func:`parse_selection`, so the file and the inline
    flag cannot drift.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return parse_selection(handle.read())
    except OSError as exc:
        raise ZoombieError(f"Could not read the selection file: {exc}") from exc


def apply_selection(
    records: list[dict],
    keep: list[str] | None = None,
    drop: list[str] | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Apply an agent's FINAL keep/drop to the detector's proposed ``records``.

    The selector PROPOSES (the covered-run rule keeps one frame per stable run);
    this is where the agent DECIDES, which is the correction the Crimson
    talking-head run needed: the detector cannot tell a webcam frame from a slide,
    so it must not be the last word. Returns ``(kept, dropped, unmatched)``.

    * With neither list, **every** record is kept and nothing is dropped -- the
      detector's set is the starting point, not the verdict.
    * ``keep`` is an **allow-list**: when non-empty it REPLACES the default set, so
      40 talking-head proposals can be narrowed to the content frames. It may also
      be used to *reorder* -- see below.
    * ``drop`` removes from whatever ``keep`` left, so a frame the agent knows is
      noise can be stripped without enumerating everything it wants.
    * A token that names no frame is returned in ``unmatched``. This function does
      not raise: a dry run reports it, and the command turns a non-empty
      ``unmatched`` into a refusal rather than silently placing an empty set.

    Kept records are returned in the caller's ``keep`` order when an allow-list was
    given, so an agent may also sequence frames (e.g. put the wide market chart
    before the single-name one); with no allow-list reading order is preserved.
    """
    keep_keys = [key for key in (_selection_key(t) for t in (keep or [])) if key]
    drop_keys = [key for key in (_selection_key(t) for t in (drop or [])) if key]

    if not keep_keys and not drop_keys:
        return list(records), [], []

    keep_hits: dict[str, bool] = {key: False for key in keep_keys}
    drop_hits: dict[str, bool] = {key: False for key in drop_keys}
    kept: list[dict] = []
    dropped: list[dict] = []
    # ``start=1``: ids are 1-based (``f001`` is the FIRST frame), so the ordinal a
    # record is enumerated at IS its ``frame_id``. An off-by-one here silently
    # selects the neighbouring frame, which is why the base is stated explicitly.
    for index, record in enumerate(records, start=1):
        in_keep = False
        for key in keep_keys:
            if _frame_matches(index, record, key):
                keep_hits[key] = True
                in_keep = True
        in_drop = False
        for key in drop_keys:
            if _frame_matches(index, record, key):
                drop_hits[key] = True
                in_drop = True
        if (not keep_keys or in_keep) and not in_drop:
            kept.append(record)
        else:
            dropped.append(record)

    if keep_keys:
        # An allow-list also SEQUENCES: reorder the survivors to the order the
        # agent named them, so reading order follows the agent's intent. A frame
        # matched by several keys lands at its first mention.
        ordered: list[dict] = []
        used: set[int] = set()
        for key in keep_keys:
            for index, record in enumerate(records, start=1):
                if index in used or not _frame_matches(index, record, key):
                    continue
                if record in dropped:
                    continue
                used.add(index)
                ordered.append(record)
        if len(ordered) == len(kept):
            kept = ordered

    unmatched = [key for key, hit in keep_hits.items() if not hit]
    unmatched += [key for key, hit in drop_hits.items() if not hit]
    return kept, dropped, unmatched


# --------------------------------------------------------------------------- #
# perceptual hashing
# --------------------------------------------------------------------------- #

def hamming(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return bin(a ^ b).count("1")


def dhash_bits(raw: bytes, width: int = _HASH_WIDTH, height: int = _HASH_HEIGHT) -> int | None:
    """Difference hash of one raw grayscale frame, or ``None`` if too short.

    Each pixel is compared with its right neighbour; a brighter-than-neighbour
    pixel sets a bit. This is the classic dHash: robust to scaling and minor
    compression noise, which are exactly the two things that differ between two
    samples of the same slide.
    """
    if len(raw) < width * height:
        return None
    bits = 0
    for y in range(height):
        row = raw[y * width:(y + 1) * width]
        for x in range(width - 1):
            bits <<= 1
            if row[x] < row[x + 1]:
                bits |= 1
    return bits


def hash_sequence(raw: bytes) -> list[int | None]:
    """Split a rawvideo stream into one dHash per frame.

    A trailing partial frame (ffmpeg killed mid-write) is dropped rather than
    zero-padded, so it can never invent a spurious slide boundary. This is the
    REPORTING hash: it becomes each kept frame's ``digest``. It is no longer the
    boundary detector, because a dHash cannot see a fade (every neighbour
    comparison survives a uniform level change).
    """
    count = len(raw) // HASH_FRAME_BYTES
    return [
        dhash_bits(raw[index * HASH_FRAME_BYTES:(index + 1) * HASH_FRAME_BYTES])
        for index in range(count)
    ]


def mean_abs_diff(first: bytes, second: bytes) -> float | None:
    """Mean absolute per-pixel difference between two raw grayscale frames.

    This is the boundary signal. Unlike the dHash it compares a pixel with the
    SAME pixel of the other frame, so a fade, a dim, or a whole-frame level change
    -- all invisible to a neighbour-comparison hash -- registers here. Returns
    ``None`` when the two frames are not the same size (an unreadable sample),
    which the caller treats as "cannot tell", never as "same".
    """
    if not first or len(first) != len(second):
        return None
    total = 0
    for a, b in zip(first, second):
        total += abs(a - b)
    return total / len(first)


def diff_sequence(raw: bytes) -> list[float | None]:
    """Per-adjacent-pair mean absolute diff over a rawvideo stream.

    ``result[i]`` is the diff between sample ``i`` and sample ``i + 1``, so there
    is one fewer diff than there are samples. A trailing partial frame is dropped,
    exactly as in :func:`hash_sequence`.
    """
    count = len(raw) // HASH_FRAME_BYTES
    diffs: list[float | None] = []
    for index in range(count - 1):
        start = index * HASH_FRAME_BYTES
        diffs.append(mean_abs_diff(raw[start:start + HASH_FRAME_BYTES],
                                   raw[start + HASH_FRAME_BYTES:start + 2 * HASH_FRAME_BYTES]))
    return diffs


def boundary_indices(
    diffs: list[float | None],
    threshold: float = DEFAULT_DIFF_THRESHOLD,
) -> list[int]:
    """Sample indices where a new picture begins, from the diff sequence.

    ``diffs[i]`` describes the step from sample ``i`` to ``i + 1``; a diff above
    ``threshold`` means sample ``i + 1`` starts a new picture, so ``i + 1`` is the
    boundary. A ``None`` diff (an unreadable sample) is a boundary too, because
    joining two frames whose relation is unknown is what invents a slide.
    """
    return [
        index + 1
        for index, diff in enumerate(diffs)
        if diff is None or diff > threshold
    ]


def sample_indices(
    runs: list[tuple[int, int]],
    rate: float,
    interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
) -> list[tuple[int, int]]:
    """Pick the frames to keep: every run is COVERED, long runs get samples.

    The covered-run rule (plan D-4): a run always contributes at least one frame,
    whatever its diff and whatever OCR later says about it. A frame that reads as
    no text (a presenter on camera -- Subtask C measured 13/96 frames at exactly 0
    chars) is exactly what this protects, so it can never be lost by a text gate.

    The run's LAST frame is the primary representative, matching the old "an
    animated build is complete at the end of the run" behaviour. Inside a run
    longer than ``interval_seconds`` one interior frame is added per interval, so a
    long hold gets coverage without OCR-per-sample. No index appears twice.

    Returns ``(run_index, frame_index)`` pairs in reading order.
    """
    picks: list[tuple[int, int]] = []
    for run_index, (first, last) in enumerate(runs):
        span = (last - first + 1) / rate if rate > 0 else 0.0
        if interval_seconds <= 0 or span <= interval_seconds:
            picks.append((run_index, last))
            continue
        # Interior samples every interval, plus the complete final frame.
        step = max(1, int(round(interval_seconds * rate)))
        for index in range(first, last, step):
            picks.append((run_index, index))
        picks.append((run_index, last))
    # De-duplicate while keeping order: the range above can already include `last`.
    seen: set[int] = set()
    unique: list[tuple[int, int]] = []
    for pair in picks:
        if pair[1] in seen:
            continue
        seen.add(pair[1])
        unique.append(pair)
    return unique


def runs_from_boundaries(
    count: int,
    boundaries: list[int],
    rate: float,
    min_seconds: float = DEFAULT_MIN_SLIDE_SECONDS,
) -> list[tuple[int, int]]:
    """Segment ``count`` samples into runs, cutting at each boundary index.

    ``boundaries`` are the indices that START a new run, as returned by
    :func:`boundary_indices`. A run shorter than ``min_seconds`` is a transition or
    a flap and is filtered out, exactly as :func:`stable_runs` did -- the covered-run
    rule guarantees a frame for every run that SURVIVES, not for every blip.
    """
    if count <= 0:
        return []
    cuts = sorted({0, count} | {index for index in boundaries if 0 < index < count})
    runs = [(cuts[index], cuts[index + 1] - 1) for index in range(len(cuts) - 1)]
    minimum_frames = max(1, int(round(min_seconds * rate)))
    return [run for run in runs if (run[1] - run[0] + 1) >= minimum_frames]


def png_size(path: str) -> tuple[int, int]:
    """Read ``(width, height)`` straight from a PNG's IHDR chunk.

    Cheaper and more portable than a per-file ffprobe, and it needs no image
    library: the IHDR is at a fixed offset in every PNG.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
    except OSError:
        return 0, 0
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def flat_frame_reason(size_bytes: int, min_bytes: int = DEFAULT_MIN_FRAME_BYTES) -> str | None:
    """Why ``size_bytes`` means "do not spend an OCR call on this frame", or ``None``.

    A pure predicate so the prefilter is testable without ffmpeg. A frame under
    ``min_bytes`` is usually a talking head or a transition: it compresses to very
    little because it holds no text.

    **This is a cost filter, never a drop rule.** A covered run's representative is
    always kept; when it is under this size the caller simply skips its OCR and
    records the skip. There is no path in the command in which this predicate
    removes a frame from the AI set -- it only decides where the OCR budget goes.
    """
    if size_bytes and size_bytes < min_bytes:
        return f"frame is {size_bytes} bytes, below the {min_bytes}-byte OCR-prefilter floor"
    return None


def low_text_reason(text: str | None, min_chars: int = DEFAULT_MIN_TEXT_CHARS) -> str | None:
    """Why OCR'd ``text`` is likely image-only, or ``None``. **Reporting only.**

    This is no longer a drop rule (plan D-3). It answers "was this frame probably a
    picture with no text?" so the artifact and the log can say so -- it must never
    be used to remove a frame, because Subtask C measured that the 13 zero-text
    frames are the presenter on camera, and that char count is not a usefulness
    signal at all (wrong-script OCR emits comparable garbage). A caller that wants
    a frame gone must say so for another reason.

    ``text is None`` (OCR could not run) is NOT a low-text verdict: it is unknown,
    reported as such rather than as "image-only".
    """
    if text is None:
        return None
    stripped = " ".join(text.split())
    if len(stripped) < min_chars:
        return (
            f"OCR found {len(stripped)} characters of text, below the "
            f"{min_chars}-character reporting threshold (likely image-only)"
        )
    return None


_LATIN_RE = re.compile(r"[A-Za-z]")


def script_of(text: str) -> str:
    """``"cyrillic"`` / ``"latin"`` / ``"mixed"`` / ``"none"`` for OCR text.

    Usefulness is scored from script and content, not length: the whole point of
    the Subtask C measurement is that a wrong-script run returns a comparable
    VOLUME of garbage, so only ``script`` distinguishes a real reading from a
    homoglyph rendering.
    """
    if not text:
        return "none"
    has_cyrillic = any("\u0400" <= character <= "\u04ff" for character in text)
    has_latin = _LATIN_RE.search(text) is not None
    if has_cyrillic and has_latin:
        return "mixed"
    if has_cyrillic:
        return "cyrillic"
    if has_latin:
        return "latin"
    return "none"


def text_report(
    text: str | None,
    *,
    min_chars: int = DEFAULT_MIN_TEXT_CHARS,
    lang: str = "",
    promoted: bool = False,
) -> dict:
    """A reporting record for one frame's OCR text. **Never a keep/drop verdict.**

    Kept separate from :func:`low_text_reason` so a caller reads a structured
    verdict rather than re-parsing a message. ``likelyImageOnly`` is a flag for the
    artifact; ``script`` and ``chars`` are input to the AGENT's usefulness scoring,
    which per §4.1 scores from text CONTENT/SCRIPT and never from length. ``chars``
    is reported because it is cheap, not because it decides anything.
    """
    stripped = " ".join(text.split()) if text else ""
    return {
        "chars": len(stripped),
        "lang": lang,
        "script": script_of(text or ""),
        "likelyImageOnly": bool(text is not None and len(stripped) < min_chars),
        "promoted": promoted,
        "reason": low_text_reason(text, min_chars),
    }


def stable_runs(
    hashes: list[int | None],
    rate: float,
    distance: int = DEFAULT_HASH_DISTANCE,
    min_seconds: float = DEFAULT_MIN_SLIDE_SECONDS,
) -> list[tuple[int, int]]:
    """Group consecutive sample indices into stable runs.

    Returns inclusive ``(first, last)`` index pairs. A run grows while each frame
    stays within ``distance`` of the run's *previous* frame (a chain, not a
    comparison against the first frame, so a slow slide transition is one run
    rather than many). ``None`` hashes -- an unreadable frame -- break the chain
    instead of silently joining two different slides.
    """
    runs: list[tuple[int, int]] = []
    start: int | None = None
    previous: int | None = None
    for index, digest in enumerate(hashes):
        if digest is None:
            if start is not None:
                runs.append((start, index - 1))
                start = None
            previous = None
            continue
        if start is None:
            start = index
        elif previous is not None and hamming(previous, digest) > distance:
            runs.append((start, index - 1))
            start = index
        previous = digest
    if start is not None:
        runs.append((start, len(hashes) - 1))

    minimum_frames = max(1, int(round(min_seconds * rate)))
    return [run for run in runs if (run[1] - run[0] + 1) >= minimum_frames]


def intervals_from_runs(
    runs: list[tuple[int, int]],
    rate: float,
    duration: float | None = None,
) -> list[dict]:
    """Turn stable runs into slide intervals, keeping the LAST frame of each run.

    The last frame is the complete one: an animated build reveals the slide
    progressively, so the final frame of a run shows everything the speaker
    eventually put on screen.
    """
    intervals: list[dict] = []
    for first, last in runs:
        start = first / rate
        end = (last + 1) / rate
        if duration:
            end = min(end, duration)
        intervals.append({"timeSec": round(start, 3), "endTimeSec": round(end, 3),
                          "frameIndex": last})
    return intervals


def intervals_from_times(times: list[float], duration: float | None = None) -> list[dict]:
    """Turn explicit timestamps into intervals: each runs to the next one.

    With no next timestamp, the window ends at the video duration (or stays open
    when the duration is unknown), so ``anchor_text`` for the final slide still
    has a sensible bound to collect narration from.
    """
    intervals: list[dict] = []
    for index, start in enumerate(times):
        if index + 1 < len(times):
            end: float | None = times[index + 1]
        else:
            end = duration
        entry: dict = {"timeSec": round(start, 3), "frameIndex": index}
        if end is not None:
            entry["endTimeSec"] = round(end, 3)
        intervals.append(entry)
    return intervals


def cues_in_window(cues, start: float, end: float | None, *, max_chars: int = 400) -> str:
    """The narration spoken during ``[start, end)`` -- a slide's anchor text.

    This is the whole join between the two channels: the audio says *what* was
    being said, and ``postprocess`` matches that text against the block-6
    paragraphs to place the image. ``max_chars`` bounds the anchor so one long
    monologue cannot dominate the manifest.
    """
    if not cues:
        return ""
    stop = end if end is not None else float("inf")
    spoken: list[str] = []
    for cue in cues:
        # Overlap, not containment: a cue straddling the slide change belongs to
        # the earlier slide, which is when the speaker was still on it.
        if cue.end <= start:
            continue
        if cue.start >= stop:
            break
        spoken.append(cue.text)
    return " ".join(spoken).strip()[:max_chars]


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #

def time_tag(seconds: float) -> str:
    """``00-01-23`` -- the timestamp as a filename-safe tag."""
    return textnorm.hhmmss(seconds).replace(":", "-")


def frame_name(index: int, seconds: float) -> str:
    """``001 - 00-01-23.png`` -- reading order, then the time, like ``001 - p01``."""
    return f"{index:03d} - {time_tag(seconds)}.png"


# --------------------------------------------------------------------------- #
# ffmpeg argv
# --------------------------------------------------------------------------- #

def hash_argv(ffmpeg: str, source: str, rate: float) -> list[str]:
    """ffmpeg argv that writes tiny grayscale frames as rawvideo to stdout.

    ``-f rawvideo -pix_fmt gray -`` makes each frame exactly
    :data:`HASH_FRAME_BYTES` bytes on the pipe, so the hashing loop needs no image
    decoder. The ``fps`` filter fixes the cadence, so frame ``k`` (0-based) is at
    ``k / rate`` seconds -- the one mapping both the hash pass and the extraction
    pass rely on.
    """
    return [
        ffmpeg, "-hide_banner", "-nostats", "-v", "error",
        "-i", source,
        "-vf", f"fps={rate},scale={_HASH_WIDTH}:{_HASH_HEIGHT}:flags=bilinear,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]




def single_frame_argv(ffmpeg: str, source: str, seconds: float, output: str, width: int) -> list[str]:
    """ffmpeg argv that writes ONE frame at ``seconds``.

    ``-ss`` precedes ``-i`` so the seek is fast and lands on the keyframe before
    the target; for a slide (a static image held for seconds) that is exactly
    right, and it is what keeps the exact-timestamps path cheap.
    """
    return [
        ffmpeg, "-hide_banner", "-nostats", "-v", "error",
        "-ss", f"{seconds:.3f}",
        "-i", source,
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-y", output,
    ]


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

def build_rows(records: list[dict]) -> list[dict]:
    """Build manifest rows, mirroring ``pdf.ImageInfo.to_manifest`` plus time keys.

    The keys the image pass and ``verify`` read -- ``file``, ``anchor_text``,
    ``page_title``, ``digest``, ``bytes`` -- keep their meaning so
    ``postprocess.insert_images`` works untouched. ``page``/``bbox`` are absent,
    because a slide has no page; a reader that needs them treats the absence as
    "not a PDF figure". ``timeSec`` is what ``postprocess`` prefers over its fuzzy
    text search for the heading stamp.
    """
    rows: list[dict] = []
    for record in records:
        row: dict = {
            "file": record.get("file", ""),
            "width": int(record.get("width") or 0),
            "height": int(record.get("height") or 0),
            "timeSec": record.get("timeSec"),
            "timecode": textnorm.hhmmss(record.get("timeSec") or 0),
            "endTimeSec": record.get("endTimeSec"),
            "anchor_text": record.get("anchor_text", ""),
            "page_title": record.get("slide_title", ""),
            "digest": record.get("digest", ""),
            "bytes": int(record.get("bytes") or 0),
        }
        rows.append(row)
    return rows


def _table_cell(text: str, limit: int = 60) -> str:
    """One-line, pipe-safe cell for the README table (as in ``pdf.py``)."""
    cell = " ".join(str(text or "").split()).replace("|", "\\|")
    if len(cell) > limit:
        cell = cell[: limit - 1].rstrip() + "\u2026"
    return cell or "\u2014"


def write_sidecar(directory: str, rows: list[dict], source: str = "") -> str:
    """Write ``manifest.json`` + ``README.md`` and return the manifest path.

    The JSON shape is ``{source, count, images: [...]}`` -- identical to the PDF
    image sidecar -- so ``postprocess.load_manifest`` and ``verify`` read it
    without change. The README is slide-specific (a Time column, no page/bbox),
    because a placement table with a meaningless page number would be worse than
    no table at all.
    """
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    manifest_path = os.path.join(directory, SIDECAR_MANIFEST)
    payload = {
        "source": os.path.basename(source) if source else "",
        "kind": "slides",
        "count": len(rows),
        "images": rows,
    }
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    lines = ["# Extracted slides", ""]
    if source:
        lines.append(f"Source: `{os.path.basename(source)}`")
    lines.append(f"Slides: {len(rows)}")
    lines.extend([
        "",
        "| File | Time | Size, px | Anchor (narration) | Bytes |",
        "|------|------|----------|--------------------|-------|",
    ])
    for row in rows:
        lines.append(
            f"| `{row.get('file', '')}` | {row.get('timecode', '')} "
            f"| {row.get('width', 0)}\u00d7{row.get('height', 0)} "
            f"| {_table_cell(row.get('anchor_text', ''))} "
            f"| {row.get('bytes', 0)} |"
        )
    lines.append("")
    with open(os.path.join(directory, SIDECAR_README), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return manifest_path


def write_ocr_artifact(directory: str, entries: list[dict], *, source: str = "", lang: str = "") -> str:
    """Write the OCR text sidecar (plan D-6) and return its path.

    The text lives HERE and not in the result payload: a caller that wants it reads
    this file, and the common case -- an agent that only needs the frame paths --
    does not pay for every frame's text in the transport. ``entries`` are
    :func:`text_report` records plus the frame identity, so one artifact answers
    both "what did OCR read" and "which frames looked image-only".
    """
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, OCR_ARTIFACT_NAME)
    payload = {
        "source": os.path.basename(source) if source else "",
        "kind": "slides-ocr",
        "lang": lang,
        "count": len(entries),
        "imagesOnly": sum(1 for entry in entries if entry.get("likelyImageOnly")),
        "frames": entries,
    }
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path

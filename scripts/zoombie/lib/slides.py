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
* **auto-detect**: sample at a low cadence, then dedup consecutive frames with a
  perceptual hash (dHash + Hamming). A deck is slow, so most sampled frames
  collapse; an animated bullet build is the one case that legitimately yields
  several frames for one slide, which is a *threshold* decision, not a bug.

The perceptual hash is computed from ffmpeg's own ``rawvideo`` grayscale output
rather than through PIL, so this adds no Python dependency beyond what the
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
    "DEFAULT_MIN_SLIDE_SECONDS",
    "DEFAULT_MIN_PX",
    "SIDECAR_MANIFEST",
    "SIDECAR_README",
    "HASH_FRAME_BYTES",
    "parse_time",
    "parse_times",
    "load_times_file",
    "hamming",
    "dhash_bits",
    "hash_sequence",
    "png_size",
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
]

# One sample per second is the default: a slide does not change faster than a
# speaker can talk about it, and a higher cadence only costs decode time (the
# dedup pass discards the extra frames anyway).
DEFAULT_SAMPLE_RATE = 1.0

# 1280 is wide enough for slide text to stay legible to the model while keeping
# the per-image token cost down; the hash pass is tiny and independent of it.
DEFAULT_SCALE_WIDTH = 1280

# Max Hamming distance between two 64-bit dHashes for them to count as the same
# slide. Generous on purpose: a cursor moving or a video-codec artifact shifts a
# few bits, and treating those as new slides is the failure that inflates the
# image count.
DEFAULT_HASH_DISTANCE = 8

# A stable run shorter than this is a transition/flap, not a slide worth keeping.
DEFAULT_MIN_SLIDE_SECONDS = 2.0

# Same idea as pdf.DEFAULT_MIN_PX: drop frames too small to be a real slide.
DEFAULT_MIN_PX = 64

SIDECAR_MANIFEST = "manifest.json"
SIDECAR_README = "README.md"

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
    zero-padded, so it can never invent a spurious slide boundary.
    """
    count = len(raw) // HASH_FRAME_BYTES
    return [
        dhash_bits(raw[index * HASH_FRAME_BYTES:(index + 1) * HASH_FRAME_BYTES])
        for index in range(count)
    ]


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

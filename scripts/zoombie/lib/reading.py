"""Compressed reading copies: a small JPEG the agent reads inline (plan §12).

The 413 root cause (plan §3) was **payload size**, not a vision failure: a
session read eight full-resolution 1280x720 PNGs (~6.4 M base64 chars) and blew the
context. The fix §12 describes is a *reading copy*: a JPEG sibling of the frame, at
the SAME resolution, which the agent opens instead of the PNG. The PNG stays the
deliverable -- it is what ``ocr`` reads and what ``postprocess`` inlines into
``summary.md`` -- so only the agent's *inline read* changes format.

Every parameter here is fixed by Subtask H's measurement
([`feedback/subtask-H-reading-copies.md`], not re-derived):

* **Encoder and quality are ffmpeg's.** §12's "q3" is ffmpeg ``-q:v 3`` (qscale),
  NOT PIL/Pillow ``quality=3``: H measured 129,038 B vs ~19,000-29,765 B on the same
  frame, and the PIL version visually destroys the text. H's table for frame 010 is
  reproduced exactly by ``ffmpeg -q:v 3`` -> 129,038 B (7.29x the 940,052 B PNG).
* **Source resolution stays.** H measured the 1600-wide upscale as ~1.25x the bytes
  of q3@1280 for *more* numeric-token loss (interpolation invents tokens), so the
  1600 option is struck: there is no ``-vf scale`` here.
* **q3 is not safe for small text.** It is safe at >= 8 px ink height; the real
  dense frames carry 5-8 px figures, where q3 silently substitutes digits. So the
  quality escalates for a dense small-numeral frame.

The escalation rule (a single constant plus a pure function):

    DEFAULT_QUALITY = 3          # the cheap default (ffmpeg -q:v 3)
    DENSE_QUALITY = 2            # for frames with dense small numerals
    DENSE_NUMERIC_TOKENS = 20    # the threshold, inside H's measured gap

H's numeric-token counts on the real Crimson frames separate cleanly: **032 = 100,
094 = 80, 096 = 48** (dense, must escalate) against **085 = 11** (prose) and
**010 = 0** (poster). The gap between 11 and 48 is wide, so 20 sits inside it rather
than on a cliff edge. COUNTING NUMERIC TOKENS RATHER THAN CHARS is deliberate: H
proved raw char/word counts are misleading (frame 085 *gains* 333 chars at q3 while
getting noisier), and H's own fidelity metric is the numeric-token count.

The signal comes from the frame's OCR text, which the selector already computes once
per run (plan D-2) -- there is no second OCR pass. When the OCR text is unavailable
(``-NoTextGate``, an unavailable engine, or a frame whose OCR was skipped) the
default q3 is used and reported, never guessed into an escalation.
"""

from __future__ import annotations

import os
import re

from . import paths, process, tools

# ffmpeg mjpeg qscale for the reading copy. NOT PIL quality (see module docstring).
DEFAULT_QUALITY = 3

# The escalated quality for a dense small-numeral frame (H: q3 misreads 5-8 px
# figures; q2 trades ~15% more bytes for ~20% fewer figure drops on 032/094/096).
DENSE_QUALITY = 2

# The escalation threshold, in numeric tokens. H's measured counts are 032=100,
# 094=80, 096=48 (escalate) vs 085=11 and 010=0 (do not). 20 lies inside the wide
# 11..48 gap, so a borderline frame is not decided on a cliff edge.
DENSE_NUMERIC_TOKENS = 20

# Where the copies live, relative to an image directory: a DEDICATED subdirectory,
# so a ``.jpg`` never sits among the ``.png`` frames the manifest, the README,
# ``image_count()`` and ``mcp.facets.next`` enumerate. Chosen over a loose sibling
# so the exclusion is structural (a directory boundary, not a filter each reader
# would have to remember), and self-contained rather than a fixed name in the
# parent -- ``readpdf``'s ``-Vision`` destination is an arbitrary user path.
READING_DIR_NAME = "readings"

# Our own reading-copy file names end this way; the pruning sweep and the "is this
# ours?" test both key on it, so nothing foreign inside a reading dir is removed.
READING_SUFFIX_RE = re.compile(r"\.q\d+\.jpe?g$", re.IGNORECASE)

# Same numeric-token definition Subtask H measured with
# (``.tmp/subtask_h/measure6.py``): a run of digits, optionally with inner , and .
# separators. Kept identical so the threshold is comparable to H's numbers.
_NUMERIC_TOKEN_RE = re.compile(r"\d[\d,\.]*\d|\d")

__all__ = [
    "DEFAULT_QUALITY",
    "DENSE_QUALITY",
    "DENSE_NUMERIC_TOKENS",
    "READING_DIR_NAME",
    "count_numeric_tokens",
    "quality_for",
    "reading_dir",
    "reading_name",
    "is_reading_copy",
    "index_copies",
    "plan",
    "summary",
    "make_copies",
    "prune",
    "disabled",
]


def disabled(args) -> bool:
    """True when ``-NoReadingCopy`` was given.

    Read via ``getattr`` so a programmatic caller (and the existing unit tests) that
    builds a Namespace without the flag is not penalised -- the reading copy is the
    default, and only an explicit switch turns it off.
    """
    return bool(getattr(args, "no_reading_copy", False))


def count_numeric_tokens(text: str | None) -> int:
    """How many numeric tokens ``text`` contains (H's fidelity metric).

    A numeric token is a digit run with optional inner ``,`` / ``.`` separators --
    exactly the pattern Subtask H used to compare PNG and JPEG OCR, so the threshold
    below is expressed in the same units as H's table (032 = 100, 085 = 11).
    """
    if not text:
        return 0
    return len(_NUMERIC_TOKEN_RE.findall(text))


def quality_for(text: str | None) -> tuple[int, str]:
    """``(ffmpeg qscale, reason)`` for a frame whose OCR text is ``text``.

    Pure and deterministic, so the rule is testable without ffmpeg and reviewable
    against H's numbers. ``text is None`` -- the OCR text is UNAVAILABLE, as with
    ``-NoTextGate`` -- is the cheap default q3 and says so; it is never treated as
    "zero numerals, definitely sparse".
    """
    if text is None:
        return DEFAULT_QUALITY, (
            "OCR text unavailable, so the reading copy uses the default "
            f"-q:v {DEFAULT_QUALITY} (escalation needs the OCR text)"
        )
    tokens = count_numeric_tokens(text)
    if tokens >= DENSE_NUMERIC_TOKENS:
        return DENSE_QUALITY, (
            f"{tokens} numeric tokens (>= {DENSE_NUMERIC_TOKENS}) means dense small "
            f"numerals, so the reading copy escalates to -q:v {DENSE_QUALITY}"
        )
    return DEFAULT_QUALITY, (
        f"{tokens} numeric tokens (< {DENSE_NUMERIC_TOKENS}), so the reading copy "
        f"stays at the default -q:v {DEFAULT_QUALITY}"
    )


def reading_dir(images_dir: str) -> str:
    """``<images_dir>/readings`` -- the dedicated reading-copy directory.

    A SUBDIRECTORY of the frames' directory, so it is invisible to the files-only,
    non-recursive enumerations that read ``.png`` frames (``image_count``, the MCP
    facet, the manifest builder), while staying colocated with the frames it copies.
    """
    return os.path.join(images_dir, READING_DIR_NAME)


def reading_name(png_name: str, quality: int) -> str:
    """The reading copy's file name for a PNG frame: ``<stem>.q<N>.jpg``.

    The ``.q<N>.`` infix keeps the quality auditable from the directory listing and
    makes the name unmistakably ours (see :data:`READING_SUFFIX_RE`). The extension
    is ``.jpg``, never ``.png``, so no PNG-enumerating reader can mistake a copy for
    a frame.
    """
    stem = paths.without_extension(png_name)
    return f"{stem}.q{quality}.jpg"


def is_reading_copy(name: str) -> bool:
    """True when ``name`` is one of our reading-copy file names."""
    return bool(READING_SUFFIX_RE.search(name or ""))


def index_copies(directory: str) -> dict[str, str]:
    """Map a PNG's stem to its reading-copy path, for every copy in ``directory``.

    Read-only, so a consumer that must not write (the MCP ``facets.next`` tool) can
    still advertise the copies a run already wrote. The first copy wins when a stale
    quality variant is also present.
    """
    out: dict[str, str] = {}
    if not paths.is_dir(directory):
        return out
    for entry in paths.list_dir(directory, files=True):
        if not is_reading_copy(entry.name):
            continue
        stem = READING_SUFFIX_RE.sub("", entry.name)
        out.setdefault(stem, entry.path)
    return out


def _text_of(frame: dict) -> str | None:
    """The frame's OCR text, or ``None`` when it is genuinely unavailable.

    ``ocrText`` of ``None`` means "OCR did not run for this frame"; an empty string
    means "OCR ran and found nothing", which is a real zero-numeral measurement.
    """
    return frame.get("ocrText") if isinstance(frame, dict) else None


def plan(frames: list[dict]) -> dict:
    """A pure quality plan for ``frames``: ``{png_name: {...}}``.

    Each entry is ``{"quality", "reason", "tokens", "escalated"}``. No file is
    touched and no tool is run -- this is the deterministic half, so a test can pin
    "dense escalates, prose stays" without ffmpeg. ``frames`` entries need only
    ``file`` and (optionally) ``ocrText``.
    """
    out: dict[str, dict] = {}
    for frame in frames or []:
        if not isinstance(frame, dict):
            continue
        name = frame.get("file")
        if not name:
            continue
        text = _text_of(frame)
        quality, reason = quality_for(text)
        out[name] = {
            "quality": quality,
            "reason": reason,
            "tokens": None if text is None else count_numeric_tokens(text),
            "escalated": quality == DENSE_QUALITY,
        }
    return out


def summary(planned: dict) -> dict:
    """Count a plan by quality: ``{"qualities", "escalated", "routed"}``."""
    qualities: dict[str, int] = {}
    escalated = 0
    for entry in (planned or {}).values():
        key = str(entry.get("quality"))
        qualities[key] = qualities.get(key, 0) + 1
        if entry.get("escalated"):
            escalated += 1
    return {
        "qualities": qualities,
        "escalated": escalated,
        "routed": len(planned or {}),
    }


def _resolve_ffmpeg(ffmpeg: str | None) -> str | None:
    """The toolchain ffmpeg, resolved the way the rest of the codebase resolves it.

    An explicit path wins; otherwise :func:`zoombie.lib.tools.resolve` probes the
    manifest, the toolchain ``bin`` directories and then PATH. There is no hardcoded
    install location.
    """
    if ffmpeg and paths.is_file(ffmpeg):
        return ffmpeg
    return tools.resolve("ffmpeg")


def _encode_argv(ffmpeg: str, png: str, dest: str, quality: int) -> list[str]:
    """ffmpeg argv that writes ONE JPEG reading copy at the source resolution.

    Deliberately no ``-vf scale``: H measured the 1600-wide upscale as more bytes
    for MORE numeric-token loss, so the reading copy keeps the source resolution.
    ``-frames:v 1`` mirrors :func:`zoombie.lib.slides.single_frame_argv`'s shape.
    """
    return [
        ffmpeg, "-hide_banner", "-nostats", "-v", "error",
        "-i", png,
        "-frames:v", "1",
        "-q:v", str(quality),
        "-y", dest,
    ]


def make_copies(
    source_dir: str,
    dest_dir: str,
    planned: dict,
    *,
    ffmpeg: str | None = None,
    runner=None,
    timeout: int | None = 300,
) -> dict:
    """Write one JPEG per planned frame into ``dest_dir``; return what was written.

    ``source_dir`` holds the PNGs (an ASCII work dir, so ffmpeg never sees a
    Cyrillic path); ``dest_dir`` is the matching ASCII working copy directory, which
    the caller copies back to the item's dedicated reading directory.

    Returns ``{"available", "path", "count", "bytes", "byFrame", "failed"}`` where
    ``byFrame`` maps the PNG name to ``{"file", "bytes", "quality", "escalated"}``.
    When ffmpeg cannot be resolved the result is ``available: False`` and an empty
    map -- the run continues, advertising the PNGs, rather than failing.
    """
    result: dict = {
        "available": False, "path": None, "count": 0, "bytes": 0,
        "byFrame": {}, "failed": [],
    }
    if not planned:
        return result
    exe = _resolve_ffmpeg(ffmpeg)
    if not exe:
        result["failed"].append(
            {"reason": "ffmpeg-unavailable", "detail": "ffmpeg could not be resolved"}
        )
        return result

    run = runner or process.run_text
    paths.ensure_dir(dest_dir)
    result["available"] = True
    result["path"] = dest_dir
    for png_name, entry in planned.items():
        source = os.path.join(source_dir, png_name)
        if not paths.is_file(source):
            result["failed"].append({"file": png_name, "reason": "missing-source"})
            continue
        quality = int(entry.get("quality") or DEFAULT_QUALITY)
        jpeg_name = reading_name(png_name, quality)
        target = os.path.join(dest_dir, jpeg_name)
        code, detail = run(
            _encode_argv(exe, source, target, quality), timeout=timeout
        )
        if code != 0 or not paths.is_file(target):
            result["failed"].append({
                "file": png_name, "reason": "encode-error",
                "detail": (detail or "").strip()[-200:] or f"ffmpeg exit {code}",
            })
            continue
        size = paths.file_size(target)
        result["byFrame"][png_name] = {
            "file": jpeg_name, "bytes": size, "quality": quality,
            "escalated": bool(entry.get("escalated")),
        }
        result["count"] += 1
        result["bytes"] += size
    return result


def prune(directory: str, keep: set[str]) -> int:
    """Remove OUR reading copies in ``directory`` that are not in ``keep``.

    Only names matching :data:`READING_SUFFIX_RE` are ever removed, so a re-run over
    a shorter frame set leaves no stale sibling and nothing foreign is touched. A
    missing directory is not an error.
    """
    if not paths.is_dir(directory):
        return 0
    removed = 0
    for entry in paths.list_dir(directory, files=True):
        if not is_reading_copy(entry.name):
            continue
        if entry.name in keep:
            continue
        if paths.remove_quietly(entry.path):
            removed += 1
    return removed

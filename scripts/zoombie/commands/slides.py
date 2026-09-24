"""``slides``: pull slide frames out of a video, with a placement manifest.

Two modes, and the choice is the user's:

* **exact timestamps** (``-Times``/``-TimesFile``) -- one frame per timestamp.
  This is the minimal set: it extracts exactly what the user pointed at, with no
  sampling and no dedup, so an hour long deck costs N images, not hundreds.
* **auto-detect** -- sample at a low cadence, hash each frame perceptually, and
  keep one frame per stable run. For a deck this collapses to roughly one image
  per slide; an animated bullet build is the only case that legitimately yields
  more, which is a threshold decision (``-HashDistance``), not a bug.

The frames land in the item's image directory with the standard sidecar
(``manifest.json`` + ``README.md``), so ``postprocess`` embeds them into
``summary.md`` block 6 with no slides-specific code. ``anchor_text`` for each
slide is the narration spoken during its interval, read from the sibling SRT --
that text is the only bridge needed between the audio and the video channels.

Same native-path discipline as ``extract``/``readpdf``: the video is copied into
an ASCII work dir, ffmpeg runs entirely there, and the frames are copied back to
the confirmed (possibly Cyrillic) destination.
"""

from __future__ import annotations

import json
import os

from ..cli import Outcome
from ..item import paths as item_paths
from ..lib import env as env_mod, paths, process, slides
from ..lib.errors import ZoombieError
from ..lib.srt import parse as parse_srt


def _duration_seconds(ffprobe: str | None, source: str) -> float | None:
    """Video duration from ffprobe, or ``None`` when it cannot be determined."""
    if not ffprobe or not paths.is_file(ffprobe):
        return None
    code, text = process.run_text(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", source],
        timeout=120,
    )
    if code != 0:
        return None
    try:
        value = float(text.strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _owned_files(manifest_path: str) -> list[str] | None:
    """Image names recorded by our own previous run, or ``None`` if not ours."""
    if not paths.is_file(manifest_path):
        return None
    try:
        with open(paths.to_extended(manifest_path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    images = payload.get("images")
    if not isinstance(images, list):
        return None
    return [str(entry["file"]) for entry in images
            if isinstance(entry, dict) and entry.get("file")]


def _prepare_images_dir(images_dir: str, force: bool) -> int:
    """Make ``images_dir`` ready without ever deleting a foreign directory.

    The same rule ``readpdf`` applies to a PDF's image directory: our own previous
    output is pruned per file (so a re-run does not leave stale slides from an
    earlier timestamp set), and a directory with no readable manifest is refused
    unless ``-Force`` is given. Nothing foreign is ever deleted.
    """
    if not paths.is_dir(images_dir):
        return 0
    owned = _owned_files(os.path.join(images_dir, slides.SIDECAR_MANIFEST))
    if owned is None:
        if not force:
            raise ZoombieError(
                f"Image directory exists and was not written by slides (no "
                f"{slides.SIDECAR_MANIFEST}): {images_dir}. Pass -Force to write "
                f"into it, or pick a different -ImageDir. Nothing was deleted."
            )
        process.log(
            f"  {images_dir} is not slides'; -Force given, our files will be added "
            f"alongside what is already there (nothing deleted)"
        )
        return 0
    existing = {entry.name.lower() for entry in paths.list_dir(images_dir, files=True)}
    pruned = 0
    for name in owned:
        if name.lower() in existing:
            paths.remove_quietly(os.path.join(images_dir, name))
            pruned += 1
    return pruned


def _default_image_dir(output_dir: str) -> str:
    """``<output>/.data/img`` -- where ``postprocess`` finds figures by default."""
    return item_paths.image_dir(output_dir)


def _default_srt(output_dir: str) -> str | None:
    """The sibling transcript SRT, in either layout, or ``None``.

    The item layout is tried first, then the historical side-by-side location, so
    whichever generation of item exists resolves its own narration.
    """
    for candidate in (
        item_paths.transcript_path(output_dir, "srt"),
        os.path.join(output_dir, "transcript.srt"),
    ):
        if paths.is_file(candidate):
            return candidate
    return None


def _extract_frames(environment, ffmpeg: str, safe_video: str, work_images: str,
                    intervals: list[dict], width: int, min_px: int) -> tuple[list[dict], list[dict]]:
    """Write one PNG per interval; return ``(records, skipped)``.

    A frame that ffmpeg cannot produce, or that comes out smaller than
    ``min_px``, is recorded in ``skipped`` rather than silently dropped -- the
    same "never a silent drop" rule the PDF image pass follows.
    """
    records: list[dict] = []
    skipped: list[dict] = []
    paths.ensure_dir(work_images)

    for index, interval in enumerate(intervals, start=1):
        target = os.path.join(work_images, slides.frame_name(index, interval["timeSec"]))
        argv = slides.single_frame_argv(ffmpeg, safe_video, interval["timeSec"], target, width)
        code, detail = process.run_text(argv, timeout=None)
        if code != 0 or not paths.is_file(target):
            skipped.append({"reason": "error", "timeSec": interval["timeSec"],
                            "error": detail.strip()[-200:] or f"ffmpeg exit {code}"})
            continue
        width_px, height_px = slides.png_size(target)
        if max(width_px, height_px) < min_px:
            paths.remove_quietly(target)
            skipped.append({"reason": "tiny", "timeSec": interval["timeSec"],
                            "width": width_px, "height": height_px})
            continue
        records.append({
            "file": os.path.basename(target),
            "width": width_px,
            "height": height_px,
            "timeSec": interval["timeSec"],
            "endTimeSec": interval.get("endTimeSec"),
            "digest": "",
            "anchor_text": interval.get("anchor_text", ""),
            "slide_title": "",
        })
    return records, skipped


def run(args) -> Outcome:
    environment = env_mod.resolve()
    ffmpeg = environment.require("ffmpeg", "ffmpeg")

    if not paths.is_file(args.source):
        raise ZoombieError(f"Input not found: {args.source}")

    output_dir = paths.absolute(args.output or os.path.dirname(os.path.abspath(args.source)))
    if not output_dir:
        raise ZoombieError("Could not resolve an output folder; pass -Output.")
    images_dir = paths.absolute(args.image_dir) if args.image_dir else _default_image_dir(output_dir)
    # Room for '<NNN> - HH-MM-SS.png' inside the directory.
    paths.assert_fits(images_dir, "The slides image directory", slack=24)
    paths.assert_fits(args.source, "The slides input path")

    # Timestamps: an explicit flag wins, then a file; both are already sorted and
    # de-duplicated so the extraction order matches the manifest order.
    times = slides.parse_times(args.times or "")
    if args.times_file:
        times = slides.load_times_file(args.times_file)
    mode = "timestamps" if times else "detect"

    srt_path = args.srt or _default_srt(output_dir)
    cues = parse_srt(srt_path) if srt_path else []

    work_root = args.work_root or paths.env_path(paths.WORK_FOLDER)
    if not paths.is_ascii(work_root):
        raise ZoombieError(f"Work root must be ASCII: {work_root}")
    paths.assert_fits(work_root, "The work root (-WorkRoot)", slack=80)

    if args.dry_run:
        return Outcome(
            ok=True,
            data={
                "dryRun": True,
                "mode": mode,
                "output": output_dir,
                "images": images_dir,
                "srt": srt_path,
                "times": times,
                "sampleRate": args.sample_rate,
                "scale": args.scale,
            },
        )

    work = paths.new_ascii_dir(work_root)
    work_images = os.path.join(work, "images")
    try:
        copied = paths.copy_into_safe_work(args.source, work)
        safe_video = copied["input_path"]
        duration = _duration_seconds(environment.ffprobe, safe_video)

        candidate_frames = 0
        if mode == "timestamps":
            intervals = slides.intervals_from_times(times, duration)
            candidate_frames = len(times)
        else:
            process.log("slides: sampling to detect scene changes", "step")
            code, raw, hashes = _hash_pass(ffmpeg, safe_video, args.sample_rate)
            if code != 0 or not hashes:
                raise ZoombieError(
                    "Could not sample the video to detect slides. Pass -Times with "
                    "the slide timestamps, or check that the file plays."
                )
            candidate_frames = len(hashes)
            runs = slides.stable_runs(
                hashes, args.sample_rate, args.hash_distance, args.min_slide_seconds
            )
            intervals = slides.intervals_from_runs(runs, args.sample_rate, duration)

        # The narration spoken during each interval is the slide's anchor text:
        # this is the only join between the audio channel and the video channel.
        for interval in intervals:
            interval["anchor_text"] = slides.cues_in_window(
                cues, interval["timeSec"], interval.get("endTimeSec")
            )

        process.log(
            f"slides: mode={mode} candidates={candidate_frames} kept={len(intervals)}", "step"
        )
        if not intervals:
            process.log(
                "  no slide intervals found; the video may be a talking head with no "
                "visual change (use -Times if the slides are known)",
                "warn",
            )

        records, skipped = _extract_frames(
            environment, ffmpeg, safe_video, work_images, intervals, args.scale, args.min_px
        )
        rows = slides.build_rows(records)
        slides.write_sidecar(work_images, rows, args.source)

        pruned = _prepare_images_dir(images_dir, args.force)
        if pruned:
            process.log(f"  pruned {pruned} slide file(s) from a previous run")
        if paths.is_dir(work_images):
            paths.copy_tree(work_images, images_dir)

        intervals_out = [
            {"file": record["file"], "timeSec": record["timeSec"],
             "timecode": slides.time_tag(record["timeSec"]),
             "endTimeSec": record.get("endTimeSec")}
            for record in records
        ]
    finally:
        if not args.keep_work:
            paths.remove_quietly(work, recursive=True)

    return Outcome(
        ok=True,
        data={
            "output": output_dir,
            "mode": mode,
            "images": {
                "path": images_dir,
                "count": len(records),
                "manifest": os.path.join(images_dir, slides.SIDECAR_MANIFEST),
                "readme": os.path.join(images_dir, slides.SIDECAR_README),
                "skipped": len(skipped),
            },
            "sourceDurationSec": duration,
            # The dedup ratio is reported so the cost of the vision pass is
            # measurable per run rather than assumed.
            "candidateFrames": candidate_frames,
            "keptFrames": len(records),
            "intervals": intervals_out,
            "srt": srt_path,
            "asciiSafe": True,
        },
    )


def _hash_pass(ffmpeg: str, safe_video: str, rate: float) -> tuple[int, bytes, list]:
    """Run the tiny-frame hash pass; return ``(exit, raw, hashes)``."""
    argv = slides.hash_argv(ffmpeg, safe_video, rate)
    try:
        completed = process.run(argv, timeout=None)
    except OSError as exc:  # pragma: no cover - ffmpeg is required earlier
        raise ZoombieError(f"ffmpeg could not be launched: {exc}") from exc
    raw = completed.stdout or b""
    return completed.returncode, raw, slides.hash_sequence(raw)

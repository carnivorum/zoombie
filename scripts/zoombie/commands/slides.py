"""``slides``: pull slide frames out of a video, with a placement manifest.

Two modes, and the choice is the user's:

* **exact timestamps** (``-Times``/``-TimesFile``) -- one frame per timestamp.
  This is the minimal set: it extracts exactly what the user pointed at, with no
  sampling and no dedup, so an hour long deck costs N images, not hundreds.
* **auto-detect** -- sample at 0.25 fps, cut the samples into stable runs with a
  **grayscale diff**, then keep one frame per run (the covered-run rule) plus
  interior samples in a long run. Perceptual-hash dedup collapses the extras that
  are the same picture, so a static long run still costs exactly one image and one
  OCR call. See ``lib/slides.py`` for why the boundary signal is a diff and not the
  dHash (a dHash cannot see a fade; the dHash is reporting only now).

Nothing on the text track ever DELETES a frame. OCR is used to *describe* a frame
and to *score usefulness from its text* (see ``data.ocr.artifact``), never to drop
it: the Subtask C measurement showed char count is not a usefulness signal, and
that the 13 zero-text frames are the presenter on camera -- exactly the frames a
text floor would have deleted. The only drops left are structural (ffmpeg error,
a frame too small to be a slide) and a same-picture duplicate, and every drop is
named in ``data.images.skippedReasons``.

Same native-path discipline as ``extract``/``readpdf``: the video is copied into
an ASCII work dir, ffmpeg runs entirely there, and the frames are copied back to
the confirmed (possibly Cyrillic) destination.
"""

from __future__ import annotations

import json
import os

from ..cli import Outcome
from ..item import paths as item_paths
from ..lib import (
    env as env_mod,
    next as next_mod,
    ocr,
    paths,
    process,
    reading,
    scratch,
    slides,
)
from ..lib.errors import ZoombieError
from ..lib.srt import parse as parse_srt


def _flag_or(args, name: str, default: bool = False) -> bool:
    """Read a boolean flag tolerantly, for a Namespace that may predate it."""
    value = getattr(args, name, default)
    return default if value is None else bool(value)


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


# --------------------------------------------------------------------------- #
# candidate construction
# --------------------------------------------------------------------------- #

def _run_window(run: tuple[int, int], rate: float, duration: float | None) -> tuple[float, float]:
    """The ``(start, end)`` seconds a stable run covers."""
    first, last = run
    start = first / rate
    end = (last + 1) / rate
    if duration:
        end = min(end, duration)
    return start, end


def _detect_candidates(
    hashes: list,
    runs: list[tuple[int, int]],
    rate: float,
    cues,
    duration: float | None,
    sample_interval: float,
) -> list[dict]:
    """Build the covered-run candidate set from the diff/dHash pass.

    One representative per run (its last frame, the complete slide), plus interior
    samples every ``sample_interval`` seconds in a longer run. Every candidate
    carries its run index, its dHash (for dedup and reporting) and the narration
    spoken during its run (the slide's anchor text).
    """
    candidates: list[dict] = []
    for run_index, run in enumerate(runs):
        start, end = _run_window(run, rate, duration)
        anchor = slides.cues_in_window(cues, start, end)
        for _run, frame_index in slides.sample_indices([run], rate, sample_interval):
            candidates.append({
                "runIndex": run_index,
                "frameIndex": frame_index,
                "representative": frame_index == run[1],
                "timeSec": round(frame_index / rate, 3),
                "endTimeSec": round(end, 3),
                "anchor_text": anchor,
                "sourceHash": hashes[frame_index] if 0 <= frame_index < len(hashes) else None,
            })
    return candidates


def _timestamp_candidates(times: list[float], cues, duration: float | None) -> list[dict]:
    """Build candidates for explicit timestamps: each is its own covered run.

    No dedup and no sampling, matching the documented contract that the kept count
    equals the number of timestamps. ``sourceHash`` is ``None`` because there is no
    cheap pass in this mode; the manifest ``digest`` is correspondingly empty.
    """
    candidates: list[dict] = []
    intervals = slides.intervals_from_times(times, duration)
    for index, interval in enumerate(intervals):
        candidate = {
            "runIndex": index,
            "frameIndex": index,
            "representative": True,
            "timeSec": interval["timeSec"],
            "anchor_text": slides.cues_in_window(
                cues, interval["timeSec"], interval.get("endTimeSec")
            ),
            "sourceHash": None,
        }
        if "endTimeSec" in interval:
            candidate["endTimeSec"] = interval["endTimeSec"]
        candidates.append(candidate)
    return candidates


def _dedup_candidates(
    candidates: list[dict],
    distance: int,
) -> tuple[list[dict], list[dict]]:
    """Drop same-picture DUPLICATES, keeping every run's representative.

    This is the union dedup (plan D-5). A run's representative is never dropped --
    that is the covered-run guarantee, and it is what keeps an image-only slide. Only
    an INTERIOR sample is dropped, and only when it is visually the same picture as
    its own run's representative or as a frame already kept, so a static long run
    collapses to one image and an animated build keeps its distinct steps.
    """
    representatives: dict[int, list[int]] = {}
    for candidate in candidates:
        if candidate["representative"] and candidate.get("sourceHash") is not None:
            representatives.setdefault(candidate["runIndex"], []).append(candidate["sourceHash"])

    kept: list[dict] = []
    kept_hashes: list[tuple[int, str]] = []
    skipped: list[dict] = []
    for candidate in candidates:
        digest = candidate.get("sourceHash")
        if candidate["representative"] or digest is None:
            kept.append(candidate)
            if digest is not None:
                kept_hashes.append((digest, candidate["timeSec"]))
            continue
        siblings = representatives.get(candidate["runIndex"], [])
        if any(slides.hamming(digest, sibling) <= distance for sibling in siblings):
            skipped.append({
                "reason": "dedup", "timeSec": candidate["timeSec"],
                "runIndex": candidate["runIndex"],
                "detail": "same picture as this run's representative",
            })
            continue
        match = next(
            (seconds for other, seconds in kept_hashes if slides.hamming(digest, other) <= distance),
            None,
        )
        if match is not None:
            skipped.append({
                "reason": "dedup", "timeSec": candidate["timeSec"],
                "runIndex": candidate["runIndex"],
                "detail": f"duplicate of the frame at {match} s",
            })
            continue
        kept.append(candidate)
        kept_hashes.append((digest, candidate["timeSec"]))
    return kept, skipped


# --------------------------------------------------------------------------- #
# extraction + the non-destructive OCR pass
# --------------------------------------------------------------------------- #

def _extract_frames(
    ffmpeg: str,
    safe_video: str,
    work_images: str,
    candidates: list[dict],
    width: int,
    min_px: int,
    *,
    min_frame_bytes: int,
    min_text_chars: int,
    ocr_usable: bool,
    ocr_lang: str,
) -> tuple[list[dict], list[dict], list[dict], int, dict]:
    """Write one PNG per candidate; OCR each run once. Returns five.

    ``(records, skipped, ocr_entries, ocr_calls, ocr_texts)``. ``ocr_texts`` maps a
    frame's PNG name to its OCR text (or ``None`` when OCR did not run for it). Each
    record also carries ``ocrText``: the frame's own text when OCR ran for it, else the
    text of its RUN's representative. That is what the compressed-reading-copy
    escalation rule (plan §12) is scored from -- so choosing q2 vs q3 costs NO extra
    OCR: it inherits the run's single call, which is what keeps the D-2 policy
    (one OCR per run, not per frame) intact.

    Frame extraction is once per candidate -- no frame is ever written or analysed
    twice. A frame ffmpeg cannot produce, or one smaller than ``min_px``, is a real
    drop and is named in ``skipped``.

    **No text gate exists.** OCR runs once per RUN, on the run's representative, so
    a five-minute slide costs one call, not one per sample. Text is never a keep/drop
    verdict: a representative is OCR'd and kept whatever it reads, including the
    empty string (Subtask C: 13/96 frames are exactly 0 chars and are the presenter
    on camera). The byte prefilter only decides whether to spend the OCR call -- an
    undersized representative is kept with its OCR skipped and reported.
    """
    records: list[dict] = []
    skipped: list[dict] = []
    ocr_entries: list[dict] = []
    ocr_texts: dict[str, str | None] = {}
    ocr_calls = 0
    paths.ensure_dir(work_images)

    # One OCR per run (D-2): OCR only a unique run's representative, once.
    ocr_done: set[int] = set()
    # The representative's text per run, inherited by that run's other frames so the
    # reading-copy escalation score never costs a second OCR call (plan §12).
    run_text: dict[int, str | None] = {}
    # A sample index is a frame's identity. It is extracted (and so analysed)
    # exactly once; the guard makes "no frame twice" structural, not a hope.
    seen_frames: set[object] = set()

    for index, candidate in enumerate(candidates, start=1):
        frame_key = candidate.get("frameIndex")
        if frame_key is not None and frame_key in seen_frames:
            skipped.append({"reason": "dedup", "timeSec": candidate["timeSec"],
                            "runIndex": candidate["runIndex"],
                            "detail": "the same sample index was already extracted"})
            continue
        if frame_key is not None:
            seen_frames.add(frame_key)
        target = os.path.join(work_images, slides.frame_name(index, candidate["timeSec"]))
        argv = slides.single_frame_argv(ffmpeg, safe_video, candidate["timeSec"], target, width)
        code, detail = process.run_text(argv, timeout=None)
        if code != 0 or not paths.is_file(target):
            skipped.append({"reason": "error", "timeSec": candidate["timeSec"],
                            "error": detail.strip()[-200:] or f"ffmpeg exit {code}"})
            continue
        width_px, height_px = slides.png_size(target)
        if max(width_px, height_px) < min_px:
            paths.remove_quietly(target)
            skipped.append({"reason": "tiny", "timeSec": candidate["timeSec"],
                            "width": width_px, "height": height_px})
            continue

        size_bytes = paths.file_size(target)
        run_index = candidate["runIndex"]
        ocr_text: str | None = None
        ocr_report: dict | None = None

        # One OCR per RUN (D-2): only the run's representative is OCR'd, so a
        # five-minute slide costs one call. The reading-copy escalation score inherits
        # that call rather than adding a second pass -- a call per frame would undo the
        # D-2 win, which is why ``record["ocrText"]`` falls back to the run's
        # representative just below.
        wants_ocr = ocr_usable and (
            candidate["representative"] and run_index not in ocr_done
        )
        if wants_ocr:
            ocr_done.add(run_index)
            prefilter = slides.flat_frame_reason(size_bytes, min_frame_bytes)
            if prefilter is not None:
                # Kept, but no OCR spent: the byte prefilter is a cost gate, never
                # a drop. Reported so the missing text is not mistaken for "empty".
                ocr_report = slides.text_report(
                    None, min_chars=min_text_chars, lang=ocr_lang, promoted=False
                )
                ocr_report["ocrSkipped"] = prefilter
            else:
                try:
                    ocr_text = ocr.ocr_image(target, ocr_lang)
                except RuntimeError as exc:
                    # Tesseract said it was available but failed on THIS frame: keep
                    # the frame and report the failure rather than dropping a slide.
                    process.log(f"  OCR failed for frame {index}: {str(exc)[:200]}", "warn")
                    ocr_text = None
                ocr_calls += 1
                ocr_report = slides.text_report(
                    ocr_text, min_chars=min_text_chars, lang=ocr_lang, promoted=True
                )
        elif candidate["representative"]:
            ocr_report = slides.text_report(
                None, min_chars=min_text_chars, lang="", promoted=False
            )
            ocr_report["ocrUnavailable"] = True

        # The escalation signal: this frame's own text, else its run representative's,
        # so every frame of the run is scored without an extra OCR call (plan §12).
        ocr_texts[os.path.basename(target)] = ocr_text
        if ocr_text is not None and run_index not in run_text:
            run_text[run_index] = ocr_text
        frame_text = ocr_text if ocr_text is not None else run_text.get(run_index)

        if ocr_report is not None:
            ocr_entries.append({
                "file": os.path.basename(target),
                "timeSec": candidate["timeSec"],
                "runIndex": run_index,
                "text": ocr_text or "",
                **ocr_report,
            })

        # ``digest`` is the dHash -- REPORTING only now, never a boundary test. It is
        # populated in detect mode (the pass already hashed every sample) and empty
        # in timestamps mode (no cheap pass runs).
        digest = candidate.get("sourceHash")
        records.append({
            "file": os.path.basename(target),
            "width": width_px,
            "height": height_px,
            "timeSec": candidate["timeSec"],
            "endTimeSec": candidate.get("endTimeSec"),
            "digest": f"{digest:016x}" if digest is not None else "",
            "anchor_text": candidate.get("anchor_text", ""),
            "slide_title": "",
            "bytes": size_bytes,
            "representative": candidate["representative"],
            "runIndex": run_index,
            # This frame's own text when OCR ran for it, else filled from its run
            # representative after the loop -- see below.
            "ocrText": frame_text,
        })
    # A run's representative is normally the run's LAST frame, so the frames before it
    # are recorded before its text exists. Back-fill them: every frame of a run is
    # scored from that ONE OCR call (plan §12), which is what keeps D-2's call count
    # (one per run) intact instead of one call per frame.
    for record in records:
        if record["ocrText"] is None:
            record["ocrText"] = run_text.get(record["runIndex"])
    return records, skipped, ocr_entries, ocr_calls, ocr_texts


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

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

    # -DryRun writes NOTHING: no scratch, no .data/ocr.json, no manifest.
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
                "diffThreshold": args.diff_threshold,
                "scale": args.scale,
                # No frames were extracted, so there are none to attach. The
                # recommended step is unchanged and the cap is inert here, but the
                # block is still emitted so every result has the same shape.
                "next": next_mod.build(
                    "postprocess",
                    {"-Md": os.path.join(output_dir, item_paths.SUMMARY_NAME)},
                    why=(
                        "dry run: nothing was written, so there is nothing to read "
                        "yet; re-run without -DryRun to extract the frames, then "
                        "run postprocess -Apply to inline the chosen figures"
                    ),
                    attachable=[],
                    attach_cap=next_mod.cap_of(args),
                ),
            },
        )

    work = paths.new_ascii_dir(work_root)
    work_images = os.path.join(work, "images")
    # Reading copies (plan §12) are encoded in an ASCII work subdir and copied back
    # into the item's dedicated reading directory beside ``.data/img``.
    work_reading = os.path.join(work, "reading")
    reading_target = reading.reading_dir(images_dir)
    reading_result: dict = {"available": False, "path": None, "count": 0,
                            "bytes": 0, "byFrame": {}, "failed": []}
    reading_plan: dict = {}
    reading_summary: dict = {"qualities": {}, "escalated": 0, "routed": 0}
    reading_pruned = 0
    try:
        copied = paths.copy_into_safe_work(args.source, work)
        safe_video = copied["input_path"]
        duration = _duration_seconds(environment.ffprobe, safe_video)

        # OCR is a REPORTING reader, not a gate. -NoTextGate still turns it off so a
        # run that does not want the cost has an explicit switch.
        ocr_usable = False
        ocr_detail: str | None = None
        if not getattr(args, "no_text_gate", False):
            ocr_usable, ocr_detail = ocr.available()
            if not ocr_usable:
                process.log(
                    "  OCR unavailable (Tesseract not found); frames are kept and no "
                    f"OCR text artifact is written. Detail: {ocr_detail}",
                    "warn",
                )
        else:
            process.log("  OCR disabled by -NoTextGate")
        ocr_lang = getattr(args, "lang", None) or ocr.derive_lang(srt_path or args.source)

        candidate_frames = 0
        runs: list[tuple[int, int]] = []
        if mode == "timestamps":
            candidates = _timestamp_candidates(times, cues, duration)
            candidate_frames = len(times)
            dedup_skipped: list[dict] = []
        else:
            process.log("slides: sampling at the diff cadence to detect boundaries", "step")
            code, raw, hashes, diffs = _diff_pass(ffmpeg, safe_video, args.sample_rate)
            if code != 0 or not hashes:
                raise ZoombieError(
                    "Could not sample the video to detect slides. Pass -Times with "
                    "the slide timestamps, or check that the file plays."
                )
            candidate_frames = len(hashes)
            boundaries = slides.boundary_indices(diffs, args.diff_threshold)
            runs = slides.runs_from_boundaries(
                len(hashes), boundaries, args.sample_rate, args.min_slide_seconds
            )
            candidates = _detect_candidates(
                hashes, runs, args.sample_rate, cues, duration, args.sample_interval
            )
            candidates, dedup_skipped = _dedup_candidates(candidates, args.hash_distance)

        process.log(
            f"slides: mode={mode} samples={candidate_frames} runs={len(runs)} "
            f"candidates={len(candidates) + len(dedup_skipped)} kept={len(candidates)}",
            "step",
        )
        if not candidates:
            process.log(
                "  no slide candidates found; the video may be a talking head with no "
                "visual change (use -Times if the slides are known)",
                "warn",
            )

        # A reading copy is made for a frame the agent may read (plan §12). The q2/q3
        # escalation is scored from each frame's OCR text, which ``_extract_frames``
        # resolves to the frame's own text or its RUN representative's -- so the
        # reading copy costs NO extra OCR and the D-2 one-call-per-run policy holds.
        encode_reading = (not _flag_or(args, "no_reading_copy"))

        records, skipped, ocr_entries, ocr_calls, ocr_texts = _extract_frames(
            ffmpeg, safe_video, work_images, candidates, args.scale, args.min_px,
            min_frame_bytes=getattr(args, "min_frame_bytes", slides.DEFAULT_MIN_FRAME_BYTES),
            min_text_chars=getattr(args, "min_text_chars", slides.DEFAULT_MIN_TEXT_CHARS),
            ocr_usable=ocr_usable,
            ocr_lang=ocr_lang,
        )
        skipped = dedup_skipped + skipped
        rows = slides.build_rows(records)
        slides.write_sidecar(work_images, rows, args.source)

        # The OCR text is a DATA ARTIFACT (plan D-6), never the result payload: the
        # agent reads the cheap text from here to score usefulness, and a caller that
        # only needs paths does not pay for 96 frames of text in the transport.
        ocr_artifact = None
        data_dir = item_paths.data_dir(output_dir)
        if ocr_entries:
            ocr_artifact = slides.write_ocr_artifact(
                data_dir, ocr_entries, source=args.source, lang=ocr_lang
            )

        # The reading copy (plan §12) is planned from each frame's OCR text, then
        # encoded at the source resolution with ffmpeg. The PNG stays the frame of
        # record: this changes only what the agent reads inline. ``frame["file"]`` is
        # NEVER overwritten -- the manifest, README and postprocess read it.
        if encode_reading:
            reading_plan = reading.plan([
                {"file": record["file"], "ocrText": record.get("ocrText")}
                for record in records
            ])
            reading_result = reading.make_copies(
                work_images, work_reading, reading_plan, ffmpeg=ffmpeg
            )
            reading_summary = reading.summary(reading_plan)

        def _reading_path(png_name: str) -> str | None:
            entry = reading_result.get("byFrame", {}).get(png_name)
            if not entry:
                return None
            return os.path.join(reading_target, entry["file"])

        vision_frames = []
        for record in records:
            png_name = record["file"]
            reading_path = _reading_path(png_name)
            frame = {
                "file": png_name,
                "timeSec": record["timeSec"],
                "timecode": slides.time_tag(record["timeSec"]),
                "representative": record.get("representative", False),
            }
            if reading_path:
                # Separate keys -- the PNG identity above is untouched.
                frame["readingPath"] = reading_path
                frame["readingBytes"] = reading_result["byFrame"][png_name]["bytes"]
            vision_frames.append(frame)
        # ``visionFrames`` is the agent-facing read list, so it is CAPPED (plan §9):
        # a 96-frame deck cannot be attached in one result without recreating the
        # 413 (§3). The first ``cap`` frames are what the result advertises inline;
        # every kept frame stays on disk and is named in data.next.overAttach, so the
        # excess is deferred, not hidden. The cap is applied HERE, not left to the
        # caller, because a convention that relies on compliance is the failure mode
        # this item exists to remove.
        attach_cap = next_mod.cap_of(args)
        vision_selected = next_mod.select(vision_frames, attach_cap)

        # Per-reason DROP counts. The only reasons left are structural (``error``,
        # ``tiny``) and a same-picture ``dedup`` -- text is never among them.
        skip_reasons: dict[str, int] = {}
        for entry in skipped:
            skip_reasons[entry["reason"]] = skip_reasons.get(entry["reason"], 0) + 1
        image_only = sum(1 for entry in ocr_entries if entry.get("likelyImageOnly"))

        pruned = _prepare_images_dir(images_dir, args.force)
        if pruned:
            process.log(f"  pruned {pruned} slide file(s) from a previous run")
        if paths.is_dir(work_images):
            paths.copy_tree(work_images, images_dir)

        # The reading copies persist beside the item's image directory, in their own
        # subdirectory so no PNG-enumerating reader sees them. Prune only our own
        # stale copies first (a re-run over a shorter frame set must not accumulate),
        # then copy this run's in from the ASCII work dir.
        if reading_result.get("available"):
            keep = {entry["file"] for entry in reading_result["byFrame"].values()}
            reading_pruned = reading.prune(reading_target, keep)
            if paths.is_dir(work_reading):
                paths.copy_tree(work_reading, reading_target)
            reading_result["path"] = reading_target

        intervals_out = [
            {"file": record["file"], "timeSec": record["timeSec"],
             "timecode": slides.time_tag(record["timeSec"]),
             "endTimeSec": record.get("endTimeSec")}
            for record in records
        ]
    finally:
        # CLI-owned cleanup (plan §10): the run removes its own scratch unless
        # -KeepScratch/-KeepWork asked to retain it. The frames the result
        # advertises were already COPIED to ``images_dir``, so removing this
        # scratch cannot invalidate a single advertised path -- and it is removed
        # AFTER the bounded read E defined, because the copy-back precedes it.
        # ``None`` on the failure path: for an exception the run never reached the
        # result, so there is nothing to advertise and cleanup is best effort.
        scratch_block = scratch.report(work, kept=scratch.keep_requested(args))

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
                # Every drop, by reason: ``error`` (ffmpeg), ``tiny`` (pixel floor),
                # ``dedup`` (a same-picture duplicate). No ``flat``/``low-text`` key
                # exists any more -- nothing is dropped for being flat or textless.
                "skippedReasons": skip_reasons,
            },
            "sourceDurationSec": duration,
            "candidateFrames": candidate_frames,
            "keptFrames": len(records),
            "runs": len(runs) if mode == "detect" else len(times),
            "intervals": intervals_out,
            # The vision hand-off: the frames the agent must read with its OWN
            # vision, HARD-CAPPED at ``DEFAULT_ATTACH_CAP`` (plan §9). ``path`` is the
            # COMPRESSED READING COPY (plan §12) when one was written -- the PNG is
            # still advertised by ``file`` and by readingPath's absence, so the
            # manifest/README/postprocess are untouched -- and the PNG only as the
            # fallback. The full kept count is ``keptFrames``; the deferred frames are
            # named in data.next.overAttach.
            "visionFrames": [
                {**frame, "path": frame.get("readingPath")
                 or os.path.join(images_dir, frame["file"])}
                for frame in vision_selected["attach"]
            ],
            "visionFrameCount": len(vision_frames),
            # The recommended next step (plan §9 E): read the OCR text, then run
            # postprocess to inline the chosen figures and stamp the headings.
            # ``attach`` is the CAPPED inline list; ``overAttach`` is the remainder,
            # kept so a programmatic consumer can still reach every kept frame.
            "next": next_mod.build(
                "postprocess",
                {"-Md": os.path.join(output_dir, item_paths.SUMMARY_NAME)},
                why=(
                    "frames are kept and their OCR text is in data.ocr.artifact; read "
                    "the attach list (the compressed reading copies, plan §12) and "
                    "write the block-6 prose, then run postprocess -Apply to inline "
                    "the chosen figures and stamp the headings"
                ),
                attach_cap=attach_cap,
                # When more frames were kept than a result may attach, the
                # recommended re-invocation NARROWS the request instead of reading
                # everything: -Times with the deferred frames' timestamps extracts
                # exactly those, one image each. That is the graph edge a tired agent
                # would otherwise improvise (plan §9).
                args_capped=vision_selected["truncated"],
                # ``path`` is the reading copy when there is one, and ``bytes`` is
                # THAT file's size, so ``budget`` reports what the agent is about to
                # spend (plan §9/§12) -- a JPEG, not a ~3x larger PNG.
                attachable=[
                    {
                        "file": frame["file"],
                        "path": frame.get("readingPath")
                        or os.path.join(images_dir, frame["file"]),
                        "bytes": frame.get("readingBytes") or (
                            paths.file_size(os.path.join(images_dir, frame["file"]))
                            if paths.is_file(os.path.join(images_dir, frame["file"]))
                            else 0
                        ),
                    }
                    for frame in vision_frames
                ],
            ),
            # The OCR track as a report and a data artifact -- not a filter.
            "ocr": {
                "used": ocr_usable,
                "detail": ocr_detail,
                "lang": ocr_lang,
                # ONE call per run in detect mode -- the efficiency claim of D-2.
                "calls": ocr_calls,
                "artifact": ocr_artifact,
                "imageOnlyFrames": image_only,
                "reportingThreshold": getattr(
                    args, "min_text_chars", slides.DEFAULT_MIN_TEXT_CHARS
                ),
            },
            # The compressed reading copies (plan §12): what was written, where, and
            # the q2/q3 routing. Purely a report -- the frames themselves are intact.
            "readingCopy": {
                "dir": reading_target if reading_result.get("available") else None,
                "available": bool(reading_result.get("available")),
                "count": reading_result.get("count", 0),
                "bytes": reading_result.get("bytes", 0),
                "pruned": reading_pruned,
                "qualities": reading_summary.get("qualities", {}),
                "escalated": reading_summary.get("escalated", 0),
                "reason": (
                    "disabled by -NoReadingCopy" if not encode_reading else
                    "ffmpeg was not available, so PNG frames were advertised"
                    if not reading_result.get("available") else
                    f"{reading_summary.get('escalated', 0)} frame(s) escalated to "
                    f"-q:v {reading.DENSE_QUALITY} for dense small numerals; the rest "
                    f"use -q:v {reading.DEFAULT_QUALITY}"
                ),
            },
            # Kept for continuity with callers that already read these names.
            "textGateUsed": ocr_usable,
            "textGateDetail": ocr_detail,
            "srt": srt_path,
            # The scratch lifecycle, reported rather than left to prose (plan §10):
            # ``kept`` under -KeepScratch, ``removed``/``leftover`` otherwise.
            "scratch": scratch_block,
            "asciiSafe": True,
        },
    )


def _diff_pass(ffmpeg: str, safe_video: str, rate: float) -> tuple[int, bytes, list, list]:
    """Run the tiny grayscale pass; return ``(exit, raw, hashes, diffs)``.

    One decode feeds both signals: the dHash of every sample (reporting) and the
    mean absolute diff between consecutive samples (boundaries).
    """
    argv = slides.hash_argv(ffmpeg, safe_video, rate)
    try:
        completed = process.run(argv, timeout=None)
    except OSError as exc:  # pragma: no cover - ffmpeg is required earlier
        raise ZoombieError(f"ffmpeg could not be launched: {exc}") from exc
    raw = completed.stdout or b""
    return completed.returncode, raw, slides.hash_sequence(raw), slides.diff_sequence(raw)

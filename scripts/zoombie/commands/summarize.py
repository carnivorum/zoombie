"""``summarize``: the front door as a step machine.

The agent supplies CHOICES and PROSE; the backend assembles the document. ONE
verb, a ``step`` parameter, and each step's ``data.next`` points at the step that
follows -- so the agent reads a graph rather than remembering a procedure, and no
step's argument list grows past what that step needs.

    step 0  source   summarise a URL or a path: produce the source material into
                     a run scratch dir, extrapolate a name, decide routing
    step 1  name     create the destination folder (or confirm an in-place one)
    step 2  slides   optionally pull slide frames (a SEPARATE question from the
                     name, so the agent never presents a four-way prompt)
    step 3  prose    the agent's title, short summary, optional criticism and the
                     topic-change section headings; the backend splits the
                     transcript, ARCHIVES any existing summary, writes the
                     skeleton and runs postprocess
    step 4  verify   gate the tree, then delete the run scratch

Every throwaway -- the transcript, the SRT, the origin sidecar, the OCR report,
the manifest, the reading copies -- lives in the run scratch and is removed at
step verify. A finished item holds only ``summary.md``, the media, and ``img/``.

Nothing here is a session: the run is a PATH the agent carries in ``data.next``,
and each step re-derives what it needs from ``run.json`` in that directory. A
restarted process can therefore continue a run, and out-of-order calls are
refused by checking the prerequisites on disk.
"""

from __future__ import annotations

import json
import os
import re
import uuid

from .. import SKILL_VERSION
from ..cli import Outcome, build_parser
from ..item import paths as item_paths
from ..lib import paths, process, reading, workspace
from ..lib.errors import ZoombieError

__all__ = ["run"]

RUN_MARKER = "run.json"

_VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
_PDF_EXT = {".pdf"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# The step order, used to refuse an out-of-order call.
STEPS = ("source", "name", "slides", "prose", "verify")


def _scratch_root() -> str:
    """``<workspace>/.tmp/zoombie-summarize``: where a run's throwaway lives."""
    return os.path.join(workspace.workspace_root(), ".tmp", "zoombie-summarize")


def _new_run() -> str:
    """Create and return a fresh run scratch dir."""
    run = os.path.join(_scratch_root(), uuid.uuid4().hex)
    paths.ensure_dir(run)
    return run


def _run_file(run: str) -> str:
    return os.path.join(run, RUN_MARKER)


def _write_run(run: str, payload: dict) -> None:
    with open(paths.to_extended(_run_file(run)), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _read_run(run: str) -> dict:
    """Load a run's state, or refuse with a clear message."""
    if not run or not paths.is_file(_run_file(run)):
        raise ZoombieError(
            f"summarize: no run at {run!r}. Start with step 0 by passing -Source."
        )
    with open(paths.to_extended(_run_file(run)), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _kind_of(source: str) -> str:
    """``video`` / ``audio`` / ``pdf`` / ``image`` / ``images`` for a source."""
    if _URL_RE.match(source or ""):
        return "video"
    if paths.is_dir(source):
        return "images"
    extension = (paths.extension_of(source) or "").lower()
    if extension in _VIDEO_EXT:
        return "video"
    if extension in _AUDIO_EXT:
        return "audio"
    if extension in _PDF_EXT:
        return "pdf"
    if extension in _IMAGE_EXT:
        return "image"
    # Unknown extension: treat as audio and let the producer refuse if it cannot.
    return "audio"


def _media_in(directory: str) -> str | None:
    """The newest media file in ``directory``, or ``None``."""
    from ..lib import ytdlp

    found = ytdlp.newest_download(directory)
    return found["file"] if found else None


def _ns(argv: list[str]):
    """An argparse Namespace for another subcommand, built from its own parser."""
    return build_parser().parse_args(argv)


def _next(step: str, args: dict, why: str) -> dict:
    """A ``data.next`` block pointing at the following step."""
    from ..lib import next as next_mod

    return next_mod.build("summarize", {"-Step": step, **args}, why=why, attachable=[])


# --------------------------------------------------------------------------- #
# step 0: source
# --------------------------------------------------------------------------- #


def _step_source(args) -> Outcome:
    if not args.source:
        raise ZoombieError("summarize: pass -Source <url-or-file> to start.")
    source = args.source
    kind = _kind_of(source)
    if not _URL_RE.match(source) and not paths.exists(source):
        raise ZoombieError(f"summarize: source not found: {source}")

    run = _new_run()
    process.log(f"summarize: run scratch {run}", "step")

    media: str | None = None
    base: str | None = None
    title: str | None = None

    if kind in ("video", "audio"):
        # ONE pipeline call does download (when the source is a URL), audio
        # extraction and transcription, all into the run scratch. It keeps the
        # downloaded media in the scratch too, for the name step to place.
        argv = ["pipeline", "-Source", source, "-Output", run]
        if _URL_RE.match(source):
            argv += ["-DownloadDir", run]
        if args.language and args.language != "auto":
            argv += ["-Language", args.language]
        if args.work_root:
            argv += ["-WorkRoot", args.work_root]
        outcome = _dispatch("pipeline", argv)
        if not outcome.ok:
            return outcome
        made = outcome.data or {}
        media = (made.get("sourceFile") and os.path.join(run, made["sourceFile"])) or _media_in(run)
        base = os.path.join(run, "transcript")
        title = (made.get("source") or {}).get("title") if isinstance(made.get("source"), dict) else None
    elif kind == "pdf":
        argv = ["readpdf", "-Source", source, "-Output", os.path.join(run, "source"),
                "-Images", "-ImageDir", os.path.join(run, "img")]
        outcome = _dispatch("readpdf", argv)
        if not outcome.ok:
            return outcome
        base = os.path.join(run, "source")
    else:
        argv = ["readimages", "-Source", source, "-Output", os.path.join(run, "source"),
                "-ImageDir", os.path.join(run, "img")]
        outcome = _dispatch("readimages", argv)
        if not outcome.ok:
            return outcome
        base = os.path.join(run, "source")

    internal = (not _URL_RE.match(source)) and workspace.inside_workspace(source)
    if internal:
        destination = os.path.dirname(paths.absolute(source))
        proposed = workspace.sanitize_name(
            title or os.path.basename(os.path.normpath(os.path.dirname(paths.absolute(source))))
        )
    else:
        proposed = workspace.sanitize_name(title or workspace._stem_of(source))
        destination = os.path.join(
            workspace.unsorted_dir(workspace.KIND_SUMMARIES), proposed
        )

    _write_run(run, {
        "source": source, "kind": kind, "internal": internal,
        "destination": destination, "proposed": proposed,
        "media": media, "base": base, "destinationFinal": None,
    })

    return Outcome(ok=True, data={
        "step": "source", "run": run, "kind": kind, "internal": internal,
        "proposed": proposed, "destination": destination,
        "next": _next("name", {"-Run": run},
                      "confirm or replace the folder name with the user, then pass "
                      "it as -Name; for an in-place source the folder is fixed, so "
                      "confirm it as-is"),
    })


# --------------------------------------------------------------------------- #
# step 1: name
# --------------------------------------------------------------------------- #


def _step_name(args) -> Outcome:
    run = args.run
    state = _read_run(run)
    if state.get("destinationFinal"):
        raise ZoombieError("summarize: the name step already ran for this run.")

    destination = state["destination"]
    if state.get("internal"):
        # In place: the destination is the source folder; the media is already
        # there and is never moved or renamed.
        final = destination
    else:
        name = workspace.sanitize_name(args.name) if args.name else state["proposed"]
        parent = workspace.unsorted_dir(workspace.KIND_SUMMARIES)
        name = workspace.unique_name(parent, name)
        final = os.path.join(parent, name)

    paths.assert_fits(final, "The summarize destination")
    paths.ensure_dir(final)

    # Place the media in the item folder. For an in-place source the media is
    # already the source file; for an external or URL source it is moved out of
    # the scratch (or copied from the source path) unless -NoMedia was given.
    media = state.get("media")
    placed: str | None = None
    if media and paths.is_file(media):
        target = os.path.join(final, os.path.basename(media))
        if os.path.normcase(paths.absolute(media)) != os.path.normcase(paths.absolute(target)):
            if args.no_media:
                placed = None
            else:
                paths.copy_file(media, target)
                placed = target
        else:
            placed = target
    elif state.get("internal") and not _URL_RE.match(state["source"]):
        # A local source inside the workspace: the media is the source itself.
        placed = paths.absolute(state["source"])

    # Figures are NOT published here. They are extracted into the run scratch by the
    # slides step and published by the prose step, which knows the final keep/drop
    # selection. Publishing early would copy frames the agent later drops.

    state["destinationFinal"] = final
    state["media"] = placed or media

    # Archive the existing summary ONCE, at the START of the task: the snapshot must
    # be the file as the USER left it, not an intermediate the run itself produced.
    # The later steps (prose can run several times) only ever overwrite the live
    # summary, so a re-run of the task archives exactly one copy.
    archived = _archive_existing(final)
    state["archived"] = archived
    _write_run(run, state)

    why = (
        f"the previous summary was archived to {os.path.basename(archived['to'])} "
        "-- tell the user; then ask whether to extract slide frames (a separate "
        "question) and call step slides with -Slides true/false"
        if archived else
        "ask the user whether to extract slide frames (a separate question), then "
        "call step slides with -Slides true/false"
    )
    return Outcome(ok=True, data={
        "step": "name", "run": run, "itemDir": final, "media": placed,
        "archived": archived,
        "next": _next("slides", {"-Run": run}, why),
    })


# --------------------------------------------------------------------------- #
# step 2: slides
# --------------------------------------------------------------------------- #


def _step_slides(args) -> Outcome:
    run = args.run
    state = _read_run(run)
    item_dir = state.get("destinationFinal")
    if not item_dir:
        raise ZoombieError("summarize: run the name step before the slides step.")

    want = str(args.slides or "").strip().lower() in {"1", "true", "yes", "on"}
    media = state.get("media")
    frames: dict = {"requested": want, "extracted": 0}

    if want:
        if not media or not paths.is_file(media):
            raise ZoombieError("summarize: no retained media to extract slides from.")
        # Extract into the RUN SCRATCH, never straight into the item. Two reasons:
        # the item's img/ may hold a previous run's frames with no manifest (we prune
        # the sidecars at verify), which slides would refuse as "foreign"; and the
        # kept set is not known until a keep/drop pass. The prose step publishes the
        # final set into the item's img/.
        image_dir = os.path.join(run, item_paths.IMAGE_DIR_NAME)
        argv = ["slides", "-Source", media, "-Output", run, "-ImageDir", image_dir]
        if args.times:
            argv += ["-Times", args.times]
        # Talking-head-heavy video detection: a video that samples into many runs of
        # the SAME picture (a webcam that keeps cutting back between slides) yields
        # dozens of near-identical frames. -GlobalDedup collapses those to the first
        # occurrence, which is what an auto-detect run over a webinar needs. Off for
        # an explicit-timestamp run (the user pointed at exact slides).
        if not args.times:
            argv += ["-GlobalDedup"]
        # The agent's FINAL keep/drop, threaded from a prior slides call. The
        # detector only PROPOSES; the agent, which can see the frames, decides.
        if getattr(args, "keep", None):
            argv += ["-Keep", args.keep]
        if getattr(args, "drop", None):
            argv += ["-Drop", args.drop]
        if args.work_root:
            argv += ["-WorkRoot", args.work_root]
        outcome = _dispatch("slides", argv)
        if not outcome.ok:
            return outcome
        data = outcome.data or {}
        images = data.get("images") or {}
        vision = data.get("visionFrames") or []
        frames = {
            "requested": True,
            "extracted": images.get("count", 0),
            "imageDir": image_dir,
            "detected": images.get("detected"),
            "proposed": images.get("proposed", images.get("count", 0)),
            # The proposed frames, so the agent can SEE them and name a keep/drop.
            # Each carries its stable id (fNNN) and the reading-copy path, exactly
            # what slides reports -- the summarize flow must not hide the proposals.
            "frames": [
                {"id": frame.get("id"), "path": frame.get("path"),
                 "timeSec": frame.get("timeSec"), "timecode": frame.get("timecode")}
                for frame in vision
            ],
            "selection": images.get("selection"),
        }

    # The recommended next step is `prose` for an explicit-timestamp or an
    # already-selected run, but a first auto-detect run has proposals the agent
    # should look at: `why` tells it to keep/drop them (or proceed) before prose.
    proposals = bool(frames.get("frames")) and not (getattr(args, "keep", None) or getattr(args, "drop", None))
    why = (
        f"review the {len(frames.get('frames') or [])} proposed frame(s) above (by id "
        "fNNN or timestamp): call step slides again with -Keep/-Drop to prune "
        "talking-head frames, or call step prose to accept them all"
        if proposals else
        "write the title, the short summary, an optional criticism and the "
        "topic-change section headings (with an anchor phrase for each), then call "
        "step prose; the backend assembles block 6"
    )
    return Outcome(ok=True, data={
        "step": "slides", "run": run, "itemDir": item_dir, "slides": frames,
        "next": _next("slides" if proposals else "prose",
                      {"-Run": run} if proposals else {"-Run": run}, why),
    })


# --------------------------------------------------------------------------- #
# step 3: prose
# --------------------------------------------------------------------------- #


def _archive_existing(item_dir: str) -> dict | None:
    """Rename an existing ``summary.md`` to ``summary_<yyyyMMdd_HHmm>.md``.

    The user's edited document is NEVER destroyed: a re-summarize archives the
    previous summary under its own last-edit timestamp and reports the new path,
    so the agent can tell the user where their work went. Returns
    ``{"from", "to", "timestamp"}`` or ``None`` when there was nothing to archive.
    """
    summary = item_paths.summary_path(item_dir)
    if not paths.is_file(summary):
        return None
    stamp = _timestamp_of(summary)
    archived = os.path.join(item_dir, f"summary_{stamp}.md")
    index = 1
    while paths.exists(archived):
        index += 1
        archived = os.path.join(item_dir, f"summary_{stamp} ({index}).md")
    paths.move(summary, archived)
    process.log(f"  archived the previous summary -> {archived}", "step")
    return {"from": summary, "to": archived, "timestamp": stamp}


def _timestamp_of(path: str) -> str:
    """``yyyyMMdd_HHmm`` from a file's last-write time."""
    import datetime

    seconds = os.path.getmtime(paths.to_extended(path))
    moment = datetime.datetime.fromtimestamp(seconds)
    return moment.strftime("%Y%m%d_%H%M")


def _sections(raw: str | None) -> list[dict]:
    """Parse the ``-Sections`` JSON into ``[{heading, at}]``."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ZoombieError(f"summarize: -Sections is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ZoombieError("summarize: -Sections must be a JSON array.")
    out: list[dict] = []
    for entry in parsed:
        if isinstance(entry, dict) and entry.get("heading"):
            out.append({"heading": str(entry["heading"]).strip(),
                        "at": str(entry.get("at") or "").strip()})
    return out


def _split_transcript(text: str, sections: list[dict], fallback_title: str) -> list[tuple[str, str]]:
    """Split the transcript into ``(heading, body)`` pairs at each anchor.

    The anchor (``at``) is a phrase the agent read off the transcript; the body
    for a section runs from the start of that phrase to the start of the next
    section's. An anchor that cannot be found starts its section at the previous
    cut, so a slightly mis-quoted anchor degrades to a short section rather than
    dropping text.
    """
    if not sections:
        return [(fallback_title, text.strip())]
    from ..lib.textnorm import norm, normalize_with_map

    stream, index_map = normalize_with_map(text)
    positions: list[int | None] = []
    cursor = 0
    for section in sections:
        needle = norm(section["at"])
        found = stream.find(needle, cursor) if needle else -1
        if found >= 0:
            positions.append(index_map[found])
            cursor = found + len(needle)
        else:
            positions.append(None)

    # Fill an unfound anchor with the previous cut so no text is lost.
    resolved: list[int] = []
    last = 0
    for position in positions:
        last = position if position is not None else last
        resolved.append(last)

    pairs: list[tuple[str, str]] = []
    for position, section in enumerate(sections):
        start = resolved[position]
        end = len(text)
        for following in range(position + 1, len(resolved)):
            if resolved[following] > start:
                end = resolved[following]
                break
        pairs.append((section["heading"], text[start:end].strip()))
    return pairs


def _publish_figures(run: str, item_dir: str) -> int:
    """Move the run scratch's figures into the item's visible ``img/``.

    The image directory is REBUILT: a previous run's frames (which have no manifest,
    because verify prunes the sidecars) are cleared first, so a re-run never leaves
    old and new frames side by side. Only the run's kept frames are copied -- the
    agent's keep/drop already narrowed them. Returns the number published.
    """
    source = os.path.join(run, item_paths.IMAGE_DIR_NAME)
    if not paths.is_dir(source):
        return 0
    target = item_paths.image_dir(item_dir)
    if paths.is_dir(target):
        paths.remove(target, recursive=True)
    paths.ensure_dir(target)
    published = 0
    for entry in paths.list_dir(source, files=True):
        # The frames AND the manifest: postprocess reads the manifest to place each
        # figure by its anchor, so it MUST travel with the frames. The README and a
        # readings/ subdirectory are the run's scratch, not the item's deliverable,
        # and are not copied (verify prunes any that slip through anyway).
        if entry.name.lower().endswith(".png") or entry.name == item_paths.MANIFEST_NAME:
            paths.copy_file(entry.path, os.path.join(target, entry.name))
            if entry.name.lower().endswith(".png"):
                published += 1
    return published


def _relative_link(from_dir: str, target: str) -> str:
    """A forward-slashed, percent-encoded relative link from a directory."""
    from ..lib.textnorm import percent_encode_dest

    relative = os.path.relpath(target, from_dir).replace("\\", "/")
    return percent_encode_dest(relative)


def _step_prose(args) -> Outcome:
    run = args.run
    state = _read_run(run)
    item_dir = state.get("destinationFinal")
    if not item_dir:
        raise ZoombieError("summarize: run the name step before the prose step.")
    if not args.title:
        raise ZoombieError("summarize: pass -Title for the document.")

    base = state.get("base") or os.path.join(run, "transcript")
    # A recording yields ``transcript.txt``; a PDF or images yield ``<base>.md``.
    transcript_text: str | None = None
    for candidate in (f"{base}.txt", f"{base}.md"):
        if paths.is_file(candidate):
            with open(paths.to_extended(candidate), "r", encoding="utf-8-sig") as handle:
                transcript_text = handle.read()
            break
    if transcript_text is None:
        raise ZoombieError(
            f"summarize: no source text at {base}.txt or {base}.md; the source step "
            "did not produce one."
        )
    transcript = transcript_text

    sections = _sections(args.sections)
    pairs = _split_transcript(transcript, sections, args.title)

    # Assemble the six-block skeleton. postprocess takes over from here: it
    # numbers and stamps the block-6 headings, regenerates block 4, repairs the
    # links and inlines the figures.
    media = state.get("media")
    source_line = _relative_link(item_dir, media) if media else state.get("source", "")
    parts = [
        f"# {args.title}",
        "",
        "## 1. Титул",
        args.title,
        "",
        "## 2. Источник",
        source_line,
        "",
        "## 3. Краткое содержание",
        (args.summary_text or "").strip(),
        "",
    ]
    if args.criticism:
        parts += ["***Критика***", args.criticism.strip(), ""]
    parts += [
        "## 4. Содержание",
        "",
        "## 5. Связанные статьи",
        "",
        "## 6. Полный текст источника (копия, очищенная от артефактов распознавания)",
        "",
    ]
    for heading, body in pairs:
        parts.append(f"### {heading}")
        parts.append("")
        if body:
            parts.append(body)
            parts.append("")
    document = "\n".join(parts).rstrip("\n") + "\n"

    # Publish the figures: move the KEPT set from the run scratch into the item's
    # visible img/, REPLACING any frames a previous run left. The item is the
    # deliverable, so a re-run must not accumulate old frames beside new ones, and
    # pruning the sidecars at verify means a stale img/ has no manifest to tell us
    # what is ours -- so the whole image directory is rebuilt here.
    _publish_figures(run, item_dir)

    # NO archive here: the name step already snapshotted the user's file at the START
    # of the task. The prose step only writes the live summary, so re-running it
    # (an iterative prose pass) overwrites the live document without piling up
    # snapshots of intermediate work.
    archived = state.get("archived")
    summary = item_paths.summary_path(item_dir)
    with open(paths.to_extended(summary), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(document)

    # Run the mechanical pass in the SAME call, so the agent never has to chain.
    image_dir = item_paths.image_dir(item_dir)
    argv = ["postprocess", "-Md", summary, "-Apply"]
    srt = f"{base}.srt"
    if paths.is_file(srt):
        argv += ["-Srt", srt]
    if paths.is_dir(image_dir):
        argv += ["-ImageDir", image_dir]
    outcome = _dispatch("postprocess", argv)

    why = "check the finished tree, then the run scratch is removed"
    if archived:
        why = (
            f"the previous summary was archived to {os.path.basename(archived['to'])} "
            "-- tell the user; then check the finished tree"
        )
    return Outcome(ok=True, data={
        "step": "prose", "run": run, "itemDir": item_dir, "summary": summary,
        "archived": archived,
        "postprocess": (outcome.data or {}) if outcome else None,
        "next": _next("verify", {"-Run": run}, why),
    })


# --------------------------------------------------------------------------- #
# step 4: verify
# --------------------------------------------------------------------------- #


# The files a finished item must NOT keep: the run's image sidecars and the
# agent-facing reading copies. The figures the document links survive; these are
# mechanical and were only needed while the run was in progress.
_ITEM_THROWAWAY = (item_paths.MANIFEST_NAME, "README.md")


def _prune_item_sidecars(item_dir: str) -> list[str]:
    """Delete the run's image sidecars and reading copies from a finished item.

    The contract is that a finished item holds ONLY ``summary.md``, the kept media
    and the figures the document inlines. The manifest and the image README are
    scratch, and ``img/readings/`` holds the compressed copies the agent read once.
    This runs at ``verify`` so a successful task leaves exactly the deliverable.
    Best effort: a busy or foreign file is reported by absence, never fatal.
    """
    removed: list[str] = []
    image_dir = item_paths.image_dir(item_dir)
    for name in _ITEM_THROWAWAY:
        target = os.path.join(image_dir, name)
        if paths.is_file(target):
            paths.remove_quietly(target)
            removed.append(target)
    readings = os.path.join(image_dir, reading.READING_DIR_NAME)
    if paths.is_dir(readings):
        paths.remove_quietly(readings, recursive=True)
        removed.append(readings)
    return removed


def _step_verify(args) -> Outcome:
    run = args.run
    state = _read_run(run)
    item_dir = state.get("destinationFinal")
    if not item_dir:
        raise ZoombieError("summarize: run the prose step before verify.")

    outcome = _dispatch("verify", ["verify", "-Dir", item_dir])
    report = (outcome.data or {}) if outcome else {}
    ok = bool(report.get("ok", outcome.ok if outcome else False))

    cleaned = False
    pruned: list[str] = []
    if ok:
        # Deterministic cleanup, in this order: first thin the ITEM (sidecars and
        # reading copies are throwaway once the document is verified), then remove
        # the run scratch. A failure keeps both for inspection.
        pruned = _prune_item_sidecars(item_dir)
        for path in pruned:
            process.log(f"  removed {path}", "step")
        paths.remove(run, recursive=True)
        cleaned = True
        process.log(f"  removed the run scratch {run}", "step")

    return Outcome(
        ok=ok,
        data={
            "step": "verify", "run": run, "itemDir": item_dir,
            "verify": report, "cleaned": cleaned, "prunedSidecars": pruned,
            "next": _next("verify", {"-Run": run},
                          "the tree is clean; the run is finished"
                          if ok else "fix the reported problems and re-run verify"),
        },
        error=None if ok else f"{len(report.get('problems', []))} problem(s) found",
    )


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

_STEP_HANDLERS = {
    "source": _step_source,
    "name": _step_name,
    "slides": _step_slides,
    "prose": _step_prose,
    "verify": _step_verify,
}


def _dispatch(command: str, argv: list[str]) -> Outcome:
    """Run another subcommand in-process, through its own parser and dispatch."""
    from ..cli import _dispatch as cli_dispatch

    return cli_dispatch(_ns(argv))


def run(args) -> Outcome:
    """Entry point behind ``zoombie summarize``."""
    step = args.step or "source"
    handler = _STEP_HANDLERS.get(step)
    if handler is None:
        raise ZoombieError(f"summarize: unknown step {step!r}; expected one of {STEPS}")
    return handler(args)

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
                     transcript, writes the skeleton and runs postprocess
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

# The run-state schema version written into ``run.json`` and checked on every
# resume. Bumping it is how a future change to the recorded state fails a resume
# LOUDLY ("start again") instead of half-working against a stale shape. It exists
# because the media model gained ``sourceMedia`` after runs had already shipped
# WITHOUT it: an old ``run.json`` is not resumable (see :func:`_read_run`).
RUN_VERSION = 2

_VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
_PDF_EXT = {".pdf"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# The step order, used to refuse an out-of-order call.
STEPS = ("source", "name", "slides", "prose", "verify")

# How an EXISTING local media may be placed in the item. A URL download is always
# placed (it is ours); these govern a source the user already owns:
#   keep -- leave it where it is (the default when it is already the item's folder)
#   copy -- duplicate it into the item (the default otherwise)
#   move -- move it into the item, the original removed
#   none -- keep no copy, reference the source path
_MEDIA_MODES = ("keep", "copy", "move", "none")


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
    # Stamp the schema version on EVERY write, so a run.json always declares the
    # shape a later step may assume and an older file is recognisable (see
    # :func:`_read_run`).
    payload = {**payload, "runVersion": RUN_VERSION}
    with open(paths.to_extended(_run_file(run)), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _read_run(run: str) -> dict:
    """Load a run's state, or refuse with a clear message.

    A ``run.json`` written by a DIFFERENT toolchain version -- its ``runVersion`` is
    missing (a pre-change file) or ahead of :data:`RUN_VERSION` -- is REFUSED rather
    than half-read: the media model gained ``sourceMedia`` after early runs shipped,
    so an old file carries no readable media handle and resuming it would mis-place
    the media. The run scratch is throwaway, so the fix is to start again.
    """
    if not run or not paths.is_file(_run_file(run)):
        raise ZoombieError(
            f"summarize: no run at {run!r}. Start with step 0 by passing -Source."
        )
    with open(paths.to_extended(_run_file(run)), "r", encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("runVersion") != RUN_VERSION:
        found = state.get("runVersion")
        raise ZoombieError(
            f"summarize: the run at {run!r} was written by a different version "
            f"(runVersion {found!r}, expected {RUN_VERSION}); the run scratch is not "
            "resumable across versions -- start again with step 0 (-Source)."
        )
    return state


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


def _media_choice_required(state: dict) -> bool:
    """Whether the USER must answer the media-placement question.

    Only an EXISTING local file that is NOT already inside its item raises the
    question: a URL download is ours to place, and an in-place source is already
    beside the summary. A PDF or an image folder has no separate media to place at
    all, so it never asks.

    ``externalLocal`` (recorded at the source step) is required in addition to
    ``sourceMedia`` because a DOWNLOAD also has a ``sourceMedia`` handle (the file
    the pipeline left in the scratch) -- but that one is ours, not the user's.
    """
    return (
        bool(state.get("sourceMedia"))
        and not state.get("internal")
        and bool(state.get("externalLocal"))
    )


def _default_media_mode(state: dict) -> str:
    """The placement used when the user gives no ``-Media``: keep in place, else copy."""
    return "keep" if state.get("internal") else "copy"


def _media_choice(state: dict, mode: str | None = None) -> dict:
    """The media decision to echo in the NAME step's result.

    Named ``mediaChoice``, distinct from the ``source`` step's ``data.media``
    (the readable-handle block): the two steps carry DIFFERENT shapes --
    ``source`` is the description of a question, ``name`` is the scalar answer --
    so they are named apart rather than sharing ``media`` and inviting a
    mis-read. ``mode`` is the placement actually applied (the user's ``-Media``
    or the routing's default). ``default`` is emitted ONLY when a choice is
    actually required: a PDF/image kind has no media to place, and a misleading
    ``"copy"`` there would invent a decision the user was never asked for.
    """
    required = _media_choice_required(state)
    block: dict = {
        "mode": mode or _default_media_mode(state),
        "choiceRequired": required,
    }
    if required:
        block["default"] = _default_media_mode(state)
    return block


def _media_mode(args, state: dict) -> str:
    """The user's media-placement choice, validated and defaulted.

    An omitted ``-Media`` falls back to the routing: a source already inside the
    item is KEPT (the user's file is not moved out of its own folder), anything
    else is COPIED. An explicit value wins, and an unknown one is refused rather
    than silently treated as the default.

    ``move`` is GATED (H3): it RELOCATES the user's own file, deleting the source
    location, so it additionally requires an explicit ``-ConfirmMove``. A mistyped
    or accidental ``move`` therefore cannot destroy where the user kept the file;
    while the run is still alive the source is only ever relocated when the caller
    said so twice (the value AND the acknowledgement).
    """
    chosen = (getattr(args, "media", None) or "").strip().lower()
    if not chosen:
        return _default_media_mode(state)
    if chosen not in _MEDIA_MODES:
        raise ZoombieError(
            f"summarize: -Media must be one of {_MEDIA_MODES}, got {chosen!r}."
        )
    if chosen == "move" and not getattr(args, "confirm_move", False):
        raise ZoombieError(
            "summarize: -Media move RELOCATES the user's own file (the original is "
            "removed). Re-run with -ConfirmMove (and media:\"move\") only after the "
            "user explicitly agreed; use -Media copy to leave the original in place."
        )
    return chosen


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

    # ``media`` is the readable media handle for THIS run, resolved independently of
    # where it finally lands. The pipeline deliberately does NOT duplicate a
    # user-supplied local file, so for a local source the handle is the source file
    # ITSELF, wherever the user keeps it -- nothing media-shaped is left in the run
    # scratch. A URL is downloaded into the scratch, so its handle is what the
    # pipeline left there. The USER then chooses (at the name step) whether the item
    # keeps a copy, a move, or nothing.
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
        if _URL_RE.match(source):
            media = (made.get("sourceFile") and os.path.join(run, made["sourceFile"])) or _media_in(run)
        else:
            # A local source: the user's own file is the media, at its real path.
            media = paths.absolute(source)
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

    is_url = bool(_URL_RE.match(source))
    internal = (not is_url) and workspace.inside_workspace(source)
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

    # The media question is the USER's to answer, and only an EXISTING local file
    # raises it: a download is ours to place, an in-place source is already beside
    # the summary, and a PDF or an image folder has no separate media to place.
    # ``externalLocal`` is recorded so the name step can tell a USER-owned file from
    # a DOWNLOAD (both carry a ``sourceMedia`` handle, but only the former is asked
    # about).
    external_local = (not is_url) and bool(media) and not internal

    _write_run(run, {
        "source": source, "kind": kind, "internal": internal,
        "destination": destination, "proposed": proposed,
        # ``sourceMedia`` is the readable handle; ``media`` becomes the PLACED
        # deliverable at the name step (and equals sourceMedia when nothing is
        # copied). Kept separate so a "no copy"/"keep" choice never blinds the
        # slides step to a perfectly readable source.
        "sourceMedia": media, "media": media, "base": base,
        "externalLocal": external_local,
        "mediaMode": None, "mediaPlaced": None, "destinationFinal": None,
    })

    # The media question is the USER's to answer, and only an EXISTING local file
    # raises it (``external_local`` above). The answer arrives at the name step as
    # -Media, and the source step describes the question in ``data.media`` and in
    # ``why`` -- NOT in ``data.next.args``, which is a replayable invocation.
    choice_required = external_local
    mode_default = _default_media_mode({"internal": internal})
    why = (
        "ASK the user (a separate question) whether to COPY the source media into "
        "the item, MOVE it there (with an explicit confirm), or keep NO copy; then "
        "pass the folder name as -Name and the answer as -Media copy|move|none"
        if choice_required else
        "confirm or replace the folder name with the user, then pass it as -Name; "
        "for an in-place source the folder is fixed, so confirm it as-is, and for a "
        "URL or a rendered source -Media does not apply"
    )
    # ``args`` is the recommended INVOCATION and must be REPLAYABLE: a placeholder
    # like "keep|copy|move|none" is not a valid ``-Media`` choice (it is not in
    # argparse ``choices``) and would exit argparse. The question therefore rides in
    # the sibling ``data.media`` descriptor and in ``why`` -- exactly as the name
    # step reports its ANSWER under ``data.mediaChoice`` -- and ``-Media`` is never
    # added to any ``next.args``.
    next_args = {"-Run": run}
    return Outcome(ok=True, data={
        "step": "source", "run": run, "kind": kind, "internal": internal,
        "proposed": proposed, "destination": destination,
        # ``data.media`` is the source step's DESCRIPTOR (dict): the readable
        # handle plus the question's shape. The NAME step echoes a scalar answer
        # under a DIFFERENT name (``mediaChoice``) so the two shapes are never
        # confused. ``default`` is emitted only when a choice is actually required
        # -- a PDF/image kind has no media to place and must not advertise a
        # misleading "copy" default for a question the user is never asked.
        "media": _source_media_block(
            media=media, internal=internal, choice_required=choice_required,
            default=mode_default,
        ),
        "next": _next("name", next_args, why),
    })


def _source_media_block(
    *, media: str | None, internal: bool, choice_required: bool, default: str
) -> dict:
    """The ``data.media`` DESCRIPTOR emitted by the SOURCE step.

    A dict -- distinct from the NAME step's scalar ``data.media`` answer -- so a
    reader can tell the two apart by shape. ``default`` is present only when
    ``choiceRequired``: otherwise there is no question to default.
    """
    block: dict = {
        "source": media,
        "insideItem": internal,
        "choiceRequired": choice_required,
    }
    if choice_required:
        block["default"] = default
    return block


# --------------------------------------------------------------------------- #
# step 1: name
# --------------------------------------------------------------------------- #


def _step_name(args) -> Outcome:
    run = args.run
    state = _read_run(run)
    if state.get("destinationFinal"):
        raise ZoombieError("summarize: the name step already ran for this run.")

    # H2: validate the media answer FIRST, BEFORE the destination is computed or
    # created. A bad ``-Media`` must raise before ANY folder exists: otherwise a
    # retry finds the name already taken (and an empty item folder to clean up),
    # so one typo consumes a unique name and litters ``My Item``/``My Item (2)``.
    mode = _media_mode(args, state)

    # M2: a ``sourceMedia`` that VANISHED between the source and name steps is
    # named HERE, at the step that recorded it -- never left for ``slides`` to
    # report as the misleading "no retained media to extract slides from". The
    # error names the exact path, so the user knows WHAT disappeared.
    source_media = state.get("sourceMedia") or state.get("media")
    if source_media and not paths.is_file(source_media):
        raise ZoombieError(
            f"summarize: the source media is gone: {source_media} no longer exists "
            "(it was recorded at the source step). Re-run step source to produce "
            "it again."
        )

    destination = state["destination"]
    if state.get("internal"):
        # In place: the destination is the source folder; the media is already
        # there and is never moved or renamed.
        final = destination
    else:
        name = workspace.sanitize_name(args.name) if args.name else state["proposed"]
        # M1: the external parent is the RECORDED destination's parent -- the
        # folder the source step already reported as ``data.destination`` -- not a
        # fresh ``_unsorted`` path recomputed from the cwd. Since
        # ``workspace_root()`` is unconditionally the cwd, recomputing here could
        # name a DIFFERENT parent than the source step routed to; the item must
        # land where the source step said it would.
        parent = os.path.dirname(destination) or destination
        # REUSE an existing item of ours rather than accreting ``Name (2)``: a
        # re-run is a re-run of the SAME item, and the overwrite gate below -- not
        # a second folder nobody asked for -- is what protects the user's document.
        # A folder we did NOT make still falls back to ``unique_name``, so we never
        # write into a directory that is not ours.
        if _reusable_item(os.path.join(parent, name)):
            final = os.path.join(parent, name)
        else:
            final = os.path.join(parent, workspace.unique_name(parent, name))

    paths.assert_fits(final, "The summarize destination")
    paths.ensure_dir(final)

    # Place the media according to the USER'S choice (-Media), defaulted from the
    # routing. ``source_media`` is the readable handle; ``placed`` is where it now
    # lives INSIDE the item, or None when the user keeps no copy.
    #   keep -- already beside the summary (an in-place source): nothing to do.
    #   copy -- duplicate the source into the item; the original is untouched.
    #   move -- move the source into the item; the original is gone.
    #   none -- keep no copy; the item references the source where it lies.
    placed: str | None = None
    if source_media:
        target = os.path.join(final, os.path.basename(source_media))
        same = (
            os.path.normcase(paths.absolute(source_media))
            == os.path.normcase(paths.absolute(target))
        )
        if same and mode != "none":
            # Already the item's file (an in-place source, or a re-run): nothing to
            # move or copy. An explicit "none" still wins and records no placement.
            placed = target
        elif not same and mode == "move":
            paths.move(source_media, target)
            source_media = target
            placed = target
        elif not same and mode == "copy":
            paths.copy_file(source_media, target)
            placed = target
        # "none" (or "keep" for a source outside the item): leave it where it is.

    # Figures are NOT published here. They are extracted into the run scratch by the
    # slides step and published by the prose step, which knows the final keep/drop
    # selection. Publishing early would copy frames the agent later drops.

    # OVERWRITE SAFETY. Exactly two artifacts belong to the tool and are the only
    # things a re-run may replace: the document and the figures. The media and every
    # other file in the folder are the user's own material and are never touched --
    # for an IN-PLACE source the item folder IS the source folder, so a folder-level
    # wipe here would delete the user's recording. That invariant is why the reuse
    # rule above never deletes a directory, why ``_publish_figures`` rebuilds only
    # ``img/``, and why this gate names only ``summary.md`` and ``img/``.
    #
    # The gate REFUSES rather than archiving on its own: the user must be told what
    # is at risk and be free to back down and archive by hand. ``-Archive`` takes a
    # snapshot of the document AND its figures; ``-Overwrite`` replaces them with no
    # backup. Neither flag is required for an empty target, so a first run is
    # unchanged.
    existing = _existing_artifacts(final)
    overwrite_required = bool(existing["summary"])
    if overwrite_required and not (args.overwrite or args.archive):
        raise ZoombieError(
            f"summarize: {final} already holds a summary ({existing['summary']}), and "
            f"{existing['images']} figure(s) in {item_paths.IMAGE_DIR_NAME}/. "
            "Proceeding would replace the document and rebuild the figures. Ask the "
            "user first: pass -Archive to move the existing document AND its figures "
            "into a summary_<timestamp>/ folder, or -Overwrite to replace them with "
            "no backup. The source media is never touched either way."
        )

    state["destinationFinal"] = final
    # ``sourceMedia`` is the readable handle (always the file to READ from);
    # ``media`` is the PLACED deliverable in the item, or None when the user kept
    # no copy. Keeping them distinct is what lets ``slides`` fall back to
    # ``sourceMedia`` when ``media`` is None -- a "none" choice must not blind the
    # slides step to a perfectly readable source.
    state["sourceMedia"] = source_media
    state["media"] = placed
    state["mediaMode"] = mode
    state["mediaPlaced"] = placed

    # The snapshot happens HERE, at the START of the task -- not at prose -- so it
    # is the document as the USER left it, never an intermediate the run produced.
    # Prose may run several times and only ever rewrites the live document, so a
    # task takes at most one snapshot. Taking it is the user's explicit choice
    # (``-Archive``); there is no silent backup any more.
    archived = _archive_existing(final) if args.archive else None
    state["archived"] = archived
    state["overwrote"] = overwrite_required
    _write_run(run, state)

    # The slides question is the agent's to ASK, so ``-Slides`` cannot ride in
    # ``next.args``: it is an answer, not a step of the invocation. The ``why``
    # therefore names the flag the agent must supply, exactly as the source step's
    # ``why`` names ``-Media``.
    question = "ask whether to extract slide frames (a separate question)"
    if archived:
        why = (
            f"the previous document and its figures were archived to "
            f"{os.path.basename(archived['to'])}/ -- tell the user where their work "
            f"went; then {question} and call step slides with -Slides true/false"
        )
    elif overwrite_required:
        why = (
            "the user chose to overwrite with no backup, so the previous document "
            f"was replaced in place and its figures will be rebuilt; then {question} "
            "and call step slides with -Slides true/false"
        )
    else:
        why = f"{question}, then call step slides with -Slides true/false"
    return Outcome(ok=True, data={
        "step": "name", "run": run, "itemDir": final,
        # ``media`` is the PLACED deliverable (a path, or None when no copy was
        # kept) -- a scalar, unlike the source step's ``data.media`` DESCRIPTOR.
        "media": placed, "mediaMode": mode, "sourceMedia": source_media,
        # H1/L1/L2: the media DECISION echoed under its own name, so the answer is
        # machine-readable next to ``why`` and the two steps' media shapes never
        # collide. ``default`` rides only when a choice was required.
        "mediaChoice": _media_choice(state, mode),
        # What the target ALREADY held, so the answer is machine-readable rather
        # than inferred from ``why``: the document at risk, its figure count, the
        # media that survives, and any strays a human should look at.
        "existing": existing,
        "overwrite": {
            "required": overwrite_required,
            "archived": bool(archived),
            "mode": ("archive" if archived else
                     "overwrite" if overwrite_required else None),
        },
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
    # A keep/drop can only mean "prune the frames I was just shown", so naming ids
    # implies the run. WITHOUT this the selection was parsed and silently discarded:
    # the step answered ok/exit 0 with an UNCHANGED frame set, which reads as
    # success. Silently failing open on a narrowing intent is the worst shape a
    # selection can have -- the whole frame set is published instead of the kept
    # subset -- so the flag is INFERRED and the inference is logged.
    if not want and (getattr(args, "keep", None) or getattr(args, "drop", None)):
        want = True
        process.log(
            "  -Keep/-Drop given without -Slides: treating it as -Slides true "
            "(a selection implies the run it selects from)",
            "step",
        )
    media = state.get("media") or state.get("sourceMedia")
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
        "fNNN or timestamp): call step slides again with -Slides true -Keep/-Drop to "
        "prune talking-head frames (e.g. -Slides true -Drop \"f001,f003\"), or call "
        "step prose to accept them all; then check data.slides.selection.applied -- "
        "false means the selection was NOT honoured"
        if proposals else
        "write the title, the short summary, an optional criticism and the "
        "topic-change section headings (with an anchor phrase for each), then call "
        "step prose; the backend assembles block 6"
    )
    # ``-Slides true`` is part of the returned invocation, not a placeholder: the
    # prune step is only replayable WITH it, because without it the selection is
    # ignored. The question of WHETHER to prune stays in ``why``.
    return Outcome(ok=True, data={
        "step": "slides", "run": run, "itemDir": item_dir, "slides": frames,
        "next": _next(
            "slides" if proposals else "prose",
            {"-Run": run, "-Slides": "true"} if proposals else {"-Run": run},
            why,
        ),
    })


# --------------------------------------------------------------------------- #
# step 3: prose
# --------------------------------------------------------------------------- #


def _reusable_item(folder: str) -> bool:
    """Whether ``folder`` is an EXISTING item we may write into again.

    True when the folder does not exist (nothing to reuse but nothing to avoid), is
    empty (an aborted run's leftover), or already holds our document or figures.
    False for a folder holding anything else, so a name collision with a stranger's
    directory still falls back to ``unique_name`` and we never write into -- or
    archive -- a directory that is not ours.
    """
    if not paths.is_dir(folder):
        return not paths.exists(folder)
    try:
        entries = paths.list_dir(folder)
    except OSError:
        return False
    if not entries:
        return True
    names = {entry.name.lower() for entry in entries}
    return item_paths.SUMMARY_NAME.lower() in names or item_paths.IMAGE_DIR_NAME.lower() in names


def _existing_artifacts(folder: str) -> dict:
    """What an existing item holds, split by ownership.

    The tool owns exactly ``summary.md`` and ``img/``; everything else -- the
    media, a stray note -- is the user's and is reported rather than touched. This
    is the factual basis of the overwrite gate, so the question the agent puts to
    the user names real paths and a real figure count, not a guess.
    """
    summary = item_paths.summary_path(folder)
    image_dir = item_paths.image_dir(folder)
    media: list[str] = []
    others: list[str] = []
    if paths.is_dir(folder):
        for entry in paths.list_dir(folder, files=True):
            name = entry.name.lower()
            if name == item_paths.SUMMARY_NAME.lower():
                continue
            if os.path.splitext(name)[1] in (_VIDEO_EXT | _AUDIO_EXT | _PDF_EXT | _IMAGE_EXT):
                media.append(entry.name)
            else:
                others.append(entry.name)
    figures = 0
    if paths.is_dir(image_dir):
        figures = len([
            entry for entry in paths.list_dir(image_dir, files=True)
            if entry.name.lower().endswith(".png")
        ])
    return {
        "summary": summary if paths.is_file(summary) else None,
        "images": figures,
        "media": media,
        "others": others,
    }


def _archive_existing(item_dir: str) -> dict | None:
    """Move ``summary.md`` AND its ``img/`` into ``summary_<yyyyMMdd_HHmm>/``.

    The archive is a FOLDER because the figures are index-numbered: a 20-frame run
    and a 17-frame run disagree about what ``005 - ...`` means, so a flat backup
    beside a rebuilt ``img/`` would repoint the archived document at the wrong
    pictures, or at none. Moving the pair keeps every relative ``img/...`` link
    valid without rewriting a single one.

    Only the document is required; a run that kept no figures archives the document
    alone. The media is never moved. Returns ``{"from", "to", "timestamp", "images"}``
    or ``None`` when there was nothing to archive.
    """
    summary = item_paths.summary_path(item_dir)
    if not paths.is_file(summary):
        return None
    stamp = _timestamp_of(summary)
    archived = os.path.join(item_dir, f"summary_{stamp}")
    index = 1
    while paths.exists(archived):
        index += 1
        archived = os.path.join(item_dir, f"summary_{stamp} ({index})")
    paths.ensure_dir(archived)
    paths.move(summary, os.path.join(archived, item_paths.SUMMARY_NAME))
    image_dir = item_paths.image_dir(item_dir)
    figures = 0
    if paths.is_dir(image_dir):
        figures = len([
            entry for entry in paths.list_dir(image_dir, files=True)
            if entry.name.lower().endswith(".png")
        ])
        paths.move(image_dir, item_paths.image_dir(archived))
    process.log(f"  archived the previous summary and its figures -> {archived}", "step")
    return {"from": summary, "to": archived, "timestamp": stamp, "images": figures}


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


def _code_span(text: str) -> str:
    """``text`` as a Markdown code span, safe against spaces/Cyrillic/parens/quotes.

    Block 2 sometimes names a raw origin path or URL rather than a link into the
    item (the user kept no copy, or a URL was summarized). That value is
    content-controlled -- it can carry a space, parentheses, a quote or a backtick
    -- and emitted bare it would be re-parsed as emphasis or a broken link. A code
    span swallows all of them. The one character a code span cannot carry is a
    backtick, so a value containing one is fenced with a longer run and
    space-padded, per CommonMark.
    """
    if not text:
        return ""
    if "`" not in text:
        return f"`{text}`"
    fence = "`" * (max(len(run) for run in re.findall(r"`+", text)) + 1)
    return f"{fence} {text} {fence}"


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
    # Block 2 points at the media only when it actually sits IN the item; otherwise
    # it names the origin (the source path or URL) so the reference is never a link
    # to a file the item does not own.
    #
    # L3: a WELL-FORMED relative link is percent-encoded by ``_relative_link``, but a
    # raw origin path/URL is not -- an absolute path with spaces, Cyrillic, parens or
    # a quote would land in the document verbatim. Wrap that origin in backticks (a
    # code span) so Markdown never re-parses it as emphasis, a link, or a broken
    # construct; a backtick inside the path is the one character a code span cannot
    # carry, so it is escaped by pairing.
    placed = state.get("mediaPlaced")
    source_line = (
        _relative_link(item_dir, placed) if placed
        else _code_span(state.get("source") or "")
    )
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

    # NO archive here: the name step already took the snapshot -- if the user asked
    # for one -- at the START of the task. The prose step only writes the live
    # summary, so re-running it
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
            f"the previous document and its {archived.get('images', 0)} figure(s) were "
            f"archived to {os.path.basename(archived['to'])}/ -- tell the user where "
            "their work went; then check the finished tree"
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

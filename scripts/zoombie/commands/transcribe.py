"""``transcribe``: turn an audio file into text, subtitles and an origin sidecar.

The stage is a pure SOURCE PRODUCER: ``<base>.txt`` and ``<base>.srt`` are written
on every run (``-NoSrt`` opts the SRT out), together with a ``<base>.source.json``
origin sidecar. The SRT is what a downstream indexing pass reads its timings from,
and the sidecar is what it reads the origin link from.
"""

from __future__ import annotations

from ..cli import Outcome
from ..lib import env as env_mod, paths, scratch, stt
from ..lib.errors import ZoombieError


def run(args) -> Outcome:
    if not paths.is_file(args.source):
        raise ZoombieError(f"Input not found: {args.source}")

    # ``-Output`` names the ITEM folder; the artifacts go to ``<item>/.data/``.
    item_dir = stt.item_dir_for(args.output, args.source)
    base = stt.resolve_output_base(args.source, args.output)
    stt.ensure_item_dir(args.output, item_dir)

    # Refused BEFORE the environment is resolved and before any scratch dir
    # exists, so an accidental re-run costs nothing. ``stt.transcribe`` repeats the
    # check, which is what covers ``pipeline``.
    #
    # The guard must check the base the WRITER will use, window included: checking
    # ``transcript.txt`` while a window writes ``transcript - <from> - <to>.txt``
    # both false-refused a window run and missed a real window collision.
    # ``request_output_base`` is that one expression, shared with the writer.
    window_request = stt.Request(
        audio_path=args.source, output_base=base, item_dir=item_dir,
        from_time=getattr(args, "from_time", None),
        to_time=getattr(args, "to_time", None),
    )
    stt.guard_overwrite(stt.request_output_base(window_request), args.force)

    environment = env_mod.resolve(args.model)

    request = stt.Request(
        audio_path=args.source,
        output_base=base,
        item_dir=item_dir,
        language=args.language,
        # Want_srt is the accepted no-op alias; no_srt is what actually governs.
        want_srt=args.srt,
        no_srt=args.no_srt,
        force=args.force,
        no_gpu=args.no_gpu,
        no_flash_attn=args.no_flash_attn,
        threads=args.threads,
        # A time window: -From/-To decode only [From, To) as a short file. The
        # stage refuses an inverted window and qualifies the artifact names, so
        # a window never overwrites the full transcript. No splicing is done.
        # ``getattr`` keeps a programmatic caller that predates the flags working.
        from_time=getattr(args, "from_time", None),
        to_time=getattr(args, "to_time", None),
        allow_cpu_fallback=args.allow_cpu_fallback,
        strict_gpu=args.strict_gpu,
        work_root=args.work_root,
        # -KeepScratch (plan §10) and its alias -KeepWork are the same decision.
        keep_work=scratch.keep_requested(args),
        dry_run=args.dry_run,
    )

    report = stt.transcribe(environment, request)
    return Outcome(ok=True, data=report.to_data())

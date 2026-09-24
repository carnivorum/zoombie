"""``transcribe``: turn an audio file into text, subtitles and an origin sidecar.

The stage is a pure SOURCE PRODUCER: ``<base>.txt`` and ``<base>.srt`` are written
on every run (``-NoSrt`` opts the SRT out), together with a ``<base>.source.json``
origin sidecar. The SRT is what a downstream indexing pass reads its timings from,
and the sidecar is what it reads the origin link from.
"""

from __future__ import annotations

from ..cli import Outcome
from ..lib import env as env_mod, paths, stt
from ..lib.errors import ZoombieError


def run(args) -> Outcome:
    if not paths.is_file(args.source):
        raise ZoombieError(f"Input not found: {args.source}")

    # ``-Output`` names the ITEM folder; the artifacts go to ``<item>/.data/``.
    item_dir = stt.item_dir_for(args.output, args.source)
    base = stt.resolve_output_base(args.source, args.output)

    # Refused BEFORE the environment is resolved and before any scratch dir
    # exists, so an accidental re-run costs nothing. ``stt.transcribe`` repeats the
    # check, which is what covers ``pipeline``.
    stt.guard_overwrite(base, args.force)

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
        allow_cpu_fallback=args.allow_cpu_fallback,
        strict_gpu=args.strict_gpu,
        work_root=args.work_root,
        keep_work=args.keep_work,
        dry_run=args.dry_run,
    )

    report = stt.transcribe(environment, request)
    return Outcome(ok=True, data=report.to_data())

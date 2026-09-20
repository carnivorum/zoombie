"""``transcribe``: turn an audio file into text (and optionally .srt)."""

from __future__ import annotations

from ..cli import Outcome
from ..lib import env as env_mod, paths, stt
from ..lib.errors import ZoombieError


def run(args) -> Outcome:
    environment = env_mod.resolve(args.model)

    if not paths.is_file(args.source):
        raise ZoombieError(f"Input not found: {args.source}")

    base = stt.resolve_output_base(args.source, args.output)

    request = stt.Request(
        audio_path=args.source,
        output_base=base,
        language=args.language,
        want_srt=args.srt,
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

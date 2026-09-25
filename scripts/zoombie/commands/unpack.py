"""``unpack``: extract an NSIS installer or a 7z archive into a confirmed folder.

This is the first-class form of the capability the installer uses internally. It
exists because unpacking an installer is exactly the work an agent should never do
by hand: doing it manually pollutes the workspace with a tree nobody asked for and
then burns tokens listing files that should never have been read.

Three properties are enforced here rather than left to prose:

* **Nothing is executed.** The source is data: 7-Zip reads the NSIS payload as an
  archive. No ``/S`` switch, no registry writes, no elevation.
* **The ASCII-work-dir invariant holds.** Extraction happens in a scratch dir under
  the (ASCII) toolchain root, and the selected files are copied to the caller's
  confirmed destination through managed I/O, so no native tool ever receives a
  non-ASCII path.
* **Installer scaffolding never lands.** ``$PLUGINSDIR`` is excluded by DIRECTORY,
  so the installer's own DLLs cannot be mistaken for the product's.

Modes follow the house convention: ``-Check`` lists the archive and writes nothing,
``-DryRun`` computes the real file list in scratch and writes nothing to the
destination, and apply provisions the extractor when needed.
"""

from __future__ import annotations

import os

from ..cli import Outcome
from ..lib import paths, process, scratch
from ..lib import unpack as unpack_mod
from ..lib.errors import ZoombieError

# Extensions this verb claims to understand. The extension is a HINT, not the
# truth -- 7-Zip sniffs an NSIS payload inside an .exe -- so an unexpected suffix
# is reported rather than refused.
KNOWN_SUFFIXES = (".exe", ".7z", ".7zip", ".zip", ".rar", ".tar", ".gz", ".bz2", ".xz", ".iso")

# How many file names the result payload carries inline before it truncates. The
# caller can read the archive itself for the rest; this is a report, not a listing.
MAX_REPORTED_FILES = 200


def _kind(source: str) -> str:
    """A plain-language label for what the source most likely is."""
    suffix = paths.extension_of(source).lower()
    if suffix in (".exe", ".msi"):
        return "nsis-installer"
    if suffix in (".7z", ".7zip"):
        return "7z-archive"
    if suffix == ".zip":
        return "zip-archive"
    return "archive"


def _existing_targets(destination: str, selection: list[dict]) -> list[str]:
    """Selected files whose destination path already exists."""
    return [
        item["relative"]
        for item in selection
        if paths.exists(os.path.join(destination, item["relative"]))
    ]


def run(args) -> Outcome:
    """Entry point behind ``zoombie unpack``."""
    if not getattr(args, "output", None):
        raise ZoombieError("Pass -Output <dir>: the folder that receives the extracted files.")

    check = bool(getattr(args, "check", False))
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))
    # -KeepScratch is the plan-§10 name; -KeepWork its historical alias.
    keep_work = scratch.keep_requested(args)
    strip = int(getattr(args, "strip", 0) or 0)
    include = getattr(args, "include", None) or None

    if strip < 0:
        raise ZoombieError("-Strip must be zero or a positive number of path levels.")

    source = paths.absolute(args.source)
    if not paths.is_file(source):
        raise ZoombieError(f"Source not found: {source}")
    paths.assert_fits(source, "The unpack source path", slack=6)

    destination = paths.absolute(args.output)
    if paths.is_file(destination):
        raise ZoombieError(f"-Output is an existing FILE, not a folder: {destination}")
    paths.assert_fits(destination, "The unpack output path", slack=48)

    extractor = unpack_mod.extractor_path()
    extractor_present = paths.is_file(extractor)
    kind = _kind(source)
    suffix = paths.extension_of(source).lower()

    report: dict = {
        "source": source,
        "kind": kind,
        "suffixKnown": suffix in KNOWN_SUFFIXES,
        "output": destination,
        "strip": strip,
        "include": include,
        "extractor": {
            "path": extractor,
            "name": unpack_mod.EXTRACTOR["name"],
            "version": unpack_mod.EXTRACTOR["version"],
            "present": extractor_present,
        },
        "asciiSafe": True,
    }
    if suffix not in KNOWN_SUFFIXES:
        process.log(
            f"unrecognised extension '{suffix}'; 7-Zip will decide whether it can read it",
            "warn",
        )

    # --- -Check: report the archive's contents, touching nothing --------------
    if check:
        entries: list[dict] = []
        if extractor_present:
            entries = unpack_mod.list_archive(source, extractor)
        else:
            process.log(
                "the 7-Zip extractor is not provisioned; run `python -m zoombie.install` "
                "to list this source",
                "warn",
            )
        report.update(
            check=True,
            dryRun=False,
            entries=len(entries),
            files=[entry["path"] for entry in entries[:MAX_REPORTED_FILES]],
            filesTruncated=len(entries) > MAX_REPORTED_FILES,
            note=None if extractor_present else "extractor not provisioned",
        )
        process.log(f"unpack (check): {len(entries)} entr(y/ies) in {os.path.basename(source)}")
        return Outcome(ok=True, data=report)

    # --- Apply / -DryRun: extract to scratch, select, then copy --------------
    # -DryRun never downloads the extractor, so an unprovisioned machine is told
    # what is missing instead of silently fetching 25 MB to answer a question.
    if not extractor_present and dry_run:
        report.update(
            check=False,
            dryRun=True,
            planned=0,
            files=[],
            filesTruncated=False,
            note="extractor not provisioned; run `python -m zoombie.install` first",
        )
        process.log("unpack (dry run): the 7-Zip extractor is not provisioned", "warn")
        return Outcome(ok=True, data=report)

    if not extractor_present or force:
        # Reached only outside -Check/-DryRun, so writing here is permitted.
        process.log(
            f"provisioning pinned 7-Zip {unpack_mod.EXTRACTOR['version']} extractor", "step"
        )
        unpack_mod.install_extractor()

    work = paths.new_temp_dir("zoombie-unpack")
    try:
        staging = os.path.join(work, "payload")
        process.log(f"extracting {os.path.basename(source)} -> {staging}")
        unpack_mod.extract(source, extractor, staging)

        # The GENERIC choke point: scaffolding excluded by directory, then -Strip
        # and -Include applied. Nothing product-specific is involved.
        selection = unpack_mod.select_payload(staging, strip=strip, include=include)
        total_bytes = sum(
            paths.file_size(item["source"]) for item in selection
        )
        report.update(
            check=False,
            dryRun=dry_run,
            count=len(selection),
            files=[item["relative"] for item in selection[:MAX_REPORTED_FILES]],
            filesTruncated=len(selection) > MAX_REPORTED_FILES,
            bytes=total_bytes,
            scaffoldingExcluded=True,
        )

        if dry_run:
            report["note"] = "dry run: nothing was written to the destination"
            process.log(
                f"unpack (dry run): {len(selection)} file(s) would be written to {destination}"
            )
            return Outcome(ok=True, data=report)

        # No overwrite unless -Force, matching every other command's convention.
        conflicts = _existing_targets(destination, selection)
        if conflicts and not force:
            listing = ", ".join(conflicts[:5]) + (" ..." if len(conflicts) > 5 else "")
            raise ZoombieError(
                f"{len(conflicts)} target file(s) already exist in {destination} "
                f"({listing}). Use -Force to overwrite, or a different -Output."
            )

        written = unpack_mod.copy_payload(
            staging, destination, strip=strip, include=include
        )
        report["written"] = len(written)
        process.log(f"unpack: {len(written)} file(s) -> {destination}", "step")
        if keep_work:
            report["workDir"] = work
            process.log(f"kept the scratch dir: {work}")
        return Outcome(ok=True, data=report)
    finally:
        # CLI-owned cleanup (plan §10): the extracted files were already COPIED to
        # the confirmed destination, so removing this scratch invalidates nothing.
        # Reported rather than done silently, so a retained/leftover dir is legible.
        block = scratch.report(work, kept=keep_work)
        report["scratch"] = block
        # Flagged explicitly (the house verb for the same idea) so an agent can call
        # `clean -CleanScratch` without having to infer it from ``leftover``.
        report["cleanScratchVerb"] = "clean -CleanScratch" if block["leftover"] else None

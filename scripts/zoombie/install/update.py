"""Fetch the latest repo and run the installer: the ``setup.ps1`` replacement.

The contract is unchanged from the PowerShell bootstrap: **any** start of setup
means "install or update to the latest". The worker is always re-fetched, so
there is no cached copy to go stale and no gate that can skip the update.

The difference is that this now runs INSIDE Python, so there is one code path
rather than a bootstrap that spawns a worker that imports a module.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile

from ..lib import download, paths, process

DEFAULT_REPO_SLUG = "carnivorum/zoombie"
DEFAULT_REPO_REF = "main"


def repo_slug() -> str:
    return os.environ.get("ZOOMBIE_REPO_SLUG") or DEFAULT_REPO_SLUG


def repo_ref() -> str:
    return os.environ.get("ZOOMBIE_REPO_REF") or DEFAULT_REPO_REF


def archive_url() -> str:
    return f"https://codeload.github.com/{repo_slug()}/zip/refs/heads/{repo_ref()}"


def fetch_repo(destination: str) -> bool:
    """Download and extract the repo archive into ``destination``.

    One request keeps the tree consistent, which is why the archive is preferred
    over per-file raw fetches. Returns False (rather than raising) so the caller
    can decide whether a stale local checkout is an acceptable fallback.
    """
    url = archive_url()
    process.log(f"==> download {url}", "step")
    try:
        with download._open(url) as response:  # noqa: SLF001 - the stream helper
            payload = response.read()
    except Exception as exc:  # noqa: BLE001 - reported as a soft failure
        process.log(f"archive fetch failed: {exc}", "warn")
        return False

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            archive.extractall(paths.to_extended(destination))
    except (OSError, zipfile.BadZipFile) as exc:
        process.log(f"archive extraction failed: {exc}", "warn")
        return False
    return True


def extracted_root(directory: str) -> str | None:
    """The single top-level folder a GitHub archive extracts into."""
    entries = paths.list_dir(directory, dirs=True)
    return entries[0].path if entries else None


def run(argv: list[str] | None = None) -> int:
    """Refresh the repo into a temp checkout, then run the installer from it.

    ``sys.path`` is bootstrapped so the installer is importable from the fresh
    checkout, which is what makes "run any copy, get the latest" work: an old
    local ``update.py`` cannot install a stale tree, because it re-fetches the
    tree it then runs.
    """
    process.log("==> zoombie bootstrap: fetching the latest repo", "step")

    staging = paths.new_temp_dir("zoombie-update")
    try:
        if not fetch_repo(staging):
            process.log(
                "could not fetch the latest repo; running the installer from this copy",
                "warn",
            )
            from .main import run as run_install
            return run_install(argv, write_result=True)

        root = extracted_root(staging)
        if not root:
            process.log("the fetched archive contained no folder", "warn")
            from .main import run as run_install
            return run_install(argv, write_result=True)

        scripts_dir = os.path.join(root, "scripts")
        if not os.path.isdir(os.path.join(scripts_dir, "zoombie")):
            process.log("the fetched archive has no scripts/zoombie package", "warn")
            from .main import run as run_install
            return run_install(argv, write_result=True)

        process.log(f"==> running the installer from {scripts_dir}")

        # Run the fresh copy in a subprocess. Importing it in-process would keep
        # THIS module's already-imported siblings, so a stale local checkout would
        # still be what executes.
        argv_list = [
            sys.executable, "-m", "zoombie.install",
            *([] if argv is None else argv),
        ]
        # The child's stderr is INHERITED, not captured, so its per-component
        # trace reaches the user exactly as it does on the local-installer
        # fallback path below (and as it did when PowerShell spawned the worker).
        # Capturing it with process.run would swallow the whole log and leave only
        # our own three lines visible. Stdout is captured because it carries the
        # single JSON result line that must be reproduced, not printed twice.
        completed = subprocess.run(
            argv_list,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,
            env=process.child_env({"PYTHONPATH": scripts_dir}),
            check=False,
        )
        output = process.decode_console(completed.stdout or b"")

        # Reproduce the installer's single JSON line on our stdout so callers
        # parse this entry point exactly as they parse the installer. Any other
        # line on the child's stdout is progress, so it joins the trace on stderr.
        result_line = None
        for line in output.splitlines():
            if line.strip().startswith("{"):
                result_line = line
            elif line.strip():
                process.log(line)
        if result_line:
            sys.stdout.write(result_line + "\n")
            sys.stdout.flush()

        return completed.returncode
    finally:
        paths.remove_quietly(staging, recursive=True)


if __name__ == "__main__":
    sys.exit(run())

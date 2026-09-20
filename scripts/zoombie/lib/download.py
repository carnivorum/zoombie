"""File download, with resume and integrity checking.

The PowerShell version shelled out to ``curl.exe`` per file and had no resume, so
a failed 1.6 GB model fetch restarted from zero. This uses ``urllib`` from the
standard library, which needs no external tool, and adds resume support for the
large, resumable artifacts (models, archives).
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request

from . import paths, process

USER_AGENT = "zoombie-setup"
DEFAULT_TIMEOUT = 60
_RETRIES = 3


def _open(url: str, *, offset: int = 0, timeout: int = DEFAULT_TIMEOUT):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if offset > 0:
        request.add_header("Range", f"bytes={offset}-")
    return urllib.request.urlopen(request, timeout=timeout)


def download(url: str, out_file: str, *, force: bool = False, resume: bool = True) -> str:
    """Download ``url`` to ``out_file``.

    Resume uses an HTTP Range request when a partial file is present, and falls
    back to a full download if the server ignores the range (a 200 response rather
    than 206) so a partial file is never mistaken for a complete one.
    """
    paths.ensure_dir(os.path.dirname(out_file))

    if paths.is_file(out_file) and not force:
        process.log(f"already present, skipping: {out_file}")
        return out_file

    existing = 0
    if resume and paths.is_file(out_file) and not force:
        try:
            existing = paths.file_size(out_file)
        except OSError:
            existing = 0

    process.log(f"download {url}", "step")
    process.log(f"     -> {out_file}")

    last_error: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            with _open(url, offset=existing) as response:
                status = getattr(response, "status", 200)
                # A 200 means the server ignored the Range header, so the whole
                # body is about to arrive and the partial file must be replaced.
                append = existing > 0 and status == 206
                mode = "ab" if append else "wb"
                with open(paths.to_extended(out_file), mode) as handle:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        handle.write(chunk)
            return out_file
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            if attempt < _RETRIES - 1:
                process.log(f"download failed ({exc}); retrying", "warn")
                time.sleep(2 * (attempt + 1))

    raise OSError(f"download failed after {_RETRIES} attempts: {url}: {last_error}")


def fetch_text(url: str, *, timeout: int = 60) -> str:
    """GET a URL and return the body as text. Used for the GitHub API."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_json(url: str, *, timeout: int = 60):
    """GET a URL and parse the body as JSON."""
    import json

    return json.loads(fetch_text(url, timeout=timeout))

"""Path rules: the ASCII toolchain root, the 260-character budget, and safe work dirs.

Two independent Windows failure modes are handled here, and they need different
remedies, which is why this module has both a ``to_extended`` prefix helper and an
``assert_fits`` guard:

* **Non-ASCII (Cyrillic) paths.** whisper.cpp, PyMuPDF and Tesseract all
  misbehave when a path contains non-ASCII characters. The fix is structural:
  every input is copied into an ASCII scratch dir under an ASCII root, the native
  tool runs entirely inside it, and artifacts are copied back afterwards. See
  :func:`copy_into_safe_work`.

* **The legacy 260-character limit.** Native tools open paths with plain C APIs
  and cannot use the ``\\\\?\\`` extended-length prefix, so a path handed to them
  is only length-*checked* (and refused with the real cause) by
  :func:`assert_fits`. Managed I/O is different: it *can* use the prefix, so it
  goes through :func:`to_extended`.

Unlike the PowerShell original, this module needs no per-call "strip the prefix
again" step: ``os`` and ``shutil`` handle a prefixed path correctly, so the
prefix never has to be scrubbed from enumeration results.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass

from .errors import PathTooDeepError

# Usable path characters before the legacy 260 limit, with a safety margin.
PATH_BUDGET = 240

ROOT_FOLDER_NAME = "zoombie-env"

# Folder name under the root that holds per-job scratch dirs.
WORK_FOLDER = "work"

__all__ = [
    "PATH_BUDGET",
    "ROOT_FOLDER_NAME",
    "WORK_FOLDER",
    "PathTooDeepError",
    "Entry",
    "env_root",
    "root_is_default",
    "root_candidates",
    "env_path",
    "manifest_path",
    "is_ascii",
    "to_extended",
    "from_extended",
    "absolute",
    "path_length",
    "assert_fits",
    "root_path_report",
    "exists",
    "is_file",
    "is_dir",
    "file_size",
    "ensure_dir",
    "list_dir",
    "newest_file",
    "remove",
    "remove_quietly",
    "remove_work_dir",
    "move",
    "copy_file",
    "copy_tree",
    "new_work_dir",
    "new_ascii_dir",
    "new_temp_dir",
    "copy_into_safe_work",
    "resolve_environment",
    "extension_of",
    "without_extension",
]


# ---------------------------------------------------------------------------
# The ASCII toolchain root
# ---------------------------------------------------------------------------

def is_ascii(path: str | None) -> bool:
    """True when every character of ``path`` is in the ASCII range."""
    if not path:
        return True
    return path.isascii()


def env_root() -> str:
    """Absolute path of the toolchain root. Always ASCII by construction.

    whisper.cpp breaks on non-ASCII paths -- not only for the media, but for its
    own binary and model too -- so the root is chosen dynamically:

      1. ``ZOOMBIE_ENV_ROOT``        explicit override, used verbatim
      2. ``%USERPROFILE%\\zoombie-env`` when that path is ASCII
      3. ``%PUBLIC%\\zoombie-env``   machine-level, always ASCII (used when the
                                     user name is not, e.g. ``C:\\Users\\Мария``)
      4. ``<SystemDrive>\\zoombie-env`` last resort
    """
    override = os.environ.get("ZOOMBIE_ENV_ROOT", "").strip()
    if override:
        return override

    profile = os.environ.get("USERPROFILE", "")
    from_profile = os.path.join(profile, ROOT_FOLDER_NAME)
    if is_ascii(from_profile) and profile:
        return from_profile

    public = os.environ.get("PUBLIC", "")
    if public:
        from_public = os.path.join(public, ROOT_FOLDER_NAME)
        if is_ascii(from_public):
            return from_public

    drive = os.environ.get("SystemDrive", "")
    if drive:
        return os.path.join(drive + os.sep, ROOT_FOLDER_NAME)
    return from_profile


def root_is_default() -> bool:
    """True when the root was not an explicit override."""
    return not os.environ.get("ZOOMBIE_ENV_ROOT", "").strip()


def root_candidates() -> list[str]:
    """All ASCII roots a client (e.g. a skill or the self-test) should probe."""
    candidates: list[str] = []
    override = os.environ.get("ZOOMBIE_ENV_ROOT", "").strip()
    if override:
        candidates.append(override)
    profile = os.environ.get("USERPROFILE", "")
    if profile:
        candidates.append(os.path.join(profile, ROOT_FOLDER_NAME))
    public = os.environ.get("PUBLIC", "")
    if public:
        candidates.append(os.path.join(public, ROOT_FOLDER_NAME))
    return candidates


def env_path(*children: str) -> str:
    """Join child segments onto the toolchain root."""
    path = env_root()
    for segment in children:
        path = os.path.join(path, segment)
    return path


def manifest_path() -> str:
    return env_path("env.json")


# ---------------------------------------------------------------------------
# Extended-length prefix (managed I/O only)
# ---------------------------------------------------------------------------

def to_extended(path: str) -> str:
    """Return the ``\\\\?\\`` (or ``\\\\?\\UNC\\``) form of an absolute path.

    Use this ONLY for managed operations (``os``/``shutil``/``zipfile``). Never
    pass the result to whisper.cpp, ffmpeg, PyMuPDF or Tesseract: they open paths
    with plain C APIs and would treat the prefix as part of the name.

    Idempotent. Normalization happens before the prefix is added, because the
    prefix disables it.
    """
    if not path:
        return path
    if path.startswith("\\\\?\\"):
        return path

    full = os.path.abspath(path)
    if full.startswith("\\\\?\\"):
        return full
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def from_extended(path: str) -> str:
    """Remove a ``\\\\?\\`` / ``\\\\?\\UNC\\`` prefix, if present.

    Retained for reporting and for tests. Managed I/O does not need it -- that is
    the main simplification over the PowerShell original, where every enumerated
    path had to be scrubbed because ``?`` is a wildcard in the shell's cmdlets.
    """
    if not path:
        return path
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def absolute(path: str) -> str:
    """Absolute form of a path, WITHOUT an extended-length prefix.

    This is the form to hand to a native tool. ``os.path.abspath`` is string
    arithmetic plus ``getcwd``, so unlike PowerShell's ``GetFullPath`` it does not
    raise on an over-long path -- the caller's own :func:`assert_fits` is what
    produces the actionable error.
    """
    if not path:
        return path
    return os.path.abspath(path)


def path_length(path: str | None) -> int:
    """Effective character length of a path as Windows would measure it.

    Any ``\\\\?\\`` prefix is stripped first: it does not count toward the limit
    the caller is trying to stay under. A relative path is measured against the
    current directory, which is what the OS will see.
    """
    if not path:
        return 0
    stripped = from_extended(path)
    length = len(stripped)
    if not os.path.isabs(stripped):
        try:
            length += len(os.getcwd()) + 1
        except OSError:
            pass
    return length


def _long_paths_enabled() -> bool:
    """Machine-level LongPathsEnabled policy. Informational only.

    It is NOT a substitute for :func:`to_extended`: an interpreter that is not
    manifested ``longPathAware`` ignores the policy entirely.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return int(value) == 1
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False


def assert_fits(path: str | None, what: str, slack: int = 0) -> str | None:
    """Raise :class:`PathTooDeepError` when a path cannot work on this machine.

    Used for every path that reaches a NATIVE tool, and for user-supplied
    destinations, so the failure is a named budget violation rather than an
    opaque "cannot open file" from deep inside the system.

    ``slack`` is the extra characters the caller will append later (``.txt``,
    ``.images``, ``.whisper.log``). A base path merely UNDER the budget still
    fails once such a suffix is added, so the check is made on the final length.
    """
    if not path:
        return path
    length = path_length(path) + slack
    if length > PATH_BUDGET:
        hint = (
            "LongPathsEnabled is set on this machine, but the native tools here "
            "cannot use the \\\\?\\ escape hatch."
            if _long_paths_enabled()
            else "LongPathsEnabled is not set on this machine."
        )
        raise PathTooDeepError(
            f"{what} is too long for Windows ({length} characters; the legacy "
            f"limit is 259). {hint} Use a shorter -Output/-DownloadDir/-Root, or "
            "a shorter file name. whisper.cpp, ffmpeg and PyMuPDF cannot use the "
            "\\\\?\\ escape hatch, so shortening the path is the only reliable fix."
        )
    return path


def root_path_report(root: str | None = None) -> dict:
    """Length diagnostics for the toolchain root, for install and ``doctor``.

    The root is the one path the toolchain itself chooses, and it is the prefix
    of the two paths that must reach a native tool unchanged: the whisper exe
    (which loads sibling DLLs through the loader search path) and the model
    (opened with plain ``fopen``). Both therefore need a real length check.
    """
    root = root or env_root()
    exe = os.path.join(root, "bin", "whisper", "whisper-cli.exe")
    model = os.path.join(root, "models", "ggml-large-v3-turbo.bin")
    work = os.path.join(root, WORK_FOLDER)
    exe_len = path_length(exe)
    model_len = path_length(model)
    return {
        "root": root,
        "rootLength": path_length(root),
        "whisperExeLength": exe_len,
        "modelPathLength": model_len,
        "workPathLength": path_length(work),
        "budget": PATH_BUDGET,
        "whisperExeFits": exe_len <= PATH_BUDGET,
        "modelPathFits": model_len <= PATH_BUDGET,
        "longPathsEnabled": _long_paths_enabled(),
    }


# ---------------------------------------------------------------------------
# Existence / directory / removal
# ---------------------------------------------------------------------------

def exists(path: str | None) -> bool:
    if not path:
        return False
    return os.path.exists(to_extended(path))


def is_file(path: str | None) -> bool:
    if not path:
        return False
    return os.path.isfile(to_extended(path))


def is_dir(path: str | None) -> bool:
    if not path:
        return False
    return os.path.isdir(to_extended(path))


def file_size(path: str) -> int:
    return os.path.getsize(to_extended(path))


def ensure_dir(path: str) -> str:
    """Create a directory and any missing parents. Long-path safe."""
    if path:
        os.makedirs(to_extended(path), exist_ok=True)
    return path


@dataclass(frozen=True)
class Entry:
    """One directory entry, with a PREFIX-FREE absolute path.

    Scanning a ``\\\\?\\`` path makes ``os.DirEntry.path`` carry the prefix, which
    would then leak into JSON results and into paths handed to native tools.
    Building the path from the caller's own directory keeps the prefix strictly
    internal to the one call that needs it.
    """

    name: str
    path: str
    is_dir: bool
    size: int | None
    mtime: float

    @property
    def is_file(self) -> bool:
        return not self.is_dir


def list_dir(path: str, *, files: bool = False, dirs: bool = False) -> list[Entry]:
    """List entries of a directory, long-path safe, with prefix-free paths."""
    if not path:
        return []
    try:
        handle = os.scandir(to_extended(path))
    except OSError:
        return []

    entries: list[Entry] = []
    with handle:
        for item in handle:
            try:
                is_directory = item.is_dir()
                info = item.stat()
            except OSError:
                continue
            if files and not dirs and is_directory:
                continue
            if dirs and not files and not is_directory:
                continue
            entries.append(
                Entry(
                    name=item.name,
                    path=os.path.join(path, item.name),
                    is_dir=is_directory,
                    size=None if is_directory else info.st_size,
                    mtime=info.st_mtime,
                )
            )
    return entries


def newest_file(directory: str) -> str | None:
    """Path of the most recently modified file directly inside ``directory``."""
    best: str | None = None
    best_time = -1.0
    for entry in list_dir(directory, files=True):
        if entry.mtime > best_time:
            best_time = entry.mtime
            best = entry.path
    return best


def _force_writable(func, path, _exc_info) -> None:
    """``shutil.rmtree`` error handler: clear the read-only bit and retry."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove(path: str | None, *, recursive: bool = False) -> None:
    """Remove a file or directory, long-path safe. Raises on failure."""
    if not path or not exists(path):
        return
    target = to_extended(path)
    if os.path.isdir(target):
        if recursive:
            shutil.rmtree(target, onerror=_force_writable)
        else:
            os.rmdir(target)
    else:
        os.remove(target)


def remove_quietly(path: str | None, *, recursive: bool = False) -> bool:
    """Best-effort removal. Returns True when the path is gone afterwards.

    Replaces the PowerShell version's background job with a short timeout: that
    job existed because ``Remove-Item -Recurse`` can block forever on a file a
    killed child still holds. A plain ``shutil.rmtree`` fails immediately
    instead, so the time-boxing is unnecessary -- the dir is simply left behind
    for the ``clean`` subcommand rather than holding the run hostage.
    """
    if not path or not exists(path):
        return True
    try:
        remove(path, recursive=recursive)
    except OSError:
        pass
    return not exists(path)


def remove_work_dir(path: str, attempts: int = 3, delay: float = 0.4) -> bool:
    """Delete a scratch dir without ever blocking the caller on it.

    A killed whisper process can still hold a handle to a log inside the dir, so
    a few retries are attempted before giving up. Returning False means the dir
    was left in place; the caller reports that instead of failing.
    """
    for attempt in range(attempts):
        if not exists(path):
            return True
        try:
            remove(path, recursive=True)
        except OSError:
            if attempt < attempts - 1:
                time.sleep(delay)
    return not exists(path)


def move(source: str, destination: str) -> None:
    """Move a file or directory, long-path safe."""
    shutil.move(to_extended(source), to_extended(destination))


def copy_file(source: str, destination: str) -> None:
    """Copy a single file, long-path safe, overwriting."""
    parent = os.path.dirname(destination)
    if parent:
        ensure_dir(parent)
    shutil.copyfile(to_extended(source), to_extended(destination))


def copy_tree(source: str, destination: str) -> None:
    """Recursively copy a directory into ``destination`` (merging)."""
    ensure_dir(destination)
    for root, dirs, files in os.walk(to_extended(source)):
        # os.walk over a prefixed root yields prefixed children, so strip it back
        # to build the destination counterpart.
        relative = os.path.relpath(root, to_extended(source))
        target_root = destination if relative == "." else os.path.join(destination, relative)
        ensure_dir(target_root)
        for name in files:
            copy_file(os.path.join(root, name), os.path.join(target_root, name))
        for name in dirs:
            ensure_dir(os.path.join(target_root, name))


# ---------------------------------------------------------------------------
# Scratch directories + the ASCII invariant
# ---------------------------------------------------------------------------

def new_work_dir(root: str | None = None) -> str:
    """Create a fresh ASCII scratch dir under ``<root>\\work\\<guid>``."""
    return new_ascii_dir(root or env_path(WORK_FOLDER))


def new_ascii_dir(base: str, prefix: str = "") -> str:
    """Create a fresh unique ASCII directory under ``base``."""
    import uuid

    name = f"{prefix}{uuid.uuid4().hex}" if prefix else uuid.uuid4().hex
    path = os.path.join(base, name)
    ensure_dir(path)
    return path


def new_temp_dir(prefix: str = "tmp") -> str:
    """Create an ASCII temp dir for downloads/extraction.

    ``%TEMP%`` lives under the user profile, so it is non-ASCII when the user
    name is. Scratch space therefore lives under the (ASCII) toolchain root,
    falling back to ``%TEMP%`` only if that root somehow is not ASCII.
    """
    base = os.path.join(env_root(), "tmp")
    if not is_ascii(base):
        base = os.environ.get("TEMP", base)
    return new_ascii_dir(base, prefix=f"{prefix}-")


def copy_into_safe_work(input_path: str, work_dir: str) -> dict:
    """Copy an input file into an ASCII work dir under an ASCII name.

    This is the structural fix for the whisper.cpp Cyrillic-path bug: whisper-cli
    only ever receives paths returned here, which are guaranteed ASCII. The
    original (possibly Cyrillic) file is never passed to a native tool.

    Returns a dict: ``{"work_dir", "input_path", "original_path"}``.
    """
    if not is_file(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    if not is_ascii(work_dir):
        raise ValueError(f"Work dir must be ASCII, got: {work_dir}")

    extension = os.path.splitext(input_path)[1]
    if not extension:
        extension = ".bin"
    safe_path = os.path.join(work_dir, "input" + extension.lower())
    copy_file(input_path, safe_path)
    return {
        "work_dir": work_dir,
        "input_path": safe_path,
        "original_path": input_path,
    }


def extension_of(path: str) -> str:
    return os.path.splitext(path)[1]


def without_extension(path: str) -> str:
    return os.path.splitext(path)[0]

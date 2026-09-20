"""Process and CLI-contract helpers: logging, the JSON result line, subprocesses.

The contract every caller depends on:

* STDOUT carries exactly ONE line: the JSON result. It is the only thing an
  agent parses.
* STDERR carries all human-readable progress, warnings and errors.
* The exit code is 0 on success and 1 on failure.

That split is why :func:`log` never touches stdout and why native tool output is
always captured rather than inherited.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .errors import StepFailedError, ZoombieError

_LEVEL_PREFIX = {
    "warn": "WARN ",
    "error": "ERROR",
    "step": "==>",
    "info": "    ",
}


def log(message: str, level: str = "info") -> None:
    """Write one human-readable progress line to stderr."""
    prefix = _LEVEL_PREFIX.get(level, "    ")
    sys.stderr.write(f"{prefix} {message}\n")
    sys.stderr.flush()


def write_result(
    action: str,
    ok: bool = True,
    data: dict | None = None,
    error: str | None = None,
) -> None:
    """Emit the single machine-readable JSON result line on stdout.

    Key order is fixed (ok, action, error, data, timestamp) so the output is
    byte-comparable with the PowerShell implementation during the port.
    """
    payload = {
        "ok": bool(ok),
        "action": action,
        "error": error,
        "data": data if data is not None else {},
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def cpu_threads() -> int | None:
    """Logical processor count for the CPU path, or None to let whisper decide."""
    try:
        count = os.cpu_count()
    except (NotImplementedError, ValueError):
        return None
    return count if count and count > 0 else None


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a child process, forcing UTF-8.

    The PowerShell original toggled ``PYTHONUTF8`` around every native call; here
    the variable is simply always set for children, and our own reads/writes pass
    an explicit encoding anyway.
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def which(name: str) -> str | None:
    """Absolute path of an executable, or None."""
    return shutil.which(name)


def refresh_path_from_registry() -> str:
    """Add the Machine + User registry PATH entries to this process's PATH.

    A fresh install (winget, MSI) updates the registry PATH but NOT the
    environment of an already-running process. Refreshing turns a stale-PATH
    "tool not found" false negative into a correct positive.

    Entries are MERGED with the existing PATH rather than replacing it. The
    system directories (``System32``, ``Windows``) are injected by the OS and are
    not stored in either registry value, so replacing would DROP them and break
    tools that live there -- ``nvidia-smi.exe`` being the one this toolchain
    depends on most.
    """
    if sys.platform != "win32":
        return os.environ.get("PATH", "")
    try:
        import winreg
    except ImportError:
        return os.environ.get("PATH", "")

    registry_parts: list[str] = []
    for root, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ):
        try:
            key = winreg.OpenKey(root, subkey)
            try:
                value, _ = winreg.QueryValueEx(key, "Path")
                if value:
                    registry_parts.append(str(value))
            finally:
                winreg.CloseKey(key)
        except OSError:
            continue

    existing = os.environ.get("PATH", "")
    combined = ";".join([*registry_parts, existing]) if existing else ";".join(registry_parts)

    seen: set[str] = set()
    unique: list[str] = []
    for entry in combined.split(";"):
        stripped = entry.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(stripped)

    os.environ["PATH"] = ";".join(unique)
    return os.environ["PATH"]


def decode_console(data: bytes) -> str:
    """Decode captured native output.

    Native tools on Windows may emit the console code page, UTF-16, or UTF-8
    depending on the build. UTF-8 with ``replace`` is the pragmatic default: it
    never raises, and a mis-decoded byte only harms a diagnostic line.
    """
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


def run(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    merge_stderr: bool = False,
    quiet: bool = True,
) -> subprocess.CompletedProcess:
    """Run a native tool, capturing output.

    ``quiet`` keeps stdout and stderr off our streams; ``merge_stderr`` combines
    them, which is what the whisper ``--help`` probe wants (it writes its backend
    banner to stderr, and reading the wrong stream is exactly the bug the probe
    exists to avoid).
    """
    stderr_target = subprocess.STDOUT if merge_stderr else subprocess.PIPE
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=env or child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr_target,
        timeout=timeout,
        check=False,
    )


def run_text(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[int, str]:
    """Run a tool and return ``(exit_code, text)`` with stderr merged.

    Never raises on a non-zero exit: callers decide what a failure means. An
    unlaunchable tool yields ``(-1, "")``.
    """
    try:
        completed = run(argv, cwd=cwd, env=env, timeout=timeout, merge_stderr=True)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return completed.returncode, decode_console(completed.stdout or b"")


def run_checked(
    argv: Sequence[str],
    *,
    what: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> None:
    """Run a tool and raise :class:`StepFailedError` if it exits non-zero."""
    try:
        completed = run(argv, cwd=cwd, env=env, timeout=timeout)
    except OSError as exc:
        raise StepFailedError(f"{what} could not be launched: {exc}") from exc
    if completed.returncode != 0:
        detail = decode_console(completed.stderr or b"").strip()
        suffix = f": {detail.splitlines()[-1]}" if detail else ""
        raise StepFailedError(f"{what} failed (exit {completed.returncode}){suffix}")

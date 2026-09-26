"""Register the zoombie MCP server in the client's own MCP settings file.

The problem this module solves is the same one :mod:`zoombie.lib.modes` solves
for custom modes: **shared ownership**. ``mcp_settings.json`` is a document the
client owns and the user edits by hand -- it may hold other servers -- so we must
merge our entry without touching anything else. Unlike ``custom_modes.yaml`` the
file is JSON, so the merge is a parse-edit-reserialize on the *servers map only*:
every foreign key is carried through verbatim, and only ``mcpServers.zoombie`` is
replaced.

Why the entry points at the Python interpreter, not at a ``.cmd`` shim
---------------------------------------------------------------------

An MCP stdio server is spawned by the client as ``command`` + ``args``. The CLI
launcher ``zoombie.cmd`` runs ``python -m zoombie`` and cannot reach the facade
submodule, so the entry names the interpreter directly and passes ``-m
zoombie.mcp``. The interpreter is the toolchain's own resolved Python
(:func:`zoombie.lib.tools.find_python`), not a hard-coded ``<root>\\python`` --
there is no such directory (see ``feedback/subtask-G-mcp.md`` §11).

``PYTHONPATH`` is set to the deployed package directory (``<...>\\zoombie-env\\bin\\
zoombie``), the same value the ``.cmd`` launcher exports, so ``-m zoombie.mcp``
resolves from any working directory. ``PYTHONUTF8=1`` forces the UTF-8 mode the
facade relies on for the Cyrillic paths it round-trips.
"""

from __future__ import annotations

import json
import os

from . import io as io_mod, paths, process, tools

# The key our server is registered under. Fixed, so a re-run replaces our entry
# and never appends a second one.
SERVER_NAME = "zoombie"

# The MCP submodule the client spawns as ``-m``.
SERVER_MODULE = "zoombie.mcp"

# Environment override, mirroring ZOOMBIE_MODES_PATH: lets a test (or a
# non-standard install) redirect the target without touching the real profile.
TARGET_ENV_VAR = "ZOOMBIE_MCP_SETTINGS_PATH"

# The client's global settings folder, relative to %APPDATA%. Mirrors
# ``modes.EXTENSION_ID``; the extension id is fixed by the product, so the file
# has one known location.
EXTENSION_ID = "zoocodeorganization.zoo-code"

# The file name inside that folder. The extension also recognises the older
# ``cline_mcp_settings.json`` and migrates it forward; we always write the
# current name.
SETTINGS_FILE = "mcp_settings.json"

# Stated timeout for a long stage. The client's own default is 60 s (measured in
# the extension bundle); a slides/preprocess or a pipeline call can exceed that, so
# the entry raises it while the ``start``/``status``/``result`` lifecycle remains
# the intended path for the longest ones.
ENTRY_TIMEOUT_SECONDS = 600


def global_settings_path() -> str:
    """The global ``mcp_settings.json`` path.

    ``ZOOMBIE_MCP_SETTINGS_PATH`` wins so a test (or an unusual install) can
    redirect it without touching the real user profile.
    """
    override = os.environ.get(TARGET_ENV_VAR, "").strip()
    if override:
        return override
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        raise RuntimeError(
            "APPDATA is not set, so the global mcp_settings.json cannot be located"
        )
    return os.path.join(
        appdata, "Code", "User", "globalStorage", EXTENSION_ID, "settings", SETTINGS_FILE
    )


def package_dir() -> str:
    """The DEPLOYED directory holding the ``zoombie`` package (``...\\bin\\zoombie``).

    This is what the client must put on ``PYTHONPATH`` for ``-m zoombie.mcp`` to
    resolve: the package sits directly under it. It is the deployed bin dir the
    ``zoombie.cmd`` launcher is written into (``components.deploy_cli``), NOT the
    source checkout the installer happens to run from. The bootstrap runs from a
    temp checkout that is deleted afterwards, so pointing the client there would
    break the server as soon as setup finished.
    """
    return paths.env_path("bin", "zoombie")


def interpreter() -> str | None:
    """The Python the client should spawn, or ``None`` when none is resolved.

    Resolved the way every other native dependency is, never by a guessed path.
    """
    return tools.find_python()


def server_entry(interpreter_path: str | None = None, package: str | None = None) -> dict:
    """The stdio server definition for our MCP facade.

    Shape is the client's: ``command`` + ``args`` + ``env`` for a ``stdio``
    server, plus the optional ``disabled``/``timeout`` fields. A ``.cmd`` shim is
    deliberately NOT used: the client spawns the interpreter directly.
    """
    python = interpreter_path or interpreter()
    if not python:
        raise RuntimeError(
            "No Python interpreter resolved, so the MCP server entry cannot be "
            "written. Install Python (winget install Python.Python.3.12) and re-run."
        )
    package_path = package or package_dir()
    return {
        "command": python,
        "args": ["-m", SERVER_MODULE],
        "env": {
            "PYTHONPATH": package_path,
            "PYTHONUTF8": "1",
        },
        "disabled": False,
        "timeout": ENTRY_TIMEOUT_SECONDS,
    }


def _empty_document() -> dict:
    """The minimal valid settings document: an object with an ``mcpServers`` map."""
    return {"mcpServers": {}}


def merge(
    existing_text: str,
    entry: dict,
    name: str = SERVER_NAME,
) -> tuple[str, str]:
    """Merge ``entry`` under ``existing_text``'s ``mcpServers[name]``.

    Returns ``(text, action)`` where action is ``"created"``, ``"updated"`` or
    ``"up to date"``. Foreign servers and every other top-level key are preserved.

    An empty/whitespace-only document is treated as "create"; a document that is
    not JSON, or whose ``mcpServers`` is not an object, is REFUSED rather than
    silently repaired -- overwriting a file we do not understand could destroy a
    user's other servers.
    """
    if not (existing_text or "").strip():
        document = _empty_document()
        action = "created"
    else:
        try:
            document = json.loads(existing_text)
        except ValueError as exc:
            raise RuntimeError(
                f"Refusing to modify a file that is not valid JSON: {exc}. Fix it by "
                f"hand, or point {TARGET_ENV_VAR} at the correct file."
            ) from exc
        if not isinstance(document, dict):
            raise RuntimeError(
                "Refusing to modify an mcp_settings file whose top level is not a "
                "JSON object."
            )
        servers = document.get("mcpServers")
        if servers is None:
            servers = {}
            document["mcpServers"] = servers
        elif not isinstance(servers, dict):
            raise RuntimeError(
                "Refusing to modify an mcp_settings file whose 'mcpServers' is not "
                "an object."
            )
        action = "up to date" if servers.get(name) == entry else (
            "updated" if name in servers else "created"
        )

    document.setdefault("mcpServers", {})[name] = entry
    # One trailing newline, indent 2: the same discipline env.json and every
    # sidecar use, so the file diffs cleanly and a re-run is byte-identical.
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    return text, action


def deploy(
    target_path: str | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    interpreter_path: str | None = None,
    package: str | None = None,
) -> dict:
    """Write our server entry into ``target_path``.

    ``dry_run`` computes the action and the resulting text but writes nothing.
    ``force`` rewrites even when the entry already matches. Returns
    ``{"name", "action", "path", "command", "pythonFound"}``.
    """
    target = target_path or global_settings_path()
    python = interpreter_path or interpreter()
    entry = server_entry(python, package)

    existing = io_mod.read_text(target) if paths.is_file(target) else ""
    text, action = merge(existing, entry)
    should_write = action != "up to date" or force
    if should_write and not dry_run:
        io_mod.write_text(target, text)
    return {
        "name": SERVER_NAME,
        "action": action,
        "path": target,
        "command": entry["command"],
        "package": entry["env"]["PYTHONPATH"],
        "pythonFound": bool(python),
    }


def is_registered(target_path: str | None = None, name: str = SERVER_NAME) -> bool:
    """Whether our server entry is present in the (GLOBAL) target file.

    Read straight from the file, because ``deploy`` returns the action it *took*,
    not the state that is now on disk. The installer asserts this after writing, so
    "the MCP server is installed globally" is a checked fact rather than an
    inference from a successful-looking log line.
    """
    target = target_path or global_settings_path()
    if not paths.is_file(target):
        return False
    document = io_mod.read_json(target) or {}
    servers = document.get("mcpServers")
    return isinstance(servers, dict) and isinstance(servers.get(name), dict)


def probe_server(
    *,
    interpreter_path: str | None = None,
    package: str | None = None,
    timeout: float = 60,
) -> dict:
    """Spawn the REGISTERED command the way the client would and report the verdict.

    ``registered: true`` proves only that the entry exists in the settings file; it
    says nothing about whether ``<command> -m zoombie.mcp`` actually starts. A clean
    install was therefore reported on a machine where the server never came up (see
    ``feedback/report (1).md``). This runs EXACTLY the registered argv with the
    registered ``PYTHONPATH``/``PYTHONUTF8`` and records whether it answered, so the
    manifest gains a checked sibling to :func:`is_registered`.

    Best effort, never raises: a failure is a reportable fact, not a setup failure.
    """
    python = interpreter_path or interpreter()
    if not python:
        return {"ok": False, "version": None, "error": "no Python interpreter resolved"}
    env = process.child_env()
    env["PYTHONPATH"] = package or package_dir()
    env["PYTHONUTF8"] = "1"
    # The real entrypoint, not an import shim: this is what the client spawns.
    argv = [python, "-m", SERVER_MODULE, "--version"]
    code, text = process.run_text(argv, env=env, timeout=timeout)
    version = None
    if code == 0 and (text or "").strip():
        version = text.strip().splitlines()[-1].strip()
    return {
        "ok": code == 0 and bool(version),
        "version": version,
        "error": None if code == 0 else f"exit {code}",
    }


def deploy_safe(
    target_path: str | None = None,
    *,
    interpreter_path: str | None = None,
    package: str | None = None,
) -> dict:
    """Install-time wrapper: always writes, never raises.

    The installer calls this so a missing Python or an unreadable settings file is
    reported on the manifest rather than aborting the whole setup -- the MCP server
    is a convenience layer over a CLI that is already installed and verified.
    """
    try:
        record = deploy(
            target_path, dry_run=False, interpreter_path=interpreter_path, package=package
        )
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to setup
        record = {
            "name": SERVER_NAME,
            "action": "failed",
            "path": target_path or _safe_global_path(),
            "command": None,
            "package": None,
            "pythonFound": bool(interpreter_path or interpreter()),
            "note": f"{type(exc).__name__}: {exc}",
        }
    # Confirmed against the file, not the action map: the entry being present in the
    # GLOBAL settings is the property the caller cares about.
    record["registered"] = is_registered(record.get("path"))
    return record


def _safe_global_path() -> str | None:
    """``global_settings_path()`` without raising, for a failure record."""
    try:
        return global_settings_path()
    except Exception:  # noqa: BLE001
        return None


def report() -> dict:
    """A read-only description of what we would write, for ``doctor``/``mcp -Check``."""
    python = interpreter()
    target = _safe_global_path()
    installed = None
    if target and paths.is_file(target):
        document = io_mod.read_json(target) or {}
        servers = document.get("mcpServers")
        if isinstance(servers, dict):
            installed = servers.get(SERVER_NAME)
    return {
        "server": SERVER_NAME,
        "module": SERVER_MODULE,
        "scope": "global",
        "settingsPath": target,
        "python": python,
        "package": package_dir(),
        "installed": installed,
        "registered": bool(installed),
    }

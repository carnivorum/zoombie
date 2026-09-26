"""``python -m zoombie.mcp`` -- a thin MCP facade over the SAME command modules.

Plan §11 (``plans/zoombie-image-pipeline-rework.md``). This is **our own process**,
not a module injected into another product: an MCP client declares a command and
args, spawns it, and speaks **JSON-RPC over stdio**.

Why a facade at all, and why *in process* is the decisive part
-------------------------------------------------------------

The result the CLI prints on stdout is already transport-agnostic -- one line of
``{ok, action, error, data, timestamp}`` -- so the facade does not re-derive a
contract. What it buys is three things, none of which the process boundary gave:

* **No ``cmd.exe`` and no console code page.** :func:`call_tool` imports
  ``zoombie.commands.<name>`` and calls its ``run(args)`` directly in this
  interpreter. Nothing is spawned, so a Cyrillic ``-Output`` never round-trips
  through a console and the ``@file`` staging convention is not needed. The
  interpreter is resolved the way every other native dependency is
  (:func:`zoombie.lib.tools.find_python` / ``env.json``), not by a hard-coded
  ``<root>\\python`` path -- there is no such directory (plan §11 is wrong about
  that; see the receipt ``feedback/subtask-G-mcp.md``).
* **The server decides the SHAPE of a result.** :func:`shape_image` applies the
  attach cap that already lives in :mod:`zoombie.lib.next` (it does NOT invent a
  second one), so a 96-frame deck becomes 8 image blocks plus a paginated
  remainder rather than 96 blocks that overflow the client.
* **Long stages get ``start`` / ``status`` / ``result``** instead of one blocking
  call, so a slides/preprocess run can be polled.

What this module deliberately does **not** do
---------------------------------------------

* It does not change the envelope's shape (plan §14). Every tool result and
  ``tools/call`` error carries :func:`zoombie.lib.process.result_payload`, i.e.
  the *same* five keys the CLI writes -- single-sourced, not re-declared here.
* It does not re-implement capping/pagination (:mod:`zoombie.lib.next`) or the
  scratch ownership rule (:mod:`zoombie.lib.scratch`). ``zoombie.facets.status``
  is the only place the facade reaches into scratch, and it reuses
  :func:`zoombie.lib.scratch.inventory`.
* It does not make stdout anything other than the protocol. :func:`serve` wraps
  any tool call in :class:`_suppress_stdout`, so even the one in-tree stdout
  writer (:func:`zoombie.lib.process.write_result`) cannot corrupt the stream.
  All human progress goes to stderr via :func:`zoombie.lib.process.log`.

Framing
-------

One JSON object per line (the "one line" the JSON contract already speaks), NOT
the spec's ``Content-Length`` envelope. The choice is explicit and tested
round-trip (:func:`zoombie.mcp.frame` / :func:`zoombie.mcp.read_message`); a
bearer of the Content-Length framing is a later, additive change.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

from . import SKILL_VERSION
from .item import paths as item_paths
from .lib import next as next_mod, paths, process, reading, scratch, tools

# --- protocol constants ----------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "zoombie"

JSONRPC_VERSION = "2.0"

# JSON-RPC / MCP error codes we emit. The range follows the spec.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TOOL_NOT_FOUND = -32000
TOOL_FAILED = -32001
JOB_NOT_FOUND = -32002

# Every verb the CLI exposes is CALLABLE as a tool, but not every one is
# ADVERTISED. ``TOOL_COMMANDS`` is the acceptance set: ``tools/call`` honours any
# name in it. ``ADVERTISED_TOOLS`` is what ``tools/list`` shows the model -- the
# small set a user might ask for directly. The rest are reached by the
# ``summarize`` flow (in process, never through this facade) or are one-time
# setup/diagnostics, so listing them only bloats the model's tool menu.
#
# Kept explicit rather than derived from the parser: ``_dispatch`` is the single
# source of truth, and a test asserts every verb is at least callable -- so a new
# subcommand that is not mirrored here is a failure.
TOOL_COMMANDS: dict[str, str] = {
    "doctor": "doctor",
    "clean": "clean",
    "download": "download",
    "extract": "extract",
    "unpack": "unpack",
    "transcribe": "transcribe",
    "pipeline": "pipeline",
    "readpdf": "readpdf",
    "readimages": "readimages",
    "slides": "slides",
    "summarize": "summarize",
    "postprocess": "postprocess",
    "verify": "verify",
    "items": "items",
    "modes": "modes",
    "mcp": "mcp",
}

# The tools ``tools/list`` advertises. The agent-facing surface: the front door,
# the three "fetch/convert a file" wrappers, and the read-only orientation verbs.
# A tool NOT listed here is still callable by name -- it is simply not offered,
# because it duplicates the summarize flow, is a one-time setup action, or is a
# maintenance verb the model should not reach for.
#
# Order is the order :func:`tool_schemas` emits, so the menu reads top-down.
ADVERTISED_TOOLS: tuple[str, ...] = (
    "summarize",   # the front door: one document from any source
    "download",    # save a link (a file, not a document)
    "extract",     # pull the audio out of a video
    "readpdf",     # text from a PDF, or OCR a scan
    "readimages",  # read an image or a folder of images
    "unpack",      # open any archive 7-Zip can read (and an NSIS installer)
    "items",       # read-only: what summaries exist here, and how they are named
    "doctor",      # read-only: is the toolchain usable
)

# The stages that may block for a long time are the ones ``start``/``status``/
# ``result`` exists for. A short, read-only verb is executed inline by
# ``tools/call`` so a client does not have to poll to learn "no". ``summarize`` is
# LONG because its source step can download AND transcribe in one call.
LONG_TOOLS = frozenset(
    {"pipeline", "transcribe", "download", "slides", "readpdf", "extract", "unpack",
     "summarize"}
)

# How many attachable images a single ``tools/call`` may return as image blocks.
# This is the plan-§9 cap, referenced -- NOT re-declared -- so raising the one
# constant raises both transports.
IMAGE_ATTACH_CAP = next_mod.DEFAULT_ATTACH_CAP

# Extra CLI options a client may pass beyond the tool's own JSON schema. The
# schema advertises the important ones; this whitelist is how a raw ``-Flag``
# reaches argparse as ``--flag`` without silently inventing a reverse mapping for
# a flag whose destination differs from its name (``-Source`` -> ``source`` is
# handled specially, and ``-CleanScratch`` -> ``clean_scratch`` by the generic
# rule).
KNOWN_OPTIONS: dict[str, dict] = {
    "unpack": {"strip": "int", "include": "list", "check": "bool"},
    "transcribe": {"from_time": "str", "to_time": "str", "language": "str"},
    "pipeline": {"from_time": "str", "to_time": "str", "language": "str", "download_dir": "str"},
    # ``ocr`` is a mode switch and ``vision`` maps to the ``--vision <dir>`` flag
    # (whose argparse destination is ``vision_dir``): both are the scan-escalation
    # path, and neither is optional in the sense the whitelist used to imply.
    "readpdf": {
        "pages": "str", "lang": "str", "dpi": "int", "min_px": "int", "min_pt": "int",
        "ocr": "bool", "vision": "str", "images": "bool", "images_only": "bool",
        "image_dir": "str",
    },
    "readimages": {"lang": "str", "ocr": "bool", "image_dir": "str"},
    "slides": {
        "times": "str", "lang": "str", "scale": "int", "min_px": "int",
        "min_frame_bytes": "int", "min_text_chars": "int", "min_slide_sec": "float",
        "sample_rate": "float", "sample_interval_sec": "float", "diff_threshold": "float",
        "hash_distance": "int", "global_dedup": "bool",
        # The agent's final keep/drop. Handles are frame ids (``f005``) or their
        # timestamps -- NEVER a path: the agent decides, the tool moves the files.
        "keep": "str", "drop": "str", "keep_file": "str", "drop_file": "str",
    },
    "postprocess": {
        "md": "str", "dir": "str", "recurse": "bool", "srt": "str", "image_dir": "str",
        "report": "str", "apply": "bool",
    },
    "verify": {"dir": "str", "recurse": "bool", "json": "bool"},
    "summarize": {
        "step": "str", "run": "str", "name": "str", "slides": "str", "times": "str",
        "title": "str", "summary_text": "str", "criticism": "str", "sections": "str",
        # How an EXISTING local source is placed: keep|copy|move|none (a value the
        # agent carries back from the user's answer, not a boolean toggle).
        # ``confirm_move`` is the acknowledgement -Media move RELOCATES the user's
        # own file: move is refused without it.
        "media": "str", "confirm_move": "bool", "language": "str",
        "keep": "str", "drop": "str",
    },
    "items": {"root": "str", "depth": "int", "recurse": "bool", "json": "bool", "title": "str", "date": "str"},
    "modes": {"target": "str", "check": "bool", "apply": "bool"},
    "mcp": {"target": "str", "check": "bool", "apply": "bool"},
    "clean": {"work_root": "str", "clean_scratch": "bool"},
    "download": {"download_dir": "str", "format": "str", "audio_only": "bool", "name": "str"},
    "extract": {"format": "str"},
}

# Keys whose CLI flag does NOT follow the ``apply -> --apply`` underscore-to-dash
# rule. Kept explicit because argparse's own spelling is irregular here
# (``-From``/``-To``), and the generic rule would fabricate ``--from-time`` -- a
# flag the parser rejects. The reverse direction matters too: a whitelist entry
# that maps to no real flag is caught by the parser-derived test, so this table is
# the single place an irregular spelling has to be declared.
OPTION_FLAGS: dict[str, dict[str, str]] = {
    "transcribe": {"from_time": "from", "to_time": "to"},
    "pipeline": {"from_time": "from", "to_time": "to"},
    # ``-SummaryText`` is the block-3 text; its argparse destination is
    # ``summary_text``, which the underscore-to-dash rule would wrongly turn into
    # ``--summary-text``. The other summarize keys follow the generic rule.
    "summarize": {"summary_text": "summary-text"},
}


def _flag_for(command: str, key: str) -> str:
    """The ``--flag`` token for a whitelisted key, honouring :data:`OPTION_FLAGS`."""
    name = OPTION_FLAGS.get(command, {}).get(key, key.replace("_", "-"))
    return "--" + name


# --- framing / JSON-RPC plumbing -------------------------------------------


def frame(message: dict) -> str:
    """Serialise one JSON-RPC message as a single line (the framing we speak).

    ``ensure_ascii=False`` so a Cyrillic path survives as itself rather than as
    ``\\uXXXX`` escapes; the line is UTF-8 because the interpreter is forced to
    UTF-8 (see :func:`zoombie.lib.process.child_env` and :func:`serve`).
    """
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"


def read_message(line: str) -> dict:
    """Parse one request line into a JSON-RPC object.

    Raises :class:`ValueError` for anything that is not a single JSON object, so
    the caller can answer with the spec's parse/invalid-request error rather than
    crashing the server.
    """
    text = (line or "").strip()
    if not text:
        raise ValueError("empty line")
    try:
        message = json.loads(text)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ValueError("a JSON-RPC message must be an object")
    return message


def result(request_id, payload: dict) -> dict:
    """A JSON-RPC success response carrying any payload."""
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": payload}


def failure(request_id, code: int, message: str, data: dict | None = None) -> dict:
    """A JSON-RPC error response. The shape is the spec's, never our envelope."""
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _tool_error(request_id, code: int, message: str, data: dict | None = None) -> dict:
    """A ``tools/call`` failure expressed the way plan §11 requires.

    An MCP tool that fails should not be a JSON-RPC transport error -- the client
    asked a valid question and the *tool* said no -- so the five-key envelope
    (plan §14, unchanged) rides inside ``result.isError`` with the same JSON-RPC
    error code carried in ``data.jsonrpcCode`` for a programmatic consumer. A
    genuinely malformed request is still answered with a top-level error (see
    :func:`dispatch`), never this.
    """
    envelope = process.result_payload("tools/call", ok=False, error=message)
    if data is not None:
        envelope["data"] = {**envelope["data"], **data}
    envelope["data"].setdefault("jsonrpcCode", code)
    return result(
        request_id,
        {"content": [{"type": "text", "text": message}], "isError": True, "data": envelope},
    )


# --- stdout purity ----------------------------------------------------------


class _suppress_stdout:
    """Redirect anything written to stdout into a throwaway buffer.

    **stdout is the transport.** Any ``print``, banner or stray result line would
    land in the middle of the JSON-RPC stream and corrupt it, so every tool call
    runs under this context manager. It is the structural half of the guarantee;
    the other half is that :func:`zoombie.lib.process.log` only ever writes to
    stderr. The two together mean stdout carries protocol messages and nothing
    else, which is asserted live in ``tests/test_mcp.py``.

    A tool that legitimately writes a *result* line (:func:`process.write_result`)
    is therefore muted -- and that is correct here: the facade composes the
    envelope from the command's returned :class:`~zoombie.cli.Outcome`, it does
    not scrape a printed line.
    """

    def __enter__(self) -> io.StringIO:
        self._buffer = io.StringIO()
        self._redirect = redirect_stdout(self._buffer)
        self._redirect.__enter__()
        return self._buffer

    def __exit__(self, *exc) -> None:
        self._redirect.__exit__(*exc)


# --- argument building (in process, never a shell) -------------------------


def _coerce(kind: str, value):
    """Coerce a JSON value to the Python type argparse would produce."""
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if kind == "list":
        return list(value) if isinstance(value, (list, tuple)) else [value]
    return value


def _option_flags(command: str, arguments: dict) -> list[str]:
    """Turn extra JSON keys into ``--flag value`` tokens for this command.

    Only a key on the command's :data:`KNOWN_OPTIONS` whitelist is accepted, so
    an unknown key is a clean ``INVALID_PARAMS`` rather than a silent no-op -- a
    facade that dropped a misspelled flag would make a wrong request look
    successful.
    """
    known = KNOWN_OPTIONS.get(command, {})
    tokens: list[str] = []
    for key, value in (arguments or {}).items():
        if key in ("source", "output", "dry_run", "force", "attach_limit", "keep_scratch", "keep_work", "no_gpu", "threads", "model"):
            continue
        if key not in known:
            raise ValueError(f"unknown argument for {command}: {key}")
        kind = known[key]
        if kind == "bool":
            if _coerce("bool", value):
                tokens.append(_flag_for(command, key))
            continue
        if kind == "list":
            items = _coerce("list", value)
            if not isinstance(items, list):
                items = [items]
            for item in items:
                tokens.append(_flag_for(command, key))
                tokens.append(str(item))
            continue
        tokens.append(_flag_for(command, key))
        tokens.append(str(_coerce(kind, value)))
    return tokens


def build_argv(command: str, arguments: dict | None = None) -> list[str]:
    """Map a tool's JSON arguments to the CLI's own argv.

    The mapping is deliberately shallow: it does NOT reverse-engineer argparse
    flag names for the common options, it uses the ones both spellings share
    (``--source``/``--output`` are declared by ``_add_source_output``), so a
    change to the parser's flag spelling is caught by a test rather than silently
    producing a wrong call.
    """
    arguments = dict(arguments or {})
    argv: list[str] = [command]

    source = arguments.pop("source", None)
    output = arguments.pop("output", None)
    if source is not None:
        argv += ["--source", str(source)]
    if output is not None:
        argv += ["--output", str(output)]

    if _coerce("bool", arguments.pop("dry_run", False)):
        argv.append("--dry-run")
    if _coerce("bool", arguments.pop("force", False)):
        argv.append("--force")
    if _coerce("bool", arguments.pop("keep_scratch", False)):
        argv.append("--keep-scratch")
    if _coerce("bool", arguments.pop("keep_work", False)):
        argv.append("--keep-work")
    if _coerce("bool", arguments.pop("no_gpu", False)):
        argv.append("--no-gpu")
    if _coerce("bool", arguments.pop("strict_gpu", False)):
        argv.append("--strict-gpu")
    if _coerce("bool", arguments.pop("allow_cpu_fallback", False)):
        argv.append("--allow-cpu-fallback")

    limit = arguments.pop("attach_limit", None)
    if limit is not None:
        argv += ["--attach-limit", str(int(limit))]
    threads = arguments.pop("threads", None)
    if threads:
        argv += ["--threads", str(int(threads))]
    model = arguments.pop("model", None)
    if model:
        argv += ["--model", str(model)]

    argv += _option_flags(command, arguments)

    # The operator's override wins; otherwise the cap is the plan-§9 constant.
    if limit is None and command in ("slides", "readimages", "readpdf", "postprocess"):
        argv += ["--attach-limit", str(IMAGE_ATTACH_CAP)]
    return argv


def call_tool(command: str, arguments: dict | None = None):
    """Run a command **in this process** and return its :class:`Outcome`.

    This is the whole point of the facade (plan §11): the command module is
    imported and its ``run(args)`` invoked directly. No subprocess, no
    ``cmd.exe``, no second interpreter -- state a test can prove with an
    ``OSError``-raising ``subprocess`` monkeypatch.

    ``args`` is an ``argparse.Namespace`` for the *same* parser the CLI uses, so a
    tool sees exactly the defaults, types and ``getattr`` fallbacks a CLI caller
    would. Only the parsed values differ; the code path does not.
    """
    from .cli import _dispatch, build_parser
    from .lib.errors import ZoombieError

    argv = build_argv(command, arguments)
    # argparse calls sys.exit on a missing/invalid flag. A facade must turn that
    # into a REPORTED tool error -- a bare SystemExit would be an opaque exit code
    # to the client and an unhandled exception in a job thread, and the parser's
    # complaint (stderr) is the only useful diagnosis.
    complaint = io.StringIO()
    try:
        with redirect_stderr(complaint):
            namespace = build_parser().parse_args(argv)
    except SystemExit as exc:
        detail = (complaint.getvalue().strip().splitlines() or ["invalid arguments"])[-1]
        # argparse prints "<prog>: error: <detail>"; the prog is already implied
        # by the tool name, so keep only the useful half.
        if "error: " in detail:
            detail = detail.split("error: ", 1)[1]
        raise ZoombieError(f"{command}: {detail}") from exc

    # A suppressed stdout is the transport guarantee: process.write_result is the
    # only in-tree stdout writer and it must never reach the JSON-RPC stream.
    with _suppress_stdout() as captured:
        outcome = _dispatch(namespace)
    stray = captured.getvalue()
    if stray:
        # Reported, never hidden: a tool that printed to stdout has leaked into
        # what would be the protocol stream, and that is a bug worth seeing.
        process.log(
            f"{command} wrote {len(stray)} byte(s) to stdout; suppressed so the "
            f"JSON-RPC stream stays clean",
            "warn",
        )
    return outcome


# --- result shaping (reuse lib/next, do not re-implement) ------------------


def shape_image(attachable: list[dict] | None, cap: int = IMAGE_ATTACH_CAP) -> dict:
    """Split an attach list at the cap using :mod:`zoombie.lib.next`.

    Reusing :func:`zoombie.lib.next.select` rather than truncating here is the
    point: there is ONE cap in the codebase, so the CLI and the facade cannot
    drift. Returns ``{"attach", "overAttach", "truncated", "dropped"}``.
    """
    return next_mod.select(attachable, cap)


def _mime_for(path: str) -> str:
    suffix = paths.extension_of(path).lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(suffix, "image/png")


def image_blocks(entries: list[dict]) -> list[dict]:
    """MCP image content blocks for the attached entries.

    The bytes are base64-encoded from the real file on disk; an entry whose path
    does not exist is reported as text rather than claimed as an image, so the
    content list never advertises a file the client cannot open.
    """
    import base64

    blocks: list[dict] = []
    for entry in entries:
        path = entry.get("path") or ""
        if not path or not paths.is_file(path):
            blocks.append({
                "type": "text",
                "text": f"[missing image: {entry.get('file') or path}]",
            })
            continue
        try:
            with open(paths.to_extended(path), "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            blocks.append({"type": "text", "text": f"[unreadable image: {path}: {exc}]"})
            continue
        blocks.append({
            "type": "image",
            "data": base64.b64encode(raw).decode("ascii"),
            "mimeType": _mime_for(path),
        })
    return blocks


def _attachable_from_data(data: dict | None) -> list[dict]:
    """The canonical attachable list inside a result, from the block that owns it.

    ``data.next.attach`` is the sanctioned list (plan §9) and is preferred. The
    fallbacks exist because a terminal command emits ``next`` with an empty attach
    but may still have produced ``visionFrames``/``visionPages``.
    """
    if not isinstance(data, dict):
        return []
    block = data.get("next")
    if isinstance(block, dict) and isinstance(block.get("attach"), list) and block["attach"]:
        return [entry for entry in block["attach"] if isinstance(entry, dict)]
    for key in ("visionFrames", "visionPages"):
        entries = data.get(key)
        if isinstance(entries, list) and entries:
            return [entry for entry in entries if isinstance(entry, dict)]
    return []


def _deferred_label(_index: int, entry: dict, _total: int) -> str:
    """``f013 013 - 00-08-40.png`` -- a deferred frame named by its allowed handle.

    The id is the only handle an agent may name in ``keep``/``drop``; the file is
    shown beside it so a human reading the transcript can identify the frame. A
    command whose attach entries carry no id (an image run, say) degrades to the
    file name alone rather than inventing a handle it cannot honour.
    """
    name = str(entry.get("file") or entry.get("path") or "?")
    handle = entry.get("id")
    return f"{handle} {name}" if handle else name


def tool_result(command: str, outcome, cap: int = IMAGE_ATTACH_CAP) -> dict:
    """Wrap an :class:`Outcome` as an MCP ``tools/call`` result.

    The JSON envelope is the CLI's own five keys; the *MCP* content list is where
    the shaping lives: prose carries the whole JSON, then -- at most ``cap`` --
    image blocks so the model can actually look at the frames this result
    recommends. The remainder is not hidden: it is enumerated as text with the
    narrow-the-request advice, exactly as ``data.next`` does on the CLI side.
    """
    data = dict(getattr(outcome, "data", None) or {})
    envelope = process.result_payload(
        command, ok=bool(getattr(outcome, "ok", True)),
        data=data, error=getattr(outcome, "error", None),
    )
    content: list[dict] = [{
        "type": "text",
        "text": json.dumps(envelope, ensure_ascii=False),
    }]

    # The cap already applied by the command is reported; applying it again with
    # the same function is idempotent and means a command that forgot cannot leak
    # more than the cap through this transport.
    attachable = _attachable_from_data(data)
    if attachable:
        selected = shape_image(attachable, cap)
        content.append({
            "type": "text",
            "text": (
                f"attachCap {cap}: {len(selected['attach'])} of {len(attachable)} "
                f"image(s) attached"
                + (
                    f"; {len(selected['overAttach'])} deferred (re-run with a narrower "
                    "request to read them)"
                    if selected["truncated"]
                    else ""
                )
            ),
        })
        content.extend(image_blocks(selected["attach"]))
        if selected["truncated"]:
            # Name each deferred frame by its ID as well as its file. The id is the
            # handle the agent is allowed to use, and the guidance below is the
            # correction the Crimson talking-head run needed: the agent does not
            # raise the cap or touch files, it makes a KEEP/DROP decision and lets
            # the server do the work.
            described = ", ".join(
                _deferred_label(index, entry, len(attachable))
                for index, entry in enumerate(selected["overAttach"], start=1)
            )
            content.append({
                "type": "text",
                "text": (
                    "deferred images (each as <id> <file>): " + described
                ),
            })
            content.append({
                "type": "text",
                "text": (
                    "AGENT DECIDES, THE TOOL EDITS: look at what is attached, decide "
                    "which frames are worth keeping, then re-run this tool with "
                    "keep:\"<ids or timestamps>\" (or drop:\"...\"). Name frames by "
                    "their id (fNNN) or timestamp -- never a file path: do NOT delete, "
                    "move, rename or hand-edit anything under img/. The tool applies "
                    "the selection, prunes the dropped frames and rewrites the "
                    "manifest itself."
                ),
            })

    return {
        "content": content,
        "isError": not envelope["ok"],
        "data": envelope,
    }


# --- MCP method handlers ----------------------------------------------------


def _server_info() -> dict:
    return {"name": SERVER_NAME, "version": SKILL_VERSION}


def tool_schemas() -> list[dict]:
    """The ``tools/list`` payload: the ADVERTISED tools only.

    The descriptions state the mode flags that decide whether a call WRITES, so a
    client cannot accidentally invoke a writing mode: the house convention is that
    a dry run is the default and ``-Force``/``-Apply`` is required to write.

    A tool that is CALLABLE but not advertised (see :data:`ADVERTISED_TOOLS`) is
    built here and then dropped by the final filter, so its schema still exists to
    keep the two lists honest -- the emission order is :data:`ADVERTISED_TOOLS`.
    """
    def schema(name: str, description: str, props: dict, required: list[str]) -> dict:
        return {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": True,
            },
        }

    source_output = {
        "source": {"type": "string", "description": "source file or URL"},
        "output": {"type": "string", "description": "output file, folder or basename"},
        "dry_run": {"type": "boolean", "description": "plan only; write nothing (the default where the command supports it)"},
        "force": {"type": "boolean", "description": "overwrite existing outputs"},
        "keep_scratch": {"type": "boolean", "description": "retain this run's scratch for inspection"},
        "attach_limit": {"type": "integer", "description": f"max image blocks this result may return (default {IMAGE_ATTACH_CAP})"},
    }
    gpu = {
        "model": {"type": "string"},
        "no_gpu": {"type": "boolean"},
        "threads": {"type": "integer"},
    }

    tools_out: list[dict] = [
        schema("doctor", "Report tool status.", {}, []),
        schema("clean", "Remove scratch dirs.", {**source_output, "clean_scratch": {"type": "boolean", "description": "sweep every toolchain-owned scratch dir"}}, []),
        schema("download", "Download a video or its audio. The destination follows the summarize rule: a source inside the workspace keeps its folder; a URL lands under _unsorted/download/<name>. Override with download_dir, or refine the name with name.", {**source_output, "download_dir": {"type": "string"}, "name": {"type": "string", "description": "folder name under _unsorted/download (default: the title)"}, "audio_only": {"type": "boolean", "description": "download the audio track only"}, "format": {"type": "string", "enum": ["wav", "mp3", "m4a", "flac"]}}, ["source"]),
        schema("extract", "Extract audio from a video.", {**source_output, "format": {"type": "string", "enum": ["wav", "mp3", "m4a", "flac"]}}, ["source"]),
        schema("unpack", "Open any archive 7-Zip can read (7z, zip, rar, tar, gz, xz, cab, iso, ...) or an NSIS installer, into a folder. The source is DATA, never executed. Use check=true to list it first.", {**source_output, "strip": {"type": "integer"}, "include": {"type": "array", "items": {"type": "string"}}, "check": {"type": "boolean", "description": "list the archive's contents; write nothing"}}, ["source", "output"]),
        schema("transcribe", "Transcribe audio to text (blocks; use start/status/result).", {**source_output, **gpu, "language": {"type": "string"}, "from_time": {"type": "string"}, "to_time": {"type": "string"}}, ["source"]),
        schema("pipeline", "Download, extract and transcribe in one call (blocks; use start/status/result).", {**source_output, **gpu, "language": {"type": "string"}, "download_dir": {"type": "string"}}, ["source"]),
        schema("readpdf", "Convert a PDF to Markdown; vision renders text-less pages for a reader.", {**source_output, "pages": {"type": "string"}, "lang": {"type": "string"}, "dpi": {"type": "integer"}, "ocr": {"type": "boolean", "description": "OCR scanned pages with Tesseract"}, "vision": {"type": "string", "description": "render pages with NO text layer to PNG in this directory for a vision reader (ignored when ocr=true)"}, "images": {"type": "boolean", "description": "also extract embedded images"}, "images_only": {"type": "boolean", "description": "extract images and the sidecar only; render no Markdown"}, "image_dir": {"type": "string", "description": "explicit image directory"}}, ["source"]),
        schema("readimages", "Turn images into Markdown (OCR with ocr=true, else vision/metadata only).", {**source_output, "ocr": {"type": "boolean"}, "lang": {"type": "string"}, "image_dir": {"type": "string"}}, ["source"]),
        schema(
            "slides",
            (
                "Extract slide frames from a video plus a placement manifest (blocks; "
                "use start/status/result). The detector only PROPOSES frames; the agent "
                "makes the FINAL keep/drop call with keep/drop, naming frames by their "
                "id (fNNN) or timestamp as reported in data.images.imageIds -- never a "
                "path. The tool performs every file operation."
            ),
            {
                **source_output,
                "times": {"type": "string"},
                "scale": {"type": "integer"},
                "lang": {"type": "string"},
                "min_frame_bytes": {"type": "integer"},
                "keep": {
                    "type": "string",
                    "description": "agent ALLOW-LIST of frames to keep: ids (f005), timestamps, or seconds, comma/space separated. Non-empty replaces the default set",
                },
                "drop": {
                    "type": "string",
                    "description": "agent DENY-LIST of frames to drop (same handles as keep)",
                },
                "keep_file": {"type": "string", "description": "read keep from a UTF-8 file (one id per line)"},
                "drop_file": {"type": "string", "description": "read drop from a UTF-8 file"},
            },
            ["source"],
        ),
        schema(
            "summarize",
            (
                "Produce a summary.md step by step - the FRONT DOOR. Call once per step "
                "and follow data.next: source (summarize a URL or path; produces the "
                "source material), name (confirm the folder name and, for an existing "
                "local source, how the media is placed; slides is a SEPARATE question), "
                "slides (optional frames), prose (your title, summary, optional "
                "criticism and section headings; the backend assembles block 6 and "
                "archives any existing summary), verify (gate the tree and delete the "
                "run scratch). Args: step, run (from the previous step), source, name, "
                "media, slides, times, title, summary_text, criticism, sections."
            ),
            {
                "source": {"type": "string", "description": "source file or URL (step source)"},
                "step": {"type": "string", "enum": ["source", "name", "slides", "prose", "verify"]},
                "run": {"type": "string", "description": "the run scratch path from the previous step"},
                "name": {"type": "string", "description": "the confirmed folder name (step name)"},
                "media": {"type": "string", "enum": ["keep", "copy", "move", "none"], "description": "existing local media: keep in place, copy into the item, move into the item (requires confirm_move), or keep no copy (ask the user; default keep if in-place, else copy)"},
                "confirm_move": {"type": "boolean", "description": "acknowledge that media:\"move\" RELOCATES the user's own file (the source is removed); move is refused without it"},
                "slides": {"type": "string", "description": "true/false: extract slide frames? (step slides)"},
                "times": {"type": "string", "description": "exact slide timestamps"},
                "title": {"type": "string", "description": "the document title (step prose)"},
                "summary_text": {"type": "string", "description": "the short summary, block 3"},
                "criticism": {"type": "string", "description": "optional criticism sub-block"},
                "sections": {"type": "string", "description": "JSON array of {heading, at} topic-change sections for block 6"},
                "keep": {"type": "string", "description": "slide frames to KEEP (ids fNNN or timestamps); re-run step slides"},
                "drop": {"type": "string", "description": "slide frames to DROP (same handles as keep)"},
                "language": {"type": "string"},
            },
            [],
        ),
        schema("postprocess", "Assign anchors, timestamps, index and images in a summary.md (dry run unless apply=true).", {"md": {"type": "string"}, "dir": {"type": "string"}, "recurse": {"type": "boolean"}, "srt": {"type": "string"}, "image_dir": {"type": "string"}, "report": {"type": "string"}, "apply": {"type": "boolean"}, "attach_limit": {"type": "integer"}}, []),
        schema("verify", "Check a produced summary tree (exit 1 on problems).", {"dir": {"type": "string"}, "recurse": {"type": "boolean"}, "json": {"type": "boolean"}}, []),
        schema("items", "Scan a workspace for items and report them compactly.", {"root": {"type": "string"}, "depth": {"type": "integer"}, "recurse": {"type": "boolean"}, "json": {"type": "boolean"}}, []),
        schema("modes", "Deploy the Zoombie role into the global custom modes (dry run unless apply=true).", {"target": {"type": "string"}, "check": {"type": "boolean"}, "apply": {"type": "boolean"}, "force": {"type": "boolean"}}, []),
        schema("mcp", "Register the zoombie MCP server in the client's global MCP settings (dry run unless apply=true).", {"target": {"type": "string"}, "check": {"type": "boolean"}, "apply": {"type": "boolean"}, "force": {"type": "boolean"}}, []),
    ]
    # Advertise only the small agent-facing set, in the order ADVERTISED_TOOLS
    # names it. A tool not in the tuple is still honoured by ``tools/call``.
    by_name = {tool["name"]: tool for tool in tools_out}
    return [by_name[name] for name in ADVERTISED_TOOLS if name in by_name]


def handle_initialize(request_id, params: dict) -> dict:
    return result(request_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": _server_info(),
        # Stated in the reply, not only in the code: a client may inspect what it
        # has attached to, and the interpreter here is the resolved one.
        "instructions": (
            "zoombie toolchain. Tools call the CLI commands in-process (no cmd.exe, "
            "no console code page). Image results are hard-capped; read the attached "
            "blocks, and NARROW the request to reach the deferred ones."
        ),
    })


def handle_tools_list(request_id, params: dict) -> dict:
    return result(request_id, {"tools": tool_schemas()})


def handle_tools_call(request_id, params: dict) -> dict:
    if not isinstance(params, dict):
        return failure(request_id, INVALID_PARAMS, "tools/call requires an object of params")
    name = params.get("name")
    if not name:
        return failure(request_id, INVALID_PARAMS, "tools/call requires a tool name")
    if name not in TOOL_COMMANDS:
        return _tool_error(request_id, TOOL_NOT_FOUND, f"unknown tool: {name}",
                           {"tool": name, "known": sorted(TOOL_COMMANDS)})
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return failure(request_id, INVALID_PARAMS, "tools/call arguments must be an object")

    from .cli import Outcome
    from .lib.errors import ZoombieError

    try:
        outcome = call_tool(name, arguments)
    except ZoombieError as exc:
        return _tool_error(request_id, TOOL_FAILED, str(exc), {"tool": name})
    except Exception as exc:  # noqa: BLE001 - a tool must not crash the server
        return _tool_error(
            request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}", {"tool": name}
        )
    if outcome is None:
        outcome = Outcome(ok=False, error="the command returned no result")
    limit = arguments.get("attach_limit")
    cap = int(limit) if isinstance(limit, (int, float)) and int(limit) > 0 else IMAGE_ATTACH_CAP
    return result(request_id, tool_result(name, outcome, cap))


# --- async job registry (start / status / result) ---------------------------


class JobStore:
    """Thread-safe registry for the ``start``/``status``/``result`` lifecycle.

    A tool may block for minutes, and a client that must round-trip ``tools/call``
    for it is the blocking call plan §11 replaces. ``start`` returns a handle at
    once; the work runs on a daemon thread; ``status`` polls; ``result`` returns
    the finished outcome once and then reports it as gone (the outcome is held
    until the first successful read, then released, so a long session does not
    accumulate result payloads).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._counter = 0

    def start(self, command: str, arguments: dict | None = None) -> dict:
        with self._lock:
            self._counter += 1
            job_id = f"{command}-{self._counter}"
            record = {
                "id": job_id,
                "tool": command,
                "arguments": dict(arguments or {}),
                "state": "running",
                "startedAt": process.utc_now_iso(),
                "finishedAt": None,
                "error": None,
                "outcome": None,
                "thread": None,
            }
            self._jobs[job_id] = record
        thread = threading.Thread(
            target=self._run, args=(job_id, command, arguments), daemon=True,
            name=f"zoombie-{job_id}",
        )
        record["thread"] = thread
        thread.start()
        described = self.describe(job_id)
        return described if described is not None else self._public(record)

    def _run(self, job_id: str, command: str, arguments: dict | None) -> None:
        from .lib.errors import ZoombieError

        try:
            outcome = call_tool(command, arguments)
            with self._lock:
                self._jobs[job_id]["outcome"] = outcome
                self._jobs[job_id]["state"] = "done"
                self._jobs[job_id]["finishedAt"] = process.utc_now_iso()
        except ZoombieError as exc:
            with self._lock:
                self._jobs[job_id]["state"] = "failed"
                self._jobs[job_id]["error"] = str(exc)
                self._jobs[job_id]["finishedAt"] = process.utc_now_iso()
        except Exception as exc:  # noqa: BLE001 - a job failure is reported, not raised
            with self._lock:
                self._jobs[job_id]["state"] = "failed"
                self._jobs[job_id]["error"] = f"{type(exc).__name__}: {exc}"
                self._jobs[job_id]["finishedAt"] = process.utc_now_iso()

    def _public(self, record: dict) -> dict:
        return {
            "jobId": record["id"],
            "tool": record["tool"],
            "state": record["state"],
            "startedAt": record["startedAt"],
            "finishedAt": record["finishedAt"],
            "error": record["error"],
            "hasResult": record["outcome"] is not None,
        }

    def describe(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            return self._public(record)

    def wait(self, job_id: str, timeout: float | None = None) -> dict | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            thread = record["thread"]
        if thread is not None:
            thread.join(timeout)
        return self.describe(job_id)

    def take(self, job_id: str) -> dict | None:
        """Pop a finished job's outcome; the job is then gone."""
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            if record["state"] == "running":
                return {"state": "running", "outcome": None, "error": None}
            outcome = record.pop("outcome", None)
            state = record["state"]
            error = record["error"]
            self._jobs.pop(job_id, None)
            return {"state": state, "outcome": outcome, "error": error}


# --- facets: the read-only tools over the pipeline's own data ---------------


def _read_json(path: str) -> dict | None:
    if not paths.is_file(path):
        return None
    try:
        with open(paths.to_extended(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def facet_next(item: str) -> dict:
    """``zoombie.facets.next``: what to do next with an item.

    An item is recognized by its ``summary.md``; its figures live in the visible
    ``img/``. This facet reports what is there and, through
    :func:`zoombie.lib.next.build`, what the next step is -- reusing the ONE cap in
    the codebase rather than inventing a second.
    """
    if not item:
        raise ValueError("facets.next requires an item path")
    root = paths.absolute(item)
    if not paths.is_dir(root):
        raise ValueError(f"item not found: {root}")

    image_dir = item_paths.image_dir(root)
    frames = [entry for entry in paths.list_dir(image_dir, files=True)
              if entry.name.lower().endswith(".png")]
    attachable = [
        {"file": entry.name, "path": entry.path, "bytes": paths.file_size(entry.path)}
        for entry in sorted(frames, key=lambda item: item.name)
    ]

    has_summary = paths.is_file(item_paths.summary_path(root))
    is_item = item_paths.is_item(root)
    block = next_mod.build(
        "postprocess",
        {"-Md": item_paths.summary_path(root)} if has_summary else None,
        why=(
            "the item has figures; run postprocess -Apply to (re)inline them"
            if attachable else
            "this folder is not an item yet; start a summarize run to produce one"
            if not is_item else
            "the item has no figures under img/"
        ),
        attachable=attachable,
        args_capped=len(attachable) > IMAGE_ATTACH_CAP,
    )
    return {
        "item": root,
        "isItem": is_item,
        "hasSummary": has_summary,
        "images": {"dir": image_dir, "count": len(attachable)},
        "next": block,
        "asciiSafe": True,
    }


def facet_status() -> dict:
    """``zoombie.facets.status``: is the toolchain usable, and is scratch clean?

    Deliberately does NOT require the server to be "up": it is a read-only probe a
    client can call immediately, and it answers whether the interpreter, engine and
    roots look right. Scratch uses :func:`zoombie.lib.scratch.inventory`, so the
    ownership rule is the one place it is defined.
    """
    python = tools.find_python()
    entries = scratch.inventory()
    owned = [entry for entry in entries if entry.get("owned")]
    foreign = [entry for entry in entries if not entry.get("owned")]

    from .lib import ocr, tesseract

    available, detail = (False, "not probed")
    try:
        available, detail = ocr.available()
    except Exception as exc:  # noqa: BLE001 - a missing engine is a status, not a crash
        detail = f"{type(exc).__name__}: {exc}"

    return {
        "server": _server_info(),
        "protocolVersion": PROTOCOL_VERSION,
        "interpreter": {
            "path": python,
            "running": sys.executable,
            "same": bool(
                python
                and paths.is_file(python)
                and os.path.normcase(os.path.abspath(python))
                == os.path.normcase(os.path.abspath(sys.executable))
            ),
        },
        "root": paths.env_root(),
        "asciiRoot": paths.is_ascii(paths.env_root()),
        "ocr": {"available": available, "detail": detail,
                "engine": tesseract.resolve_engine() if available else None},
        "scratch": {
            "owned": [entry["path"] for entry in owned],
            "ownedCount": len(owned),
            "foreignCount": len(foreign),
            "cleanScratchVerb": "clean -CleanScratch" if owned else None,
        },
    }


def facet_clean(request_id, params: dict) -> dict:
    """``zoombie.facets.clean``: the plan-§10 scratch sweep as a tool.

    Reuses :func:`zoombie.lib.scratch.clean` so the ownership rule (a run-marker
    name whose parent is one of the two roots) is not re-stated here; ``dryRun``
    removes nothing, matching the house convention.
    """
    dry_run = bool(params.get("dryRun", False))
    include_temp = params.get("includeTemp", True)
    if not isinstance(include_temp, bool):
        include_temp = True
    block = scratch.clean(include_temp=include_temp, dry_run=dry_run)
    # Like the other `zoombie/*` facets, the payload IS the result: only
    # `tools/call` wraps in MCP content blocks plus the five-key envelope.
    return result(request_id, block)


# --- dispatch ---------------------------------------------------------------


def dispatch(message: dict) -> dict | None:
    """Answer one JSON-RPC message, or return ``None`` for a notification.

    Unknown methods get a spec-correct ``METHOD_NOT_FOUND``; a malformed request
    gets ``INVALID_REQUEST``; an exception inside a handler is caught and reported
    rather than allowed to kill the server loop.
    """
    if not isinstance(message, dict):
        return failure(None, INVALID_REQUEST, "a JSON-RPC message must be an object")

    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        if message.get("id") is None:
            return None
        return failure(request_id, INVALID_PARAMS, "params must be an object")

    # A notification (no id) is never answered -- including initialize in some
    # client flows and `notifications/initialized`.
    is_notification = "id" not in message or message.get("id") is None

    if not method:
        return None if is_notification else failure(request_id, INVALID_REQUEST, "missing method")

    try:
        if method == "initialize":
            response = handle_initialize(request_id, params)
        elif method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None
        elif method == "ping":
            response = result(request_id, {})
        elif method == "tools/list":
            response = handle_tools_list(request_id, params)
        elif method == "tools/call":
            response = handle_tools_call(request_id, params)
        elif method in ("zoombie/job/start", "start"):
            command = params.get("tool") or params.get("name")
            if not command:
                response = failure(request_id, INVALID_PARAMS, "start requires a tool name")
            elif command not in TOOL_COMMANDS:
                response = failure(request_id, TOOL_NOT_FOUND, f"unknown tool: {command}")
            else:
                response = result(request_id, _JOBS.start(command, params.get("arguments")))
        elif method in ("zoombie/job/status", "status"):
            job_id = params.get("jobId") or params.get("id")
            described = _JOBS.describe(job_id) if job_id else None
            if described is None:
                response = failure(request_id, JOB_NOT_FOUND, f"unknown job: {job_id}")
            else:
                response = result(request_id, described)
        elif method in ("zoombie/job/result", "result"):
            job_id = params.get("jobId") or params.get("id")
            taken = _JOBS.take(job_id) if job_id else None
            if taken is None:
                response = failure(request_id, JOB_NOT_FOUND, f"unknown job: {job_id}")
            elif taken["state"] == "running":
                response = result(request_id, {"state": "running", "jobId": job_id})
            elif taken["state"] == "failed":
                response = _tool_error(request_id, TOOL_FAILED, taken["error"] or "job failed", {"jobId": job_id})
            else:
                response = result(request_id, tool_result("job", taken["outcome"]))
        elif method in ("zoombie/facets/next", "facets/next"):
            response = result(
                request_id,
                facet_next(str(params.get("item") or params.get("path") or "")),
            )
        elif method in ("zoombie/facets/status", "facets/status"):
            response = result(request_id, facet_status())
        elif method in ("zoombie/facets/clean", "facets/clean"):
            response = facet_clean(request_id, params)
        else:
            response = failure(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")
    except ValueError as exc:
        response = failure(request_id, INVALID_PARAMS, str(exc))
    except Exception as exc:  # noqa: BLE001 - the server must survive any handler
        response = failure(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    if is_notification:
        return None
    return response


_JOBS = JobStore()


# --- the server loop --------------------------------------------------------


def serve(stdin=None, stdout=None, stderr=None) -> int:
    """Read JSON-RPC requests line by line and answer them on stdout.

    stdout is the transport: the ONLY things written to it are
    :func:`frame`-encoded responses and the tool-result ``text`` blocks the client
    asked for. Progress goes to stderr through
    :func:`zoombie.lib.process.log`.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if stderr is not None and stderr is not sys.stderr:
        sys.stderr = stderr
    process.log(f"zoombie MCP facade ready ({SERVER_NAME} {SKILL_VERSION}); "
                f"python={sys.executable}", "step")

    exit_code = 0
    for raw in stdin:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        try:
            request = read_message(line)
        except ValueError as exc:
            # A parse error has no id to echo; the spec says answer with null id.
            stdout.write(frame(failure(None, PARSE_ERROR, str(exc))))
            stdout.flush()
            continue
        try:
            response = dispatch(request)
        except Exception as exc:  # noqa: BLE001 - a bug must not take the server down
            response = failure(request.get("id"), INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            exit_code = 1
        if response is not None:
            stdout.write(frame(response))
            stdout.flush()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Entry point behind ``python -m zoombie.mcp``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-Version"):
        sys.stderr.write(f"zoombie-mcp {SKILL_VERSION}\n")
        return 0
    if argv and argv[0] in ("--help", "-h"):
        sys.stderr.write(
            "python -m zoombie.mcp -- a JSON-RPC (MCP) facade over the zoombie CLI.\n"
            "Framing: one JSON object per line on stdin/stdout; logs on stderr.\n"
            "Methods: initialize, tools/list, tools/call, zoombie/job/{start,status,result},\n"
            "         zoombie/facets/{next,status,clean}.\n"
        )
        return 0
    return serve()


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m`
    sys.exit(main())

"""Tests for the MCP facade (plan §11).

The facade is a *thin* transport, so the assertions are about four things and
almost nothing else:

1. **Framing** -- one JSON object per line, round-tripping, rejecting junk with a
   proper JSON-RPC error instead of a crash.
2. **In-process routing** -- a tool call reaches ``commands/*.run(args)`` in THIS
   interpreter. No subprocess, no ``cmd.exe``. This is the decisive property:
   it is what removes the console-code-page problem and the ``@file`` staging.
3. **stdout purity** -- stdout carries protocol messages and nothing else, even
   when the called command writes its own result line or a stray ``print``.
4. **Shaping** -- image content is hard-capped by REUSING
   :mod:`zoombie.lib.next`; the facade defines no second cap.

Plus the ``start``/``status``/``result`` lifecycle and the read-only facets.

The one thing these tests CANNOT prove is that a real MCP client renders image
content blocks and progress per spec -- that is the client, outside this repo
(plan §15). Server-side behaviour is what is pinned here; the receipt says so
plainly.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

import pytest

from zoombie import cli, mcp
from zoombie.lib import next as next_mod
from zoombie.lib.errors import ZoombieError


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _png() -> bytes:
    """A minimal but genuine PNG, so a path is a real file, not a placeholder."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def _attachable(directory, count: int) -> list[dict]:
    """``count`` real PNG files, as ``data.next``-style attach entries."""
    entries: list[dict] = []
    for index in range(count):
        path = os.path.join(str(directory), f"{index:03d} - 00-00-0{index % 10}.png")
        with open(path, "wb") as handle:
            handle.write(_png())
        entries.append({"file": os.path.basename(path), "path": path, "bytes": os.path.getsize(path)})
    return entries


def _request(request_id, method, params=None) -> dict:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _serve(lines: list[dict | str]) -> tuple[int, list[dict], str]:
    """Drive ``serve`` over the given lines and decode every response line.

    Returns ``(exit_code, responses, stdout_text)``. The stdout text is returned
    as well as the decoded objects, because the purity tests assert on the BYTES
    that would reach a client.
    """
    payload = []
    for line in lines:
        payload.append(line if isinstance(line, str) else json.dumps(line))
    stdin = io.StringIO("\n".join(payload) + "\n")
    stdout = io.StringIO()
    code = mcp.serve(stdin=stdin, stdout=stdout)
    text = stdout.getvalue()
    responses = [json.loads(line) for line in text.splitlines() if line.strip()]
    return code, responses, text


# --------------------------------------------------------------------------- #
# 1. framing
# --------------------------------------------------------------------------- #

class TestFraming:
    def test_frame_is_one_line_and_round_trips(self):
        message = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
        line = mcp.frame(message)
        assert line.endswith("\n")
        assert line.count("\n") == 1
        assert mcp.read_message(line) == message

    def test_non_ascii_survives_the_frame(self):
        """A Cyrillic path must not become \\uXXXX escapes on the wire."""
        message = {"jsonrpc": "2.0", "id": 1, "params": {"output": "Дивиденды"}}
        line = mcp.frame(message)
        assert "Дивиденды" in line
        assert mcp.read_message(line)["params"]["output"] == "Дивиденды"

    def test_read_message_rejects_an_empty_line(self):
        with pytest.raises(ValueError):
            mcp.read_message("   ")

    def test_read_message_rejects_non_json(self):
        with pytest.raises(ValueError):
            mcp.read_message("this is not json")

    def test_read_message_rejects_a_json_array(self):
        """A JSON-RPC message is an object; an array is a clean error, not a crash."""
        with pytest.raises(ValueError):
            mcp.read_message("[1, 2, 3]")

    def test_a_bad_line_becomes_a_parse_error_with_a_null_id(self):
        _code, responses, _text = _serve(["not json at all"])
        assert responses[0]["error"]["code"] == mcp.PARSE_ERROR
        assert responses[0]["id"] is None

    def test_a_blank_line_is_skipped_silently(self):
        code, responses, _text = _serve(["", _request(1, "ping")])
        assert code == 0
        assert len(responses) == 1


# --------------------------------------------------------------------------- #
# 2. in-process routing (no subprocess, no cmd.exe)
# --------------------------------------------------------------------------- #

class TestInProcessRouting:
    def test_a_tool_call_succeeds_with_subprocess_disabled(self, monkeypatch, tmp_path):
        """The decisive test: spawn nothing and it still works.

        If the facade shelled out to ``zoombie.cmd`` (or any helper), an
        ``OSError``-raising ``subprocess`` would break this call. It does not.
        """
        import subprocess

        def boom(*_args, **_kwargs):
            raise AssertionError("the facade spawned a subprocess")

        monkeypatch.setattr(subprocess, "run", boom)
        monkeypatch.setattr(subprocess, "Popen", boom)
        monkeypatch.setattr(os, "system", boom)

        code, responses, _text = _serve([
            _request(1, "tools/call", {"name": "items", "arguments": {"root": str(tmp_path), "json": True}})
        ])
        assert code == 0
        payload = responses[0]["result"]["data"]
        assert payload["ok"] is True
        assert payload["action"] == "items"

    def test_the_call_reaches_the_command_module_run(self, monkeypatch, tmp_path):
        """A spy on ``commands/<name>.run`` proves the in-process hand-off."""
        from zoombie.commands import items as items_cmd

        seen: dict = {}
        original = items_cmd.run

        def spy(args):
            seen["args"] = args
            return original(args)

        monkeypatch.setattr(items_cmd, "run", spy)
        _serve([_request(1, "tools/call", {"name": "items", "arguments": {"root": str(tmp_path), "json": True}})])
        assert isinstance(seen["args"], argparse.Namespace)
        assert seen["args"].command == "items"
        assert seen["args"].root == str(tmp_path)

    def test_dispatch_is_the_cli_dispatch_not_a_private_copy(self, monkeypatch, tmp_path):
        """The facade routes through ``cli._dispatch`` -- one dispatcher, not two."""
        seen: dict = {}

        def spy(namespace):
            seen["command"] = namespace.command
            from zoombie.cli import Outcome

            return Outcome(ok=True, data={"spied": True})

        monkeypatch.setattr(cli, "_dispatch", spy)
        _code, responses, _text = _serve([
            _request(1, "tools/call", {"name": "items", "arguments": {"root": str(tmp_path)}})
        ])
        assert seen["command"] == "items"
        assert responses[0]["result"]["data"]["data"]["spied"] is True

    def test_no_cmd_exe_anywhere_in_the_argv_path(self):
        """The CLI launcher is never referenced by the facade's argv builder."""
        for name in mcp.TOOL_COMMANDS:
            argv = mcp.build_argv(name, {"source": "s", "output": "o"})
            assert argv[0] == name
            assert not any("cmd" in token.lower() for token in argv)
            assert not any("zoombie.cmd" in token.lower() for token in argv)

    def test_build_argv_maps_the_shared_flag_spellings(self):
        argv = mcp.build_argv(
            "slides",
            {"source": "deck.mp4", "output": "item", "dry_run": True, "force": True, "times": "00:01:00"},
        )
        assert argv[0] == "slides"
        assert "--source" in argv and "deck.mp4" in argv
        assert "--output" in argv and "item" in argv
        assert "--dry-run" in argv
        assert "--force" in argv
        assert "--times" in argv and "00:01:00" in argv

    def test_an_unknown_argument_is_refused_not_dropped(self):
        with pytest.raises(ValueError):
            mcp.build_argv("items", {"source": "s", "totallyMadeUp": 1})

    def test_the_attach_limit_flag_is_always_injected(self):
        """A command that emits ``data.next`` always gets the plan-§9 cap."""
        for name in ("slides", "readimages", "readpdf", "postprocess"):
            argv = mcp.build_argv(name, {})
            assert "--attach-limit" in argv
            assert str(next_mod.DEFAULT_ATTACH_CAP) in argv

    def test_an_explicit_limit_overrides_the_injected_one(self):
        argv = mcp.build_argv("readpdf", {"source": "x.pdf", "attach_limit": 3})
        assert argv.count("--attach-limit") == 1
        assert "3" in argv


# --------------------------------------------------------------------------- #
# 3. stdout purity
# --------------------------------------------------------------------------- #

class TestStdoutPurity:
    def test_a_tool_that_prints_to_stdout_cannot_corrupt_the_stream(self, monkeypatch, tmp_path):
        """The structural guarantee behind "stdout is the transport".

        A command is stubbed to write a result line AND a stray print. Both are
        swallowed by ``_suppress_stdout``, so the only bytes on stdout are the
        JSON-RPC response.
        """
        from zoombie.cli import Outcome
        from zoombie.lib import process

        def noisy(namespace):
            process.write_result("items", ok=True, data={"wrote": "a line"})
            print("a stray line from a careless tool")
            return Outcome(ok=True, data={"clean": True})

        monkeypatch.setattr(cli, "_dispatch", noisy)
        code, responses, text = _serve([
            _request(1, "tools/call", {"name": "items", "arguments": {"root": str(tmp_path)}})
        ])
        assert code == 0
        # Every line on stdout parses as JSON-RPC, and there is exactly one.
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["id"] == 1
        assert "a stray line" not in text
        assert responses[0]["result"]["data"]["data"]["clean"] is True

    def test_the_suppressed_bytes_are_reported_not_hidden(self, monkeypatch, tmp_path, capsys):
        from zoombie.cli import Outcome

        monkeypatch.setattr(cli, "_dispatch", lambda ns: (print("noise"), Outcome(ok=True))[1])
        _serve([_request(1, "tools/call", {"name": "items", "arguments": {"root": str(tmp_path)}})])
        assert "stdout" in capsys.readouterr().err

    def test_the_ready_banner_goes_to_stderr_not_stdout(self):
        _code, _responses, text = _serve([_request(1, "ping")])
        assert "ready" not in text

    def test_command_logging_goes_to_stderr(self, tmp_path, capsys):
        """``process.log`` never touches stdout -- the facade relies on that."""
        _serve([_request(1, "tools/call", {"name": "items", "arguments": {"root": str(tmp_path), "json": True}})])
        captured = capsys.readouterr()
        # The scan logs to stderr; nothing of it is on stdout.
        assert "--" not in captured.out

    def test_suppress_stdout_swallows_everything(self):
        with mcp._suppress_stdout() as buffer:
            print("hello")
            sys.stdout.write("world")
        assert buffer.getvalue() == "hello\nworld"


# --------------------------------------------------------------------------- #
# 4. shaping: the cap is lib/next's, not a second one
# --------------------------------------------------------------------------- #

class TestCapShaping:
    def test_the_facade_defines_no_second_cap(self):
        assert mcp.IMAGE_ATTACH_CAP == next_mod.DEFAULT_ATTACH_CAP

    def test_shape_image_delegates_to_next_select(self, monkeypatch):
        """Proven by spying: if the facade truncated on its own, this fails."""
        calls: dict = {}
        original = next_mod.select

        def spy(attachable, cap=next_mod.DEFAULT_ATTACH_CAP):
            calls["cap"] = cap
            return original(attachable, cap)

        monkeypatch.setattr(mcp.next_mod, "select", spy)
        mcp.shape_image([{"file": "a.png"}], 3)
        assert calls["cap"] == 3

    def test_twelve_images_become_eight_blocks_plus_a_deferred_note(self, tmp_path):
        from zoombie.cli import Outcome

        entries = _attachable(tmp_path, 12)
        outcome = Outcome(ok=True, data={"next": {"attach": entries}, "count": 12})
        payload = mcp.tool_result("slides", outcome)

        images = [block for block in payload["content"] if block.get("type") == "image"]
        assert len(images) == next_mod.DEFAULT_ATTACH_CAP
        assert all(block["mimeType"] == "image/png" for block in images)
        # The deferred remainder is NAMED, not silently dropped.
        deferred = [block for block in payload["content"] if "deferred images" in block.get("text", "")]
        assert deferred and "009 -" in deferred[0]["text"]
        assert payload["isError"] is False

    def test_the_result_envelope_is_the_cli_envelope(self, tmp_path):
        from zoombie.cli import Outcome

        payload = mcp.tool_result("items", Outcome(ok=True, data={"a": 1}))
        envelope = payload["data"]
        assert list(envelope.keys()) == ["ok", "action", "error", "data", "timestamp"]
        assert envelope["action"] == "items"

    def test_a_small_attach_list_is_untouched(self, tmp_path):
        from zoombie.cli import Outcome

        entries = _attachable(tmp_path, 3)
        payload = mcp.tool_result("slides", Outcome(ok=True, data={"next": {"attach": entries}}))
        images = [block for block in payload["content"] if block.get("type") == "image"]
        assert len(images) == 3

    def test_an_explicit_cap_is_honoured(self, tmp_path):
        from zoombie.cli import Outcome

        entries = _attachable(tmp_path, 5)
        payload = mcp.tool_result("slides", Outcome(ok=True, data={"next": {"attach": entries}}), cap=2)
        images = [block for block in payload["content"] if block.get("type") == "image"]
        assert len(images) == 2

    def test_a_missing_image_is_text_not_a_claimed_image(self, tmp_path):
        from zoombie.cli import Outcome

        outcome = Outcome(ok=True, data={"next": {"attach": [
            {"file": "gone.png", "path": os.path.join(str(tmp_path), "gone.png")}
        ]}})
        payload = mcp.tool_result("slides", outcome)
        images = [block for block in payload["content"] if block.get("type") == "image"]
        assert images == []
        assert any("missing image" in block.get("text", "") for block in payload["content"])

    def test_the_base64_is_the_real_file_bytes(self, tmp_path):
        import base64

        from zoombie.cli import Outcome

        entries = _attachable(tmp_path, 1)
        payload = mcp.tool_result("readimages", Outcome(ok=True, data={"next": {"attach": entries}}))
        image = [block for block in payload["content"] if block.get("type") == "image"][0]
        with open(entries[0]["path"], "rb") as handle:
            assert base64.b64decode(image["data"]) == handle.read()

    def test_vision_frames_are_a_fallback_when_next_has_no_attach(self, tmp_path):
        from zoombie.cli import Outcome

        entries = _attachable(tmp_path, 2)
        outcome = Outcome(ok=True, data={"next": {"attach": []}, "visionFrames": entries})
        payload = mcp.tool_result("slides", outcome)
        images = [block for block in payload["content"] if block.get("type") == "image"]
        assert len(images) == 2

    def test_an_error_result_is_flagged(self):
        from zoombie.cli import Outcome

        payload = mcp.tool_result("items", Outcome(ok=False, error="nope"))
        assert payload["isError"] is True
        assert payload["data"]["error"] == "nope"


# --------------------------------------------------------------------------- #
# 5. errors: spec-correct, never a crash
# --------------------------------------------------------------------------- #

class TestErrorShapes:
    def test_an_unknown_method_is_method_not_found(self):
        _code, responses, _text = _serve([_request(9, "no/such/method")])
        assert responses[0]["error"]["code"] == mcp.METHOD_NOT_FOUND
        assert responses[0]["id"] == 9

    def test_an_unknown_tool_is_a_tool_error_carrying_the_envelope(self):
        _code, responses, _text = _serve([
            _request(1, "tools/call", {"name": "no_such_tool", "arguments": {}})
        ])
        payload = responses[0]["result"]
        assert payload["isError"] is True
        assert payload["data"]["data"]["jsonrpcCode"] == mcp.TOOL_NOT_FOUND
        assert "no_such_tool" in payload["content"][0]["text"]

    def test_a_missing_tool_name_is_invalid_params(self):
        _code, responses, _text = _serve([_request(1, "tools/call", {})])
        assert responses[0]["error"]["code"] == mcp.INVALID_PARAMS

    def test_arguments_must_be_an_object(self):
        _code, responses, _text = _serve([
            _request(1, "tools/call", {"name": "items", "arguments": [1, 2]})
        ])
        assert responses[0]["error"]["code"] == mcp.INVALID_PARAMS

    def test_a_zoombie_error_is_a_tool_error_not_a_transport_error(self, monkeypatch):
        def boom(namespace):
            raise ZoombieError("no frames found")

        monkeypatch.setattr(cli, "_dispatch", boom)
        # A source is supplied so parsing succeeds and the STUBBED dispatch runs:
        # this test is about a command failing, not about a missing flag.
        _code, responses, _text = _serve([
            _request(1, "tools/call", {"name": "slides", "arguments": {"source": "deck.mp4"}})
        ])
        assert "error" not in responses[0], "a tool saying no is not a transport failure"
        assert responses[0]["result"]["isError"] is True
        assert responses[0]["result"]["data"]["error"] == "no frames found"

    def test_an_internal_crash_is_reported_and_the_server_survives(self, monkeypatch):
        def boom(namespace):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(cli, "_dispatch", boom)
        _code, responses, _text = _serve([
            _request(1, "tools/call", {"name": "slides", "arguments": {"source": "deck.mp4"}}),
            _request(2, "ping"),
        ])
        assert responses[0]["result"]["isError"] is True
        assert "kaboom" in responses[0]["result"]["data"]["error"]
        # The next request is still answered: the loop did not die.
        assert responses[1]["result"] == {}

    def test_a_handler_crash_at_dispatch_level_is_caught(self, monkeypatch):
        monkeypatch.setattr(mcp, "handle_tools_list", lambda i, p: (_ for _ in ()).throw(RuntimeError("x")))
        response = mcp.dispatch(_request(1, "tools/list"))
        assert response["error"]["code"] == mcp.INTERNAL_ERROR

    def test_a_request_without_a_method_is_invalid(self):
        response = mcp.dispatch({"jsonrpc": "2.0", "id": 3})
        assert response["error"]["code"] == mcp.INVALID_REQUEST

    def test_a_notification_is_never_answered(self):
        assert mcp.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        _code, responses, text = _serve([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        assert responses == [] and text.strip() == ""


# --------------------------------------------------------------------------- #
# 6. the envelope is unchanged (plan §14)
# --------------------------------------------------------------------------- #

class TestEnvelopeUnchanged:
    def test_the_cli_and_the_facade_share_one_envelope_builder(self, tmp_path, capsys):
        """The facade must not re-declare the five keys; it must call the CLI's."""
        from zoombie.lib import process

        assert cli.main(["items", "-Root", str(tmp_path), "-Json"]) == 0
        cli_envelope = json.loads(capsys.readouterr().out.strip())

        from zoombie.cli import Outcome

        facade_envelope = mcp.tool_result("items", Outcome(ok=True, data=cli_envelope["data"]))["data"]
        assert list(cli_envelope.keys()) == ["ok", "action", "error", "data", "timestamp"]
        assert list(facade_envelope.keys()) == list(cli_envelope.keys())

    def test_a_real_tool_call_carries_exactly_the_five_keys(self, tmp_path):
        _code, responses, _text = _serve([
            _request(1, "tools/call", {"name": "items", "arguments": {"root": str(tmp_path), "json": True}})
        ])
        assert list(responses[0]["result"]["data"].keys()) == [
            "ok", "action", "error", "data", "timestamp"
        ]

    def test_initialize_does_not_wrap_itself_in_the_envelope(self):
        """A protocol reply is the spec's shape; only tool RESULTS carry ours."""
        _code, responses, _text = _serve([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}}}
        ])
        result = responses[0]["result"]
        assert list(result.keys()) == ["protocolVersion", "capabilities", "serverInfo", "instructions"]
        assert result["serverInfo"]["name"] == "zoombie"


# --------------------------------------------------------------------------- #
# 7. tools/list -- one tool per CLI verb, and it stays in step
# --------------------------------------------------------------------------- #

class TestToolListing:
    def test_every_tool_is_a_real_cli_subcommand(self):
        parser = cli.build_parser()
        subcommands = set()
        for action in parser._actions:  # noqa: SLF001 - argparse exposes no public API
            if getattr(action, "choices", None):
                subcommands.update(action.choices.keys())
        for name in mcp.TOOL_COMMANDS:
            assert name in subcommands, f"tool {name} has no CLI subcommand"

    def test_every_tool_is_dispatched_by_the_cli(self):
        """A facade tool that ``_dispatch`` does not know would be a dead end."""
        import inspect

        source = inspect.getsource(cli._dispatch)
        for name in mcp.TOOL_COMMANDS:
            assert f'"{name}"' in source, f"{name} is not in cli._dispatch"

    def test_tools_list_reports_a_schema_for_each(self):
        _code, responses, _text = _serve([_request(1, "tools/list")])
        listed = {tool["name"] for tool in responses[0]["result"]["tools"]}
        assert listed == set(mcp.TOOL_COMMANDS)

    def test_every_schema_is_well_formed(self):
        for tool in mcp.tool_schemas():
            assert tool["name"] and tool["description"]
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            assert isinstance(schema["properties"], dict)
            assert isinstance(schema["required"], list)

    def test_the_long_stages_are_declared(self):
        """The set that ``start``/``status``/``result`` exists for is explicit."""
        assert {"slides", "pipeline", "transcribe"} <= mcp.LONG_TOOLS


# --------------------------------------------------------------------------- #
# 8. the job lifecycle
# --------------------------------------------------------------------------- #

class TestJobLifecycle:
    def test_start_then_status_then_result(self, tmp_path):
        store = mcp.JobStore()
        handle = store.start("items", {"root": str(tmp_path), "json": True})
        assert handle["state"] == "running"
        assert handle["jobId"].startswith("items-")

        described = store.wait(handle["jobId"], timeout=10)
        assert described["state"] == "done"
        assert described["hasResult"] is True

        taken = store.take(handle["jobId"])
        assert taken["state"] == "done"
        assert taken["outcome"].data["count"] == 0
        # The result is released after the read: a long session cannot accumulate.
        assert store.describe(handle["jobId"]) is None

    def test_a_failed_job_reports_the_error(self, monkeypatch):
        store = mcp.JobStore()

        def boom(namespace):
            raise ZoombieError("no such source")

        monkeypatch.setattr(cli, "_dispatch", boom)
        handle = store.start("items", {})
        described = store.wait(handle["jobId"], timeout=10)
        assert described["state"] == "failed"
        assert described["error"] == "no such source"
        taken = store.take(handle["jobId"])
        assert taken["state"] == "failed"

    def test_status_before_completion_reports_running(self, monkeypatch):
        import threading

        release = threading.Event()
        store = mcp.JobStore()

        def slow(namespace):
            release.wait(timeout=10)
            from zoombie.cli import Outcome

            return Outcome(ok=True, data={"done": True})

        monkeypatch.setattr(cli, "_dispatch", slow)
        handle = store.start("items", {})
        assert store.describe(handle["jobId"])["state"] == "running"
        release.set()
        assert store.wait(handle["jobId"], timeout=10)["state"] == "done"

    def test_an_unknown_job_is_job_not_found(self):
        _code, responses, _text = _serve([
            _request(1, "zoombie/job/status", {"jobId": "nope-1"})
        ])
        assert responses[0]["error"]["code"] == mcp.JOB_NOT_FOUND

    def test_result_on_an_unknown_job_is_job_not_found(self):
        _code, responses, _text = _serve([
            _request(1, "zoombie/job/result", {"jobId": "nope-1"})
        ])
        assert responses[0]["error"]["code"] == mcp.JOB_NOT_FOUND

    def test_start_rejects_an_unknown_tool(self):
        _code, responses, _text = _serve([
            _request(1, "zoombie/job/start", {"tool": "nope"})
        ])
        assert responses[0]["error"]["code"] == mcp.TOOL_NOT_FOUND

    def test_start_requires_a_tool(self):
        _code, responses, _text = _serve([_request(1, "zoombie/job/start", {})])
        assert responses[0]["error"]["code"] == mcp.INVALID_PARAMS

    def test_the_short_names_alias_the_namespaced_ones(self, monkeypatch):
        import threading

        release = threading.Event()

        def slow(namespace):
            release.wait(timeout=10)
            from zoombie.cli import Outcome

            return Outcome(ok=True, data={})

        monkeypatch.setattr(cli, "_dispatch", slow)
        _code, responses, _text = _serve([_request(1, "start", {"tool": "items"})])
        job_id = responses[0]["result"]["jobId"]
        release.set()
        _code, responses, _text = _serve([_request(2, "status", {"id": job_id})])
        assert responses[0]["result"]["jobId"] == job_id

    def test_a_job_keeps_the_attach_cap(self, tmp_path, monkeypatch):
        """A long stage's result is shaped by the same cap as an inline call."""
        from zoombie.cli import Outcome

        entries = _attachable(tmp_path, 12)

        monkeypatch.setattr(cli, "_dispatch", lambda ns: Outcome(ok=True, data={"next": {"attach": entries}}))
        store = mcp.JobStore()
        handle = store.start("slides", {"source": "deck.mp4"})
        store.wait(handle["jobId"], timeout=10)
        payload = store.take(handle["jobId"])["outcome"]
        shaped = mcp.tool_result("slides", payload)
        images = [block for block in shaped["content"] if block.get("type") == "image"]
        assert len(images) == next_mod.DEFAULT_ATTACH_CAP


# --------------------------------------------------------------------------- #
# 9. facets -- read-only views that reuse lib/next and lib/scratch
# --------------------------------------------------------------------------- #

def _make_item(root) -> str:
    """A minimal genuine item: a folder carrying ``.data/``."""
    item = root / "1 - 2020-05-06 - Пример"
    (item / ".data" / "img").mkdir(parents=True)
    return str(item)


class TestFacets:
    def test_status_reports_the_resolved_interpreter(self):
        status = mcp.facet_status()
        assert status["server"]["name"] == "zoombie"
        assert status["interpreter"]["path"]
        # The §11 contradiction, made visible: the resolved interpreter is the
        # system Python, and there is no <root>\python.
        assert status["interpreter"]["same"] is True

    def test_status_reuses_the_scratch_inventory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZOOMBIE_ENV_ROOT", str(tmp_path / "env"))
        (tmp_path / "env" / "work" / "0123456789abcdef0123456789abcdef").mkdir(parents=True)
        (tmp_path / "env" / "work" / "my-notes").mkdir()
        status = mcp.facet_status()
        assert status["scratch"]["ownedCount"] == 1
        assert status["scratch"]["foreignCount"] == 1
        assert status["scratch"]["cleanScratchVerb"] == "clean -CleanScratch"

    def test_status_survives_a_missing_ocr_engine(self, monkeypatch):
        from zoombie.lib import ocr

        def boom():
            raise RuntimeError("no engine")

        monkeypatch.setattr(ocr, "available", boom)
        status = mcp.facet_status()
        assert status["ocr"]["available"] is False
        assert "no engine" in status["ocr"]["detail"]

    def test_next_on_an_item_recommends_postprocess(self, tmp_path):
        item = _make_item(tmp_path)
        _attachable(os.path.join(item, ".data", "img"), 3)
        block = mcp.facet_next(item)
        assert block["next"]["command"] == "postprocess"
        assert block["images"]["count"] == 3
        assert block["asciiSafe"] is True

    def test_next_caps_the_attach_list_by_reusing_next_build(self, tmp_path):
        item = _make_item(tmp_path)
        _attachable(os.path.join(item, ".data", "img"), 12)
        block = mcp.facet_next(item)
        assert len(block["next"]["attach"]) == next_mod.DEFAULT_ATTACH_CAP
        assert block["next"]["truncated"] is True
        assert len(block["next"]["overAttach"]) == 4
        assert block["next"]["count"] == 12

    def test_reading_copies_are_advertised_but_do_not_change_the_frame_count(self, tmp_path):
        """Plan §12: the JPEG copies live in ``readings/`` and are invisible to the
        PNG enumeration, yet ``next.attach`` advertises them (with JPEG bytes)."""
        item = _make_item(tmp_path)
        img = os.path.join(item, ".data", "img")
        _attachable(img, 2)
        readings = os.path.join(img, "readings")
        os.makedirs(readings)
        name = sorted(os.listdir(img))[0]
        stem = os.path.splitext(name)[0]
        (tmp_path / "copy.bin").write_bytes(b"x" * 300)
        import shutil

        shutil.copyfile(str(tmp_path / "copy.bin"), os.path.join(readings, f"{stem}.q3.jpg"))

        block = mcp.facet_next(item)
        # The PNG count is unchanged by the copies.
        assert block["images"]["count"] == 2
        advertised = {entry["file"]: entry for entry in block["next"]["attach"]}
        copy_entry = advertised[name]
        assert copy_entry["path"].endswith(".q3.jpg")
        assert copy_entry["bytes"] == 300

    def test_next_reads_the_ocr_artifact(self, tmp_path):
        item = _make_item(tmp_path)
        with open(os.path.join(item, ".data", "ocr.json"), "w", encoding="utf-8") as handle:
            json.dump({"used": True, "frames": [{"file": "001.png", "chars": 120}]}, handle)
        block = mcp.facet_next(item)
        assert block["ocr"]["used"] is True
        assert block["ocr"]["frames"] == 1
        assert block["ocr"]["artifact"].endswith("ocr.json")

    def test_next_refuses_a_non_item(self, tmp_path):
        plain = tmp_path / "not-an-item"
        plain.mkdir()
        with pytest.raises(ValueError):
            mcp.facet_next(str(plain))

    def test_next_refuses_a_missing_folder(self, tmp_path):
        with pytest.raises(ValueError):
            mcp.facet_next(str(tmp_path / "absent"))

    def test_next_requires_a_path(self):
        with pytest.raises(ValueError):
            mcp.facet_next("")

    def test_a_bad_facet_argument_is_invalid_params_not_a_crash(self):
        _code, responses, _text = _serve([
            _request(1, "zoombie/facets/next", {"item": "Z:/definitely/absent"})
        ])
        assert responses[0]["error"]["code"] == mcp.INVALID_PARAMS

    def test_facets_clean_dry_run_removes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZOOMBIE_ENV_ROOT", str(tmp_path / "env"))
        owned = tmp_path / "env" / "work" / "0123456789abcdef0123456789abcdef"
        owned.mkdir(parents=True)
        _code, responses, _text = _serve([
            _request(1, "zoombie/facets/clean", {"dryRun": True})
        ])
        assert responses[0]["result"]["dryRun"] is True
        assert owned.exists(), "-DryRun must not delete"

    def test_facets_clean_removes_only_owned(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZOOMBIE_ENV_ROOT", str(tmp_path / "env"))
        owned = tmp_path / "env" / "work" / "0123456789abcdef0123456789abcdef"
        foreign = tmp_path / "env" / "work" / "my-notes"
        owned.mkdir(parents=True)
        foreign.mkdir()
        _code, responses, _text = _serve([_request(1, "zoombie/facets/clean", {})])
        assert responses[0]["result"]["removedCount"] == 1
        assert not owned.exists()
        assert foreign.exists(), "a user folder in a root is never removed"

    def test_a_missing_required_flag_is_a_reported_tool_error(self):
        """argparse exits on a missing -Source; the facade must report, not exit."""
        _code, responses, _text = _serve([
            _request(1, "tools/call", {"name": "slides", "arguments": {}})
        ])
        assert "error" not in responses[0], "a bad argument is not a transport failure"
        payload = responses[0]["result"]
        assert payload["isError"] is True
        assert "source" in payload["data"]["error"].lower()


# --------------------------------------------------------------------------- #
# 10. the real entry point, as a real process
# --------------------------------------------------------------------------- #

class TestEntryPointAsProcess:
    def test_python_m_zoombie_mcp_round_trips_a_tool_call(self, tmp_path):
        """The launcher itself, spawned the way a client would.

        This is the ONE place a subprocess is correct: an MCP *client* spawns the
        server. It is not a tool call shelling out.
        """
        import subprocess

        scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(mcp.__file__)))
        lines = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "items", "arguments": {"root": str(tmp_path), "json": True}}}),
        ]) + "\n"
        completed = subprocess.run(
            [sys.executable, "-m", "zoombie.mcp"],
            input=lines, cwd=scripts_dir, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": scripts_dir},
        )
        assert completed.returncode == 0
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        assert len(responses) == 2
        assert responses[0]["result"]["serverInfo"]["name"] == "zoombie"
        assert responses[1]["result"]["data"]["action"] == "items"
        # Progress went to stderr, so stdout is pure protocol.
        assert "ready" in completed.stderr

    def test_version_flag_prints_to_stderr(self):
        import subprocess

        scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(mcp.__file__)))
        completed = subprocess.run(
            [sys.executable, "-m", "zoombie.mcp", "--version"],
            cwd=scripts_dir, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": scripts_dir},
        )
        assert completed.returncode == 0
        assert "zoombie-mcp" in completed.stderr
        assert completed.stdout == ""

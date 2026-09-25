"""Tests for registering the MCP server in the client's MCP settings.

The property that matters is SHARED OWNERSHIP, the same one ``lib/modes`` guards
for ``custom_modes.yaml``: ``mcp_settings.json`` is a file the user also owns and
may hold other servers, so our merge must replace exactly ``mcpServers.zoombie``
and carry every foreign key through untouched. A document we cannot understand is
refused, never silently repaired.
"""

from __future__ import annotations

import json

import pytest

from zoombie.lib import mcpsettings


class TestTargetResolution:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        target = tmp_path / "mcp_settings.json"
        monkeypatch.setenv("ZOOMBIE_MCP_SETTINGS_PATH", str(target))
        assert mcpsettings.global_settings_path() == str(target)

    def test_no_appdata_is_a_clear_failure(self, monkeypatch):
        monkeypatch.delenv("ZOOMBIE_MCP_SETTINGS_PATH", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        with pytest.raises(RuntimeError):
            mcpsettings.global_settings_path()


class TestServerEntry:
    def test_entry_spawns_the_module_not_cmd(self):
        entry = mcpsettings.server_entry("C:/python/python.exe", "C:/bin/zoombie")
        assert entry["command"] == "C:/python/python.exe"
        assert entry["args"] == ["-m", "zoombie.mcp"]
        # A .cmd shim is deliberately NOT used: the client spawns the interpreter.
        assert not any(token.lower().endswith(".cmd") for token in entry["args"])
        assert entry["env"]["PYTHONPATH"] == "C:/bin/zoombie"
        assert entry["env"]["PYTHONUTF8"] == "1"
        assert entry["disabled"] is False
        assert isinstance(entry["timeout"], int) and entry["timeout"] >= 60

    def test_no_python_is_refused(self, monkeypatch):
        monkeypatch.setattr(mcpsettings, "interpreter", lambda: None)
        with pytest.raises(RuntimeError):
            mcpsettings.server_entry(None, "C:/bin/zoombie")


class TestMerge:
    def _entry(self):
        return mcpsettings.server_entry("C:/python/python.exe", "C:/bin/zoombie")

    def test_empty_document_creates(self):
        text, action = mcpsettings.merge("", self._entry())
        assert action == "created"
        document = json.loads(text)
        assert document["mcpServers"]["zoombie"]["command"] == "C:/python/python.exe"

    def test_foreign_servers_are_preserved(self):
        existing = json.dumps({
            "mcpServers": {
                "other": {"command": "other.exe", "args": ["--serve"]},
            }
        })
        text, action = mcpsettings.merge(existing, self._entry())
        assert action == "created"
        document = json.loads(text)
        assert document["mcpServers"]["other"] == {"command": "other.exe", "args": ["--serve"]}
        assert document["mcpServers"]["zoombie"]["command"] == "C:/python/python.exe"

    def test_other_top_level_keys_are_preserved(self):
        existing = json.dumps({"mcpServers": {}, "theme": "dark"})
        text, _action = mcpsettings.merge(existing, self._entry())
        assert json.loads(text)["theme"] == "dark"

    def test_rerun_is_up_to_date_and_byte_identical(self):
        first, action = mcpsettings.merge("", self._entry())
        assert action == "created"
        second, action = mcpsettings.merge(first, self._entry())
        assert action == "up to date"
        assert second == first

    def test_a_changed_entry_is_updated(self):
        first, _ = mcpsettings.merge("", self._entry())
        entry = self._entry()
        entry["command"] = "D:/python/python.exe"
        _text, action = mcpsettings.merge(first, entry)
        assert action == "updated"

    def test_invalid_json_is_refused(self):
        with pytest.raises(RuntimeError):
            mcpsettings.merge("{ this is not json", self._entry())

    def test_a_non_object_servers_map_is_refused(self):
        with pytest.raises(RuntimeError):
            mcpsettings.merge(json.dumps({"mcpServers": [1, 2, 3]}), self._entry())

    def test_a_missing_servers_key_is_added(self):
        text, action = mcpsettings.merge(json.dumps({"theme": "dark"}), self._entry())
        assert action == "created"
        assert json.loads(text)["mcpServers"]["zoombie"]


class TestDeploy:
    def test_dry_run_writes_nothing(self, tmp_path):
        target = tmp_path / "mcp_settings.json"
        record = mcpsettings.deploy(
            str(target), dry_run=True,
            interpreter_path="C:/python/python.exe", package="C:/bin/zoombie",
        )
        assert record["action"] == "created"
        assert not target.exists()

    def test_apply_writes_and_is_idempotent(self, tmp_path):
        target = tmp_path / "mcp_settings.json"
        first = mcpsettings.deploy(
            str(target), interpreter_path="C:/python/python.exe", package="C:/bin/zoombie"
        )
        assert first["action"] == "created" and target.is_file()
        second = mcpsettings.deploy(
            str(target), interpreter_path="C:/python/python.exe", package="C:/bin/zoombie"
        )
        assert second["action"] == "up to date"

    def test_safe_wrapper_reports_a_failure_instead_of_raising(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcpsettings, "interpreter", lambda: None)
        record = mcpsettings.deploy_safe(str(tmp_path / "mcp_settings.json"))
        assert record["action"] == "failed"
        assert record["note"]

    def test_report_sees_a_registered_server(self, tmp_path, monkeypatch):
        target = tmp_path / "mcp_settings.json"
        monkeypatch.setenv("ZOOMBIE_MCP_SETTINGS_PATH", str(target))
        mcpsettings.deploy(
            str(target), interpreter_path="C:/python/python.exe", package="C:/bin/zoombie"
        )
        report = mcpsettings.report()
        assert report["registered"] is True
        assert report["installed"]["command"] == "C:/python/python.exe"


class TestGlobalRegistration:
    """The server is a GLOBAL entry; its presence is confirmed from the file."""

    def test_is_registered_reads_the_file_not_the_action(self, tmp_path):
        target = tmp_path / "mcp_settings.json"
        assert mcpsettings.is_registered(str(target)) is False
        mcpsettings.deploy(
            str(target), interpreter_path="C:/python/python.exe", package="C:/bin/zoombie"
        )
        assert mcpsettings.is_registered(str(target)) is True

    def test_is_registered_false_for_a_foreign_only_file(self, tmp_path):
        target = tmp_path / "mcp_settings.json"
        target.write_text(
            json.dumps({"mcpServers": {"other": {"command": "x.exe"}}}), encoding="utf-8"
        )
        assert mcpsettings.is_registered(str(target)) is False

    def test_deploy_safe_confirms_global_registration(self, tmp_path):
        target = tmp_path / "mcp_settings.json"
        record = mcpsettings.deploy_safe(
            str(target), interpreter_path="C:/python/python.exe", package="C:/bin/zoombie"
        )
        assert record["registered"] is True
        assert mcpsettings.is_registered(str(target)) is True

    def test_deploy_safe_reports_registered_false_on_a_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcpsettings, "interpreter", lambda: None)
        record = mcpsettings.deploy_safe(str(tmp_path / "mcp_settings.json"))
        assert record["action"] == "failed"
        assert record["registered"] is False

    def test_report_declares_the_global_scope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOOMBIE_MCP_SETTINGS_PATH", str(tmp_path / "mcp_settings.json"))
        assert mcpsettings.report()["scope"] == "global"

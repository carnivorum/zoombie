"""Tests for the top-level CLI: argument parsing helpers.

The ``@file`` convention is the escape hatch for a non-ASCII argument on Windows.
A Cyrillic value passed INLINE through the console is mangled by the code page,
so the caller writes it to a UTF-8 file and passes ``@path``; Python reads the
bytes back, never touching the console code page.
"""

from __future__ import annotations

from zoombie import cli


class TestExpandArgFiles:
    def test_a_file_value_replaces_the_at_token(self, tmp_path):
        value = tmp_path / "itemname.txt"
        value.write_text("Могилко-телеграм-канал\n", encoding="utf-8")
        argv = ["pipeline", "-Source", "@" + str(value), "-Output", "out"]
        assert cli.expand_arg_files(argv) == [
            "pipeline", "-Source", "Могилко-телеграм-канал", "-Output", "out",
        ]

    def test_a_trailing_newline_is_stripped(self, tmp_path):
        value = tmp_path / "v.txt"
        value.write_text("  spaced value  \n", encoding="utf-8")
        assert cli.expand_arg_files(["@" + str(value)]) == ["spaced value"]

    def test_a_utf8_bom_does_not_leak_into_the_value(self, tmp_path):
        """A Notepad-saved file starts with a BOM; it must not reach the path."""
        value = tmp_path / "bom.txt"
        value.write_bytes("\ufeffПуть".encode("utf-8"))
        assert cli.expand_arg_files(["@" + str(value)]) == ["Путь"]

    def test_a_missing_file_is_left_verbatim(self, tmp_path):
        """A leading ``@`` is also a legal file name, so a miss is not an error."""
        token = "@" + str(tmp_path / "absent.txt")
        assert cli.expand_arg_files([token]) == [token]

    def test_a_bare_at_is_untouched(self):
        assert cli.expand_arg_files(["@"]) == ["@"]

    def test_an_at_inside_a_value_is_untouched(self, tmp_path):
        """An email or URL userinfo must never be read as a file reference."""
        token = "user@example.com"
        assert cli.expand_arg_files([token]) == [token]

    def test_expansion_reaches_the_parser(self, tmp_path, capsys):
        """The @file form works end to end for a real subcommand's argument."""
        value = tmp_path / "root.txt"
        value.write_text(str(tmp_path), encoding="utf-8")
        assert cli.main(["items", "-Root", "@" + str(value), "-Json"]) == 0


class TestResultLineEncoding:
    """The result line must survive a console code page that cannot encode it.

    The live test found this the hard way: a remote video's title carries U+29F8,
    a legal codepoint cp1251 has no mapping for. The work completed, and then
    ``write_result`` raised ``UnicodeEncodeError`` on the way out -- emitting NO
    result line at all, which breaks the one-line-per-invocation contract and
    leaves a caller nothing to read.
    """

    def test_an_unencodable_codepoint_still_produces_a_line(self, monkeypatch):
        """Written as UTF-8 bytes, so the code page is never consulted."""
        import io
        import json

        from zoombie.lib import process

        raw = io.BytesIO()
        stdout = io.TextIOWrapper(raw, encoding="cp1251", errors="strict")
        monkeypatch.setattr(process.sys, "stdout", stdout)

        exotic = "Гаттака \u29f8 Gattaca"
        process.write_result("summarize", ok=True, data={"title": exotic})

        stdout.flush()
        decoded = json.loads(raw.getvalue().decode("utf-8"))
        assert decoded["ok"] is True
        assert decoded["data"]["title"] == exotic

    def test_a_replaced_stdout_without_a_buffer_still_writes(self, monkeypatch):
        """A capture object has no ``buffer``; the fallback must not raise."""
        import io
        import json

        from zoombie.lib import process

        capture = io.StringIO()
        monkeypatch.setattr(process.sys, "stdout", capture)
        process.write_result("items", ok=True, data={"title": "Гаттака \u29f8"})

        assert json.loads(capture.getvalue())["action"] == "items"

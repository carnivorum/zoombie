"""Tests for ``data.next`` -- the recommended-next-step block and its CAP.

The enforcement is the point (plan §9), so the central assertions here are about
the cap: a result may not advertise more than ``DEFAULT_ATTACH_CAP`` attachable
images, the excess must be *reported by name* rather than silently dropped, and the
cap must hold in ``-DryRun`` too (because that is what an agent plans its reads
from). The shape assertions are secondary but cheap: every result carries the same
block, so a skill never has to branch on "is there a next at all".
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from zoombie import cli
from zoombie.lib import next as next_mod


def _png(width: int = 4, height: int = 4) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


# --------------------------------------------------------------------------- #
# the cap: the reason this module exists
# --------------------------------------------------------------------------- #

class TestCapEnforcement:
    def test_the_cap_truncates_and_names_the_excess(self):
        entries = [{"file": f"f{i}.png"} for i in range(11)]
        selected = next_mod.select(entries, 8)
        assert len(selected["attach"]) == 8
        assert selected["truncated"] is True
        assert selected["dropped"] == 3
        # The excess is PRESERVED, not deleted: a programmatic consumer (or the
        # later MCP facade) still has to be able to reach every frame.
        assert [entry["file"] for entry in selected["overAttach"]] == [
            "f8.png", "f9.png", "f10.png"
        ]

    def test_a_list_at_or_below_the_cap_is_untouched(self):
        entries = [{"file": f"f{i}.png"} for i in range(8)]
        selected = next_mod.select(entries, 8)
        assert len(selected["attach"]) == 8
        assert selected["truncated"] is False
        assert selected["overAttach"] == []

    def test_a_zero_cap_defers_everything(self):
        selected = next_mod.select([{"file": "a.png"}], 0)
        assert selected["attach"] == []
        assert selected["dropped"] == 1

    def test_entries_are_copied_not_aliased(self):
        """Mutating the block must not reach back into the caller's list."""
        original = {"file": "a.png"}
        block = next_mod.build("verify", attachable=[original])
        block["attach"][0]["file"] = "changed.png"
        assert original["file"] == "a.png"

    def test_build_reports_the_truncation_as_a_named_reason(self):
        block = next_mod.build(
            "postprocess", {"-Md": "summary.md"}, why="w",
            attachable=[{"file": f"f{i}.png"} for i in range(12)],
        )
        assert block["truncated"] is True
        assert block["attachCap"] == next_mod.DEFAULT_ATTACH_CAP
        assert block["attachCount"] == next_mod.DEFAULT_ATTACH_CAP
        # The FULL count, so the caller can tell 12 available from 8 attachable.
        assert block["count"] == 12
        assert len(block["overAttach"]) == 4
        # A refusal is REPORTED: which cap, and by how much.
        assert "12" in block["reason"] and "8" in block["reason"]
        assert block["reason"] == "; ".join(block["overBudgetReasons"])

    def test_build_applies_the_cap_itself_not_the_caller(self):
        """The whole point: the cap is not a convention the caller may skip."""
        block = next_mod.build(
            "postprocess", None,
            attachable=[{"file": f"f{i}.png"} for i in range(96)],
        )
        assert len(block["attach"]) <= next_mod.DEFAULT_ATTACH_CAP
        assert len(block["attach"]) + len(block["overAttach"]) == 96

    def test_args_capped_reports_a_narrowing_and_never_a_bypass(self):
        block = next_mod.build(
            "slides", {"-Times": "00:01:28"}, why="w",
            attachable=[{"file": f"f{i}.png"} for i in range(9)],
            args_capped=True,
        )
        assert len(block["overBudgetReasons"]) == 2
        assert "NARROW" in block["reason"]
        # There is no wording that offers to switch the cap off: it is a transport
        # guarantee, so "-Force makes the cap inert" would be a false promise.
        assert "inert" not in block["reason"]

    def test_the_cap_cannot_be_bypassed_by_force(self):
        """``build`` has no force parameter at all -- the cap is not switchable."""
        import inspect

        assert "force" not in inspect.signature(next_mod.build).parameters
        assert "force" not in inspect.signature(next_mod.select).parameters

    def test_build_never_refuses_a_terminal_result(self):
        """A terminal step still emits the block, with a full shape and no attach."""
        block = next_mod.build(None, None, why="nothing left to do", attachable=[])
        for key in ("command", "args", "why", "attach", "budget",
                    "attachCap", "attachCount", "count", "truncated",
                    "overAttach", "reason", "writes"):
            assert key in block
        assert block["command"] is None
        assert block["attach"] == []
        assert block["truncated"] is False
        assert block["writes"] is False


class TestBudget:
    def test_bytes_sums_the_files_that_are_really_there(self, tmp_path):
        one = tmp_path / "1.png"
        two = tmp_path / "2.png"
        one.write_bytes(_png())
        two.write_bytes(_png())
        block = next_mod.build(
            "postprocess", None,
            attachable=[{"file": "1.png", "path": str(one)},
                        {"file": "2.png", "path": str(two)}],
        )
        assert block["budget"]["images"] == 2
        assert block["budget"]["bytes"] == one.stat().st_size + two.stat().st_size

    def test_a_missing_path_contributes_nothing(self, tmp_path):
        block = next_mod.build(
            "postprocess", None,
            attachable=[{"file": "gone.png", "path": str(tmp_path / "gone.png")}],
        )
        assert block["budget"] == {"images": 1, "bytes": 0}

    def test_a_measured_byte_count_is_not_restatted(self, tmp_path):
        """A command that already measured a frame hands the number over."""
        block = next_mod.build(
            "postprocess", None,
            attachable=[{"file": "x.png", "path": str(tmp_path / "absent.png"),
                         "bytes": 4242}],
        )
        assert block["budget"]["bytes"] == 4242


class TestCapOf:
    def test_the_flag_raises_the_cap(self):
        args = argparse.Namespace(attach_limit=2)
        assert next_mod.cap_of(args) == 2

    def test_absent_or_unusable_values_fall_back_to_the_default(self):
        assert next_mod.cap_of(argparse.Namespace()) == next_mod.DEFAULT_ATTACH_CAP
        assert next_mod.cap_of(argparse.Namespace(attach_limit=None)) == next_mod.DEFAULT_ATTACH_CAP
        assert next_mod.cap_of(argparse.Namespace(attach_limit=0)) == next_mod.DEFAULT_ATTACH_CAP
        assert next_mod.cap_of(argparse.Namespace(attach_limit="x")) == next_mod.DEFAULT_ATTACH_CAP


# --------------------------------------------------------------------------- #
# the command surfaces
# --------------------------------------------------------------------------- #

def _readimages_args(**overrides) -> argparse.Namespace:
    values = {
        "source": None, "output": None, "ocr": False, "lang": "eng",
        "image_dir": None, "dry_run": False, "force": False, "attach_limit": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class TestReadimagesNext:
    def _folder(self, tmp_path, count: int) -> str:
        images = tmp_path / "in"
        images.mkdir(exist_ok=True)
        for index in range(count):
            (images / f"{index + 1:03d}.png").write_bytes(_png(6, 6))
        return str(images)

    def test_the_vision_hand_off_is_capped(self, tmp_path):
        from zoombie.commands import readimages

        images = self._folder(tmp_path, 12)
        outcome = readimages.run(
            _readimages_args(source=images, output=str(tmp_path / "doc"))
        )
        block = outcome.data["next"]
        # The next step is an AGENT action (read, then hand off), not a CLI verb:
        # recommending ``readimages`` again would be a pointless self-loop.
        assert block["command"] is None
        # 12 images, 8 attachable -- the cap is enforced by the CLI, not requested.
        assert len(block["attach"]) == next_mod.DEFAULT_ATTACH_CAP
        assert block["count"] == 12
        assert block["truncated"] is True
        assert len(block["overAttach"]) == 4
        # Every advertised path really exists: the block is executable.
        assert all(os.path.isfile(entry["path"]) for entry in block["attach"])
        assert block["budget"]["bytes"] > 0

    def test_an_explicit_limit_narrows_the_attach_list(self, tmp_path):
        from zoombie.commands import readimages

        images = self._folder(tmp_path, 12)
        outcome = readimages.run(
            _readimages_args(source=images, output=str(tmp_path / "doc"),
                             attach_limit=3)
        )
        block = outcome.data["next"]
        assert len(block["attach"]) == 3
        assert block["attachCap"] == 3
        assert len(block["overAttach"]) == 9

    def test_ocr_removes_the_attach_list(self, tmp_path, monkeypatch):
        """With the text in the document there is nothing left to look at."""
        from zoombie.commands import readimages

        images = self._folder(tmp_path, 2)
        monkeypatch.setattr(readimages.ocr, "available", lambda: (True, "x"))
        monkeypatch.setattr(readimages.ocr, "ocr_image", lambda _path, _lang: "text")
        outcome = readimages.run(
            _readimages_args(source=images, output=str(tmp_path / "doc"), ocr=True)
        )
        assert outcome.data["next"]["attach"] == []

    def test_dry_run_is_capped_but_writes_nothing(self, tmp_path, monkeypatch):
        from zoombie.commands import readimages

        images = self._folder(tmp_path, 12)
        base = tmp_path / "doc"
        outcome = readimages.run(
            _readimages_args(source=images, output=str(base), dry_run=True)
        )
        # The cap is applied to the PLANNED file list...
        assert outcome.data["filesTruncated"] is True
        assert len(outcome.data["files"]) == next_mod.DEFAULT_ATTACH_CAP
        # ...but nothing was written, so nothing may be attached.
        assert outcome.data["next"]["attach"] == []
        assert not (tmp_path / "doc.md").exists()
        assert not (tmp_path / "doc.images").exists()


class TestSlidesNext:
    """``slides -DryRun`` needs no ffmpeg, which is why the cap is proved here."""

    def _args(self, source: str, output: str, **overrides) -> argparse.Namespace:
        values = {
            "source": source, "output": output, "image_dir": None, "times": None,
            "times_file": None, "srt": None, "scale": 1280, "sample_rate": 0.25,
            "diff_threshold": 3.0, "hash_distance": 8, "min_slide_seconds": 2.0,
            "sample_interval": 30.0, "min_px": 64, "min_frame_bytes": 150_000,
            "min_text_chars": 12, "no_text_gate": False, "lang": None,
            "dry_run": True, "force": False, "keep_work": False, "work_root": None,
            "attach_limit": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def _run(self, tmp_path, monkeypatch):
        from zoombie.commands import slides as slides_cmd

        source = tmp_path / "media.mp4"
        source.write_bytes(b"\x00" * 32)
        output = tmp_path / "item"
        output.mkdir()

        class _Env:
            ffprobe = None

            def require(self, *_args, **_kwargs):
                return "ffmpeg"

        monkeypatch.setattr(slides_cmd.env_mod, "resolve", lambda: _Env())
        return slides_cmd, slides_cmd.run(self._args(str(source), str(output)))

    def test_dry_run_emits_a_well_formed_next_and_writes_nothing(self, tmp_path, monkeypatch):
        _cmd, outcome = self._run(tmp_path, monkeypatch)
        block = outcome.data["next"]
        assert outcome.data["dryRun"] is True
        assert block["command"] == "postprocess"
        assert block["attach"] == []          # nothing extracted -> nothing to read
        assert block["truncated"] is False
        assert block["attachCap"] == next_mod.DEFAULT_ATTACH_CAP
        assert list((tmp_path / "item").iterdir()) == []

    def test_dry_run_honours_an_explicit_limit(self, tmp_path, monkeypatch):
        """The cap is inert with no frames, but the reported cap follows the flag."""
        from zoombie.commands import slides as slides_cmd

        source = tmp_path / "media.mp4"
        source.write_bytes(b"\x00" * 32)
        output = tmp_path / "item"
        output.mkdir()

        class _Env:
            ffprobe = None

            def require(self, *_args, **_kwargs):
                return "ffmpeg"

        monkeypatch.setattr(slides_cmd.env_mod, "resolve", lambda: _Env())
        outcome = slides_cmd.run(self._args(str(source), str(output), attach_limit=3))
        assert outcome.data["next"]["attachCap"] == 3


class TestPostprocessNext:
    def _doc(self, tmp_path) -> str:
        path = tmp_path / "summary.md"
        path.write_text(
            "# T\n\n## 1. Титул\n\n## 2. Источник\n\n## 3. Сводка\n\n"
            "## 4. Содержание\n\n## 5. Связанные\n\n## 6. Полный текст источника\n",
            encoding="utf-8",
        )
        return str(path)

    def test_the_terminal_step_recommends_verify(self, tmp_path):
        from zoombie.commands import postprocess

        doc = self._doc(tmp_path)
        args = argparse.Namespace(md=doc, dir=None, recurse=False, srt=None,
                                 image_dir=None, apply=True, report=None,
                                 attach_limit=None)
        outcome = postprocess.run(args)
        block = outcome.data["next"]
        # postprocess produces no images, so the attach list is empty by
        # construction -- but the block is present and shape-complete.
        assert block["attach"] == []
        assert block["command"] == "verify"
        assert block["args"] == {"-Dir": str(tmp_path)}
        assert block["writes"] is False

    def test_a_run_that_changes_nothing_has_no_next_command(self, tmp_path):
        """Idempotency: an already-canonical document has no work left to hand on."""
        from zoombie.commands import postprocess

        doc = self._doc(tmp_path)
        base = dict(md=doc, dir=None, recurse=False, srt=None, image_dir=None,
                    report=None, attach_limit=None)
        # First pass canonicalises the document (anchors, whitespace) and reports
        # a change, so it DOES recommend the next step.
        first = postprocess.run(argparse.Namespace(**base, apply=True))
        assert first.data["changed"] == 1
        assert first.data["next"]["command"] == "verify"
        # Second pass is byte-identical -> nothing changed -> nothing left to do.
        second = postprocess.run(argparse.Namespace(**base, apply=False))
        assert second.data["dryRun"] is True
        assert second.data["changed"] == 0
        assert second.data["next"]["command"] is None
        # The block is still complete, only the command is absent.
        assert second.data["next"]["attach"] == []


class TestEnvelopeUnchanged:
    def test_data_next_is_an_addition_inside_data_not_a_new_envelope(self, tmp_path, capsys):
        """Plan §14: the envelope shape is unchanged; ``next`` lives inside data."""
        images = tmp_path / "in"
        images.mkdir()
        (images / "001.png").write_bytes(_png())

        code = cli.main(["readimages", "-Source", str(images), "-Output",
                         str(tmp_path / "doc"), "-DryRun"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert list(payload.keys()) == ["ok", "action", "error", "data", "timestamp"]
        assert payload["action"] == "readimages"
        assert "next" in payload["data"]


class TestAttachLimitFlag:
    """Each pipeline command takes its own required argument, not ``-Source``."""

    _SOURCE_COMMANDS = [
        ["slides", "-Source", "x"],
        ["readimages", "-Source", "x"],
        ["readpdf", "-Source", "x"],
    ]

    @pytest.mark.parametrize("argv", [
        ["slides", "-Source", "x"],
        ["readimages", "-Source", "x"],
        ["readpdf", "-Source", "x"],
        ["postprocess", "-Md", "x.md"],
    ])
    def test_the_pipeline_commands_expose_the_flag(self, argv):
        args = cli.build_parser().parse_args([*argv, "-AttachLimit", "4"])
        assert args.attach_limit == 4

    @pytest.mark.parametrize("argv", [
        ["slides", "-Source", "x"],
        ["readimages", "-Source", "x"],
        ["readpdf", "-Source", "x"],
        ["postprocess", "-Md", "x.md"],
    ])
    def test_the_default_is_the_library_cap(self, argv):
        args = cli.build_parser().parse_args(argv)
        assert next_mod.cap_of(args) == next_mod.DEFAULT_ATTACH_CAP

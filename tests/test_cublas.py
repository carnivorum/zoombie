"""Tests for cuBLAS major parsing, provisioning table and runtime readiness."""

from __future__ import annotations

from zoombie.lib import cublas


class TestMajorFromAssetName:
    def test_cublas_asset_parses_the_major(self):
        assert cublas.major_from_asset_name("whisper-cublas-11.8.0-bin-x64.zip") == 11

    def test_cublas_12_asset_parses_12(self):
        assert cublas.major_from_asset_name("whisper-cublas-12.4.0-bin-x64.zip") == 12

    def test_cuda_named_asset_parses_the_major(self):
        assert cublas.major_from_asset_name("whisper-cuda-11.8-bin.zip") == 11

    def test_cpu_asset_has_no_major(self):
        assert cublas.major_from_asset_name("whisper-bin-x64.zip") is None

    def test_empty_name(self):
        assert cublas.major_from_asset_name(None) is None
        assert cublas.major_from_asset_name("") is None


class TestProvisions:
    def test_major_11_is_pinned(self):
        spec = cublas.provision_spec(11)
        assert spec is not None
        assert spec["major"] == 11
        assert "cublas64_11.dll" in spec["dlls"]
        assert "cublasLt64_11.dll" in spec["dlls"]

    def test_major_12_is_not_pinned(self):
        # Refusing is the point: an unverified native DLL must never be installed.
        assert cublas.provision_spec(12) is None

    def test_default_major_is_11(self):
        assert cublas.DEFAULT_MAJOR == 11
        assert cublas.provision_spec(0) is not None

    def test_supported_majors_names_11(self):
        assert 11 in cublas.supported_majors()

    def test_every_row_has_a_published_hash(self):
        # A row without a sha256 could not be verified before installation.
        for major, spec in cublas.PROVISIONS.items():
            assert len(spec["sha256"]) == 64, f"major {major} has no usable sha256"
            assert spec["url"].startswith("https://")


class TestSha256:
    def test_hash_matches(self, tmp_path):
        target = tmp_path / "x.bin"
        target.write_bytes(b"abc")
        # sha256("abc"), independently known.
        assert cublas.sha256_file(str(target)) == (
            "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"
        )

    def test_verify_accepts_a_match(self, tmp_path):
        target = tmp_path / "x.bin"
        target.write_bytes(b"abc")
        digest = cublas.sha256_file(str(target))
        cublas.verify_archive(str(target), digest)

    def test_verify_rejects_a_mismatch(self, tmp_path):
        target = tmp_path / "x.bin"
        target.write_bytes(b"abc")
        try:
            cublas.verify_archive(str(target), "00" * 32)
        except RuntimeError as exc:
            assert "mismatch" in str(exc)
        else:
            raise AssertionError("a hash mismatch must raise")


class TestRuntimeStatus:
    def test_missing_directory_reports_dir_missing(self, tmp_path):
        status = cublas.runtime_status(str(tmp_path / "nope" / "whisper-cli.exe"))
        assert status.dir_missing is True
        # Not installed is NOT "every DLL is missing".
        assert status.missing == []
        assert status.ready is False

    def test_cpu_only_build_is_not_reported_missing(self, tmp_path):
        exe = tmp_path / "whisper-cli.exe"
        exe.write_bytes(b"MZ")
        status = cublas.runtime_status(str(exe))
        assert status.gpu_module is False
        assert status.missing == []
        assert status.ready is False

    def test_gpu_module_without_runtime_is_not_ready(self, tmp_path):
        exe = tmp_path / "whisper-cli.exe"
        exe.write_bytes(b"MZ")
        (tmp_path / "ggml-cuda.dll").write_bytes(b"MZ")
        status = cublas.runtime_status(str(exe), 11)
        assert status.ready is False
        assert "cublas64_11.dll" in status.missing

    def test_wrong_major_runtime_does_not_satisfy(self, tmp_path):
        exe = tmp_path / "whisper-cli.exe"
        exe.write_bytes(b"MZ")
        (tmp_path / "ggml-cuda.dll").write_bytes(b"MZ")
        (tmp_path / "cublas64_12.dll").write_bytes(b"MZ")
        status = cublas.runtime_status(str(exe), 11)
        assert status.ready is False
        assert "cublas64_11.dll" in status.missing

    def test_complete_runtime_is_ready(self, tmp_path):
        exe = tmp_path / "whisper-cli.exe"
        exe.write_bytes(b"MZ")
        (tmp_path / "ggml-cuda.dll").write_bytes(b"MZ")
        (tmp_path / "cublas64_11.dll").write_bytes(b"MZ")
        (tmp_path / "cublasLt64_11.dll").write_bytes(b"MZ")
        status = cublas.runtime_status(str(exe), 11)
        assert status.ready is True
        assert status.missing == []

    def test_major_inferred_from_present_dll(self, tmp_path):
        exe = tmp_path / "whisper-cli.exe"
        exe.write_bytes(b"MZ")
        (tmp_path / "ggml-cuda.dll").write_bytes(b"MZ")
        (tmp_path / "cublas64_12.dll").write_bytes(b"MZ")
        status = cublas.runtime_status(str(exe))
        # Inferred from what is present, so a re-run stays honest.
        assert status.cublas_major == 12
        assert status.ready is False
        assert "cublas64_12.dll" not in status.missing

    def test_report_shape(self, tmp_path):
        exe = tmp_path / "whisper-cli.exe"
        exe.write_bytes(b"MZ")
        report = cublas.runtime_status(str(exe)).to_report()
        for key in ("gpuModule", "dirMissing", "ready", "cublasMajor", "present", "missing", "warnings", "version"):
            assert key in report

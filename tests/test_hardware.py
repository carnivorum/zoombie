"""Tests for the backend chooser: capability AND benefit.

These are the regression tests for the field report's core ask: an integrated GPU
must NOT be selected merely because a display adapter exists. The chooser used to
be ``cuda > vulkan > cpu`` from capability alone, which put shared-memory iGPUs on
Vulkan and left non-NVIDIA machines re-downloading the CPU build every run.
"""

from __future__ import annotations

from zoombie.install import hardware


class TestClassifyAdapter:
    def test_intel_uhd_is_integrated(self):
        assert hardware._classify_adapter("Intel(R) UHD Graphics 620", 128) == "integrated"

    def test_intel_iris_xe_is_integrated(self):
        assert hardware._classify_adapter("Intel(R) Iris(R) Xe Graphics", None) == "integrated"

    def test_iris_xe_max_is_not_integrated(self):
        # A discrete-class part must NOT be misread as integrated: that would push
        # a real GPU onto the CPU.
        assert hardware._classify_adapter("Intel(R) Iris(R) Xe MAX Graphics", 4096) == "discrete"

    def test_arc_is_not_integrated(self):
        assert hardware._classify_adapter("Intel(R) Arc(TM) A770 Graphics", 8192) == "discrete"

    def test_amd_igpu_is_integrated(self):
        assert hardware._classify_adapter("AMD Radeon(TM) Graphics", None) == "integrated"

    def test_radeon_rx_with_vram_is_discrete(self):
        assert hardware._classify_adapter("AMD Radeon RX 6800", 16384) == "discrete"

    def test_unknown_name_without_vram_is_unknown(self):
        assert hardware._classify_adapter("Some Accelerator", None) == "unknown"


class TestDriverAge:
    def test_none_date_is_unknown(self):
        assert hardware._driver_age_months(None) is None

    def test_unparseable_date_is_unknown(self):
        assert hardware._driver_age_months("yesterday") is None

    def test_old_driver_is_many_months(self):
        age = hardware._driver_age_months("2000-01-01")
        assert age is not None and age > hardware.DRIVER_MAX_AGE_MONTHS

    def test_future_date_clamps_to_zero(self):
        assert hardware._driver_age_months("2999-01-01") == 0


class TestChooseBackend:
    def _profile(self, **kwargs):
        return hardware.Profile(**kwargs)

    def test_nvidia_wins_cuda(self):
        profile = self._profile(nvidia="NVIDIA GeForce RTX 4070", gpus=["NVIDIA GeForce RTX 4070"])
        hardware._choose_backend(profile, {})
        assert profile.backend == "cuda"
        assert profile.backend_reason and "NVIDIA" in profile.backend_reason

    def test_no_gpu_is_cpu(self):
        profile = self._profile(gpus=[])
        hardware._choose_backend(profile, {})
        assert profile.backend == "cpu"
        assert profile.backend_reason and "no GPU" in profile.backend_reason

    def test_integrated_gpu_stays_on_cpu(self):
        profile = self._profile(gpus=["Intel(R) Iris(R) Xe Graphics"])
        hardware._choose_backend(profile, {"Intel(R) Iris(R) Xe Graphics": {"vramMb": 128}})
        assert profile.backend == "cpu"
        assert profile.gpu_class == "integrated"
        assert profile.backend_reason and "Integrated" in profile.backend_reason

    def test_integrated_gpu_opt_in_selects_vulkan(self, monkeypatch):
        monkeypatch.setenv(hardware.ENV_ALLOW_IGPU, "1")
        profile = self._profile(gpus=["Intel(R) UHD Graphics 620"])
        hardware._choose_backend(profile, {"Intel(R) UHD Graphics 620": {"vramMb": 128}})
        assert profile.backend == "vulkan"
        assert profile.backend_reason and "request" in profile.backend_reason

    def test_discrete_gpu_below_vram_threshold_is_cpu(self):
        profile = self._profile(gpus=["AMD Radeon RX 6400"])
        hardware._choose_backend(profile, {"AMD Radeon RX 6400": {"vramMb": 1024}})
        assert profile.backend == "cpu"
        assert profile.backend_reason and "below the" in profile.backend_reason

    def test_discrete_gpu_without_vram_is_cpu(self):
        profile = self._profile(gpus=["Mystery Adapter"])
        hardware._choose_backend(profile, {"Mystery Adapter": {}})
        assert profile.backend == "cpu"
        assert profile.backend_reason and "no dedicated VRAM" in profile.backend_reason

    def test_discrete_gpu_with_vram_and_unknown_age_is_vulkan(self):
        profile = self._profile(gpus=["AMD Radeon RX 6800"])
        hardware._choose_backend(profile, {"AMD Radeon RX 6800": {"vramMb": 16384}})
        assert profile.backend == "vulkan"
        assert profile.gpu_class == "discrete"
        assert profile.backend_reason and "Vulkan chosen" in profile.backend_reason

    def test_discrete_gpu_with_old_driver_is_cpu(self):
        profile = self._profile(gpus=["AMD Radeon RX 6800"])
        hardware._choose_backend(
            profile,
            {"AMD Radeon RX 6800": {"vramMb": 16384, "driverDate": "2023-03-16"}},
        )
        assert profile.backend == "cpu"
        assert profile.backend_reason and "driver is" in profile.backend_reason


class TestProfile:
    def test_integrated_machine_chooses_cpu(self, monkeypatch):
        monkeypatch.setattr(hardware, "nvidia_smi_info", lambda: (None, None, None))
        monkeypatch.setattr(hardware, "display_adapters", lambda: ["Intel(R) Iris(R) Xe Graphics"])
        monkeypatch.setattr(
            hardware,
            "adapter_info",
            lambda: {"Intel(R) Iris(R) Xe Graphics": {"vramMb": 128, "driverVersion": "31.0.101.4255"}},
        )
        monkeypatch.setattr(hardware, "_cpu_name", lambda: "12th Gen Intel Core i5-1235U")
        monkeypatch.setattr(hardware, "_ram_gb", lambda: 15.7)
        monkeypatch.setattr(hardware.process, "cpu_threads", lambda: 12)

        profile = hardware.profile()
        # The whole point: detected adapters do NOT imply vulkan.
        assert profile.gpus == ["Intel(R) Iris(R) Xe Graphics"]
        assert profile.backend == "cpu"
        assert profile.gpu_class == "integrated"

    def test_nvidia_machine_chooses_cuda(self, monkeypatch):
        monkeypatch.setattr(
            hardware, "nvidia_smi_info", lambda: ("NVIDIA GeForce RTX 4070", 12282, "560.94")
        )
        monkeypatch.setattr(hardware, "display_adapters", lambda: ["Intel(R) UHD Graphics 770"])
        monkeypatch.setattr(hardware, "adapter_info", lambda: {})
        monkeypatch.setattr(hardware, "_cpu_name", lambda: "Intel Core i9")
        monkeypatch.setattr(hardware, "_ram_gb", lambda: 32.0)
        monkeypatch.setattr(hardware.process, "cpu_threads", lambda: 16)

        profile = hardware.profile()
        assert profile.backend == "cuda"
        assert profile.vram_mb == 12282


class TestRecommendedModel:
    def test_vulkan_backend_recommends_small(self):
        assert hardware.recommend_model(hardware.Profile(backend="vulkan")) == "small"

    def test_cpu_16gb_recommends_medium(self):
        assert hardware.recommend_model(hardware.Profile(backend="cpu", ram_gb=16.0)) == "medium"

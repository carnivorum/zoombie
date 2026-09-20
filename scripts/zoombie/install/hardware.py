"""Hardware probe and model recommendation.

The PowerShell version used ``Get-CimInstance Win32_*``. Python has no WMI
equivalent in the standard library, so this uses the tools that are actually
reliable on Windows 10/11 and reports ``None`` rather than guessing:

* **nvidia-smi** for CUDA: it is the only source that gives the VRAM total that
  matters (``Win32_VideoController.AdapterRAM`` overflows above 4 GB) plus the
  driver version.
* **DXGI adapter enumeration** through ``ctypes`` for "is there a display
  adapter at all", which decides ``vulkan`` vs ``cpu``. This avoids ``wmic``
  (removed on recent Windows) and avoids depending on PowerShell.
* **The registry** for the CPU marketing name, and ``GlobalMemoryStatusEx`` for
  total physical RAM.
"""

from __future__ import annotations

import ctypes
import re
import sys
from dataclasses import dataclass, field

from ..lib import process, tools


@dataclass
class Profile:
    """Detected hardware, in the shape the manifest records."""

    cpu_name: str | None = None
    cpu_cores: int | None = None
    cpu_threads: int | None = None
    ram_gb: float | None = None
    gpus: list[str] = field(default_factory=list)
    nvidia: str | None = None
    vram_mb: int | None = None
    gpu_driver: str | None = None
    backend: str = "cpu"

    def to_dict(self) -> dict:
        return {
            "cpuName": self.cpu_name,
            "cpuCores": self.cpu_cores,
            "cpuThreads": self.cpu_threads,
            "ramGb": self.ram_gb,
            "gpus": list(self.gpus),
            "nvidia": self.nvidia,
            "vramMb": self.vram_mb,
            "gpuDriver": self.gpu_driver,
            "backend": self.backend,
        }


def _cpu_name() -> str | None:
    """Marketing name of the first physical CPU, from the registry."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return str(value).strip() or None
        finally:
            winreg.CloseKey(key)
    except OSError:
        return None


def _ram_gb() -> float | None:
    """Total physical RAM in GB, via GlobalMemoryStatusEx."""
    if sys.platform != "win32":
        return None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
    except (AttributeError, OSError):
        return None
    return round(status.ullTotalPhys / (1024**3), 1)


def display_adapters() -> list[str]:
    """Names of the installed display adapters.

    Used purely to answer "does this machine have a real GPU", which decides
    ``vulkan`` vs ``cpu`` when no NVIDIA GPU is present.

    Implemented with ``EnumDisplayDevicesW``, a documented and stable Win32 API.
    DXGI was tried first and rejected: its adapter interfaces are COM objects that
    must be called through hand-computed vtable offsets, which is both unreadable
    and unverifiable without the exact interface version on the machine in
    question. This call needs neither a COM interface nor an index limit.

    Duplicate descriptions are collapsed: a machine with two identical cards
    should not be reported as having four adapters, and virtual/remote adapters
    report the same name as their host.
    """
    if sys.platform != "win32":
        return []

    class DisplayDeviceW(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("DeviceName", ctypes.c_wchar * 32),
            ("DeviceString", ctypes.c_wchar * 128),
            ("StateFlags", ctypes.c_ulong),
            ("DeviceID", ctypes.c_wchar * 128),
            ("DeviceKey", ctypes.c_wchar * 128),
        ]

    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return []

    names: list[str] = []
    index = 0
    while True:
        device = DisplayDeviceW()
        device.cb = ctypes.sizeof(DisplayDeviceW)
        ok = user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0)
        if not ok:
            break
        index += 1
        name = (device.DeviceString or "").strip()
        if not name:
            continue
        # "Microsoft Basic Display Adapter" is the generic fallback driver, which
        # means no usable GPU driver is installed: not a Vulkan-capable device.
        if "basic display" in name.lower() or "remote" in name.lower():
            continue
        if name not in names:
            names.append(name)
    return names


def nvidia_smi_info() -> tuple[str | None, int | None, str | None]:
    """``(name, vram_mb, driver)`` from nvidia-smi, or ``(None, None, None)``.

    Machine-readable output is requested because the human-readable table cannot
    be parsed reliably across driver versions.
    """
    exe = tools.resolve("nvidia-smi")
    if not exe:
        return None, None, None

    code, text = process.run_text(
        [exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
        timeout=60,
    )
    if code != 0:
        return None, None, None

    first = next((line.strip() for line in text.splitlines() if line.strip()), None)
    if not first:
        return None, None, None

    parts = [part.strip() for part in first.split(",")]
    name = parts[0] or None
    vram = None
    if len(parts) >= 2:
        digits = re.sub(r"[^0-9]", "", parts[1])
        if digits:
            vram = int(digits)
    driver = parts[2] if len(parts) >= 3 and parts[2] else None
    return name, vram, driver


def profile(*, probe_adapters: bool = True) -> Profile:
    """Detect the machine, deciding the whisper backend cuda > vulkan > cpu."""
    result = Profile()
    result.cpu_name = _cpu_name()
    result.cpu_threads = process.cpu_threads()
    if result.cpu_threads:
        # Hyper-threading is assumed; the physical count is an approximation and
        # only ever used for reporting.
        result.cpu_cores = max(1, result.cpu_threads // 2)
    result.ram_gb = _ram_gb()

    name, vram, driver = nvidia_smi_info()
    result.nvidia = name
    result.vram_mb = vram
    result.gpu_driver = driver

    if probe_adapters:
        result.gpus = display_adapters()
        if name and name not in result.gpus:
            result.gpus.insert(0, name)

    if name:
        result.backend = "cuda"
    elif result.gpus:
        result.backend = "vulkan"
    else:
        result.backend = "cpu"
    return result


def recommend_model(profile_: Profile) -> str:
    """Pick a model size from the hardware profile. Smaller/faster when constrained.

    This is deliberately one-dimensional (size, not quantization): adding
    quantization-aware selection is tracked separately in
    ``plans/zoombie-model-selection.md`` and must not be smuggled in here.
    """
    if profile_.backend == "cuda" and profile_.vram_mb:
        if profile_.vram_mb >= 8000:
            return "large-v3-turbo"
        if profile_.vram_mb >= 4000:
            return "medium"
        return "small"
    if profile_.backend == "vulkan":
        return "small"
    ram = profile_.ram_gb or 0
    if ram >= 16:
        return "medium"
    if ram >= 8:
        return "small"
    if ram >= 4:
        return "base"
    return "tiny"

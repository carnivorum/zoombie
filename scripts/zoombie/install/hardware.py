"""Hardware probe and model recommendation.

Python has no WMI in the standard library, so this uses the sources that are
reliable on Windows 10/11 and reports ``None`` rather than guessing:

* **nvidia-smi** for CUDA: the only source that gives the VRAM total that matters
  plus the driver version.
* **``EnumDisplayDevicesW``** through ``ctypes`` for "is there a display adapter
  at all". Avoids ``wmic``, which recent Windows has removed.
* **The display-adapter class registry key** for each adapter's driver version,
  driver date and dedicated VRAM. This is what tells an integrated GPU (shared
  system memory) from a discrete one, which decides ``vulkan`` vs ``cpu``.
* **The registry** for the CPU marketing name, and ``GlobalMemoryStatusEx`` for
  total physical RAM. The registry is preferred over ``Win32_VideoController``
  because its ``AdapterRAM`` overflows above 4 GB.

The backend choice is capability AND benefit: a discrete GPU with enough
dedicated VRAM and a current driver is worth Vulkan; an integrated GPU shares the
system-RAM bus and is not, so it stays on the CPU unless the user opts in.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

from ..lib import process, tools

# A dedicated GPU needs at least this much VRAM before Vulkan is auto-selected:
# below it the working set spills to system RAM and the GPU loses its edge.
VULKAN_MIN_VRAM_MB = 2048

# A driver older than this is treated as a reason NOT to auto-select Vulkan on a
# discrete GPU. Unknown age is never a block: only a KNOWN-old driver is.
DRIVER_MAX_AGE_MONTHS = 12

# Opt-in escape hatch for an integrated GPU (whose Vulkan path is the buggiest and
# shares bandwidth with the CPU). Set ZOOMBIE_ALLOW_IGPU_VULKAN=1 to force it.
ENV_ALLOW_IGPU = "ZOOMBIE_ALLOW_IGPU_VULKAN"


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
    gpu_driver_date: str | None = None
    # "discrete" | "integrated" | "unknown", for the primary non-NVIDIA adapter.
    gpu_class: str | None = None
    backend: str = "cpu"
    # Why this backend was chosen, in plain language, for the report.
    backend_reason: str | None = None

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
            "gpuDriverDate": self.gpu_driver_date,
            "gpuClass": self.gpu_class,
            "backend": self.backend,
            "backendReason": self.backend_reason,
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


# Name words that identify an INTEGRATED GPU. Kept narrow on purpose: an Intel
# "Arc" or "Iris Xe MAX" is a discrete-class part and must NOT match, so the
# bare-word checks below avoid them. A false "integrated" is the expensive
# mistake (it would push a real dGPU onto the CPU), a false "discrete" is caught
# by the VRAM threshold instead.
_INTEGRATED_HINTS = (
    re.compile(r"\bUHD Graphics\b", re.IGNORECASE),
    re.compile(r"\bHD Graphics\b", re.IGNORECASE),
    re.compile(r"\bIris\s+Xe\b(?!\s*MAX)", re.IGNORECASE),
    re.compile(r"\bRadeon\s+Graphics\b", re.IGNORECASE),
    re.compile(r"\bVega\s+\d+\s+Graphics\b", re.IGNORECASE),
    re.compile(r"\bRadeon\s+Vega\s+Graphics\b", re.IGNORECASE),
)


# Adapter names carry vendor marks: "Intel(R) Iris(R) Xe", "AMD Radeon(TM)
# Graphics". They break word-boundary matching, so they are stripped first.
_VENDOR_MARKS = re.compile(r"\((?:R|TM|C)\)", re.IGNORECASE)


def _normalize_adapter_name(description: str) -> str:
    """Drop vendor marks and collapse whitespace, for reliable name matching."""
    return re.sub(r"\s+", " ", _VENDOR_MARKS.sub("", description)).strip()


def _classify_adapter(description: str, dedicated_vram_mb: int | None) -> str:
    """Classify a non-NVIDIA adapter as ``discrete``/``integrated``/``unknown``."""
    text = _normalize_adapter_name(description)
    if any(pattern.search(text) for pattern in _INTEGRATED_HINTS):
        return "integrated"
    if dedicated_vram_mb and dedicated_vram_mb > 0:
        return "discrete"
    # No name hint and no dedicated VRAM figure: do not guess, and let the
    # caller's VRAM threshold decide.
    return "unknown"


def adapter_info() -> dict:
    """Per-adapter ``{description, driverVersion, driverDate, vramMb}``, deduped.

    The dedicated VRAM and driver metadata live in the display-adapter class key
    (``...\\Control\\Class\\{4d36e968-...}``), which ``EnumDisplayDevices`` does
    not return. The key is read for exactly this: telling an integrated,
    shared-memory GPU from a discrete one, plus driver age.

    ``HardwareInformation.MemorySize`` is absent on many integrated adapters and
    overflows above 4 GB, so a value is only trusted below 4 GB; the name is the
    primary integrated-vs-discrete signal and the VRAM is the secondary one.
    """
    if sys.platform != "win32":
        return {}
    try:
        import winreg
    except ImportError:
        return {}

    out: dict[str, dict] = {}
    try:
        root = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}",
        )
    except OSError:
        return {}

    try:
        index = 0
        while True:
            try:
                sub_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not sub_name.isdigit():
                continue  # skip the "Properties" and other non-instance subkeys
            try:
                sub = winreg.OpenKey(root, sub_name)
            except OSError:
                continue
            try:
                description = _read_reg_str(sub, "DriverDesc")
                if not description:
                    continue
                record = {
                    "description": description,
                    "driverVersion": _read_reg_str(sub, "DriverVersion"),
                    "driverDate": _read_reg_str(sub, "DriverDate"),
                    "vramMb": _read_dedicated_vram(sub),
                }
                # Same adapter enumerated twice (multi-monitor) must not appear
                # twice; keep the entry that actually carries metadata.
                existing = out.get(description)
                if existing is None or (not existing.get("vramMb") and record["vramMb"]):
                    out[description] = record
            finally:
                winreg.CloseKey(sub)
    finally:
        winreg.CloseKey(root)
    return out


def _read_reg_str(key, name: str) -> str | None:
    """Read a REG_SZ value, or None. Kept tiny because winreg is only on Windows."""
    import winreg

    try:
        value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    text = str(value).strip() if value is not None else ""
    return text or None


def _read_dedicated_vram(key) -> int | None:
    """Dedicated VRAM in MB, refusing the known-overflowing 4 GB+ region."""
    raw = _read_reg_str(key, "HardwareInformation.MemorySize")
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits:
        return None
    # The value may be a plain MB number or a QWORD byte count. A figure over
    # ~4,000,000 (MB) is the overflow artefact, so only trust the plausible band.
    value = int(digits)
    if value > 4_000_000:
        return None
    if value > 100_000:  # bytes, not MB: 1..100 GB
        return value // (1024 * 1024)
    return value


def _driver_age_months(driver_date: str | None) -> int | None:
    """Whole months between a Windows driver date (``YYYY-MM-DD``) and today."""
    if not driver_date:
        return None
    match = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})", driver_date)
    if not match:
        return None
    try:
        when = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    today = datetime.now().date()
    if when > today:
        return 0
    return (today.year - when.year) * 12 + (today.month - when.month)


def _allow_integrated_vulkan() -> bool:
    """The opt-in, read from the environment so it works unattended."""
    value = (os.environ.get(ENV_ALLOW_IGPU) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _choose_backend(result: Profile, adapters: dict) -> None:
    """Set ``backend`` and ``backend_reason`` from capability AND benefit.

    cuda wins outright. Vulkan is chosen ONLY for a discrete GPU with enough
    dedicated VRAM and a current-or-unknown driver; an integrated GPU (shared
    memory, bandwidth-bound exactly like the CPU) stays on the CPU unless the
    user opts in with ``ZOOMBIE_ALLOW_IGPU_VULKAN=1``. Every branch states its
    reason in plain language.
    """
    if result.nvidia:
        result.backend = "cuda"
        result.backend_reason = (
            f"NVIDIA GPU detected ({result.nvidia}); CUDA is the fastest path"
        )
        return

    if not result.gpus:
        result.backend = "cpu"
        result.backend_reason = "no GPU detected; CPU chosen"
        return

    primary = result.gpus[0]
    record = adapters.get(primary) or {}
    vram = record.get("vramMb")
    cls = _classify_adapter(primary, vram)
    result.gpu_class = cls
    if record.get("driverVersion"):
        result.gpu_driver = result.gpu_driver or record["driverVersion"]
    result.gpu_driver_date = record.get("driverDate")

    if cls == "integrated":
        if _allow_integrated_vulkan():
            result.backend = "vulkan"
            result.backend_reason = (
                f"Integrated GPU ({primary}, shared memory) but "
                f"{ENV_ALLOW_IGPU} is set; Vulkan chosen by request"
            )
        else:
            result.backend = "cpu"
            result.backend_reason = (
                f"Integrated GPU ({primary}, shared memory) detected; CPU chosen - "
                "Vulkan would contend for the same memory bus"
            )
        return

    # Not known-integrated: require real dedicated VRAM before recommending it.
    if vram is None:
        result.backend = "cpu"
        result.backend_reason = (
            f"GPU ({primary}) reports no dedicated VRAM; CPU chosen rather than "
            "guessing it can host the model"
        )
        return
    if vram < VULKAN_MIN_VRAM_MB:
        result.backend = "cpu"
        result.backend_reason = (
            f"GPU ({primary}) has {vram} MB dedicated VRAM, below the "
            f"{VULKAN_MIN_VRAM_MB} MB needed for Vulkan; CPU chosen"
        )
        return

    age = _driver_age_months(result.gpu_driver_date)
    if age is not None and age > DRIVER_MAX_AGE_MONTHS:
        result.backend = "cpu"
        result.backend_reason = (
            f"GPU ({primary}) has {vram} MB VRAM but its driver is {age} months "
            f"old (limit {DRIVER_MAX_AGE_MONTHS}); CPU chosen - update the driver "
            "to use Vulkan"
        )
        return

    result.backend = "vulkan"
    age_text = "driver age unknown" if age is None else f"driver {age} months old"
    result.backend_reason = (
        f"Discrete GPU ({primary}) with {vram} MB dedicated VRAM and a current "
        f"driver ({age_text}); Vulkan chosen"
    )


def profile(*, probe_adapters: bool = True) -> Profile:
    """Detect the machine, deciding the whisper backend cuda > vulkan > cpu.

    Vulkan is capability AND benefit, so an integrated GPU is not selected merely
    because a display adapter exists; see :func:`_choose_backend`.
    """
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

    adapters: dict = {}
    if probe_adapters:
        result.gpus = display_adapters()
        adapters = adapter_info()
        if name and name not in result.gpus:
            result.gpus.insert(0, name)

    _choose_backend(result, adapters)
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

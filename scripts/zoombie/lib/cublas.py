"""The cuBLAS runtime whisper.cpp's CUDA build loads, and its pinned provisioner.

Why this module exists
----------------------
The ggml-org ``whisper-cublas-*`` asset ships ``ggml-cuda.dll`` but NOT the
cuBLAS runtime that ``ggml-cuda.dll`` loads on first use. Without it,
``ggml_cuda_init`` cannot create a device, whisper.cpp then transcribes on the
CPU, and it STILL exits 0. Nothing in the pipeline sees a failure: ``env.json``
keeps saying ``cuda``, ``doctor`` keeps reporting ``cuda``, and a long file runs
at roughly realtime on the CPU.

So the runtime is provisioned separately from NVIDIA's own redist archives. Each
row below is one archive with a published sha256 -- which is what makes this
reproducible without installing the CUDA Toolkit, and what makes the download
verifiable before a native DLL is placed next to the binary.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field

from . import paths

# The CUDA major this table falls back to. NOT a cosmetic pin: ggml-cuda.dll
# loads cuBLAS by MAJOR-versioned name (cublas64_11.dll), so an asset built
# against a different major needs a different runtime. Keying the runtime off the
# ASSET rather than hard-coding 11 is what stops a future cublas64_12.dll install
# from passing a "runtime present" check while the GPU silently never
# initialises.
DEFAULT_MAJOR = 11

# Per-major provisioning records. Adding a major means adding one verified row.
PROVISIONS: dict[int, dict] = {
    11: {
        "major": 11,
        "version": "11.11.3.6",
        "url": (
            "https://developer.download.nvidia.com/compute/cuda/redist/libcublas/"
            "windows-x86_64/libcublas-windows-x86_64-11.11.3.6-archive.zip"
        ),
        "sha256": "67B0934A6359E4EE26FFF823C356021589D392C4FD49CA12624F570EDC08E2B9",
        "dlls": ["cublas64_11.dll", "cublasLt64_11.dll"],
    },
}


def supported_majors() -> list[int]:
    """The cuBLAS majors this toolchain has a pinned, hash-verified redist for."""
    return sorted(PROVISIONS)


def provision_spec(cuda_major: int = 0) -> dict | None:
    """The provisioning record for a major, or None when it is not pinned.

    None is deliberate: the caller must then REFUSE the install (naming the asset
    and the missing major) rather than download an unverified binary or leave
    ``ggml-cuda.dll`` without the runtime it loads.
    """
    major = cuda_major if cuda_major > 0 else DEFAULT_MAJOR
    return PROVISIONS.get(major)


def major_from_asset_name(name: str | None) -> int | None:
    """Extract the CUDA major a whisper.cpp asset was built against.

    ggml-org names its assets after the toolkit they were built with
    (``whisper-cublas-11.8.0-bin-x64.zip``). Parsing that name is how the
    installer knows which runtime the asset will load instead of assuming.

    Returns None when the name carries no version, in which case the caller falls
    back to :data:`DEFAULT_MAJOR`.
    """
    if not name:
        return None
    match = re.search(r"cublas-(\d+)[.\-_]", name)
    if match:
        return int(match.group(1))
    match = re.search(r"\bcuda[-_ ]?(\d+)[.\-_]", name)
    if match:
        return int(match.group(1))
    return None


def sha256_file(path: str) -> str:
    """Hex sha256 of a file, read in chunks so a 400 MB archive is not held in RAM."""
    digest = hashlib.sha256()
    with open(paths.to_extended(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_archive(path: str, expected: str) -> None:
    """Raise when a downloaded archive does not match its published sha256."""
    actual = sha256_file(path)
    if actual != expected.upper():
        raise RuntimeError(
            f"cuBLAS archive hash mismatch: expected {expected}, got {actual}"
        )


@dataclass
class RuntimeStatus:
    """Whether the CUDA build beside ``whisper-cli.exe`` can actually initialise.

    Three states are kept DISTINCT, because conflating them is what made a broken
    CUDA install look healthy:

    * ``dir_missing`` - there is no whisper folder (not installed). This is NOT
      "all DLLs are missing".
    * no ``gpu_module`` - a CPU/Vulkan build, where cuBLAS is irrelevant.
    * ``ready`` - a CUDA build whose own runtime is complete.

    Sibling CUDA DLLs that are loaded but not provisioned here (``cudart64_*``)
    are reported in ``warnings`` rather than ``missing``, so readiness means "the
    DLLs we install", not "every possible DLL".
    """

    exe: str | None = None
    directory: str | None = None
    dir_missing: bool = False
    gpu_module: bool = False
    cublas_major: int | None = None
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ready: bool = False
    provision_version: str | None = None

    def to_report(self) -> dict:
        """The exact camelCase shape ``doctor`` and the manifest publish."""
        return {
            "gpuModule": self.gpu_module,
            "dirMissing": self.dir_missing,
            "ready": self.ready,
            "cublasMajor": self.cublas_major,
            "present": list(self.present),
            "missing": list(self.missing),
            "warnings": list(self.warnings),
            "version": self.provision_version,
        }


def runtime_status(whisper_exe: str | None, cuda_major: int = 0) -> RuntimeStatus:
    """Report whether the CUDA runtime next to ``whisper-cli.exe`` is complete.

    A CUDA build needs, side by side with the exe:
      * ``ggml-cuda.dll`` (shipped in the asset), and
      * the cuBLAS runtime ``ggml-cuda.dll`` loads by major-versioned name
        (``cublas64_<major>.dll`` / ``cublasLt64_<major>.dll``), which the asset
        does NOT ship.

    The required major comes from the asset (see :func:`major_from_asset_name`)
    or from whatever ``cublas64_*.dll`` is already present, so the check cannot
    be fooled by a runtime of the wrong major sitting in the folder.
    """
    status = RuntimeStatus(exe=whisper_exe)
    if not whisper_exe:
        return status

    directory = os.path.dirname(whisper_exe)
    status.directory = directory
    if not paths.is_dir(directory):
        status.dir_missing = True
        return status

    status.gpu_module = paths.is_file(os.path.join(directory, "ggml-cuda.dll"))
    if not status.gpu_module:
        return status

    required = cuda_major
    if required <= 0:
        existing = [
            entry.name
            for entry in paths.list_dir(directory, files=True)
            if entry.name.startswith("cublas64_")
        ]
        for name in existing:
            match = re.match(r"cublas64_(\d+)\.dll", name)
            if match:
                required = int(match.group(1))
                break
        else:
            required = DEFAULT_MAJOR
    status.cublas_major = required

    spec = provision_spec(required)
    if spec:
        status.provision_version = spec["version"]
    names = list(spec["dlls"]) if spec else [
        f"cublas64_{required}.dll",
        f"cublasLt64_{required}.dll",
    ]

    for dll in names:
        if paths.is_file(os.path.join(directory, dll)):
            status.present.append(dll)
        else:
            status.missing.append(dll)

    # Loaded but not provisioned by us: report, never block on it.
    names_present = {entry.name for entry in paths.list_dir(directory, files=True)}
    for pattern in ("cudart64_*.dll", "ggml-base.dll"):
        if not any(fnmatch.fnmatch(name, pattern) for name in names_present):
            status.warnings.append(f"no {pattern} beside whisper-cli.exe")

    status.ready = not status.missing
    return status

"""Install/repair each component of the toolchain into the ASCII root.

Every function is idempotent and honours the three modes via :class:`Modes`:

* ``check``   - detect only, write nothing
* ``dry_run`` - report what would be done, write nothing
* apply       - the default

The mode is carried in one object, so the write decision is explicit at every
write site.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .. import ROLE_VERSION, SKILL_VERSION
from ..lib import (
    archive,
    cublas,
    download,
    env as env_mod,
    manifest,
    modes as modes_mod,
    paths,
    process,
    skills,
    tesseract,
    tools,
    unpack,
)
from ..install import hardware

GITHUB_REPO = "ggml-org/whisper.cpp"
FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
MODEL_URL_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

# Asset filename fragments per backend, in preference order.
ASSET_PATTERNS = {
    "cuda": [r"cublas.*bin-x64\.zip$", r"cuda.*bin-x64\.zip$"],
    "vulkan": [r"vulkan.*bin-x64\.zip$"],
    "cpu": [r"^whisper-bin-x64\.zip$", r"bin-x64\.zip$"],
}


@dataclass
class Modes:
    """How this run is allowed to behave."""

    check: bool = False
    dry_run: bool = False
    force: bool = False
    changes: list[str] = field(default_factory=list)

    @property
    def may_write(self) -> bool:
        """False in -Check and -DryRun, where the filesystem must be untouched."""
        return not self.check and not self.dry_run

    def note(self, change: str) -> None:
        self.changes.append(change)


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def install_ffmpeg(modes: Modes) -> bool:
    """Fetch the static ffmpeg + ffprobe build into ``<root>\\bin``."""
    ffmpeg = paths.env_path("bin", "ffmpeg.exe")
    ffprobe = paths.env_path("bin", "ffprobe.exe")

    if paths.is_file(ffmpeg) and paths.is_file(ffprobe) and not modes.force:
        process.log(f"ffmpeg present: {tools.tool_version(ffmpeg, ['-version'])}")
        return True
    if not modes.may_write:
        process.log("would install ffmpeg -> " + paths.env_path("bin"), "step")
        return False

    tmp = paths.new_temp_dir("zoombie-ffmpeg")
    try:
        zip_path = os.path.join(tmp, "ffmpeg.zip")
        download.download(FFMPEG_URL, zip_path)
        extracted = os.path.join(tmp, "x")
        archive.extract_zip(zip_path, extracted)

        found = tools.find_file_named(extracted, "ffmpeg.exe")
        if not found:
            raise RuntimeError("ffmpeg.exe not found in archive")
        paths.copy_file(found, ffmpeg)

        probe = tools.find_file_named(os.path.dirname(found), "ffprobe.exe", recursive=False)
        if probe:
            paths.copy_file(probe, ffprobe)

        modes.note("installed ffmpeg/ffprobe")
        return True
    finally:
        paths.remove_quietly(tmp, recursive=True)


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------

def install_ytdlp(modes: Modes, python: str | None) -> dict:
    """Ensure yt-dlp is importable, invoked as ``python -m yt_dlp``.

    yt-dlp is pure Python and therefore ASCII-path safe, so unlike whisper.cpp it
    needs none of the ASCII isolation the media tools get.
    """
    result = {"ok": False, "version": None, "note": None}

    if not python:
        result["note"] = "Python not found. Install with: winget install Python.Python.3.12"
        process.log(f"yt-dlp skipped: {result['note']}", "warn")
        return result

    existing = tools.tool_version(python, ["-m", "yt_dlp", "--version"])
    if existing and not modes.force:
        result.update(ok=True, version=existing)
        process.log(f"yt-dlp present: {existing} (python -m yt_dlp)")
        return result

    if not modes.may_write:
        process.log(f"would pip install yt-dlp into {python}", "step")
        result["ok"] = True
        return result

    before = tools.pip_shim_snapshot()
    process.log("installing yt-dlp (pip --user)", "step")
    if tools.pip_install(python, ["yt-dlp"]) != 0:
        result["note"] = "pip install yt-dlp failed"
        process.log(f"yt-dlp: {result['note']}", "warn")
        return result

    # We invoke the module, never the console launcher, so drop the shim pip made.
    removed = tools.remove_new_pip_shims(before)
    if removed:
        process.log(f"removed {removed} unused pip shim(s)")

    result["version"] = tools.tool_version(python, ["-m", "yt_dlp", "--version"])
    if not result["version"]:
        result["note"] = "yt-dlp installed but `python -m yt_dlp` is not working"
        process.log(f"yt-dlp: {result['note']}", "warn")
        return result

    result["ok"] = True
    modes.note("installed yt-dlp (python -m yt_dlp)")
    return result


# ---------------------------------------------------------------------------
# whisper.cpp asset selection
# ---------------------------------------------------------------------------

def github_releases(repo: str, count: int = 30) -> list[dict]:
    """Fetch releases (newest first) for a repo."""
    url = f"https://api.github.com/repos/{repo}/releases?per_page={count}"
    try:
        releases = download.fetch_json(url)
    except Exception as exc:  # noqa: BLE001 - surfaced with context
        raise RuntimeError(f"Could not query GitHub releases for {repo}: {exc}") from exc
    return releases if isinstance(releases, list) else []


def select_asset(backend: str) -> dict | None:
    """Pick the newest suitable Windows x64 asset, PREFERRING a provisionable one.

    For the cuda backend the selection is provision-aware rather than merely
    name-aware. cuBLAS is loaded by major-versioned name, and this toolchain only
    pins a redist for majors in :data:`cublas.PROVISIONS`. A naive "newest cublas
    asset" pick would break the moment ggml-org publishes a CUDA-12 build: the
    install would select it and then fail to provision, on every release, forever.

    So releases are walked newest-first and the first cuda candidate whose asset
    name maps to a PROVISIONABLE major wins. Only if none exists is the newest
    cuda asset returned anyway, so the caller can refuse with an exact message.
    """
    patterns = ASSET_PATTERNS.get(backend, ASSET_PATTERNS["cpu"])
    releases = github_releases(GITHUB_REPO)
    fallback: dict | None = None

    for release in releases:
        assets = release.get("assets") or []
        if not assets:
            continue
        for pattern in patterns:
            hit = next(
                (a for a in assets if re.search(pattern, a.get("name", ""), re.IGNORECASE)),
                None,
            )
            if not hit:
                continue

            record = {
                "tag": release.get("tag_name"),
                "name": hit.get("name"),
                "url": hit.get("browser_download_url"),
                "backend": backend,
                "cudaMajor": cublas.major_from_asset_name(hit.get("name")),
                "provisionable": None,
            }
            if backend != "cuda":
                return record

            # An unversioned name implies the default major, which IS provisionable.
            effective = record["cudaMajor"] or cublas.DEFAULT_MAJOR
            if cublas.provision_spec(effective):
                record["provisionable"] = True
                return record

            record["provisionable"] = False
            if fallback is None:
                fallback = record

    return fallback


def install_cublas(modes: Modes, dest_dir: str, cuda_major: int = 0) -> dict:
    """Ensure the cuBLAS runtime ``ggml-cuda.dll`` loads sits beside the exe.

    The asset ships ``ggml-cuda.dll`` but NOT the cuBLAS DLLs it loads, and
    without them ``ggml_cuda_init`` cannot create a device while whisper.cpp still
    exits 0. The archive's sha256 is verified BEFORE anything is copied, because
    these DLLs are loaded into the whisper process.
    """
    major = cuda_major if cuda_major > 0 else cublas.DEFAULT_MAJOR
    spec = cublas.provision_spec(major)
    result = {"ok": False, "installed": [], "missing": [], "major": major,
              "version": None, "note": None}

    # No pinned, hash-verified redist for this major: refuse. Copying an
    # unverified DLL into the process is not acceptable, and continuing would
    # leave a CUDA build that silently transcribes on the CPU.
    if not spec:
        result["note"] = (
            f"no verified cuBLAS redist is pinned for CUDA {major}; cannot "
            "provision the runtime the selected asset loads"
        )
        process.log(f"cuBLAS runtime: {result['note']}", "warn")
        return result
    result["version"] = spec["version"]

    runtime = cublas.runtime_status(os.path.join(dest_dir, "whisper-cli.exe"), major)
    if runtime.ready and not modes.force:
        result.update(ok=True, installed=list(runtime.present))
        process.log(f"cuBLAS runtime present: {', '.join(runtime.present)}")
        return result

    if not modes.may_write:
        # -Check/-DryRun must still report the truth, naming exactly which DLL is
        # absent instead of only saying "not installed".
        process.log(f"would provision cuBLAS {spec['version']} -> {dest_dir}", "step")
        if runtime.missing:
            process.log(f"     CUDA runtime missing: {', '.join(runtime.missing)}", "warn")
        result.update(ok=True, missing=list(runtime.missing))
        return result

    process.log(
        f"provisioning CUDA runtime cuBLAS {spec['version']} "
        "(not shipped in the whisper asset)",
        "step",
    )

    tmp = paths.new_temp_dir("zoombie-cublas")
    try:
        zip_path = os.path.join(tmp, "cublas.zip")
        download.download(spec["url"], zip_path)
        cublas.verify_archive(zip_path, spec["sha256"])

        import zipfile

        with zipfile.ZipFile(paths.to_extended(zip_path)) as zf:
            for dll in spec["dlls"]:
                member = next(
                    (
                        entry.filename
                        for entry in zf.infolist()
                        if entry.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
                        == dll.lower()
                    ),
                    None,
                )
                if not member:
                    result["missing"].append(dll)
                    continue
                archive.extract_member(zip_path, member, os.path.join(dest_dir, dll))
                result["installed"].append(dll)
    finally:
        paths.remove_quietly(tmp, recursive=True)

    after = cublas.runtime_status(os.path.join(dest_dir, "whisper-cli.exe"), major)
    result["missing"] = list(after.missing)
    result["ok"] = after.ready
    if result["ok"]:
        modes.note(f"provisioned cuBLAS {spec['version']} runtime")
    else:
        # Never leave a CUDA install that will silently run on the CPU.
        result["note"] = f"cuBLAS runtime still incomplete (missing: {', '.join(result['missing'])})"
    return result


def install_whisper(modes: Modes, hardware_profile: hardware.Profile) -> dict:
    """Install the whisper.cpp build matching the CURRENT hardware backend.

    The detected backend is authoritative, not any value a previous run wrote into
    env.json: a machine that gained or lost a GPU must not keep the old build.

    ONE exception, and it is what stops a reinstall loop on a non-NVIDIA machine:
    when the repo ships no asset for the detected backend, the CPU build is an
    unavoidable substitution. That fact is recorded in env.json
    (``backendSubstituted`` + ``requestedBackend``), so the next run recognises the
    same substitution and keeps the existing build instead of re-downloading it.
    A real change (a GPU appears, so the request changes) clears the invariant and
    the build is genuinely replaced.
    """
    dest_dir = paths.env_path("bin", "whisper")
    exe = os.path.join(dest_dir, "whisper-cli.exe")

    desired = hardware_profile.backend
    previous = manifest.load()
    installed_backend = manifest.dig(previous, "whisper.backend")
    previous_tag = manifest.dig(previous, "whisper.tag")
    previous_asset = manifest.dig(previous, "whisper.asset")
    previous_requested = manifest.dig(previous, "whisper.requestedBackend")
    previous_substituted = bool(manifest.dig(previous, "whisper.backendSubstituted"))

    # A substitution we already made, for the SAME request, is not a mismatch: the
    # CPU build was the best available answer and still is. Keeping it is the whole
    # point; only a changed request (or a non-substituted mismatch) forces a swap.
    substitution_holds = (
        previous_substituted
        and installed_backend == "cpu"
        and desired != "cpu"
        and previous_requested == desired
    )

    present = paths.is_file(exe) and not modes.force
    if present and installed_backend and installed_backend != desired:
        if substitution_holds:
            process.log(
                f"installed whisper backend 'cpu' is the recorded substitution for "
                f"'{desired}' (no {desired} asset shipped); keeping it",
                "warn",
            )
        elif modes.may_write:
            process.log(
                f"installed whisper backend '{installed_backend}' no longer matches "
                f"detected hardware '{desired}'; reinstalling",
                "warn",
            )
            present = False
        else:
            process.log(
                f"whisper backend mismatch: installed '{installed_backend}' but "
                f"hardware detects '{desired}' (re-run without -Check to fix)",
                "warn",
            )

    if present:
        process.log(f"whisper-cli present: {exe}")
        effective = installed_backend or desired
        substituted = bool(installed_backend) and installed_backend != desired
        # If a previous run never recorded which asset it used, recover it with a
        # single release scan so env.json stays a complete record.
        if not previous_tag and modes.may_write:
            scan = select_asset(effective)
            if scan:
                previous_tag, previous_asset = scan["tag"], scan["name"]

        # The CUDA runtime is provisioned independently of the asset, so it is
        # repaired here even when the exe is already present: an install with the
        # asset but no cuBLAS is exactly the broken state this guards against.
        cuda_major = None
        cublas_info = None
        if effective == "cuda":
            cuda_major = cublas.major_from_asset_name(previous_asset) or cublas.DEFAULT_MAJOR
            cublas_info = install_cublas(modes, dest_dir, cuda_major)
        return {
            "exe": exe, "tag": previous_tag, "asset": previous_asset,
            "backend": effective, "requestedBackend": desired, "substituted": substituted,
            "cudaMajor": cuda_major, "cublas": cublas_info,
        }

    asset = select_asset(desired)
    substituted = False
    if not asset and desired != "cpu":
        process.log(f"no {desired} asset found; falling back to CPU build", "warn")
        asset = select_asset("cpu")
        substituted = asset is not None
        if substituted:
            process.log(
                f"recording the CPU build as a substitution for '{desired}' so the "
                "next run does not re-download it",
                "warn",
            )
    if not asset:
        raise RuntimeError("No suitable whisper.cpp Windows asset found in any release.")

    # Refuse BEFORE downloading a build whose runtime we cannot provision: it
    # could never initialise the GPU, so installing it would hand back a build
    # that silently runs on the CPU.
    if asset["backend"] == "cuda" and asset.get("provisionable") is False:
        supported = ", ".join(str(m) for m in cublas.supported_majors())
        message = (
            f"whisper asset '{asset['name']}' is built against CUDA "
            f"{asset['cudaMajor']}, but no cuBLAS {asset['cudaMajor']} redist is "
            f"pinned (supported majors: {supported}). Add one verified redist row "
            "to zoombie/lib/cublas.py, or install an older cublas asset."
        )
        if modes.may_write:
            raise RuntimeError(message)
        process.log(message, "warn")

    process.log(
        f"selected whisper.cpp {asset['tag']} asset {asset['name']} [{asset['backend']}]",
        "step",
    )

    # Derived from the ASSET, not a constant, so a CUDA-12 release stays correct.
    asset_cuda_major = asset["cudaMajor"]
    if asset["backend"] == "cuda" and not asset_cuda_major:
        process.log(
            f"asset '{asset['name']}' names no CUDA version; assuming cuBLAS major "
            f"{cublas.DEFAULT_MAJOR}",
            "warn",
        )

    if not modes.may_write:
        process.log(f"     would extract to {dest_dir}")
        dry_cublas = None
        if asset["backend"] == "cuda":
            dry_cublas = install_cublas(modes, dest_dir, asset_cuda_major or 0)
        return {
            "exe": exe, "tag": asset["tag"], "asset": asset["name"],
            "backend": asset["backend"], "requestedBackend": desired,
            "substituted": substituted, "cudaMajor": asset_cuda_major,
            "cublas": dry_cublas,
        }

    tmp = paths.new_temp_dir("zoombie-whisper")
    try:
        zip_path = os.path.join(tmp, "whisper.zip")
        download.download(asset["url"], zip_path)
        paths.ensure_dir(dest_dir)
        # Keep DLLs next to the exe: the CUDA bundle needs them adjacent. The
        # archive nests everything under Release\, and those assembled output
        # paths are what PowerShell's Expand-Archive used to refuse past 260 chars.
        archive.extract_zip(zip_path, dest_dir)

        found = tools.find_file_named(dest_dir, "whisper-cli.exe")
        if not found:
            raise RuntimeError("whisper-cli.exe not found in archive")

        # Flatten the release folder INTO the destination recursively: the sibling
        # ggml-*.dll backends (including ggml-cuda.dll) live there too, and copying
        # only the exe's own directory would silently drop them.
        source_dir = os.path.dirname(found)
        if os.path.normcase(source_dir) != os.path.normcase(dest_dir):
            paths.copy_tree(source_dir, dest_dir)

        cublas_info = None
        if asset["backend"] == "cuda":
            cublas_info = install_cublas(modes, dest_dir, asset_cuda_major or 0)
            if not cublas_info["ok"]:
                raise RuntimeError(
                    "CUDA backend selected but the cuBLAS runtime could not be "
                    f"provisioned: {cublas_info['note']}"
                )

        if substituted:
            modes.note(f"whisper backend substituted: cpu instead of {desired}")
        else:
            modes.note(f"installed whisper.cpp {asset['tag']} ({asset['backend']})")
        return {
            "exe": exe, "tag": asset["tag"], "asset": asset["name"],
            "backend": asset["backend"], "requestedBackend": desired,
            "substituted": substituted, "cudaMajor": asset_cuda_major,
            "cublas": cublas_info,
        }
    finally:
        paths.remove_quietly(tmp, recursive=True)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def install_model(modes: Modes, model_name: str) -> dict:
    """Download a ggml model, choosing a hardware-appropriate default upstream."""
    models_dir = paths.env_path("models")
    file_name = model_name if model_name.startswith("ggml-") else f"ggml-{model_name}.bin"
    destination = os.path.join(models_dir, file_name)

    if paths.is_file(destination) and not modes.force:
        size_mb = round(paths.file_size(destination) / (1024 * 1024), 1)
        process.log(f"model present: {file_name} ({size_mb} MB)")
        return {"path": destination, "name": file_name, "sizeMb": size_mb}

    if not modes.may_write:
        process.log(f"would download model {file_name}", "step")
        return {"path": destination, "name": file_name, "sizeMb": 0}

    paths.ensure_dir(models_dir)
    download.download(f"{MODEL_URL_BASE}/{file_name}", destination)

    size_mb = round(paths.file_size(destination) / (1024 * 1024), 1)
    if size_mb < 1:
        raise RuntimeError(f"Downloaded model looks too small ({size_mb} MB): {destination}")
    modes.note(f"installed model {file_name}")
    return {"path": destination, "name": file_name, "sizeMb": size_mb}


# ---------------------------------------------------------------------------
# Archive extractor (provisioned: pinned URL + published sha256)
# ---------------------------------------------------------------------------

def install_extractors(modes: Modes) -> dict:
    """Provision the pinned 7-Zip extractor as its OWN component.

    The NSIS/7z reader is NOT a Tesseract detail: it is the shared capability any
    installer-only upstream needs, so it is provisioned and recorded on its own
    instead of being an implicit side effect of whichever component happens to
    require it first. That also means it is repaired even when Tesseract is
    already present, and that a machine with no Tesseract can still unpack.

    Idempotent: a complete extractor (``7z.exe`` + ``7z.dll``) is left untouched
    unless ``-Force``. Honours ``-Check``/``-DryRun`` by writing nothing.
    """
    bin_dir = paths.env_path("bin")
    exe = unpack.extractor_path()
    dll = os.path.join(bin_dir, "7z.dll")
    result = {
        "ok": False,
        "path": exe,
        "name": unpack.EXTRACTOR["name"],
        "version": unpack.EXTRACTOR["version"],
        "bootstrap": {
            "name": unpack.EXTRACTOR_BOOTSTRAP["name"],
            "version": unpack.EXTRACTOR_BOOTSTRAP["version"],
        },
        "note": None,
    }

    complete = paths.is_file(exe) and paths.is_file(dll)
    if complete and not modes.force:
        result["ok"] = True
        process.log(f"extractor present: {exe} ({result['name']} {result['version']})")
        return result

    if not modes.may_write:
        process.log(f"would provision 7-Zip {result['version']} extractor -> {bin_dir}", "step")
        result["ok"] = True  # it reports what WOULD happen and writes nothing
        return result

    process.log(f"provisioning pinned 7-Zip {result['version']} extractor", "step")
    unpack.install_extractor()

    result["ok"] = paths.is_file(exe) and paths.is_file(dll)
    if not result["ok"]:
        result["note"] = "the extractor is still incomplete after provisioning"
        process.log(f"extractor: {result['note']}", "warn")
        return result
    modes.note(f"provisioned 7-Zip {result['version']} extractor")
    return result


# ---------------------------------------------------------------------------
# Tesseract OCR engine (provisioned: pinned URL + published sha256)
# ---------------------------------------------------------------------------

def install_tesseract(modes: Modes) -> dict:
    """Provision the pinned Tesseract engine and its language data into the root.

    The engine is the ONE component with no portable archive upstream, so it is
    provisioned from the official NSIS installer by EXTRACTING its payload with a
    pinned 7-Zip -- the installer is never run, so no elevation is needed and
    nothing is written into a system location. Both downloads (the installer and
    the extractor) are verified against their published sha256 before use, and the
    traineddata is fetched separately from the official tessdata repo because the
    installer ships none.

    Idempotent: a complete engine + the expected languages is left untouched unless
    ``-Force``. Honours ``-Check``/``-DryRun`` through :class:`Modes` (write nothing).
    """
    result = {
        "ok": False, "engine": None, "engineVersion": None,
        "languages": [], "expectedLanguages": list(tesseract.DEFAULT_LANGUAGES),
        "note": None,
    }
    engine = tesseract.engine_path()

    present_languages = tesseract.language_list()
    complete = (
        paths.is_file(engine)
        and all(lang in present_languages for lang in tesseract.DEFAULT_LANGUAGES)
    )
    if complete and not modes.force:
        result.update(
            ok=True, engine=engine,
            engineVersion=tesseract.engine_version(engine),
            languages=present_languages,
        )
        process.log(
            f"tesseract present: {engine} "
            f"(v{result['engineVersion']}, langs: {', '.join(present_languages)})"
        )
        return result

    if not modes.may_write:
        process.log(
            f"would provision Tesseract {tesseract.ENGINE['version']} -> "
            f"{tesseract.engine_dir()}",
            "step",
        )
        result.update(
            ok=True,
            engine=engine if paths.is_file(engine) else None,
            engineVersion=tesseract.engine_version(engine) if paths.is_file(engine) else None,
            languages=present_languages,
        )
        return result

    tmp = paths.new_temp_dir("zoombie-tesseract-dl")
    try:
        # 1. The NSIS-capable extractor was provisioned as its OWN component before
        # this one (see install_extractors). It is repaired on demand here too, so
        # the engine never depends on the ORDER components are installed in.
        extractor = tesseract.extractor_path()
        if not paths.is_file(extractor) or modes.force:
            tesseract.install_extractor()

        # 2. The engine installer, verified before its payload is touched.
        installer = os.path.join(tmp, "tesseract-setup.exe")
        process.log(
            f"downloading Tesseract {tesseract.ENGINE['version']} "
            f"({round(tesseract.ENGINE['size'] / (1024 * 1024), 1)} MB)",
            "step",
        )
        download.download(tesseract.ENGINE["url"], installer, force=True)
        tesseract.verify_archive(installer, tesseract.ENGINE["sha256"])

        # 3. Extract the payload (never execute it) into the ASCII engine dir.
        process.log(f"extracting the engine payload -> {tesseract.engine_dir()}", "step")
        info = tesseract.install_engine(installer, extractor)

        # 4. Language data, same pinning discipline.
        process.log("downloading pinned traineddata (eng, rus)", "step")
        langs = tesseract.install_languages()
    finally:
        paths.remove_quietly(tmp, recursive=True)

    result.update(
        engine=info.get("engine"),
        engineVersion=tesseract.engine_version(info.get("engine")),
        languages=tesseract.language_list(),
    )
    if not paths.is_file(engine) or result["engineVersion"] is None:
        result["note"] = "engine extracted but `tesseract --version` did not report a version"
        process.log(f"tesseract: {result['note']}", "warn")
        return result

    result["ok"] = all(lang in result["languages"] for lang in tesseract.DEFAULT_LANGUAGES)
    modes.note(
        f"provisioned Tesseract {tesseract.ENGINE['version']} "
        f"({', '.join(langs['languages'])})"
    )
    return result


# ---------------------------------------------------------------------------
# PDF toolchain
# ---------------------------------------------------------------------------

def install_pdf_dependencies(modes: Modes, python: str | None, requirements: str) -> dict:
    """Ensure the PDF -> Markdown Python dependencies are present.

    Installed with ``pip --user`` into the interpreter this repo already uses, so
    there is no second interpreter to maintain and no elevation needed.
    """
    result = {
        "ok": False, "python": python, "pythonVersion": None,
        "tesseract": None, "tesseractVersion": None, "note": None,
    }

    # Report the engine THIS toolchain resolves (toolchain engine -> system -> PATH)
    # rather than only probing Program Files, so the report agrees with
    # ``ocr.available()``. Provisioning happens in install_tesseract().
    tess, _reason = tesseract.resolve_engine()
    if tess:
        result["tesseract"] = tess
        result["tesseractVersion"] = tesseract.engine_version(tess)

    if not python:
        result["note"] = "Python not found. Install with: winget install Python.Python.3.12"
        process.log(f"PDF toolchain skipped: {result['note']}", "warn")
        return result

    result["pythonVersion"] = tools.tool_version(python, ["--version"])

    if not paths.is_file(requirements):
        result["note"] = "requirements-pdf.txt not found"
        process.log(f"PDF toolchain skipped: {result['note']}", "warn")
        return result

    if not modes.may_write:
        process.log(f"would pip install -r {requirements} into {python}", "step")
        result["ok"] = True
        return result

    before = tools.pip_shim_snapshot()
    process.log("installing Python PDF dependencies (pip --user)", "step")
    if tools.pip_install(python, ["-r", requirements]) != 0:
        result["note"] = "pip install failed"
        process.log(f"PDF toolchain: {result['note']}", "warn")
        return result

    removed = tools.remove_new_pip_shims(before)
    if removed:
        process.log(f"removed {removed} unused pip entry-point shim(s)")

    # Confirm the imports actually resolve, so a partial install is reported as a
    # failure here rather than surfacing later as an opaque readpdf error.
    if not tools.python_module_ok(python, "pymupdf4llm"):
        result["note"] = "PDF dependencies installed but not importable"
        process.log(f"PDF toolchain: {result['note']}", "warn")
        return result

    result["ok"] = True
    modes.note("installed PDF python dependencies")
    return result


# ---------------------------------------------------------------------------
# Self-test speech dependency
# ---------------------------------------------------------------------------

def install_selftest_dependencies(modes: Modes, python: str | None, requirements: str) -> dict:
    """Ensure the self-test's TTS dependency (pyttsx3) is present.

    pyttsx3 is NOT a runtime dependency of the pipeline: ffmpeg, whisper.cpp and
    yt-dlp all work without it. It is only needed by the self-test's speech step.
    But the project rule is "never claim success without the self-test passing",
    and without pyttsx3 the end-to-end chain SKIPS instead. So the installer
    provisions it here, which is what makes a green result mean the chain ran.

    A failure is reported, never raised: the self-test degrades to a SKIP, exactly
    as readpdf does when its toolchain is absent, and that must not fail setup.
    """
    result = {"ok": False, "python": python, "note": None}

    if not python:
        result["note"] = "Python not found; the self-test TTS step will be SKIPPED"
        process.log(f"self-test dependency: {result['note']}", "warn")
        return result

    # Already importable: nothing to do, and nothing to download.
    if tools.python_module_ok(python, "pyttsx3"):
        result["ok"] = True
        process.log("self-test speech dependency present: pyttsx3")
        return result

    if not paths.is_file(requirements):
        result["note"] = "requirements-selftest.txt not found"
        process.log(f"self-test dependency: {result['note']}", "warn")
        return result

    if not modes.may_write:
        process.log(f"would pip install -r {requirements} into {python}", "step")
        result["ok"] = True
        return result

    before = tools.pip_shim_snapshot()
    process.log("installing the self-test speech dependency (pip --user)", "step")
    if tools.pip_install(python, ["-r", requirements]) != 0:
        result["note"] = "pip install failed; the self-test TTS step will be SKIPPED"
        process.log(f"self-test dependency: {result['note']}", "warn")
        return result

    removed = tools.remove_new_pip_shims(before)
    if removed:
        process.log(f"removed {removed} unused pip entry-point shim(s)")

    if not tools.python_module_ok(python, "pyttsx3"):
        result["note"] = "installed but not importable; the self-test TTS step will be SKIPPED"
        process.log(f"self-test dependency: {result['note']}", "warn")
        return result

    result["ok"] = True
    modes.note("installed the self-test speech dependency (pyttsx3)")
    return result


# ---------------------------------------------------------------------------
# CLI + bootstrap + skills
# ---------------------------------------------------------------------------

def deploy_cli(modes: Modes) -> str:
    """Copy the CLI package to a stable ASCII location, plus its launcher.

    Skills invoke the CLI by an absolute, stable path so they work from any
    project and never depend on PATH or on where this repo was cloned.
    """
    dest_dir = paths.env_path("bin", "zoombie")
    launcher = os.path.join(dest_dir, "zoombie.cmd")

    if not modes.may_write:
        process.log(f"would install CLI -> {dest_dir}", "step")
        return launcher

    source_dir = env_mod.cli_dir()
    paths.ensure_dir(dest_dir)
    paths.copy_tree(os.path.join(source_dir, "zoombie"), os.path.join(dest_dir, "zoombie"))

    # The PDF helper lives beside the package, and the installed layout mirrors
    # the repo layout, so Invoke-ReadPdf resolves it relative to the package.
    pdf_source = os.path.join(source_dir, "pdf")
    if paths.is_dir(pdf_source):
        paths.copy_tree(pdf_source, os.path.join(dest_dir, "pdf"))

    requirements = os.path.join(source_dir, "requirements-pdf.txt")
    if paths.is_file(requirements):
        paths.copy_file(requirements, os.path.join(dest_dir, "requirements-pdf.txt"))

    # The self-test requirements travel too, so the documented remedy
    # (`pip install --user -r requirements-selftest.txt`) works from the deployed
    # directory and not only from a checkout.
    selftest_requirements = os.path.join(source_dir, "requirements-selftest.txt")
    if paths.is_file(selftest_requirements):
        paths.copy_file(
            selftest_requirements, os.path.join(dest_dir, "requirements-selftest.txt")
        )

    write_launcher(launcher, dest_dir)
    process.log(f"CLI installed: {launcher}")
    return launcher


def write_launcher(launcher: str, dest_dir: str) -> None:
    """Write the stable ``.cmd`` shim skills call.

    The shim sets ``PYTHONPATH`` to the directory holding the ``zoombie`` package
    and runs it as a module, so the CLI works from ANY working directory and does
    not depend on an installed package or on the caller's cwd.
    """
    content = (
        "@echo off\r\n"
        "rem zoombie CLI launcher: runs the package as a module from its own\r\n"
        "rem directory so it works from any working directory.\r\n"
        'set "PYTHONPATH=%~dp0"\r\n'
        'set "PYTHONUTF8=1"\r\n'
        'python -m zoombie %*\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )
    paths.ensure_dir(dest_dir)
    with open(paths.to_extended(launcher), "w", encoding="ascii", newline="") as handle:
        handle.write(content)


def deploy_skills(modes: Modes) -> list[dict]:
    """Deploy ``skills/<name>/SKILL.md`` into the global skills root."""
    source_root = os.path.join(env_mod.cli_dir(), "..", "skills")
    source_root = os.path.normpath(source_root)

    if not paths.is_dir(source_root):
        process.log("no skills folder in the repo; skipping skill deployment", "warn")
        return []

    if not modes.may_write:
        process.log(f"would deploy skills -> {skills.skills_root()}", "step")
        return []

    return skills.deploy(source_root, SKILL_VERSION)


def deploy_modes(modes: Modes) -> list[dict]:
    """Deploy ``modes/<name>.yaml`` into the global Zoo Code ``custom_modes.yaml``.

    Unlike a skill, the target is a document the user also owns, so this is a
    merge rather than an overwrite: only our slug is replaced and every foreign
    mode is preserved. In ``-Check``/``-DryRun`` the merge is still computed --
    so the reported action is truthful -- but nothing is written.
    """
    source_root = modes_mod.source_dir(env_mod.cli_dir())
    if not paths.is_dir(source_root):
        process.log("no modes folder in the repo; skipping mode deployment", "warn")
        return []

    target = modes_mod.global_modes_path()

    if not modes.may_write:
        planned = modes_mod.deploy_all(source_root, target, dry_run=True, force=modes.force)
        for record in planned:
            process.log(f"mode '{record['slug']}' would be {record['action']} -> {target}", "step")
        return planned

    results = modes_mod.deploy_all(source_root, target, force=modes.force)
    for record in results:
        modes.note(f"mode {record['slug']}: {record['action']}")
    # Recorded so a deployed role can be identified after the fact. The action
    # itself is content-based, so this can never go stale the way a marker would.
    for record in results:
        record["roleVersion"] = ROLE_VERSION
    return results

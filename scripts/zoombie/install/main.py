"""The installer/updater flow: detect, then install each component in order.

The three modes (``check``/``dry_run``/apply) are carried explicitly in one
:class:`components.Modes` value, rather than passed around as loose flags.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from .. import SKILL_VERSION
from ..lib import cublas, env as env_mod, manifest, paths, process, tools, whisper
from . import components, hardware


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m zoombie.install",
        description="Install or update the zoombie toolchain into an ASCII root.",
    )
    parser.add_argument("-Check", "--check", action="store_true", help="detect only; write nothing")
    parser.add_argument("-DryRun", "--dry-run", action="store_true", help="plan only; write nothing")
    parser.add_argument("-Model", "--model", default=None, help="whisper model name to ensure")
    parser.add_argument("-Root", "--root", default=None, help="override the toolchain root")
    parser.add_argument("-Force", "--force", action="store_true", help="re-download even if present")
    return parser


def find_python() -> str | None:
    """Locate the interpreter to install the Python dependencies into."""
    process.refresh_path_from_registry()
    resolved = tools.find_python()
    if resolved and not tools.is_store_stub(resolved):
        return resolved
    candidates = tools.find_python_candidates()
    found = tools.resolve("python", candidates=candidates)
    if found and not tools.is_store_stub(found):
        return found
    return None


def is_gpu_ignored(hardware_backend: str, installed_backend: str | None) -> bool:
    """True when hardware advertises a GPU but the effective backend is the CPU.

    This is the silent-CPU-on-a-GPU-machine case: the chooser legitimately prefers
    the CPU for an integrated GPU (shared memory) or when no GPU asset ships, but
    the caller must still be told the GPU is unused. Extracted so the policy is
    testable without running the whole installer.
    """
    return hardware_backend in ("cuda", "vulkan") and installed_backend == "cpu"


def run(argv: list[str] | None = None, *, write_result: bool = True) -> int:
    args = build_parser().parse_args(argv)

    if args.root:
        os.environ["ZOOMBIE_ENV_ROOT"] = args.root

    modes = components.Modes(check=args.check, dry_run=args.dry_run, force=args.force)
    mode_name = "CHECK" if args.check else ("DRY-RUN" if args.dry_run else "APPLY")
    root = paths.env_root()

    process.log(f"zoombie setup [{mode_name}]", "step")
    process.log(f"toolchain root: {root}")

    result: dict = {"mode": mode_name, "root": root}
    error: str | None = None
    ok = False

    try:
        ok, result, error = _install(modes, args, root, result)
    except Exception as exc:  # noqa: BLE001 - the contract is one clean result line
        error = f"{type(exc).__name__}: {exc}"
        ok = False
        process.log(error, "error")

    if write_result:
        process.write_result("setup", ok=ok, data=result, error=error)
    return 0 if ok else 1


def _install(modes: components.Modes, args, root: str, result: dict) -> tuple[bool, dict, str | None]:
    """Run the install sequence. Returns ``(ok, data, error)``."""
    # --- root validation ---------------------------------------------------
    # The root must be ASCII because whisper.cpp breaks on non-ASCII paths.
    if not paths.is_ascii(root):
        if paths.root_is_default():
            fallback = os.path.join(os.environ.get("PUBLIC", ""), paths.ROOT_FOLDER_NAME)
            if os.environ.get("PUBLIC") and paths.is_ascii(fallback):
                os.environ["ZOOMBIE_ENV_ROOT"] = fallback
                root = fallback
                process.log(f"%USERPROFILE% is not ASCII; using {root} instead", "warn")
            else:
                raise RuntimeError(
                    "No ASCII toolchain root available. Pass an explicit -Root on an "
                    "ASCII path (Cyrillic paths break whisper.cpp)."
                )
        else:
            raise RuntimeError(
                f"The -Root you passed is not ASCII (Cyrillic paths break "
                f"whisper.cpp): {root}"
            )

    # The ASCII invariant has a sibling, checked at the only moment it can still
    # be fixed: install time. whisper.cpp opens the model with fopen and loads its
    # sibling DLLs through the loader search path, so neither can use the extended
    # prefix. An over-long root installs cleanly and then fails at the first
    # transcription, which is the class of silent breakage this must prevent.
    root_paths = paths.root_path_report(root)
    process.log(
        "root path lengths: root={0} exe={1} model={2} budget={3} longPathsEnabled={4}".format(
            root_paths["rootLength"], root_paths["whisperExeLength"],
            root_paths["modelPathLength"], root_paths["budget"],
            root_paths["longPathsEnabled"],
        )
    )
    if not root_paths["whisperExeFits"] or not root_paths["modelPathFits"]:
        deepest = max(root_paths["whisperExeLength"], root_paths["modelPathLength"])
        message = (
            f"The toolchain root is too deep for whisper.cpp: '{root}' is "
            f"{root_paths['rootLength']} characters, and the paths whisper-cli must "
            f"open reach {deepest} (the usable limit is {root_paths['budget']}). "
            "whisper.cpp cannot use \\\\?\\ paths, so this must be fixed now. Pass a "
            "shorter -Root (for example C:\\zoombie-env) or set ZOOMBIE_ENV_ROOT."
        )
        if modes.may_write:
            raise RuntimeError(message)
        process.log(message, "warn")

    # --- hardware + python -------------------------------------------------
    process.log("probing hardware", "step")
    hw = hardware.profile()
    process.log(f"cpu={hw.cpu_name} cores={hw.cpu_cores}/{hw.cpu_threads} ram={hw.ram_gb}GB")
    process.log(
        f"gpu={', '.join(hw.gpus)} nvidia={hw.nvidia} vramMb={hw.vram_mb} "
        f"backend={hw.backend}"
    )

    process.log("checking Python", "step")
    python = find_python()
    if python:
        process.log(f"python: {python} ({tools.tool_version(python, ['--version'])})")
    else:
        process.log(
            "python not found. Optional for this pipeline. Install with: "
            "winget install Python.Python.3.12",
            "warn",
        )

    # --- components --------------------------------------------------------
    process.log("ensuring ffmpeg/ffprobe", "step")
    components.install_ffmpeg(modes)

    process.log("ensuring yt-dlp", "step")
    ytdlp_info = components.install_ytdlp(modes, python)

    process.log("ensuring whisper.cpp", "step")
    whisper_info = components.install_whisper(modes, hw)

    # Prove which backend whisper-cli can ACTUALLY initialise. Only a probe reports
    # what is usable, which is how a CUDA install missing its cuBLAS runtime
    # becomes visible at install time rather than as hours of CPU transcription.
    process.log("probing whisper backend (what it can actually initialise)", "step")
    whisper_exe = whisper_info.get("exe")
    backend_probe = whisper.probe_backend(whisper_exe)
    backend_configured = whisper_info.get("backend")
    cuda_major = whisper_info.get("cudaMajor") or 0
    cuda_runtime = cublas.runtime_status(whisper_exe, int(cuda_major) if cuda_major else 0)
    if backend_probe.device:
        process.log(f"backend observed: {backend_probe.device} (configured: {backend_configured})")
    else:
        process.log(f"backend not observed: {backend_probe.reason}", "warn")
    if backend_configured == "cuda" and not cuda_runtime.ready:
        process.log(f"CUDA build is missing its runtime: {', '.join(cuda_runtime.missing)}", "warn")

    model_name = args.model or hardware.recommend_model(hw)
    process.log(f"ensuring whisper model '{model_name}'", "step")
    model_info = components.install_model(modes, model_name)

    process.log("installing the CLI", "step")
    cli_path = components.deploy_cli(modes)

    process.log("deploying skills to the global root", "step")
    skill_results = components.deploy_skills(modes)

    process.log("deploying the Zoombie role to the global custom modes", "step")
    mode_results = components.deploy_modes(modes)

    process.log("ensuring the PDF -> Markdown toolchain", "step")
    requirements = os.path.join(env_mod.cli_dir(), "requirements-pdf.txt")
    pdf_info = components.install_pdf_dependencies(modes, python, requirements)
    if pdf_info.get("tesseract"):
        process.log(f"tesseract present: {pdf_info['tesseract']}")
    else:
        process.log(
            "tesseract not found (optional; OCR fallback unavailable). Install with: "
            "winget install UB-Mannheim.TesseractOCR"
        )

    # The self-test's own dependency (pyttsx3, for the TTS step). It is NOT a
    # runtime dependency of the pipeline, but the project rule is "never claim
    # success without the self-test passing", and without it the end-to-end chain
    # SKIPS. Installing it here is what makes a green result mean the chain ran.
    process.log("ensuring the self-test speech dependency", "step")
    selftest_requirements = os.path.join(env_mod.cli_dir(), "requirements-selftest.txt")
    selftest_info = components.install_selftest_dependencies(
        modes, python, selftest_requirements
    )

    # --- manifest ---------------------------------------------------------
    built = _build_manifest(
        root=root, python=python, python_version=tools.tool_version(python, ["--version"]),
        ytdlp_info=ytdlp_info, whisper_info=whisper_info, backend_probe=backend_probe,
        cuda_runtime=cuda_runtime, model_info=model_info, pdf_info=pdf_info,
        selftest_info=selftest_info, mode_results=mode_results, hw=hw,
    )
    if modes.may_write:
        manifest.save(built)
        process.log(f"manifest written: {paths.manifest_path()}")

    # --- missing + policy --------------------------------------------------
    missing: list[str] = []
    if not paths.is_file(paths.env_path("bin", "ffmpeg.exe")):
        missing.append("ffmpeg")
    if not paths.is_file(paths.env_path("bin", "ffprobe.exe")):
        missing.append("ffprobe")
    if not ytdlp_info.get("version"):
        missing.append("yt-dlp")
    if not paths.is_file(paths.env_path("bin", "whisper", "whisper-cli.exe")):
        missing.append("whisper-cli")
    # A CUDA install missing its cuBLAS runtime is not usable: whisper.cpp would
    # silently transcribe on the CPU and still exit 0. Name the exact DLLs.
    if backend_configured == "cuda" and not cuda_runtime.ready:
        missing += [f"whisper-cuda-runtime ({dll})" for dll in cuda_runtime.missing]
    if not paths.is_dir(paths.env_path("models")):
        missing.append("models")
    if modes.check and not pdf_info.get("ok"):
        missing.append("pdf-deps")
    if modes.check and not pdf_info.get("tesseract"):
        missing.append("tesseract (optional)")
    # pyttsx3 is deliberately NOT in `missing`: without it the self-test degrades
    # to a reported SKIP rather than a failure, exactly as readpdf does. It is
    # reported as its own field instead of failing the whole install.

    # POLICY 1 (hard): a GPU is fitted and the build is CUDA, yet the backend
    # cannot initialise. That is a hard failure, not a note. CHECK/DRY-RUN stay
    # informational because they are allowed to inspect an unfinished install.
    cuda_mismatch = (
        backend_configured == "cuda"
        and backend_probe.device
        and backend_probe.device != "cuda"
    )
    cuda_policy_failed = (
        backend_configured == "cuda"
        and (cuda_mismatch or not cuda_runtime.ready)
        and modes.may_write
    )
    if cuda_policy_failed:
        process.log(
            "GPU policy: a CUDA-capable GPU is fitted but the CUDA backend cannot "
            "initialise; refusing to leave an install that would silently run on the CPU",
            "warn",
        )

    # POLICY 2 (warning): a GPU is present but the effective backend is the CPU.
    # This is NOT a failure -- the chooser deliberately prefers the CPU for an
    # integrated GPU, and a substitution is unavoidable when no GPU asset ships --
    # but the user must be told the GPU is unused, in plain language, instead of
    # having to read the self-test log to discover it.
    requested_backend = whisper_info.get("requestedBackend") or hw.backend
    backend_substituted = bool(whisper_info.get("substituted"))
    gpu_ignored = is_gpu_ignored(hw.backend, backend_configured)
    if gpu_ignored:
        reason = hw.backend_reason or f"hardware advertises '{hw.backend}' but the CPU build will run"
        process.log(
            f"GPU policy: a GPU is fitted but the effective backend is 'cpu'; GPU "
            f"acceleration is unused. Reason: {reason}",
            "warn",
        )

    if modes.check:
        process.log(f"CHECK complete. Missing: {', '.join(missing) if missing else 'none'}", "step")

    ok = (not missing or modes.check or modes.dry_run) and not cuda_policy_failed
    result.update({
        "cli": cli_path,
        "skills": skill_results,
        "modes": mode_results,
        "missing": missing,
        "changes": modes.changes,
        "gpuPolicy": {
            "enforced": cuda_policy_failed,
            "hardwareBackend": hw.backend,
            "installedBackend": backend_configured,
            "requestedBackend": requested_backend,
            "backendSubstituted": backend_substituted,
            "gpuIgnored": gpu_ignored,
            "backendObserved": backend_probe.device,
            "cudaRuntimeReady": cuda_runtime.ready,
        },
        "manifest": built,
    })

    if cuda_policy_failed:
        error = (
            "a CUDA-capable GPU is fitted but the CUDA backend cannot initialise "
            f"(observed '{backend_probe.device}'); the install would silently use the CPU"
        )
    elif missing and not modes.check:
        error = f"Missing after setup: {', '.join(missing)}"
    else:
        error = None
    return ok, result, error


def _build_manifest(
    *, root, python, python_version, ytdlp_info, whisper_info, backend_probe,
    cuda_runtime, model_info, pdf_info, selftest_info, mode_results, hw,
) -> dict:
    """Assemble env.json.

    Built as a plain nested dict: the PowerShell version needed an incremental
    build with a StrictMode-safe field reader because one bad member reference in
    a large literal aborted the whole statement. Python dicts have no such hazard.
    """
    return {
        "zoombieVersion": SKILL_VERSION,
        "updatedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "root": root,
        "asciiRoot": paths.is_ascii(root),
        "python": {"path": python, "version": python_version},
        "ffmpeg": {
            "path": paths.env_path("bin", "ffmpeg.exe"),
            "version": tools.tool_version(paths.env_path("bin", "ffmpeg.exe"), ["-version"]),
        },
        "ffprobe": {
            "path": paths.env_path("bin", "ffprobe.exe"),
            "version": tools.tool_version(paths.env_path("bin", "ffprobe.exe"), ["-version"]),
        },
        "ytDlp": {
            "via": "python -m yt_dlp",
            "python": python,
            "version": ytdlp_info.get("version"),
            "ok": bool(ytdlp_info.get("ok")),
            "note": ytdlp_info.get("note"),
        },
        "whisper": {
            "path": whisper_info.get("exe"),
            "tag": whisper_info.get("tag"),
            "asset": whisper_info.get("asset"),
            "backend": whisper_info.get("backend"),
            # backendDetected is the CURRENT machine's verdict, kept beside
            # `backend` (the installed build) so a machine that gained or lost a
            # GPU is visible instead of being masked by a sticky old value.
            "backendDetected": hw.backend,
            # The reason the chooser picked the backend, in plain language.
            "backendReason": hw.backend_reason,
            # requestedBackend is the chooser's ask; `backend` is what was actually
            # installed. They differ exactly when a substitution was unavoidable
            # (no asset shipped for the requested backend), and recording BOTH is
            # what lets the next run recognise the substitution and stop
            # re-downloading the CPU build on every invocation.
            "requestedBackend": whisper_info.get("requestedBackend"),
            "backendSubstituted": bool(whisper_info.get("substituted")),
            "backendRecomputed": whisper_info.get("backend") == hw.backend,
            # Recorded separately because they answer different questions:
            # `backend` is what we asked for, `backendObserved` is what
            # whisper-cli can really initialise.
            "backendObserved": backend_probe.device,
            "cudaRuntimeReady": cuda_runtime.ready,
            "cudaRuntime": cuda_runtime.to_report(),
        },
        "selftest": {
            "python": selftest_info.get("python"),
            "ok": bool(selftest_info.get("ok")),
            "note": selftest_info.get("note"),
        },
        "model": {
            "path": model_info.get("path"),
            "name": model_info.get("name"),
            "sizeMb": model_info.get("sizeMb", 0),
        },
        "pdf": {
            "python": pdf_info.get("python"),
            "pythonVersion": pdf_info.get("pythonVersion"),
            "ok": bool(pdf_info.get("ok")),
            "tesseract": pdf_info.get("tesseract"),
            "tesseractVersion": pdf_info.get("tesseractVersion"),
            "note": pdf_info.get("note"),
        },
        # The deployed Zoo Code role. The target path is taken from the records
        # rather than recomputed, so the manifest names the file that was actually
        # written (which ZOOMBIE_MODES_PATH can redirect).
        "modes": {
            "path": mode_results[0]["path"] if mode_results else None,
            "entries": mode_results,
        },
        "hardware": hw.to_dict(),
    }


if __name__ == "__main__":
    sys.exit(run())

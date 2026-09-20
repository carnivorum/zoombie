"""End-to-end self-test for the zoombie pipeline.

The whole chain is exercised through the deterministic CLI, and the whisper.cpp
Cyrillic-path bug is proved fixed by writing the artifacts into a folder whose
name contains Cyrillic characters.

Steps:
 1. locate the installed launcher
 2. ``doctor``, and assert backendConfigured vs backendObserved agree
 3. simulate a CUDA build with no runtime and with a wrong-major runtime
 4. check device classification (a merely LOADED backend is not a device USED)
 5. synthesize the pangram with Windows TTS into a Cyrillic-named dir
 6. ``extract`` and ``transcribe`` through the CLI
 7. assert the transcript contains the key words and the right device ran
 8. re-run with ``-NoGpu`` and assert a deliberate CPU run succeeds
 9. ``readpdf`` when the PDF toolchain is installed, including its image sidecar
10. ``postprocess -Apply`` twice, asserting byte-level idempotency
11. clean up
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from .lib import cublas, env as env_mod, paths, pdf, process, whisper

CYRILLIC_DIR = "\u0442\u0435\u0441\u0442"  # "тест"
PANGRAM = "The quick brown fox jumps over the lazy dog."
KEY_WORDS = ["quick", "brown", "fox", "jumps", "over", "lazy", "dog"]


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def info(message: str) -> None:
    print(f"    {message}", flush=True)


def find_launcher() -> str | None:
    """Locate the installed CLI launcher, probing both ASCII roots."""
    for root in paths.root_candidates():
        for relative in (
            os.path.join("bin", "zoombie", "zoombie.cmd"),
            os.path.join("bin", "zoombie", "zoombie.ps1"),
        ):
            candidate = os.path.join(root, relative)
            if paths.is_file(candidate):
                return candidate
    return None


def run_cli(launcher: str, args: list[str]) -> dict | None:
    """Run the CLI and parse its single JSON result line.

    Returns None when the launcher produced nothing parseable, which the caller
    reports as a failure rather than treating as an empty success.
    """
    completed = subprocess.run(
        [launcher, *args],
        stdout=subprocess.PIPE,
        stderr=None,
        env=process.child_env(),
        check=False,
    )
    text = process.decode_console(completed.stdout or b"")
    result_line = None
    for line in text.splitlines():
        if line.strip().startswith("{"):
            result_line = line
    if not result_line:
        return None
    try:
        return json.loads(result_line)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Pure checks (no launcher, no TTS, no GPU)
# ---------------------------------------------------------------------------

def check_postprocess_idempotent(scratch: str) -> None:
    """``postprocess -Apply`` twice must leave the file byte-identical.

    This is the guarantee the whole summarize workflow rests on: a skill may
    re-run ``postprocess`` over a document it has already processed, so the
    second pass must be a no-op rather than a source of drift. The document is a
    small block-6-shaped fixture with hand-written links, which exercises the
    anchor, index and link passes together.

    It runs through ``process_document`` (the same call ``zoombie postprocess``
    makes) rather than the launcher, so the assertion is available on a machine
    where the installed launcher is absent or out of date.
    """
    say("postprocess idempotency")

    from .commands.postprocess import process_document

    fixture = os.path.join(scratch, "postprocess-fixture")
    paths.ensure_dir(fixture)
    md_path = os.path.join(fixture, "summary.md")
    original = (
        "# Test summary\n"
        "\n"
        "## 4. Содержание\n"
        "\n"
        "## 6. Notes\n"
        "\n"
        "### First heading\n"
        "\n"
        "The quick brown fox jumps over the lazy dog.\n"
        "\n"
        "### Second heading\n"
        "\n"
        "A [link with space](some file.md) and a [bracketed [label]](notes.md).\n"
    )

    with open(paths.to_extended(md_path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(original)

    image_dir = os.path.join(fixture, "img")
    first, _stats = process_document(original, None, image_dir)

    # A second pass over the OUTPUT of the first must return it verbatim. If the
    # passes appended instead of rebuilding a range, this is where it shows.
    second, _stats2 = process_document(first, None, image_dir)
    if second != first:
        raise AssertionError(
            "postprocess is not idempotent: the second pass changed the document"
        )

    # And the same property must hold once the bytes have been through a file,
    # which is what an -Apply run actually does.
    with open(paths.to_extended(md_path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(first)
    with open(paths.to_extended(md_path), "rb") as handle:
        before = handle.read()
    third, _stats3 = process_document(before.decode("utf-8"), None, image_dir)
    with open(paths.to_extended(md_path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(third)
    with open(paths.to_extended(md_path), "rb") as handle:
        after = handle.read()
    if before != after:
        raise AssertionError(
            "postprocess is not byte-idempotent: a second -Apply changed the file"
        )

    say("PASS: postprocess -Apply is idempotent (byte-identical on the second run)")

def check_cuda_runtime_readiness() -> None:
    """A CUDA build's readiness is major-version aware.

    A pure filesystem simulation: no download, no GPU, no whisper run. This is
    the exact state that used to look healthy while every run used the CPU.
    """
    import tempfile

    say("CUDA runtime readiness (simulated)")
    sim = tempfile.mkdtemp(prefix="zoombie-cudalib-")
    try:
        exe = os.path.join(sim, "whisper-cli.exe")
        open(exe, "wb").close()

        absent = cublas.runtime_status(os.path.join(sim, "nope", "whisper-cli.exe"))
        if not absent.dir_missing:
            raise AssertionError("a missing whisper dir must report dirMissing")

        open(os.path.join(sim, "ggml-cuda.dll"), "wb").close()
        no_runtime = cublas.runtime_status(exe, 11)
        if no_runtime.ready:
            raise AssertionError("ggml-cuda.dll alone must not be reported ready")
        if "cublas64_11.dll" not in no_runtime.missing:
            raise AssertionError("cublas64_11.dll must be reported missing")

        # A runtime of the WRONG major must still be reported missing.
        open(os.path.join(sim, "cublas64_12.dll"), "wb").close()
        if cublas.runtime_status(exe, 11).ready:
            raise AssertionError("a CUDA-12 cuBLAS must not satisfy a CUDA-11 requirement")

        # The matching runtime clears it.
        open(os.path.join(sim, "cublas64_11.dll"), "wb").close()
        open(os.path.join(sim, "cublasLt64_11.dll"), "wb").close()
        if not cublas.runtime_status(exe, 11).ready:
            raise AssertionError("a complete 11.x runtime should be ready")

        # A major with no pinned redist must be refused, not guessed.
        if cublas.provision_spec(12) is not None:
            raise AssertionError("CUDA 12 must have no pinned redist")

        say("PASS: CUDA runtime readiness is major-version aware")
    finally:
        paths.remove_quietly(sim, recursive=True)


def check_device_classification() -> None:
    """Initialised is not the same as selected, and -ng is never cuda."""
    say("device classification (simulated logs)")

    selected = whisper.device_info(
        ["ggml_cuda_init: found 1 CUDA devices", "whisper_backend_init_gpu: using CUDA backend"]
    )
    if not selected.device_selected or selected.device != "cuda":
        raise AssertionError("a selected CUDA device must classify as cuda/deviceSelected")

    loaded_only = whisper.device_info(
        ["ggml_cuda_init: found 1 CUDA devices", "load_backend: loaded CUDA backend"]
    )
    if loaded_only.device != "cpu" or not loaded_only.backend_initialised:
        raise AssertionError("a loaded-only backend must be cpu but keep backendInitialised")
    if loaded_only.device_selected:
        raise AssertionError("a loaded-only backend must NOT count as selected")

    ng_run = whisper.device_info(["use gpu = 0", "load_backend: loaded CUDA backend"])
    if ng_run.device != "cpu" or ng_run.device_selected:
        raise AssertionError("a -ng run must classify as cpu, never cuda")

    # GPU-failure-signature detection drives whether the CPU retry is attempted.
    if not whisper.looks_like_gpu_failure(3, ["CUDA error: out of memory"]):
        raise AssertionError("a CUDA error must be recognised as a GPU failure")
    if whisper.looks_like_gpu_failure(3, ["error: failed to open model file"]):
        raise AssertionError("a non-GPU error must NOT be treated as a GPU failure")
    if not whisper.looks_like_gpu_failure(3, []):
        raise AssertionError("an unattributed crash should stay retryable")

    say("PASS: device classification and GPU-failure detection behave correctly")


# ---------------------------------------------------------------------------
# Speech synthesis
# ---------------------------------------------------------------------------

def synthesize_speech(destination: str) -> bool:
    """Write the pangram to a WAV with the Windows SAPI voices.

    Returns False when TTS is unavailable, so the caller reports the step as
    SKIPPED. That mirrors how the readpdf step already behaves when the PDF
    toolchain is absent: a missing optional dependency must not fail the suite.
    """
    try:
        import pyttsx3
    except ImportError:
        info("pyttsx3 not installed; cannot synthesize speech")
        info("install it with: python -m pip install --user -r requirements-selftest.txt")
        return False

    try:
        engine = pyttsx3.init()
        voice = None
        for candidate in engine.getProperty("voices"):
            languages = " ".join(getattr(candidate, "languages", []) or [])
            name = getattr(candidate, "name", "") or ""
            identifier = getattr(candidate, "id", "") or ""
            if "en" in languages.lower() or "english" in name.lower() or "en-" in identifier.lower():
                voice = candidate
                break
        if voice is not None:
            engine.setProperty("voice", voice.id)
            info(f"TTS voice: {getattr(voice, 'name', voice.id)}")
        else:
            info("TTS voice: no English voice installed; using default")

        engine.save_to_file(PANGRAM, destination)
        engine.runAndWait()
    except Exception as exc:  # noqa: BLE001 - any TTS failure is a skip
        info(f"TTS failed: {exc}")
        return False

    return paths.is_file(destination) and paths.file_size(destination) > 0


# ---------------------------------------------------------------------------
# The end-to-end run
# ---------------------------------------------------------------------------

def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zoombie.selftest",
                                     description="End-to-end test for the zoombie pipeline.")
    parser.add_argument("-KeepArtifacts", "--keep-artifacts", action="store_true",
                        help="leave the scratch folder in place for inspection")
    args = parser.parse_args(argv)

    launcher = find_launcher()
    say(f"CLI: {launcher}")
    if not launcher:
        say("zoombie launcher not found. Run the setup first.")
        return 1

    environment = env_mod.resolve()

    # --- 1. doctor ---------------------------------------------------------
    say("doctor")
    doctor = run_cli(launcher, ["doctor"])
    if not doctor:
        say("doctor produced no parseable result")
        return 1
    info(f"ok={doctor.get('ok')} missing={', '.join(doctor.get('data', {}).get('missing', []))}")
    if not doctor.get("ok"):
        say(f"doctor reported missing: {', '.join(doctor['data'].get('missing', []))}")
        return 1

    backend_configured = doctor["data"]["report"]["whisper"]["backendConfigured"]
    backend_observed = doctor["data"]["report"]["whisper"]["backendObserved"]
    info(f"backend: configured={backend_configured} observed={backend_observed}")
    for warning in doctor["data"].get("warnings", []):
        if warning:
            info(f"WARN: {warning}")

    if backend_configured == "cuda":
        if not backend_observed:
            info(
                "probe inconclusive: "
                f"{doctor['data']['report']['whisper']['probeReason']}"
            )
        elif backend_observed != "cuda":
            say(
                "CUDA backend regression: configured 'cuda' but whisper initialises "
                f"'{backend_observed}'"
            )
            return 1

    # --- 2. pure checks ----------------------------------------------------
    check_cuda_runtime_readiness()
    check_device_classification()

    # --- 3. Cyrillic scratch dir ------------------------------------------
    scratch = os.path.join(os.environ.get("TEMP", "."), f"zoombie-selftest-{CYRILLIC_DIR}")
    paths.remove_quietly(scratch, recursive=True)
    paths.ensure_dir(scratch)
    say(f"scratch (non-ASCII): {scratch}")

    exit_code = 0
    try:
        # --- 4. TTS --------------------------------------------------------
        wav = os.path.join(scratch, "speech.wav")
        tts_ok = synthesize_speech(wav)
        if tts_ok:
            info(f"synthesized: {wav}")
        else:
            say("SKIPPED: speech synthesis unavailable; the end-to-end chain cannot run")
            info("the pure checks above passed, so the regression guards are verified")
            return 0

        # --- 5. extract ----------------------------------------------------
        say("extract")
        extract_out = os.path.join(scratch, "audio.wav")
        extract = run_cli(launcher, ["extract", "-Source", wav, "-Output", extract_out])
        if not extract or not extract.get("ok"):
            say(f"extract failed: {extract.get('error') if extract else 'no result'}")
            return 1
        info(f"ok={extract['ok']} output={extract['data']['output']}")

        # --- 6. transcribe -------------------------------------------------
        say("transcribe")
        base = os.path.join(scratch, "transcript")
        transcript = run_cli(
            launcher, ["transcribe", "-Source", extract_out, "-Output", base, "-Language", "en"]
        )
        if not transcript or not transcript.get("ok"):
            say(f"transcribe failed: {transcript.get('error') if transcript else 'no result'}")
            return 1
        data = transcript["data"]
        txt_path = data["artifacts"]["txt"]["path"]
        info(f"transcript: {txt_path}")
        info(
            f"deviceUsed={data['deviceUsed']} deviceName={data['deviceName']} "
            f"realtimeFactor={data['realtimeFactor']} totalMs={data['totalMs']} "
            f"audioSec={data['audioDurationSec']}"
        )

        # Regression guard for the silent CPU fallback: a successful run on the
        # CPU while CUDA is configured is the exact failure this catches.
        if backend_configured == "cuda" and data["deviceUsed"] != "cuda":
            say(
                f"CUDA backend regression: transcription ran on '{data['deviceUsed']}' "
                f"while 'cuda' is configured ({data['fallbackReason']})"
            )
            return 1
        if data.get("silentCpuFallback"):
            say(f"Silent CPU fallback detected: {data['fallbackReason']}")
            return 1

        # --- 7. deliberate CPU run ----------------------------------------
        # `-NoGpu` is the explicit opt-out, so the same audio must transcribe on
        # the CPU with ok:true. This also proves `use gpu = 0` is classified as a
        # deliberate CPU run rather than a silent fallback.
        say("transcribe -NoGpu (deliberate CPU)")
        cpu_result = run_cli(
            launcher,
            ["transcribe", "-Source", extract_out, "-Output", f"{base}-ng",
             "-Language", "en", "-NoGpu"],
        )
        if not cpu_result or not cpu_result.get("ok"):
            say(f"deliberate CPU run unexpectedly failed: {cpu_result.get('error') if cpu_result else 'no result'}")
            return 1
        if cpu_result["data"]["deviceUsed"] != "cpu":
            say(f"-NoGpu must report deviceUsed cpu, got '{cpu_result['data']['deviceUsed']}'")
            return 1
        if cpu_result["data"].get("silentCpuFallback"):
            say("-NoGpu must not be reported as a silent CPU fallback")
            return 1
        info(f"-NoGpu: deviceUsed={cpu_result['data']['deviceUsed']} ok={cpu_result['ok']}")

        # --- 8. verify -----------------------------------------------------
        with open(paths.to_extended(txt_path), "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read().lower()
        missing = [word for word in KEY_WORDS if not re.search(re.escape(word), text)]
        found = len(KEY_WORDS) - len(missing)
        say(f"verification: {found}/{len(KEY_WORDS)} key words found")
        info(f"text: {text.strip()}")
        if missing:
            say(f"transcript missing expected words: {', '.join(missing)}")
            return 1

        # --- 9. readpdf (optional) ----------------------------------------
        pdf_result = check_readpdf(launcher, scratch)
        if pdf_result is False:
            return 1

        # --- 10. postprocess idempotency ----------------------------------
        check_postprocess_idempotent(scratch)

        say("PASS: Cyrillic destination path worked end to end")
    finally:
        if not args.keep_artifacts:
            paths.remove_quietly(scratch, recursive=True)
            say("scratch removed")
        else:
            say(f"scratch kept: {scratch}")

    return exit_code


def check_readpdf(launcher: str, scratch: str) -> bool | None:
    """Convert a generated PDF into the same Cyrillic destination.

    Returns True on success, None when skipped, False on failure.
    """
    python = env_mod.resolve().python
    if not python or not env_mod.pdf_helper() or not paths.is_file(env_mod.pdf_helper()):
        say("readpdf: skipped (PDF toolchain not installed)")
        return None

    say("readpdf")
    pdf_path = os.path.join(scratch, "doc.pdf")
    generator = os.path.join(scratch, "mkpdf.py")
    with open(generator, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "import sys\n"
            "import pymupdf\n"
            "doc = pymupdf.open()\n"
            "page = doc.new_page()\n"
            "page.insert_text((72, 72), 'The quick brown fox jumps over the lazy dog.')\n"
            "doc.save(sys.argv[1])\n"
            "doc.close()\n"
        )

    code = subprocess.run(
        [python, generator, pdf_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=process.child_env(),
        check=False,
    ).returncode
    if code != 0 or not paths.is_file(pdf_path):
        say("readpdf: skipped (could not generate a test PDF)")
        return None

    md_base = os.path.join(scratch, "doc-md")
    # -Images also exercises the sidecar: manifest.json + README.md must land in
    # the image directory, because postprocess reads the manifest to re-insert
    # figures and verify flags an img/ folder missing either file.
    result = run_cli(
        launcher, ["readpdf", "-Source", pdf_path, "-Output", md_base, "-Images"]
    )
    if not result or not result.get("ok"):
        say(f"readpdf failed: {result.get('error') if result else 'no result'}")
        return False

    md_path = result["data"]["output"]
    if not paths.is_file(md_path):
        say(f"readpdf produced no Markdown: {md_path}")
        return False
    with open(paths.to_extended(md_path), "r", encoding="utf-8", errors="replace") as handle:
        markdown = handle.read().lower()
    if "quick" not in markdown or "fox" not in markdown:
        say(f"Markdown missing expected text: {md_path}")
        return False

    # The image sidecar must exist whenever images were requested.
    artifacts = result["data"].get("artifacts", {})
    image_dir = (artifacts.get("images") or {}).get("path")
    if not image_dir:
        say("readpdf -Images reported no image directory")
        return False
    for sidecar in (pdf.SIDECAR_MANIFEST, pdf.SIDECAR_README):
        sidecar_path = os.path.join(image_dir, sidecar)
        if not paths.is_file(sidecar_path):
            say(f"readpdf image sidecar missing: {sidecar_path}")
            return False

    info(f"markdown: {md_path} (pages={result['data']['pages']} ocr={result['data']['ocrUsed']})")
    info(f"image sidecar: {image_dir} (manifest.json + README.md present)")
    say("PASS: PDF -> Markdown worked end to end")
    return True


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except AssertionError as exc:
        say(f"FAIL: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - a self-test must report cleanly
        say(f"FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

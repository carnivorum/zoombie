#Requires -Version 5.1
<#
.SYNOPSIS
    End-to-end self-test for the zoombie pipeline.

.DESCRIPTION
    Exercises the whole chain through the deterministic CLI and, crucially,
    proves the whisper.cpp Cyrillic-path bug is fixed by writing the test
    artifacts into a folder whose name contains Cyrillic characters.

    Steps:
     1. locate the installed CLI (zoombie-env\bin\zoombie\zoombie.ps1)
      2. run `doctor`
      3. synthesize the pangram with SAPI TTS (English voice) -> a Cyrillic-named dir
      4. run `extract` via the CLI
      5. run `transcribe` via the CLI, output to the same Cyrillic dir
      6. verify the transcript contains the key words
      7. clean up

.PARAMETER KeepArtifacts
    Leave the scratch folder in place for inspection.

.PARAMETER NoCleanup
    Alias-style switch kept for clarity; same as -KeepArtifacts.
#>
[CmdletBinding()]
param(
    [switch]$KeepArtifacts,
    [switch]$NoCleanup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Say  { param([string]$m) Write-Host "==> $m" }
function Info { param([string]$m) Write-Host "    $m" }

# Find the installed CLI. The toolchain root may be %USERPROFILE%\zoombie-env
# (normal) or %PUBLIC%\zoombie-env (when the user name is not ASCII), so probe
# both before falling back to the repo copy.
$candidates = @()
if ($env:ZOOMBIE_ENV_ROOT) { $candidates += $env:ZOOMBIE_ENV_ROOT }
if ($env:USERPROFILE)      { $candidates += (Join-Path $env:USERPROFILE 'zoombie-env') }
if ($env:PUBLIC)           { $candidates += (Join-Path $env:PUBLIC 'zoombie-env') }
$candidates += $PSScriptRoot

$cli = $null
foreach ($c in $candidates) {
    foreach ($rel in @('bin\zoombie\zoombie.ps1', 'zoombie.ps1')) {
        $p = Join-Path $c $rel
        if (Test-Path -LiteralPath $p) { $cli = $p; break }
    }
    if ($cli) { break }
}
Say "CLI: $cli"
if (-not $cli) { throw "zoombie.ps1 not found. Run scripts/setup.ps1 first." }

# --- 0. Shell sanity -------------------------------------------------------
$psv = $PSVersionTable.PSVersion.ToString()
Say "PowerShell $psv"
if ($PSVersionTable.PSVersion.Major -lt 5) { throw "PowerShell too old: $psv" }

# --- 1. doctor -------------------------------------------------------------
Say 'doctor'
$doctorJson = & $cli doctor | Select-Object -Last 1
$doctor = $doctorJson | ConvertFrom-Json
Info ("ok={0} missing={1}" -f $doctor.ok, ($doctor.data.missing -join ', '))
if (-not $doctor.ok) { throw "doctor reported missing: $($doctor.data.missing -join ', ')" }

# --- 1b. Backend regression guard (configured vs actually initialised) ------
# `doctor` reports two different things: backendConfigured (what env.json
# intends) and backendObserved (what whisper-cli can really initialise). A CUDA
# install missing its cuBLAS runtime used to report `cuda` while every run
# silently fell back to the CPU, so this asserts the two agree instead of
# trusting the manifest. On a CPU/Vulkan machine the check is a no-op.
$backendConfigured = $doctor.data.report.whisper.backendConfigured
$backendObserved   = $doctor.data.report.whisper.backendObserved
Info ("backend: configured={0} observed={1}" -f $backendConfigured, $backendObserved)
foreach ($w in @($doctor.data.warnings) | Where-Object { $_ }) { Info "WARN: $w" }
if ($backendConfigured -eq 'cuda' -and $backendObserved -ne 'cuda') {
    throw ("CUDA backend regression: configured 'cuda' but whisper initialises '{0}' - {1}" -f `
        $backendObserved, $doctor.data.report.whisper.probeReason)
}

# --- 2. scratch folder with a CYRILLIC name (the regression test) ----------
$cyr = -join ([char[]]@(0x442, 0x435, 0x441, 0x442))  # "тест"
$scratch = Join-Path $env:TEMP ("zoombie-selftest-" + $cyr)
if (Test-Path -LiteralPath $scratch) { Remove-Item -Recurse -Force $scratch }
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
Say "scratch (non-ASCII): $scratch"

$done = $false
try {
    # --- 3. SAPI TTS ------------------------------------------------------------------
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $enVoice = $synth.GetInstalledVoices() |
        Where-Object { $_.VoiceInfo.Culture.Name -like 'en-*' } |
        Select-Object -First 1
    if ($enVoice) {
        $synth.SelectVoice($enVoice.VoiceInfo.Name)
        Say "TTS voice: $($enVoice.VoiceInfo.Name)"
    } else {
        Say 'TTS voice: no English voice installed; using default'
    }
    $wav = Join-Path $scratch 'speech.wav'
    $synth.SetOutputToWaveFile($wav)
    $synth.Speak('The quick brown fox jumps over the lazy dog.')
    $synth.Dispose()
    Info "synthesized: $wav"

    # --- 4. extract via the CLI (writes into the Cyrillic dir) ---------------
    Say 'extract'
    $extractOut = Join-Path $scratch 'audio.wav'
    $extractJson = & $cli extract -Source $wav -Output $extractOut | Select-Object -Last 1
    $extract = $extractJson | ConvertFrom-Json
    if (-not $extract.ok) { throw "extract failed: $($extract.error)" }
    Info ("ok={0} output={1}" -f $extract.ok, $extract.data.output)

    # --- 5. transcribe via the CLI (ASCII isolation + copy back) -------------
    Say 'transcribe'
    $base = Join-Path $scratch 'transcript'
    $txJson = & $cli transcribe -Source $extractOut -Output $base -Language en | Select-Object -Last 1
    $tx = $txJson | ConvertFrom-Json
    if (-not $tx.ok) { throw "transcribe failed: $($tx.error)" }
    $txtPath = $tx.data.artifacts.txt.path
    Info "transcript: $txtPath"
    Info ("deviceUsed={0} deviceName={1} realtimeFactor={2} totalMs={3} audioSec={4}" -f `
        $tx.data.deviceUsed, $tx.data.deviceName, $tx.data.realtimeFactor, $tx.data.totalMs, $tx.data.audioDurationSec)

    # Regression guard for the silent CPU fallback: a successful run (exit 0) on
    # the CPU while CUDA is configured is the exact failure this suite exists to
    # catch, so it is a hard failure rather than a note.
    if ($backendConfigured -eq 'cuda' -and $tx.data.deviceUsed -ne 'cuda') {
        throw ("CUDA backend regression: transcription ran on '{0}' while 'cuda' is configured ({1})" -f `
            $tx.data.deviceUsed, $tx.data.fallbackReason)
    }
    if ($tx.data.silentCpuFallback) {
        throw ("Silent CPU fallback detected: {0}" -f $tx.data.fallbackReason)
    }

    # --- 6. verify ------------------------------------------------------------
    $text = (Get-Content -LiteralPath $txtPath -Raw).ToLowerInvariant()
    $keys = @('quick', 'brown', 'fox', 'jumps', 'over', 'lazy', 'dog')
    $found = @($keys | Where-Object { $text -match [regex]::Escape($_) })
    $missing = @($keys | Where-Object { $text -notmatch [regex]::Escape($_) })
    Say "verification: $($found.Count)/$($keys.Count) key words found"
    Info "text: $($text.Trim())"
    if ($missing.Count -gt 0) {
        throw "transcript missing expected words: $($missing -join ', ')"
    }

    # --- 7. readpdf (PDF -> Markdown), optional ------------------------------
    # Skipped entirely when the PDF toolchain has not been installed, so this
    # remains a pure regression test for the transcription pipeline. When it IS
    # present, the PDF is generated with PyMuPDF and converted into the same
    # Cyrillic destination, exercising the ASCII-isolation copy-back.
    $pyForPdf = $null
    $manifestPath = Join-Path (Split-Path -Parent (Split-Path -Parent $cli)) 'env.json'
    if (Test-Path -LiteralPath $manifestPath) {
        $pyForPdf = (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).python.path
    }
    if ($pyForPdf -and (Test-Path -LiteralPath $pyForPdf)) {
        Say 'readpdf'
        $pdfPath = Join-Path $scratch 'doc.pdf'
        $gen = @'
import sys
import pymupdf
doc = pymupdf.open()
page = doc.new_page()
page.insert_text((72, 72), "The quick brown fox jumps over the lazy dog.")
doc.save(sys.argv[1])
doc.close()
'@
        $genFile = Join-Path $scratch 'mkpdf.py'
        [System.IO.File]::WriteAllText($genFile, $gen, (New-Object System.Text.UTF8Encoding($false)))
        $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & $pyForPdf $genFile $pdfPath 2>$null } finally { $ErrorActionPreference = $prevEap }

        $mdBase = Join-Path $scratch 'doc-md'
        $pdfJson = & $cli readpdf -Source $pdfPath -Output $mdBase | Select-Object -Last 1
        $pdf = $pdfJson | ConvertFrom-Json
        if (-not $pdf.ok) { throw "readpdf failed: $($pdf.error)" }
        $mdPath = $pdf.data.output
        if (-not (Test-Path -LiteralPath $mdPath)) { throw "readpdf produced no Markdown: $mdPath" }
        $mdText = (Get-Content -LiteralPath $mdPath -Raw).ToLowerInvariant()
        if ($mdText -notmatch 'quick' -or $mdText -notmatch 'fox') {
            throw "Markdown missing expected text: $mdPath"
        }
        Info "markdown: $mdPath (pages=$($pdf.data.pages) ocr=$($pdf.data.ocrUsed))"
        Say 'PASS: PDF -> Markdown worked end to end'
    } else {
        Say 'readpdf: skipped (PDF toolchain not installed)'
    }

    Say 'PASS: Cyrillic destination path worked end to end'
    $done = $true
}
finally {
    if (-not $KeepArtifacts -and -not $NoCleanup) {
        Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue
        Say 'scratch removed'
    } else {
        Say "scratch kept: $scratch"
    }
}

if ($done) { exit 0 } else { exit 1 }

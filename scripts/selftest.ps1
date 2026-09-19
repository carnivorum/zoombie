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

$root = if ($env:ZOOMBIE_ENV_ROOT) { $env:ZOOMBIE_ENV_ROOT } else { Join-Path $env:USERPROFILE 'zoombie-env' }
$cli  = Join-Path $root 'bin\zoombie\zoombie.ps1'
if (-not (Test-Path -LiteralPath $cli)) {
    $cli = Join-Path $PSScriptRoot 'zoombie.ps1'
}
Say "CLI: $cli"
if (-not (Test-Path -LiteralPath $cli)) { throw "zoombie.ps1 not found. Run scripts/setup.ps1 first." }

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

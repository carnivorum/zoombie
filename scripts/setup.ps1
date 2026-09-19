#Requires -Version 5.1
<#
.SYNOPSIS
    Thin entry point: always fetch the latest setup worker and run it.

.DESCRIPTION
    This script is deliberately minimal. It downloads the CURRENT
    scripts/setup-worker.ps1 from GitHub and runs it, so any start of setup —
    a fresh machine or an already-configured one — means "install or update to
    the latest". There is no state to go stale and no gate that can skip the
    update: the worker is always re-fetched and invoked with -Refresh, which
    makes it re-pull the repo files (module, CLI, self-test, skills) too.

    The heavy lifting (hardware probe, component installs, CLI + skill
    deployment, env.json) lives in setup-worker.ps1, not here.

    Local development is a different path on purpose: run
    scripts/setup-worker.ps1 directly. That installs your working tree as-is and
    does NOT touch the network, so uncommitted edits are what gets installed.

    Every exception is allowed to propagate: if the download or the worker
    fails, the failure is what the caller sees. There is no silent fallback to
    a possibly-stale local copy.

.PARAMETER Check
    Detect only (passed through). Report what is present/missing. Writes nothing.

.PARAMETER DryRun
    Print the actions that would be taken (passed through). Writes nothing.

.PARAMETER Model
    Whisper model name to ensure (passed through).

.PARAMETER Root
    Override the toolchain root (passed through).

.PARAMETER Force
    Re-download even if a component looks present (passed through).

.EXAMPLE
    pwsh -File scripts/setup.ps1 -Check
.EXAMPLE
    pwsh -File scripts/setup.ps1
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$DryRun,
    [string]$Model,
    [string]$Root,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Where to fetch the worker from. Honors the same overrides as the bootstrap so
# a fork or branch can be used without editing the script.
$repoSlug = if ($env:ZOOMBIE_REPO_SLUG) { $env:ZOOMBIE_REPO_SLUG } else { 'carnivorum/zoombie' }
$repoRef  = if ($env:ZOOMBIE_REPO_REF)  { $env:ZOOMBIE_REPO_REF }  else { 'main' }
$rawBase  = "https://raw.githubusercontent.com/$repoSlug/$repoRef"
$workerUrl = "$rawBase/scripts/setup-worker.ps1"

# Stage the freshest worker in an ASCII temp dir (never the repo/install root,
# so a local working copy is never silently used instead of the network copy).
# It is single-use and removed again on every exit path below, so repeated
# bootstrap runs do not accumulate %TEMP%\zoombie-setup-<guid> folders.
$stamp      = [guid]::NewGuid().ToString('N')
$workerPath = Join-Path $env:TEMP "zoombie-setup-$stamp\setup-worker.ps1"
$workerDir  = Split-Path -Parent $workerPath
New-Item -ItemType Directory -Force -Path $workerDir | Out-Null

[Console]::Error.WriteLine("==> zoombie bootstrap: fetching the latest setup worker")
[Console]::Error.WriteLine("    $workerUrl")

$prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
try {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & curl.exe -L --fail --retry 3 --silent --show-error -o $workerPath $workerUrl 2>$null
        $code = $LASTEXITCODE
    } else {
        Invoke-WebRequest -Uri $workerUrl -OutFile $workerPath -UseBasicParsing
        $code = 0
    }
}
catch {
    $code = 1
}
finally {
    $ErrorActionPreference = $prevEap
}
if ($code -ne 0 -or -not (Test-Path -LiteralPath $workerPath)) {
    Remove-Item -Recurse -Force $workerDir -ErrorAction SilentlyContinue
    throw "Could not download the setup worker from $workerUrl (curl exit $code). Check the network, or set ZOOMBIE_REPO_SLUG / ZOOMBIE_REPO_REF."
}

# Run the freshly fetched worker. -Refresh makes it pull the repo files again,
# so every bootstrap run updates rather than re-installs what is already there.
$workerArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $workerPath, '-Refresh')
if ($Check)  { $workerArgs += '-Check' }
if ($DryRun) { $workerArgs += '-DryRun' }
if ($Model)  { $workerArgs += @('-Model', $Model) }
if ($Root)   { $workerArgs += @('-Root', $Root) }
if ($Force)  { $workerArgs += '-Force' }

[Console]::Error.WriteLine("==> running $workerPath")

$prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
try {
    $output = & powershell @workerArgs
    $exit   = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $prevEap
    # Remove the staged worker and its folder on both success and failure, so the
    # bootstrap leaves no scratch behind (it only ever needed the one script).
    Remove-Item -Recurse -Force $workerDir -ErrorAction SilentlyContinue
}

# Reproduce the worker's single JSON result line on our stdout so callers (and
# the setup.md flow) parse setup.ps1 exactly as they parsed setup.ps1 before.
$resultLine = @($output) | Where-Object { "$_" -match '^\s*\{' } | Select-Object -Last 1
if ($resultLine) { Write-Output $resultLine }

# Derive a clean exit code from the result; fall back to the child's code.
$workerOk = $null
if ($resultLine) {
    try { $workerOk = [bool]($resultLine | ConvertFrom-Json).ok } catch { $workerOk = $null }
}
if ($null -eq $workerOk) {
    if ($exit -ne 0) { exit $exit } else { exit 0 }
}
if ($workerOk) { exit 0 } else { exit 1 }

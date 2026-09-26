<#
.SYNOPSIS
    zoombie installer core: ensure Python, fetch the repo, run the Python installer.

.DESCRIPTION
    This is the evolving half of the install. It is ALWAYS fetched fresh by the
    frozen getter (scripts\zoombie-install.cmd), so it can gain steps without any
    distribution change.

    Steps:
      1. locate a usable Python (reject the Microsoft Store alias stub);
      2. if none exists, elevate ONCE and run `winget install Python.Python.3.12`;
      3. fetch the current repo archive into an ASCII staging dir;
      4. extract it and locate the single top-level folder;
      5. run `python -m zoombie.install` from that checkout, forwarding the flags.

    Non-ASCII safety: the staging base and the child's TEMP/TMP are forced to an
    ASCII path, because a Cyrillic user profile makes %TEMP% non-ASCII and neither
    curl/Expand-Archive nor the Chinese-whisper path rules tolerate it. The launch
    directory is irrelevant, so running from anywhere is safe.

    Nothing global is mutated: the only elevation is the one-shot winget call, and
    no execution policy, PATH or profile is changed.

.PARAMETER Check
    Detect only; write nothing.
.PARAMETER DryRun
    Show the plan; write nothing.
.PARAMETER Model
    Force a whisper model (e.g. small, large-v3-turbo).
.PARAMETER Root
    Use a different (still ASCII) toolchain root.
.PARAMETER Force
    Re-download even when a component is present.
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$DryRun,
    [string]$Model,
    [string]$Root,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$defaultSlug = 'carnivorum/zoombie'
$defaultRef = 'main'
$slug = if ($env:ZOOMBIE_REPO_SLUG) { $env:ZOOMBIE_REPO_SLUG } else { $defaultSlug }
$ref = if ($env:ZOOMBIE_REPO_REF) { $env:ZOOMBIE_REPO_REF } else { $defaultRef }
$archive = "https://codeload.github.com/$slug/zip/refs/heads/$ref"

function Test-AsciiText {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return $false }
    return $Text -notmatch '[^\x00-\x7F]'
}

function Resolve-Python {
    foreach ($name in @('python.exe', 'py.exe')) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        $candidate = $found.Source
        # The Microsoft Store alias stub is not a real interpreter: running it
        # opens the Store instead of executing anything.
        if ($candidate -match 'WindowsApps') { continue }
        & $candidate -c "import sys" *> $null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

Write-Host "==> zoombie install (core)"
Write-Host "    repo: $slug @ $ref"

# --- 1/2. Python ------------------------------------------------------------
$python = Resolve-Python
if (-not $python) {
    Write-Host "==> no usable Python found; installing Python 3.12 (elevating once)"
    try {
        Start-Process -FilePath 'winget' -Verb RunAs -Wait -ArgumentList @(
            'install', '--id', 'Python.Python.3.12', '--scope', 'user',
            '--accept-package-agreements', '--accept-source-agreements', '--silent'
        )
    } catch {
        Write-Host "    winget elevation failed: $($_.Exception.Message)"
    }
    $python = Resolve-Python
}
if (-not $python) {
    throw ("Python 3.10+ is required but could not be found or installed. Run:`r`n" +
           "    winget install Python.Python.3.12`r`nand re-run zoombie-install.")
}
Write-Host "    python: $python"

# --- ASCII staging base -----------------------------------------------------
$base = @($env:TEMP, $env:PUBLIC, ($env:SystemDrive + '\')) |
    Where-Object { Test-AsciiText $_ } | Select-Object -First 1
if (-not $base) { $base = Join-Path $env:PUBLIC 'zoombie-install' }
$stage = Join-Path $base ('zoombie-install-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$zip = Join-Path $stage 'repo.zip'

$tempWas = @{ TEMP = $env:TEMP; TMP = $env:TMP }
$code = 1
try {
    # --- 3. fetch the repo archive ------------------------------------------
    Write-Host "==> downloading $archive"
    $ProgressPreference = 'SilentlyContinue'
    $downloaded = $false
    try {
        Invoke-WebRequest -Uri $archive -OutFile $zip -UseBasicParsing -ErrorAction Stop
        $downloaded = $true
    } catch {
        try {
            (New-Object Net.WebClient).DownloadFile($archive, $zip)
            $downloaded = $true
        } catch {
            throw "could not download the repository archive: $($_.Exception.Message)"
        }
    }

    # --- 4. extract ---------------------------------------------------------
    # tar.exe ships with Windows 10 1803+ and unpacks a zip, so no PowerShell is
    # needed for extraction on a modern machine; Expand-Archive (which throws on
    # failure under ErrorActionPreference=Stop) is the fallback.
    if (Get-Command tar.exe -ErrorAction SilentlyContinue) {
        & tar.exe -xf $zip -C $stage
        if ($LASTEXITCODE -ne 0) { throw "could not extract the repository archive" }
    } else {
        Expand-Archive -LiteralPath $zip -DestinationPath $stage -Force
    }

    $repo = Get-ChildItem -LiteralPath $stage -Directory | Select-Object -First 1
    if (-not $repo) { throw "the archive contained no folder" }

    # --- 5. run the Python installer ----------------------------------------
    # Force the child's TEMP/TMP to an ASCII dir: %TEMP% inherits a Cyrillic
    # profile, and the installer creates scratch dirs beneath it.
    $asciiTmp = Join-Path $stage 'tmp'
    New-Item -ItemType Directory -Force -Path $asciiTmp | Out-Null
    $env:TEMP = $asciiTmp
    $env:TMP = $asciiTmp
    $env:PYTHONPATH = Join-Path $repo.FullName 'scripts'
    $env:PYTHONUTF8 = '1'

    $argv = @()
    if ($Check) { $argv += '-Check' }
    if ($DryRun) { $argv += '-DryRun' }
    if ($Model) { $argv += @('-Model', $Model) }
    if ($Root) { $argv += @('-Root', $Root) }
    if ($Force) { $argv += '-Force' }

    Write-Host "==> running the installer"
    & $python -m zoombie.install @argv
    $code = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 1 }
} finally {
    $env:TEMP = $tempWas.TEMP
    $env:TMP = $tempWas.TMP
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}

exit $code

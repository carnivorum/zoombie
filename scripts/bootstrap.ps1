<#
.SYNOPSIS
    One-line PowerShell entry point for the zoombie setup.

.DESCRIPTION
    A thin shim, NOT a second installer. It downloads scripts/bootstrap.cmd from
    the repo and runs it; bootstrap.cmd finds (or installs) Python, fetches the
    repo archive, and hands off to the Python installer. Because ALL install
    logic stays in bootstrap.cmd, this file cannot drift from it.

    It runs unattended: it never prompts. The setup prompt (setup.md) is what
    tells the agent to describe the download and ask the user before applying.

    Nothing global is mutated. No execution-policy change, no setx, no profile
    edit, no permanent PATH change. The staged batch file is removed afterwards.

.PARAMETER Check
    Detect only; write nothing. Same as bootstrap.cmd -Check.

.PARAMETER DryRun
    Show the plan; write nothing. Same as bootstrap.cmd -DryRun.

.PARAMETER Model
    Force a whisper model (e.g. small, large-v3-turbo). Same as -Model.

.PARAMETER Root
    Use a different (still ASCII) toolchain root. Same as -Root.

.PARAMETER Force
    Re-download even when a component is present. Same as -Force.

.EXAMPLE
    # the whole distribution, from any shell and any working directory
    irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex

.EXAMPLE
    # no-IE alternative, useful when Invoke-WebRequest cannot parse the response
    iex (New-Object Net.WebClient).DownloadString('https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1')

.EXAMPLE
    # after saving the file (file mode, so parameters bind and the exit code is returned)
    .\bootstrap.ps1 -Check
    .\bootstrap.ps1 -Model large-v3-turbo

.NOTES
    Under `irm ... | iex` there is no way to pass parameters, so every option also
    has an environment-variable form for the unattended one-liner:

        ZOOMBIE_CHECK=1     ZOOMBIE_DRYRUN=1   ZOOMBIE_FORCE=1
        ZOOMBIE_MODEL=<name> ZOOMBIE_ROOT=<path>
        ZOOMBIE_REPO_SLUG=<owner/repo>         (default carnivorum/zoombie)
        ZOOMBIE_REPO_REF=<branch|tag>          (default main)

    An explicit switch wins over its environment variable, which wins over the
    default. Example:

        $env:ZOOMBIE_CHECK = '1'
        irm <url> | iex

    Failure handling depends on how the script is run. As a FILE it exits with the
    installer's exit code. Inline (via iex or a script block) it THROWS instead of
    exiting, because `exit` in the caller's session would close the user's shell.
#>

param(
    [switch]$Check,
    [switch]$DryRun,
    [string]$Model,
    [string]$Root,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# File mode vs inline mode. $PSScriptRoot is set only when this runs as a file.
# It is the single switch that decides whether a failure exits or throws, and
# whether a successful run returns an exit code.
$runAsFile = -not [string]::IsNullOrEmpty($PSScriptRoot)

$defaultSlug = 'carnivorum/zoombie'
$defaultRef = 'main'

function Test-AsciiText {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return $true }
    return $Text -notmatch '[^\x00-\x7F]'
}

function Get-EnvValue {
    param([string]$Name)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $null }
    return $value
}

function Get-EnvFlag {
    param([string]$Name)
    $value = Get-EnvValue $Name
    if (-not $value) { return $false }
    return ($value -eq '1' -or $value -ieq 'true' -or $value -ieq 'yes' -or $value -ieq 'on')
}

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Stop-Setup {
    param([string]$Message, [int]$Code = 1)
    [Console]::Error.WriteLine($Message)
    if ($runAsFile) { exit $Code }
    throw $Message
}

# --- 1. resolve the options (switch, then environment, then default) --------
$useCheck = [bool]$Check -or (Get-EnvFlag 'ZOOMBIE_CHECK')
$useDryRun = [bool]$DryRun -or (Get-EnvFlag 'ZOOMBIE_DRYRUN')
$useForce = [bool]$Force -or (Get-EnvFlag 'ZOOMBIE_FORCE')

$modelValue = $Model
if (-not $modelValue) { $modelValue = Get-EnvValue 'ZOOMBIE_MODEL' }

$rootValue = $Root
if (-not $rootValue) { $rootValue = Get-EnvValue 'ZOOMBIE_ROOT' }

$repoSlug = Get-EnvValue 'ZOOMBIE_REPO_SLUG'
if (-not $repoSlug) { $repoSlug = $defaultSlug }

$repoRef = Get-EnvValue 'ZOOMBIE_REPO_REF'
if (-not $repoRef) { $repoRef = $defaultRef }

$scriptUrl = "https://raw.githubusercontent.com/$repoSlug/$repoRef/scripts/bootstrap.cmd"

# --- 2. stage the batch entry point on an ASCII path ------------------------
# %TEMP% is not ASCII when the user profile is not, and cmd.exe processes a
# non-ASCII path through the console code page. %PUBLIC% is the machine-level
# fallback the toolchain already uses for its root, so reuse the same rule.
$stagingBase = $env:TEMP
if (-not (Test-AsciiText $stagingBase)) { $stagingBase = $env:PUBLIC }
if (-not (Test-AsciiText $stagingBase)) { $stagingBase = $env:SystemDrive + '\' }
if ([string]::IsNullOrEmpty($stagingBase)) { $stagingBase = '.' }

$staging = Join-Path $stagingBase ('zoombie-bootstrap-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $staging | Out-Null

# A separate ASCII dir feeding the child's TEMP/TMP, so bootstrap.cmd's own
# staging (repo download + extraction) is not done through a code-paged path.
$asciiTemp = Join-Path $staging 'tmp'
New-Item -ItemType Directory -Force -Path $asciiTemp | Out-Null

$bootstrapScript = Join-Path $staging 'bootstrap.cmd'

$tempWas = @{ TEMP = $env:TEMP; TMP = $env:TMP }
$overrodeTemp = $false
if (-not (Test-AsciiText $env:TEMP) -or -not (Test-AsciiText $env:TMP)) {
    $overrodeTemp = $true
    $env:TEMP = $asciiTemp
    $env:TMP = $asciiTemp
}

$childArgs = @()
if ($useCheck) { $childArgs += '-Check' }
if ($useDryRun) { $childArgs += '-DryRun' }
if ($modelValue) { $childArgs += @('-Model', $modelValue) }
if ($rootValue) { $childArgs += @('-Root', $rootValue) }
if ($useForce) { $childArgs += '-Force' }

$code = 1
try {
    Write-Step "zoombie bootstrap"
    Write-Step "repo: $repoSlug @ $repoRef"

    # --- 3. fetch the batch entry point ------------------------------------
    $downloaded = $false
    $fetchError = $null

    # The progress bar is a large, measurable slowdown on Windows PowerShell 5.1,
    # and -UseBasicParsing avoids the IE parsing engine, which fails on a machine
    # where IE has never been run. Restore the preference rather than leave it
    # changed for the caller.
    $progressWas = $ProgressPreference
    try {
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $scriptUrl -OutFile $bootstrapScript -UseBasicParsing -ErrorAction Stop
        $downloaded = $true
    } catch {
        $fetchError = $_.Exception.Message
    } finally {
        $ProgressPreference = $progressWas
    }

    if (-not $downloaded) {
        # No IE dependency: this works wherever .NET can reach the URL.
        try {
            $web = New-Object System.Net.WebClient
            $web.DownloadFile($scriptUrl, $bootstrapScript)
            $downloaded = $true
        } catch {
            $fetchError = "$fetchError / $($_.Exception.Message)"
        }
    }

    if (-not $downloaded -or -not (Test-Path -LiteralPath $bootstrapScript)) {
        Stop-Setup (
            "Could not download the zoombie bootstrap from:`r`n  $scriptUrl`r`n" +
            "Reason: $fetchError`r`n" +
            "Check the network, or set ZOOMBIE_REPO_SLUG / ZOOMBIE_REPO_REF to a fork " +
            "or branch, then re-run. If Invoke-WebRequest cannot parse the response, " +
            "use: iex (New-Object Net.WebClient).DownloadString('<url>')"
        ) 1
    }

    if ((Get-Item -LiteralPath $bootstrapScript).Length -le 0) {
        Stop-Setup "The downloaded bootstrap.cmd is empty: $scriptUrl" 1
    }

    # --- 4. run it ---------------------------------------------------------
    # Invoked directly rather than through `cmd /c "<path>"`: PowerShell routes a
    # .cmd through cmd.exe and quotes the argv correctly, which sidesteps cmd's
    # quote-stripping rules when the staging path contains a space.
    Write-Step "running $bootstrapScript $($childArgs -join ' ')"
    # Clear any inherited exit code first: a stale 0 from an earlier command in
    # the caller's session must never be mistaken for this run's success.
    $global:LASTEXITCODE = $null
    & $bootstrapScript @childArgs
    $code = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 1 }
} finally {
    if ($overrodeTemp) {
        $env:TEMP = $tempWas.TEMP
        $env:TMP = $tempWas.TMP
    }
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
}

if ($code -ne 0) {
    Stop-Setup "zoombie setup failed (exit code $code)." $code
}

if ($runAsFile) { exit 0 }

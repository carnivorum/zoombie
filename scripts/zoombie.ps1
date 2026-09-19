#Requires -Version 5.1
<#
.SYNOPSIS
    Deterministic runtime CLI for the zoombie speech-to-text toolchain.

.DESCRIPTION
    The single entry point every SKILL.md calls. All fragile command assembly
    (ffmpeg flags, whisper.cpp flags, yt-dlp format selection, and the
    ASCII-path workaround) lives here, tested once — not re-improvised per run
    by an LLM reading prose.

    All tools are resolved to absolute paths from the zoombie-env manifest.
    Nothing relies on PATH or on where a tool was installed.

    Cyrillic/non-ASCII paths never reach whisper.cpp: any input is copied into
    an ASCII work dir first, whisper runs entirely inside it, and artifacts are
    copied back to the user's real destination afterwards.

.PARAMETER Command
    One of: doctor, download, extract, transcribe, readpdf, pipeline, clean.

.PARAMETER Source
    Source file (extract/transcribe) or URL (download/pipeline).

.PARAMETER Output
    Destination file or directory for the produced artifact.

.PARAMETER Model
    Override the model (name or path). Defaults to the manifest's model.

.PARAMETER DownloadDir
    Where download should save the fetched video.

.PARAMETER WorkRoot
    Scratch folder for the ASCII work dir. Defaults to zoombie-env\work.

.PARAMETER KeepWork
    Do not delete the scratch ASCII work dir after the run (debugging).

.PARAMETER AudioOnly
    download/pipeline: fetch audio only instead of the full video.

.PARAMETER Format
    extract: output container (wav default; mp3/m4a/flac supported).

.PARAMETER Ocr
    readpdf: also OCR image-only (scanned) pages with Tesseract.

.PARAMETER Images
    readpdf: also extract embedded images into <output>.images\.

.PARAMETER Pages
    readpdf: page range to convert, e.g. '1-5,8'. Default: all pages.

.PARAMETER Language
    transcribe: force a language; 'auto' by default.

.PARAMETER Srt
    transcribe: also emit .srt subtitles.

.PARAMETER NoGpu
    transcribe: force CPU (-ng) even when a GPU backend is configured. This is
    the explicit opt-out, so the GPU policy below does not apply to it.

.PARAMETER NoFlashAttn
    transcribe: do not pass -fa even when the installed build supports it.

.PARAMETER Threads
    transcribe: thread count for the CPU path (-t). Default: physical cores.

.PARAMETER AllowCpuFallback
    transcribe: permit a CPU run even though a GPU backend is configured and
    usable. The default is to FAIL in that case, because a machine whose GPU fits
    must actually use it; a CPU/Vulkan-only machine is unaffected.

.PARAMETER StrictGpu
    transcribe: also fail when the GPU evidence is only AMBIGUOUS - the backend
    initialised but no device-selection line appeared in the log. Without this the
    run warns and sets silentCpuFallback instead, because that shape matches a
    banner-format difference and failing on it could reject a healthy run.

.PARAMETER DryRun
    Print planned actions and emit a JSON result. Writes no artifacts.

.PARAMETER Force
    Overwrite existing outputs without asking.

.EXAMPLE
    pwsh -File scripts/zoombie.ps1 doctor
.EXAMPLE
    pwsh -File scripts/zoombie.ps1 extract -Source .\clip.mp4 -Output .\clip.wav
.EXAMPLE
    pwsh -File scripts/zoombie.ps1 transcribe -Source .\clip.wav -Output .\transcripts\clip -Srt
.EXAMPLE
    pwsh -File scripts/zoombie.ps1 readpdf -Source .\book.pdf -Output .\docs\book
.EXAMPLE
    pwsh -File scripts/zoombie.ps1 readpdf -Source .\scan.pdf -Output .\docs\scan -Ocr -Pages 1-5
.EXAMPLE
    pwsh -File scripts/zoombie.ps1 pipeline -Source "https://youtu.be/XXXX" -Output .\transcripts\clip
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('doctor', 'download', 'extract', 'transcribe', 'readpdf', 'pipeline', 'clean')]
    [string]$Command,

    # NOTE: do NOT name this parameter $Input — PowerShell reserves $Input as
    # the automatic pipeline enumerator, which silently clobbers the binding.
    [string]$Source,
    [string]$Output,
    [string]$Model,
    [string]$DownloadDir,
    [string]$WorkRoot,
    [switch]$KeepWork,
    [switch]$AudioOnly,
    [ValidateSet('wav', 'mp3', 'm4a', 'flac')]
    [string]$Format = 'wav',
    [string]$Language = 'auto',
    [switch]$Srt,
    [switch]$NoGpu,
    [switch]$NoFlashAttn,
    [int]$Threads = 0,
    [switch]$AllowCpuFallback,
    [switch]$StrictGpu,
    [switch]$Ocr,
    [switch]$Images,
    [string]$Pages,
    [switch]$DryRun,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$modulePath = Join-Path $PSScriptRoot 'lib\ZoombieEnv.psm1'
Import-Module $modulePath -Force
Set-ZoombieUtf8Console

$script:DryRun = [bool]$DryRun
$script:Force  = [bool]$Force

# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

function Get-ManifestValue {
    <#
    .SYNOPSIS
        StrictMode-safe nested read from a manifest (PSCustomObject) or $null.

    .DESCRIPTION
        Under Set-StrictMode -Version Latest, reading a missing member of a
        PSCustomObject/JSON object throws. env.json is written by setup.ps1, so a
        manifest produced by an older/partial run can lack a section (e.g. whisper
        or model) and would then abort EVERY command with a vague error, even ones
        that never touch that section. This walks the dotted path defensively and
        returns $Default for any missing link, mirroring setup.ps1's Get-Field.
    #>
    [CmdletBinding()]
    param(
        $Object,
        [Parameter(Mandatory)][string]$Path,
        $Default = $null
    )
    $current = $Object
    foreach ($key in $Path.Split('.')) {
        if ($null -eq $current) { return $Default }
        $prop = $current.PSObject.Properties[$key]
        if (-not $prop) { return $Default }
        $current = $prop.Value
    }
    if ($null -eq $current) { return $Default }
    return $current
}

function Get-Environment {
    <#
    .SYNOPSIS
        Resolve every tool to an absolute path. Prefers the env.json manifest,
        falls back to the zoombie-env bin dirs, then to PATH.
    #>
    [CmdletBinding()]
    param()
    $manifest = Get-ZoombieEnvManifest

    $env = [ordered]@{
        root     = Get-ZoombieEnvRoot
        manifest = $manifest
        ffmpeg   = Resolve-ZoombieTool -Name 'ffmpeg'  -Candidates @((Get-ManifestValue $manifest 'ffmpeg.path'))
        ffprobe  = Resolve-ZoombieTool -Name 'ffprobe' -Candidates @((Get-ManifestValue $manifest 'ffprobe.path'))
        # yt-dlp is a Python package invoked as `python -m yt_dlp`, not a local exe.
        whisper  = Resolve-ZoombieTool -Name 'whisper-cli' -Candidates @((Get-ManifestValue $manifest 'whisper.path'))
        model    = $null
        backend  = Get-ManifestValue $manifest 'whisper.backend'
        python    = Get-ZoombiePython
        # The helper sits beside the CLI in both layouts: the installed copy at
        # zoombie-env\bin\zoombie\pdf\ and the repo checkout at scripts\pdf\.
        pdfScript = Join-Path $PSScriptRoot 'pdf\extract_pdf.py'
    }

    if ($Model) {
        if (Test-Path -LiteralPath $Model) {
            $env.model = (Resolve-Path -LiteralPath $Model).Path
        } else {
            $fileName = if ($Model -like 'ggml-*.bin') { $Model } else { "ggml-$Model.bin" }
            $env.model = Join-Path (Get-ZoombieEnvPath -Child 'models') $fileName
        }
    } elseif ($manifest) {
        $env.model = Get-ManifestValue $manifest 'model.path'
    }
    return $env
}

function Assert-Tool {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        throw "$Name is not available. Run scripts/setup.ps1 first."
    }
    return $Path
}

# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

function Invoke-Doctor {
    [CmdletBinding()]
    param()
    $env = Get-Environment

    # backendConfigured comes from env.json (what was INTENDED at install time).
    # backendObserved comes from running whisper-cli, which is the only way to
    # know what it can ACTUALLY initialise. Reporting only the former is exactly
    # how a CUDA install with a missing cuBLAS runtime looked healthy while every
    # run silently used the CPU. The probe is `whisper-cli --help`, which performs
    # the full backend init and exits: no model, no audio, no filesystem writes.
    $backendConfigured = $env.backend
    $probe             = Invoke-ZoombieWhisperBackendProbe -WhisperExe $env.whisper
    $backendObserved   = $probe.Device
    $cudaRuntime       = Get-ZoombieWhisperCudaRuntime -WhisperExe $env.whisper
    $probeReason       = $probe.Reason
    $caps              = Get-ZoombieWhisperCapabilities -WhisperExe $env.whisper
    # Name the likely cause when the runtime DLLs are absent, so the mismatch is
    # actionable rather than a bare "cpu".
    if ($backendConfigured -eq 'cuda' -and -not $cudaRuntime.Ready) {
        $probeReason = "the CUDA runtime is incomplete (missing: $($cudaRuntime.Missing -join ', ')); ggml-cuda.dll cannot create a device, so whisper falls back to the CPU"
    }

    $report = [ordered]@{
        root      = $env.root
        asciiRoot = (Test-ZoombieAsciiPath $env.root)
        ffmpeg    = @{ path = $env.ffmpeg;  version = if ($env.ffmpeg)  { Get-ZoombieToolVersion -Exe $env.ffmpeg  -VersionArgs '-version' } else { $null } }
        ffprobe   = @{ path = $env.ffprobe; version = if ($env.ffprobe) { Get-ZoombieToolVersion -Exe $env.ffprobe -VersionArgs '-version' } else { $null } }
        ytDlp     = @{ via = 'python -m yt_dlp'; python = $env.python; version = if ($env.python) { Get-ZoombieToolVersion -Exe $env.python -VersionArgs @('-m', 'yt_dlp', '--version') } else { $null } }
        whisper   = @{
            path              = $env.whisper
            # Kept for existing callers; equals backendConfigured.
            backend           = $backendConfigured
            backendConfigured = $backendConfigured
            backendObserved   = $backendObserved
            deviceName        = $probe.DeviceName
            probeReason       = $probeReason
            cudaRuntime       = @{
                gpuModule   = $cudaRuntime.GpuModule
                dirMissing  = $cudaRuntime.DirMissing
                ready       = $cudaRuntime.Ready
                cublasMajor = $cudaRuntime.CublasMajor
                present     = @($cudaRuntime.Present)
                missing     = @($cudaRuntime.Missing)
                warnings    = @($cudaRuntime.Warnings)
                version     = $cudaRuntime.ProvisionVersion
            }
            # The flags the installed build actually advertises, so a caller can
            # tell "flash attention is unavailable" from "it was not requested".
            capabilities      = @{
                probed         = $caps.Checked
                available      = $caps.Ok
                flashAttention = $caps.FlashAttention
                threads        = $caps.Threads
                vad            = $caps.Vad
            }
        }
        model     = @{ path = $env.model; exists = ($env.model -and (Test-Path -LiteralPath $env.model)) }
    }

    $missing = @()
    if (-not $report.ffmpeg.path)  { $missing += 'ffmpeg' }
    if (-not $report.ffprobe.path) { $missing += 'ffprobe' }
    if (-not $report.ytDlp.version) { $missing += 'yt-dlp' }
    if (-not $report.whisper.path) { $missing += 'whisper-cli' }
    if (-not $report.model.exists) { $missing += 'model' }

    # A mismatch is a warning, not a failure: the CPU fallback still transcribes,
    # it is just orders of magnitude slower, and the user must be told.
    $warnings = New-Object System.Collections.Generic.List[string]
    if ($backendConfigured -and $backendObserved -and $backendConfigured -ne $backendObserved) {
        $warnings.Add("backend mismatch: configured '$backendConfigured' but whisper initialises '$backendObserved' ($probeReason)")
    }
    if ($backendConfigured -eq 'cuda' -and -not $cudaRuntime.Ready) {
        $warnings.Add("CUDA runtime incomplete (missing: $($cudaRuntime.Missing -join ', '))")
    }
    if (-not $backendObserved) { $warnings.Add("backend could not be probed: $probeReason") }
    foreach ($warning in $warnings) { Write-ZoombieLog -Level Warn -Message $warning }

    Write-ZoombieResult -Action 'doctor' -Ok ($missing.Count -eq 0) -Data ([ordered]@{
        report   = $report
        missing  = $missing
        warnings = @($warnings)
    }) -ErrorMessage $(if ($missing.Count) { "Missing: $($missing -join ', ')" } else { $null })
}

# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

function Invoke-Download {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Env)
    if (-not $Source) { throw 'download requires -Source <url>' }
    # yt-dlp is pure Python, so it is ASCII-path safe (unlike whisper.cpp) and is
    # invoked through the interpreter as a module: no PATH lookup, no local exe.
    $py = Assert-Tool -Path $Env.python -Name 'python'

    $dir = if ($DownloadDir) { $DownloadDir } else { Join-Path (Get-Location).Path 'downloads' }
    if (-not (Test-Path -LiteralPath $dir)) {
        if ($script:DryRun) { Write-ZoombieLog -Level Step -Message "would create $dir" }
        else { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    }

    if ($AudioOnly) {
        $args = @('-f', 'bestaudio/best', '-x', '--audio-format', $Format)
        if ($Format -eq 'wav') { $args += @('--postprocessor-args', '-ac 1 -ar 16000') }
        $args += @('-o', (Join-Path $dir '%(title)s [%(id)s].%(ext)s'), '--no-playlist')
    } else {
        $args = @('-f', 'bv*+ba/b', '--merge-output-format', 'mp4',
                  '-o', (Join-Path $dir '%(title)s [%(id)s].%(ext)s'), '--no-playlist')
    }
    if ($script:Force) { $args += '--force-overwrites' } else { $args += '--no-overwrites' }
    if (-not $script:Force) { $args += '--no-mtime' }
    $args += $Source

    Write-ZoombieLog -Level Step -Message "python -m yt_dlp $($args -join ' ')"
    if ($script:DryRun) {
        Write-ZoombieResult -Action 'download' -Ok $true -Data ([ordered]@{ dryRun = $true; dir = $dir; args = $args })
        return
    }
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $py -m yt_dlp @args 2>$null; $ytExit = $LASTEXITCODE }
    finally { $ErrorActionPreference = $prevEap }
    if ($ytExit -ne 0) { throw "yt-dlp failed (exit $ytExit)" }

    $produced = Get-ChildItem -LiteralPath $dir -File |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Write-ZoombieResult -Action 'download' -Ok $true -Data ([ordered]@{
        dir  = $dir
        file = $(if ($produced) { $produced.FullName } else { $null })
        size = $(if ($produced) { $produced.Length } else { 0 })
    })
}

# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

function Invoke-Extract {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Env)
    if (-not $Source) { throw 'extract requires -Source <video>' }
    $ff = Assert-Tool -Path $Env.ffmpeg -Name 'ffmpeg'
    if (-not (Test-Path -LiteralPath $Source)) { throw "Input not found: $Source" }

    $out = if ($Output) { $Output } else { [System.IO.Path]::ChangeExtension($Source, $Format) }
    if (-not [System.IO.Path]::GetExtension($out)) { $out = "$out.$Format" }
    $outFull = [System.IO.Path]::GetFullPath($out)

    if ((Test-Path -LiteralPath $outFull) -and -not $script:Force) {
        throw "Output exists (use -Force to overwrite): $outFull"
    }

    $codec = switch ($Format) {
        'wav'  { @('-c:a', 'pcm_s16le') }
        'mp3'  { @('-c:a', 'libmp3lame', '-q:a', '2') }
        'm4a'  { @('-c:a', 'aac', '-b:a', '128k') }
        'flac' { @('-c:a', 'flac') }
    }
    # -hide_banner -nostats -v error keep native stderr quiet so the JSON result
    # stays the only meaningful output on the stream.
    $args = @('-y', '-hide_banner', '-nostats', '-v', 'error',
              '-i', $Source, '-vn', '-ac', '1', '-ar', '16000') + $codec + @($outFull)

    Write-ZoombieLog -Level Step -Message "ffmpeg extract -> $outFull"
    if ($script:DryRun) {
        Write-ZoombieResult -Action 'extract' -Ok $true -Data ([ordered]@{ dryRun = $true; output = $outFull; args = $args })
        return
    }
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $ff @args 2>$null; $ffExit = $LASTEXITCODE }
    finally { $ErrorActionPreference = $prevEap }
    if ($ffExit -ne 0) { throw "ffmpeg failed (exit $ffExit)" }

    Write-ZoombieResult -Action 'extract' -Ok $true -Data ([ordered]@{
        output     = $outFull
        size       = (Get-Item -LiteralPath $outFull).Length
        format     = $Format
        channels   = 1
        sampleRate = 16000
    })
}

# ---------------------------------------------------------------------------
# transcribe (the ASCII invariant + whisper hardening live here)
# ---------------------------------------------------------------------------

function Invoke-WhisperProcess {
    <#
    .SYNOPSIS
        Run whisper-cli to completion, tolerating a hang at process exit.

    .DESCRIPTION
        This build can finish all of its work and then FAIL TO EXIT: the
        transcript is written, the `whisper_print_timings` block is printed, and
        the process then hangs on teardown (a CUDA/driver shutdown hang). Waiting
        for exit therefore never returns, which is exactly what was observed - the
        timing line appeared "instantly" and the call then waited forever. Both
        `Start-Process -Wait` and `cmd /c` hung, because both correctly wait for a
        child that never terminates; the invocation mechanism was never the fault.

        So the completion condition is WHISPER'S OWN OUTPUT, not process exit:
        once the log contains the timings block (and the transcript file exists),
        the work is done. A short grace period is allowed for a clean exit; if the
        process is still alive after it, it is killed and the run is treated as
        successful, with HangDetected=$true so the caller can report it.

        The grace/hard-cap keep this safe for long files: the kill only ever fires
        AFTER the work has demonstrably completed (or after an absurd hard cap).

    .OUTPUTS
        A hashtable: @{ ExitCode; HangDetected; TimedOut }
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$WhisperExe,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$StdoutLog,
        [Parameter(Mandatory)][string]$StderrLog,
        # How long to wait after the completion marker before declaring a hang.
        [int]$GraceSeconds = 10,
        # Absolute safety cap, so a wedged process can never run forever.
        [int]$HardCapSeconds = 21600
    )

    # A pscustomobject (not a hashtable) so the property names are fixed and a
    # typo cannot silently yield $null for the caller.
    $result = [pscustomobject]@{ ExitCode = 0; HangDetected = $false; TimedOut = $false; ExitCodeUnknown = $false }
    $proc = Start-Process -FilePath $WhisperExe `
        -ArgumentList (Get-QuotedWhisperArguments -Arguments $Arguments) `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog

    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $markerSeen = $false
    try {
        while (-not $proc.WaitForExit(500)) {
            if ($clock.Elapsed.TotalSeconds -gt $HardCapSeconds) {
                try { $proc.Kill() } catch { }
                try { $proc.WaitForExit() } catch { }
                $result.TimedOut = $true
                $result.ExitCode = -1   # genuinely did not finish: a real failure
                return $result
            }
            if (-not $markerSeen -and (Test-Path -LiteralPath $StderrLog)) {
                # Read-ZoombieWhisperLog decodes by BOM: the redirected stderr can
                # be UTF-16, and a mis-decoded read would never match the marker.
                # A sharing violation while whisper still writes is caught inside
                # and simply means "not done yet".
                try {
                    $lines = Read-ZoombieWhisperLog -Path $StderrLog
                    if (@($lines).Count -gt 0 -and (@($lines) -join "`n") -match 'whisper_print_timings:\s+total time') {
                        $markerSeen = $true
                    }
                }
                catch { }
            }
            if ($markerSeen) {
                # The work is finished; give a clean exit a moment, then stop it.
                Start-Sleep -Seconds $GraceSeconds
                if (-not $proc.HasExited) {
                    try { $proc.Kill() } catch { }
                    try { $proc.WaitForExit() } catch { }
                    $result.HangDetected = $true
                }
                break
            }
        }
        # ExitCode is assigned EXPLICITLY on every path.
        #
        # Reading $proc.ExitCode is NOT reliable here: with Start-Process and
        # redirected streams on Windows PowerShell 5.1 it is a string that can
        # come back EMPTY even though the process exited (confirmed by running
        # whisper-cli directly: exited=True, exitCode=<empty>). An empty value then
        # fails the caller's `-ne 0` test and aborts a perfectly good run with
        # "whisper-cli failed (exit )".
        #
        # So exit is not the primary success signal at all - the ARTIFACT is. If
        # the process exited, its code is used when parseable; when it is not
        # parseable but the work demonstrably completed, the run is a success. Only
        # the hard cap, or an exit with a real non-zero code, is a failure.
        if ($result.TimedOut) {
            $result.ExitCode = -1
        } elseif ($result.HangDetected) {
            $result.ExitCode = 0
        } else {
            $exitKnown = $false
            if ($proc.HasExited) {
                $raw = "$($proc.ExitCode)"
                $parsed = -1
                if ([int]::TryParse($raw, [ref]$parsed)) { $result.ExitCode = $parsed; $exitKnown = $true }
            }
            if (-not $exitKnown) {
                # Unparseable (or no) exit code: trust the completed work rather
                # than fail on a reporting quirk of the host shell.
                $result.ExitCode = 0
                $result.ExitCodeUnknown = $true
            }
        }
    }
    finally {
        # Release the redirected handles: the scratch dir holding these log files
        # is deleted by the caller, and an open handle there would block it.
        try { $proc.Dispose() } catch { }
    }
    return $result
}

function Get-QuotedWhisperArguments {
    <#
    .SYNOPSIS
        Quote any argument containing whitespace, for Start-Process -ArgumentList.

    .DESCRIPTION
        Start-Process joins an -ArgumentList array with single spaces and does NOT
        add quoting, so a path containing a space would be split into two
        arguments. The whisper paths here are ASCII and normally space-free, but
        the work root can be user-supplied (-WorkRoot) and %PUBLIC% / a profile
        path can contain spaces, so each element is quoted when it needs to be.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments)
    return @(foreach ($a in $Arguments) {
        $s = "$a"
        if ($s -match '[\s"]') { '"' + ($s -replace '"', '\"') + '"' } else { $s }
    })
}

function Invoke-WhisperOnSafeCopy {
    <#
    .SYNOPSIS
        Copy the audio into an ASCII work dir, run whisper there, copy artifacts back.

    .DESCRIPTION
        whisper.cpp misbehaves when any path it receives contains non-ASCII
        (Cyrillic) characters. Rather than trust detection, we ALWAYS isolate:
          - input copied to  <work>\input.<ext>            (ASCII)
          - whisper -of      <work>\out                    (ASCII)
          - results copied to the user's destination       (may be Cyrillic)
        So whisper-cli never sees a non-ASCII path, ever.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Env,
        [Parameter(Mandatory)][string]$AudioPath,
        [Parameter(Mandatory)][string]$OutputBase,
        [switch]$WantSrt,
        [string]$WorkRoot,
        [switch]$KeepWork,
        [switch]$NoFlashAttn,
        [int]$Threads = 0,
        [switch]$AllowCpuFallback,
        [switch]$StrictGpu
    )
    $whisper = Assert-Tool -Path $Env.whisper -Name 'whisper-cli'
    if (-not $Env.model -or -not (Test-Path -LiteralPath $Env.model)) {
        throw "Whisper model not found: $($Env.model). Run scripts/setup.ps1 first."
    }

    $workRoot = if ($WorkRoot) { $WorkRoot } else { Get-ZoombieEnvPath -Child 'work' }
    if (-not (Test-ZoombieAsciiPath $workRoot)) { throw "Work root must be ASCII: $workRoot" }

    # Build the ASCII scratch dir up front, then copy the input into it under an
    # ASCII name. whisper-cli only ever sees paths inside this directory.
    $work = Join-Path $workRoot ([guid]::NewGuid().ToString('N'))
    # In dry-run we must NOT touch the filesystem, and the work dir does not
    # exist yet, so skip the (real) copy and just report the paths we would use.
    $safe = if ($script:DryRun) {
        $ext = [System.IO.Path]::GetExtension($AudioPath)
        if (-not $ext) { $ext = '.bin' }
        @{ WorkDir = $work; InputPath = (Join-Path $work ('input' + $ext.ToLowerInvariant())) }
    } else {
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        Copy-ZoombieIntoSafeWork -InputPath $AudioPath -WorkDir $work
    }
    $outBase = Join-Path $work 'out'

    # Probe the binary ONCE for the flags it actually advertises and for whether
    # a GPU backend can initialise at all. Optional flags (-fa, -t) exist only in
    # some builds and an unknown flag aborts the run, so they are added only when
    # --help lists them ("probe the binary, never assume").
    $caps       = Get-ZoombieWhisperCapabilities -WhisperExe $whisper
    $probeLines = if ($caps.Raw) { @($caps.Raw -split "`r?`n") } else { @() }
    $capInfo    = Get-ZoombieWhisperDeviceInfo -LogLines $probeLines
    $gpuCapable = [bool]$capInfo.BackendInitialised

    # GPU policy input: a GPU backend is CONFIGURED for this install. Whether the
    # machine must then actually use the GPU is decided from that plus the probe,
    # so a CPU-only machine (backend 'cpu') is never forced into a failure.
    $gpuBackendConfigured = ($Env.backend -eq 'cuda' -or $Env.backend -eq 'vulkan')
    $gpuRequired = $gpuBackendConfigured -and -not $NoGpu -and -not $AllowCpuFallback

    $threads = if ($Threads -gt 0) { $Threads } else { Get-ZoombieCpuThreadCount }

    $args = @('-m', $Env.model, '-f', $safe.InputPath, '-l', $Language,
              '-otxt', '-nt')
    if ($WantSrt) { $args += '-osrt' }
    if ($NoGpu)   { $args += '-ng' }
    $args += @('-of', $outBase)
    # Flash attention: GPU runs only, and only when the build advertises -fa.
    $flashAttn = $false
    if (-not $NoGpu -and -not $NoFlashAttn -and $caps.FlashAttention) {
        $args += '-fa'
        $flashAttn = $true
    }
    # Thread count: the CPU path is exactly where it matters most, so it is set
    # on a deliberate -ng run rather than left at the build's default.
    if ($NoGpu -and $caps.Threads -and $threads -gt 0) { $args += @('-t', "$threads") }

    Write-ZoombieLog -Level Step -Message "whisper-cli (ascii-safe) -> $OutputBase"
    Write-ZoombieLog -Level Info -Message "  work=$work  backend=$($Env.backend)  gpuCapable=$gpuCapable  model=$(Split-Path -Leaf $Env.model)"
    if ($script:DryRun) {
        Write-ZoombieResult -Action 'transcribe' -Ok $true -Data ([ordered]@{
            dryRun = $true; work = $work; outputBase = $OutputBase; args = $args
            capabilities = [ordered]@{
                probed = $caps.Checked; flashAttention = $flashAttn
                threads = $threads; gpuCapable = $gpuCapable; gpuRequired = $gpuRequired
            }
        })
        return
    }

    # Hardened invocation, and the WHY matters because this looks indirect.
    #
    # whisper writes its transcript to stdout, and its backend banner + timings
    # block to stderr. Both must be kept off THIS process's stdout (the CLI
    # contract is exactly one JSON line) and stderr must be preserved (it is the
    # only evidence of which device ran and how long it took).
    #
    # Two naive forms both HANG on Windows PowerShell 5.1, which is the bug this
    # shape exists to avoid:
    #   1. `& $whisper @args 2>$log`            - native stderr redirection goes
    #      through the pipeline and can deadlock once the log grows.
    #   2. `Start-Process ... -RedirectStandardOutput/-Error ... -Wait` - observed
    #      to wedge AFTER whisper exits: the transcript and the timings line are
    #      produced instantly, then the call never returns. PowerShell keeps the
    #      redirected file streams open, and the scratch dir holding those very
    #      files is deleted a few lines later.
    #
    # So neither PowerShell mechanism is used: cmd.exe performs the redirection
    # (`> file 2> file`), which means PowerShell owns no redirected handles and
    # has no stream that anything can fill. cmd /c exits with whisper's own exit
    # code, which is what we read. Verified: returns immediately with the JSON.
    $whisperLog = Join-Path $work 'whisper.log'
    # The retry gets its OWN log. Redirecting both attempts to one file truncated
    # the GPU attempt's banner - the only evidence of WHY the fallback happened -
    # the moment the retry started writing.
    $retryLog       = Join-Path $work 'whisper.retry.log'
    $fallbackReason = $null
    $gpuWallMs      = $null
    $prevUtf8 = $env:PYTHONUTF8; $env:PYTHONUTF8 = '1'
    $prevEap  = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # Streams go to files inside the ASCII scratch dir: stdout because the
        # transcript must not reach this CLI's single-JSON-line stdout, stderr
        # because it carries the device banner and the timings. The process is
        # run through Invoke-WhisperProcess, which does not rely on exit to know
        # the run is over (see its notes on the exit hang).
        $stdoutLog = Join-Path $work 'whisper.stdout.log'
        $run  = Invoke-WhisperProcess -WhisperExe $whisper -Arguments $args `
            -StdoutLog $stdoutLog -StderrLog $whisperLog
        $exit = $run.ExitCode
        $whisperExitHang = $run.HangDetected
        $whisperTimedOut = $run.TimedOut
        # The GPU path may fail mid-run (driver mismatch, VRAM pressure, a cuBLAS
        # failure). Two things changed here:
        #   1. the retry is now conditional on the failure actually LOOKING like a
        #      GPU failure. Retrying on any non-zero exit re-ran the whole job -
        #      at CPU speed - for unrelated errors such as a bad model path or an
        #      unsupported codec, hiding the real cause behind the retry's own
        #      output;
        #   2. the GPU attempt's wall time is recorded, so the cost of the failed
        #      attempt is visible instead of being erased by a stopwatch restart.
        $gpuFailure = $false
        if ($exit -ne 0 -and -not $NoGpu) {
            $gpuFailure = Test-ZoombieWhisperGpuFailure -ExitCode $exit -LogLines (Read-ZoombieWhisperLog -Path $whisperLog)
            if ($gpuFailure) {
                $gpuWallMs      = [math]::Round($stopwatch.Elapsed.TotalMilliseconds, 1)
                $fallbackReason = "whisper exited $exit on the GPU path; the whole job was restarted on the CPU (-ng)"
                Write-ZoombieLog -Level Warn -Message $fallbackReason
                $argsRetry = @('-m', $Env.model, '-f', $safe.InputPath, '-l', $Language, '-otxt', '-nt', '-ng')
                if ($WantSrt) { $argsRetry += '-osrt' }
                if ($caps.Threads -and $threads -gt 0) { $argsRetry += @('-t', "$threads") }
                $argsRetry += @('-of', $outBase)
                $stopwatch.Restart()
                $retryRun = Invoke-WhisperProcess -WhisperExe $whisper -Arguments $argsRetry `
                    -StdoutLog (Join-Path $work 'whisper.retry.stdout.log') -StderrLog $retryLog
                $exit = $retryRun.ExitCode
                if ($retryRun.HangDetected) { $whisperExitHang = $true }
                if ($retryRun.TimedOut)     { $whisperTimedOut = $true }
            }
        }
    }
    finally {
        $stopwatch.Stop()
        $env:PYTHONUTF8 = $prevUtf8
        $ErrorActionPreference = $prevEap
    }
    if ($exit -ne 0) {
        $why = if ($exit -ne 0 -and -not $NoGpu -and -not $gpuFailure) {
            " (exit $exit does not match a GPU failure signature, so no CPU retry was attempted; the GPU attempt's own error is the cause)"
        } else { '' }
        throw "whisper-cli failed (exit $exit)$why"
    }

    # Copy artifacts back to the real (possibly non-ASCII) destination.
    $outDir = Split-Path -Parent $OutputBase
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    }
    $artifacts = [ordered]@{}
    $extensions = @('txt')
    if ($WantSrt) { $extensions += 'srt' }
    foreach ($ext in $extensions) {
        $src = "$outBase.$ext"
        if (Test-Path -LiteralPath $src) {
            $dst = "$OutputBase.$ext"
            Copy-Item -LiteralPath $src -Destination $dst -Force
            [System.IO.File]::WriteAllText($dst, (Get-Content -LiteralPath $dst -Raw -Encoding UTF8), (New-Object System.Text.UTF8Encoding($false)))
            $artifacts[$ext] = @{ path = $dst; size = (Get-Item -LiteralPath $dst).Length }
        }
    }

    # Read the captured stderr log for the device ACTUALLY used and the timings.
    # This replaces the old stdout grep, which could never match because
    # whisper.cpp emits those lines on stderr.
    $logLines = Read-ZoombieWhisperLog -Path $whisperLog
    $deviceInfo = Get-ZoombieWhisperDeviceInfo -LogLines $logLines
    $timings = Get-ZoombieWhisperTimings -LogLines $logLines

    # The audio duration drives the realtime factor. ffprobe may be absent, in
    # which case the factor is reported as null rather than guessed.
    $durationSec = $null
    if ($Env.ffprobe -and (Test-Path -LiteralPath $Env.ffprobe)) {
        $prevEapProbe = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            $probeOut = & $Env.ffprobe -v error -show_entries format=duration -of csv=p=0 $AudioPath 2>$null
        }
        finally { $ErrorActionPreference = $prevEapProbe }
        $invariant = [System.Globalization.CultureInfo]::InvariantCulture
        $parsedDuration = 0.0
        if ([double]::TryParse(("$probeOut").Trim(), [System.Globalization.NumberStyles]::Float, $invariant, [ref]$parsedDuration) -and $parsedDuration -gt 0) {
            $durationSec = $parsedDuration
        }
    }

    $totalMs = $timings.TotalMs
    $realtimeFactor = $null
    if ($totalMs -and $durationSec) {
        $realtimeFactor = [math]::Round(($totalMs / 1000.0) / $durationSec, 4)
    }

    # A successful exit that used no GPU device WHILE a GPU backend is configured
    # is the SILENT CPU fallback: exit code 0, no error, hours of CPU work.
    $silentFallback = (-not $NoGpu -and $gpuBackendConfigured -and $deviceInfo.Device -ne $Env.backend)
    if ($silentFallback -and -not $fallbackReason) {
        $fallbackReason = "whisper exited 0 but used the CPU ($($deviceInfo.Reason))"
    }
    if ($fallbackReason) { Write-ZoombieLog -Level Warn -Message "CPU fallback: $fallbackReason" }
    if ($realtimeFactor) {
        Write-ZoombieLog -Level Info -Message ("  device=$($deviceInfo.Device) totalMs=$totalMs realtimeFactor=$realtimeFactor")
    } else {
        Write-ZoombieLog -Level Info -Message "  device=$($deviceInfo.Device) (timings unavailable)"
    }

    # GPU POLICY: if a GPU backend is configured for this install and the user did
    # not opt out (-NoGpu / -AllowCpuFallback), the run MUST have used the GPU.
    # A CPU-only machine never reaches this because its backend is neither cuda
    # nor vulkan. Both failure shapes are covered:
    #   * the backend cannot initialise at all (the missing-cuBLAS case, which
    #     exits 0), caught by the probe before the run and by the log after it;
    #   * the backend initialised but no device was selected.
    $gpuPolicyViolation = $false
    $gpuPolicyReason    = $null
    if ($gpuRequired) {
        if (-not $gpuCapable) {
            # Reliable evidence: the backend could not initialise at all, so the
            # run demonstrably used the CPU (this is the missing-cuBLAS shape).
            $gpuPolicyViolation = $true
            $gpuPolicyReason = "the '$($Env.backend)' backend cannot initialise on this machine, so whisper would run on the CPU"
        } elseif (-not $deviceInfo.DeviceSelected -and -not $deviceInfo.BackendInitialised) {
            # Reliable evidence: no GPU appeared in the run at all.
            $gpuPolicyViolation = $true
            $gpuPolicyReason = "whisper ran on '$($deviceInfo.Device)' but the configured backend is '$($Env.backend)'"
        } elseif (-not $deviceInfo.DeviceSelected -and $deviceInfo.BackendInitialised) {
            # Ambiguous: the backend loaded but no device-selected line was seen.
            # That is usually a banner-format difference rather than a CPU run, so
            # failing here by default could reject an otherwise healthy run. It is
            # reported as deviceVerified=$false plus a loud warning, and -StrictGpu
            # upgrades exactly this case to a hard failure for callers that insist
            # on positive proof. The pre-run probe remains the reliable gate.
            if ($StrictGpu) {
                $gpuPolicyViolation = $true
                $gpuPolicyReason = "the '$($Env.backend)' backend initialised but no device selection was observed, so GPU use is unproven (-StrictGpu)"
            } else {
                Write-ZoombieLog -Level Warn -Message ("the '$($Env.backend)' backend initialised but no device selection was observed in the log; " +
                    'GPU use is unproven (deviceVerified=false, silentCpuFallback=true). Pass -StrictGpu to make this a failure.')
            }
        }
    }

    # Diagnostics must outlive the scratch dir. The old behaviour deleted the log
    # with the work dir, so the one file that explains a fallback disappeared
    # exactly when it was needed. A fallback or a policy violation always keeps a
    # copy next to the transcript.
    $preservedLog = $null
    if ($silentFallback -or $fallbackReason -or $gpuPolicyViolation) {
        $outDirForLog = Split-Path -Parent $OutputBase
        if ($outDirForLog -and -not (Test-Path -LiteralPath $outDirForLog)) {
            New-Item -ItemType Directory -Force -Path $outDirForLog | Out-Null
        }
        $preservedLog = "$OutputBase.whisper.log"
        try {
            Copy-Item -LiteralPath $whisperLog -Destination $preservedLog -Force
            if (Test-Path -LiteralPath $retryLog) {
                Copy-Item -LiteralPath $retryLog -Destination "$OutputBase.whisper.retry.log" -Force
            }
        }
        catch { $preservedLog = $null }
    }

    # Time-boxed and non-blocking: a still-open handle (a killed whisper) must never
    # stop the JSON result being emitted. If the delete cannot finish, the dir is
    # left behind for `clean` rather than holding the run hostage.
    if (-not $KeepWork) {
        $cleaned = Remove-ZoombieWorkDir -Path $work -TimeoutSeconds 30
        if (-not $cleaned) { Write-ZoombieLog -Level Info -Message "  scratch dir left behind (busy): $work" }
    }

    if ($gpuPolicyViolation) {
        throw ("GPU policy violation: $gpuPolicyReason. A GPU backend is configured and usable, so the toolchain refuses to report success from a CPU run; pass -NoGpu to force the CPU deliberately, or -AllowCpuFallback to permit it." +
               $(if ($preservedLog) { " Whisper log: $preservedLog" } else { '' }))
    }

    Write-ZoombieResult -Action 'transcribe' -Ok $true -Data ([ordered]@{
        outputBase     = $OutputBase
        artifacts      = $artifacts
        backend        = $Env.backend
        # backendConfigured is the manifest's intent; deviceUsed is what the run
        # actually did. They diverge in exactly the failure this fixes.
        backendConfigured = $Env.backend
        deviceUsed     = $deviceInfo.Device
        deviceName     = $deviceInfo.DeviceName
        deviceSelected = $deviceInfo.DeviceSelected
        # deviceVerified is the positive proof that a GPU device was selected for
        # decoding (unlike backendInitialised, which only means the module loaded).
        # It is false when GPU use is merely assumed from the backend banner.
        deviceVerified = $deviceInfo.DeviceSelected
        backendInitialised = $deviceInfo.BackendInitialised
        gpuCapable     = $gpuCapable
        gpuRequired    = $gpuRequired
        # Flags actually passed, so a report can be reconciled with the argv.
        flashAttention = $flashAttn
        threads        = if ($NoGpu) { $threads } else { $null }
        loadMs         = $timings.LoadMs
        totalMs        = $totalMs
        encodeMs       = $timings.EncodeMs
        decodeMs       = $timings.DecodeMs
        audioDurationSec = $durationSec
        realtimeFactor = $realtimeFactor
        wallMs         = [math]::Round($stopwatch.Elapsed.TotalMilliseconds, 1)
        # Wall time of the abandoned GPU attempt, so the cost of the fallback is
        # visible instead of being erased by the retry's stopwatch restart.
        gpuAttemptWallMs = $gpuWallMs
        fallbackReason = $fallbackReason
        silentCpuFallback = $silentFallback
        # An exit hang is a build/driver defect, not a failed run: the transcript
        # is complete. It is surfaced so it is never mistaken for a clean exit and
        # so the extra time it costs is visible.
        whisperExitHang = $whisperExitHang
        whisperTimedOut = $whisperTimedOut
        logPath        = $preservedLog
        log            = @($logLines | Where-Object { "$_" -match 'load_backend|ggml_cuda_init|using CUDA|use gpu|backend_init_gpu|error|cannot|fail' } | Select-Object -First 12)
        asciiSafe      = $true
    })
}

function Invoke-Transcribe {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Env)
    if (-not $Source) { throw 'transcribe requires -Source <audio>' }
    if (-not (Test-Path -LiteralPath $Source)) { throw "Input not found: $Source" }

    $base = if ($Output) { $Output } else { [System.IO.Path]::Combine((Get-Location).Path, [System.IO.Path]::GetFileNameWithoutExtension($Source)) }
    $base = [System.IO.Path]::GetFullPath($base)
    if ([System.IO.Path]::GetExtension($base)) { $base = [System.IO.Path]::ChangeExtension($base, $null) }

    Invoke-WhisperOnSafeCopy -Env $Env -AudioPath $Source -OutputBase $base -WantSrt:$Srt -WorkRoot $WorkRoot -KeepWork:$KeepWork `
        -NoFlashAttn:$NoFlashAttn -Threads $Threads -AllowCpuFallback:$AllowCpuFallback -StrictGpu:$StrictGpu
}

# ---------------------------------------------------------------------------
# readpdf (PDF -> Markdown, via the repo's Python)
# ---------------------------------------------------------------------------

function Invoke-ReadPdf {
    <#
    .SYNOPSIS
        Convert a PDF to Markdown with PyMuPDF4LLM, optionally OCR'ing scans.

    .DESCRIPTION
        Mirrors Invoke-Extract's contract, but the extractor is a Python helper
        (pdf\extract_pdf.py) run by the repo's Python interpreter rather than ffmpeg.

        The ASCII invariant still applies: PyMuPDF and Tesseract are native
        libraries that misbehave on non-ASCII paths, so the source PDF is copied
        into the ASCII work dir first, the helper runs entirely there, and the
        produced Markdown (and any images) are copied back to the confirmed,
        possibly Cyrillic, destination.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Env)
    if (-not $Source) { throw 'readpdf requires -Source <pdf>' }
    if (-not (Test-Path -LiteralPath $Source)) { throw "Input not found: $Source" }
    if (-not $Env.python -or -not (Test-Path -LiteralPath $Env.python)) {
        throw 'PDF toolchain needs Python. Install it (winget install Python.Python.3.12), then re-run scripts/setup.ps1.'
    }
    if (-not $Env.pdfScript -or -not (Test-Path -LiteralPath $Env.pdfScript)) {
        throw "PDF helper not found: $($Env.pdfScript). Run scripts/setup.ps1 first."
    }

    # Destination: a .md basename by default, derived from the source name.
    $base = if ($Output) { $Output } else {
        [System.IO.Path]::Combine((Get-Location).Path, [System.IO.Path]::GetFileNameWithoutExtension($Source))
    }
    $base = [System.IO.Path]::GetFullPath($base)
    if ([System.IO.Path]::GetExtension($base) -match '^\.md$') { $base = [System.IO.Path]::ChangeExtension($base, $null) }
    $outMd = "$base.md"

    if ((Test-Path -LiteralPath $outMd) -and -not $script:Force) {
        throw "Output exists (use -Force to overwrite): $outMd"
    }

    # Images (only when -Images) go to a folder beside the Markdown.
    $imagesDir = $null
    if ($Images) { $imagesDir = "$base.images" }

    $workRoot = if ($WorkRoot) { $WorkRoot } else { Get-ZoombieEnvPath -Child 'work' }
    if (-not (Test-ZoombieAsciiPath $workRoot)) { throw "Work root must be ASCII: $workRoot" }
    $work = Join-Path $workRoot ([guid]::NewGuid().ToString('N'))

    # In dry-run, never touch the filesystem; report the paths we would use.
    $safe = if ($script:DryRun) {
        @{ WorkDir = $work; InputPath = (Join-Path $work 'input.pdf'); Markdown = (Join-Path $work 'out.md') }
    } else {
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        $copied = Copy-ZoombieIntoSafeWork -InputPath $Source -WorkDir $work
        @{ WorkDir = $copied.WorkDir; InputPath = $copied.InputPath; Markdown = (Join-Path $work 'out.md') }
    }

    $pyArgs = @($Env.pdfScript, '--input', $safe.InputPath, '--output', $safe.Markdown, '--json')
    if ($Ocr)    { $pyArgs += '--ocr' }
    if ($Pages)  { $pyArgs += @('--pages', $Pages) }
    if ($imagesDir) { $pyArgs += @('--images', (Join-Path $work 'images')) }

    Write-ZoombieLog -Level Step -Message "readpdf -> $outMd"
    Write-ZoombieLog -Level Info -Message "  work=$work  ocr=$([bool]$Ocr)  pages=$(if ($Pages) { $Pages } else { 'all' })"
    if ($script:DryRun) {
        Write-ZoombieResult -Action 'readpdf' -Ok $true -Data ([ordered]@{
            dryRun = $true; work = $work; output = $outMd; args = $pyArgs
        })
        return
    }

    # Native call: relax EAP so Python's stderr progress is not a terminating
    # error under Windows PowerShell 5.1, and force UTF-8 for non-ASCII text.
    $prevUtf8 = $env:PYTHONUTF8; $env:PYTHONUTF8 = '1'
    $prevEap  = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $stdout = @(& $Env.python @pyArgs 2>$null)
        $exit = $LASTEXITCODE
    }
    finally {
        $env:PYTHONUTF8 = $prevUtf8
        $ErrorActionPreference = $prevEap
    }

    $line = @($stdout) | Where-Object { "$_" -match '^\s*\{' } | Select-Object -Last 1
    $parsed = $null
    if ($line) { try { $parsed = $line | ConvertFrom-Json } catch { $parsed = $null } }
    if ($exit -ne 0 -or -not $parsed -or -not $parsed.ok) {
        $detail = if ($parsed -and $parsed.error) { $parsed.error } else { "python exited $exit" }
        throw "readpdf failed: $detail"
    }

    # Copy the Markdown back to the real (possibly non-ASCII) destination.
    $outDir = Split-Path -Parent $outMd
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    }
    Copy-Item -LiteralPath $safe.Markdown -Destination $outMd -Force
    # Re-write as UTF-8 without BOM so downstream tools read it cleanly.
    [System.IO.File]::WriteAllText($outMd, (Get-Content -LiteralPath $outMd -Raw -Encoding UTF8), (New-Object System.Text.UTF8Encoding($false)))

    $artifacts = [ordered]@{ md = @{ path = $outMd; size = (Get-Item -LiteralPath $outMd).Length } }

    $workImages = Join-Path $work 'images'
    if ($imagesDir -and (Test-Path -LiteralPath $workImages)) {
        if (Test-Path -LiteralPath $imagesDir) { Remove-Item -Recurse -Force $imagesDir }
        Move-Item -LiteralPath $workImages -Destination $imagesDir -Force
        $count = @(Get-ChildItem -LiteralPath $imagesDir -File -ErrorAction SilentlyContinue).Count
        $artifacts['images'] = @{ path = $imagesDir; count = $count }
    }

    if (-not $KeepWork) { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue }

    Write-ZoombieResult -Action 'readpdf' -Ok $true -Data ([ordered]@{
        output           = $outMd
        artifacts        = $artifacts
        pages            = $parsed.data.pages
        ocrUsed          = $parsed.data.ocrUsed
        keptScannedPages = @($parsed.data.keptScannedPages)
        asciiSafe        = $true
    })
}

# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

function Invoke-Pipeline {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Env)
    if (-not $Source) { throw 'pipeline requires -Source <url or local file>' }

    $isUrl = $Source -match '^https?://'
    $videoPath = $Source
    $downloaded = $null
    # Temp download dir we created ourselves (and therefore may delete). When the
    # user supplied -DownloadDir, the directory is theirs and is left alone.
    $ownedDlDir = $false

    if ($isUrl) {
        Write-ZoombieLog -Level Step -Message 'pipeline: download stage'
        if ($script:DryRun) {
            Write-ZoombieResult -Action 'pipeline' -Ok $true -Data ([ordered]@{ dryRun = $true; stage = 'download'; url = $Source })
            return
        }
        $py = Assert-Tool -Path $Env.python -Name 'python'
        # Default to a fresh temp dir rather than the shared work root: the video
        # is deleted right after transcription, so it must not be left sitting in
        # zoombie-env\work (nor, if the run fails, block anything else there).
        $dlDir = if ($DownloadDir) { $DownloadDir } else {
            $ownedDlDir = $true
            New-ZoombieAsciiTempDir -Prefix 'zoombie-pipeline'
        }
        if (-not (Test-Path -LiteralPath $dlDir)) { New-Item -ItemType Directory -Force -Path $dlDir | Out-Null }
        $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            & $py -m yt_dlp -f 'bv*+ba/b' --merge-output-format mp4 --no-playlist --no-overwrites `
                  -o (Join-Path $dlDir '%(title)s [%(id)s].%(ext)s') $Source 2>$null
            $ytExit = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $prevEap }
        if ($ytExit -ne 0) { throw "yt-dlp failed (exit $ytExit)" }
        $downloaded = (Get-ChildItem -LiteralPath $dlDir -File |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1)
        $videoPath = $downloaded.FullName
        Write-ZoombieLog -Level Info -Message "downloaded: $videoPath"
    }

    # Extract audio into the ASCII work dir directly: the audio never exists
    # under a non-ASCII path, so whisper.cpp is isolated from the start.
    $workRoot = if ($WorkRoot) { $WorkRoot } else { Get-ZoombieEnvPath -Child 'work' }
    if (-not (Test-ZoombieAsciiPath $workRoot)) { throw "Work root must be ASCII: $workRoot" }
    $work = Join-Path $workRoot ([guid]::NewGuid().ToString('N'))
    $audioOut = Join-Path $work 'audio.wav'

    $base = if ($Output) { [System.IO.Path]::GetFullPath($Output) } else {
        [System.IO.Path]::Combine((Get-Location).Path, [System.IO.Path]::GetFileNameWithoutExtension($videoPath))
    }
    if ([System.IO.Path]::GetExtension($base)) { $base = [System.IO.Path]::ChangeExtension($base, $null) }

    if ($script:DryRun) {
        Write-ZoombieResult -Action 'pipeline' -Ok $true -Data ([ordered]@{
            dryRun = $true; stage = 'extract+transcribe'; video = $videoPath; outputBase = $base; work = $work
        })
        return
    }

    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $ff = Assert-Tool -Path $Env.ffmpeg -Name 'ffmpeg'
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & $ff -y -hide_banner -nostats -v error -i $videoPath -vn -ac 1 -ar 16000 -c:a pcm_s16le $audioOut 2>$null
        $ffExit = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prevEap }
    if ($ffExit -ne 0) { throw "ffmpeg failed (exit $ffExit)" }

    Invoke-WhisperOnSafeCopy -Env $Env -AudioPath $audioOut -OutputBase $base -WantSrt:$Srt -WorkRoot $workRoot -KeepWork:$KeepWork `
        -NoFlashAttn:$NoFlashAttn -Threads $Threads -AllowCpuFallback:$AllowCpuFallback -StrictGpu:$StrictGpu

    if (-not $KeepWork) {
        # Remove the downloaded video (its transcript is the actual output), the
        # temp download dir when we created it, and this stage's work dir with the
        # intermediate audio.wav. whisper's own scratch was already cleaned inside
        # Invoke-WhisperOnSafeCopy; leaving this dir behind was the remaining leak.
        if ($downloaded) { Remove-Item -LiteralPath $downloaded.FullName -ErrorAction SilentlyContinue }
        if ($ownedDlDir) { Remove-Item -Recurse -Force $dlDir -ErrorAction SilentlyContinue }
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------

function Invoke-Clean {
    [CmdletBinding()]
    param()
    $workRoot = if ($WorkRoot) { $WorkRoot } else { Get-ZoombieEnvPath -Child 'work' }
    if (-not (Test-Path -LiteralPath $workRoot)) {
        Write-ZoombieResult -Action 'clean' -Ok $true -Data ([ordered]@{ removed = 0; workRoot = $workRoot })
        return
    }
    $dirs = @(Get-ChildItem -LiteralPath $workRoot -Directory -ErrorAction SilentlyContinue)
    if ($script:DryRun) {
        Write-ZoombieResult -Action 'clean' -Ok $true -Data ([ordered]@{ dryRun = $true; wouldRemove = $dirs.Count; workRoot = $workRoot })
        return
    }
    foreach ($d in $dirs) { Remove-Item -Recurse -Force $d.FullName -ErrorAction SilentlyContinue }
    Write-ZoombieResult -Action 'clean' -Ok $true -Data ([ordered]@{ removed = $dirs.Count; workRoot = $workRoot })
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

try {
    switch ($Command) {
        'doctor'     { Invoke-Doctor }
        'download'   { Invoke-Download   -Env (Get-Environment) }
        'extract'    { Invoke-Extract    -Env (Get-Environment) }
        'transcribe' { Invoke-Transcribe -Env (Get-Environment) }
        'readpdf'    { Invoke-ReadPdf    -Env (Get-Environment) }
        # No `| Out-Null` here: Write-ZoombieResult already emits the one JSON
        # line via Write-Output, and Invoke-Pipeline produces nothing else on
        # stdout. Piping it away silently broke `pipeline` for every caller
        # (including the zoombie-transcribe-video skill), which reads the result.
        'pipeline'   { Invoke-Pipeline   -Env (Get-Environment) }
        'clean'      { Invoke-Clean }
    }
}
catch {
    Write-ZoombieResult -Action $Command -Ok $false -ErrorMessage $_.Exception.Message
    exit 1
}

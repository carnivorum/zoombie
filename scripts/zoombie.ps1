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
    One of: doctor, download, extract, transcribe, pipeline, clean.

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

.PARAMETER Language
    transcribe: force a language; 'auto' by default.

.PARAMETER Srt
    transcribe: also emit .srt subtitles.

.PARAMETER NoGpu
    transcribe: force CPU (-ng) even when a GPU backend is configured.

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
    pwsh -File scripts/zoombie.ps1 pipeline -Source "https://youtu.be/XXXX" -Output .\transcripts\clip
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('doctor', 'download', 'extract', 'transcribe', 'pipeline', 'clean')]
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
        ffmpeg   = Resolve-ZoombieTool -Name 'ffmpeg'  -Candidates @($(if ($manifest) { $manifest.ffmpeg.path }))
        ffprobe  = Resolve-ZoombieTool -Name 'ffprobe' -Candidates @($(if ($manifest) { $manifest.ffprobe.path }))
        ytDlp    = Resolve-ZoombieTool -Name 'yt-dlp'  -Candidates @($(if ($manifest) { $manifest.ytDlp.path }))
        whisper  = Resolve-ZoombieTool -Name 'whisper-cli' -Candidates @($(if ($manifest) { $manifest.whisper.path }))
        model    = $null
        backend  = $(if ($manifest) { $manifest.whisper.backend } else { $null })
    }

    if ($Model) {
        if (Test-Path -LiteralPath $Model) {
            $env.model = (Resolve-Path -LiteralPath $Model).Path
        } else {
            $fileName = if ($Model -like 'ggml-*.bin') { $Model } else { "ggml-$Model.bin" }
            $env.model = Join-Path (Get-ZoombieEnvPath -Child 'models') $fileName
        }
    } elseif ($manifest) {
        $env.model = $manifest.model.path
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
    $report = [ordered]@{
        root      = $env.root
        asciiRoot = (Test-ZoombieAsciiPath $env.root)
        ffmpeg    = @{ path = $env.ffmpeg;  version = if ($env.ffmpeg)  { Get-ZoombieToolVersion -Exe $env.ffmpeg  -VersionArgs '-version' } else { $null } }
        ffprobe   = @{ path = $env.ffprobe; version = if ($env.ffprobe) { Get-ZoombieToolVersion -Exe $env.ffprobe -VersionArgs '-version' } else { $null } }
        ytDlp     = @{ path = $env.ytDlp;   version = if ($env.ytDlp)   { Get-ZoombieToolVersion -Exe $env.ytDlp   -VersionArgs '--version' } else { $null } }
        whisper   = @{ path = $env.whisper; backend = $env.backend }
        model     = @{ path = $env.model; exists = ($env.model -and (Test-Path -LiteralPath $env.model)) }
    }
    $missing = @()
    if (-not $report.ffmpeg.path)  { $missing += 'ffmpeg' }
    if (-not $report.ffprobe.path) { $missing += 'ffprobe' }
    if (-not $report.ytDlp.path)   { $missing += 'yt-dlp' }
    if (-not $report.whisper.path) { $missing += 'whisper-cli' }
    if (-not $report.model.exists) { $missing += 'model' }
    Write-ZoombieResult -Action 'doctor' -Ok ($missing.Count -eq 0) -Data ([ordered]@{
        report  = $report
        missing = $missing
    }) -ErrorMessage $(if ($missing.Count) { "Missing: $($missing -join ', ')" } else { $null })
}

# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

function Invoke-Download {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Env)
    if (-not $Source) { throw 'download requires -Source <url>' }
    $yt = Assert-Tool -Path $Env.ytDlp -Name 'yt-dlp'

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

    Write-ZoombieLog -Level Step -Message "yt-dlp $($args -join ' ')"
    if ($script:DryRun) {
        Write-ZoombieResult -Action 'download' -Ok $true -Data ([ordered]@{ dryRun = $true; dir = $dir; args = $args })
        return
    }
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $yt @args 2>$null; $ytExit = $LASTEXITCODE }
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
        [switch]$WantSrt
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
    if (-not $script:DryRun) {
        New-Item -ItemType Directory -Force -Path $work | Out-Null
    }
    $safe = Copy-ZoombieIntoSafeWork -InputPath $AudioPath -WorkDir $work
    $outBase = Join-Path $work 'out'

    $args = @('-m', $Env.model, '-f', $safe.InputPath, '-l', $Language,
              '-otxt', '-nt')
    if ($WantSrt) { $args += '-osrt' }
    if ($NoGpu)   { $args += '-ng' }
    $args += @('-of', $outBase)

    Write-ZoombieLog -Level Step -Message "whisper-cli (ascii-safe) -> $OutputBase"
    Write-ZoombieLog -Level Info -Message "  work=$work  backend=$($Env.backend)  model=$(Split-Path -Leaf $Env.model)"
    if ($script:DryRun) {
        Write-ZoombieResult -Action 'transcribe' -Ok $true -Data ([ordered]@{
            dryRun = $true; work = $work; outputBase = $OutputBase; args = $args
        })
        return
    }

    # Hardened invocation.
    # IMPORTANT (Windows PowerShell 5.1): whisper-cli writes a log banner to
    # stderr. With $ErrorActionPreference='Stop' that native stderr output is
    # turned into a terminating error, so the preference is relaxed for the
    # duration of the native call. stdout is captured on its own so the banner
    # never pollutes the transcript file.
    $prevUtf8 = $env:PYTHONUTF8; $env:PYTHONUTF8 = '1'
    $prevEap  = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $stdout = @(& $whisper @args 2>$null)
        $exit = $LASTEXITCODE

        if ($exit -ne 0 -and -not $NoGpu) {
            # GPU path may fail on driver mismatch: retry once forcing CPU.
            Write-ZoombieLog -Level Warn -Message "whisper exited $exit; retrying with -ng (CPU)"
            $argsRetry = @('-m', $Env.model, '-f', $safe.InputPath, '-l', $Language, '-otxt', '-nt', '-ng')
            if ($WantSrt) { $argsRetry += '-osrt' }
            $argsRetry += @('-of', $outBase)
            $stdout = @(& $whisper @argsRetry 2>$null)
            $exit = $LASTEXITCODE
        }
    }
    finally {
        $env:PYTHONUTF8 = $prevUtf8
        $ErrorActionPreference = $prevEap
    }
    if ($exit -ne 0) { throw "whisper-cli failed (exit $exit)" }

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

    # Log banner captured from stdout (kept out of the .txt file).
    $banner = @($stdout) | Where-Object { "$_" -match 'ggml_cuda_init|whisper_init|GPU|CUDA' } | Select-Object -First 6

    if (-not $KeepWork) { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue }

    Write-ZoombieResult -Action 'transcribe' -Ok $true -Data ([ordered]@{
        outputBase = $OutputBase
        artifacts  = $artifacts
        backend    = $Env.backend
        log        = $banner
        asciiSafe  = $true
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

    Invoke-WhisperOnSafeCopy -Env $Env -AudioPath $Source -OutputBase $base -WantSrt:$Srt
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

    if ($isUrl) {
        Write-ZoombieLog -Level Step -Message 'pipeline: download stage'
        if ($script:DryRun) {
            Write-ZoombieResult -Action 'pipeline' -Ok $true -Data ([ordered]@{ dryRun = $true; stage = 'download'; url = $Source })
            return
        }
        $yt = Assert-Tool -Path $Env.ytDlp -Name 'yt-dlp'
        $dlDir = if ($DownloadDir) { $DownloadDir } else { Get-ZoombieEnvPath -Child 'work' }
        if (-not (Test-Path -LiteralPath $dlDir)) { New-Item -ItemType Directory -Force -Path $dlDir | Out-Null }
        $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            & $yt -f 'bv*+ba/b' --merge-output-format mp4 --no-playlist --no-overwrites `
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

    Invoke-WhisperOnSafeCopy -Env $Env -AudioPath $audioOut -OutputBase $base -WantSrt:$Srt -WorkRoot $workRoot -KeepWork:$KeepWork

    if ($downloaded -and -not $KeepWork) {
        Remove-Item -LiteralPath $downloaded.FullName -ErrorAction SilentlyContinue
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
        'pipeline'   { Invoke-Pipeline   -Env (Get-Environment) | Out-Null }
        'clean'      { Invoke-Clean }
    }
}
catch {
    Write-ZoombieResult -Action $Command -Ok $false -ErrorMessage $_.Exception.Message
    exit 1
}

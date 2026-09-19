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
    $report = [ordered]@{
        root      = $env.root
        asciiRoot = (Test-ZoombieAsciiPath $env.root)
        ffmpeg    = @{ path = $env.ffmpeg;  version = if ($env.ffmpeg)  { Get-ZoombieToolVersion -Exe $env.ffmpeg  -VersionArgs '-version' } else { $null } }
        ffprobe   = @{ path = $env.ffprobe; version = if ($env.ffprobe) { Get-ZoombieToolVersion -Exe $env.ffprobe -VersionArgs '-version' } else { $null } }
        ytDlp     = @{ via = 'python -m yt_dlp'; python = $env.python; version = if ($env.python) { Get-ZoombieToolVersion -Exe $env.python -VersionArgs @('-m', 'yt_dlp', '--version') } else { $null } }
        whisper   = @{ path = $env.whisper; backend = $env.backend }
        model     = @{ path = $env.model; exists = ($env.model -and (Test-Path -LiteralPath $env.model)) }
    }
    $missing = @()
    if (-not $report.ffmpeg.path)  { $missing += 'ffmpeg' }
    if (-not $report.ffprobe.path) { $missing += 'ffprobe' }
    if (-not $report.ytDlp.version) { $missing += 'yt-dlp' }
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
        [switch]$KeepWork
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

    Invoke-WhisperOnSafeCopy -Env $Env -AudioPath $Source -OutputBase $base -WantSrt:$Srt -WorkRoot $WorkRoot -KeepWork:$KeepWork
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

    Invoke-WhisperOnSafeCopy -Env $Env -AudioPath $audioOut -OutputBase $base -WantSrt:$Srt -WorkRoot $workRoot -KeepWork:$KeepWork

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

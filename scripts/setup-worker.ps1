#Requires -Version 5.1
<#
.SYNOPSIS
    Install/update worker for the zoombie speech-to-text toolchain.

.DESCRIPTION
    This is the WORKER, not the entry point. The thin entry point is
    scripts/setup.ps1: it always fetches the latest copy of this file from
    GitHub and runs it with -Refresh, so any start of setup means
    "install or update the latest".

    Local development runs THIS file directly (no network refresh), so edits in
    a working tree are installed as-is.

    Everything is installed into a single ASCII root (default
    %USERPROFILE%\zoombie-env) so that:

      * whisper.cpp never sees a non-ASCII / Cyrillic path (the bug we are fixing),
      * nothing depends on install locations or a refreshed PATH,
      * a re-run is a no-op for anything already present.

    Tools are fetched straight into the root; winget is not required.

    When the repo files (shared module, runtime CLI, self-test, skills) are not
    present next to this script — or when -Refresh is set by setup.ps1 — it
    bootstraps them from GitHub into a local checkout before installing.

.PARAMETER Check
    Detect only. Report what is present/missing. Writes nothing.

.PARAMETER DryRun
    Print the actions that would be taken. Writes nothing.

.PARAMETER Model
    Whisper model name to ensure (e.g. large-v3-turbo, small, base-q5_0).
    Default: the size recommended from the detected hardware.

.PARAMETER Root
    Override the toolchain root. Default: %USERPROFILE%\zoombie-env.

.PARAMETER Force
    Re-download even if a component looks present.

.PARAMETER Refresh
    Internal. Set by setup.ps1 so a run through the thin entry point always
    re-fetches the repo files (module, CLI, self-test, worker) from GitHub,
    guaranteeing an update rather than a stale re-install.

.EXAMPLE
    pwsh -File scripts/setup-worker.ps1 -Check
.EXAMPLE
    pwsh -File scripts/setup-worker.ps1 -DryRun
.EXAMPLE
    pwsh -File scripts/setup-worker.ps1
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$DryRun,
    [string]$Model,
    [string]$Root,
    [switch]$Force,
    [switch]$Refresh
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 0. Bootstrap: fetch the repo files (shared module, CLI, self-test, skills).
# ---------------------------------------------------------------------------
# This runs when the module is not next to us (a fresh/standalone checkout) or
# when setup.ps1 passed -Refresh (a run through the thin entry point must always
# update to the latest). A plain local `setup-worker.ps1` run inside a working
# tree skips it, so uncommitted local edits are installed as-is.

$script:RepoSlug       = if ($env:ZOOMBIE_REPO_SLUG) { $env:ZOOMBIE_REPO_SLUG } else { 'carnivorum/zoombie' }
$script:RepoRef        = if ($env:ZOOMBIE_REPO_REF)  { $env:ZOOMBIE_REPO_REF }  else { 'main' }
$script:RepoRawBase    = "https://raw.githubusercontent.com/$($script:RepoSlug)/$($script:RepoRef)"
$script:RepoArchiveUrl = "https://codeload.github.com/$($script:RepoSlug)/zip/refs/heads/$($script:RepoRef)"

function Get-ZoombieRepoFile {
    <#
    .SYNOPSIS
        Fetch one file from the repo and write it into the local checkout.

    .DESCRIPTION
        $RepoPath is the path inside the repository (e.g. scripts/lib/...), used
        to build the raw URL. $DestRelative is where it lands relative to this
        script, which mirrors the repo's scripts\ folder layout.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$RepoPath,
        [Parameter(Mandatory)][string]$DestRelative
    )
    $dest = Join-Path $PSScriptRoot $DestRelative
    $dir  = Split-Path -Parent $dest
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $url = "$($script:RepoRawBase)/$($RepoPath -replace '\\', '/')"
    [Console]::Error.WriteLine("==> fetch $url")
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & curl.exe -L --fail --retry 3 --silent --show-error -o $dest $url 2>$null
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prevEap }
    if ($code -ne 0) { throw "Failed to fetch $url (curl exit $code)" }
}

$repoFilesPresent = Test-Path -LiteralPath (Join-Path $PSScriptRoot 'lib\ZoombieEnv.psm1')
if ($Refresh -or -not $repoFilesPresent) {
    if ($Refresh) { [Console]::Error.WriteLine('==> refreshing repo files to the latest') }
    else          { [Console]::Error.WriteLine('==> standalone run: fetching the rest of the repo') }

    # Prefer a single archive download: it is one request and keeps the tree
    # consistent. Fall back to per-file raw fetches if the archive is blocked.
    $fetched = $false
    $tmpZip = Join-Path $env:TEMP ("zoombie-repo-" + [guid]::NewGuid().ToString('N') + '.zip')
    $tmpEx  = Join-Path $env:TEMP ("zoombie-repo-" + [guid]::NewGuid().ToString('N'))
    try {
        [Console]::Error.WriteLine("==> download $($script:RepoArchiveUrl)")
        $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & curl.exe -L --fail --retry 3 --silent --show-error -o $tmpZip $script:RepoArchiveUrl 2>$null; $code = $LASTEXITCODE }
        finally { $ErrorActionPreference = $prevEap }
        if ($code -eq 0) {
            New-Item -ItemType Directory -Force -Path $tmpEx | Out-Null
            Expand-Archive -LiteralPath $tmpZip -DestinationPath $tmpEx -Force
            $rootInZip = Get-ChildItem -LiteralPath $tmpEx -Directory | Select-Object -First 1
            if ($rootInZip) {
                # Copy the repo's own folders next to this script so relative
                # paths (lib\, zoombie.ps1, ..\skills) keep working. The worker
                # refreshes itself too, so a stale local copy cannot persist.
                foreach ($f in @('lib', 'pdf', 'zoombie.ps1', 'selftest.ps1', 'setup-worker.ps1', 'requirements-pdf.txt')) {
                    $from = Join-Path $rootInZip.FullName (Join-Path 'scripts' $f)
                    if (Test-Path -LiteralPath $from) {
                        Copy-Item -LiteralPath $from -Destination $PSScriptRoot -Recurse -Force
                    }
                }
                $skillsFrom = Join-Path $rootInZip.FullName 'skills'
                if (Test-Path -LiteralPath $skillsFrom) {
                    Copy-Item -LiteralPath $skillsFrom -Destination (Split-Path -Parent $PSScriptRoot) -Recurse -Force
                }
                $fetched = $true
                [Console]::Error.WriteLine('==> repo fetched from archive')
            }
        }
    }
    catch {
        [Console]::Error.WriteLine("WARN archive fetch failed: $($_.Exception.Message)")
    }
    finally {
        Remove-Item -LiteralPath $tmpZip -Force -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $tmpEx -ErrorAction SilentlyContinue
    }

    if (-not $fetched) {
        [Console]::Error.WriteLine('==> falling back to per-file download')
        # The local checkout mirrors the repo's scripts\ folder, so the repo path
        # carries the `scripts/` prefix while the destination does not.
        $fileMap = @(
            @{ Repo = 'scripts/lib/ZoombieEnv.psm1';  Dest = 'lib\ZoombieEnv.psm1' },
            @{ Repo = 'scripts/pdf/extract_pdf.py';   Dest = 'pdf\extract_pdf.py' },
            @{ Repo = 'scripts/requirements-pdf.txt'; Dest = 'requirements-pdf.txt' },
            @{ Repo = 'scripts/zoombie.ps1';          Dest = 'zoombie.ps1' },
            @{ Repo = 'scripts/selftest.ps1';         Dest = 'selftest.ps1' },
            @{ Repo = 'scripts/setup-worker.ps1';     Dest = 'setup-worker.ps1' }
        )
        foreach ($m in $fileMap) {
            Get-ZoombieRepoFile -RepoPath $m.Repo -DestRelative $m.Dest
        }
        foreach ($skill in @('zoombie-download-video', 'zoombie-extract-audio', 'zoombie-transcribe-audio', 'zoombie-transcribe-video', 'zoombie-pdf-to-md')) {
            $destSkill = Join-Path (Split-Path -Parent $PSScriptRoot) "skills\$skill\SKILL.md"
            $dir = Split-Path -Parent $destSkill
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            $url = "$($script:RepoRawBase)/skills/$skill/SKILL.md"
            [Console]::Error.WriteLine("==> fetch $url")
            $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try { & curl.exe -L --fail --retry 3 --silent --show-error -o $destSkill $url 2>$null; $code = $LASTEXITCODE }
            finally { $ErrorActionPreference = $prevEap }
            if ($code -ne 0) { throw "Failed to fetch $url (curl exit $code)" }
        }
    }
}

$modulePath = Join-Path $PSScriptRoot 'lib\ZoombieEnv.psm1'
Import-Module $modulePath -Force

if ($Root) { $env:ZOOMBIE_ENV_ROOT = $Root }
Set-ZoombieUtf8Console

$script:DryRun = [bool]$DryRun
$script:Check  = [bool]$Check
$script:Force  = [bool]$Force
$script:Changes = New-Object System.Collections.Generic.List[string]

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

function Test-WriteAllowed {
    # In -Check and -DryRun we never touch the filesystem.
    return (-not $script:Check -and -not $script:DryRun)
}

function Get-Field {
    <#
    .SYNOPSIS
        StrictMode-safe read of a (optionally dotted) field from an object.

    .DESCRIPTION
        Under Set-StrictMode -Version Latest, reading a MISSING member of a
        PSCustomObject (e.g. one parsed from a legacy/partial env.json) throws
        and aborts the caller. Reading is done through PSObject.Properties so a
        missing field yields $Default instead. Dotted paths walk nested objects,
        so a missing intermediate section also yields $Default, never an error.

        Declared here (with the other small helpers) because Install-Whisper
        runs before the main flow and needs it.
    #>
    param($Object, [Parameter(Mandatory)][string]$Path, $Default = $null)
    $current = $Object
    foreach ($key in $Path.Split('.')) {
        if ($null -eq $current) { return $Default }
        if ($current -is [System.Collections.IDictionary]) {
            if (-not $current.Contains($key)) { return $Default }
            $current = $current[$key]
            continue
        }
        $prop = $current.PSObject.Properties[$key]
        if (-not $prop) { return $Default }
        $current = $prop.Value
    }
    if ($null -eq $current) { return $Default }
    return $current
}

function Invoke-ZoombieDownload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$OutFile
    )
    Write-ZoombieLog -Level Step -Message "download $Url"
    Write-ZoombieLog -Level Info -Message "     -> $OutFile"
    if (-not (Test-WriteAllowed)) { return }
    if ((Test-Path -LiteralPath $OutFile) -and -not $script:Force) {
        Write-ZoombieLog -Level Info -Message "     already present, skipping"
        return
    }
    $parent = Split-Path -Parent $OutFile
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & curl.exe -L --fail --retry 3 --silent --show-error -o $OutFile $Url 2>$null; $curlExit = $LASTEXITCODE }
    finally { $ErrorActionPreference = $prevEap }
    if ($curlExit -ne 0) {
        throw "Download failed (curl exit $curlExit): $Url"
    }
}

function Get-GitHubReleases {
    <#
    .SYNOPSIS
        Fetch releases (newest first) for a repo. Returns an array of release objects.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Repo, [int]$Count = 30)
    $uri = "https://api.github.com/repos/$Repo/releases?per_page=$Count"
    $headers = @{ 'User-Agent' = 'zoombie-setup'; 'Accept' = 'application/vnd.github+json' }
    try {
        return Invoke-RestMethod -Uri $uri -Headers $headers -UseBasicParsing
    }
    catch {
        throw "Could not query GitHub releases for $Repo`: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Hardware probe + model recommendation
# ---------------------------------------------------------------------------

function Get-HardwareProfile {
    [CmdletBinding()]
    param()
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
    $gpus = @(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name)

    $nvidiaName = $null
    $vramMb = $null
    $driver = $null
    $smi = Resolve-ZoombieTool -Name 'nvidia-smi'
    if ($smi) {
        try {
            # Machine-readable: name,total VRAM,driver. AdapterRAM is unusable (>4GB overflow).
            $csv = & $smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>$null
            $first = @($csv) | Select-Object -First 1
            if ($first) {
                $parts = ($first -split ',').Trim()
                $nvidiaName = $parts[0]
                if ($parts.Count -ge 2) { $vramMb = [int]($parts[1] -replace '[^0-9]', '') }
                if ($parts.Count -ge 3) { $driver = $parts[2] }
            }
        }
        catch { }
    }

    $backend = 'cpu'
    if ($nvidiaName) { $backend = 'cuda' }
    elseif ($gpus.Count -gt 0) { $backend = 'vulkan' }

    return [ordered]@{
        cpuName    = if ($cpu) { $cpu.Name } else { $null }
        cpuCores   = if ($cpu) { $cpu.NumberOfCores } else { $null }
        cpuThreads = if ($cpu) { $cpu.NumberOfLogicalProcessors } else { $null }
        ramGb      = $ramGb
        gpus       = $gpus
        nvidia     = $nvidiaName
        vramMb     = $vramMb
        gpuDriver  = $driver
        backend    = $backend
    }
}

function Get-RecommendedModel {
    <#
    .SYNOPSIS
        Pick a model size from the hardware profile. Smaller/faster when constrained.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Hw)
    if ($Hw.backend -eq 'cuda' -and $Hw.vramMb) {
        if ($Hw.vramMb -ge 8000) { return 'large-v3-turbo' }
        if ($Hw.vramMb -ge 4000) { return 'medium' }
        return 'small'
    }
    if ($Hw.backend -eq 'vulkan') { return 'small' }
    if ($Hw.ramGb -ge 16) { return 'medium' }
    if ($Hw.ramGb -ge 8)  { return 'small' }
    if ($Hw.ramGb -ge 4)  { return 'base' }
    return 'tiny'
}

# ---------------------------------------------------------------------------
# Component installers
# ---------------------------------------------------------------------------

function Install-FFmpeg {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$BinDir)
    $ffmpeg  = Get-ZoombieEnvPath -Child 'bin', 'ffmpeg.exe'
    $ffprobe = Get-ZoombieEnvPath -Child 'bin', 'ffprobe.exe'

    if ((Test-Path $ffmpeg) -and (Test-Path $ffprobe) -and -not $script:Force) {
        Write-ZoombieLog -Level Info -Message "ffmpeg present: $(Get-ZoombieToolVersion -Exe $ffmpeg -VersionArgs '-version')"
        return $true
    }
    if (-not (Test-WriteAllowed)) { Write-ZoombieLog -Level Step -Message "would install ffmpeg -> $BinDir"; return $false }

    $tmp = New-ZoombieAsciiTempDir -Prefix 'zoombie-ffmpeg'
    try {
        $zip = Join-Path $tmp 'ffmpeg.zip'
        # BtbN static GPL build: ships ffmpeg + ffprobe in bin\.
        Invoke-ZoombieDownload -Url 'https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip' -OutFile $zip
        $extract = Join-Path $tmp 'x'
        Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
        $binSrc = Get-ChildItem -Path $extract -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
        if (-not $binSrc) { throw 'ffmpeg.exe not found in archive' }
        Copy-Item -LiteralPath $binSrc.FullName -Destination $ffmpeg -Force
        $probeSrc = Get-ChildItem -Path $binSrc.Directory.FullName -Filter 'ffprobe.exe' | Select-Object -First 1
        if ($probeSrc) { Copy-Item -LiteralPath $probeSrc.FullName -Destination $ffprobe -Force }
        $script:Changes.Add('installed ffmpeg/ffprobe')
        return $true
    }
    finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

function Install-YtDlp {
    <#
    .SYNOPSIS
        Ensure yt-dlp is installed as a Python package, invoked via `python -m yt_dlp`.

    .DESCRIPTION
        yt-dlp is pure Python and therefore ASCII-path safe - unlike whisper.cpp,
        which is native C++ and breaks on non-ASCII paths - so it needs none of
        the ASCII isolation the media tools get. It is installed into the shared
        Python interpreter instead of being shipped as a local exe, which removes
        a per-machine download and the launcher shim that came with it.

        `python -m yt_dlp` runs the module directly: no PATH entry and no console
        shim are needed, so the shim pip creates is removed right after install.

        Idempotent: an already-working yt-dlp is left untouched and no network
        access happens. Any pre-existing local copy is left alone - this function
        only adds the Python package the CLI now invokes.

    .OUTPUTS
        A hashtable: @{ Ok; Version; Note }
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowNull()][string]$Python)

    $result = @{ Ok = $false; Version = $null; Note = $null }

    if (-not $Python) {
        $result.Note = 'Python not found. Install with: winget install Python.Python.3.12'
        Write-ZoombieLog -Level Warn -Message "yt-dlp skipped: $($result.Note)"
        return $result
    }

    # Already importable? Nothing to do (and no network access).
    $existing = Get-ZoombieToolVersion -Exe $Python -VersionArgs @('-m', 'yt_dlp', '--version')
    if ($existing -and -not $script:Force) {
        $result.Ok = $true
        $result.Version = $existing
        Write-ZoombieLog -Level Info -Message "yt-dlp present: $existing (python -m yt_dlp)"
        return $result
    }

    if (-not (Test-WriteAllowed)) {
        Write-ZoombieLog -Level Step -Message "would pip install yt-dlp into $Python"
        $result.Ok = $true
        return $result
    }

    $before = Get-ZoombiePipShimSnapshot
    Write-ZoombieLog -Level Step -Message 'installing yt-dlp (pip --user)'
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & $Python -m pip install --user --no-warn-script-location --disable-pip-version-check --quiet yt-dlp 2>$null
        $pipExit = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prevEap }
    if ($pipExit -ne 0) {
        $result.Note = "pip install yt-dlp failed (exit $pipExit)"
        Write-ZoombieLog -Level Warn -Message "yt-dlp: $($result.Note)"
        return $result
    }

    # We invoke the module, never the console launcher, so drop the shim pip made.
    $removed = Remove-ZoombieNewPipShims -Before $before
    if ($removed -gt 0) { Write-ZoombieLog -Level Info -Message "removed $removed unused pip shim(s)" }

    $result.Version = Get-ZoombieToolVersion -Exe $Python -VersionArgs @('-m', 'yt_dlp', '--version')
    if (-not $result.Version) {
        $result.Note = 'yt-dlp installed but `python -m yt_dlp` is not working'
        Write-ZoombieLog -Level Warn -Message "yt-dlp: $($result.Note)"
        return $result
    }
    $result.Ok = $true
    $script:Changes.Add('installed yt-dlp (python -m yt_dlp)')
    return $result
}

function Select-WhisperAsset {
    <#
    .SYNOPSIS
        Scan releases newest-first and pick the first that actually ships a suitable
        Windows x64 binary asset (tagged releases often have no binaries attached).

    .OUTPUTS
        A hashtable: @{ Tag; Name; Url; Backend }
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateSet('cuda', 'vulkan', 'cpu')][string]$Backend)

    # Preference order of asset filename fragments per backend.
    $patterns = switch ($Backend) {
        'cuda'   { @('cublas.*bin-x64\.zip$', 'cuda.*bin-x64\.zip$') }
        'vulkan' { @('vulkan.*bin-x64\.zip$') }
        'cpu'    { @('^whisper-bin-x64\.zip$', 'bin-x64\.zip$') }
    }

    $releases = Get-GitHubReleases -Repo 'ggml-org/whisper.cpp'
    foreach ($rel in $releases) {
        if (-not $rel.assets -or $rel.assets.Count -eq 0) { continue }
        foreach ($pattern in $patterns) {
            $hit = $rel.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
            if ($hit) {
                return @{
                    Tag     = $rel.tag_name
                    Name    = $hit.name
                    Url     = $hit.browser_download_url
                    Backend = $Backend
                }
            }
        }
    }
    return $null
}

function Install-Whisper {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Backend)
    $destDir = Get-ZoombieEnvPath -Child 'bin', 'whisper'
    $exe = Join-Path $destDir 'whisper-cli.exe'

    if ((Test-Path $exe) -and -not $script:Force) {
        Write-ZoombieLog -Level Info -Message "whisper-cli present: $exe"
        # Preserve tag/asset recorded by a previous run so the manifest stays complete.
        # Read StrictMode-safely: a legacy/partial env.json may have no `whisper`
        # section, in which case $prev.whisper.tag would throw under StrictMode.
        $prev = Get-ZoombieEnvManifest
        $prevTag     = Get-Field $prev 'whisper.tag'
        $prevAsset   = Get-Field $prev 'whisper.asset'
        $prevBackend = Get-Field $prev 'whisper.backend' $Backend
        # If a previous run never recorded which asset it used, recover it with a
        # single release scan so env.json stays a complete record.
        if (-not $prevTag -and -not $script:Check -and -not $script:DryRun) {
            $scan = Select-WhisperAsset -Backend $prevBackend
            if ($scan) { $prevTag = $scan.Tag; $prevAsset = $scan.Name }
        }
        return @{ Exe = $exe; Tag = $prevTag; Asset = $prevAsset; Backend = $prevBackend }
    }

    $asset = Select-WhisperAsset -Backend $Backend
    if (-not $asset -and $Backend -ne 'cpu') {
        Write-ZoombieLog -Level Warn -Message "no $Backend asset found; falling back to CPU build"
        $asset = Select-WhisperAsset -Backend 'cpu'
    }
    if (-not $asset) { throw 'No suitable whisper.cpp Windows asset found in any release.' }

    Write-ZoombieLog -Level Step -Message "selected whisper.cpp $($asset.Tag) asset $($asset.Name) [$($asset.Backend)]"
    if (-not (Test-WriteAllowed)) {
        Write-ZoombieLog -Level Info -Message "     would extract to $destDir"
        return @{ Exe = $exe; Tag = $asset.Tag; Asset = $asset.Name; Backend = $asset.Backend }
    }

    $tmp = New-ZoombieAsciiTempDir -Prefix 'zoombie-whisper'
    try {
        $zip = Join-Path $tmp 'whisper.zip'
        Invoke-ZoombieDownload -Url $asset.Url -OutFile $zip
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        # Keep DLLs next to the exe: the CUDA bundle needs them adjacent.
        Expand-Archive -LiteralPath $zip -DestinationPath $destDir -Force
        $found = Get-ChildItem -Path $destDir -Recurse -Filter 'whisper-cli.exe' | Select-Object -First 1
        if (-not $found) { throw 'whisper-cli.exe not found in archive' }
        if ($found.Directory.FullName -ne $destDir) {
            # Flatten one level if the archive nested the binaries.
            Get-ChildItem -Path $found.Directory.FullName -File | ForEach-Object {
                Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destDir $_.Name) -Force
            }
            $exe = Join-Path $destDir 'whisper-cli.exe'
        }
        $script:Changes.Add("installed whisper.cpp $($asset.Tag) ($($asset.Backend))")
        return @{ Exe = $exe; Tag = $asset.Tag; Asset = $asset.Name; Backend = $asset.Backend }
    }
    finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

function Install-WhisperModel {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ModelName)
    $modelsDir = Get-ZoombieEnvPath -Child 'models'
    $fileName = if ($ModelName -like 'ggml-*.bin') { $ModelName } else { "ggml-$ModelName.bin" }
    $dest = Join-Path $modelsDir $fileName

    if ((Test-Path $dest) -and -not $script:Force) {
        $sizeMb = [math]::Round((Get-Item $dest).Length / 1MB, 1)
        Write-ZoombieLog -Level Info -Message "model present: $fileName ($sizeMb MB)"
        return @{ Path = $dest; Name = $fileName; SizeMb = $sizeMb }
    }
    $url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$fileName"
    if (-not (Test-WriteAllowed)) {
        Write-ZoombieLog -Level Step -Message "would download model $fileName"
        return @{ Path = $dest; Name = $fileName; SizeMb = 0 }
    }
    New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null
    Invoke-ZoombieDownload -Url $url -OutFile $dest
    $sizeMb = [math]::Round((Get-Item $dest).Length / 1MB, 1)
    if ($sizeMb -lt 1) { throw "Downloaded model looks too small ($sizeMb MB): $dest" }
    $script:Changes.Add("installed model $fileName")
    return @{ Path = $dest; Name = $fileName; SizeMb = $sizeMb }
}

# ---------------------------------------------------------------------------
# CLI + skill deployment
# ---------------------------------------------------------------------------

function Install-ZoombieCli {
    <#
    .SYNOPSIS
        Copy the CLI (scripts + lib + pdf helper) to a stable ASCII location.

    .DESCRIPTION
        Skills invoke the CLI by an absolute, stable path so that they work from
        any project and never depend on PATH or on where this repo was cloned.
        $PSScriptRoot-relative resolution in zoombie.ps1 keeps lib and pdf
        discovery working because the layout (zoombie.ps1 + lib\ + pdf\) is preserved.
    #>
    [CmdletBinding()]
    param()
    $destDir = Get-ZoombieEnvPath -Child 'bin', 'zoombie'
    $destCli = Join-Path $destDir 'zoombie.ps1'
    $destLib = Join-Path $destDir 'lib\ZoombieEnv.psm1'
    $srcCli  = Join-Path $PSScriptRoot 'zoombie.ps1'
    $srcLib  = Join-Path $PSScriptRoot 'lib\ZoombieEnv.psm1'
    # The PDF -> Markdown helper lives beside the CLI so Invoke-ReadPdf can
    # resolve it relative to $PSScriptRoot in both the repo and installed layouts.
    $srcPdf  = Join-Path $PSScriptRoot 'pdf\extract_pdf.py'

    if (-not (Test-WriteAllowed)) {
        Write-ZoombieLog -Level Step -Message "would install CLI -> $destDir"
        return $destCli
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $destDir 'lib') | Out-Null
    Copy-Item -LiteralPath $srcCli -Destination $destCli -Force
    Copy-Item -LiteralPath $srcLib -Destination $destLib -Force
    if (Test-Path -LiteralPath $srcPdf) {
        $destPdfDir = Join-Path $destDir 'pdf'
        New-Item -ItemType Directory -Force -Path $destPdfDir | Out-Null
        Copy-Item -LiteralPath $srcPdf -Destination (Join-Path $destPdfDir 'extract_pdf.py') -Force
    }
    Write-ZoombieLog -Level Info -Message "CLI installed: $destCli"
    return $destCli
}

function Install-PdfToolchain {
    <#
    .SYNOPSIS
        Ensure the PDF -> Markdown Python dependencies are present.

    .DESCRIPTION
        PyMuPDF4LLM does the Markdown conversion; pytesseract drives the optional
        OCR fallback. Python is already a prerequisite of this repo, so the
        dependencies are installed into that interpreter rather than a second,
        duplicated one.

        pip runs with --user so installation never needs elevation and never
        writes into a protected location. It is also a cheap no-op on re-runs
        (pip resolves the already-satisfied requirements and downloads nothing).

        Tesseract is a separate, optional system tool; it is detected (never
        installed) so the report can state whether OCR will work.

        Returns a hashtable describing the result for the manifest:
            @{ Ok; Python; PythonVersion; RequirementsVersion; Tesseract; TesseractVersion; Note }
        A missing Python or a failed pip is not fatal - the transcription
        pipeline does not depend on it - so this reports Ok=$false with a clear
        reason instead of throwing.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowNull()][string]$Python)

    $result = @{
        Ok                  = $false
        Python              = $Python
        PythonVersion       = $null
        RequirementsVersion = $null
        Tesseract           = $null
        TesseractVersion    = $null
        RemovedShims        = 0
        Note                = $null
    }

    # Tesseract is optional and system-wide; detect it either way so the report
    # can say whether OCR will actually work.
    $tess = Resolve-ZoombieTool -Name 'tesseract' -Candidates @(
        (Join-Path ${env:ProgramFiles} 'Tesseract-OCR\tesseract.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Tesseract-OCR\tesseract.exe')
    )
    if ($tess) {
        $result.Tesseract = $tess
        $result.TesseractVersion = Get-ZoombieToolVersion -Exe $tess -VersionArgs '--version'
    }

    if (-not $Python) {
        $result.Note = 'Python not found. Install with: winget install Python.Python.3.12'
        Write-ZoombieLog -Level Warn -Message "PDF toolchain skipped: $($result.Note)"
        return $result
    }
    $result.PythonVersion = Get-ZoombieToolVersion -Exe $Python -VersionArgs '--version'
    $result.RequirementsVersion = $result.PythonVersion

    $reqSrc = Join-Path $PSScriptRoot 'requirements-pdf.txt'
    if (-not (Test-Path -LiteralPath $reqSrc)) {
        $result.Note = 'requirements-pdf.txt not found next to setup-worker.ps1'
        Write-ZoombieLog -Level Warn -Message "PDF toolchain skipped: $($result.Note)"
        return $result
    }

    if (-not (Test-WriteAllowed)) {
        Write-ZoombieLog -Level Step -Message "would pip install -r requirements-pdf.txt into $Python"
        $result.Ok = $true
        return $result
    }

    # The helper imports libraries directly by absolute interpreter path, so no
    # console entry-point shim is ever needed, yet pip still drops them into
    # %APPDATA%\Python\<ver>\Scripts (not on PATH). Snapshot the directory before
    # the install and remove only what THIS install created, so a pre-existing
    # unrelated tool (e.g. yt-dlp.exe) is never touched.
    $before = Get-ZoombiePipShimSnapshot

    Write-ZoombieLog -Level Step -Message "installing Python PDF dependencies (pip --user)"
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & $Python -m pip install --user --no-warn-script-location --disable-pip-version-check --quiet -r $reqSrc 2>$null
        $pipExit = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prevEap }
    if ($pipExit -ne 0) {
        $result.Note = "pip install failed (exit $pipExit)"
        Write-ZoombieLog -Level Warn -Message "PDF toolchain: $($result.Note)"
        return $result
    }

    $removedShims = Remove-ZoombieNewPipShims -Before $before
    if ($removedShims -gt 0) {
        Write-ZoombieLog -Level Info -Message "removed $removedShims unused pip entry-point shim(s)"
    }
    $result.RemovedShims = $removedShims

    # Confirm the imports actually resolve, so a partial install is reported as a
    # failure here rather than surfacing later as an opaque readpdf error.
    # No quotes in the snippet: PowerShell strips inner double quotes when
    # passing a -c argument to a native exe, which would turn print("ok") into
    # print(ok) and raise a NameError. The exit code is the signal, not stdout.
    $probe = 'import pymupdf4llm, pymupdf'
    $ErrorActionPreference = 'Continue'
    try {
        & $Python -c $probe 2>$null | Out-Null
        $probeExit = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prevEap }
    if ($probeExit -ne 0) {
        $result.Note = 'PDF dependencies installed but not importable'
        Write-ZoombieLog -Level Warn -Message "PDF toolchain: $($result.Note)"
        return $result
    }

    $result.Ok = $true
    $script:Changes.Add('installed PDF python dependencies')
    return $result
}

function Install-ZoombieSkills {
    <#
    .SYNOPSIS
        Deploy skills\<name>\SKILL.md into the global Zoo skills root.

    .DESCRIPTION
        Global root is the ABSOLUTE path %USERPROFILE%\.roo\skills (never a
        relative ..\.. path).

        Every skill is namespaced `zoombie-*`, so a name can never collide with a
        foreign skill; deployment simply overwrites. The version compare only
        decides whether to report `up to date` or `updated`.
    #>
    [CmdletBinding()]
    param()
    $skillsRoot = Join-Path $env:USERPROFILE '.roo\skills'
    $srcRoot = Join-Path (Split-Path $PSScriptRoot -Parent) 'skills'
    $mine = Get-ZoombieSkillVersion
    $results = New-Object System.Collections.Generic.List[object]

    if (-not (Test-Path -LiteralPath $srcRoot)) {
        Write-ZoombieLog -Level Warn -Message "no skills\ folder in the repo; skipping skill deployment"
        return $results
    }

    foreach ($dir in Get-ChildItem -LiteralPath $srcRoot -Directory) {
        $name = $dir.Name
        $src = Join-Path $dir.FullName 'SKILL.md'
        if (-not (Test-Path -LiteralPath $src)) { continue }
        $dest = Join-Path $skillsRoot "$name\SKILL.md"

        $action = 'created'
        if (Test-Path -LiteralPath $dest) {
            $marker = Get-ZoombieSkillMarker -Path $dest
            if ($marker.Version -eq $mine) {
                $action = 'up to date'
            } elseif ($marker.Version) {
                $action = "updated ($($marker.Version) -> $mine)"
            } else {
                $action = 'updated (no version -> ' + $mine + ')'
            }
        }

        if ($action -ne 'up to date') {
            if (Test-WriteAllowed) {
                New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
                Copy-Item -LiteralPath $src -Destination $dest -Force
            }
        }
        $results.Add([pscustomobject]@{ skill = $name; action = $action; path = $dest })
    }

    $deployed = $results.Count
    if (-not (Test-WriteAllowed)) {
        Write-ZoombieLog -Level Step -Message "would deploy $deployed skills -> $skillsRoot"
    } else {
        Write-ZoombieLog -Level Info -Message "skills deployed -> $skillsRoot"
    }
    return $results
}

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

$mode = if ($script:Check) { 'CHECK' } elseif ($script:DryRun) { 'DRY-RUN' } else { 'APPLY' }
$root = Get-ZoombieEnvRoot

Write-ZoombieLog -Level Step -Message "zoombie setup [$mode]"
Write-ZoombieLog -Level Info -Message "toolchain root: $root"

# The root must be ASCII because whisper.cpp breaks on non-ASCII paths. When the
# user name is not ASCII (e.g. C:\Users\Мария) and no explicit -Root was given,
# Get-ZoombieEnvRoot already falls back to %PUBLIC%\zoombie-env. Guard the two
# remaining cases: a bad explicit override, or no ASCII location at all.
if (-not (Test-ZoombieAsciiPath $root)) {
    if (Test-ZoombieRootIsDefault) {
        $asciiFallback = Join-Path $env:PUBLIC 'zoombie-env'
        if ($env:PUBLIC -and (Test-ZoombieAsciiPath $asciiFallback)) {
            $env:ZOOMBIE_ENV_ROOT = $asciiFallback
            $root = $asciiFallback
            Write-ZoombieLog -Level Warn -Message "%USERPROFILE% is not ASCII; using $root instead"
        } else {
            throw "No ASCII toolchain root available. Pass an explicit -Root on an ASCII path (Cyrillic paths break whisper.cpp)."
        }
    } else {
        throw "The -Root you passed is not ASCII (Cyrillic paths break whisper.cpp): $root"
    }
}

# NOTE: PowerShell variable names are case-insensitive, so these locals must NOT
# collide with the -Model parameter. $whisperInfo / $modelInfo are the results.
$python       = $null
$whisperInfo  = $null
$modelInfo    = $null
$hw           = $null
$cliPath      = $null
$skillResults = @()
$pdfInfo      = $null

Write-ZoombieLog -Level Step -Message "PowerShell $($PSVersionTable.PSVersion) on $([System.Environment]::OSVersion.VersionString)"

$manifest = Get-ZoombieEnvManifest
if (-not $manifest) { $manifest = [ordered]@{} }

if (Test-WriteAllowed) { New-ZoombieEnvSkeleton | Out-Null }

# 1. Hardware probe (needed for whisper backend + model choice).
Write-ZoombieLog -Level Step -Message "probing hardware"
$hw = Get-HardwareProfile
Write-ZoombieLog -Level Info -Message "cpu=$($hw.cpuName) cores=$($hw.cpuCores)/$($hw.cpuThreads) ram=$($hw.ramGb)GB"
Write-ZoombieLog -Level Info -Message "gpu=$($hw.gpus -join ', ') nvidia=$($hw.nvidia) vramMb=$($hw.vramMb) backend=$($hw.backend)"

# 2. Python (tooling only; whisper.cpp does not need it, but the prompt requests it).
Write-ZoombieLog -Level Step -Message "checking Python"
Update-ZoombiePath | Out-Null
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd -and -not (Test-ZoombieWindowsStoreStub $pyCmd.Source)) {
    $python = $pyCmd.Source
} else {
    $candidates = @()
    foreach ($v in @('313', '312', '311', '310')) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe")
    }
    $python = Resolve-ZoombieTool -Name 'python' -Candidates $candidates
    if ($python -and (Test-ZoombieWindowsStoreStub $python)) { $python = $null }
}
if ($python) {
    Write-ZoombieLog -Level Info -Message "python: $python ($(Get-ZoombieToolVersion -Exe $python -VersionArgs '--version'))"
} else {
    Write-ZoombieLog -Level Warn -Message "python not found. Optional for this pipeline. Install with: winget install Python.Python.3.12"
}

# 3. ffmpeg + ffprobe into zoombie-env\bin.
Write-ZoombieLog -Level Step -Message "ensuring ffmpeg/ffprobe"
Install-FFmpeg -BinDir (Get-ZoombieEnvPath -Child 'bin') | Out-Null

# 4. yt-dlp as a Python package (invoked via `python -m yt_dlp`).
Write-ZoombieLog -Level Step -Message "ensuring yt-dlp"
$ytDlpInfo = Install-YtDlp -Python $python

# 5. whisper.cpp (backend-aware) into zoombie-env\bin\whisper.
Write-ZoombieLog -Level Step -Message "ensuring whisper.cpp"
$whisperInfo = Install-Whisper -Backend $hw.backend

# 6. Model.
if (-not $Model) { $Model = Get-RecommendedModel -Hw $hw }
Write-ZoombieLog -Level Step -Message "ensuring whisper model '$Model'"
$modelInfo = Install-WhisperModel -ModelName $Model

# 6b. CLI + skills.
Write-ZoombieLog -Level Step -Message "installing CLI to zoombie-env\bin\zoombie"
$cliPath = Install-ZoombieCli
Write-ZoombieLog -Level Step -Message "deploying skills to the global root"
$skillResults = Install-ZoombieSkills

# 6c. PDF -> Markdown toolchain (Python deps + optional Tesseract detection).
Write-ZoombieLog -Level Step -Message "ensuring PDF -> Markdown toolchain"
$pdfInfo = Install-PdfToolchain -Python $python
if ($pdfInfo.Tesseract) {
    Write-ZoombieLog -Level Info -Message "tesseract present: $($pdfInfo.Tesseract)"
} else {
    Write-ZoombieLog -Level Info -Message "tesseract not found (optional; OCR fallback unavailable). Install with: winget install UB-Mannheim.TesseractOCR"
}

# 7. Persist the manifest.
# Built incrementally rather than as one big literal: under StrictMode on
# Windows PowerShell 5.1, a single bad member reference inside a large
# [ordered]@{} literal aborts the whole statement with a vague error line.
$envRoot     = Get-ZoombieEnvRoot
$ffmpegPath  = Join-Path $envRoot 'bin\ffmpeg.exe'
$ffprobePath = Join-Path $envRoot 'bin\ffprobe.exe'

$pythonVersion = $null
if ($python) { $pythonVersion = Get-ZoombieToolVersion -Exe $python -VersionArgs '--version' }
$ffmpegVersion  = Get-ZoombieToolVersion -Exe $ffmpegPath  -VersionArgs '-version'
$ffprobeVersion = Get-ZoombieToolVersion -Exe $ffprobePath -VersionArgs '-version'
$ytdlpVersion   = $null
if ($python) { $ytdlpVersion = Get-ZoombieToolVersion -Exe $python -VersionArgs @('-m', 'yt_dlp', '--version') }

# Read result fields defensively (Get-Field lives with the small helpers above),
# so a missing key can never abort the run under StrictMode.

$manifest = [ordered]@{}
$manifest['zoombieVersion'] = Get-ZoombieSkillVersion
$manifest['updatedUtc']     = (Get-Date).ToUniversalTime().ToString('o')
$manifest['root']           = $root
$manifest['asciiRoot']      = (Test-ZoombieAsciiPath $root)
$manifest['python']         = [ordered]@{ path = $python; version = $pythonVersion }
$manifest['ffmpeg']         = [ordered]@{ path = $ffmpegPath;  version = $ffmpegVersion }
$manifest['ffprobe']        = [ordered]@{ path = $ffprobePath; version = $ffprobeVersion }
$manifest['ytDlp']          = [ordered]@{ via = 'python -m yt_dlp'; python = $python; version = $ytdlpVersion; ok = (Get-Field $ytDlpInfo 'Ok' $false); note = Get-Field $ytDlpInfo 'Note' }
$manifest['whisper']        = [ordered]@{
    path    = Get-Field $whisperInfo 'Exe'
    tag     = Get-Field $whisperInfo 'Tag'
    asset   = Get-Field $whisperInfo 'Asset'
    backend = Get-Field $whisperInfo 'Backend'
}
$manifest['model']          = [ordered]@{
    path   = Get-Field $modelInfo 'Path'
    name   = Get-Field $modelInfo 'Name'
    sizeMb = Get-Field $modelInfo 'SizeMb' 0
}
$manifest['pdf']            = [ordered]@{
    python           = Get-Field $pdfInfo 'Python'
    pythonVersion    = Get-Field $pdfInfo 'PythonVersion'
    ok               = Get-Field $pdfInfo 'Ok' $false
    tesseract        = Get-Field $pdfInfo 'Tesseract'
    tesseractVersion = Get-Field $pdfInfo 'TesseractVersion'
    note             = Get-Field $pdfInfo 'Note'
}
$manifest['hardware']       = $hw
if (Test-WriteAllowed) {
    Save-ZoombieEnvManifest -Manifest $manifest | Out-Null
    Write-ZoombieLog -Level Info -Message "manifest written: $(Get-ZoombieEnvPath -Child 'env.json')"
}

$missing = @()
if (-not (Test-Path (Get-ZoombieEnvPath -Child 'bin','ffmpeg.exe')))  { $missing += 'ffmpeg' }
if (-not (Test-Path (Get-ZoombieEnvPath -Child 'bin','ffprobe.exe'))) { $missing += 'ffprobe' }
if (-not $ytdlpVersion) { $missing += 'yt-dlp' }
if (-not (Test-Path (Get-ZoombieEnvPath -Child 'bin','whisper','whisper-cli.exe'))) { $missing += 'whisper-cli' }
if (-not (Test-Path (Get-ZoombieEnvPath -Child 'models')))            { $missing += 'models' }
# PDF tooling is optional for the transcription pipeline, so it is reported as
# missing only in CHECK mode (which is purely informational and writes nothing).
if ($script:Check -and -not (Get-Field $pdfInfo 'Ok' $false))     { $missing += 'pdf-deps' }
if ($script:Check -and -not (Get-Field $pdfInfo 'Tesseract'))     { $missing += 'tesseract (optional)' }

if ($script:Check) {
    Write-ZoombieLog -Level Step -Message "CHECK complete. Missing: $(if ($missing) { $missing -join ', ' } else { 'none' })"
}

# In CHECK/DRY-RUN, "missing" simply means "not installed yet" and is expected.
$ok = ($missing.Count -eq 0) -or ($script:Check) -or ($script:DryRun)
$resultData = [ordered]@{
    mode     = $mode
    root     = $root
    cli      = $cliPath
    skills   = @($skillResults)
    missing  = $missing
    changes  = @($script:Changes)
    manifest = $manifest
}
Write-ZoombieResult -Action 'setup' -Ok $ok -Data $resultData -ErrorMessage $(if ($missing.Count -gt 0 -and -not $script:Check) { "Missing after setup: $($missing -join ', ')" } else { $null })

# Explicit process exit so the thin bootstrap (and CI) can chain on the result.
if ($ok) { exit 0 } else { exit 1 }

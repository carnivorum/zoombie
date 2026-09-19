#Requires -Version 5.1
<#
.SYNOPSIS
    Shared helpers for the zoombie toolchain (setup.ps1 and zoombie.ps1).

.DESCRIPTION
    Central place for the environment rules that used to live as prose in setup.md:
      * where the ASCII toolchain root lives (the Cyrillic-path fix)
      * PATH refresh from the registry (kills stale-PATH false negatives)
      * absolute-path tool resolution (no reliance on install locations)
      * ASCII-path guards for whisper.cpp
      * the env.json manifest read/write
      * a single JSON result emitter so callers parse one shape

    Every function here is deterministic and side-effect free except the ones that
    explicitly create directories or write the manifest.
#>

Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Marker written into every SKILL.md we own. Bump when skill content changes.
# 3.3.0: the GPU-fits policy (fail when a usable GPU exists but was not used),
# probe-unavailable handled as unknown rather than cpu, and capability-probed
# flash attention / thread flags all change reported behaviour.
$script:ZoombieSkillVersion = '3.3.0'

# Marker key written into SKILL.md front matter to prove ownership.
$script:ZoombieMarkerKey = 'cvrm-zoombie-version'

# Preferred install root. It MUST be ASCII, so it is chosen dynamically:
# if %USERPROFILE% is ASCII we use it, otherwise (e.g. a Cyrillic user name) we
# fall back to a machine-level ASCII location. Override with $env:ZOOMBIE_ENV_ROOT.
$script:ZoombieRootFolderName = 'zoombie-env'

# ---------------------------------------------------------------------------
# Root + manifest
# ---------------------------------------------------------------------------

function Get-ZoombieEnvRoot {
    <#
    .SYNOPSIS
        Absolute path of the toolchain root. Always ASCII by construction.

    .DESCRIPTION
        whisper.cpp breaks on non-ASCII paths, so the root must be ASCII. This
        picks the first usable ASCII location:

          1. $env:ZOOMBIE_ENV_ROOT      (explicit override, used verbatim)
          2. %USERPROFILE%\zoombie-env  (when the profile path is ASCII)
          3. %PUBLIC%\zoombie-env       (machine-level, ASCII by definition;
                                         used when the user name is not ASCII,
                                         e.g. C:\Users\Мария)
          4. <SystemDrive>\zoombie-env  (last resort)

        %PUBLIC% exists on every Windows install, is writable without elevation,
        and is always ASCII, which is why it is the fallback.
    #>
    [CmdletBinding()]
    param()
    if ($env:ZOOMBIE_ENV_ROOT -and $env:ZOOMBIE_ENV_ROOT.Trim()) {
        return $env:ZOOMBIE_ENV_ROOT.Trim()
    }

    $fromProfile = Join-Path $env:USERPROFILE $script:ZoombieRootFolderName
    if (Test-ZoombieAsciiPath $fromProfile) { return $fromProfile }

    if ($env:PUBLIC) {
        $fromPublic = Join-Path $env:PUBLIC $script:ZoombieRootFolderName
        if (Test-ZoombieAsciiPath $fromPublic) { return $fromPublic }
    }

    if ($env:SystemDrive) {
        return (Join-Path ($env:SystemDrive + '\') $script:ZoombieRootFolderName)
    }
    return $fromProfile
}

function Test-ZoombieRootIsDefault {
    <#
    .SYNOPSIS
        $true when the root is not an explicit override (safe to report/choose).
    #>
    [CmdletBinding()]
    param()
    return (-not ($env:ZOOMBIE_ENV_ROOT -and $env:ZOOMBIE_ENV_ROOT.Trim()))
}

function Get-ZoombieRootCandidates {
    <#
    .SYNOPSIS
        All ASCII roots a client (e.g. a skill) should probe, in priority order.

    .DESCRIPTION
        Lets a caller find an existing install without knowing which root the
        setup chose. Includes the two user-visible locations plus an explicit
        override, if any.
    #>
    [CmdletBinding()]
    param()
    $list = New-Object System.Collections.Generic.List[string]
    if ($env:ZOOMBIE_ENV_ROOT -and $env:ZOOMBIE_ENV_ROOT.Trim()) {
        $list.Add($env:ZOOMBIE_ENV_ROOT.Trim())
    }
    if ($env:USERPROFILE) { $list.Add((Join-Path $env:USERPROFILE $script:ZoombieRootFolderName)) }
    if ($env:PUBLIC)      { $list.Add((Join-Path $env:PUBLIC $script:ZoombieRootFolderName)) }
    return $list
}

function Get-ZoombieSkillVersion {
    [CmdletBinding()]
    param()
    return $script:ZoombieSkillVersion
}

function Get-ZoombieEnvPath {
    <#
    .SYNOPSIS
        Join one or more child segments onto the toolchain root.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Child)
    $root = Get-ZoombieEnvRoot
    foreach ($segment in $Child) {
        $root = Join-Path $root $segment
    }
    return $root
}

function Get-ZoombieEnvManifest {
    <#
    .SYNOPSIS
        Read env.json, or $null when it does not exist / cannot be parsed.
    #>
    [CmdletBinding()]
    param()
    $path = Get-ZoombieEnvPath -Child 'env.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Save-ZoombieEnvManifest {
    <#
    .SYNOPSIS
        Write env.json as UTF-8 WITHOUT BOM (so ConvertFrom-Json round-trips cleanly).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Manifest)
    $root = Get-ZoombieEnvRoot
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $path = Join-Path $root 'env.json'
    $json = $Manifest | ConvertTo-Json -Depth 12
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $json, $utf8NoBom)
    return $path
}

function New-ZoombieEnvSkeleton {
    <#
    .SYNOPSIS
        Create the zoombie-env directory tree (bin, bin\whisper, models, work).
    #>
    [CmdletBinding()]
    param()
    $dirs = @(
        (Get-ZoombieEnvPath -Child 'bin'),
        (Get-ZoombieEnvPath -Child 'bin', 'whisper'),
        (Get-ZoombieEnvPath -Child 'models'),
        (Get-ZoombieEnvPath -Child 'work')
    )
    foreach ($d in $dirs) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
    return $dirs
}

# ---------------------------------------------------------------------------
# PATH hygiene
# ---------------------------------------------------------------------------

function Update-ZoombiePath {
    <#
    .SYNOPSIS
        Rebuild $env:Path from the Machine + User registry values.

    .DESCRIPTION
        A fresh install (winget, MSI) updates the registry PATH but NOT the environment
        of an already-running shell. Refreshing here is what turns a stale-PATH
        "tool not found" false negative into a correct positive.
    #>
    [CmdletBinding()]
    param()
    $machine = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($machine) { $parts += $machine }
    if ($user)    { $parts += $user }
    if ($parts.Count -gt 0) {
        $env:Path = ($parts -join ';')
    }
    return $env:Path
}

# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------

function Resolve-ZoombieTool {
    <#
    .SYNOPSIS
        Resolve a tool to an absolute path, or $null.

    .DESCRIPTION
        Resolution order (first hit wins):
          1. explicit candidate paths (e.g. from env.json)
          2. the toolchain bin directories (no PATH needed at all)
          3. Get-Command by name, after refreshing PATH from the registry

        This is the "never rely on install paths" rule: callers use the returned
        absolute path and never assume a tool is on PATH.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Name,
        [string[]]$Candidates = @()
    )

    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $binNames = @("$Name.exe", $Name)
    $binDirs = @(
        (Get-ZoombieEnvPath -Child 'bin'),
        (Get-ZoombieEnvPath -Child 'bin', 'whisper')
    )
    foreach ($dir in $binDirs) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        foreach ($binName in $binNames) {
            $p = Join-Path $dir $binName
            if (Test-Path -LiteralPath $p -PathType Leaf) {
                return (Resolve-Path -LiteralPath $p).Path
            }
        }
    }

    Update-ZoombiePath | Out-Null
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }
    return $null
}

function Get-ZoombiePython {
    <#
    .SYNOPSIS
        Absolute path of the Python interpreter this toolchain uses, or $null.

    .DESCRIPTION
        Python is a prerequisite of this repo and is used for more than one job:
        the PDF -> Markdown helper (pymupdf4llm/pytesseract) and yt-dlp (invoked
        as `python -m yt_dlp`). The dependencies for both are installed into this
        one interpreter by setup-worker.ps1.

        yt-dlp is pure Python, so - unlike whisper.cpp, which is native C++ and
        breaks on non-ASCII paths - it is ASCII-path safe and needs no isolation.

        Resolution order (first hit wins), so a stale PATH can never cause a
        false negative:
          1. python.path recorded in env.json (exactly what setup installed into)
          2. the toolchain bin dirs
          3. Get-Command python, after refreshing PATH, skipping the Microsoft
             Store alias stub (which is not a real interpreter)
    #>
    [CmdletBinding()]
    param()
    $manifest = Get-ZoombieEnvManifest
    $fromManifest = $null
    if ($manifest) {
        $prop = $manifest.PSObject.Properties['python']
        if ($prop -and $prop.Value) {
            $fromManifest = $prop.Value.path
        }
    }
    if ($fromManifest -and (Test-Path -LiteralPath $fromManifest -PathType Leaf)) {
        return (Resolve-Path -LiteralPath $fromManifest).Path
    }

    $resolved = Resolve-ZoombieTool -Name 'python'
    if ($resolved -and -not (Test-ZoombieWindowsStoreStub $resolved)) {
        return $resolved
    }
    return $null
}

function Get-ZoombiePipScriptDirs {
    <#
    .SYNOPSIS
        Directories where pip may drop console entry-point shims for a --user install.

    .DESCRIPTION
        `pip install --user` can write small console launchers (e.g. pymupdf.exe)
        into %APPDATA%\Python\<ver>\Scripts. Nothing in this toolchain calls them
        (helpers import the libraries directly by absolute interpreter path), so
        they are pure clutter. The directory is version-tagged, so it is
        enumerated rather than assumed.
    #>
    [CmdletBinding()]
    param()
    $dirs = New-Object System.Collections.Generic.List[string]
    $pyRoot = Join-Path $env:APPDATA 'Python'
    if ($env:APPDATA -and (Test-Path -LiteralPath $pyRoot)) {
        foreach ($child in @(Get-ChildItem -LiteralPath $pyRoot -Directory -ErrorAction SilentlyContinue)) {
            $scripts = Join-Path $child.FullName 'Scripts'
            if (Test-Path -LiteralPath $scripts) { $dirs.Add($scripts) }
        }
    }
    return $dirs
}

function Get-ZoombiePipShimSnapshot {
    <#
    .SYNOPSIS
        Snapshot of the file names present in every pip script directory.

    .DESCRIPTION
        Used before a pip install so the caller can tell exactly which shims that
        install created, and therefore remove only those. Anything already there
        (a foreign tool such as yt-dlp.exe) is preserved by construction.
    #>
    [CmdletBinding()]
    param()
    $snapshot = @{}
    foreach ($dir in Get-ZoombiePipScriptDirs) {
        $snapshot[$dir] = @(Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Name)
    }
    return $snapshot
}

function Remove-ZoombieNewPipShims {
    <#
    .SYNOPSIS
        Remove only the pip shims created since a previous snapshot.

    .DESCRIPTION
        Deletion is best-effort: a locked file yields a warning, never an error,
        so a stray shim can never fail the install.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][hashtable]$Before)

    $removed = 0
    foreach ($dir in Get-ZoombiePipScriptDirs) {
        $known = if ($Before.ContainsKey($dir)) { @($Before[$dir]) } else { @() }
        $candidates = @(Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
            Where-Object { $known -notcontains $_.Name })
        foreach ($file in $candidates) {
            try {
                Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
                $removed++
            }
            catch {
                Write-ZoombieLog -Level Warn -Message "could not remove pip shim (in use?): $($file.FullName)"
            }
        }
    }
    return $removed
}

function Get-ZoombieToolVersion {
    <#
    .SYNOPSIS
        Run a tool with version arguments and return the first output line, or $null.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$VersionArgs = @('--version')
    )
    if (-not (Test-Path -LiteralPath $Exe)) { return $null }
    try {
        $out = & $Exe @VersionArgs 2>&1
        if ($LASTEXITCODE -ne 0 -and -not $out) { return $null }
        $line = @($out) | Where-Object { "$_".Trim() } | Select-Object -First 1
        if ($line) { return "$line".Trim() }
        return $null
    }
    catch {
        return $null
    }
}

function Test-ZoombieWindowsStoreStub {
    <#
    .SYNOPSIS
        True when a python path is the Microsoft Store alias stub, not a real interpreter.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    return ($Path -match '\\WindowsApps\\')
}

# ---------------------------------------------------------------------------
# Skill ownership marker
# ---------------------------------------------------------------------------

function Get-ZoombieSkillMarker {
    <#
    .SYNOPSIS
        Read the ownership + version marker from a SKILL.md file.

    .OUTPUTS
        A hashtable: @{ Owned = [bool]; Version = [string] }
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    $result = @{ Owned = $false; Version = $null }
    if (-not (Test-Path -LiteralPath $Path)) { return $result }

    $head = Get-Content -LiteralPath $Path -TotalCount 12 -ErrorAction SilentlyContinue
    foreach ($line in $head) {
        if ($line -match "^\s*$([regex]::Escape($script:ZoombieMarkerKey))\s*:\s*(\S+)") {
            $result.Owned = $true
            $result.Version = $Matches[1]
            return $result
        }
    }
    return $result
}

# ---------------------------------------------------------------------------
# ASCII invariant (the whisper.cpp Cyrillic-path fix)
# ---------------------------------------------------------------------------

function Test-ZoombieAsciiPath {
    <#
    .SYNOPSIS
        $true when every character of the path is in the ASCII range (<= 127).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Path)
    if (-not $Path) { return $true }
    foreach ($ch in $Path.ToCharArray()) {
        if ([int][char]$ch -gt 127) { return $false }
    }
    return $true
}

function New-ZoombieAsciiWorkDir {
    <#
    .SYNOPSIS
        Create a fresh ASCII scratch directory under zoombie-env\work\<guid>.
    #>
    [CmdletBinding()]
    param()
    $dir = Get-ZoombieEnvPath -Child 'work', ([guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    return $dir
}

function New-ZoombieAsciiTempDir {
    <#
    .SYNOPSIS
        Create an ASCII temp directory for downloads/extraction.

    .DESCRIPTION
        %TEMP% lives under the user profile, so it is non-ASCII when the user
        name is (e.g. C:\Users\Мария\AppData\Local\Temp). Since some archived
        tools care about non-ASCII paths, scratch space is placed under the
        (ASCII) toolchain root instead, falling back to %TEMP% only if needed.
    #>
    [CmdletBinding()]
    param([string]$Prefix = 'tmp')
    $base = Join-Path (Get-ZoombieEnvRoot) 'tmp'
    if (-not (Test-ZoombieAsciiPath $base)) { $base = $env:TEMP }
    $dir = Join-Path $base ("$Prefix-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    return $dir
}

function Copy-ZoombieIntoSafeWork {
    <#
    .SYNOPSIS
        Copy an input file into an ASCII work dir under an ASCII name.

    .DESCRIPTION
        This is the structural fix for the whisper.cpp Cyrillic-path bug: whisper-cli
        only ever receives paths from this function's output, which are guaranteed
        ASCII. The original (possibly Cyrillic) file is never passed to whisper.

    .OUTPUTS
        A hashtable: @{ WorkDir; InputPath; OriginalPath }
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$InputPath,
        [string]$WorkDir
    )
    if (-not (Test-Path -LiteralPath $InputPath -PathType Leaf)) {
        throw "Input file not found: $InputPath"
    }
    if (-not $WorkDir) { $WorkDir = New-ZoombieAsciiWorkDir }
    if (-not (Test-ZoombieAsciiPath $WorkDir)) {
        throw "Work dir must be ASCII, got: $WorkDir"
    }

    $ext = [System.IO.Path]::GetExtension($InputPath)
    if (-not $ext) { $ext = '.bin' }
    $safeName = 'input' + $ext.ToLowerInvariant()
    $safePath = Join-Path $WorkDir $safeName
    Copy-Item -LiteralPath $InputPath -Destination $safePath -Force

    return @{
        WorkDir      = $WorkDir
        InputPath    = $safePath
        OriginalPath = (Resolve-Path -LiteralPath $InputPath).Path
    }
}

# ---------------------------------------------------------------------------
# whisper.cpp CUDA runtime + whisper log parsing
# ---------------------------------------------------------------------------

# The ggml-org prebuilt CUDA asset ships ggml-cuda.dll but NOT the cuBLAS
# runtime that ggml-cuda.dll loads on first use. Without it ggml_cuda_init
# cannot create a device, whisper.cpp then transcribes on the CPU and STILL
# exits 0, so nothing in the pipeline notices: env.json keeps saying `cuda`,
# doctor keeps reporting `cuda`, and a long file runs at roughly realtime.
#
# The matching redistributable is therefore provisioned from NVIDIA's own
# CUDA 11.8 redist manifest. That manifest is a single deterministic JSON
# document (one version + one sha256 per component), which is what makes this
# reproducible without installing the CUDA Toolkit: the archived GPU build,
# the manifest and the DLLs all agree on the 11.x cuBLAS ABI (repo hosts
# today's asset; NVIDIA redistributes 11.x forever, so `-Force` can refresh).
# The CUDA major version this table can actually satisfy. It is NOT a cosmetic
# pin: ggml-cuda.dll loads cuBLAS by major-versioned name (cublas64_11.dll), so
# an asset built against a different major needs a different runtime. Keying the
# runtime off the ASSET rather than hard-coding 11 is what stops a future
# "cublas64_12.dll" install from passing a "runtime present" check while the GPU
# silently never initialises.
$script:ZoombieCublasDefaultMajor = 11

# Per-major cuBLAS provisioning records. Each row is one NVIDIA redist archive
# with a published sha256, so "provisioning" never means "install the CUDA
# Toolkit", and adding a major means adding one verified row - nothing more.
# An asset whose major is absent here is refused at setup time (see
# Install-CublasRuntime) instead of being left without the DLLs it loads.
$script:ZoombieCublasProvisions = @{
    11 = [ordered]@{
        major   = 11
        version = '11.11.3.6'
        url     = 'https://developer.download.nvidia.com/compute/cuda/redist/libcublas/windows-x86_64/libcublas-windows-x86_64-11.11.3.6-archive.zip'
        sha256  = '67B0934A6359E4EE26FFF823C356021589D392C4FD49CA12624F570EDC08E2B9'
        dlls    = @('cublas64_11.dll', 'cublasLt64_11.dll')
    }
}

function Get-ZoombieCublasDefaultMajor {
    <#
    .SYNOPSIS
        The cuBLAS major version the provisioning table falls back to.
    #>
    [CmdletBinding()]
    param()
    return $script:ZoombieCublasDefaultMajor
}

function Get-ZoombieCublasProvision {
    <#
    .SYNOPSIS
        The deterministic cuBLAS runtime provisioning record (major, names,
        version, URL, sha256) so installers and reports agree on one source of
        truth.

    .DESCRIPTION
        Returns $null for a major that has no verified redist row pinned. That
        is deliberate: the caller must then refuse the install (naming the
        asset and the missing major) rather than download an unverified binary
        or leave ggml-cuda.dll without the runtime it loads.
    #>
    [CmdletBinding()]
    param([int]$CudaMajor = 0)
    if ($CudaMajor -le 0) { $CudaMajor = $script:ZoombieCublasDefaultMajor }
    if (-not $script:ZoombieCublasProvisions.ContainsKey($CudaMajor)) { return $null }
    return $script:ZoombieCublasProvisions[$CudaMajor]
}

function Get-ZoombieCudaMajorFromAssetName {
    <#
    .SYNOPSIS
        Extract the CUDA major version a whisper.cpp asset was built against.

    .DESCRIPTION
        ggml-org names its assets after the CUDA toolkit they were built with
        (e.g. `whisper-cublas-11.8.0-bin-x64.zip`). Parsing that name is how the
        installer knows which cuBLAS runtime the asset will load, instead of
        assuming 11 forever. Returns $null when the name carries no version, in
        which case the caller falls back to the default major.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowNull()][string]$Name)
    if (-not $Name) { return $null }
    if ($Name -match 'cublas-(\d+)[.\-_]') { return [int]$Matches[1] }
    if ($Name -match '\bcuda[-_ ]?(\d+)[.\-_]') { return [int]$Matches[1] }
    return $null
}

# Cache for the per-exe capability probe, so a single run reads --help once.
$script:ZoombieWhisperCapabilityCache = @{}

function Get-ZoombieWhisperCudaRuntime {
    <#
    .SYNOPSIS
        Report whether the CUDA build of whisper.cpp can actually initialise.

    .DESCRIPTION
        A CUDA build needs, side by side next to whisper-cli.exe:
          * ggml-cuda.dll (shipped in the asset), and
          * the cuBLAS runtime ggml-cuda.dll loads by MAJOR-versioned name
            (cublas64_<major>.dll / cublasLt64_<major>.dll), which the asset
            does NOT ship.

        The required major comes from the asset (see
        Get-ZoombieCudaMajorFromAssetName) or from whatever cublas64_*.dll is
        already present, so the check cannot be fooled by a runtime of the
        wrong major sitting in the folder.

        Three states are kept distinct, because conflating them is what made a
        broken CUDA install look healthy:
          * DirMissing  - there is no whisper folder (not installed), which is
                          NOT "all DLLs are missing";
          * no GpuModule - a CPU/Vulkan build, where cuBLAS is irrelevant;
          * Ready       - a CUDA build whose own runtime is complete.

        Sibling CUDA DLLs that are loaded but not provisioned here (cudart64_*)
        are reported in Warnings rather than Missing, so readiness still means
        "the buyer's DLLs we install", not "every possible DLL".

    .OUTPUTS
        A hashtable:
        @{ Exe; Dir; DirMissing; GpuModule; CublasMajor; Present; Missing;
           Warnings; Ready; ProvisionVersion }
    #>
    [CmdletBinding()]
    # NOT [Parameter(Mandatory)]: under PowerShell, a Mandatory [string] coerces a
    # $null argument to '' and then REJECTS it as an empty string, so a missing or
    # uninstalled toolchain produced an opaque parameter-binding error instead of
    # this function's own "not installed" result. AllowNull + AllowEmptyString
    # keeps the diagnostic meaningful when there is no whisper-cli.exe yet.
    param(
        [AllowNull()][AllowEmptyString()][string]$WhisperExe,
        [int]$CudaMajor = 0
    )

    $result = [ordered]@{
        Exe              = $WhisperExe
        Dir              = $null
        DirMissing       = $false
        GpuModule        = $false
        CublasMajor      = $null
        Present          = @()
        Missing          = @()
        Warnings         = @()
        Ready            = $false
        ProvisionVersion = $null
    }
    if (-not $WhisperExe) { return $result }

    $dir = Split-Path -Parent $WhisperExe
    $result.Dir = $dir
    if (-not (Test-Path -LiteralPath $dir)) {
        # No whisper folder at all means "not installed yet". Reporting every
        # cuBLAS DLL as missing here was a false alarm on CPU-only installs,
        # where the runtime was never relevant in the first place.
        $result.DirMissing = $true
        return $result
    }

    $result.GpuModule = Test-Path -LiteralPath (Join-Path $dir 'ggml-cuda.dll')
    # A CPU-only build has no CUDA module, so "everything is missing" would be a
    # false alarm; report the runtime state only for a CUDA build.
    if (-not $result.GpuModule) { return $result }

    # Required major: explicit wins, else infer from an existing cublas64_N.dll,
    # else the default. Inferring from what is present keeps a re-run honest
    # about the runtime the installed asset actually expects.
    $required = $CudaMajor
    if ($required -le 0) {
        $existing = @(Get-ChildItem -LiteralPath $dir -Filter 'cublas64_*.dll' -ErrorAction SilentlyContinue)
        if ($existing.Count -gt 0 -and $existing[0].Name -match 'cublas64_(\d+)\.dll') {
            $required = [int]$Matches[1]
        } else {
            $required = $script:ZoombieCublasDefaultMajor
        }
    }
    $result.CublasMajor = $required

    $spec = Get-ZoombieCublasProvision -CudaMajor $required
    if ($spec) { $result.ProvisionVersion = $spec.version }
    $names = if ($spec) { @($spec.dlls) } else { @("cublas64_$required.dll", "cublasLt64_$required.dll") }

    foreach ($dll in $names) {
        $dllPath = Join-Path $dir $dll
        if (Test-Path -LiteralPath $dllPath) { $result.Present += $dll }
        else { $result.Missing += $dll }
    }

    # Loaded but not provisioned by us: report, never block on it.
    foreach ($pattern in @('cudart64_*.dll', 'ggml-base.dll')) {
        if (-not @(Get-ChildItem -LiteralPath $dir -Filter $pattern -ErrorAction SilentlyContinue).Count) {
            $result.Warnings += "no $pattern beside whisper-cli.exe"
        }
    }

    $result.Ready = ($result.Missing.Count -eq 0)
    return $result
}

function Read-ZoombieWhisperLog {
    <#
    .SYNOPSIS
        Read a captured whisper.log as an array of lines (empty when unusable).

    .DESCRIPTION
        whisper.cpp writes its backend banner and whisper_print_timings block to
        STDERR, so the caller redirects stderr to a file and reads it here.
        Never throws: a missing or locked log yields an empty array, which the
        parsers below treat as "no information" rather than "CPU".
    #>
    [CmdletBinding()]
    # Not Mandatory for the same reason as Get-ZoombieWhisperCudaRuntime: an empty
    # path must yield an empty array, not a binding error.
    param([AllowNull()][AllowEmptyString()][string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return @() }
    try { return @(Get-Content -LiteralPath $Path -ErrorAction Stop) }
    catch { return @() }
}

function Get-ZoombieWhisperDeviceInfo {
    <#
    .SYNOPSIS
        Determine the device whisper ACTUALLY used from its captured log.

    .DESCRIPTION
        The signals are checked in a deliberate order, because the later ones
        are also present in the negative cases:

          1. `whisper_backend_init_gpu: using <NAME> backend` - the GPU backend
             was actually SELECTED for decoding. This is the only signal that
             proves the GPU was used.
          2. `use gpu = 0` - the caller disabled the GPU (`-ng`). This must be
             checked BEFORE the `loaded CUDA backend` signal, because `-ng` still
             loads the CUDA module and would otherwise be reported as a GPU run.
          3. `ggml_cuda_init: found <n> CUDA devices` / `load_backend: loaded
             CUDA backend` - the CUDA backend INITIALISED. This is what the
             `--help` capability probe shows (it initialises backends but never
             loads a model, so signal 1 never appears). It is capability, not
             usage, so it must never be reported as "the GPU was used": a
             successful exit whose only GPU signal is this one is still a silent
             CPU fallback.

        Device is therefore 'cuda'/'vulkan' ONLY when the device was actually
        selected. DeviceSelected and BackendInitialised expose the two facts
        separately, so a caller can require proof (transcribe) or accept mere
        capability (the --help probe). A Reason is always produced when the run
        did not demonstrably use a GPU.

    .OUTPUTS
        A hashtable: @{ Device; DeviceName; DeviceSelected; BackendInitialised; Reason }
    #>
    [CmdletBinding()]
    param([AllowNull()][string[]]$LogLines)

    $info = [ordered]@{
        Device             = 'cpu'
        DeviceName         = $null
        DeviceSelected     = $false
        BackendInitialised = $false
        Reason             = $null
    }
    $text = if ($LogLines) { ($LogLines -join "`n") } else { '' }
    if (-not $text.Trim()) {
        $info.Reason = 'whisper wrote no log output; the device could not be verified'
        return $info
    }

    # 1. The backend that was actually selected (definitive, real runs only).
    if ($text -match 'whisper_backend_init_gpu:\s+using\s+(\S+)\s+backend') {
        $info.DeviceName         = $Matches[1]
        $info.Device             = if ($Matches[1] -match 'Vulkan') { 'vulkan' } else { 'cuda' }
        $info.DeviceSelected     = $true
        $info.BackendInitialised = $true
        return $info
    }

    # 2. GPU explicitly disabled by the caller (-ng) - a deliberate CPU run.
    if ($text -match 'use gpu\s*=\s*0') {
        $info.Reason = 'GPU explicitly disabled for this run (-ng): use gpu = 0'
        $info.BackendInitialised = ($text -match 'load_backend:\s+loaded CUDA backend' -or
                                    $text -match 'ggml_cuda_init:\s*found \d+ CUDA devices' -or
                                    $text -match 'load_backend:\s+loaded Vulkan backend')
        return $info
    }

    # 3. A GPU backend initialised but NO device was selected: capability only,
    # not usage. Reported as cpu (the device that actually decoded), with the
    # distinction preserved in BackendInitialised.
    if ($text -match 'load_backend:\s+loaded CUDA backend' -or $text -match 'ggml_cuda_init:\s*found \d+ CUDA devices') {
        $info.DeviceName         = 'CUDA'
        $info.BackendInitialised = $true
        $info.Reason             = 'a CUDA backend initialised but no device was selected for decoding (silent CPU fallback)'
        return $info
    }
    if ($text -match 'load_backend:\s+loaded Vulkan backend') {
        $info.DeviceName         = 'Vulkan'
        $info.BackendInitialised = $true
        $info.Reason             = 'a Vulkan backend initialised but no device was selected for decoding (silent CPU fallback)'
        return $info
    }

    # 4. No GPU backend initialised even though one was expected: name the
    # observable cause so a silent CPU fallback is never just "slow".
    if ($text -match 'ggml_cuda_init:\s*no CUDA devices') {
        $info.Reason = 'ggml_cuda_init found no CUDA devices'
    }
    elseif ($text -match 'load_backend:\s+failed to load (?:ggml-cuda|CUDA)[^\r\n]*') {
        $info.Reason = "the CUDA backend failed to load: $($Matches[0].Trim())"
    }
    elseif ($text -match 'ggml_cuda_init[^\r\n]*error') {
        $info.Reason = 'ggml_cuda_init reported an error'
    }
    else {
        $info.Reason = 'no GPU backend was initialised (silent CPU fallback)'
    }
    return $info
}

function Get-ZoombieWhisperDevice {
    <#
    .SYNOPSIS
        Convenience wrapper: the device string ('cuda'|'vulkan'|'cpu') from a log.
    #>
    [CmdletBinding()]
    param([AllowNull()][string[]]$LogLines)
    return (Get-ZoombieWhisperDeviceInfo -LogLines $LogLines).Device
}

function Invoke-ZoombieWhisperBackendProbe {
    <#
    .SYNOPSIS
        Ask whisper-cli which device it can ACTUALLY initialise, without a file.

    .DESCRIPTION
        `env.json` records what was intended (`backend: cuda`), which is exactly
        why the missing-cuBLAS bug was invisible: setup, doctor and every report
        repeated the manifest while the run silently used the CPU.

        `whisper-cli --help` performs the full backend initialisation (it loads
        every ggml backend and reports CUDA devices) and then exits, so it proves
        the GPU is usable in about a second, before any model or audio is
        involved. It needs no model and no input.

        stderr is merged into the captured text rather than redirected to a file:
        -Check and -DryRun must leave the filesystem untouched, so the probe is
        deliberately file-less. The preference is relaxed for the native call
        because Windows PowerShell 5.1 turns native stderr into a terminating
        error under $ErrorActionPreference='Stop'.

    .OUTPUTS
        A hashtable: @{ Device; DeviceName; Reason; Log } - Device is $null when
        the probe could not run at all (e.g. whisper-cli.exe is absent).
    #>
    [CmdletBinding()]
    # See Get-ZoombieWhisperCudaRuntime: not Mandatory, so a $null/empty path is
    # reported as "not found" rather than raising a binding error.
    param([AllowNull()][AllowEmptyString()][string]$WhisperExe)

    $result = [ordered]@{ Device = $null; DeviceName = $null; Reason = $null; Log = @() }
    if (-not $WhisperExe -or -not (Test-Path -LiteralPath $WhisperExe -PathType Leaf)) {
        $result.Reason = 'whisper-cli.exe not found; the backend could not be probed'
        return $result
    }

    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $text = (& $WhisperExe '--help' 2>&1 | Out-String)
    }
    catch {
        $text = ''
    }
    finally { $ErrorActionPreference = $prevEap }

    $lines = @($text -split "`r?`n" | Where-Object { $_.Trim() })
    $result.Log = $lines

    # No captured output at all is "unknown", NOT "cpu". Returning 'cpu' here is
    # what made a build whose --help exits before backend init look like a failed
    # GPU machine, and turned the self-test into a false regression.
    if ($lines.Count -eq 0) {
        $result.Device = $null
        $result.Reason = 'whisper-cli --help produced no output; the backend could not be probed'
        return $result
    }

    $info = Get-ZoombieWhisperDeviceInfo -LogLines $lines
    # --help never loads a model, so the device-selected signal (1) does not
    # appear even on a healthy CUDA box. Capability is therefore taken from
    # BackendInitialised, while DeviceSelected stays reserved for real runs.
    if ($info.BackendInitialised -and -not $info.DeviceSelected) {
        $result.Device     = if ($info.DeviceName -match 'Vulkan') { 'vulkan' } else { 'cuda' }
        $result.DeviceName = $info.DeviceName
        $result.Reason     = "backend initialised ($($info.DeviceName)); reported as capability, not as a selected device"
        return $result
    }
    $result.Device = $info.Device
    $result.DeviceName = $info.DeviceName
    $result.Reason = $info.Reason
    return $result
}

function Get-ZoombieWhisperCapabilities {
    <#
    .SYNOPSIS
        Which optional whisper-cli flags the installed build advertises.

    .DESCRIPTION
        Flags such as flash attention (`-fa`), a thread count (`-t`) and the VAD
        options exist only in some builds, and passing an unknown flag aborts the
        run. `whisper-cli --help` lists exactly the flags the binary accepts, so
        it is read once here and cached per exe path. This is the "probe the
        binary, never assume" rule: a false negative merely omits a flag, while a
        false positive fails the run.

    .OUTPUTS
        A hashtable with a boolean per probed flag, plus Ok (the probe ran) and
        Help (the raw text, for reporting when Ok is false).
    #>
    [CmdletBinding()]
    # See Get-ZoombieWhisperCudaRuntime: not Mandatory, so an uninstalled
    # toolchain reports "no capabilities" instead of failing to bind.
    param([AllowNull()][AllowEmptyString()][string]$WhisperExe)

    $empty = [ordered]@{
        Checked = $false; Ok = $false; Raw = ''
        FlashAttention = $false; Threads = $false
        Vad = $false; VadModel = $false; BestOf = $false; NoFallback = $false
    }
    if (-not $WhisperExe -or -not (Test-Path -LiteralPath $WhisperExe -PathType Leaf)) {
        # The executable is absent, so there is nothing honest to report. All
        # flags stay false, which makes the caller omit them rather than guess.
        return $empty
    }

    if ($script:ZoombieWhisperCapabilityCache.ContainsKey($WhisperExe)) {
        return $script:ZoombieWhisperCapabilityCache[$WhisperExe]
    }

    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $raw = (& $WhisperExe '--help' 2>&1 | Out-String) }
    catch { $raw = '' }
    finally { $ErrorActionPreference = $prevEap }

    $caps = [ordered]@{
        Checked = $true; Ok = $false; Raw = ''
        FlashAttention = $false; Threads = $false
        Vad = $false; VadModel = $false; BestOf = $false; NoFallback = $false
    }
    if ($raw -and $raw.Trim()) {
        $caps.Ok = $true
        $caps.Raw = $raw
        if ($raw -match '(?m)^\s*-fa\b|--flash-attn\b')            { $caps.FlashAttention = $true }
        if ($raw -match '(?m)^\s*-t\b[^\r\n]*threads|--threads\b') { $caps.Threads = $true }
        if ($raw -match '--vad\b')                                  { $caps.Vad = $true }
        if ($raw -match '(?m)^\s*-vm\b|--vad-model\b')             { $caps.VadModel = $true }
        if ($raw -match '(?m)^\s*-bs\b|--beam-size\b|--best-of\b') { $caps.BestOf = $true }
        if ($raw -match '(?m)^\s*-nf\b|--no-fallback\b')           { $caps.NoFallback = $true }
    }

    $script:ZoombieWhisperCapabilityCache[$WhisperExe] = $caps
    return $caps
}

function Get-ZoombieWhisperTimings {
    <#
    .SYNOPSIS
        Parse the whisper_print_timings block into milliseconds.

    .DESCRIPTION
        These values were previously thrown away (stderr was sent to $null),
        which is why no realtime factor was ever reported. They are parsed with
        the invariant culture so a comma-decimal locale cannot turn "2.30 ms"
        into a parse failure.

    .OUTPUTS
        A hashtable: @{ LoadMs; TotalMs; EncodeMs; DecodeMs }
    #>
    [CmdletBinding()]
    param([AllowNull()][string[]]$LogLines)

    $result = [ordered]@{ LoadMs = $null; TotalMs = $null; EncodeMs = $null; DecodeMs = $null }
    if (-not $LogLines) { return $result }
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    foreach ($line in $LogLines) {
        if ($line -match 'whisper_print_timings:\s+load time\s*=\s*([0-9.]+)\s*ms') {
            $result.LoadMs = [double]::Parse($Matches[1], $invariant)
        }
        elseif ($line -match 'whisper_print_timings:\s+total time\s*=\s*([0-9.]+)\s*ms') {
            $result.TotalMs = [double]::Parse($Matches[1], $invariant)
        }
        elseif ($line -match 'whisper_print_timings:\s+encode time\s*=\s*([0-9.]+)\s*ms') {
            $result.EncodeMs = [double]::Parse($Matches[1], $invariant)
        }
        elseif ($line -match 'whisper_print_timings:\s+decode time\s*=\s*([0-9.]+)\s*ms') {
            $result.DecodeMs = [double]::Parse($Matches[1], $invariant)
        }
    }
    return $result
}

function Test-ZoombieWhisperGpuFailure {
    <#
    .SYNOPSIS
        Does a non-zero whisper exit LOOK like a GPU failure worth retrying?

    .DESCRIPTION
        The CPU retry restarts the WHOLE job, so it must only fire when the
        failure is plausibly the GPU's. The old code retried on ANY non-zero
        exit, which means a bad model path or an unsupported codec was silently
        re-run at CPU speed and the real cause was buried under the retry's own
        output.

        An EMPTY log with a non-zero exit is treated as retryable: there is no
        evidence either way, and refusing the retry there would turn a transient
        native crash into a hard failure. A log that shows a non-GPU error is
        not retried.

    .OUTPUTS
        A boolean.
    #>
    [CmdletBinding()]
    param(
        [int]$ExitCode,
        [AllowNull()][string[]]$LogLines
    )
    if ($ExitCode -eq 0) { return $false }

    $text = if ($LogLines) { ($LogLines -join "`n") } else { '' }
    # No captured output: cannot attribute the failure, so allow the retry
    # rather than convert an unexplained crash into a guaranteed failure.
    if (-not $text.Trim()) { return $true }

    if ($text -match 'gpu|ggml_backend_cuda|ggml_cuda_init|CUDA error|cudaError|CUDA_ERROR|out of memory|CUBLAS_STATUS|cublas|cublasLt|device-side|uncorrectable|invalid device|no CUDA devices|failed to load [^\r\n]*cuda|driver') {
        return $true
    }
    return $false
}

function Get-ZoombieCpuThreadCount {
    <#
    .SYNOPSIS
        A sensible default thread count for the CPU path, or $null.

    .DESCRIPTION
        Used for `whisper-cli -t` on a deliberate CPU run (and on the retry),
        which is exactly where the build's own default hurts most. The logical
        processor count is taken from the environment rather than a CIM query so
        this stays cheap and dependency-free; $null means "let whisper decide"
        rather than guessing a number the machine may not have.
    #>
    [CmdletBinding()]
    param()
    $n = 0
    if ($env:NUMBER_OF_PROCESSORS) { [void][int]::TryParse($env:NUMBER_OF_PROCESSORS, [ref]$n) }
    if ($n -gt 0) { return $n }
    return $null
}

# ---------------------------------------------------------------------------
# Result emitter
# ---------------------------------------------------------------------------

function Write-ZoombieResult {
    <#
    .SYNOPSIS
        Emit one machine-readable JSON result line.

    .DESCRIPTION
        Every zoombie.ps1 subcommand (and setup.ps1) emits exactly this shape so
        the calling agent never has to guess what happened:
            { ok, action, error, data, timestamp }
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Action,
        [bool]$Ok = $true,
        [AllowNull()]$Data = @{},
        [AllowNull()][string]$ErrorMessage
    )
    $obj = [ordered]@{
        ok        = $Ok
        action    = $Action
        error     = $ErrorMessage
        data      = $Data
        timestamp = (Get-Date).ToUniversalTime().ToString('o')
    }
    $json = $obj | ConvertTo-Json -Depth 12 -Compress
    Write-Output $json
    if (-not $Ok) { $global:LASTEXITCODE = 1 }
}

function Write-ZoombieLog {
    <#
    .SYNOPSIS
        Human-readable progress line (stderr), so JSON results stay parseable on stdout.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('Info', 'Warn', 'Error', 'Step')][string]$Level = 'Info'
    )
    $prefix = switch ($Level) {
        'Warn'  { 'WARN ' }
        'Error' { 'ERROR' }
        'Step'  { '==>'   }
        default { '    '  }
    }
    [Console]::Error.WriteLine("$prefix $Message")
}

# ---------------------------------------------------------------------------
# Console/encoding hardening (clean UTF-8 output for non-ASCII transcripts)
# ---------------------------------------------------------------------------

function Set-ZoombieUtf8Console {
    <#
    .SYNOPSIS
        Force UTF-8 so non-ASCII transcript text survives the console/file boundary.
    #>
    [CmdletBinding()]
    param()
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
    try { $OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
    $env:PYTHONUTF8 = '1'
}

Export-ModuleMember -Function @(
    'Get-ZoombieEnvRoot',
    'Test-ZoombieRootIsDefault',
    'Get-ZoombieRootCandidates',
    'Get-ZoombieSkillVersion',
    'Get-ZoombieEnvPath',
    'Get-ZoombieEnvManifest',
    'Save-ZoombieEnvManifest',
    'New-ZoombieEnvSkeleton',
    'Update-ZoombiePath',
    'Resolve-ZoombieTool',
    'Get-ZoombiePython',
    'Get-ZoombiePipScriptDirs',
    'Get-ZoombiePipShimSnapshot',
    'Remove-ZoombieNewPipShims',
    'Get-ZoombieToolVersion',
    'Test-ZoombieWindowsStoreStub',
    'Get-ZoombieSkillMarker',
    'Test-ZoombieAsciiPath',
    'New-ZoombieAsciiWorkDir',
    'New-ZoombieAsciiTempDir',
    'Copy-ZoombieIntoSafeWork',
    'Get-ZoombieCublasProvision',
    'Get-ZoombieCublasDefaultMajor',
    'Get-ZoombieCudaMajorFromAssetName',
    'Get-ZoombieWhisperCudaRuntime',
    'Read-ZoombieWhisperLog',
    'Get-ZoombieWhisperDeviceInfo',
    'Get-ZoombieWhisperDevice',
    'Invoke-ZoombieWhisperBackendProbe',
    'Get-ZoombieWhisperCapabilities',
    'Get-ZoombieWhisperTimings',
    'Test-ZoombieWhisperGpuFailure',
    'Get-ZoombieCpuThreadCount',
    'Write-ZoombieResult',
    'Write-ZoombieLog',
    'Set-ZoombieUtf8Console'
)

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
$script:ZoombieSkillVersion = '3.0.0'

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
    'Get-ZoombieToolVersion',
    'Test-ZoombieWindowsStoreStub',
    'Get-ZoombieSkillMarker',
    'Test-ZoombieAsciiPath',
    'New-ZoombieAsciiWorkDir',
    'New-ZoombieAsciiTempDir',
    'Copy-ZoombieIntoSafeWork',
    'Write-ZoombieResult',
    'Write-ZoombieLog',
    'Set-ZoombieUtf8Console'
)

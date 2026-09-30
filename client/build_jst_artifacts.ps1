[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Clean", "CleanRuntime", "WriteManifest", "CreateZip", "Publish", "WriteZipDigest")]
    [string]$Operation,
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$ManifestName = "SHA256SUMS.txt"
$MaxFileCount = 20000
$MaxEntryBytes = 512MB
$MaxTotalBytes = 1536MB

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
    }
}

$RootPath = (Resolve-Path -LiteralPath $SourceRoot).Path
$RootPrefix = $RootPath.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar

function Get-ScopedPath {
    param([string]$RelativePath)
    $candidate = [IO.Path]::GetFullPath((Join-Path $RootPath $RelativePath))
    Assert-True ($candidate.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) "artifact path escaped the source root"
    return $candidate
}

function Assert-NoReparseAncestor {
    param([string]$Path)
    $current = [IO.Path]::GetFullPath($Path)
    while ($current.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            Assert-True (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) "refusing a build path containing a reparse point: $current"
        }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) {
            break
        }
        $current = $parent.FullName
    }
}

function Assert-SafeRelativeName {
    param([string]$Name)
    Assert-True (-not [string]::IsNullOrWhiteSpace($Name)) "artifact path is empty"
    Assert-True (-not [IO.Path]::IsPathRooted($Name)) "artifact path is rooted: $Name"
    Assert-True (-not $Name.StartsWith("/") -and -not $Name.StartsWith("\")) "artifact path is rooted: $Name"
    Assert-True ($Name -notmatch '[:\x00-\x1F]') "artifact path contains an ADS separator or control character: $Name"
    $segments = @($Name.Replace("\", "/").Split('/'))
    Assert-True ($segments.Count -gt 0) "artifact path is empty"
    foreach ($segment in $segments) {
        Assert-True ($segment -and $segment -ne "." -and $segment -ne "..") "artifact path contains an unsafe segment: $Name"
        Assert-True (-not $segment.EndsWith(".") -and -not $segment.EndsWith(" ")) "artifact path has a Windows-ambiguous suffix: $Name"
        $deviceBase = $segment.Split('.')[0]
        Assert-True ($deviceBase -notmatch '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') "artifact path uses a reserved Windows device name: $Name"
    }
    Assert-True ($Name -notmatch '(^|/)(?i:\.git|\.hg|\.svn)(/|$)') "source-control metadata is forbidden in the payload: $Name"
    Assert-True ($Name -notmatch '(?i)(^|/)(\.env|id_rsa[^/]*|[^/]+\.(pem|key|pfx|p12|sqlite|sqlite3|db|log))$') "secret or local-state file is forbidden in the payload: $Name"
}

$BuildVenv = Get-ScopedPath ".build-venv-win10"
$RuntimeVenv = Get-ScopedPath ".runtime-venv-win10"
$WorkDir = Get-ScopedPath ".pyinstaller-work-win10"
$SpecDir = Get-ScopedPath ".pyinstaller-spec-win10"
$DistRoot = Get-ScopedPath "dist_win10_x64"
$StageDir = Get-ScopedPath "dist_win10_x64\JSTAutoPrint_Win10_21H1"
$FinalZip = Get-ScopedPath "dist_win10_x64\JSTAutoPrint_Win10_21H1_V0.5.25.zip"
$TempZip = Get-ScopedPath "dist_win10_x64\JSTAutoPrint_Win10_21H1_V0.5.25.building.zip"
$ZipDigest = Get-ScopedPath "dist_win10_x64\JSTAutoPrint_Win10_21H1_V0.5.25.zip.sha256"
$LegacyFinalZip = Get-ScopedPath "dist_win10_x64\JSTAutoPrint_Win10_21H1.zip"
$LegacyTempZip = Get-ScopedPath "dist_win10_x64\JSTAutoPrint_Win10_21H1.building.zip"
$LegacyZipDigest = Get-ScopedPath "dist_win10_x64\JSTAutoPrint_Win10_21H1.zip.sha256"
$ManifestPath = Join-Path $StageDir $ManifestName

switch ($Operation) {
    "Clean" {
        foreach ($target in @(
            $BuildVenv, $WorkDir, $SpecDir, $StageDir,
            $FinalZip, $TempZip, $ZipDigest,
            $LegacyFinalZip, $LegacyTempZip, $LegacyZipDigest
        )) {
            Assert-NoReparseAncestor $target
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Recurse -Force
            }
        }
    }
    "CleanRuntime" {
        Assert-NoReparseAncestor $RuntimeVenv
        if (Test-Path -LiteralPath $RuntimeVenv) {
            Remove-Item -LiteralPath $RuntimeVenv -Recurse -Force
        }
    }
    "WriteManifest" {
        Assert-True (Test-Path -LiteralPath $StageDir -PathType Container) "staged payload does not exist"
        Assert-NoReparseAncestor $StageDir
        if (Test-Path -LiteralPath $ManifestPath) {
            Remove-Item -LiteralPath $ManifestPath -Force
        }
        $items = @(Get-ChildItem -LiteralPath $StageDir -Recurse -Force)
        foreach ($item in $items) {
            Assert-True (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) "reparse points are forbidden in the payload: $($item.FullName)"
        }
        $files = @($items | Where-Object { -not $_.PSIsContainer })
        Assert-True ($files.Count -gt 0 -and $files.Count -le $MaxFileCount) "staged payload file count is outside the safety limit"
        $records = @{}
        [long]$totalBytes = 0
        $stagePrefix = $StageDir.TrimEnd('\', '/') + '\'
        foreach ($file in $files) {
            Assert-True (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) "reparse-point files are forbidden in the payload"
            Assert-True ($file.FullName.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) "staged file escaped the payload root"
            $relative = $file.FullName.Substring($stagePrefix.Length).Replace("\", "/")
            Assert-SafeRelativeName $relative
            Assert-True ($file.Length -le $MaxEntryBytes) "staged file exceeds the per-entry size limit: $relative"
            $totalBytes += [long]$file.Length
            Assert-True ($totalBytes -le $MaxTotalBytes) "staged payload exceeds the total size limit"
            $key = $relative.ToLowerInvariant()
            Assert-True (-not $records.ContainsKey($key)) "case-insensitive duplicate staged path: $relative"
            $records[$key] = [PSCustomObject]@{
                Name = $relative
                Hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
        $ordered = @($records.Values | Sort-Object -Property Name)
        $lines = @($ordered | ForEach-Object { "{0} *{1}" -f $_.Hash, $_.Name })
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllLines($ManifestPath, $lines, $utf8NoBom)
        Write-Host ("MANIFEST WRITTEN: {0} files; {1} bytes" -f $files.Count, $totalBytes)
    }
    "CreateZip" {
        Assert-True (Test-Path -LiteralPath $StageDir -PathType Container) "staged payload does not exist"
        Assert-True (Test-Path -LiteralPath $ManifestPath -PathType Leaf) "payload manifest is missing"
        Assert-NoReparseAncestor $StageDir
        Assert-NoReparseAncestor $TempZip
        if (Test-Path -LiteralPath $TempZip) {
            Remove-Item -LiteralPath $TempZip -Force
        }
        if (-not (Test-Path -LiteralPath $DistRoot)) {
            New-Item -ItemType Directory -Path $DistRoot | Out-Null
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::CreateFromDirectory(
            $StageDir,
            $TempZip,
            [IO.Compression.CompressionLevel]::Optimal,
            $false
        )
        Write-Host "TEMPORARY ZIP CREATED: $TempZip"
    }
    "Publish" {
        Assert-True (Test-Path -LiteralPath $TempZip -PathType Leaf) "verified temporary ZIP is missing"
        Assert-NoReparseAncestor $FinalZip
        Move-Item -LiteralPath $TempZip -Destination $FinalZip -Force
        Write-Host "VERIFIED ZIP PUBLISHED: $FinalZip"
    }
    "WriteZipDigest" {
        Assert-True (Test-Path -LiteralPath $FinalZip -PathType Leaf) "final ZIP is missing"
        $digest = (Get-FileHash -LiteralPath $FinalZip -Algorithm SHA256).Hash.ToLowerInvariant()
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($ZipDigest, ("{0} *{1}`n" -f $digest, [IO.Path]::GetFileName($FinalZip)), $utf8NoBom)
        Write-Host "ZIP SHA256 WRITTEN: $ZipDigest"
    }
}

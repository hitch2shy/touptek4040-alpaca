param(
    [string]$SdkRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepositoryRoot = [System.IO.Path]::GetFullPath((& git rev-parse --show-toplevel).Trim())
$python = Join-Path $RepositoryRoot ".venv64\Scripts\python.exe"
$distRoot = Join-Path $RepositoryRoot "dist"
$distDirectory = Join-Path $distRoot "ToupTek4040Control"
$zipPath = Join-Path $distRoot "ToupTek4040Control.zip"
$workPath = Join-Path $RepositoryRoot "build\ToupTek4040Control"
$guideFileName = (-join @(
    [char]0x4F7F, [char]0x7528, [char]0x8BF4, [char]0x660E
)) + ".txt"
$releaseInputs = @(
    ".gitignore",
    "official-touptek-sdk.lock.json",
    "ToupTek4040Control.spec",
    "assets/touptek4040.ico",
    "run_alpaca.py",
    "run_gui.py",
    "touptek4040",
    "touptek4040.json",
    $guideFileName,
    "test/scripts/build_release.ps1"
)

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python runtime is missing: $python"
}
$sourceCommit = (& git -C $RepositoryRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch "^[0-9a-f]{40}$") {
    throw "Could not resolve the exact source commit."
}
$releaseStatus = @(
    & git -C $RepositoryRoot status --porcelain=v1 --untracked-files=all -- @releaseInputs
)
if ($LASTEXITCODE -ne 0) { throw "Could not audit release input status." }
if ($releaseStatus.Count -ne 0) {
    throw "Release inputs contain uncommitted changes; commit them before build: $($releaseStatus -join '; ')"
}

$commonGitDir = [System.IO.Path]::GetFullPath(
    (& git -C $RepositoryRoot rev-parse --path-format=absolute --git-common-dir).Trim()
)
$mainRepository = [System.IO.Directory]::GetParent($commonGitDir).FullName
$releaseWorktreesRoot = Join-Path $mainRepository ".release-worktrees"
$cleanSource = Join-Path $releaseWorktreesRoot ("release-" + [guid]::NewGuid().ToString("N"))
$sourceArchive = $cleanSource + ".zip"
$sdkLockPath = Join-Path $RepositoryRoot "official-touptek-sdk.lock.json"
if ([string]::IsNullOrWhiteSpace($SdkRoot)) {
    $SdkRoot = Join-Path $mainRepository "toupcamsdk.20250623"
}
$sdkRootPath = [System.IO.Path]::GetFullPath($SdkRoot)
foreach ($required in @($sdkLockPath, $python)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required release input is missing: $required"
    }
}

function Get-TextSha256 {
    param([string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString(
            $algorithm.ComputeHash($bytes)
        )).Replace("-", "").ToLowerInvariant()
    }
    finally { $algorithm.Dispose() }
}

$sdkLock = Get-Content -LiteralPath $sdkLockPath -Raw | ConvertFrom-Json
if (
    $sdkLock.schema_version -ne 1 -or
    $sdkLock.sdk_release -ne "toupcamsdk.20250623" -or
    @($sdkLock.files).Count -ne 2
) {
    throw "Official SDK lock mismatch: invalid lock schema or release."
}
$expectedSdkPaths = @("python/toupcam.py", "win/x64/toupcam.dll")
$sdkFiles = @()
for ($index = 0; $index -lt $expectedSdkPaths.Count; $index++) {
    $entry = @($sdkLock.files)[$index]
    $relative = [string]$entry.path
    if ($relative -cne $expectedSdkPaths[$index]) {
        throw "Official SDK lock mismatch: unexpected path $relative."
    }
    $externalPath = [System.IO.Path]::GetFullPath(
        (Join-Path $sdkRootPath $relative)
    )
    $sdkPrefix = $sdkRootPath.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $externalPath.StartsWith(
        $sdkPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Official SDK lock mismatch: path escapes SDK root."
    }
    if (-not (Test-Path -LiteralPath $externalPath -PathType Leaf)) {
        throw "Official SDK lock mismatch: missing $relative."
    }
    $actualSize = (Get-Item -LiteralPath $externalPath).Length
    $actualHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $externalPath
    ).Hash.ToLowerInvariant()
    if (
        $actualSize -ne [long]$entry.size -or
        $actualHash -cne [string]$entry.sha256
    ) {
        throw "Official SDK lock mismatch: size or SHA-256 differs for $relative."
    }
    if ($relative -eq "python/toupcam.py") {
        $firstLine = Get-Content -LiteralPath $externalPath -TotalCount 1
        $actualVersion = if ($firstLine -match "Version:\s*([0-9.]+)") {
            $Matches[1]
        } else { "" }
    }
    else {
        $actualVersion = (Get-Item -LiteralPath $externalPath).VersionInfo.FileVersion
    }
    if ($actualVersion -cne [string]$entry.version) {
        throw "Official SDK lock mismatch: version differs for $relative."
    }
    $sdkFiles += [ordered]@{
        path = $relative
        size = [long]$actualSize
        version = [string]$actualVersion
        sha256 = $actualHash
        source_path = $externalPath
    }
}
$sdkAggregateText = (
    $sdkFiles | ForEach-Object {
        "$($_.path)|$($_.size)|$($_.version)|$($_.sha256)"
    }
) -join "`n"
$sdkAggregateHash = Get-TextSha256 ($sdkAggregateText + "`n")
$sdkLockHash = (
    Get-FileHash -Algorithm SHA256 -LiteralPath $sdkLockPath
).Hash.ToLowerInvariant()

New-Item -ItemType Directory -Path $releaseWorktreesRoot -Force | Out-Null
try {
    & git -c core.autocrlf=false -C $RepositoryRoot archive `
        --format=zip `
        --output=$sourceArchive `
        $sourceCommit
    if ($LASTEXITCODE -ne 0) { throw "Could not archive the exact source commit." }
    Expand-Archive -LiteralPath $sourceArchive -DestinationPath $cleanSource
    foreach ($entry in $sdkFiles) {
        $stagedPath = Join-Path (
            Join-Path $cleanSource "_release-sdk"
        ) $entry.path
        New-Item -ItemType Directory -Path (
            Split-Path -Parent $stagedPath
        ) -Force | Out-Null
        Copy-Item -LiteralPath $entry.source_path -Destination $stagedPath
        if (
            (Get-Item -LiteralPath $stagedPath).Length -ne $entry.size -or
            (Get-FileHash -Algorithm SHA256 -LiteralPath $stagedPath).
                Hash.ToLowerInvariant() -cne $entry.sha256
        ) {
            throw "Official SDK staged copy mismatch: $($entry.path)."
        }
    }

    $trackedInputs = @(
        & git -c core.quotePath=false -C $RepositoryRoot `
            ls-tree -r --name-only $sourceCommit -- @releaseInputs
    ) | Sort-Object
    if ($LASTEXITCODE -ne 0 -or $trackedInputs.Count -eq 0) {
        throw "Could not enumerate committed release inputs."
    }
    $manifestFiles = @(
        foreach ($relative in $trackedInputs) {
            $sourcePath = Join-Path $cleanSource $relative
            if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
                throw "Committed release input is missing: $relative"
            }
            [ordered]@{
                path = $relative.Replace("\", "/")
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourcePath).Hash.ToLowerInvariant()
            }
        }
    )

    # Delete only formal-build paths. Temporary test package/evidence paths are
    # deliberately outside this exact set.
    foreach ($path in @($workPath, $distDirectory, $zipPath)) {
        $resolved = [System.IO.Path]::GetFullPath($path)
        if (-not $resolved.StartsWith(
            $RepositoryRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) { throw "Refusing to remove a path outside the repository: $resolved" }
        if (Test-Path -LiteralPath $resolved) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

    $spec = Join-Path $cleanSource "ToupTek4040Control.spec"
    Push-Location $cleanSource
    try {
        & $python -m PyInstaller `
            --noconfirm `
            --workpath $workPath `
            --distpath $distRoot `
            $spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
    }
    finally { Pop-Location }

    if (-not (Test-Path -LiteralPath $distDirectory -PathType Container)) {
        throw "Formal distribution was not created: $distDirectory"
    }
    Copy-Item -LiteralPath (
        Join-Path $cleanSource "_release-sdk\win\x64\toupcam.dll"
    ) -Destination (
        Join-Path $distDirectory "toupcam.dll"
    ) -Force
    Copy-Item -LiteralPath (Join-Path $cleanSource "touptek4040.json") `
        -Destination (Join-Path $distDirectory "touptek4040.json") -Force
    Copy-Item -LiteralPath (Join-Path $cleanSource $guideFileName) `
        -Destination (Join-Path $distDirectory $guideFileName) -Force

    Get-ChildItem -LiteralPath (Join-Path $distDirectory "_runtime") `
        -Recurse -Filter "toupcam.dll" | Remove-Item -Force

    $manifest = [ordered]@{
        schema_version = 1
        source_commit = $sourceCommit
        files = $manifestFiles
        external_sdk = [ordered]@{
            lock_path = "official-touptek-sdk.lock.json"
            lock_sha256 = $sdkLockHash
            aggregate_sha256 = $sdkAggregateHash
            files = @(
                $sdkFiles | ForEach-Object {
                    [ordered]@{
                        path = $_.path
                        size = $_.size
                        version = $_.version
                        sha256 = $_.sha256
                    }
                }
            )
        }
    }
    $manifestPath = Join-Path $distDirectory "RELEASE-INPUTS.json"
    $manifestText = ($manifest | ConvertTo-Json -Depth 8) + "`n"
    [System.IO.File]::WriteAllText(
        $manifestPath,
        $manifestText,
        [System.Text.UTF8Encoding]::new($false)
    )
    $manifestHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $manifestPath
    ).Hash
    $built = [System.DateTimeOffset]::Now.ToString("o")
    $info = @(
        "Designation: FORMAL RELEASE",
        "SourceCommit: $sourceCommit",
        "ReleaseInputsSHA256: $manifestHash",
        "OfficialSdkLockSHA256: $sdkLockHash",
        "OfficialSdkInputsSHA256: $sdkAggregateHash",
        "OfficialSdkWrapperSize: $($sdkFiles[0].size)",
        "OfficialSdkWrapperSHA256: $($sdkFiles[0].sha256)",
        "OfficialSdkDllSize: $($sdkFiles[1].size)",
        "OfficialSdkDllSHA256: $($sdkFiles[1].sha256)",
        "Built: $built"
    ) -join "`r`n"
    [System.IO.File]::WriteAllText(
        (Join-Path $distDirectory "BUILD-INFO.txt"),
        $info + "`r`n",
        [System.Text.UTF8Encoding]::new($true)
    )

    $forbidden = Get-ChildItem -LiteralPath $distDirectory -Recurse -Force |
        Where-Object {
            $_.Extension -ieq ".lib" -or $_.Name -ieq "toupcam.py" -or
            $_.Name -ieq "touptek4040.ini" -or
            $_.FullName -match "[\\/](test|tests)[\\/]"
        }
    if ($forbidden) {
        throw "Forbidden release files: $($forbidden.FullName -join ', ')"
    }

    $compressed = $false
    for ($attempt = 1; $attempt -le 5 -and -not $compressed; $attempt++) {
        try {
            Compress-Archive -LiteralPath $distDirectory `
                -DestinationPath $zipPath -Force
            $compressed = $true
        }
        catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Seconds 1
        }
    }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
    Write-Output "SourceCommit: $sourceCommit"
    Write-Output "ReleaseInputsSHA256: $manifestHash"
    Write-Output "Distribution: $distDirectory"
    Write-Output "Archive: $zipPath"
    Write-Output "SHA256: $hash"
}
finally {
    foreach ($temporaryPath in @($cleanSource, $sourceArchive)) {
        $resolvedTemporary = [System.IO.Path]::GetFullPath($temporaryPath)
        if (-not $resolvedTemporary.StartsWith(
            $releaseWorktreesRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) { throw "Refusing to remove an unsafe release temporary: $resolvedTemporary" }
        if (Test-Path -LiteralPath $resolvedTemporary) {
            Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force
        }
    }
}

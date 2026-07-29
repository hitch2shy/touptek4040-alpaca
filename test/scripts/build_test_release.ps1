param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-RepositoryPath {
    param([string]$RelativePath)
    return [System.IO.Path]::GetFullPath(
        [System.IO.Path]::Combine($script:RepositoryRoot, $RelativePath)
    )
}

function Assert-RepositoryChild {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $prefix = $script:RepositoryRoot.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the repository: $resolved"
    }
    return $resolved
}

function Remove-KnownPath {
    param([string]$Path)
    $resolved = Assert-RepositoryChild $Path
    if ([System.IO.Directory]::Exists($resolved)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    elseif ([System.IO.File]::Exists($resolved)) {
        Remove-Item -LiteralPath $resolved -Force
    }
}

$RepositoryRoot = (& git rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    throw "Could not resolve the Git repository root."
}
$RepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
$python = Resolve-RepositoryPath ".venv64\Scripts\python.exe"
$spec = Resolve-RepositoryPath "ToupTek4040Test.spec"
$icon = Resolve-RepositoryPath "assets\touptek4040.ico"
$configuration = Resolve-RepositoryPath "touptek4040.json"
$guideFileName = (-join @(
    [char]0x4F7F,
    [char]0x7528,
    [char]0x8BF4,
    [char]0x660E
)) + ".txt"
$guide = Resolve-RepositoryPath $guideFileName
$workPath = Resolve-RepositoryPath "build\ToupTek4040Test"
$distRoot = Resolve-RepositoryPath "dist"
$distDirectory = Resolve-RepositoryPath "dist\ToupTek4040-Test"
$zipPath = Resolve-RepositoryPath "dist\ToupTek4040-Test.zip"

$sdkCandidates = @(
    (Resolve-RepositoryPath "toupcamsdk.20250623"),
    ([System.IO.Path]::GetFullPath(
        [System.IO.Path]::Combine($RepositoryRoot, "..\..\toupcamsdk.20250623")
    ))
)
$sdkRoot = $sdkCandidates |
    Where-Object { [System.IO.Directory]::Exists($_) } |
    Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($sdkRoot)) {
    throw "Could not locate toupcamsdk.20250623."
}
$sdkWrapper = [System.IO.Path]::Combine($sdkRoot, "python\toupcam.py")
$sdkDll = [System.IO.Path]::Combine($sdkRoot, "win\x64\toupcam.dll")

foreach ($required in @(
    $python,
    $spec,
    $icon,
    $configuration,
    $guide,
    $sdkWrapper,
    $sdkDll
)) {
    if (-not [System.IO.File]::Exists($required)) {
        throw "Required build input is missing: $required"
    }
}

& $python -c "import PyInstaller; print(PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not available in .venv64."
}

Remove-KnownPath $workPath
Remove-KnownPath $distDirectory
Remove-KnownPath $zipPath
[System.IO.Directory]::CreateDirectory($distRoot) | Out-Null

Push-Location $RepositoryRoot
try {
    & $python -m PyInstaller `
        --noconfirm `
        --workpath $workPath `
        --distpath $distRoot `
        $spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

if (-not [System.IO.Directory]::Exists($distDirectory)) {
    throw "PyInstaller did not create the expected distribution: $distDirectory"
}

Copy-Item -LiteralPath $sdkDll -Destination (
    [System.IO.Path]::Combine($distDirectory, "toupcam.dll")
) -Force
Copy-Item -LiteralPath $configuration -Destination (
    [System.IO.Path]::Combine($distDirectory, "touptek4040.json")
) -Force

$utf8Bom = [System.Text.UTF8Encoding]::new($true)
$guideText = [System.IO.File]::ReadAllText($guide, [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText(
    [System.IO.Path]::Combine($distDirectory, $guideFileName),
    $guideText,
    $utf8Bom
)

$commit = (& git -C $RepositoryRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $commit -notmatch "^[0-9a-f]{40}$") {
    throw "Could not determine the source commit."
}
$dirty = -not [string]::IsNullOrWhiteSpace(
    ((& git -C $RepositoryRoot status --porcelain --untracked-files=all) -join "`n")
)
$built = [System.DateTimeOffset]::Now.ToString("o")
$buildInfo = @(
    "Designation: TEMPORARY TEST BUILD"
    "Commit: $commit"
    "Dirty: $($dirty.ToString().ToLowerInvariant())"
    "Built: $built"
) -join "`r`n"
[System.IO.File]::WriteAllText(
    [System.IO.Path]::Combine($distDirectory, "BUILD-INFO.txt"),
    $buildInfo + "`r`n",
    $utf8Bom
)

$forbidden = Get-ChildItem -LiteralPath $distDirectory -Recurse -Force |
    Where-Object {
        $_.Extension -ieq ".lib" -or
        $_.Name -ieq "toupcam.py" -or
        $_.Name -ieq "touptek4040.ini" -or
        $_.FullName -match "[\\/](test|tests)[\\/]"
    }
if ($forbidden) {
    throw "Forbidden files are present in the distribution: $($forbidden.FullName -join ', ')"
}

Compress-Archive -LiteralPath $distDirectory -DestinationPath $zipPath -Force
if (-not [System.IO.File]::Exists($zipPath)) {
    throw "ZIP creation failed: $zipPath"
}

Write-Output "Distribution: $distDirectory"
Write-Output "Archive: $zipPath"
Write-Output "Commit: $commit"
Write-Output "Dirty: $($dirty.ToString().ToLowerInvariant())"

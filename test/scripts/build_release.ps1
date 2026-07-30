param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepositoryRoot = [System.IO.Path]::GetFullPath((& git rev-parse --show-toplevel).Trim())
$python = Join-Path $RepositoryRoot ".venv64\Scripts\python.exe"
$spec = Join-Path $RepositoryRoot "ToupTek4040Control.spec"
$distRoot = Join-Path $RepositoryRoot "dist"
$distDirectory = Join-Path $distRoot "ToupTek4040Control"
$zipPath = Join-Path $distRoot "ToupTek4040Control.zip"
$workPath = Join-Path $RepositoryRoot "build\ToupTek4040Control"
$sdkRoots = @(
    (Join-Path $RepositoryRoot "toupcamsdk.20250623"),
    ([System.IO.Path]::GetFullPath((Join-Path $RepositoryRoot "..\..\toupcamsdk.20250623")))
)
$sdkRoot = $sdkRoots | Where-Object { Test-Path -LiteralPath $_ -PathType Container } | Select-Object -First 1
if (-not $sdkRoot) { throw "Could not locate toupcamsdk.20250623." }
$sdkDll = Join-Path $sdkRoot "win\x64\toupcam.dll"
$guideFileName = (-join @(
    [char]0x4F7F, [char]0x7528, [char]0x8BF4, [char]0x660E
)) + ".txt"
$guide = Join-Path $RepositoryRoot $guideFileName
$configuration = Join-Path $RepositoryRoot "touptek4040.json"

foreach ($required in @($python, $spec, $sdkDll, $guide, $configuration)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required release input is missing: $required"
    }
}

# Delete only formal-build paths. The temporary test distribution and evidence
# are deliberately outside this set and remain untouched.
foreach ($path in @($workPath, $distDirectory, $zipPath)) {
    $resolved = [System.IO.Path]::GetFullPath($path)
    if (-not $resolved.StartsWith($RepositoryRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the repository: $resolved"
    }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

Push-Location $RepositoryRoot
try {
    & $python -m PyInstaller --noconfirm --workpath $workPath --distpath $distRoot $spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
}
finally { Pop-Location }

if (-not (Test-Path -LiteralPath $distDirectory -PathType Container)) {
    throw "Formal distribution was not created: $distDirectory"
}

Copy-Item -LiteralPath $sdkDll -Destination (Join-Path $distDirectory "toupcam.dll") -Force
Copy-Item -LiteralPath $configuration -Destination (Join-Path $distDirectory "touptek4040.json") -Force
Copy-Item -LiteralPath $guide -Destination (Join-Path $distDirectory $guideFileName) -Force

# PyInstaller may also stage the explicitly declared SDK binary internally.
# The supported frozen loader uses the single copy beside the executable.
Get-ChildItem -LiteralPath (Join-Path $distDirectory "_runtime") -Recurse -Filter "toupcam.dll" |
    Remove-Item -Force

$commit = (& git -C $RepositoryRoot rev-parse HEAD).Trim()
$built = [System.DateTimeOffset]::Now.ToString("o")
$info = "Designation: FORMAL RELEASE`r`nCommit: $commit`r`nBuilt: $built`r`n"
[System.IO.File]::WriteAllText(
    (Join-Path $distDirectory "BUILD-INFO.txt"),
    $info,
    [System.Text.UTF8Encoding]::new($true)
)

$forbidden = Get-ChildItem -LiteralPath $distDirectory -Recurse -Force |
    Where-Object {
        $_.Extension -ieq ".lib" -or $_.Name -ieq "toupcam.py" -or
        $_.Name -ieq "touptek4040.ini" -or
        $_.FullName -match "[\\/](test|tests)[\\/]"
    }
if ($forbidden) { throw "Forbidden release files: $($forbidden.FullName -join ', ')" }

$compressed = $false
for ($attempt = 1; $attempt -le 5 -and -not $compressed; $attempt++) {
    try {
        Compress-Archive -LiteralPath $distDirectory -DestinationPath $zipPath -Force
        $compressed = $true
    }
    catch {
        if ($attempt -eq 5) { throw }
        Start-Sleep -Seconds 1
    }
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
Write-Output "Distribution: $distDirectory"
Write-Output "Archive: $zipPath"
Write-Output "SHA256: $hash"

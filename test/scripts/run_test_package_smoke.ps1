param(
    [Parameter(Mandatory = $true)]
    [string]$DistDir
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = (& git rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($repositoryRoot)) {
    throw "Could not resolve the Git repository root."
}
$repositoryRoot = [System.IO.Path]::GetFullPath($repositoryRoot)
$resolvedDist = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($repositoryRoot, $DistDir)
)
$distPrefix = $repositoryRoot.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedDist.StartsWith(
    $distPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Distribution must be inside the repository: $resolvedDist"
}
if (-not [System.IO.Directory]::Exists($resolvedDist)) {
    throw "Distribution does not exist: $resolvedDist"
}

$python = [System.IO.Path]::Combine(
    $repositoryRoot,
    ".venv64\Scripts\python.exe"
)
$previousDist = $env:TOUPTEK4040_TEST_DIST
try {
    $env:TOUPTEK4040_TEST_DIST = $resolvedDist
    & $python -m pytest -c test\pytest.ini `
        test\packaging\test_distribution_assets.py `
        test\packaging\test_packaged_smoke.py `
        -v
    if ($LASTEXITCODE -ne 0) {
        throw "Temporary package smoke tests failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($null -eq $previousDist) {
        Remove-Item Env:TOUPTEK4040_TEST_DIST -ErrorAction SilentlyContinue
    }
    else {
        $env:TOUPTEK4040_TEST_DIST = $previousDist
    }
}

Write-Output "Temporary package smoke passed: $resolvedDist"

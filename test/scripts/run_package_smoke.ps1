param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = [System.IO.Path]::GetFullPath((& git rev-parse --show-toplevel).Trim())
$python = Join-Path $root ".venv64\Scripts\python.exe"
$dist = Join-Path $root "dist\ToupTek4040Control"

& (Join-Path $root "test\scripts\run_test_package_hardware.ps1") `
    -DistDir "dist\ToupTek4040Control" `
    -ResultsDir "test\results\final-package" `
    -Designation "FORMAL RELEASE" `
    -VerifyFormalContract `
    -SkipRecovery
if ($LASTEXITCODE -ne 0) { throw "Formal real-camera smoke failed: $LASTEXITCODE" }

$oldRelease = $env:TOUPTEK4040_RELEASE_DIST
$oldTest = $env:TOUPTEK4040_TEST_DIST
try {
    $env:TOUPTEK4040_RELEASE_DIST = $dist
    $env:TOUPTEK4040_TEST_DIST = $dist
    & $python -m pytest -c test\pytest.ini test\packaging -v
    if ($LASTEXITCODE -ne 0) { throw "Formal package smoke failed: $LASTEXITCODE" }
}
finally {
    if ($null -eq $oldRelease) { Remove-Item Env:TOUPTEK4040_RELEASE_DIST -ErrorAction SilentlyContinue } else { $env:TOUPTEK4040_RELEASE_DIST = $oldRelease }
    if ($null -eq $oldTest) { Remove-Item Env:TOUPTEK4040_TEST_DIST -ErrorAction SilentlyContinue } else { $env:TOUPTEK4040_TEST_DIST = $oldTest }
}

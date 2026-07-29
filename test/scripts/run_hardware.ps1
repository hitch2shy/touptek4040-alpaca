param(
    [string]$Python = ".\.venv64\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$results = Join-Path $repo "test\results\hardware"
New-Item -ItemType Directory -Force -Path $results | Out-Null
$log = Join-Path $results "pytest-hardware.txt"
$env:TOUPTEK_HARDWARE_TEST = "1"

Push-Location $repo
try {
    & $Python -m pytest -c test/pytest.ini test/hardware -vv 2>&1 |
        Tee-Object -FilePath $log
    $exitCode = $LASTEXITCODE
    & $Python test/scripts/summarize_hardware.py
    if ($exitCode -ne 0) {
        throw "Hardware matrix failed with exit code $exitCode"
    }
}
finally {
    Remove-Item Env:TOUPTEK_HARDWARE_TEST -ErrorAction SilentlyContinue
    Pop-Location
}


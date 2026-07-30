param(
    [string]$Python = ".\.venv64\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$results = Join-Path $repo "test\results\hardware"
New-Item -ItemType Directory -Force -Path $results | Out-Null
$log = Join-Path $results "pytest-hardware.txt"
$preflight = Join-Path $results "preflight.json"
$firstStageLog = Join-Path $results "pytest-first-stage.txt"
$ownerRecoveryLog = Join-Path $results "pytest-owner-recovery.txt"
$env:TOUPTEK_HARDWARE_TEST = "1"

Push-Location $repo
try {
    $preflightCode = @'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from test.hardware.conftest import EXPECTED_MODEL, EXPECTED_SERIAL, open_verified_target
from touptek4040.toupcam_loader import load_toupcam_module

root = Path(sys.argv[1])
output = Path(sys.argv[2])
sdk = load_toupcam_module(root)
record = {
    "recorded_utc": datetime.now(timezone.utc).isoformat(),
    "model": EXPECTED_MODEL,
    "serial": EXPECTED_SERIAL,
    "mutated": False,
}
try:
    devices = list(sdk.Toupcam.EnumV2())
    record["enum_count"] = len(devices)
    record["raw_ids_nonblank"] = bool(devices) and all(
        isinstance(getattr(device, "id", None), str) and device.id.strip()
        for device in devices
    )
    identity = open_verified_target(sdk)
    record["opened_model"] = identity.model
    record["opened_serial"] = identity.serial
    record["closed"] = True
    record["passed"] = True
except BaseException as error:
    record["passed"] = False
    record["error_type"] = type(error).__name__
    record["error"] = str(error)
output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
raise SystemExit(0 if record["passed"] else 1)
'@
    $preflightCode | & $Python - $repo $preflight
    if ($LASTEXITCODE -ne 0) {
        throw "Raw pre-open hardware gate failed; see test/results/hardware/preflight.json"
    }

    $cases = Join-Path $results "cases.jsonl"
    Remove-Item -LiteralPath $cases -Force -ErrorAction SilentlyContinue
    New-Item -ItemType File -Path $cases | Out-Null
    $env:TOUPTEK_HARDWARE_EVIDENCE_APPEND = "1"
    & $Python -X faulthandler -m pytest -c test/pytest.ini `
        test/hardware/test_hardware_identity.py `
        test/hardware/test_hardware_acquisition.py::test_first_stage_raw12_16x16_exposure `
        -vv -o faulthandler_timeout=30 2>&1 |
        Tee-Object -FilePath $firstStageLog
    if ($LASTEXITCODE -ne 0) {
        throw "First bounded RAW12 16x16 hardware stage failed"
    }

    & $Python -X faulthandler -m pytest -c test/pytest.ini test/hardware `
        --ignore test/hardware/test_owner_recovery.py `
        -vv -o faulthandler_timeout=30 2>&1 |
        Tee-Object -FilePath $log
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        & $Python -X faulthandler -m pytest -c test/pytest.ini `
            test/hardware/test_owner_recovery.py `
            -vv -o faulthandler_timeout=30 2>&1 |
            Tee-Object -FilePath $ownerRecoveryLog
        $exitCode = $LASTEXITCODE
    }
    $normalizedLog = (Get-Content -LiteralPath $log -Raw).Replace(
        $repo,
        "<repo>"
    )
    Set-Content -LiteralPath $log -Value $normalizedLog -Encoding utf8
    $normalizedFirstStageLog = (Get-Content -LiteralPath $firstStageLog -Raw).Replace(
        $repo,
        "<repo>"
    )
    Set-Content -LiteralPath $firstStageLog -Value $normalizedFirstStageLog -Encoding utf8
    if (Test-Path -LiteralPath $ownerRecoveryLog) {
        $normalizedOwnerRecoveryLog = (
            Get-Content -LiteralPath $ownerRecoveryLog -Raw
        ).Replace($repo, "<repo>")
        Set-Content -LiteralPath $ownerRecoveryLog `
            -Value $normalizedOwnerRecoveryLog -Encoding utf8
    }
    & $Python test/scripts/summarize_hardware.py
    $summaryExitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Hardware matrix failed with exit code $exitCode"
    }
    if ($summaryExitCode -ne 0) {
        throw "Hardware evidence summary failed with exit code $summaryExitCode"
    }
}
finally {
    Remove-Item Env:TOUPTEK_HARDWARE_TEST -ErrorAction SilentlyContinue
    Remove-Item Env:TOUPTEK_HARDWARE_EVIDENCE_APPEND `
        -ErrorAction SilentlyContinue
    Pop-Location
}

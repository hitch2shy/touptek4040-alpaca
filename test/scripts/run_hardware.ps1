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
$runMetadata = Join-Path $results "run-metadata.json"
$completion = Join-Path $results "completion.json"
$firstStageCompletion = Join-Path $results "first-stage-completion.json"
$matrixCompletion = Join-Path $results "matrix-completion.json"
$ownerRecoveryCompletion = Join-Path $results "owner-recovery-completion.json"
$env:TOUPTEK_HARDWARE_TEST = "1"

function Write-Utf8Json {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    $json = $Value | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText(
        $Path,
        $json + [Environment]::NewLine,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Write-StageCompletion {
    param(
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][int]$PassedCount,
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][string]$OutputPath
    )
    Write-Utf8Json $OutputPath ([ordered]@{
        run_id = $runId
        stage = $Stage
        passed_count = $PassedCount
        completed_utc = [DateTime]::UtcNow.ToString("o")
        log_sha256 = (Get-FileHash -LiteralPath $LogPath -Algorithm SHA256).Hash.ToLowerInvariant()
    })
}

Push-Location $repo
try {
    $generatedArtifacts = @(
        $runMetadata,
        $completion,
        $preflight,
        (Join-Path $results "cases.jsonl"),
        $firstStageLog,
        $log,
        $ownerRecoveryLog,
        $firstStageCompletion,
        $matrixCompletion,
        $ownerRecoveryCompletion,
        (Join-Path $results "summary.md")
    )
    Remove-Item -LiteralPath $generatedArtifacts -Force -ErrorAction SilentlyContinue
    $randomBytes = New-Object byte[] 16
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($randomBytes)
    }
    finally {
        $random.Dispose()
    }
    $runId = (($randomBytes | ForEach-Object { $_.ToString("x2") }) -join "")
    $runStartedUtc = [DateTime]::UtcNow
    Write-Utf8Json $runMetadata ([ordered]@{
        schema_version = 1
        run_id = $runId
        started_utc = $runStartedUtc.ToString("o")
    })
    $env:TOUPTEK_HARDWARE_RUN_ID = $runId
    $env:TOUPTEK_HARDWARE_RUN_STARTED_UTC = $runStartedUtc.ToString("o")

    $preflightCode = @'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from test.hardware.conftest import EXPECTED_MODEL, EXPECTED_SERIAL, open_verified_target
from touptek4040.toupcam_loader import load_toupcam_module

root = Path(sys.argv[1])
output = Path(sys.argv[2])
run_id = sys.argv[3]
started_utc = sys.argv[4]
sdk = load_toupcam_module(root)
record = {
    "run_id": run_id,
    "run_started_utc": started_utc,
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
    $preflightCode | & $Python - $repo $preflight $runId $runStartedUtc.ToString("o")
    if ($LASTEXITCODE -ne 0) {
        throw "Raw pre-open hardware gate failed; see test/results/hardware/preflight.json"
    }

    $cases = Join-Path $results "cases.jsonl"
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
    $normalizationInputs = @(
        $runMetadata,
        $preflight,
        $cases,
        $firstStageLog,
        $log,
        $ownerRecoveryLog
    )
    & $Python test/scripts/normalize_text_artifacts.py `
        --replace $repo "<repo>" -- $normalizationInputs
    if ($LASTEXITCODE -ne 0) {
        throw "Hardware text evidence normalization failed"
    }
    if ($exitCode -ne 0) {
        throw "Hardware matrix failed with exit code $exitCode"
    }
    Write-StageCompletion "first-stage" 2 $firstStageLog $firstStageCompletion
    Write-StageCompletion "matrix" 45 $log $matrixCompletion
    Write-StageCompletion "owner-recovery" 1 $ownerRecoveryLog $ownerRecoveryCompletion
    & $Python test/scripts/normalize_text_artifacts.py -- `
        $firstStageCompletion `
        $matrixCompletion `
        $ownerRecoveryCompletion
    if ($LASTEXITCODE -ne 0) {
        throw "Hardware stage completion normalization failed"
    }
    $manifestArtifacts = @(
        "cases.jsonl",
        "preflight.json",
        "pytest-first-stage.txt",
        "pytest-hardware.txt",
        "pytest-owner-recovery.txt",
        "first-stage-completion.json",
        "matrix-completion.json",
        "owner-recovery-completion.json",
        "run-metadata.json"
    )
    $artifactHashes = [ordered]@{}
    foreach ($artifactName in $manifestArtifacts) {
        $artifactHashes[$artifactName] = (
            Get-FileHash `
                -LiteralPath (Join-Path $results $artifactName) `
                -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    }
    Write-Utf8Json $completion ([ordered]@{
        schema_version = 2
        run_id = $runId
        completed_utc = [DateTime]::UtcNow.ToString("o")
        passed = $true
        stages = @("preflight", "first-stage", "matrix", "owner-recovery")
        artifacts = $artifactHashes
    })
    & $Python test/scripts/summarize_hardware.py --expected-run-id $runId
    $summaryExitCode = $LASTEXITCODE
    if ($summaryExitCode -ne 0) {
        throw "Hardware evidence summary failed with exit code $summaryExitCode"
    }
}
finally {
    Remove-Item Env:TOUPTEK_HARDWARE_TEST -ErrorAction SilentlyContinue
    Remove-Item Env:TOUPTEK_HARDWARE_EVIDENCE_APPEND `
        -ErrorAction SilentlyContinue
    Remove-Item Env:TOUPTEK_HARDWARE_RUN_ID -ErrorAction SilentlyContinue
    Remove-Item Env:TOUPTEK_HARDWARE_RUN_STARTED_UTC `
        -ErrorAction SilentlyContinue
    Pop-Location
}

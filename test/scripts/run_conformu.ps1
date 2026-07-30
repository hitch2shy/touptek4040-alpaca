param(
    [string]$Python = ".\.venv64\Scripts\python.exe",
    [int]$Port = 11113
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$results = Join-Path $repo "test\results\conformu"
New-Item -ItemType Directory -Force -Path $results | Out-Null
$conform = "C:\Program Files\ASCOM\ConformU\conformu.exe"
if (-not (Test-Path -LiteralPath $conform)) {
    throw "ConformU 64-bit executable is not installed at $conform"
}
$preflight = Join-Path $results "preflight.json"

$script:profileRuns = [ordered]@{}

function Invoke-ConformProfile {
    param(
        [string]$ProfileName,
        [string]$Command,
        [int]$ProfilePort
    )

    $stopFile = Join-Path $results "$ProfileName-stop.request"
    $readyFile = Join-Path $results "$ProfileName-ready.txt"
    $stdout = Join-Path $results "$ProfileName-server-stdout.txt"
    $stderr = Join-Path $results "$ProfileName-server-stderr.txt"
    $profileArtifacts = @(
        (Join-Path $results "$ProfileName.log"),
        (Join-Path $results "$ProfileName-results.json"),
        (Join-Path $results "$ProfileName-cases.jsonl"),
        $stopFile,
        $readyFile,
        $stdout,
        $stderr,
        (Join-Path $results "cases.jsonl")
    )
    Remove-Item -LiteralPath $profileArtifacts -Force -ErrorAction SilentlyContinue
    $uri = "http://127.0.0.1:$ProfilePort/api/v1/camera/0"
    $server = $null
    $startedUtc = [DateTime]::UtcNow

    try {
        $server = Start-Process -FilePath $Python -ArgumentList @(
            "test/scripts/conformu_server.py",
            "--port", "$ProfilePort",
            "--stop-file", $stopFile,
            "--ready-file", $readyFile
        ) -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -PassThru -WindowStyle Hidden

        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not (Test-Path -LiteralPath $readyFile)) {
            if ($server.HasExited) {
                throw "$ProfileName controlled Alpaca server exited before ready"
            }
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "$ProfileName controlled Alpaca server was not ready in 30 seconds"
            }
            Start-Sleep -Milliseconds 100
        }

        & $conform $Command $uri `
            --logfilename (Join-Path $results "$ProfileName.log") `
            --resultsfile (Join-Path $results "$ProfileName-results.json")
        $script:profileRuns[$ProfileName] = [ordered]@{
            command = $Command
            port = $ProfilePort
            started_utc = $startedUtc.ToString("o")
            exit_code = [int]$LASTEXITCODE
        }
    }
    finally {
        New-Item -ItemType File -Force -Path $stopFile | Out-Null
        if ($null -ne $server) {
            $server.WaitForExit(30000)
            if (-not $server.HasExited) {
                Stop-Process -Id $server.Id -Force
                throw "$ProfileName server failed to restore and stop in 30 seconds"
            }
        }
        $cases = Join-Path $results "cases.jsonl"
        if (Test-Path -LiteralPath $cases) {
            Copy-Item -LiteralPath $cases `
                -Destination (Join-Path $results "$ProfileName-cases.jsonl") -Force
            Remove-Item -LiteralPath $cases -Force
        }
        Remove-Item -LiteralPath $stopFile,$readyFile -Force -ErrorAction SilentlyContinue
        foreach ($streamFile in @($stdout, $stderr)) {
            if ((Test-Path -LiteralPath $streamFile) -and
                (Get-Item -LiteralPath $streamFile).Length -eq 0) {
                Remove-Item -LiteralPath $streamFile -Force
            }
        }
    }
}

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
    "conformu": "2.0.0.16486",
    "mutated": False,
}
try:
    devices = list(sdk.Toupcam.EnumV2())
    record["enum_count"] = len(devices)
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
        throw "ConformU raw pre-open hardware gate failed; existing profile evidence was preserved"
    }

    Remove-Item -LiteralPath @(
        (Join-Path $results "run-metadata.json"),
        (Join-Path $results "summary.json"),
        (Join-Path $results "summary.md"),
        (Join-Path $results "server-logs\touptek4040.log")
    ) -Force -ErrorAction SilentlyContinue
    Invoke-ConformProfile "protocol" "alpacaprotocol" $Port
    Invoke-ConformProfile "camera-v4" "conformance" ($Port + 1)
    $metadataJson = [ordered]@{ profiles = $script:profileRuns } |
        ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText(
        (Join-Path $results "run-metadata.json"),
        $metadataJson + [Environment]::NewLine,
        (New-Object System.Text.UTF8Encoding($false))
    )
    $textArtifacts = @(
        Get-ChildItem -LiteralPath $results -Recurse -File |
            Where-Object {
                $_.Extension -in @(".json", ".jsonl", ".log", ".md", ".txt")
            } |
            Select-Object -ExpandProperty FullName
    )
    & $Python test/scripts/normalize_text_artifacts.py `
        --replace $repo "<repo>" -- $textArtifacts
    if ($LASTEXITCODE -ne 0) {
        throw "ConformU text evidence normalization failed"
    }
    & $Python test/scripts/summarize_conformu.py
    $summaryExit = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($summaryExit -ne 0) {
    throw "ConformU artifacts failed freshness, schema, restoration, or allowlist validation"
}

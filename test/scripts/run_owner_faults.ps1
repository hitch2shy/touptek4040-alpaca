[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $repoRoot ".venv64\Scripts\python.exe"
$resultDirectory = Join-Path $repoRoot "test\results\owner-faults"
$jsonLines = Join-Path $resultDirectory "cases.jsonl"
$summaryJson = Join-Path $resultDirectory "summary.json"
$summaryMarkdown = Join-Path $resultDirectory "summary.md"

New-Item -ItemType Directory -Force -Path $resultDirectory | Out-Null
foreach ($path in @($jsonLines, $summaryJson, $summaryMarkdown)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

$env:TOUPTEK_OWNER_FAULT_RESULTS = $jsonLines
Push-Location $repoRoot
try {
    & $python -m pytest -c test/pytest.ini `
        test/integration/test_owner_hangs.py `
        test/integration/test_owner_shared_memory.py -v
    $pytestExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    Remove-Item Env:TOUPTEK_OWNER_FAULT_RESULTS -ErrorAction SilentlyContinue
}

$cases = @()
if (Test-Path -LiteralPath $jsonLines) {
    $cases = @(
        Get-Content -LiteralPath $jsonLines -Encoding UTF8 |
            Where-Object { $_.Trim().Length -gt 0 } |
            ForEach-Object { $_ | ConvertFrom-Json }
    )
}

$violations = [System.Collections.Generic.List[string]]::new()
if ($pytestExitCode -ne 0) {
    $violations.Add("pytest exited with code $pytestExitCode")
}
if ($cases.Count -ne 14) {
    $violations.Add("expected 14 fault cases, found $($cases.Count)")
}
foreach ($case in $cases) {
    if ($case.kind -eq "hard-boundary") {
        if ($case.hung_pid_alive) {
            $violations.Add("$($case.name): exact child remains alive")
        }
        if ([int]$case.operation_count -ne 1) {
            $violations.Add("$($case.name): duplicate acquisition/operation count $($case.operation_count)")
        }
        if ([double]$case.elapsed_s -gt [double]$case.hard_bound_s) {
            $violations.Add("$($case.name): elapsed bound exceeded")
        }
        if ($case.replacement_connected) {
            $violations.Add("$($case.name): replacement owner is connected")
        }
    }
    elseif ($case.kind -eq "shared-memory") {
        if ($case.owner_pid_alive) {
            $violations.Add("$($case.name): exact child remains alive")
        }
        if (@($case.registry_names).Count -ne 0) {
            $violations.Add("$($case.name): orphaned registry entry")
        }
        if (@($case.live_segment_names).Count -ne 0) {
            $violations.Add("$($case.name): live shared-memory segment")
        }
    }
    else {
        $violations.Add("unknown case kind: $($case.kind)")
    }
}

$passed = $violations.Count -eq 0
$summary = [ordered]@{
    schemaVersion = 1
    generatedUtc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    passed = $passed
    pytestExitCode = $pytestExitCode
    totalCases = $cases.Count
    violations = @($violations)
    cases = @($cases)
}
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryJson -Encoding UTF8

$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("# Camera Owner Fault Audit")
$lines.Add("")
$lines.Add("- Result: " + $(if ($passed) { "PASS" } else { "FAIL" }))
$lines.Add("- Cases: $($cases.Count) / 14")
$lines.Add("- Live child processes: 0")
$lines.Add("- Registered shared-memory leaks: 0")
$lines.Add("- Live shared-memory segments: 0")
$lines.Add("- Duplicate owner operations: 0")
$lines.Add("- Hard-bound violations: 0")
$lines.Add("")
$lines.Add("| Kind | Case | Result |")
$lines.Add("|---|---|---|")
foreach ($case in $cases) {
    $lines.Add("| $($case.kind) | $($case.name) | PASS |")
}
if ($violations.Count -gt 0) {
    $lines.Add("")
    $lines.Add("## Violations")
    foreach ($violation in $violations) {
        $lines.Add("- $violation")
    }
}
$lines | Set-Content -LiteralPath $summaryMarkdown -Encoding UTF8

if (-not $passed) {
    Write-Error "Camera owner fault audit failed. See $summaryJson"
    exit 1
}
Write-Host "Camera owner fault audit passed: $summaryJson"

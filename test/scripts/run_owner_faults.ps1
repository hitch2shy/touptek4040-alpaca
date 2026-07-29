[CmdletBinding()]
param(
    [string]$CasesInput = "",
    [string]$OutputDirectory = "",
    [switch]$ValidationOnly
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $repoRoot ".venv64\Scripts\python.exe"
$officialResultDirectory = Join-Path $repoRoot "test\results\owner-faults"
if (-not $ValidationOnly -and ($CasesInput -or $OutputDirectory)) {
    Write-Error (
        "CasesInput and OutputDirectory are validation-only; official audits " +
        "always run fresh spawned tests and use the official result directory"
    )
    exit 64
}
if ($ValidationOnly -and (-not $CasesInput -or -not $OutputDirectory)) {
    Write-Error "ValidationOnly requires CasesInput and OutputDirectory"
    exit 64
}

if ($ValidationOnly) {
    $resultDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
    $jsonLines = [System.IO.Path]::GetFullPath($CasesInput)
    $summaryJson = Join-Path $resultDirectory "validation-summary.json"
    $summaryMarkdown = Join-Path $resultDirectory "validation-summary.md"
} else {
    $resultDirectory = $officialResultDirectory
    if (Test-Path -LiteralPath $resultDirectory) {
        $resolvedResultDirectory = (Resolve-Path -LiteralPath $resultDirectory).Path
        if ($resolvedResultDirectory -ne $officialResultDirectory) {
            Write-Error "Refusing to delete a non-official evidence directory"
            exit 65
        }
        Remove-Item -LiteralPath $resultDirectory -Recurse -Force
    }
    $jsonLines = Join-Path $resultDirectory "cases.jsonl"
    $summaryJson = Join-Path $resultDirectory "summary.json"
    $summaryMarkdown = Join-Path $resultDirectory "summary.md"
}

New-Item -ItemType Directory -Force -Path $resultDirectory | Out-Null
foreach ($path in @($summaryJson, $summaryMarkdown)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

$pytestExitCode = 0
if (-not $ValidationOnly) {
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
}

$cases = @()
if (Test-Path -LiteralPath $jsonLines) {
    $cases = @(
        Get-Content -LiteralPath $jsonLines -Encoding UTF8 |
            Where-Object { $_.Trim().Length -gt 0 } |
            ForEach-Object { $_ | ConvertFrom-Json }
    )
}

$requiredKeys = @(
    "hard-boundary|startup",
    "hard-boundary|connect",
    "hard-boundary|get",
    "hard-boundary|set",
    "hard-boundary|trigger",
    "hard-boundary|heater_read",
    "hard-boundary|heater_write",
    "hard-boundary|frame_publication",
    "hard-boundary|abort",
    "hard-boundary|disconnect",
    "shared-memory|before_creation",
    "shared-memory|after_creation",
    "shared-memory|after_descriptor_send",
    "shared-memory|after_proxy_copy_before_release_ack"
)
$requiredLookup = @{}
foreach ($key in $requiredKeys) {
    $requiredLookup[$key] = $true
}
$keyCounts = @{}
foreach ($case in $cases) {
    $key = "$($case.kind)|$($case.name)"
    $keyCounts[$key] = 1 + [int]$keyCounts[$key]
}

$violations = [System.Collections.Generic.List[string]]::new()
$counters = [ordered]@{
    manifestViolations = 0
    pytestViolations = 0
    liveChildViolations = 0
    registryLeakViolations = 0
    liveSegmentViolations = 0
    generationInvocationViolations = 0
    elapsedBoundViolations = 0
    connectedReplacementViolations = 0
}
if ($pytestExitCode -ne 0) {
    $violations.Add("pytest exited with code $pytestExitCode")
    $counters.pytestViolations++
}
foreach ($key in $requiredKeys) {
    $count = [int]$keyCounts[$key]
    if ($count -eq 0) {
        $violations.Add("missing required case $key")
        $counters.manifestViolations++
    }
    elseif ($count -gt 1) {
        $violations.Add("duplicate required case $key appears $count times")
        $counters.manifestViolations++
    }
}
foreach ($key in $keyCounts.Keys) {
    if (-not $requiredLookup.ContainsKey($key)) {
        $violations.Add("unexpected case $key")
        $counters.manifestViolations++
    }
}

$evaluatedCases = [System.Collections.Generic.List[object]]::new()
foreach ($case in $cases) {
    $caseViolations = [System.Collections.Generic.List[string]]::new()
    $key = "$($case.kind)|$($case.name)"
    if (-not $requiredLookup.ContainsKey($key)) {
        $caseViolations.Add("unexpected kind/name pair")
    }
    elseif ([int]$keyCounts[$key] -ne 1) {
        $caseViolations.Add("kind/name pair is not unique")
    }

    if ($case.kind -eq "hard-boundary") {
        if ($case.hung_pid_alive -ne $false) {
            $caseViolations.Add("exact child remains alive")
            $counters.liveChildViolations++
        }
        $hungKey = [string]$case.hung_generation
        $replacementKey = [string]$case.replacement_generation
        $countProperties = @(
            $case.invocations_by_generation.PSObject.Properties
        )
        $countKeys = @($countProperties | ForEach-Object { $_.Name })
        $hungProperty = $countProperties |
            Where-Object Name -eq $hungKey |
            Select-Object -First 1
        $replacementProperty = $countProperties |
            Where-Object Name -eq $replacementKey |
            Select-Object -First 1
        $expectedReplacementCount = if ($case.name -eq "startup") { 1 } else { 0 }
        if (
            -not $hungProperty -or
            [int]$hungProperty.Value -ne 1
        ) {
            $caseViolations.Add(
                "failed generation $hungKey invocation count must be exactly 1"
            )
            $counters.generationInvocationViolations++
        }
        if (
            -not $replacementProperty -or
            [int]$replacementProperty.Value -ne $expectedReplacementCount
        ) {
            $caseViolations.Add(
                "replacement generation $replacementKey invocation count must be " +
                "exactly $expectedReplacementCount"
            )
            $counters.generationInvocationViolations++
        }
        $expectedGenerationKeys = @($hungKey, $replacementKey)
        $unexpectedGenerationKeyCount = @(
            $countKeys |
                Where-Object { $_ -notin $expectedGenerationKeys }
        ).Count
        if ($unexpectedGenerationKeyCount -ne 0) {
            $caseViolations.Add("unexpected owner generation invocation evidence")
            $counters.generationInvocationViolations++
        }
        if ([double]$case.elapsed_s -gt [double]$case.hard_bound_s) {
            $caseViolations.Add("elapsed bound exceeded")
            $counters.elapsedBoundViolations++
        }
        if ($case.replacement_connected -ne $false) {
            $caseViolations.Add("replacement owner is connected")
            $counters.connectedReplacementViolations++
        }
    }
    elseif ($case.kind -eq "shared-memory") {
        if ($case.owner_pid_alive -ne $false) {
            $caseViolations.Add("exact child remains alive")
            $counters.liveChildViolations++
        }
        if (@($case.registry_names).Count -ne 0) {
            $caseViolations.Add("orphaned registry entry")
            $counters.registryLeakViolations++
        }
        if (@($case.live_segment_names).Count -ne 0) {
            $caseViolations.Add("live shared-memory segment")
            $counters.liveSegmentViolations++
        }
    }
    foreach ($caseViolation in $caseViolations) {
        $violations.Add("$key`: $caseViolation")
    }
    $evaluatedCases.Add(
        [ordered]@{
            kind = $case.kind
            name = $case.name
            status = if ($caseViolations.Count -eq 0) { "PASS" } else { "FAIL" }
            violations = @($caseViolations)
            evidence = $case
        }
    )
}
foreach ($key in $requiredKeys) {
    if ([int]$keyCounts[$key] -ne 0) {
        continue
    }
    $parts = $key.Split("|", 2)
    $evaluatedCases.Add(
        [ordered]@{
            kind = $parts[0]
            name = $parts[1]
            status = "FAIL"
            violations = @("required case is missing")
            evidence = $null
        }
    )
}

$passed = $violations.Count -eq 0
$summary = [ordered]@{
    schemaVersion = 3
    mode = if ($ValidationOnly) { "validation-only" } else { "official-fresh-audit" }
    auditEligible = -not $ValidationOnly
    generatedUtc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    passed = $passed
    pytestExitCode = $pytestExitCode
    expectedCases = $requiredKeys.Count
    totalCases = $cases.Count
    counters = $counters
    violations = @($violations)
    cases = @($evaluatedCases)
}
$summary | ConvertTo-Json -Depth 10 |
    Set-Content -LiteralPath $summaryJson -Encoding UTF8

$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add($(if ($ValidationOnly) {
        "# Camera Owner Fault Evidence Validation (Not an Audit)"
    } else {
        "# Camera Owner Fault Audit"
    }))
$lines.Add("")
$lines.Add("- Result: " + $(if ($passed) { "PASS" } else { "FAIL" }))
$lines.Add("- Cases: $($cases.Count) / $($requiredKeys.Count)")
$lines.Add("- Manifest violations: $($counters.manifestViolations)")
$lines.Add("- Pytest violations: $($counters.pytestViolations)")
$lines.Add("- Live child violations: $($counters.liveChildViolations)")
$lines.Add("- Registry leak violations: $($counters.registryLeakViolations)")
$lines.Add("- Live segment violations: $($counters.liveSegmentViolations)")
$lines.Add(
    "- Generation invocation violations: " +
    "$($counters.generationInvocationViolations)"
)
$lines.Add("- Hard-bound violations: $($counters.elapsedBoundViolations)")
$lines.Add(
    "- Connected replacement violations: $($counters.connectedReplacementViolations)"
)
$lines.Add("")
$lines.Add("| Kind | Case | Result | Violations |")
$lines.Add("|---|---|---|---|")
foreach ($case in $evaluatedCases) {
    $detail = @($case.violations) -join "; "
    $lines.Add(
        "| $($case.kind) | $($case.name) | $($case.status) | $detail |"
    )
}
if ($violations.Count -gt 0) {
    $lines.Add("")
    $lines.Add("## Violations")
    foreach ($violation in $violations) {
        $lines.Add("- $violation")
    }
}
$lines | Set-Content -LiteralPath $summaryMarkdown -Encoding UTF8

if ($ValidationOnly) {
    Write-Host "Validation-only result written: $summaryJson"
    exit 2
}
if (-not $passed) {
    Write-Error "Camera owner fault audit failed. See $summaryJson"
    exit 1
}
Write-Host "Camera owner fault audit passed: $summaryJson"

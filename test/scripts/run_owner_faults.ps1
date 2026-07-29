[CmdletBinding()]
param(
    [string]$CasesInput = "",
    [string]$OutputDirectory = "",
    [switch]$ValidationOnly
)

$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public static class OwnerFaultPhysicalPath
{
    private const uint FileFlagBackupSemantics = 0x02000000;
    private const uint OpenExisting = 3;
    private const uint ShareRead = 1;
    private const uint ShareWrite = 2;
    private const uint ShareDelete = 4;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(
        SafeFileHandle file,
        StringBuilder path,
        uint pathLength,
        uint flags);

    public static string GetFinalDirectoryPath(string path)
    {
        using (SafeFileHandle handle = CreateFile(
            path,
            0,
            ShareRead | ShareWrite | ShareDelete,
            IntPtr.Zero,
            OpenExisting,
            FileFlagBackupSemantics,
            IntPtr.Zero))
        {
            if (handle.IsInvalid)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }

            StringBuilder buffer = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandle(
                handle,
                buffer,
                (uint)buffer.Capacity,
                0);
            if (length == 0 || length >= buffer.Capacity)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            return buffer.ToString();
        }
    }
}
"@

function Get-PhysicalDirectoryPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $finalPath = [OwnerFaultPhysicalPath]::GetFinalDirectoryPath(
        [System.IO.Path]::GetFullPath($LiteralPath)
    )
    if ($finalPath.StartsWith("\\?\UNC\")) {
        $finalPath = "\\" + $finalPath.Substring(8)
    }
    elseif ($finalPath.StartsWith("\\?\")) {
        $finalPath = $finalPath.Substring(4)
    }
    return [System.IO.Path]::GetFullPath($finalPath).TrimEnd("\")
}

function Assert-OfficialResultDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][string]$ResultDirectory
    )

    $physicalRepositoryRoot = Get-PhysicalDirectoryPath $RepositoryRoot
    $expectedPhysicalDirectory = [System.IO.Path]::GetFullPath(
        (Join-Path $physicalRepositoryRoot "test\results\owner-faults")
    ).TrimEnd("\")
    $expectedPhysicalParent = Split-Path -Parent $expectedPhysicalDirectory
    $physicalParent = Get-PhysicalDirectoryPath (
        Split-Path -Parent $ResultDirectory
    )
    if (
        -not [StringComparer]::OrdinalIgnoreCase.Equals(
            $physicalParent,
            $expectedPhysicalParent
        )
    ) {
        throw "Official evidence parent resolves outside the repository"
    }

    if (-not (Test-Path -LiteralPath $ResultDirectory)) {
        New-Item -ItemType Directory -Path $ResultDirectory | Out-Null
    }

    $directoryInfo = Get-Item -LiteralPath $ResultDirectory -Force
    if (-not $directoryInfo.PSIsContainer) {
        throw "Official evidence path is not a directory"
    }
    if (
        ($directoryInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne
        0
    ) {
        throw "Official evidence directory is a reparse point"
    }

    $physicalResultDirectory = Get-PhysicalDirectoryPath $ResultDirectory
    if (
        -not [StringComparer]::OrdinalIgnoreCase.Equals(
            $physicalResultDirectory,
            $expectedPhysicalDirectory
        )
    ) {
        throw "Official evidence directory resolves outside the repository"
    }
}

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
    if (
        [StringComparer]::OrdinalIgnoreCase.Equals(
            $resultDirectory.TrimEnd("\"),
            $officialResultDirectory.TrimEnd("\")
        )
    ) {
        Write-Error "ValidationOnly cannot target the official result directory"
        exit 64
    }
    $jsonLines = [System.IO.Path]::GetFullPath($CasesInput)
    $summaryJson = Join-Path $resultDirectory "validation-summary.json"
    $summaryMarkdown = Join-Path $resultDirectory "validation-summary.md"
} else {
    $resultDirectory = $officialResultDirectory
    try {
        Assert-OfficialResultDirectory $repoRoot $resultDirectory
    }
    catch {
        Write-Error (
            "Refusing unsafe official evidence directory before cleanup: " +
            $_.Exception.Message
        )
        exit 65
    }
    $jsonLines = Join-Path $resultDirectory "cases.jsonl"
    $summaryJson = Join-Path $resultDirectory "summary.json"
    $summaryMarkdown = Join-Path $resultDirectory "summary.md"
}

New-Item -ItemType Directory -Force -Path $resultDirectory | Out-Null
$generatedPathsToRemove = @($summaryJson, $summaryMarkdown)
if (-not $ValidationOnly) {
    $generatedPathsToRemove = @($jsonLines) + $generatedPathsToRemove
}
foreach ($path in $generatedPathsToRemove) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

$pytestExitCode = 0
if (-not $ValidationOnly) {
    $env:TOUPTEK_OWNER_FAULT_RESULTS = $jsonLines
    $env:TOUPTEK_OWNER_FAULT_AUDIT_ACTIVE = "1"
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
        Remove-Item Env:TOUPTEK_OWNER_FAULT_AUDIT_ACTIVE -ErrorAction SilentlyContinue
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

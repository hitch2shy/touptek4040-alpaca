param(
    [Parameter(Mandatory = $true)]
    [string]$DistDir
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    $listener.Start()
    try {
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Invoke-BoundedRecovery {
    param(
        [string]$Label,
        [string]$Python,
        [string]$Repository,
        [string]$Results
    )
    $stdout = [System.IO.Path]::Combine($Results, "recovery-$Label.stdout.txt")
    $stderr = [System.IO.Path]::Combine($Results, "recovery-$Label.stderr.txt")
    $recovery = [System.IO.Path]::Combine(
        $Repository,
        "test\scripts\recover_hardware.py"
    )
    $startedUtc = [System.DateTime]::UtcNow
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList @($recovery) `
        -WorkingDirectory $Repository `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    if (-not $process.WaitForExit(45000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $process.WaitForExit()
        throw "Recovery $Label exceeded 45 seconds; exact PID $($process.Id) terminated."
    }
    $process.WaitForExit()
    $process.Refresh()
    $exitCode = $process.ExitCode
    if ($null -ne $exitCode -and $exitCode -ne 0) {
        throw "Recovery $Label failed with exit code $exitCode. See $stderr"
    }
    $source = [System.IO.Path]::Combine(
        $Repository,
        "test\results\hardware\recovery.json"
    )
    if (
        -not [System.IO.File]::Exists($source) -or
        [System.IO.File]::GetLastWriteTimeUtc($source) -lt $startedUtc
    ) {
        throw "Recovery $Label did not produce fresh result evidence."
    }
    $destination = [System.IO.Path]::Combine(
        $Results,
        "recovery-$Label.json"
    )
    Copy-Item -LiteralPath $source -Destination $destination -Force
    $record = Get-Content -LiteralPath $destination -Raw | ConvertFrom-Json
    if (-not $record.restored -or -not $record.closed -or $record.errors.Count -ne 0) {
        throw "Recovery $Label did not restore and close the approved camera."
    }
    return $record
}

function Invoke-WebJson {
    param(
        [string]$BaseUrl,
        [string]$Path,
        [object]$Body = $null,
        [int]$TimeoutSeconds = 5
    )
    if ($null -eq $Body) {
        return Invoke-RestMethod `
            -Uri ($BaseUrl + $Path) `
            -Method Get `
            -TimeoutSec $TimeoutSeconds
    }
    $headers = @{
        Origin = $BaseUrl
        "X-ToupTek-Console" = "1"
    }
    return Invoke-RestMethod `
        -Uri ($BaseUrl + $Path) `
        -Method Post `
        -Headers $headers `
        -ContentType "application/json" `
        -Body ($Body | ConvertTo-Json -Depth 8 -Compress) `
        -TimeoutSec $TimeoutSeconds
}

function Wait-WebState {
    param(
        [string]$BaseUrl,
        [scriptblock]$Predicate,
        [int]$TimeoutSeconds
    )
    $deadline = [System.DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $last = $null
    while ([System.DateTime]::UtcNow -lt $deadline) {
        $last = Invoke-WebJson -BaseUrl $BaseUrl -Path "/web/v1/state"
        if (& $Predicate $last) {
            return $last
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Packaged camera state did not converge: $($last | ConvertTo-Json -Depth 8 -Compress)"
}

$repositoryRoot = (& git rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($repositoryRoot)) {
    throw "Could not resolve the Git repository root."
}
$repositoryRoot = [System.IO.Path]::GetFullPath($repositoryRoot)
$repositoryPrefix = $repositoryRoot.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
$resolvedDist = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($repositoryRoot, $DistDir)
)
if (-not $resolvedDist.StartsWith(
    $repositoryPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Distribution must be inside the repository: $resolvedDist"
}

$executable = [System.IO.Path]::Combine(
    $resolvedDist,
    "ToupTek4040Alpaca.exe"
)
$python = [System.IO.Path]::Combine(
    $repositoryRoot,
    ".venv64\Scripts\python.exe"
)
foreach ($required in @($executable, $python)) {
    if (-not [System.IO.File]::Exists($required)) {
        throw "Required hardware-test input is missing: $required"
    }
}

$resultsDirectory = [System.IO.Path]::Combine(
    $repositoryRoot,
    "test\results\temporary-package"
)
[System.IO.Directory]::CreateDirectory($resultsDirectory) | Out-Null
$logDirectory = [System.IO.Path]::Combine($resultsDirectory, "hardware-logs")
[System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
$serverStdout = [System.IO.Path]::Combine($resultsDirectory, "packaged-hardware.stdout.txt")
$serverStderr = [System.IO.Path]::Combine($resultsDirectory, "packaged-hardware.stderr.txt")
$resultPath = [System.IO.Path]::Combine($resultsDirectory, "hardware.json")
$utf8 = [System.Text.UTF8Encoding]::new($false)

$record = [ordered]@{
    designation = "TEMPORARY TEST BUILD"
    model = "sMAX16AM-U3-CL-GPS"
    serial = "TP241025143218C92B01151AEC40CB7"
    process_id = $null
    port = $null
    connected = $false
    exposure_requested = $false
    image = $null
    disconnected = $false
    exact_process_stopped = $false
    recovery_before = $null
    recovery_after = $null
    errors = @()
}
$server = $null

try {
    $record.recovery_before = Invoke-BoundedRecovery `
        -Label "before" `
        -Python $python `
        -Repository $repositoryRoot `
        -Results $resultsDirectory

    $port = Get-FreeTcpPort
    $discoveryPort = Get-FreeTcpPort
    $record.port = $port
    $baseUrl = "http://127.0.0.1:$port"
    $server = Start-Process `
        -FilePath $executable `
        -ArgumentList @(
            "--host", "127.0.0.1",
            "--port", "$port",
            "--discovery-port", "$discoveryPort",
            "--log-directory", $logDirectory
        ) `
        -WorkingDirectory $resolvedDist `
        -WindowStyle Hidden `
        -RedirectStandardOutput $serverStdout `
        -RedirectStandardError $serverStderr `
        -PassThru
    $record.process_id = $server.Id

    $startupDeadline = [System.DateTime]::UtcNow.AddSeconds(30)
    while ([System.DateTime]::UtcNow -lt $startupDeadline -and -not $server.HasExited) {
        try {
            $description = Invoke-WebJson `
                -BaseUrl $baseUrl `
                -Path "/management/v1/description" `
                -TimeoutSeconds 2
            if ($null -ne $description) {
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if ($server.HasExited) {
        throw "Packaged server exited during startup with code $($server.ExitCode)."
    }

    Invoke-WebJson `
        -BaseUrl $baseUrl `
        -Path "/web/v1/connection" `
        -Body @{ connected = $true } | Out-Null
    Wait-WebState `
        -BaseUrl $baseUrl `
        -TimeoutSeconds 25 `
        -Predicate { param($state) $state.connected -eq $true } | Out-Null
    $record.connected = $true

    Invoke-WebJson `
        -BaseUrl $baseUrl `
        -Path "/web/v1/settings" `
        -Body @{
            binning = 1
            readoutMode = "RAW 12-bit"
            gain = 100
            roi = @{ x = 0; y = 0; width = 16; height = 16 }
        } | Out-Null
    Invoke-WebJson `
        -BaseUrl $baseUrl `
        -Path "/web/v1/exposure" `
        -Body @{ duration = 0.01 } | Out-Null
    $record.exposure_requested = $true

    $state = Wait-WebState `
        -BaseUrl $baseUrl `
        -TimeoutSeconds 25 `
        -Predicate { param($current) $null -ne $current.image }
    if (
        $state.image.width -ne 16 -or
        $state.image.height -ne 16 -or
        $state.image.readoutMode -ne "RAW 12-bit"
    ) {
        throw "Packaged hardware image metadata was unexpected: $($state.image | ConvertTo-Json -Compress)"
    }
    $record.image = [ordered]@{
        width = [int]$state.image.width
        height = [int]$state.image.height
        readout_mode = [string]$state.image.readoutMode
        max_adu = [int]$state.image.maxADU
        generation = [int]$state.generation
    }

    Invoke-WebJson `
        -BaseUrl $baseUrl `
        -Path "/web/v1/connection" `
        -Body @{ connected = $false } | Out-Null
    Wait-WebState `
        -BaseUrl $baseUrl `
        -TimeoutSeconds 15 `
        -Predicate { param($current) $current.connected -eq $false } | Out-Null
    $record.disconnected = $true
}
catch {
    $record.errors += $_.Exception.Message
}
finally {
    if ($null -ne $server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        if (-not $server.WaitForExit(10000)) {
            $record.errors += "Exact packaged process did not exit within 10 seconds."
        }
    }
    $record.exact_process_stopped = (
        $null -eq $server -or $server.HasExited
    )
    try {
        $record.recovery_after = Invoke-BoundedRecovery `
            -Label "after" `
            -Python $python `
            -Repository $repositoryRoot `
            -Results $resultsDirectory
    }
    catch {
        $record.errors += $_.Exception.Message
    }
    [System.IO.File]::WriteAllText(
        $resultPath,
        ($record | ConvertTo-Json -Depth 16) + "`n",
        $utf8
    )
}

if (
    $record.errors.Count -ne 0 -or
    -not $record.connected -or
    -not $record.exposure_requested -or
    $null -eq $record.image -or
    -not $record.disconnected -or
    -not $record.exact_process_stopped -or
    -not $record.recovery_after.restored -or
    -not $record.recovery_after.closed
) {
    throw "Packaged hardware verification failed. See $resultPath"
}

Write-Output "Packaged hardware verification passed: $resultPath"

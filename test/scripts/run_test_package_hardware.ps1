param(
    [Parameter(Mandatory = $true)]
    [string]$DistDir,
    [string]$ResultsDir = "test\results\temporary-package",
    [string]$Designation = "TEMPORARY TEST BUILD",
    [switch]$VerifyFormalContract,
    [switch]$SkipRecovery
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

function Wait-OwnerChild {
    param(
        [int]$ParentPid,
        [int]$TimeoutSeconds = 10
    )
    $deadline = [System.DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([System.DateTime]::UtcNow -lt $deadline) {
        $candidate = Get-CimInstance Win32_Process |
            Where-Object {
                $_.ParentProcessId -eq $ParentPid -and
                $_.CommandLine -like "*--multiprocessing-fork*"
            } |
            Select-Object -First 1
        if ($null -ne $candidate) { return $candidate }
        Start-Sleep -Milliseconds 100
    }
    throw "Packaged owner child did not appear below PID $ParentPid."
}

function Get-OwnerChildPids {
    param([int]$ParentPid)
    return @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.ParentProcessId -eq $ParentPid -and
                $_.CommandLine -like "*--multiprocessing-fork*"
            } |
            ForEach-Object { [int]$_.ProcessId } |
            Sort-Object -Unique
    )
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

$resultsDirectory = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($repositoryRoot, $ResultsDir)
)
if (-not $resultsDirectory.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Results must remain inside the repository: $resultsDirectory"
}
[System.IO.Directory]::CreateDirectory($resultsDirectory) | Out-Null
$logDirectory = [System.IO.Path]::Combine($resultsDirectory, "hardware-logs")
[System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
$serverStdout = [System.IO.Path]::Combine($resultsDirectory, "packaged-hardware.stdout.txt")
$serverStderr = [System.IO.Path]::Combine($resultsDirectory, "packaged-hardware.stderr.txt")
$resultPath = [System.IO.Path]::Combine($resultsDirectory, "hardware.json")
$registryPath = [System.IO.Path]::Combine(
    $logDirectory,
    "touptek4040-shared-frames.json"
)
$observerOutput = [System.IO.Path]::Combine(
    $resultsDirectory,
    "observed-shared-memory.json"
)
$observerStop = [System.IO.Path]::Combine(
    $resultsDirectory,
    "observe-shared-memory.stop"
)
$probeOutput = [System.IO.Path]::Combine(
    $resultsDirectory,
    "probed-shared-memory.json"
)
$utf8 = [System.Text.UTF8Encoding]::new($false)

$record = [ordered]@{
    designation = $Designation
    model = "sMAX16AM-U3-CL-GPS"
    serial = "TP241025143218C92B01151AEC40CB7"
    process_id = $null
    port = $null
    connected = $false
    exposure_requested = $false
    image = $null
    image_bytes = $false
    image_json = $false
    png = $false
    fits = $false
    abort = $false
    heater = $null
    owner = $null
    shared_memory = $null
    disconnected = $false
    exact_process_stopped = $false
    recovery_before = $null
    recovery_after = $null
    errors = @()
}
$server = $null
$observer = $null

try {
    if (-not $SkipRecovery) {
        $record.recovery_before = Invoke-BoundedRecovery `
            -Label "before" `
            -Python $python `
            -Repository $repositoryRoot `
            -Results $resultsDirectory
    }

    if ($VerifyFormalContract) {
        Remove-Item -LiteralPath @(
            $registryPath,
            $observerOutput,
            $observerStop,
            $probeOutput
        ) -Force -ErrorAction SilentlyContinue
        $observer = Start-Process `
            -FilePath $python `
            -ArgumentList @(
                "test/scripts/observe_shared_memory.py",
                "--registry", $registryPath,
                "--output", $observerOutput,
                "--stop-file", $observerStop
            ) `
            -WorkingDirectory $repositoryRoot `
            -WindowStyle Hidden `
            -PassThru
        $observerDeadline = [System.DateTime]::UtcNow.AddSeconds(10)
        while (
            -not [System.IO.File]::Exists($observerOutput) -and
            [System.DateTime]::UtcNow -lt $observerDeadline -and
            -not $observer.HasExited
        ) { Start-Sleep -Milliseconds 20 }
        if (-not [System.IO.File]::Exists($observerOutput)) {
            throw "Shared-memory registry observer did not become ready."
        }
    }

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
    if ($VerifyFormalContract) {
        $ownerChild = Wait-OwnerChild -ParentPid $server.Id
        $record.owner = [ordered]@{
            child_pid = [int]$ownerChild.ProcessId
            child_pids_observed = @([int]$ownerChild.ProcessId)
            child_alive_while_connected = $true
            child_alive_after_disconnect = $null
            child_pid_after_disconnect = $null
            child_pid_after_reconnect = $null
            child_pids_alive_after_package_shutdown = @()
            child_dead_after_package_shutdown = $false
        }
        Invoke-WebJson `
            -BaseUrl $baseUrl `
            -Path "/web/v1/settings" `
            -Body @{
                binning = 1
                readoutMode = "RAW 12-bit"
                gain = 100
                roi = @{ x = 0; y = 0; width = 4096; height = 4096 }
            } | Out-Null
        Invoke-WebJson `
            -BaseUrl $baseUrl `
            -Path "/web/v1/exposure" `
            -Body @{ duration = 0.01 } | Out-Null
        $observedState = Wait-WebState `
            -BaseUrl $baseUrl `
            -TimeoutSeconds 25 `
            -Predicate {
                param($current)
                $null -ne $current.image -and
                $current.image.width -eq 4096 -and
                $current.image.height -eq 4096
            }
        $observedGeneration = [int]$observedState.generation
    }

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
        -Predicate {
            param($current)
            $null -ne $current.image -and
            $current.image.width -eq 16 -and
            $current.image.height -eq 16 -and
            (
                -not $VerifyFormalContract -or
                [int]$current.generation -ne $observedGeneration
            )
        }
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

    if ($VerifyFormalContract) {
        $generation = [int]$state.generation
        $png = Invoke-WebRequest -UseBasicParsing -Uri (
            "$baseUrl/web/v1/viewport.png?generation=$generation"
        ) -TimeoutSec 10
        $fits = Invoke-WebRequest -UseBasicParsing -Uri (
            "$baseUrl/web/v1/image.fits?generation=$generation"
        ) -TimeoutSec 10
        if (-not ($png.Headers["Content-Type"] -like "image/png*")) {
            throw "Packaged PNG response had unexpected content type."
        }
        if (-not ($fits.Headers["Content-Type"] -like "image/fits*")) {
            throw "Packaged FITS response had unexpected content type."
        }
        $record.png = $true
        $record.fits = $true

        $imageUrl = "$baseUrl/api/v1/camera/0/imagearray?ClientID=1&ClientTransactionID=1"
        $jsonImage = Invoke-RestMethod -Uri $imageUrl -Method Get -TimeoutSec 10
        if ($jsonImage.ErrorNumber -ne 0 -or $null -eq $jsonImage.Value) {
            throw "Packaged JSON ImageArray failed."
        }
        $record.image_json = $true
        $binaryImage = Invoke-WebRequest -UseBasicParsing -Uri $imageUrl `
            -Headers @{ Accept = "application/imagebytes" } -TimeoutSec 10
        if (-not ($binaryImage.Headers["Content-Type"] -like "application/imagebytes*")) {
            throw "Packaged ImageBytes response had unexpected content type."
        }
        $record.image_bytes = $true

        $heaterState = Invoke-WebJson -BaseUrl $baseUrl -Path "/web/v1/state"
        if (-not $heaterState.heaterSupported) {
            throw "Approved camera did not report Web heater support."
        }
        $initialHeat = [int]$heaterState.heaterLevel
        $maximumHeat = [int]$heaterState.heaterMaxLevel
        $alternateHeat = if ($initialHeat -eq 0) { 1 } else { 0 }
        if ($alternateHeat -gt $maximumHeat) { throw "No valid alternate heater level." }
        Invoke-WebJson -BaseUrl $baseUrl -Path "/web/v1/heater" -Body @{
            heaterLevel = $alternateHeat
        } | Out-Null
        $alternateState = Invoke-WebJson -BaseUrl $baseUrl -Path "/web/v1/state"
        if ([int]$alternateState.heaterLevel -ne $alternateHeat) {
            throw "Packaged Web heater alternate level was not confirmed."
        }
        $record.heater = [ordered]@{
            initial = $initialHeat
            maximum = $maximumHeat
            alternate = $alternateHeat
            alternate_left_active = $true
            manual_restore_before_disconnect = $false
            restored_by_disconnect = $false
            reconnect_confirmed_initial = $false
            restored = $null
        }

        $abortBody = "Duration=1&Light=true&ClientID=1&ClientTransactionID=2"
        Invoke-RestMethod -Uri "$baseUrl/api/v1/camera/0/startexposure" `
            -Method Put -ContentType "application/x-www-form-urlencoded" `
            -Body $abortBody -TimeoutSec 10 | Out-Null
        $abortBody = "ClientID=1&ClientTransactionID=3"
        $abortResult = Invoke-RestMethod -Uri "$baseUrl/api/v1/camera/0/abortexposure" `
            -Method Put -ContentType "application/x-www-form-urlencoded" `
            -Body $abortBody -TimeoutSec 10
        if ($abortResult.ErrorNumber -ne 0) { throw "Packaged AbortExposure failed." }
        $record.abort = $true
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
    if ($VerifyFormalContract) {
        $ownerPid = [int]$record.owner.child_pid
        $record.owner.child_alive_after_disconnect = (
            $null -ne (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue)
        )
        $afterDisconnectPids = @(Get-OwnerChildPids -ParentPid $server.Id)
        if ($afterDisconnectPids.Count -gt 0) {
            $record.owner.child_pid_after_disconnect = $afterDisconnectPids[0]
            $record.owner.child_pids_observed = @(
                $record.owner.child_pids_observed + $afterDisconnectPids |
                    Sort-Object -Unique
            )
        }
        Invoke-WebJson `
            -BaseUrl $baseUrl `
            -Path "/web/v1/connection" `
            -Body @{ connected = $true } | Out-Null
        $reconnectedState = Wait-WebState `
            -BaseUrl $baseUrl `
            -TimeoutSeconds 25 `
            -Predicate { param($current) $current.connected -eq $true }
        $reconnectedOwner = Wait-OwnerChild -ParentPid $server.Id
        $record.owner.child_pid_after_reconnect = [int]$reconnectedOwner.ProcessId
        $record.owner.child_pids_observed = @(
            $record.owner.child_pids_observed +
                [int]$reconnectedOwner.ProcessId |
                Sort-Object -Unique
        )
        $restoredHeat = [int]$reconnectedState.heaterLevel
        $record.heater.restored = $restoredHeat
        $record.heater.restored_by_disconnect = ($restoredHeat -eq $initialHeat)
        $record.heater.reconnect_confirmed_initial = ($restoredHeat -eq $initialHeat)
        if ($restoredHeat -ne $initialHeat) {
            # Safety-only repair. Do not perform a normal Disconnect afterwards:
            # its bad connection snapshot would restore the alternate level.
            Invoke-WebJson -BaseUrl $baseUrl -Path "/web/v1/heater" -Body @{
                heaterLevel = $initialHeat
            } | Out-Null
            throw "Disconnect failed to restore the packaged Web heater automatically."
        }
        Invoke-WebJson `
            -BaseUrl $baseUrl `
            -Path "/web/v1/connection" `
            -Body @{ connected = $false } | Out-Null
        Wait-WebState `
            -BaseUrl $baseUrl `
            -TimeoutSeconds 15 `
            -Predicate { param($current) $current.connected -eq $false } | Out-Null
    }
}
catch {
    $record.errors += $_.Exception.Message
}
finally {
    if (
        $VerifyFormalContract -and
        $null -ne $server -and
        -not $server.HasExited -and
        $null -ne $record.owner
    ) {
        $record.owner.child_pids_observed = @(
            $record.owner.child_pids_observed +
                @(Get-OwnerChildPids -ParentPid $server.Id) |
                Sort-Object -Unique
        )
    }
    if ($null -ne $server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        if (-not $server.WaitForExit(10000)) {
            $record.errors += "Exact packaged process did not exit within 10 seconds."
        }
    }
    $record.exact_process_stopped = (
        $null -eq $server -or $server.HasExited
    )
    if ($VerifyFormalContract -and $null -ne $observer) {
        New-Item -ItemType File -Path $observerStop -Force | Out-Null
        if (-not $observer.WaitForExit(10000)) {
            Stop-Process -Id $observer.Id -Force -ErrorAction SilentlyContinue
            $record.errors += "Shared-memory registry observer did not stop."
        }
    }
    if ($VerifyFormalContract -and $null -ne $record.owner) {
        $aliveOwnerPids = @()
        foreach ($ownerPid in @($record.owner.child_pids_observed)) {
            $ownerDeadline = [System.DateTime]::UtcNow.AddSeconds(10)
            while (
                [System.DateTime]::UtcNow -lt $ownerDeadline -and
                $null -ne (
                    Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
                )
            ) { Start-Sleep -Milliseconds 100 }
            if (
                $null -ne (
                    Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
                )
            ) { $aliveOwnerPids += [int]$ownerPid }
        }
        $record.owner.child_pids_alive_after_package_shutdown = @(
            $aliveOwnerPids
        )
        $record.owner.child_dead_after_package_shutdown = (
            $aliveOwnerPids.Count -eq 0
        )
        $observedNames = @()
        if ([System.IO.File]::Exists($observerOutput)) {
            $observation = Get-Content -LiteralPath $observerOutput -Raw |
                ConvertFrom-Json
            $observedNames = @($observation.observed_names)
        }
        $registryNames = @()
        if ([System.IO.File]::Exists($registryPath)) {
            $registry = Get-Content -LiteralPath $registryPath -Raw |
                ConvertFrom-Json
            $registryNames = @($registry.frames | ForEach-Object { $_.name })
        }
        $probedNames = @()
        $liveSegmentNames = @()
        if ([System.IO.File]::Exists($observerOutput)) {
            & $python "test/scripts/probe_shared_memory_names.py" `
                --observed $observerOutput `
                --result $probeOutput
            if ($LASTEXITCODE -ne 0) {
                $record.errors += "Shared-memory reopen probe failed."
            }
            elseif ([System.IO.File]::Exists($probeOutput)) {
                $probe = Get-Content -LiteralPath $probeOutput -Raw |
                    ConvertFrom-Json
                $probedNames = @($probe.probed_names)
                $liveSegmentNames = @($probe.live_segment_names)
            }
        }
        else {
            $record.errors += "Shared-memory observation evidence is missing."
        }
        $record.shared_memory = [ordered]@{
            observed_names = @($observedNames)
            probed_names = @($probedNames)
            registry_names = @($registryNames)
            live_segment_names = @($liveSegmentNames)
        }
    }
    if (-not $SkipRecovery) {
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
    ($VerifyFormalContract -and (
        -not $record.image_bytes -or -not $record.image_json -or
        -not $record.png -or -not $record.fits -or -not $record.abort -or
        $null -eq $record.heater -or
        -not $record.heater.restored_by_disconnect -or
        -not $record.heater.reconnect_confirmed_initial -or
        $record.heater.manual_restore_before_disconnect -or
        -not $record.heater.alternate_left_active -or
        $record.owner.child_pids_observed.Count -eq 0 -or
        $null -eq $record.owner.child_pid_after_reconnect -or
        $record.owner.child_pids_alive_after_package_shutdown.Count -ne 0 -or
        -not $record.owner.child_dead_after_package_shutdown -or
        $record.shared_memory.observed_names.Count -eq 0 -or
        @(Compare-Object `
            $record.shared_memory.observed_names `
            $record.shared_memory.probed_names).Count -ne 0 -or
        $record.shared_memory.registry_names.Count -ne 0 -or
        $record.shared_memory.live_segment_names.Count -ne 0
    )) -or
    -not $record.disconnected -or
    -not $record.exact_process_stopped -or
    (-not $SkipRecovery -and (
        -not $record.recovery_after.restored -or
        -not $record.recovery_after.closed
    ))
) {
    throw "Packaged hardware verification failed. See $resultPath"
}

Write-Output "Packaged hardware verification passed: $resultPath"

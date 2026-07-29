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

$script:conformExitCodes = @{}

function Invoke-ConformProfile {
    param(
        [string]$ProfileName,
        [string]$Command,
        [int]$ProfilePort
    )

    $stopFile = Join-Path $results "$ProfileName-stop.request"
    $readyFile = Join-Path $results "$ProfileName-ready.txt"
    Remove-Item -LiteralPath $stopFile,$readyFile -Force -ErrorAction SilentlyContinue
    $stdout = Join-Path $results "$ProfileName-server-stdout.txt"
    $stderr = Join-Path $results "$ProfileName-server-stderr.txt"
    $uri = "http://127.0.0.1:$ProfilePort/api/v1/camera/0"
    $server = $null

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
        $script:conformExitCodes[$ProfileName] = $LASTEXITCODE
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
    Invoke-ConformProfile "protocol" "alpacaprotocol" $Port
    Invoke-ConformProfile "camera-v4" "conformance" ($Port + 1)
}
finally {
    & $Python test/scripts/summarize_conformu.py
    Pop-Location
}

$protocolExit = $script:conformExitCodes["protocol"]
$cameraExit = $script:conformExitCodes["camera-v4"]
if ($protocolExit -ne 0 -or $cameraExit -ne 0) {
    throw "ConformU returned protocol=$protocolExit camera=$cameraExit"
}

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$pidFile = Join-Path $logs "runner.pid"
$outFile = Join-Path $logs "runner.out.log"
$errFile = Join-Path $logs "runner.err.log"

if (Test-Path $pidFile) {
    $existingPid = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($existingPid -and (Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue)) {
        Write-Host "Paper loop already running with PID $existingPid"
        exit 0
    }
}

$process = Start-Process `
    -FilePath "py" `
    -ArgumentList @("-u", "-m", "ai_robot.runner", "loop", "--config", "config.paper.json") `
    -WorkingDirectory $root `
    -RedirectStandardOutput $outFile `
    -RedirectStandardError $errFile `
    -WindowStyle Hidden `
    -PassThru

$process.Id | Set-Content -Path $pidFile
Write-Host "Started paper loop with PID $($process.Id)"
Write-Host "Output: $outFile"
Write-Host "Errors: $errFile"

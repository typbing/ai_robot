$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $root "logs\runner.pid"

if (-not (Test-Path $pidFile)) {
    Write-Host "No runner.pid file found. Paper loop is not recorded as running."
    exit 0
}

$runnerPid = Get-Content $pidFile -ErrorAction SilentlyContinue
if (-not $runnerPid) {
    Remove-Item $pidFile -Force
    Write-Host "Empty runner.pid removed."
    exit 0
}

$process = Get-Process -Id ([int]$runnerPid) -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id ([int]$runnerPid)
    Write-Host "Stopped paper loop PID $runnerPid"
} else {
    Write-Host "PID $runnerPid is not running."
}

Remove-Item $pidFile -Force

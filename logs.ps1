param(
    [switch]$Err,
    [switch]$Follow,
    [int]$Tail = 50
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logFile = if ($Err) {
    Join-Path $root "bot.err.log"
} else {
    Join-Path $root "bot.log"
}

if (-not (Test-Path $logFile)) {
    Write-Host "Log file not found: $logFile"
    exit 1
}

if ($Follow) {
    Get-Content -Path $logFile -Tail $Tail -Wait
    exit 0
}

Get-Content -Path $logFile -Tail $Tail

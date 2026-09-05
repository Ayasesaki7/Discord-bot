$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$pidFile = Join-Path $root "bot.pid"

function Get-BotPythonProcesses {
    Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
        Where-Object {
            $_.CommandLine -match '(^|\s)-u\s+bot\.py(\s|$)' -or
            $_.CommandLine -match '(^|\s)bot\.py(\s|$)'
        }
}

$stopped = @()

if (Test-Path $pidFile) {
    $rawPid = (Get-Content $pidFile | Select-Object -First 1).Trim()
    if ($rawPid) {
        $pidValue = [int]$rawPid
        $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue

        if ($null -ne $process) {
            Stop-Process -Id $pidValue -Force
            $stopped += $pidValue
            Write-Host "Bot stopped. PID: $pidValue"
        } else {
            Write-Host "Process $pidValue is not running anymore. bot.pid has been cleaned up."
        }
    } else {
        Write-Host "bot.pid was empty and has been removed."
    }
} else {
    Write-Host "bot.pid was not found; scanning for orphaned bot.py processes."
}

$orphanedBotProcesses = @(Get-BotPythonProcesses | Where-Object { $stopped -notcontains $_.ProcessId })
foreach ($processInfo in $orphanedBotProcesses) {
    Stop-Process -Id $processInfo.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped += $processInfo.ProcessId
    Write-Host "Stopped orphaned bot process. PID: $($processInfo.ProcessId)"
}

if ($stopped.Count -eq 0) {
    Write-Host "No running bot process was found."
}

Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue

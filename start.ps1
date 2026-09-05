param(
    [switch]$Foreground,
    [switch]$Install
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$pidFile = Join-Path $root "bot.pid"
$stdoutLog = Join-Path $root "bot.log"
$stderrLog = Join-Path $root "bot.err.log"

function Test-Python312 {
    param(
        [string]$FilePath
    )

    & $FilePath -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Get-Python312 {
    if ($env:BOT_PYTHON) {
        if ((Test-Path -LiteralPath $env:BOT_PYTHON) -and (Test-Python312 $env:BOT_PYTHON)) {
            return $env:BOT_PYTHON
        }
        Write-Host "BOT_PYTHON is set but is not Python 3.12: $env:BOT_PYTHON"
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $candidate = & py "-3.12" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidate) {
            return $candidate.Trim()
        }
    }

    $commonPaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Python312\python.exe")
    )
    foreach ($path in $commonPaths) {
        if ($path -and (Test-Path -LiteralPath $path) -and (Test-Python312 $path)) {
            return $path
        }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand -and (Test-Python312 $pythonCommand.Source)) {
        return $pythonCommand.Source
    }

    return $null
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Get-MissingPythonModules {
    param(
        [string]$FilePath
    )

    $script = @"
import importlib.util
modules = [
    "discord",
    "dotenv",
    "aiohttp",
    "certifi",
    "yt_dlp",
    "f2",
    "imageio",
    "imageio_ffmpeg",
    "PIL",
    "pypdf",
    "pypdfium2",
    "nacl",
    "davey",
    "tzdata",
]
print(",".join(name for name in modules if importlib.util.find_spec(name) is None))
"@
    $result = & $FilePath -c $script
    if ($LASTEXITCODE -ne 0) {
        return @("dependency-check-failed")
    }
    if (-not $result) {
        return @()
    }
    return @($result.Split(",") | Where-Object { $_ })
}

function Install-Python312 {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Python 3.12 was not found and winget is unavailable. Install Python 3.12 manually from https://www.python.org/downloads/release/python-312/."
    }

    Write-Host "Python 3.12 was not found. Installing Python 3.12 with winget..."
    $installArgs = @(
        "install",
        "--id", "Python.Python.3.12",
        "--exact",
        "--scope", "user",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements"
    )

    & $winget.Source @installArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "User-scope install failed; retrying without --scope..."
        $installArgs = @(
            "install",
            "--id", "Python.Python.3.12",
            "--exact",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements"
        )
        Invoke-Checked $winget.Source $installArgs
    }
}

$python = Get-Python312
if (-not $python) {
    Install-Python312
    $python = Get-Python312
}
if (-not $python) {
    throw "Python 3.12 installation completed, but start.ps1 still could not find Python 3.12. Open a new terminal or set BOT_PYTHON to the Python 3.12 python.exe path."
}
Write-Host "Using Python: $python"

if ($Install) {
    $missingModules = Get-MissingPythonModules $python
    if ($missingModules.Count -gt 0) {
        Write-Host "Missing Python modules in Python 3.12: $($missingModules -join ', ')"
    }
    Write-Host "Installing/updating Python dependencies..."
    Invoke-Checked $python @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked $python @(
        "-m", "pip", "install",
        "discord.py",
        "python-dotenv",
        "aiohttp",
        "certifi",
        "yt-dlp",
        "f2",
        "imageio",
        "imageio-ffmpeg",
        "pillow",
        "pypdf",
        "pypdfium2",
        "pynacl",
        "davey",
        "tzdata"
    )
}

function Get-RunningBotProcess {
    param(
        [string]$PidPath
    )

    if (-not (Test-Path $PidPath)) {
        return $null
    }

    $rawPid = (Get-Content $PidPath | Select-Object -First 1).Trim()
    if (-not $rawPid) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        return $null
    }

    try {
        return Get-Process -Id ([int]$rawPid) -ErrorAction Stop
    } catch {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        return $null
    }
}

function Get-BotPythonProcesses {
    Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
        Where-Object {
            $_.CommandLine -match '(^|\s)-u\s+bot\.py(\s|$)' -or
            $_.CommandLine -match '(^|\s)bot\.py(\s|$)'
        }
}

function Stop-DuplicateBotProcesses {
    param(
        [int]$KeepProcessId
    )

    $duplicates = @(Get-BotPythonProcesses | Where-Object { $_.ProcessId -ne $KeepProcessId })
    foreach ($duplicate in $duplicates) {
        Write-Host "Stopping duplicate bot process. PID: $($duplicate.ProcessId)"
        Stop-Process -Id $duplicate.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

$existing = Get-RunningBotProcess -PidPath $pidFile
if ($existing) {
    Stop-DuplicateBotProcesses -KeepProcessId $existing.Id
    Write-Host "Bot is already running. PID: $($existing.Id)"
    Write-Host "Log file: $stdoutLog"
    exit 0
}

$orphanedBotProcesses = @(Get-BotPythonProcesses)
if ($orphanedBotProcesses.Count -gt 0) {
    $primary = $orphanedBotProcesses[0]
    Set-Content -Path $pidFile -Value $primary.ProcessId -Encoding utf8
    Stop-DuplicateBotProcesses -KeepProcessId $primary.ProcessId
    Write-Host "Bot is already running without a pid file. Reattached PID: $($primary.ProcessId)"
    Write-Host "Log file: $stdoutLog"
    exit 0
}

if ($Foreground) {
    Write-Host "Starting bot in foreground..."
    & $python -u bot.py
    exit $LASTEXITCODE
}

if (Test-Path $stdoutLog) {
    Remove-Item -LiteralPath $stdoutLog -Force
}
if (Test-Path $stderrLog) {
    Remove-Item -LiteralPath $stderrLog -Force
}

$proc = Start-Process `
    -FilePath $python `
    -ArgumentList "-u", "bot.py" `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Set-Content -Path $pidFile -Value $proc.Id -Encoding utf8
Start-Sleep -Seconds 2

$running = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
if ($null -eq $running) {
    Write-Error "Bot failed to start. Check $stderrLog"
    exit 1
}

Write-Host "Bot started. PID: $($proc.Id)"
Write-Host "Log file: $stdoutLog"

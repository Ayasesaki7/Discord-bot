$ErrorActionPreference = 'Stop'

$runtimeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$nodeVersionText = (& node --version).Trim().TrimStart('v')
$nodeVersion = [version]$nodeVersionText
if ($nodeVersion -lt [version]'22.19.0') {
    throw "DeepSeek Harness requires Node.js 22.19 or newer; found $nodeVersionText"
}

Push-Location $runtimeRoot
try {
    npm ci
}
finally {
    Pop-Location
}

Write-Host "ATRI DeepSeek Harness runtime installed." -ForegroundColor Green

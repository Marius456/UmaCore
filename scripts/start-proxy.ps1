[CmdletBinding()]
param(
    [string]$PythonExe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)

$ErrorActionPreference = 'Stop'
$repoDir = Split-Path -Parent $PSScriptRoot
$proxyAddress = '100.111.216.3'
$logDir = Join-Path $repoDir 'logs'
$supervisorLog = Join-Path $logDir 'official-events-proxy-supervisor.log'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python 3.11 not found at $PythonExe. Supply -PythonExe with the proxy runtime."
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Set-Location -LiteralPath $repoDir
while ($true) {
    while (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $proxyAddress `
            -ErrorAction SilentlyContinue)) {
        Add-Content -LiteralPath $supervisorLog `
            -Value "$(Get-Date -Format o) Waiting for Tailscale address $proxyAddress"
        Start-Sleep -Seconds 10
    }

    Add-Content -LiteralPath $supervisorLog `
        -Value "$(Get-Date -Format o) Starting proxy"
    & $PythonExe (Join-Path $repoDir 'proxy_server.py') 2>> $supervisorLog
    $proxyExitCode = $LASTEXITCODE
    Add-Content -LiteralPath $supervisorLog `
        -Value "$(Get-Date -Format o) Proxy exited with code $proxyExitCode; retrying"
    Start-Sleep -Seconds 10
}

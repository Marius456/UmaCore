[CmdletBinding()]
param(
    [string]$PythonExe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)

$ErrorActionPreference = 'Stop'
$repoDir = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python 3.11 not found at $PythonExe. Supply -PythonExe with the proxy runtime."
}

Set-Location -LiteralPath $repoDir
& $PythonExe (Join-Path $repoDir 'proxy_server.py')
exit $LASTEXITCODE

[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._/-]*$')]
    [string]$Branch = 'feat/fix-issues',

    [Alias('Host')]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.:-]*$')]
    [string]$ServerHost = '158.180.28.120',

    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_-]*$')]
    [string]$User = 'ubuntu',

    [string]$IdentityFile = 'D:\Download\ssh-key-2026-06-15.key'
)

$ErrorActionPreference = 'Stop'

$remoteScriptPath = Join-Path $PSScriptRoot 'deploy-remote.sh'
if (-not (Test-Path -LiteralPath $remoteScriptPath -PathType Leaf)) {
    throw "Remote deployment script not found: $remoteScriptPath"
}

if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw "SSH identity file not found: $IdentityFile"
}

$resolvedIdentityFile = (Resolve-Path -LiteralPath $IdentityFile).Path
$destination = "$User@$ServerHost"
$sshArguments = @(
    '-i', $resolvedIdentityFile,
    '--', $destination,
    'bash -s --', $Branch
)

Write-Host "Deploying origin/$Branch to $destination..."

# Stream the checked-in Bash script to the server so the deployment logic does
# not depend on which revision is currently checked out there.
$remoteScript = (Get-Content -LiteralPath $remoteScriptPath -Raw) -replace "`r`n", "`n"
$remoteScript | & ssh @sshArguments
$sshExitCode = $LASTEXITCODE

if ($sshExitCode -ne 0) {
    throw "Deployment failed with exit code $sshExitCode."
}

Write-Host 'Deployment completed successfully.'

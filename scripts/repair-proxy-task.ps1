#Requires -RunAsAdministrator
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoDir = Split-Path -Parent $PSScriptRoot
$pythonExe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
$proxyAddress = '100.111.216.3'
$serverAddress = '100.83.153.101'
$logDir = Join-Path $repoDir 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Start-Transcript -Path (Join-Path $logDir 'proxy-repair.log') -Append | Out-Null
try {
    if (-not (Test-Path -LiteralPath $pythonExe)) { throw "Missing runtime: $pythonExe" }
    $task = Get-ScheduledTask -TaskName 'UmaProxy' -TaskPath '\'
    $backup = Join-Path $logDir ('proxy-before-repair-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Export-ScheduledTask -TaskName 'UmaProxy' -TaskPath '\' |
        Out-File -LiteralPath ($backup + '-task.xml') -Encoding utf8

    # The existing Python TCP block on Private networks overrides any allow rule.
    # Preserve it on all other ports, and keep 8888 blocked for all other sources.
    $blockRules = @(Get-NetFirewallApplicationFilter |
        Where-Object { $_.Program -eq $pythonExe } |
        Get-NetFirewallRule |
        Where-Object {
            $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' -and
            $_.Profile.ToString() -eq 'Private' -and $_.Enabled -eq 'True'
        } |
        Where-Object {
            $port = $_ | Get-NetFirewallPortFilter
            $port.Protocol -eq 'TCP' -and $port.LocalPort -eq 'Any'
        })
    $blockRules | Select-Object Name,Profile,Enabled,Action |
        Export-Clixml -LiteralPath ($backup + '-firewall.xml')

    # Windows rejects ranges containing the unspecified/broadcast addresses.
    # This proxy listens only on IPv4.
    $otherSources = @('0.0.0.1-100.83.153.100', '100.83.153.102-255.255.255.254')
    if (-not (Get-NetFirewallRule -Name 'UmaCore-Proxy-Block-Other-Sources' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'UmaCore-Proxy-Block-Other-Sources' `
            -DisplayName 'UmaCore proxy: block other sources' -Direction Inbound `
            -Action Block -Profile Private -Program $pythonExe -Protocol TCP `
            -LocalPort 8888 -RemoteAddress $otherSources | Out-Null
    }
    if (-not (Get-NetFirewallRule -Name 'UmaCore-Proxy-Production' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'UmaCore-Proxy-Production' `
            -DisplayName 'UmaCore proxy: production over Tailscale' -Direction Inbound `
            -Action Allow -Profile Private -Program $pythonExe -Protocol TCP `
            -LocalAddress $proxyAddress -LocalPort 8888 -RemoteAddress $serverAddress `
            -InterfaceAlias 'Tailscale' | Out-Null
    }
    foreach ($rule in $blockRules) {
        $rule | Get-NetFirewallPortFilter |
            Set-NetFirewallPortFilter -LocalPort @('1-8887', '8889-65535')
    }

    $task.Settings.RestartCount = 999
    $task.Settings.RestartInterval = 'PT1M'
    $task.Settings.ExecutionTimeLimit = 'PT0S'
    $task.Settings.StartWhenAvailable = $true
    $task.Settings.DisallowStartIfOnBatteries = $false
    $task.Settings.StopIfGoingOnBatteries = $false
    Set-ScheduledTask -InputObject $task | Out-Null
    if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName 'UmaProxy' -TaskPath '\' }
    Write-Output 'Proxy firewall and scheduled-task repair completed.'
} finally {
    Stop-Transcript | Out-Null
}

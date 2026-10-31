[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'Lumen-Marketing-OS-Operations',
    [switch]$Replace
)

$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$executable = Join-Path $workspace '.venv\Scripts\lmos.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Local executable not found: $executable"
}
if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw 'Windows ScheduledTasks module is unavailable on this host'
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Replace) {
    throw "Task already exists: $TaskName (pass -Replace only after inspection)"
}
$arguments = '--workspace "' + $workspace + '" operations daemon --poll-seconds 30'
$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments -WorkingDirectory $workspace
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
if ($PSCmdlet.ShouldProcess($TaskName, 'Register local Department 5 operations supervisor')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description 'Local Lumen Marketing OS schedule supervisor; no direct external dispatch' `
        -Force:$Replace | Out-Null
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
}

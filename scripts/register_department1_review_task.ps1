[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'Lumen-Marketing-OS-D1-Reviews',
    [switch]$Replace
)

$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$runner = Join-Path $workspace 'scripts\run_department1_review_cycle.ps1'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Runner missing: $runner" }
if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) { throw "PowerShell missing: $powershell" }
if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw 'Windows ScheduledTasks module is unavailable'
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Replace) { throw "Task already exists: $TaskName" }
& $runner -CheckOnly | Out-Null

$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $runner + '"'
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $workspace
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At '01:30'
    New-ScheduledTaskTrigger -Daily -At '09:30'
    New-ScheduledTaskTrigger -Daily -At '17:30'
)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register local Codex specialist review runner')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
        -Settings $settings -Principal $principal `
        -Description 'Bounded internal Department 1 Codex reviews; no public actions' `
        -Force:$Replace | Out-Null
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
}

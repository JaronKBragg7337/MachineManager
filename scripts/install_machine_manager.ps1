[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoPath,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$PythonPath = "",
    [string]$StatusFile = "",
    [string]$DashboardDir = "",
    [string]$StateDb = "",
    [string]$LockFile = "",
    [string]$LogFile = "",
    [ValidateRange(5, 3600)]
    [int]$RestartDelaySeconds = 20,
    [string]$TaskName = "MachineManager",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

function Quote-Argument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$resolvedRepo = (Resolve-Path -LiteralPath $RepoPath).Path
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$watchdogPath = Join-Path $resolvedRepo "scripts\run_machine_manager_watchdog.ps1"
if (-not (Test-Path -LiteralPath $watchdogPath)) {
    throw "Machine Manager watchdog script is missing."
}
if (-not $PythonPath) {
    $python = Get-Command python.exe -ErrorAction Stop
    $PythonPath = $python.Source
}
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not $StatusFile) {
    $StatusFile = Join-Path $resolvedRepo "var\manager_status.json"
}
if (-not $DashboardDir) {
    $DashboardDir = Join-Path $resolvedRepo "dashboard"
}
if (-not $StateDb) {
    $StateDb = Join-Path $resolvedRepo "var\machine_manager.sqlite3"
}
if (-not $LockFile) {
    $LockFile = Join-Path $resolvedRepo "var\machine_manager.lock"
}
if (-not $LogFile) {
    $LogFile = Join-Path $resolvedRepo "var\manager.log"
}

$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$argumentLine = "-NoProfile -ExecutionPolicy Bypass -File " + (Quote-Argument $watchdogPath) +
    " -RepoPath " + (Quote-Argument $resolvedRepo) +
    " -PythonPath " + (Quote-Argument $resolvedPython) +
    " -ConfigPath " + (Quote-Argument $resolvedConfig) +
    " -StatusFile " + (Quote-Argument $StatusFile) +
    " -DashboardDir " + (Quote-Argument $DashboardDir) +
    " -StateDb " + (Quote-Argument $StateDb) +
    " -LockFile " + (Quote-Argument $LockFile) +
    " -LogFile " + (Quote-Argument $LogFile) +
    " -RestartDelaySeconds " + $RestartDelaySeconds

$action = New-ScheduledTaskAction -Execute $powershell -Argument $argumentLine -WorkingDirectory $resolvedRepo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Run the Machine Manager watchdog with bounded worker recovery and public-safe telemetry." -Force | Out-Null

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, TaskPath | Format-List

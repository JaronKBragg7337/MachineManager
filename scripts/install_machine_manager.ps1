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
    [string]$TaskName = "MachineManager",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

function Quote-Argument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$resolvedRepo = (Resolve-Path -LiteralPath $RepoPath).Path
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
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

$argumentLine = "-m manager.run --config " + (Quote-Argument $resolvedConfig) +
    " --status-file " + (Quote-Argument $StatusFile) +
    " --dashboard-dir " + (Quote-Argument $DashboardDir) +
    " --state-db " + (Quote-Argument $StateDb) +
    " --lock-file " + (Quote-Argument $LockFile) +
    " --log-file " + (Quote-Argument $LogFile)

$action = New-ScheduledTaskAction -Execute $resolvedPython -Argument $argumentLine -WorkingDirectory $resolvedRepo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Run the local Machine Manager with bounded worker recovery and public-safe telemetry." -Force | Out-Null

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, TaskPath | Format-List

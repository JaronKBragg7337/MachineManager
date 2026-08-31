[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$StatusFile,
    [Parameter(Mandatory = $true)]
    [string]$DashboardDir,
    [Parameter(Mandatory = $true)]
    [string]$StateDb,
    [Parameter(Mandatory = $true)]
    [string]$LockFile,
    [Parameter(Mandatory = $true)]
    [string]$LogFile,
    [ValidateRange(5, 3600)]
    [int]$RestartDelaySeconds = 20
)

$ErrorActionPreference = "Stop"

$resolvedRepo = (Resolve-Path -LiteralPath $RepoPath).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path

$runnerArguments = @(
    "-m", "manager.run",
    "--config", $resolvedConfig,
    "--status-file", $StatusFile,
    "--dashboard-dir", $DashboardDir,
    "--state-db", $StateDb,
    "--lock-file", $LockFile,
    "--log-file", $LogFile
)

while ($true) {
    Push-Location $resolvedRepo
    try {
        & $resolvedPython @runnerArguments
    }
    finally {
        Pop-Location
    }

    # The child owns its manager lock and worker adoption. A bounded delay
    # prevents a faulting child from spinning while allowing the task to
    # recover without an operator relaunch.
    Start-Sleep -Seconds $RestartDelaySeconds
}

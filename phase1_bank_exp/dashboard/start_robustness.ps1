param(
    [string]$Distribution = "Ubuntu-22.04",
    [int]$Workers = 20
)

$ErrorActionPreference = "Stop"

if ($Workers -lt 1) {
    throw "Workers must be at least 1."
}

$ProjectWindows = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ($ProjectWindows -notmatch '^([A-Za-z]):(.*)$') {
    throw "The project must be on a Windows drive: $ProjectWindows"
}
$Drive = $Matches[1].ToLowerInvariant()
$Tail = $Matches[2].Replace('\', '/')
$ProjectWsl = "/mnt/$Drive$Tail"
$RunnerWsl = "$ProjectWsl/dashboard/run_robustness.sh"
$LogWsl = "$ProjectWsl/dashboard/sweeps/robustness_run.log"

$Existing = & wsl.exe -d $Distribution -- bash -lc "pgrep -af '[r]obustness_validate.py' || true"
if ($Existing) {
    Write-Host "Robustness validation is already running:"
    Write-Host $Existing
    exit 0
}

$Process = Start-Process -FilePath "wsl.exe" -ArgumentList @(
    "-d", $Distribution, "--", "bash", $RunnerWsl, $Workers
) -WindowStyle Hidden -PassThru

Start-Sleep -Seconds 2
$Started = & wsl.exe -d $Distribution -- bash -lc "pgrep -af '[r]obustness_validate.py' || true"
if (-not $Started) {
    throw "The process did not remain running. Check $LogWsl"
}

Write-Host "Robustness validation started on this PC."
Write-Host "Windows WSL process: $($Process.Id)"
Write-Host "Process: $Started"
Write-Host "Log: $ProjectWindows\dashboard\sweeps\robustness_run.log"

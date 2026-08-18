[CmdletBinding()]
param(
    [Parameter()]
    [string]$SshTarget = "nas",

    [Parameter()]
    [string]$IdentityFile,

    [Parameter()]
    [switch]$InstallOnly,

    [Parameter()]
    [switch]$StageOnly
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DashboardDir = Join-Path $ProjectDir "dashboard"
$ReportDir = Join-Path $DashboardDir "reports"
$RemoteDashboardDir = "/volume1/docker/life-game/app/dashboard"
$RemoteReportDir = "$RemoteDashboardDir/reports"
$RemoteStagingDir = ".life-game-deploy"
$RemoteStagingRootDir = "$RemoteStagingDir/root"
$RemoteStagingDashboardDir = "$RemoteStagingDir/dashboard"
$RemoteStagingInstitutionDir = "$RemoteStagingDir/institutions"
$RemoteStagingReportDir = "$RemoteStagingDir/reports"
$SshOptions = @()

if ($InstallOnly -and $StageOnly) {
    throw "InstallOnly and StageOnly cannot be used together."
}

if ($IdentityFile) {
    if (-not (Test-Path $IdentityFile -PathType Leaf)) {
        throw "SSH private key was not found: $IdentityFile"
    }
    $ResolvedIdentityFile = (Resolve-Path $IdentityFile).Path
    $SshOptions = @("-i", $ResolvedIdentityFile)
}

if (-not (Get-Command "ssh" -ErrorAction SilentlyContinue)) {
    throw "ssh was not found. Enable the Windows OpenSSH Client."
}

function Send-FileOverSsh {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LocalPath,
        [Parameter(Mandatory = $true)]
        [string]$RemotePath
    )

    $SshExecutable = (Get-Command "ssh").Source
    $NativeArguments = @()
    if ($IdentityFile) {
        $QuotedIdentity = $ResolvedIdentityFile.Replace('"', '\"')
        $NativeArguments += "-i `"$QuotedIdentity`""
    }
    $NativeArguments += "`"$SshTarget`""
    $NativeArguments += "`"cat > '$RemotePath'`""

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $SshExecutable
    $StartInfo.Arguments = $NativeArguments -join " "
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    if (-not $Process.Start()) {
        throw "Failed to start ssh for upload: $LocalPath"
    }

    $StdoutTask = $Process.StandardOutput.ReadToEndAsync()
    $StderrTask = $Process.StandardError.ReadToEndAsync()
    $Source = [System.IO.File]::OpenRead($LocalPath)
    $CopyError = $null
    try {
        $Source.CopyTo($Process.StandardInput.BaseStream)
        $Process.StandardInput.BaseStream.Flush()
    }
    catch {
        $CopyError = $_
    }
    finally {
        $Source.Dispose()
        $Process.StandardInput.Close()
    }

    $Process.WaitForExit()
    $StandardOutput = $StdoutTask.Result
    $StandardError = $StderrTask.Result
    if ($StandardOutput) {
        Write-Host $StandardOutput.TrimEnd()
    }
    if ($StandardError) {
        Write-Host $StandardError.TrimEnd()
    }
    if ($CopyError -or $Process.ExitCode -ne 0) {
        throw "SSH stream upload failed for $LocalPath (exit $($Process.ExitCode))."
    }
}

$RuntimeRootNames = @(
    "action_costs.py", "contract_costs.py", "counterparty_selection.py",
    "economic_rules.py", "engine.py", "event_builders.py", "event_store.py",
    "game.py", "game_session.py", "interactive_runtime.py", "llm_integration.py",
    "offline_simulation.py", "policy.py", "projection.py", "relationship_rules.py",
    "resource_rules.py", "scenario_generation.py", "trait_rules.py", "turn_engine.py"
)
$RuntimeDashboardNames = @(
    "app.html", "life_ledger.html", "server.py", "aquarium_runtime.py", "aquarium_worker.py",
    "aquarium_stream.py", "build_dashboard.py", "experiment_parameters.py", "experiment_worker.py",
    "event_density.py", "particle_cohorts.py", "particle_packet.py",
    "spatial_history.py", "template.html"
)
$RuntimeRootFiles = @($RuntimeRootNames | ForEach-Object { Join-Path $ProjectDir $_ })
$RuntimeDashboardFiles = @($RuntimeDashboardNames | ForEach-Object { Join-Path $DashboardDir $_ })
$RuntimeInstitutionFiles = @(Get-ChildItem -Path (Join-Path $ProjectDir "institutions") -Filter "*.py" -File)
$ComposeFile = Join-Path $PSScriptRoot "docker-compose.yml"
$InstallScript = Join-Path $PSScriptRoot "install-staged.sh"
$ReportFiles = @(Get-ChildItem -Path $ReportDir -Filter "barter_sweep_*.html" -File)

if (-not $InstallOnly) {
    foreach ($RuntimeFile in @($RuntimeRootFiles + $RuntimeDashboardFiles + $ComposeFile + $InstallScript)) {
        if (-not (Test-Path $RuntimeFile -PathType Leaf)) {
            throw "Runtime file was not found: $RuntimeFile"
        }
    }
    if ($ReportFiles.Count -eq 0) {
        throw "No barter_sweep_*.html files were found in dashboard/reports."
    }

    Write-Host "Preparing a staging directory in the NAS user home: $SshTarget"
    & ssh @SshOptions $SshTarget "mkdir -p '$RemoteStagingRootDir' '$RemoteStagingDashboardDir' '$RemoteStagingInstitutionDir' '$RemoteStagingReportDir'"
    if ($LASTEXITCODE -ne 0) {
        throw "SSH connection or staging directory creation failed."
    }

    Write-Host "Uploading the runtime application."
    foreach ($RuntimeFile in $RuntimeRootFiles) {
        Send-FileOverSsh -LocalPath $RuntimeFile -RemotePath "$RemoteStagingRootDir/$([IO.Path]::GetFileName($RuntimeFile))"
    }
    foreach ($RuntimeFile in $RuntimeDashboardFiles) {
        Send-FileOverSsh -LocalPath $RuntimeFile -RemotePath "$RemoteStagingDashboardDir/$([IO.Path]::GetFileName($RuntimeFile))"
    }
    foreach ($RuntimeFile in $RuntimeInstitutionFiles) {
        Send-FileOverSsh -LocalPath $RuntimeFile.FullName -RemotePath "$RemoteStagingInstitutionDir/$($RuntimeFile.Name)"
    }
    Send-FileOverSsh -LocalPath $ComposeFile -RemotePath "$RemoteStagingDir/docker-compose.yml"
    Send-FileOverSsh -LocalPath $InstallScript -RemotePath "$RemoteStagingDir/install-staged.sh"

    Write-Host ("Uploading {0} HTML report files." -f $ReportFiles.Count)
    foreach ($ReportFile in $ReportFiles) {
        Send-FileOverSsh -LocalPath $ReportFile.FullName -RemotePath "$RemoteStagingReportDir/$($ReportFile.Name)"
    }
}
else {
    Write-Host "Skipping upload and installing the files already staged on the NAS: $SshTarget"
}

if ($StageOnly) {
    Write-Host "Staging complete. Run this command in your visible PowerShell terminal:"
    Write-Host "ssh -t $SshTarget `"sh '.life-game-deploy/install-staged.sh'`""
    return
}

Write-Host "Installing files and restarting the container. Enter the NAS sudo password if prompted."
& ssh @SshOptions -t $SshTarget "sh '$RemoteStagingDir/install-staged.sh'"
if ($LASTEXITCODE -ne 0) {
    throw "File installation or container restart failed. Staged files remain in the NAS user home."
}

Write-Host "Deployment complete: http://192.168.0.22:8016/"

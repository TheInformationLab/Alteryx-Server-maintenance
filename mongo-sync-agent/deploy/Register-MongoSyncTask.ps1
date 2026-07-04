<#
.SYNOPSIS
    Registers a Windows Scheduled Task that runs the mongo-sync-agent (msa)
    on a recurring interval.

.DESCRIPTION
    Creates (or replaces) a Scheduled Task named "MongoSyncAgent" that invokes
    the agent's Python module every 30 minutes, indefinitely, using the
    supplied Python interpreter and configuration file.

.USAGE
    Run from an elevated PowerShell prompt on the host where the agent is
    installed:

        .\Register-MongoSyncTask.ps1

    Preview the action without registering anything:

        .\Register-MongoSyncTask.ps1 -WhatIf

    Override the defaults, e.g. to run the task under a service account:

        .\Register-MongoSyncTask.ps1 `
            -PythonPath "D:\mongo-sync-agent\.venv\Scripts\pythonw.exe" `
            -ConfigPath "D:\mongo-sync-agent\config\config.toml" `
            -WorkingDir "D:\mongo-sync-agent" `
            -TaskUser "CONTOSO\svc-mongosync" `
            -TaskPassword (Read-Host -AsSecureString "Password")

.NOTES
    IMPORTANT: Customise $PythonPath below (or pass -PythonPath) to point at
    the Python executable inside the mongo-sync-agent virtual environment
    (venv), e.g. "C:\path\to\venv\Scripts\pythonw.exe". Using pythonw.exe
    avoids a console window flashing up on every run; substitute python.exe
    if you want console output captured by Scheduled Tasks history instead.

    Registering with -RunLevel Highest ensures the task can read Alteryx's
    ProgramData paths, RuntimeSettings.xml, and any other locations that
    require administrative rights.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Path to the Python interpreter to run the agent with. This SHOULD be the
    # interpreter inside the mongo-sync-agent venv, not a system-wide Python.
    [string]$PythonPath = "C:\ProgramData\mongo-sync-agent\venv\Scripts\pythonw.exe",

    # Path to the agent's TOML configuration file.
    [string]$ConfigPath = "C:\ProgramData\mongo-sync-agent\config\config.toml",

    # Working directory the task runs from.
    [string]$WorkingDir = "C:\ProgramData\mongo-sync-agent",

    # Name of the scheduled task.
    [string]$TaskName = "MongoSyncAgent",

    # Account the task runs as. Defaults to the built-in SYSTEM account, which
    # has the local filesystem access needed to read Alteryx's ProgramData
    # paths and RuntimeSettings.xml. Set to a dedicated service account if
    # your security policy requires it.
    [string]$TaskUser = "SYSTEM",

    # Password for $TaskUser. Only required (and only used) when $TaskUser is
    # NOT one of the built-in accounts (SYSTEM, LOCAL SERVICE, NETWORK
    # SERVICE). Pass as a SecureString, e.g. via Read-Host -AsSecureString.
    [System.Security.SecureString]$TaskPassword
)

$ErrorActionPreference = "Stop"

$builtInAccounts = @("SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE", "NT AUTHORITY\SYSTEM")
$isBuiltInAccount = $builtInAccounts -contains $TaskUser.ToUpper()

if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-Warning "PythonPath '$PythonPath' was not found on disk. Continuing anyway — verify this points at the mongo-sync-agent venv's python(w).exe before relying on the task."
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Warning "ConfigPath '$ConfigPath' was not found on disk. Continuing anyway — verify the configuration file exists before relying on the task."
}

# Build the action: <python> -m mongo_sync_agent --config "<config>"
$argumentList = "-m mongo_sync_agent --config `"$ConfigPath`""

$action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument $argumentList `
    -WorkingDirectory $WorkingDir

# Trigger: start now (or immediately at registration time), repeat every 30
# minutes, and keep repeating indefinitely (no RepetitionDuration end and no
# trigger expiry).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date)
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition

# Run with the highest privileges available to the specified account so the
# agent can read protected Alteryx/ProgramData paths.
$principalParams = @{
    UserId   = $TaskUser
    RunLevel = "Highest"
}
if ($isBuiltInAccount) {
    $principalParams["LogonType"] = "ServiceAccount"
}
else {
    $principalParams["LogonType"] = "Password"
}
$principal = New-ScheduledTaskPrincipal @principalParams

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Runs the mongo-sync-agent (msa) to incrementally extract Alteryx MongoDB data, Gallery/Service logs, and host metrics, landing them in S3 for Snowpipe ingestion."

$registerParams = @{
    TaskName    = $TaskName
    InputObject = $task
    Force       = $true
}

# Only pass a plaintext password through to Register-ScheduledTask when the
# account requires one (i.e. it is not a built-in service account).
if (-not $isBuiltInAccount) {
    if (-not $TaskPassword) {
        throw "TaskUser '$TaskUser' is not a built-in service account; -TaskPassword is required."
    }
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($TaskPassword)
    )
    $registerParams["User"] = $TaskUser
    $registerParams["Password"] = $plainPassword
}

if ($PSCmdlet.ShouldProcess("Task Scheduler", "Register scheduled task '$TaskName'")) {
    Register-ScheduledTask @registerParams | Out-Null
    Write-Host "Scheduled task '$TaskName' registered: runs every 30 minutes as '$TaskUser'."
    Write-Host "  Python:  $PythonPath"
    Write-Host "  Config:  $ConfigPath"
    Write-Host "  Working: $WorkingDir"
}
else {
    Write-Host "WhatIf: would register scheduled task '$TaskName' with:"
    Write-Host "  Action:    $PythonPath $argumentList"
    Write-Host "  Trigger:   every 30 minutes, indefinitely"
    Write-Host "  Run as:    $TaskUser (RunLevel: Highest)"
    Write-Host "  Working:   $WorkingDir"
}

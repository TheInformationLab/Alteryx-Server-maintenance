<#
.SYNOPSIS
    Registers a Windows Scheduled Task that runs the mongo-sync-agent (msa)
    on a recurring interval.

.DESCRIPTION
    Creates (or replaces) a Scheduled Task named "MongoSyncAgent" that runs the
    agent every 30 minutes, indefinitely. Two invocation modes are supported:

      * EXE mode (client / production install): pass -ExePath pointing at the
        packaged, self-contained msa.exe. No Python is required on the host.
        This is the recommended mode for the release bundle.

      * Python mode (development / source install): pass -PythonPath pointing
        at the Python interpreter (ideally the agent's venv) that will run
        `-m mongo_sync_agent`.

    If -ExePath is supplied it takes precedence and -PythonPath is ignored.

.USAGE
    Run from an elevated PowerShell prompt on the host where the agent is
    installed.

    Client / production (exe bundle), register the packaged executable:

        .\Register-MongoSyncTask.ps1 `
            -ExePath "C:\ProgramData\mongo-sync-agent\msa.exe" `
            -ConfigPath "C:\ProgramData\mongo-sync-agent\config\config.toml" `
            -WorkingDir "C:\ProgramData\mongo-sync-agent"

    Preview the action without registering anything:

        .\Register-MongoSyncTask.ps1 -ExePath "...\msa.exe" -WhatIf

    Development (source install), run under a service account via Python:

        .\Register-MongoSyncTask.ps1 `
            -PythonPath "D:\mongo-sync-agent\.venv\Scripts\pythonw.exe" `
            -ConfigPath "D:\mongo-sync-agent\config\config.toml" `
            -WorkingDir "D:\mongo-sync-agent" `
            -TaskUser "CONTOSO\svc-mongosync" `
            -TaskPassword (Read-Host -AsSecureString "Password")

.NOTES
    EXE mode is the intended path for client hosts that have no Python: point
    -ExePath at the msa.exe shipped in the release zip.

    In Python mode, prefer the pythonw.exe inside the mongo-sync-agent venv
    (not a system-wide Python). Using pythonw.exe avoids a console window
    flashing up on every run; substitute python.exe if you want console output
    captured by Scheduled Tasks history instead.

    Registering with -RunLevel Highest ensures the task can read Alteryx's
    ProgramData paths, RuntimeSettings.xml, and any other locations that
    require administrative rights.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Path to the packaged, self-contained msa.exe (from the release bundle).
    # When set, the task runs `msa.exe --config <ConfigPath>` directly and no
    # Python is needed on the host. Takes precedence over -PythonPath.
    [string]$ExePath = "",

    # Path to the Python interpreter to run the agent with (development / source
    # install only). This SHOULD be the interpreter inside the mongo-sync-agent
    # venv, not a system-wide Python. Ignored when -ExePath is supplied.
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

# Decide the invocation mode. -ExePath (packaged msa.exe) takes precedence over
# -PythonPath (source/venv install).
$useExe = -not [string]::IsNullOrWhiteSpace($ExePath)

if ($useExe) {
    if (-not (Test-Path -LiteralPath $ExePath)) {
        Write-Warning "ExePath '$ExePath' was not found on disk. Continuing anyway — verify this points at the packaged msa.exe before relying on the task."
    }
    # Build the action: msa.exe --config "<config>"
    $execute = $ExePath
    $argumentList = "--config `"$ConfigPath`""
}
else {
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        Write-Warning "PythonPath '$PythonPath' was not found on disk. Continuing anyway — verify this points at the mongo-sync-agent venv's python(w).exe before relying on the task (or pass -ExePath to use the packaged msa.exe instead)."
    }
    # Build the action: <python> -m mongo_sync_agent --config "<config>"
    $execute = $PythonPath
    $argumentList = "-m mongo_sync_agent --config `"$ConfigPath`""
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Warning "ConfigPath '$ConfigPath' was not found on disk. Continuing anyway — verify the configuration file exists before relying on the task."
}

$action = New-ScheduledTaskAction `
    -Execute $execute `
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

$modeLabel = if ($useExe) { "Exe" } else { "Python" }

if ($PSCmdlet.ShouldProcess("Task Scheduler", "Register scheduled task '$TaskName'")) {
    Register-ScheduledTask @registerParams | Out-Null
    Write-Host "Scheduled task '$TaskName' registered: runs every 30 minutes as '$TaskUser'."
    Write-Host "  $($modeLabel):  $execute"
    Write-Host "  Config:  $ConfigPath"
    Write-Host "  Working: $WorkingDir"
}
else {
    Write-Host "WhatIf: would register scheduled task '$TaskName' with:"
    Write-Host "  Action:    $execute $argumentList"
    Write-Host "  Trigger:   every 30 minutes, indefinitely"
    Write-Host "  Run as:    $TaskUser (RunLevel: Highest)"
    Write-Host "  Working:   $WorkingDir"
}

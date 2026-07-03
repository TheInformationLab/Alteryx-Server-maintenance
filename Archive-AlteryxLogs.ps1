[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $false)]
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'Archive-AlteryxLogs.config.psd1')
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message"
}

function Write-Skip {
    param([string]$Message)
    Write-Warning "[SKIP] $Message"
}

function Assert-ConfigKey {
    param(
        [hashtable]$Config,
        [string]$Key
    )

    if (-not $Config.ContainsKey($Key)) {
        throw "Missing required config key '$Key'."
    }
}

function Test-StringArray {
    param([object]$Value)

    if ($null -eq $Value) {
        return $false
    }

    if ($Value -is [string]) {
        return $true
    }

    if ($Value -isnot [System.Array]) {
        return $false
    }

    foreach ($item in $Value) {
        if ($item -isnot [string]) {
            return $false
        }
    }

    return $true
}

function Convert-ToStringArray {
    param([object]$Value)

    if ($null -eq $Value) {
        return @()
    }

    if ($Value -is [string]) {
        return @($Value)
    }

    return @($Value)
}

function Test-PathWritable {
    param([string]$Path)

    $probe = Join-Path $Path ('.write-test-{0}.tmp' -f ([guid]::NewGuid().ToString('N')))

    try {
        [System.IO.File]::WriteAllText($probe, 'test')
        [System.IO.File]::Delete($probe)
        return $true
    }
    catch {
        if (Test-Path -LiteralPath $probe) {
            [System.IO.File]::Delete($probe)
        }

        return $false
    }
}

function Get-SafeArchiveSegment {
    param([string]$Value)

    $invalid = [System.IO.Path]::GetInvalidFileNameChars()
    $clean = $Value
    foreach ($char in $invalid) {
        $clean = $clean.Replace($char, '_')
    }

    $clean = $clean.Trim()
    if ([string]::IsNullOrWhiteSpace($clean)) {
        return 'LogRoot'
    }

    return $clean
}

function Get-FileArchiveDate {
    param(
        [System.IO.FileInfo]$File,
        [bool]$UseFilenameDate
    )

    if ($UseFilenameDate -and $File.Name -match '(?<year>\d{4})[-_](?<month>\d{2})[-_](?<day>\d{2})') {
        try {
            return [datetime]::new(
                [int]$Matches.year,
                [int]$Matches.month,
                [int]$Matches.day
            )
        }
        catch {
            Write-Skip "Could not parse filename date for '$($File.FullName)'. Falling back to LastWriteTime."
        }
    }

    return $File.LastWriteTime
}

function Test-FileReadable {
    param([System.IO.FileInfo]$File)

    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $File.FullName,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Get-RelativePath {
    param(
        [string]$BasePath,
        [string]$FullPath
    )

    $baseFullPath = [System.IO.Path]::GetFullPath($BasePath)
    if (-not $baseFullPath.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $baseFullPath = $baseFullPath + [System.IO.Path]::DirectorySeparatorChar
    }

    $baseUri = [System.Uri]::new($baseFullPath)
    $fileUri = [System.Uri]::new([System.IO.Path]::GetFullPath($FullPath))
    $relative = [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($fileUri).ToString())
    return $relative -replace '/', [System.IO.Path]::DirectorySeparatorChar
}

function Get-ConfiguredFiles {
    param([hashtable]$Config)

    $files = New-Object System.Collections.Generic.List[object]

    foreach ($root in $Config.LogRoots) {
        $rootName = [string]$root.Name
        $rootPath = [string]$root.Path
        $includePatterns = Convert-ToStringArray $root.IncludePatterns
        $excludePatterns = Convert-ToStringArray $root.ExcludePatterns

        $seen = @{}
        foreach ($pattern in $includePatterns) {
            $matchedFiles = Get-ChildItem -LiteralPath $rootPath -Recurse -File -Filter $pattern -ErrorAction Stop
            foreach ($file in $matchedFiles) {
                if ($seen.ContainsKey($file.FullName)) {
                    continue
                }

                $excluded = $false
                foreach ($excludePattern in $excludePatterns) {
                    if ($file.Name -like $excludePattern -or $file.FullName -like $excludePattern) {
                        $excluded = $true
                        break
                    }
                }

                if ($excluded) {
                    continue
                }

                $seen[$file.FullName] = $true
                $files.Add([pscustomobject]@{
                    File = $file
                    RootName = $rootName
                    RootPath = $rootPath
                })
            }
        }
    }

    return $files
}

function Open-ZipArchiveForUpdate {
    param([string]$ArchivePath)

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $fileMode = [System.IO.FileMode]::OpenOrCreate
    $fileAccess = [System.IO.FileAccess]::ReadWrite
    $fileShare = [System.IO.FileShare]::None
    $stream = [System.IO.File]::Open($ArchivePath, $fileMode, $fileAccess, $fileShare)

    try {
        $archive = [System.IO.Compression.ZipArchive]::new($stream, [System.IO.Compression.ZipArchiveMode]::Update)
        return [pscustomobject]@{
            Stream = $stream
            Archive = $archive
        }
    }
    catch {
        $stream.Dispose()
        throw
    }
}

function Add-OrReplaceZipEntry {
    param(
        [System.IO.Compression.ZipArchive]$Archive,
        [System.IO.FileInfo]$File,
        [string]$EntryName
    )

    $normalisedEntryName = $EntryName -replace '\\', '/'
    $existing = $Archive.GetEntry($normalisedEntryName)
    $replaced = $false

    if ($null -ne $existing) {
        $existing.Delete()
        $replaced = $true
    }

    $entry = $Archive.CreateEntry($normalisedEntryName, [System.IO.Compression.CompressionLevel]::Optimal)
    $entry.LastWriteTime = [System.DateTimeOffset]::new($File.LastWriteTime)

    $sourceStream = $null
    $entryStream = $null

    try {
        $sourceStream = [System.IO.File]::Open(
            $File.FullName,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $entryStream = $entry.Open()
        $sourceStream.CopyTo($entryStream)
    }
    finally {
        if ($null -ne $entryStream) {
            $entryStream.Dispose()
        }

        if ($null -ne $sourceStream) {
            $sourceStream.Dispose()
        }
    }

    return $replaced
}

function Validate-Config {
    param([hashtable]$Config)

    foreach ($key in @('RetentionDays', 'ArchiveDestinationPath', 'ArchiveNamePrefix', 'UseFilenameDate', 'DryRun', 'LogRoots')) {
        Assert-ConfigKey -Config $Config -Key $key
    }

    if (-not ($Config.RetentionDays -is [int]) -or $Config.RetentionDays -lt 1) {
        throw 'RetentionDays must be a positive whole number.'
    }

    if ([string]::IsNullOrWhiteSpace([string]$Config.ArchiveDestinationPath)) {
        throw 'ArchiveDestinationPath must not be empty.'
    }

    if ([string]::IsNullOrWhiteSpace([string]$Config.ArchiveNamePrefix)) {
        throw 'ArchiveNamePrefix must not be empty.'
    }

    if (-not (Test-Path -LiteralPath $Config.ArchiveDestinationPath -PathType Container)) {
        throw "ArchiveDestinationPath '$($Config.ArchiveDestinationPath)' does not exist."
    }

    if (-not (Test-PathWritable -Path $Config.ArchiveDestinationPath)) {
        throw "ArchiveDestinationPath '$($Config.ArchiveDestinationPath)' is not writable."
    }

    if ($null -eq $Config.LogRoots -or $Config.LogRoots.Count -eq 0) {
        throw 'LogRoots must contain at least one log root.'
    }

    foreach ($root in $Config.LogRoots) {
        foreach ($key in @('Name', 'Path', 'IncludePatterns', 'ExcludePatterns')) {
            if (-not $root.ContainsKey($key)) {
                throw "LogRoots entry is missing required key '$key'."
            }
        }

        if ([string]::IsNullOrWhiteSpace([string]$root.Name)) {
            throw 'Each LogRoots entry must have a non-empty Name.'
        }

        if (-not (Test-Path -LiteralPath $root.Path -PathType Container)) {
            throw "Log root '$($root.Name)' path '$($root.Path)' does not exist."
        }

        if (-not (Test-StringArray $root.IncludePatterns)) {
            throw "Log root '$($root.Name)' IncludePatterns must be a string or array of strings."
        }

        if ((Convert-ToStringArray $root.IncludePatterns).Count -eq 0) {
            throw "Log root '$($root.Name)' IncludePatterns must contain at least one pattern."
        }

        if (-not (Test-StringArray $root.ExcludePatterns)) {
            throw "Log root '$($root.Name)' ExcludePatterns must be a string or array of strings."
        }
    }
}

function Invoke-AlteryxLogArchive {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Config file '$Path' does not exist."
    }

    $config = Import-PowerShellDataFile -LiteralPath $Path
    Validate-Config -Config $config

    $dryRun = [bool]$config.DryRun -or [bool]$WhatIfPreference
    $useFilenameDate = [bool]$config.UseFilenameDate
    $cutoffDate = (Get-Date).Date.AddDays(-[int]$config.RetentionDays)
    $stats = [ordered]@{
        Scanned = 0
        Archived = 0
        Replaced = 0
        Retained = 0
        Deleted = 0
        Skipped = 0
        Failed = 0
    }

    Write-Info "Config: $Path"
    Write-Info "Archive destination: $($config.ArchiveDestinationPath)"
    Write-Info "Retention: keep files dated $($cutoffDate.ToString('yyyy-MM-dd')) or newer"
    if ($dryRun) {
        Write-Info 'Dry run is enabled. No archives or source files will be changed.'
    }

    $configuredFiles = @(Get-ConfiguredFiles -Config $config)
    $stats.Scanned = $configuredFiles.Count

    $archiveGroups = @{}
    foreach ($item in $configuredFiles) {
        $file = [System.IO.FileInfo]$item.File

        if (-not (Test-FileReadable -File $file)) {
            Write-Skip "File is locked or unreadable: $($file.FullName)"
            $stats.Skipped++
            continue
        }

        $archiveDate = Get-FileArchiveDate -File $file -UseFilenameDate $useFilenameDate
        $monthKey = $archiveDate.ToString('yyyy-MM')
        $archivePath = Join-Path $config.ArchiveDestinationPath ('{0}-{1}.zip' -f $config.ArchiveNamePrefix, $monthKey)
        $rootSegment = Get-SafeArchiveSegment -Value ([string]$item.RootName)
        $relativePath = Get-RelativePath -BasePath ([string]$item.RootPath) -FullPath $file.FullName
        $entryName = Join-Path $rootSegment $relativePath

        if (-not $archiveGroups.ContainsKey($archivePath)) {
            $archiveGroups[$archivePath] = New-Object System.Collections.Generic.List[object]
        }

        $archiveGroups[$archivePath].Add([pscustomobject]@{
            File = $file
            EntryName = $entryName
            ArchiveDate = $archiveDate
            IsOld = ($archiveDate.Date -lt $cutoffDate)
            Archived = $false
        })
    }

    foreach ($archivePath in $archiveGroups.Keys) {
        $group = $archiveGroups[$archivePath]
        Write-Info "Processing archive: $archivePath"

        if ($dryRun) {
            foreach ($item in $group) {
                Write-Info "Would add or replace '$($item.EntryName)' from '$($item.File.FullName)'"
                $item.Archived = $true
                $stats.Archived++
            }
            continue
        }

        $zipContext = $null
        try {
            $zipContext = Open-ZipArchiveForUpdate -ArchivePath $archivePath
            foreach ($item in $group) {
                try {
                    $replaced = Add-OrReplaceZipEntry -Archive $zipContext.Archive -File $item.File -EntryName $item.EntryName
                    $item.Archived = $true
                    $stats.Archived++
                    if ($replaced) {
                        $stats.Replaced++
                    }
                }
                catch {
                    Write-Warning "Failed to archive '$($item.File.FullName)': $($_.Exception.Message)"
                    $stats.Failed++
                }
            }
        }
        catch {
            Write-Warning "Failed to open or create archive '$archivePath': $($_.Exception.Message)"
            foreach ($item in $group) {
                $stats.Failed++
            }
        }
        finally {
            if ($null -ne $zipContext) {
                $zipContext.Archive.Dispose()
                $zipContext.Stream.Dispose()
            }
        }
    }

    foreach ($group in $archiveGroups.Values) {
        foreach ($item in $group) {
            if (-not $item.Archived) {
                continue
            }

            if ($item.IsOld) {
                if ($dryRun) {
                    Write-Info "Would delete old local file '$($item.File.FullName)'"
                    $stats.Deleted++
                    continue
                }

                try {
                    Remove-Item -LiteralPath $item.File.FullName -Force
                    $stats.Deleted++
                }
                catch {
                    Write-Warning "Failed to delete '$($item.File.FullName)': $($_.Exception.Message)"
                    $stats.Failed++
                }
            }
            else {
                $stats.Retained++
            }
        }
    }

    Write-Host ''
    Write-Host 'Run summary'
    Write-Host '-----------'
    foreach ($key in $stats.Keys) {
        Write-Host ('{0}: {1}' -f $key, $stats[$key])
    }

    if ($stats.Failed -gt 0) {
        exit 1
    }

    exit 0
}

try {
    Invoke-AlteryxLogArchive -Path $ConfigPath
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}

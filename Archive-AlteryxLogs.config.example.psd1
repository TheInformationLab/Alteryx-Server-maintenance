@{
    # Number of days to keep on the Alteryx Server.
    # Files older than this are removed only after they have been added to the monthly archive.
    RetentionDays = 7

    # Network share, mapped drive, or synced cloud folder where monthly archives are stored.
    # The folder must already exist and be writable by the account running the scheduled task.
    ArchiveDestinationPath = '\\server\share\AlteryxLogArchives'

    # Monthly archive files are named like AlteryxLogs-2026-05.zip.
    ArchiveNamePrefix = 'AlteryxLogs'

    # Prefer dates embedded in filenames, such as alteryx-2026-05-13.csv.
    # When no filename date is present, the script falls back to LastWriteTime.
    UseFilenameDate = $true

    # Dry-run mode for scheduled or unattended runs.
    # This has the same effect as running the script with PowerShell's native -WhatIf switch.
    DryRun = $true

    # Add one entry for each Alteryx log folder you want to archive.
    LogRoots = @(
        @{
            Name = 'Gallery Logs'
            Path = 'D:\Program Files\Alteryx\Gallery\Logs'
            IncludePatterns = @('*.csv', '*.log', '*.txt')
            ExcludePatterns = @()
        }
    )
}

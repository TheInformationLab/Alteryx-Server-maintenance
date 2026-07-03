# Alteryx Server Maintenance

Housekeeping and maintenance scripts for Windows-based Alteryx Server instances.

## Alteryx Log Archive

`Archive-AlteryxLogs.ps1` archives Alteryx log files into monthly zip files stored in a network folder, mapped drive, or synced cloud folder. It is designed to run unattended from Windows Task Scheduler.

The script is Windows-only and uses built-in PowerShell/.NET features. It does not need external PowerShell modules, 7-Zip, AWS CLI, Azure CLI, or any cloud SDK.

## How It Works

- Reads settings from a human-editable PowerShell data file (`.psd1`).
- Scans each configured Alteryx log folder recursively.
- Adds every matching file to a monthly archive, including files from the last 7 days.
- Uses monthly archive names such as `AlteryxLogs-2026-05.zip`.
- Assigns files to monthly archives using a date in the filename, such as `alteryx-2026-05-13.2.csv`.
- Falls back to `LastWriteTime` when a filename does not contain a date.
- Replaces an existing archive entry when the same file is added again.
- Preserves source-relative paths in the archive, prefixed with the configured log root name.
- Deletes only local files older than the configured retention period, and only after those files have been successfully added to the archive.
- Skips locked or unreadable files and leaves them on the server.

## Configuration

Copy `Archive-AlteryxLogs.config.example.psd1` to `Archive-AlteryxLogs.config.psd1`, then edit it for the server.

Example:

```powershell
@{
    # Number of days to keep on the Alteryx Server.
    RetentionDays = 7

    # Network share, mapped drive, or synced cloud folder.
    ArchiveDestinationPath = '\\server\share\AlteryxLogArchives'

    # Monthly archive files are named like AlteryxLogs-2026-05.zip.
    ArchiveNamePrefix = 'AlteryxLogs'

    # Prefer dates embedded in filenames, with LastWriteTime as fallback.
    UseFilenameDate = $true

    # Use $true for a dry run, then $false when ready.
    # This has the same effect as running the script with PowerShell's native -WhatIf switch.
    DryRun = $true

    LogRoots = @(
        @{
            Name = 'Gallery Logs'
            Path = 'D:\Program Files\Alteryx\Gallery\Logs'
            IncludePatterns = @('*.csv', '*.log', '*.txt')
            ExcludePatterns = @()
        }
    )
}
```

The `.psd1` format is used because it is easy to edit by hand, supports comments, and can be read natively by PowerShell using `Import-PowerShellDataFile`.

`ArchiveDestinationPath` must already exist and must be writable by the account running the script. For cloud storage, use a locally available folder, such as a OneDrive sync folder, SharePoint sync folder, mapped drive, or UNC path.

Add more `LogRoots` entries for additional Alteryx log folders. `IncludePatterns` and `ExcludePatterns` use PowerShell wildcard patterns.

## Running Manually

Dry run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\Alteryx-Server-maintenance\Archive-AlteryxLogs.ps1" -ConfigPath "D:\Alteryx-Server-maintenance\Archive-AlteryxLogs.config.psd1"
```

Set `DryRun = $false` in the config file when the dry-run output is correct. You can also force a dry run from the command line with PowerShell's native `-WhatIf` switch:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\Alteryx-Server-maintenance\Archive-AlteryxLogs.ps1" -ConfigPath "D:\Alteryx-Server-maintenance\Archive-AlteryxLogs.config.psd1" -WhatIf
```

## Task Scheduler

Create a scheduled task using an account that can read the Alteryx log folders and write to the archive destination.

Suggested action:

- Program: `powershell.exe`
- Arguments: `-NoProfile -ExecutionPolicy Bypass -File "D:\Alteryx-Server-maintenance\Archive-AlteryxLogs.ps1" -ConfigPath "D:\Alteryx-Server-maintenance\Archive-AlteryxLogs.config.psd1"`
- Start in: `D:\Alteryx-Server-maintenance`

Use a schedule that suits the server, usually daily outside peak usage.

## Exit Codes

- `0`: Completed successfully, including no-op runs where no matching files were found.
- `1`: Validation, archive, or deletion failure. Files that were not successfully archived are not deleted.

## Validation

Before archiving, the script validates:

- the config file exists and can be parsed;
- required config keys are present;
- `RetentionDays` is a positive whole number;
- `ArchiveDestinationPath` exists and is writable;
- each log root exists and is readable;
- include and exclude patterns are strings or arrays of strings.

## Developed By

Paul Houghton at [The Information Lab](https://www.theinformationlab.co.uk/).

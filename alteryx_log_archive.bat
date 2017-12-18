::-----------------------------------------------------------------------------
::
:: AlteryxServer Log File Archiving Script v.1 - 13/12/2017
:: Adapted from the AlteryxServer Backup Script located: bit.ly/2yWAPZF
::
::-----------------------------------------------------------------------------

@echo off

::-----------------------------------------------------------------------------
:: Set variables for Log and Temp directories
::-----------------------------------------------------------------------------

SET LogDir="D:\ProgramData\Alteryx\Backup_logs\"
SET TempDir="D:\Temp\Backup_logs\"
SET Workspace="D:\ProgramData\Alteryx\"
SET MongoBin="D:\MongoDB\Server\3.0\bin\"
SET BackupLogs="D:\ProgramData\Alteryx\Backup_logs\"
SET BackupDir="Z:\Backup\Logs\"

::-----------------------------------------------------------------------------
:: Set Date/Time to the format DD-MM-YYYY and create log
::-----------------------------------------------------------------------------
SET datetime=%DATE:/=-%

echo %date% %time%: Starting backup process > %LogDir%BackupLog-%datetime%.log
echo. >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: Copy all log files into central location for archiving
::-----------------------------------------------------------------------------

echo %date% %time%: Copying Service Logs >> %LogDir%BackupLog-%datetime%.log
robocopy %Workspace%Service %TempDir%Service /mov /s  >> %LogDir%BackupLog-%datetime%.log

echo %date% %time%: Copying Gallery Logs >> %LogDir%BackupLog-%datetime%.log
robocopy %Workspace%Gallery\Logs %TempDir%Gallery /mov /s  >> %LogDir%BackupLog-%datetime%.log

echo %date% %time%: Copying Engine Logs >> %LogDir%BackupLog-%datetime%.log
robocopy %Workspace%Engine\logs %TempDir%Engine /mov /s  >> %LogDir%BackupLog-%datetime%.log

echo %date% %time%: Copying Error Logs >> %LogDir%BackupLog-%datetime%.log
robocopy %Workspace%ErrorLogs %TempDir%ErrorLogs /mov /s  >> %LogDir%BackupLog-%datetime%.log

echo %date% %time%: Copying Backup Logs older than 7 days >> %LogDir%BackupLog-%datetime%.log
robocopy %BackupLogs% %BackupLogs%BackupLogs /move /minlad:7 /s /e >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: This section compresses the backup to a single zip archive
::
:: Please note the command below requires 7-Zip to be installed on the server.
:: You can download 7-Zip from http://www.7-zip.org/ or change the command to
:: use the zip utility of your choice.
::-----------------------------------------------------------------------------

echo %date% %time%: Archiving backup >> %LogDir%BackupLog-%datetime%.log

"c:\Program Files\7-Zip\7z.exe" a %TempDir%ServerLogs_%datetime%.7z %TempDir%ServerLogs_%datetime% >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: Move zip archive to network storage location and cleanup local files
::-----------------------------------------------------------------------------

echo. >> %LogDir%BackupLog-%datetime%.log
echo %date% %time%: Moving archive to network storage %BackupDir% >> %LogDir%BackupLog-%datetime%.log
echo. >> %LogDir%BackupLog-%datetime%.log

:: Be sure to update the UNC path for the network location to copy the file to.

robocopy %TempDir% %BackupDir% /mov /s >> %LogDir%BackupLog-%datetime%.log
rmdir /S /Q %TempDir%ServerBackup_%datetime% >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: Done
::-----------------------------------------------------------------------------

echo. >> %LogDir%BackupLog-%datetime%.log
echo %date% %time%: Backup process completed >> %LogDir%BackupLog-%datetime%.log

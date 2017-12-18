::-----------------------------------------------------------------------------
::
:: AlteryxServer Backup Script v.1 - 25/5/16
:: Created By: Kevin Powney - original script located: bit.ly/2yWAPZF
:: Modified By: Paul Houghton 13-Nov-2017
::
::-----------------------------------------------------------------------------

@echo off

::-----------------------------------------------------------------------------
:: Set variables for Log and Temp directories
::-----------------------------------------------------------------------------

SET LogDir="D:\ProgramData\Alteryx\Backup Logs"
SET TempDir="D:\Temp\"
SET MongoBin="C:\Program Files\MongoDB\Server\3.4\bin\"
SET BackupDir="Z:\Backup\Logs"

::-----------------------------------------------------------------------------
:: Set Date/Time to the format DD-MM-YYYY and create log
::-----------------------------------------------------------------------------
SET datetime=%DATE:/=-%

echo %date% %time%: Starting backup process > %LogDir%BackupLog-%datetime%.log
echo. >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: Stop Alteryx Service
::-----------------------------------------------------------------------------

echo %date% %time%: Stopping Alteryx Service >> %LogDir%BackupLog-%datetime%.log
echo. >> %LogDir%BackupLog-%datetime%.log

net stop AlteryxService >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: Backup MongoDB to local temp directory.
::-----------------------------------------------------------------------------

echo %date% %time%: Starting MongoDB Backup >> %LogDir%BackupLog-%datetime%.log
echo. >> %LogDir%BackupLog-%datetime%.log

pushd %MongoBin%
mongodump --quiet -o %TempDir%\ServerBackup_%datetime%\Mongo >> %LogDir%BackupLog-%datetime%.log
popd

::-----------------------------------------------------------------------------
:: Backup MongoDB to local temp directory.
::-----------------------------------------------------------------------------

echo. >> %LogDir%BackupLog-%datetime%.log
echo %date% %time%: Backing up settings, connections, and aliases >> %LogDir%BackupLog-%datetime%.log
echo. >> %LogDir%BackupLog-%datetime%.log

copy C:\ProgramData\Alteryx\RuntimeSettings.xml %TempDir%ServerBackup_%datetime%\RuntimeSettings.xml
copy C:\ProgramData\Alteryx\Engine\SystemAlias.xml %TempDir%ServerBackup_%datetime%\SystemAlias.xml
copy C:\ProgramData\Alteryx\Engine\SystemConnections.xml %TempDir%ServerBackup_%datetime%\SystemConnections.xml

::-----------------------------------------------------------------------------
:: Restart Alteryx Service
::-----------------------------------------------------------------------------

echo %date% %time%: Restarting Alteryx Service >> %LogDir%BackupLog-%datetime%.log
echo. >> %LogDir%BackupLog-%datetime%.log

net start AlteryxService >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: This section compresses the backup to a single zip archive
::
:: Please note the command below requires 7-Zip to be installed on the server.
:: You can download 7-Zip from http://www.7-zip.org/ or change the command to
:: use the zip utility of your choice.
::-----------------------------------------------------------------------------

echo %date% %time%: Archiving backup >> %LogDir%BackupLog-%datetime%.log

"c:\Program Files\7-Zip\7z.exe" a %TempDir%ServerBackup_%datetime%.7z %TempDir%ServerBackup_%datetime% >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: Move zip archive to network storage location and cleanup local files
::-----------------------------------------------------------------------------

echo. >> %LogDir%BackupLog-%datetime%.log
echo %date% %time%: Moving archive to network storage %BackupDir% >> %LogDir%BackupLog-%datetime%.log
echo. >> %LogDir%BackupLog-%datetime%.log

:: Be sure to update the UNC path for the network location to copy the file to.
robocopy %TempDir% %BackupDir% *.7z /mov >> %LogDir%BackupLog-%datetime%.log

del %TempDir%ServerBackup_%datetime%.7z >> %LogDir%BackupLog-%datetime%.log
rmdir /S /Q %TempDir%ServerBackup_%datetime% >> %LogDir%BackupLog-%datetime%.log

::-----------------------------------------------------------------------------
:: Done
::-----------------------------------------------------------------------------

echo. >> %LogDir%BackupLog-%datetime%.log
echo %date% %time%: Backup process completed >> %LogDir%BackupLog-%datetime%.log

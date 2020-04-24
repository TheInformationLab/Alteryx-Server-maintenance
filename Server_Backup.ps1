# Define backup variables
$install_path = "D:\Program Files\Alteryx"
$backup_root = "D:\Backups"

# change to Alteryx install path
Set-Location -path $install_path

# Set Alteryx service and timeout variables
$process = Get-Process AlteryxService
$maximumRuntimeSeconds = 600

& "D:\Program Files\Alteryx\bin\AlteryxService.exe" stop

# wait for AlteryxService to Stop. Timeout if over max set time
try
{
    $process | Wait-Process -Timeout $maximumRuntimeSeconds -ErrorAction Stop
    Write-Warning -Message 'Alteryx successfully stopped. Continuing backup.'
}
catch
{
    Write-Warning -Message 'Alteryx Shutdown exceeded timeout, will be killed now.'
    Exit
}
$limit = (Get-Date).AddDays(-30)


# Delete files older than the $limit.
Get-ChildItem -Path $backup_root -Recurse -Force |
    Where-Object { !$_.PSIsContainer -and $_.CreationTime -lt $limit } |
    Remove-Item -Force

# Delete any empty directories left behind after deleting the old files.
Get-ChildItem -Path $backup_root -Recurse -Force |
    Where-Object { $_.PSIsContainer -and (Get-ChildItem -Path $_.FullName -Recurse -Force |
    Where-Object { !$_.PSIsContainer }) -eq $null } |
    Remove-Item -Force -Recurse

# Create a new directory
$date = Get-Date -UFormat %Y-%m-%d
$backup_path = Join-Path $backup_root $date
$backup_file = -join ($date, ".zip")
Set-Location -path $backup_root

mkdir $backup_path
& "D:\Program Files\Alteryx\bin\AlteryxService.exe" emongodump=$backup_path
& "D:\Program Files\Alteryx\bin\AlteryxService.exe" start

# Move archive to zip file
7z a $backup_file $backup_path -r -sdel

#Sync backup folder to S3 bucket
aws s3 sync $backup_root s3://lner-alteryx/backups
exit
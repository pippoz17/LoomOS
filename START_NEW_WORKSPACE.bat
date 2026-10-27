@echo off
setlocal
cd /d "%~dp0"
if not exist instance\loomos.db goto launch
echo This will back up the current workspace database and start a new empty workspace.
echo Existing records will be retained in a backup file.
choice /m "Continue"
if errorlevel 2 goto launch
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
if not exist instance\backups mkdir instance\backups
copy /y instance\loomos.db "instance\backups\loomos_%STAMP%.db" >nul
if errorlevel 1 (
 echo Could not back up the current database. No changes were made.
 pause
 exit /b 1
)
move /y instance\loomos.db "instance\loomos_previous_%STAMP%.db" >nul
if errorlevel 1 (
 echo Could not move the current database. No changes were made.
 pause
 exit /b 1
)
echo Previous database retained in instance\backups and as instance\loomos_previous_%STAMP%.db.
echo A new empty workspace will be created on the next launch.
:launch
call START_LOOMOS.bat
endlocal

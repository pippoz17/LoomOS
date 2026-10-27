@echo off
setlocal
cd /d "%~dp0"
set "LOOMOS_SEED_SAMPLE_DATA=0"
echo.
echo  LOOMOS ^| TEXTILE OPERATIONS PLATFORM
echo  -----------------------------------
if not exist .venv\Scripts\python.exe (
  echo Creating Python virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
echo Installing / verifying package requirements...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo.
echo Starting LoomOS. Complete workspace setup on first launch if prompted.
echo Open http://127.0.0.1:5000 in your browser.
echo Press Ctrl+C in this window to stop the server.
.venv\Scripts\python.exe app.py
goto end
:failed
echo.
echo Startup failed. Review the Python / pip error above and check your internet connection.
pause
:end
endlocal

@echo off
REM Launch the Butterfly Bot OPTIONS dashboard on port 8050 and open it.
title Butterfly Bot Options Dashboard
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Run run.bat once to set it up.
    pause
    exit /b 1
)

REM Open the dashboard in the default browser a few seconds after the server starts.
start "" /b powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://localhost:8050'"

REM Run the server (this window stays open while the dashboard is running).
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8050

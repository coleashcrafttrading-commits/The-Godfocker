@echo off
REM Launch the Butterfly Bot dashboard on http://localhost:8000
cd /d "%~dp0"
if not exist ".venv\" (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo.
echo  Butterfly Bot running at  http://localhost:8000
echo  Press Ctrl+C to stop.
echo.
uvicorn app.main:app --host 127.0.0.1 --port 8000

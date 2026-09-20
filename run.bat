@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :nopython
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install pygame python-rtmidi mido
)

".venv\Scripts\python.exe" piano.py %*
goto :eof

:nopython
echo.
echo Python 3 was not found on PATH. Install it from https://python.org
echo and make sure "Add python.exe to PATH" is ticked.
pause

@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :nopython
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install pygame python-rtmidi mido pyinstaller

echo.
echo === selftest ===
".venv\Scripts\python.exe" piano.py --selftest || goto :failed

echo.
echo === building ===
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --icon icon.ico --name PianoforteMaestro piano.py || goto :failed

echo.
echo Done: dist\PianoforteMaestro.exe
pause
goto :eof

:failed
echo.
echo BUILD FAILED - see the output above.
pause
goto :eof

:nopython
echo Python 3 was not found on PATH. Install it from https://python.org
pause

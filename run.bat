@echo off
rem Run PS5 UltraPack from source on Windows x64 (Python 3.10 or newer on PATH).
cd /d "%~dp0"
set PYTHONUTF8=1
python -m pip install -r requirements-build.txt || goto failed
python PS5_UltraPack.py
exit /b %errorlevel%

:failed
echo Installing the Python packages failed.
pause
exit /b 1

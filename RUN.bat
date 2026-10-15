@echo off
title ps5-ultrapack
echo.
echo  ps5-ultrapack
echo.
set PYTHONUTF8=1
python -m pip install -r requirements-build.txt
python PS5_UltraPack.py
if errorlevel 1 python PS5_UltraPack.py
pause

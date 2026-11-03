@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
py -3 --version >nul 2>&1
if not errorlevel 1 (
  py -3 -B bootstrap.py %*
  goto done
)
python --version >nul 2>&1
if errorlevel 1 (
  echo Please install Python 3.11 or newer from https://www.python.org/downloads/
  pause
  exit /b 1
)
python -B bootstrap.py %*
:done
if errorlevel 1 (
  echo Startup failed. See README.md.
  pause
  exit /b 1
)
endlocal

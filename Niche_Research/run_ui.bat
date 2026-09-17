@echo off
setlocal
cd /d "%~dp0"
set "APP_PYTHON=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if exist ".venv\Scripts\python.exe" set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%APP_PYTHON%" set "APP_PYTHON=python"
"%APP_PYTHON%" -X utf8 main.py
if errorlevel 1 pause
endlocal

@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install demucs==4.1.0
if errorlevel 1 goto fail
echo Music removal installed.
pause
exit /b 0
:fail
echo Installation failed. Please check the error above.
pause
exit /b 1

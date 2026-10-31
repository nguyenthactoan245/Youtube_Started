@echo off
cd /d "%~dp0"
py -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe setup_ffmpeg.py
if errorlevel 1 goto fail
echo Installation complete. Open run.bat to start.
pause
exit /b 0
:fail
echo Installation failed. Please check the error above.
pause
exit /b 1

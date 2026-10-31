@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe main.py
) else (
    py main.py
)
if errorlevel 1 pause

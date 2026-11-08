@echo off
cd /d "%~dp0"
py -3.12 "phimora_ui.py"
if errorlevel 1 pause

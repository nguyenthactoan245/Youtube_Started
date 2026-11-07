@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" pythonw "phimora_ui.py" || python "phimora_ui.py"

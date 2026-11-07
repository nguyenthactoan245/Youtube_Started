@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3.12 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install demucs==4.1.0
if errorlevel 1 goto fail
.venv\Scripts\python.exe -c "import demucs.separate, torch; assert torch.cuda.is_available(), 'PyTorch khong nhan GPU CUDA'"
if errorlevel 1 goto fail
echo Music removal installed with CUDA GPU support.
pause
exit /b 0
:fail
echo Installation failed. Please check the error above.
pause
exit /b 1

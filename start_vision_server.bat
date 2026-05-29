@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [vision] missing .venv. Create one with: py -m venv .venv
    exit /b 1
)
set "VISION_PORT=%VISION_PORT%"
if "%VISION_PORT%"=="" set "VISION_PORT=8077"
echo [vision] checking torch/cuda availability...
".\.venv\Scripts\python.exe" -c "import torch; print('[vision] torch=' + str(torch.__version__)); print('[vision] cuda_available=' + str(torch.cuda.is_available())); print('[vision] cuda_device=' + (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'))" 2>nul
if errorlevel 1 (
    echo [vision] warning: unable to query torch/cuda diagnostics
)
echo [vision] launching on port %VISION_PORT% (first run downloads ~500MB model)
".\.venv\Scripts\python.exe" vision_server.py
endlocal

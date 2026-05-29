@echo off
setlocal
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0launch_neon_rift.ps1" %*
endlocal

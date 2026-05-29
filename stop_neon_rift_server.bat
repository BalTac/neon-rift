@echo off
setlocal
echo Stopping Neon Rift game server on :8080 ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F
)
if "%LLAMA_CPP_PORT%"=="" (
  set LLAMA_CPP_PORT=8091
)
echo Stopping llama.cpp server on :%LLAMA_CPP_PORT% ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%LLAMA_CPP_PORT%" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F
)
endlocal

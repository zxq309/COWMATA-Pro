@echo off
setlocal EnableDelayedExpansion
set "ENTRY_DIR=%~dp0"
set "PY="
for /f "delims=" %%i in ('where python.exe 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY if exist "D:\anaconda3\python.exe" set "PY=D:\anaconda3\python.exe"
if not defined PY (
  echo [ERROR] can not find python.exe
  pause
  exit /b 1
)
"%PY%" -m pip install -r "%ENTRY_DIR%..\model_runtime\requirements.txt"
pause

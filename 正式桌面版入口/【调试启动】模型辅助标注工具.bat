@echo off
setlocal EnableDelayedExpansion
set "ENTRY_DIR=%~dp0"
set "PY="
for /f "delims=" %%i in ('where python.exe 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  for %%P in (
    "%USERPROFILE%\anaconda3\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
    "C:\ProgramData\anaconda3\python.exe"
    "C:\ProgramData\miniconda3\python.exe"
    "C:\Users\%USERNAME%\anaconda3\python.exe"
    "D:\Users\%USERNAME%\anaconda3\python.exe"
    "D:\anaconda3\python.exe"
  ) do if not defined PY if exist "%%~P" set "PY=%%~P"
)
if not defined PY (
  echo [ERROR] can not find python.exe - install Python first
  pause
  exit /b 1
)
"%PY%" "%ENTRY_DIR%..\launch_model_assist.py"
pause

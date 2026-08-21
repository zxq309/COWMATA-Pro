@echo off
REM COWMATA Tail-Ring Annotator - one-click launcher (model-assisted mode, no console window)
setlocal EnableDelayedExpansion
set "ROOT=%~dp0.."
set "PYW="
for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  for %%P in (
    "%USERPROFILE%\anaconda3\pythonw.exe"
    "%USERPROFILE%\miniconda3\pythonw.exe"
    "C:\ProgramData\anaconda3\pythonw.exe"
    "C:\ProgramData\miniconda3\pythonw.exe"
    "D:\anaconda3\pythonw.exe"
  ) do if not defined PYW if exist "%%~P" set "PYW=%%~P"
)
if not defined PYW (
  echo [ERROR] pythonw.exe not found. Install Python 3.10+ then run install.bat
  pause
  exit /b 1
)
start "" "%PYW%" -m cowmata_tailring --mode model-assist

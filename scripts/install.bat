@echo off
REM COWMATA Tail-Ring Annotator - install the package and its dependencies
setlocal EnableDelayedExpansion
set "ROOT=%~dp0.."
set "PY="
for /f "delims=" %%i in ('where python.exe 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  for %%P in (
    "%USERPROFILE%\anaconda3\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
    "C:\ProgramData\anaconda3\python.exe"
    "D:\anaconda3\python.exe"
  ) do if not defined PY if exist "%%~P" set "PY=%%~P"
)
if not defined PY (
  echo [ERROR] python.exe not found. Install Python 3.10+ first.
  pause
  exit /b 1
)
echo Installing COWMATA Tail-Ring Annotator with model-assist extras...
"%PY%" -m pip install -e "%ROOT%[model]"
echo.
echo Video playback also requires VLC media player 3.x installed on Windows.
echo Done. Use launch.bat to start the application.
pause

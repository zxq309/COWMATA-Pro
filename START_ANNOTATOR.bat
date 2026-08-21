@echo off
setlocal
title COWMATA Tail-Ring Annotator

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "VENV_PYW=%VENV%\Scripts\pythonw.exe"

cd /d "%ROOT%"

if not exist "%VENV_PY%" (
  echo [COWMATA] First launch: creating the local Python environment...
  where py.exe >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv "%VENV%"
  ) else (
    where python.exe >nul 2>nul
    if errorlevel 1 goto :python_missing
    python -m venv "%VENV%"
  )
  if errorlevel 1 goto :setup_failed
)

"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :python_too_old

"%VENV_PY%" -c "import cowmata_tailring, PySide6" >nul 2>nul
if errorlevel 1 (
  echo [COWMATA] Installing the annotator into .venv. This is needed only once...
  "%VENV_PY%" -m pip install --upgrade pip
  if errorlevel 1 goto :setup_failed
  "%VENV_PY%" -m pip install -e "%ROOT%"
  if errorlevel 1 goto :setup_failed
)

if not exist "%VENV_PYW%" goto :setup_failed

start "" "%VENV_PYW%" -m cowmata_tailring --mode basic %*
exit /b 0

:python_missing
echo.
echo [ERROR] Python 3.10 or newer was not found.
echo Download Python from https://www.python.org/downloads/windows/
goto :pause_error

:python_too_old
echo.
echo [ERROR] The local .venv uses Python older than 3.10.
echo Delete .venv, install a current Python, and double-click this file again.
goto :pause_error

:setup_failed
echo.
echo [ERROR] The local environment could not be prepared.
echo Check the messages above, then run START_ANNOTATOR.bat again.
goto :pause_error

:pause_error
echo.
pause
exit /b 1

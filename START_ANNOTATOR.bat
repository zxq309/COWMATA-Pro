@echo off
setlocal
title COWMATA Multiview Annotator
cd /d "%~dp0"
if not exist "%~dp0runtime\pythonw.exe" goto missing
if not exist "%~dp0vendor\vlc\libvlc.dll" goto missing
if not exist "%~dp0vendor\ffmpeg\bin\ffprobe.exe" goto missing
set PYTHONHOME=
set PYTHONPATH=
set PYTHONUTF8=1
start "" "%~dp0runtime\pythonw.exe" -I -B "%~dp0portable_start.py" %*
exit /b 0
:missing
echo [ERROR] This portable package is incomplete.
echo Extract the COMPLETE COWMATA package, including runtime and vendor folders.
echo Do not install Python, delete .venv, or configure your system PATH.
pause
exit /b 1

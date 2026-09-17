@echo off
cd /d "%~dp0"
"%~dp0runtime\pythonw.exe" -I -B "%~dp0scripts\recover_update.py"

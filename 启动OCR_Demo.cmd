@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_demo.ps1" -Gui %*
if errorlevel 1 pause

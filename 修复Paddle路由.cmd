@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0fix_paddle_route.ps1"

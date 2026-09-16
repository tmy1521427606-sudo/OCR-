@echo off
"%~dp0.venv\Scripts\python.exe" "%~dp0创建批量任务.py" %*
if errorlevel 1 pause

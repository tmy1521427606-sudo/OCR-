@echo off
"%~dp0.venv\Scripts\python.exe" "%~dp0single_image_ocr_ui.py"
if errorlevel 1 pause

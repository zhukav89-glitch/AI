@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Удаляем старый кэш байт-кода, чтобы не запускались файлы прошлых версий
if exist "preop_check\__pycache__" rmdir /s /q "preop_check\__pycache__"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" preop_check\server.py
) else if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" preop_check\server.py
) else (
  python preop_check\server.py
)
pause

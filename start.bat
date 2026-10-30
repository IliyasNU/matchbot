@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.ps1 first to install dependencies.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" bot.py
if errorlevel 1 pause

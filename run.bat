@echo off
REM ===========================================================================
REM  Market Analyst - Windows launcher
REM  Double-click this file. It sets everything up on first run.
REM ===========================================================================
chcp 65001 >nul
cd /d "%~dp0"
title Market Analyst

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python is not installed, or not on PATH.
  echo Download it from https://www.python.org/downloads/
  echo and tick "Add Python to PATH" during installation.
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating the virtual environment...
  python -m venv .venv || (echo Failed to create the environment & pause & exit /b 1)
  echo [2/3] Installing dependencies - this takes a few minutes the first time...
  .venv\Scripts\python.exe -m pip install --upgrade pip -q
  .venv\Scripts\python.exe -m pip install -e . -q || (echo Install failed & pause & exit /b 1)
) else (
  echo [1/3] Environment is ready.
)

echo [3/3] Starting the dashboard...
echo.
echo   Open your browser at:  http://127.0.0.1:8000
echo   Press Ctrl+C to stop.
echo.
.venv\Scripts\analyst.exe serve --host 127.0.0.1 --port 8000
pause

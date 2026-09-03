@echo off
REM  Quick trial with no internet and no API keys - synthetic data.
chcp 65001 >nul
cd /d "%~dp0"
title Market Analyst - demo mode
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --upgrade pip -q
  .venv\Scripts\python.exe -m pip install -e . -q
)
.venv\Scripts\analyst.exe analyze --offline --no-alert
echo.
echo   Full report for one symbol:  .venv\Scripts\analyst.exe report XAUUSD --offline
echo   Dashboard:                   .venv\Scripts\analyst.exe serve --offline
echo.
pause

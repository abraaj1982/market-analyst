@echo off
REM  تجربة سريعة بدون انترنت وبدون اي مفاتيح - بيانات تركيبية
chcp 65001 >nul
cd /d "%~dp0"
title محلل الاسواق - وضع التجربة
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --upgrade pip -q
  .venv\Scripts\python.exe -m pip install -e . -q
)
.venv\Scripts\analyst.exe analyze --offline --no-alert
echo.
echo   لعرض تقرير كامل لرمز واحد:  .venv\Scripts\analyst.exe report XAUUSD --offline
echo   لتشغيل اللوحة:              .venv\Scripts\analyst.exe serve --offline
echo.
pause

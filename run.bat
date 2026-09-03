@echo off
REM ===========================================================================
REM  محلل الأسواق — تشغيل على ويندوز
REM  انقر نقراً مزدوجاً على هذا الملف. سيُجهّز كل شيء تلقائياً أول مرة.
REM ===========================================================================
chcp 65001 >nul
cd /d "%~dp0"
title محلل الأسواق

where python >nul 2>&1
if errorlevel 1 (
  echo [خطأ] Python غير مثبت او غير مضاف الى PATH.
  echo حمّله من https://www.python.org/downloads/  وفعّل خيار "Add Python to PATH"
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] انشاء البيئة الافتراضية...
  python -m venv .venv || (echo فشل انشاء البيئة & pause & exit /b 1)
  echo [2/3] تثبيت المكتبات - قد يستغرق دقائق في المرة الاولى...
  .venv\Scripts\python.exe -m pip install --upgrade pip -q
  .venv\Scripts\python.exe -m pip install -e . -q || (echo فشل التثبيت & pause & exit /b 1)
) else (
  echo [1/3] البيئة جاهزة.
)

echo [3/3] تشغيل لوحة التحكم...
echo.
echo   افتح المتصفح على:  http://127.0.0.1:8000
echo   لايقاف النظام: اضغط Ctrl+C
echo.
.venv\Scripts\analyst.exe serve --host 127.0.0.1 --port 8000
pause

#!/usr/bin/env bash
# محلل الأسواق — تشغيل على macOS / Linux
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] إنشاء البيئة الافتراضية…"
  python3 -m venv .venv
  echo "[2/3] تثبيت المكتبات…"
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -e . -q
else
  echo "[1/3] البيئة جاهزة."
fi

echo "[3/3] تشغيل لوحة التحكم على http://127.0.0.1:8000"
exec .venv/bin/analyst serve --host 127.0.0.1 --port 8000 "$@"

# دليل التشغيل

## أول مرة (ويندوز)

1. ثبّت [Python 3.11+](https://www.python.org/downloads/) — **فعّل خيار
   "Add Python to PATH"** أثناء التثبيت.
2. انقر نقراً مزدوجاً على **`run-demo.bat`** — تجربة فورية ببيانات تركيبية،
   بلا إنترنت وبلا مفاتيح.
3. للتشغيل الحقيقي: انقر **`run.bat`** ثم افتح <http://127.0.0.1:8000>.

## الأوامر

```bash
analyst analyze                    # تحليل كل قائمة المتابعة
analyst analyze XAUUSD EURUSD      # رموز محددة
analyst analyze --offline          # بيانات تركيبية، بلا إنترنت
analyst analyze --full             # مع التقرير الكامل لكل رمز

analyst report XAUUSD              # تقرير مفصّل لرمز واحد
analyst report XAUUSD --save-to r.txt

analyst digest                     # التقرير اليومي المجمّع
analyst digest --send              # وأرسله على تيليجرام

analyst serve                      # اللوحة + الجدولة الدورية
analyst serve --no-schedule        # اللوحة فقط

analyst stats                      # أداء التتبّع الأمامي الحقيقي
analyst status                     # فحص صحة الإعداد والتغطية
analyst test-alert                 # اختبار اتصال تيليجرام
analyst export XAUUSD -o a.json    # تصدير تحليل كامل للتدقيق
```

## المهام الدورية

| المهمة | التكرار | المصدر |
|---|---|---|
| دورة التحليل | كل 30 دقيقة (ملف swing) | `settings.yaml → profiles` |
| التقرير اليومي | 7:00 بتوقيت مسقط | `alerts.daily_digest_hour_local` |
| الصيانة + تتبّع النتائج | 2:30 UTC يومياً | مثبّتة في `scheduler/jobs.py` |

## الصيانة الدورية المطلوبة منك

| كل | المهمة |
|---|---|
| **سنة** | حدّث تواريخ FOMC في `config/calendar.yaml` من [موقع الفيدرالي](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) — دقيقتان |
| **شهر** | `analyst stats` وراجع الأداء التراكمي |
| **بعد 100 صفقة محسومة** | أعد معايرة `calibration` و`weights` (راجع `SCORING.md`) |
| **أسبوع** | راجع `data/logs/analyst.log` بحثاً عن `WARNING` متكررة |

## نسخ احتياطي

كل شيء في ملف واحد: `data/analyst.db`. أوقف النظام وانسخه.
هذا الملف يحوي كل الشموع المتراكمة وكل التحليلات وكل نتائج التتبّع — وهو
الأصل الحقيقي للمشروع مع مرور الوقت.

## حل المشاكل

| العَرَض | السبب والحل |
|---|---|
| `DataUnavailableError` لرمز واحد | المزود رفض الرمز. جرّب `fallback_symbols` في `watchlist.yaml` |
| كل الرموز NO_TRADE | طبيعي غالباً. راجع `analyst report <رمز>` لترى أي بوابة سقطت |
| اللوحة بلا شموع | مكتبة الرسم تُحمّل من CDN وتحتاج إنترنت. بقية اللوحة تعمل |
| لا تصل تنبيهات | `analyst test-alert` — تحقق من `.env` |
| `no space left on device` | احذف `data/logs/` القديمة أو خفّض `candle_retention_days` |
| البيانات لا تتحدث | تأكد أن `serve` يعمل بـ `--schedule` (الافتراضي) |

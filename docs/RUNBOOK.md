# Runbook

## First run (Windows)

1. Install [Python 3.11+](https://www.python.org/downloads/) — **tick "Add
   Python to PATH"** during installation.
2. Double-click **`run-demo.bat`** — an immediate trial on synthetic data, with
   no internet and no keys.
3. For the real thing: **`run.bat`**, then open <http://127.0.0.1:8000>.

## Commands

```bash
analyst analyze                    # analyse the whole watchlist
analyst analyze XAUUSD EURUSD      # specific symbols
analyst analyze --offline          # synthetic data, no internet
analyst analyze --full             # include the full report for each symbol

analyst report XAUUSD              # detailed report for one symbol
analyst report XAUUSD --save-to r.txt

analyst digest                     # daily roundup
analyst digest --send              # and send it over Telegram

analyst serve                      # dashboard + scheduler
analyst serve --no-schedule        # dashboard only

analyst stats                      # real forward-test performance
analyst backtest XAUUSD            # point-in-time replay of stored history
analyst calibrate                  # review whether confidence is honest

analyst status                     # configuration and coverage health
analyst test-alert                 # Telegram connectivity check
analyst export XAUUSD -o a.json    # full analysis as JSON

analyst company list               # the manual register
analyst company add BKMB --name "Bank Muscat" --price 0.240 \
    --dividend 0.018 --eps 0.030 --years-paid 7
analyst company news BKMB "Board raises dividend after record profit"
analyst company assess BKMB
```

## Scheduled jobs

| Job | Cadence | Configured in |
|---|---|---|
| Analysis cycle | every 30 minutes (swing profile) | `settings.yaml → profiles` |
| Daily digest | 07:00 local | `alerts.daily_digest_hour_local` |
| Maintenance + outcome tracking | 02:30 UTC | `scheduler/jobs.py` |

## Recurring maintenance

| Every | Task |
|---|---|
| **Year** | Refresh FOMC dates in `config/calendar.yaml` from the [Fed's calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) — two minutes |
| **Month** | `analyst stats` — review cumulative performance |
| **After ~100 resolved signals** | `analyst calibrate` and consider the suggested change |
| **Week** | Skim `data/logs/analyst.log` for repeated warnings |

## Backup

Everything lives in one file: `data/analyst.db`. Stop the system and copy it.
It holds every accumulated candle, every analysis and every tracked outcome —
over time it becomes the most valuable part of the project.

## Performance notes

A full analysis of one symbol takes roughly 80 ms, so a nine-symbol cycle is
well under a second. A backtest replays one step per N bars, at about the same
cost per step: a year of hourly data at `--step 6` is roughly 1,400 steps, so
expect a couple of minutes. Raise `--step` for a faster, coarser pass.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `DataUnavailableError` for one symbol | The provider rejected the ticker. Try `fallback_symbols` in `watchlist.yaml` |
| Everything reads NO_TRADE | Usually correct. `analyst report <symbol>` shows which gate failed |
| Dashboard has no chart | The chart library loads from a CDN and needs internet. Everything else works |
| No alerts arriving | `analyst test-alert`, then check `.env` |
| `no space left on device` | Clear `data/logs/` or lower `candle_retention_days` |
| Data not refreshing | Make sure `serve` is running with `--schedule` (the default) |
| Backtest reports 0 signals | Not enough stored history yet — run `analyst analyze` for a while first |

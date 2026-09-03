# Market Analyst

> Multi-engine analysis for metals, FX, US equities and indices, with a
> **transparent, auditable confidence score** — no AI guessing, no paid data
> subscriptions.

---

## The principle everything is built on

> **No system guarantees a fixed success rate.** An excellent backtest on
> historical data is usually overfitting, and it does not repeat live.

The goal is not prediction. It is to **measure setup quality** by the agreement
of independent schools of analysis, and then to measure the system against
outcomes it recorded in advance. Which means:

- No accuracy claims · No auto-trading · No black box
- Every number is re-derivable by hand from [`docs/SCORING.md`](docs/SCORING.md)
- Every signal is written down with its exact levels **before** the outcome is known

---

## Running it in two minutes

**Windows:** double-click **`run-demo.bat`** — no internet, no keys required.
For the real thing: **`run.bat`**, then open <http://127.0.0.1:8000>.

**macOS / Linux:**
```bash
./run.sh
```

**Manually:**
```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/analyst analyze --offline     # immediate trial
.venv/bin/analyst serve                 # dashboard + scheduler
```

---

## The nine engines

| Engine | What it reads | Applies to |
|---|---|---|
| **Multi-timeframe trend** | EMA 20/50/200, slope, Ichimoku, HH/HL structure | everything |
| **ICT / SMC** | liquidity sweeps, BOS/CHoCH, order blocks, FVGs, premium/discount | everything |
| **Classical TA** | clustered support/resistance, regression channel, Fibonacci, breakout-retest | everything |
| **Technical indicators** | RSI, MACD, Bollinger, stochastic, divergence — **read through the regime** | everything |
| **Macro & intermarket** | **10-year real yield**, dollar index, breakevens, VIX (from FRED) | metals, FX, equities |
| **COT positioning** | weekly CFTC report; crowding as a contrarian signal | metals, FX, indices |
| **Volume & seasonality** | Wyckoff effort-vs-result, monthly seasonality from stored history | where volume is real |
| **Fundamentals** | valuation, profitability, growth, leverage, dividends | equities only |
| **News & calendar** | NFP, CPI, FOMC — **never votes on direction**, only gates and modifies | everything |

Two more engines serve markets with **no price feed at all** (see below):
**dividend quality** and **news sentiment**.

---

## The confidence formula, in short

```
w_eff(i) = base × regime_multiplier × asset_class_multiplier × engine_quality
S        = Σ(w_eff × direction × strength) / Σ w_eff              ∈ [-1, +1]
D        = weighted variance around S                             ∈ [0, 1]
C        = 1 − λ·D                                                (coherence)
K        = L(|S|)                                                 (logistic calibration)
confidence = K × C × news_factor × data_quality × regime_fit
```

Every factor after `K` is a multiplier in (0, 1] — **nothing can raise
confidence, and the system can only ever talk itself down.**

**Grades:** `A+ ≥ 80%` · `A ≥ 70%` · `B ≥ 60%` · `C ≥ 45%` · else `NO_TRADE` —
**subject to** at least 3 active engines and ≥ 50% of the available weight.

The calibration constants were **measured across 90 scenarios**, not guessed:
a clean multi-timeframe trend produces median `|S|` of 0.46; a random walk,
0.165. Full derivation in [`docs/SCORING.md`](docs/SCORING.md).

---

## Hard gates

A single failed gate cancels the trade **regardless of confidence**. And
`NOT_EVALUATED` is not a pass — if the calendar failed to load, the news gate
was not cleared.

Data quality · higher-timeframe agreement · confidence floor · reward-to-risk ·
news blackout · engine coverage · earnings blackout · executability (short
selling) · volatility band — all in [`config/gates.yaml`](config/gates.yaml).

---

## Markets with no price feed (Muscat Stock Exchange)

There is no free, reliable price API for MSX. Rather than fake it, the system
offers a **manual company register** — the *Companies* tab in the dashboard, or
`analyst company` on the CLI.

You supply the published figures and the announcements. It assesses:

* **Dividend quality** — yield, cover, payout ratio, trend, payment record, and
  leverage pressure on the payout. Weighted toward *sustainability*: a 12% yield
  with a payout above 100% is scored as a warning, not an opportunity.
* **News tone** — a keyword lexicon working in **English and Arabic**, showing
  the matched terms so you can disagree with any reading, and overriding any of
  them by hand.

It produces **no entry, stop or target**, because without price history there is
nothing to base them on. Confidence is capped at 85%: two engines reading
hand-entered figures cannot justify near-certainty. Details in
[`docs/MSX.md`](docs/MSX.md).

---

## Data sources — free, no keys

| Source | Provides | Key? |
|---|---|---|
| Yahoo Finance | candles plus full equity fundamentals | no |
| Stooq | daily fallback | no |
| **FRED** (Federal Reserve) | real yields, dollar, inflation expectations, VIX | no |
| **CFTC** | the official weekly COT report | no |
| Local calendar | NFP/CPI from scheduling rules, FOMC from YAML | no |
| Telegram | alerts | free |

---

## Configuration

All behaviour lives in YAML; no code changes needed:

| File | Controls |
|---|---|
| `config/watchlist.yaml` | **the symbols you follow** — normally the only file you edit |
| `config/settings.yaml` | weights, thresholds, calibration, risk, alerts |
| `config/gates.yaml` | the hard gates and their parameters |
| `config/calendar.yaml` | FOMC dates (a two-minute annual refresh) |
| `.env` | Telegram only — the system runs fully without it |

---

## Measuring real performance

```bash
analyst stats        # live forward-test results
analyst backtest XAUUSD    # point-in-time replay of stored history
analyst calibrate    # is the confidence score actually honest?
```

Three rules keep the numbers from flattering themselves:

1. **No win rate below 30 resolved trades.** A ratio over ten trades is noise.
2. **The stop wins when one bar covers both stop and target.** Without intrabar
   data the order is unknowable, and assuming the favourable one is exactly how
   backtests manufacture profits that never survive live.
3. **Expectancy in R before win rate.** 40% at 3R beats 70% at 0.5R.

Plus a Wilson confidence interval on every ratio, and a **reliability curve**:
when the system said 70%, did roughly 70% of those work out?

Backtests exclude macro, COT and fundamentals entirely — no free point-in-time
archive exists for them, so including them would feed today's knowledge into a
past decision.

---

## Structure

```
src/analyst/
├── core/         types · settings · time
├── indicators/   indicators + structure primitives (ICT)
├── data/         providers · repository · quality · context
├── engines/      the nine engines
├── manual/       no-price-feed path: lexicon · dividends · sentiment
├── scoring/      aggregator · gates · risk plan
├── reporting/    deterministic templates (no LLM)
├── alerts/       suppression · Telegram
├── tracking/     forward-test ledger · live statistics
├── backtest/     point-in-time replay · metrics · calibration
├── storage/      SQLite / Postgres, with in-place migrations
├── api/ + web/   REST + dashboard
└── scheduler/    periodic jobs
```

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
[`docs/RUNBOOK.md`](docs/RUNBOOK.md)

---

## Tests

```bash
.venv/bin/python -m pytest       # 122 tests, all offline and deterministic
```

Covering indicator correctness, **absence of lookahead**, structure detection,
the scoring maths, hard gates, risk geometry, alert suppression, outcome
tracking, the bilingual sentiment lexicon, schema migration, and full
integration through the API and CLI.

---

## Deliberately out of scope

- **Automated trade execution** — deferred until months of real tracked outcomes exist
- **ML forecasting models** — unjustifiable at this sample size, and they turn the system into a black box
- **Elliott waves and complex chart patterns** — not detectable mechanically with enough reliability to earn a weight

---

## Disclaimer

This is an automated setup-quality analysis tool. **It is not investment advice
and it guarantees nothing.** Trading risks loss of capital. The decision and its
consequences are yours alone.

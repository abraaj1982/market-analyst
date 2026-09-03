# Architecture

## The full flow

```
             ┌──────────────────────────────────────────────┐
             │  Data providers — all free, no API key        │
             │  Yahoo · Stooq · FRED · CFTC · local calendar  │
             └───────────────────┬──────────────────────────┘
                                 ↓
                       CandleRepository
              (fetch → merge → accumulate in SQLite)
                                 ↓
                        ContextBuilder
   • native fetch per timeframe; 4H resampled from 1H, clock-anchored
   • the forming candle is dropped (repainting guard)
   • data quality scored across five checks
   • market regime detected (ADX + ATR% percentile + Bollinger bandwidth)
   • attaches macro · COT · fundamentals · calendar
                                 ↓
                          MarketContext
                    (immutable — pure input)
                                 ↓
     ┌───────────┬───────────┬───────────┬───────────┬──────────┐
     ↓           ↓           ↓           ↓           ↓          ↓
  Trend      ICT/SMC    ClassicTA   Indicators    Macro       COT
     └───────────┴─────┬─────┴───────────┴───────────┴──────────┘
                       ↓      Volume/Seasonality · Fundamentals · News
                  EngineResult[]
        (direction + strength + quality + numbered evidence)
                       ↓
                   Aggregator
        effective weight → consensus S → dispersion D → coherence C
              → calibration K = L(|S|) → confidence
                       ↓
        ┌──────────────┴──────────────┐
        ↓                             ↓
   GateEvaluator                  RiskPlanner
  (boolean hard gates)      (entry · stop · targets · R:R)
        └──────────────┬──────────────┘
                       ↓
                 AnalysisResult
                       ↓
     ┌─────────────┬───────────────┬────────────────┐
     ↓             ↓               ↓                ↓
   Report        Database      Dashboard      Telegram alert
  (templates)   + Signal       + REST API     (after dedupe)
                       ↓
                OutcomeTracker
        (what actually happened: TP / SL / expiry)
                       ↓
              Real performance statistics
                       ↓
                  Backtester
     (point-in-time replay + reliability curve → calibrate)
```

A second, deliberately separate path exists for companies with no price feed:

```
ManualCompany (hand-entered figures)  +  CompanyNews (announcements)
                       ↓
        DividendEngine        SentimentEngine (keyword lexicon, EN + AR)
                       ↓
          weighted mean, capped at 85% confidence
                       ↓
              CompanyAssessment — no trade plan, by design
```

---

## Decisions and their reasons

### Engines are pure functions
`Engine._run(MarketContext) → EngineResult`. No network, no database, no state.
That is what makes each engine individually testable and any stored analysis
exactly reproducible. The `analyse()` wrapper turns any crash inside an engine
into a `skipped_reason`: a failing engine never takes down the run, and the
aggregator redistributes its weight.

### Candles are stored, never just re-fetched
Free providers cap intraday history (60 days for 15m, two years for 1h).
Accumulating locally is the only path to multi-year intraday history — and the
only path at all for a market with no historical API.

### The forming candle is always dropped
The most recent bar from any provider is usually still open. Analysing it means
the output silently changes as the bar develops, and any historical study of it
is fiction.

### 4H is anchored to the clock, not to the download
`origin="epoch"` pins boundaries to 00/04/08/12/16/20 UTC. Without it the
boundaries drift with whatever bar the download happened to start on, and every
structure level drifts with them.

### ATR is measured as a percentage of price
Absolute ATR grows with price, so its percentile declares "record volatility" at
the top of every long uptrend and "dead calm" at the bottom of every downtrend.
That is a structural bias, not a market observation.

### Backtests see the same window as live
The replay trims each timeframe to the same bar count the live path fetches.
Without that, a decision replayed late in the history would see years of bars
where the live system sees weeks, and the two would compute different EMAs for
the same moment — at which point the backtest stops describing the system.

### Reports are deterministic templates, not generated text
Cheaper, faster, incapable of hallucinating, and byte-identical for identical
inputs — which makes them diffable and testable.

### Performance work that was also correctness work
* `find_swings` is vectorised with a sliding window (3.2x faster, provably
  identical output). It is the most-called function in the system.
* `percentile_of_last` replaced a `rolling().apply()` that computed a full
  series when every caller read only the final value (40x faster).
* The ICT engine looks at a bounded window of history: an order block from three
  thousand bars ago is not a level anyone trades against.

---

## File map

| Path | Responsibility |
|---|---|
| `core/` | Types, enums, settings, time, errors |
| `indicators/` | Pure maths: indicators and structure primitives |
| `data/providers/` | Every external source, behind one interface |
| `data/repository.py` | The only bridge between providers and the database |
| `data/quality.py` | Five checks → a [0,1] score that enters the formula |
| `data/context.py` | Builds `MarketContext` — the single I/O meeting point |
| `regime.py` | Market regime classification |
| `engines/` | Nine engines, each an independent school of analysis |
| `manual/` | Companies with no price feed: lexicon, engines, register |
| `scoring/` | Aggregator, gates, risk plan |
| `reporting/` | Deterministic templates and Telegram formatting |
| `alerts/` | Suppression and delivery |
| `tracking/` | Forward-test ledger and live statistics |
| `backtest/` | Point-in-time replay, metrics, calibration review |
| `storage/` | Schema, sessions, in-place migrations |
| `api/` + `web/` | REST API and the dashboard |
| `scheduler/` | Periodic jobs |

---

## Moving to PostgreSQL

Change `ANALYST_DATABASE_URL`. The SQLAlchemy schema is portable; the one
SQLite-specific construct is the `ON CONFLICT` upsert in `repository.store()`,
which needs its PostgreSQL equivalent.

## Schema migrations

`storage/db.py` applies in-place column renames on startup (`_RENAMES`).
`create_all` only ever creates missing tables — it never alters an existing one
— so an upgraded install would otherwise fail on the first insert.

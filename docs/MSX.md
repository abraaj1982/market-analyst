# Muscat Stock Exchange (MSX) — the honest position

## The short version

**There is no free, reliable API for MSX price data.** That is a technical fact,
not a limitation of this system. Anything claiming otherwise is either scraping
a site fragilely or selling an institutional feed.

## What was checked

| Source | What it offers | Verdict |
|---|---|---|
| ICE Consolidated Feed | The licensed official data (L1/L2 + history) | Institutional — hundreds to thousands per month |
| `msx.om` official site | 15-minute delayed data plus daily bulletins | Free, but effectively end-of-day and with no official API |
| TradingView | Covers MSX in the interface | No official data API — not usable programmatically |
| EODHD / TwelveData / Polygon | Coverage of MSX **unconfirmed** | Worth checking with a trial account |

## What this system does instead

Rather than produce a fake analysis, it offers a **manual company register** —
available in the dashboard under the *Companies* tab, and from the CLI under
`analyst company`.

A starter seed of nine large, liquid MSX names (banks, telecoms, cement,
investment, industrial — symbols verified against MSX's own site and
independent data vendors) ships at `seeds/msx_companies.csv`. Load it in one
shot instead of adding companies one at a time:

```bash
python scripts/import_companies.py seeds/msx_companies.csv
```

The CSV only carries `symbol,name,sector,currency` — add rows for any other
MSX company you want covered, or extend a row with the optional numeric
fields (`price`, `dividend_per_share`, `eps`, ...) once you have them; see
`scripts/import_companies.py` for the full column list. `analyst company add`
still works for one-off entries or edits after the bulk load.

You supply what is actually obtainable:

* **Published figures** — price, dividend per share, EPS, book value, leverage
* **Announcements** — headlines, in English or Arabic

And it assesses:

* **Dividend quality** — yield, cover, payout ratio, direction of travel,
  payment record, and leverage pressure on the payout
* **News tone** — a keyword lexicon scoring each announcement, with the matched
  terms shown so you can disagree with any of them

### What it does not do, and why

It produces **no entry, no stop and no target**. Without price history there is
no trend, no structure and no volatility measure to place them against.
Offering them anyway would be the dishonest part.

Confidence is also capped at **85%**: two engines reading hand-entered figures
cannot justify near-certainty.

### On the sentiment lexicon

It matches terms in English and Arabic, normalising Arabic orthography first
(`الأرباح` and `الارباح` are the same word). Longer phrases win over their
substrings, so "dividend cut" does not also score the positive term "dividend".

It does **not** understand negation, sarcasm, or context. A headline saying
"profit did not fall" reads as negative. Every item can be overridden by hand,
and the override wins. The report states this limitation rather than hiding it.

## If you do get price data

The architecture is ready. Add a provider:

```python
# src/analyst/data/providers/msx.py
class MsxProvider(PriceProvider):
    name = "msx"
    native_timeframes = (Timeframe.D1,)

    def fetch(self, instrument, timeframe, bars) -> pd.DataFrame:
        # return open/high/low/close/volume with a UTC DatetimeIndex
        ...
```

Register it in `runner.build_service()` and add the symbol to `watchlist.yaml`
with `market: msx`. Everything else adapts automatically:

* Engines that lack sufficient data disable themselves and say why
* The macro engine declines on MSX symbols — US macro says little about a local
  Omani name
* The `shortable` gate blocks bearish signals: in a market with no retail short
  selling, a bearish read means **exit or avoid**, not a short trade
* The coverage gate prevents a high grade resting on two engines

## Importing history from a file

```bash
analyst init-db
python scripts/import_csv.py BKMB history.csv --timeframe 1d
```

Column names are matched loosely in English and Arabic, and can be overridden
with `--column ts=Date`.

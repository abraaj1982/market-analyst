# The confidence score

> This is the authoritative reference for the maths. Every number the system
> prints can be recomputed by hand from this page and the `breakdown` object
> stored alongside each analysis.

---

## The governing principle

**No language model produces the confidence score.** There is no AI anywhere in
this project — even the written reports are deterministic templates over the
computed values. The reason is not purity: a number that cannot be re-derived
cannot be audited, and cannot be recalibrated later against real outcomes.

---

## The seven steps

### 1) Effective weight per engine

```
w_eff(i) = base_weight × regime_multiplier × asset_class_multiplier × engine_quality
```

| Factor | Source | Why |
|---|---|---|
| `base_weight` | `settings.yaml → weights.base` | Default importance of that school of analysis |
| `regime_multiplier` | `weights.regime_multipliers` | Oscillators mislead in trends; trend-following misleads in ranges |
| `asset_class_multiplier` | `weights.asset_class_multipliers` | Fundamentals are zero for metals; COT is zero for single equities |
| `engine_quality` | the engine itself | How much it trusts its own inputs |

**A design decision worth stating:** quality multiplies the **weight**, not the
**score**. An engine that distrusts its data loses *influence*; it is not pushed
toward neutral. Conflating those two lets a half-blind engine dilute a confident
one rather than simply stepping back.

### 2) Signed consensus

```
S = Σ( w_eff(i) × direction(i) × strength(i) ) / Σ w_eff(i)        ∈ [-1, +1]
```

### 3) Dispersion — the measure of disagreement

```
D = Σ( w_eff(i) × (signed(i) − S)² ) / Σ w_eff(i)                  ∈ [0, 1]
```

`D` is self-normalising: it reaches 1 only when weight is split evenly between
+1 and −1.

### 4) Coherence

```
C = 1 − λ·D                        (λ = dispersion_lambda, default 0.55)
```

### 5) Calibration — the step most systems skip

A weighted mean is **structurally compressed toward zero**, because most
sub-signals are quiet most of the time. Measured across 90 scenarios:

| Market type | median \|S\| | max \|S\| |
|---|---|---|
| Clean multi-timeframe trend | **0.460** | 0.525 |
| Realistic random walk | **0.165** | 0.516 |
| Choppy range | **0.069** | 0.369 |

Reading `|S|` directly as a percentage would put A+ **out of reach** and make the
grade scale decorative. So `|S|` passes through a normalised logistic (Platt
scaling):

```
        σ(k(x − x₀)) − σ(−k·x₀)
L(x) = ─────────────────────────         σ(z) = 1/(1+e^−z)
        σ(k(1 − x₀)) − σ(−k·x₀)

L(0) = 0   ·   L(1) = 1   ·   strictly increasing
```

With the current values (`x₀ = 0.33`, `k = 9.5`):

| \|S\| | 0.10 | 0.17 | 0.30 | 0.46 | 0.53 | 0.70 |
|---|---|---|---|---|---|---|
| **K** | 0.06 | 0.14 | 0.41 | **0.77** | 0.85 | 0.97 |

Calibration changes the **scale**, never the **ordering**: a stronger consensus
always yields a higher confidence.

> ⚠️ **`midpoint` and `steepness` are priors fitted on synthetic data.**
> Re-fit them from real forward-test outcomes with `analyst calibrate` once
> enough signals have resolved. This is not a detail — it is the difference
> between a system that learns and one that keeps repeating its author's
> assumptions.

### 6) Final confidence

```
confidence = K × C × news_factor × data_quality × regime_fit
```

Every factor after `K` is a multiplier in (0, 1]. **Nothing in this formula can
raise confidence above the calibrated consensus** — the system can only ever
talk itself *down*. That asymmetry is deliberate.

### 7) Grade

```
A+ ≥ 0.80      A ≥ 0.70      B ≥ 0.60      C ≥ 0.45      else NO_TRADE
```

And before any grade at all, **two coverage conditions**:

```
active_engines ≥ 3            AND            coverage_ratio ≥ 50%
coverage_ratio = Σ w_eff / Σ w_adjusted
```

These prevent the worst failure mode: one confident engine while eight stood
aside, arriving dressed as an A+. The ratio matters more than an absolute weight
floor, because available weight differs enormously between a metal and an
equity.

---

## Hard gates — entirely separate from the number

A gate is not a weight. A 95% confidence read with a high-impact release in 40
minutes is not a 95% opportunity; it is not an opportunity at all. Encoding that
as a small negative weight would let a strong enough consensus **buy its way
past it**, which is precisely what must not happen.

**`NOT_EVALUATED` is not a pass.** If the economic calendar failed to load, the
news gate was not cleared — it simply did not run, and the report says so.

Gates live in `config/gates.yaml`. A failed blocking gate automatically
downgrades any A/A+ signal to B in `pipeline.py`, so nothing downstream has to
remember to re-check.

---

## The manual company path

Companies with no price feed (see `MSX.md`) use the same weighted-mean maths with
two differences, both stated in the report:

* **No regime or asset-class multipliers.** They have no meaning without price
  history.
* **A hard ceiling of 85% confidence.** Two engines reading hand-entered figures
  cannot justify near-certainty regardless of how strongly they agree. Without
  the cap the maths happily returns 97%, which would be the most misleading
  number in the product.

---

## Recalibrating from reality

Once signals have accumulated:

1. **Confidence calibration.** Did roughly 70% of the setups in the 70–80% band
   actually reach target? `analyst calibrate` builds that reliability table and
   proposes a midpoint shift when the gap is both large and statistically real.
2. **Weight calibration.** Score each engine's contribution to predictive
   accuracy (Brier score, information coefficient). Raise what adds, lower what
   does not.
3. **Do not automate step 2.** Continuous auto-fitting on a small sample is
   overfitting in a nicer wrapper. The system *proposes*; a person *decides*.

# M3.1: Performance benchmark

Measured 2026-11-04 with `niftypilot/bench.py`. The data is SYNTHETIC (random walks, U-shaped
intraday volume), sized like the Nifty 50, and used **for timing only**. Real data could
behave somewhat differently: a gappier dataset triggers more candle-by-candle fallback replays
in `day_signal`, which would be slower.

Machine: Apple M2, 8 GB RAM, macOS 26.6 (arm64), Python 3.11.15, SQLite file DB. One run per
size, no warm-up, so expect run-to-run variation of roughly 10%.

```bash
uv run python -m niftypilot.bench --symbols 50 --days 250 --research --out bench.json
uv run python -m niftypilot.bench --symbols 50 --days 500 --out bench.json
```

## Dataset

| | 1 year | 2 years |
|---|---|---|
| Instruments | 50 + 1 index | 50 + 1 index |
| Sessions | 250 | 500 |
| 5m candles | 956,250 | 1,912,500 |
| SQLite size | 139 MB | 277 MB |

## Runtime per stage (seconds)

| Stage | 250 sessions | 500 sessions | Notes |
|---|---|---|---|
| Generate synthetic data | 14.6 | 29.6 | benchmark only |
| Ingest (validate + insert) | 16.4 | 33.1 | |
| Quality report | 10.6 | 21.5 | |
| Load market (DB to memory) | 12.5 | 26.4 | |
| Backtest, variant E, Rs 1L, cold | 32.3 | 66.9 | profiled; strategy decisions computed |
| Same backtest, warm cache | 1.8 | 4.0 | decisions reused; capital/execution change only |
| Sensitivity: 9 scenarios x E x Rs 1L | 19.1 | 36.5 | |
| Full research: A-E x 5 capitals, split + walk-forward (60/20 windows) | 133.5 | not run | |
| **Peak memory (max RSS)** | **529 MB** | **764 MB** | |

Every stage scales roughly linearly with sessions (about 2.0-2.2x for 2x the data).

**Estimate, not measured:** a 5-year research run would take about 133.5 s x 5, so roughly
11-12 minutes, and need about 1.3-1.5 GB of RAM. That fits an 8 GB machine.

## Bottlenecks (cold backtest profile, 250 sessions)

| Function | Cumulative time | Share of 32.3 s |
|---|---|---|
| `strategy.orb_vwap.evaluate` (1,150 candidate-days, ~20 ms each) | 22.5 s | 70% |
| ... `validation.validate_candles`, re-run inside every evaluate | 10.4 s | 32% |
| ... `indicators.relative_volume` | 5.1 s | 16% |
| ... `indicators.session_vwap` | 2.2 s | 7% |
| `MarketData.traded_value` (scanner input, cached afterwards) | 2.7 s | 8% |
| Per-slot bar lookups in the event loop | 1.5 s | 5% |

Memory is dominated by holding every candle twice: once as per-symbol frames and once as
per-day frames (`MarketData.by_day`).

## Not optimised (deliberately)

Correctness and reproducibility come first, and the current speed is adequate for the
research scope. If real data makes it necessary, these are the options in order of payoff:
1. Validate each candidate-day once instead of on every `evaluate` call. It is already
   validated at ingest; the strategy's own check is the safety net.
2. Precompute per-slot volume baselines per symbol for relative volume.
3. Drop the duplicate per-symbol frames after `by_day` is built.

Each would need the look-ahead and brute-force equivalence tests to keep passing unchanged.

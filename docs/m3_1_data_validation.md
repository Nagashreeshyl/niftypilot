# M3.1: Real-data validation

Status (2026-11-04): **the workflow is built and tested. No genuine market data has been
imported, because none has been supplied.** Nothing was scraped or generated as a substitute.

## 1. Audit of the M3 implementation (before changes)

| Area | Components | Finding |
|---|---|---|
| Import | `ingest.import_csv_directory`, `ingest_candles`, loaders for instruments/constituents/holidays | Worked, but `actual` data could be imported with no provenance. Raw files were not preserved or hashed |
| Datasets | `datasets` table, `data_kind`, single-dataset backtests | Mixing was prevented, but the `actual` label was self-declared. **Generated data could be labelled actual**: one M3 test did exactly that |
| Quality | `market_data/quality.py` | No provenance check. No check for index members without data (renames / suspensions). No symbol-change handling |
| OOS protection | `Experiment.oos_evaluated_at`, `evaluate_oos` one-shot lock | **Defect: the lock was per experiment only.** A second `research` run with a different period could put an earlier experiment's OOS into its development data, and the `backtest` command could run on OOS dates. Neither was recorded |
| Backtest / fees | `backtest/*`, `risk/charges.py` | Fee rates were unverified, which is stated in every report. Spread and slippage were reported combined, and entry delay was fixed at 1 candle |
| Unverified assumptions | spread 0.025%, slippage 0.05%, 10% volume participation, fee rates and rounding, regime thresholds, scanner thresholds | Unchanged in M3.1; the sensitivity analysis now covers spread, slippage and delay |

## 2. Fixes

- **Dataset-wide OOS seal** (`backtest/oos.py`):
  - Once an experiment exists, its OOS date range on that dataset is sealed for every later
    research run, backtest, sensitivity sweep and final evaluation.
  - Access is refused unless explicitly allowed (`--allow-oos-contamination`). An allowed
    access is written to the append-only `oos_access_log` and sets
    `experiments.contaminated = true` permanently.
  - A new experiment whose OOS was an earlier experiment's development or validation data is
    marked contaminated when it is created.
  - Research reports print `Out-of-sample status: untouched / evaluated once / CONTAMINATED`,
    together with the access log.
- **Actual data only through a provenance manifest.**
  - `create-dataset` no longer accepts `--kind actual`.
  - `import_csv_directory` and `ingest_candles` refuse actual datasets without declared
    provenance.
  - `load_market` refuses to backtest them.
  - The quality report marks them critical.
  - Instruments on the `SYNTH` exchange cannot enter an actual dataset.
- **Raw-file preservation:** every imported file is copied, unmodified and read-only, to
  `data/raw/<dataset>/<sha256[:16]>_<name>`. Its sha256, size, rows and date range are stored
  in `datasets.manifest`, and every candle's `source` carries the hash prefix.

## 3. Import workflow for genuine data

```bash
uv run python -m niftypilot.cli load-instruments instruments.csv
uv run python -m niftypilot.cli load-aliases symbol_changes.csv      # alias,trading_symbol,valid_from,valid_until,source
uv run python -m niftypilot.cli load-constituents nifty50_membership.csv
uv run python -m niftypilot.cli load-holidays nse_holidays_2020_2025.csv --source "NSE circular <ref>"
uv run python -m niftypilot.cli import-manifest data/vendor/manifest.json --raw-store data/raw
uv run python -m niftypilot.cli quality --dataset <name> --start ... --end ... --index "NIFTY 50"
```

Manifest (`market_data/provenance.py`). Every field is required, and placeholders such as
`unknown`, `n/a` or `TODO` are rejected:

```json
{
  "dataset": "vendorx-nifty50-5m-2020-2025",
  "provider": "Vendor X",
  "format": "mapped_csv",
  "permission": "Licence agreement ref ..., permits personal research use",
  "acquisition_date": "2026-11-01",
  "interval": "5m",
  "source_timezone": "Asia/Kolkata",
  "timestamp_convention": "start",
  "price_adjustment": "unadjusted",
  "adjustment_method": "raw exchange prices; no split/bonus/dividend adjustment",
  "columns": {"timestamp": "Date", "open": "Open", "high": "High", "low": "Low",
              "close": "Close", "volume": "Volume"},
  "timestamp_format": "%Y-%m-%d %H:%M:%S",
  "files": [{"path": "RELIANCE.csv", "symbol": "RELIANCE"}]
}
```

(`timestamp_convention` was added in M3.3 and is required; see m3_3_timestamp_conventions.md.)

Supported formats:
- `niftypilot_csv`: the columns `timestamp,open,high,low,close,volume`.
- `mapped_csv`: any CSV with a column mapping.

Vendor-specific or Groww API formats are **not** implemented. I have no authorised access to
their real responses, and no endpoint or schema was invented.

**What code can and cannot establish.** The manifest records a *declared* permission. Code
cannot verify a licence. The person importing is responsible for having one, and the
declaration is stored with the data so it can be audited.

## 4. Validation applied

| Check | Severity |
|---|---|
| OHLC consistency, NaN, non-positive price, negative volume, off-grid or out-of-session timestamps, conflicting duplicates | rejected at ingest; any rejection is critical in the quality report |
| Identical duplicates | collapsed (logged) |
| Candles on NSE holidays or weekends | critical |
| Missing candles / missing sessions | warning. Those sessions become `DATA_INVALID`; **nothing is filled** |
| Index member with no data on a trading day | warning (possible rename, suspension or vendor gap) |
| Symbol changes | files resolve through `instrument_aliases`. A file spanning a rename date is rejected (split it); an ambiguous alias is rejected |
| Overnight gap > 20% | critical if adjustment unknown; warning otherwise |
| Point-in-time membership missing for the period | critical |
| Provenance missing / synthetic instrument in an actual dataset | critical |
| Timezone | declared per dataset; naive timestamps are localised to it; canonical storage is UTC |

## 5. Tests

`tests/test_provenance.py` (13), `tests/test_oos_seal.py` (4), plus updates to
`test_quality.py` and `test_research.py`. The provenance tests use **generated** fixtures with
fake `TESTCO` symbols in a throwaway database. They exercise the import path; they are not
market data.

## 6. Needed to finish

1. Licensed 5-minute OHLCV for the Nifty 50 constituents **and** the Nifty 50 index, for the
   whole period, plus the licence reference.
2. Point-in-time Nifty 50 membership history (NSE index-change announcements) as CSV.
3. NSE trading-holiday lists for each year, with the circular references.
4. A symbol-change list for renamed constituents, if the vendor files use historical symbols.

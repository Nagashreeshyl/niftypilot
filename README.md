# NiftyPilot

Research-first algorithmic trading platform for NSE cash equity (Nifty 50 universe).
**Paper mode only.** No broker adapter or order code exists, and `trading_mode` accepts only `paper`.
Nothing here is evidence of profitability.

Status: **M1 and M2 done. M3 engine and M3.1 data/fee/sensitivity tooling built and tested on synthetic data; M3 not yet accepted:** no genuine market data and no verified fees yet (see docs/milestones.md).
See [docs/milestones.md](docs/milestones.md) for the audit, design decisions and known limitations.

## Setup (macOS)

```bash
cd backend
uv sync                          # Python 3.11+, installs deps into .venv
cp .env.example .env             # optional; defaults work
uv run alembic upgrade head      # creates niftypilot.db
```

## Verify

```bash
uv run ruff check .              # lint
uv run pytest -q                 # unit + integration tests (140, ~30 s)
uv run python -m niftypilot.demo                      # replay SYNTHETIC scenarios, Rs 5,000 capital
uv run python -m niftypilot.demo --capital 100000     # same setups; shows the risk-approved path
uv run python -m niftypilot.demo --persist            # also store synthetic candles/signals in the DB
uv run uvicorn niftypilot.api:app --reload            # read-only API, docs at http://127.0.0.1:8000/docs
```

## Backtesting (M3)

```bash
# engine check on SYNTHETIC data (fake symbols; results are not evidence of anything)
uv run python -m niftypilot.cli synthetic-dataset --days 260
uv run python -m niftypilot.cli quality  --dataset synthetic-random --start 2024-01-01 --end 2024-12-27
uv run python -m niftypilot.cli backtest --dataset synthetic-random --start 2024-03-01 --end 2024-12-27 \
    --benchmark SYNTH-INDEX --variant E --capital 100000        # report in reports/<run>/report.md
uv run python -m niftypilot.cli research --dataset synthetic-random --start 2024-03-01 --end 2024-12-27 \
    --benchmark SYNTH-INDEX --wf-train 60 --wf-test 20

# real data (licensed CSVs named <SYMBOL>_5m.csv)
uv run python -m niftypilot.cli load-instruments instruments.csv
uv run python -m niftypilot.cli load-constituents nifty50_history.csv
uv run python -m niftypilot.cli load-holidays nse_holidays.csv --source "NSE circular ..."
uv run python -m niftypilot.cli load-sessions nse_special_sessions.csv      # Muhurat / special / extended
uv run python -m niftypilot.cli load-corporate-actions corporate_actions.csv
uv run python -m niftypilot.cli load-aliases symbol_changes.csv          # historical symbols
uv run python -m niftypilot.cli import-manifest data/vendor/manifest.json  # actual data: provenance required
# manifest format: docs/m3_1_data_validation.md; originals are kept read-only in data/raw/
uv run python -m niftypilot.cli quality --dataset vendor-2020-2025 --start ... --end ... --index "NIFTY 50"
uv run python -m niftypilot.cli research --dataset vendor-2020-2025 --start ... --end ... \
    --index "NIFTY 50" --benchmark "NIFTY 50"
uv run python -m niftypilot.cli sensitivity --dataset vendor-2020-2025 --start ... --end ... \
    --benchmark "NIFTY 50" --variants A,E --capitals 5000,100000   # refused on sealed OOS dates
uv run python -m niftypilot.cli oos --experiment <id> ...    # once; re-runs are marked CONTAMINATED
uv run python -m niftypilot.cli reconcile-contract-note note.json  # real Groww note vs fee model
uv run python -m niftypilot.bench --symbols 50 --days 250 --research   # pipeline benchmark
```

Any run touching a sealed out-of-sample period is refused; `--allow-oos-contamination`
proceeds but permanently marks the experiment contaminated (logged in `oos_access_log`).

## Layout

```
backend/niftypilot/
  config.py            settings (env prefix NIFTYPILOT_); trading_mode: Literal["paper"]
  db.py, models.py     SQLAlchemy 2 schema (UTC-aware timestamps)
  market_data/         session calendar, CSV provider, validation, ingestion, resampling
  strategy/            indicators (VWAP, opening range, RVOL), regime classifier, ORB+VWAP state machine
  risk/                transaction charges, position sizing, pre-trade risk gate
  signals.py           strategy -> risk veto -> persisted signal (never an order)
  synthetic.py         deterministic SYNTHETIC data (SYNTH-* symbols, data_kind='synthetic')
  backtest/            event-driven simulator, execution model, metrics, reports, research
  cli.py               dataset / quality / backtest / research commands
  api.py               GET-only REST API
backend/alembic/       migrations
backend/tests/         pytest suite
```

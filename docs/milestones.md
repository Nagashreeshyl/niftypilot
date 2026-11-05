# Milestones: audit, decisions, status

## Audit of the starting repository (2026-11-04)

The `NIFTYPILOT` directory was **empty**: no Milestone 1 code, no tests, no git history. There was
nothing to preserve or audit. Because milestones may not be skipped, a lean M1 was built first
and M2 was built on top of it.

## M1 — Market data foundation: DONE

| Item | Where | Tested |
|---|---|---|
| Schema: instruments, constituent_history, candles, ingestion_runs | `models.py`, `alembic/versions` | migration up/down/`alembic check` |
| UTC-only canonical timestamps (naive datetimes rejected at the DB layer) | `db.UTCDateTime` | yes |
| `data_kind` = actual / synthetic / simulated on every candle and signal | `models.py` (CHECK) | yes |
| Point-in-time Nifty membership (`constituents_on(date)`) | `market_data/ingest.py` | yes |
| CSV provider + `CandleProvider` protocol for future Groww adapters | `market_data/providers.py` | yes |
| Validation: rejects NaN, non-positive prices, negative volume, bad OHLC, off-grid, out-of-session, conflicting duplicates; flags zero volume, outliers, missing slots | `market_data/validation.py` | yes |
| Idempotent ingestion (unique instrument+interval+ts) with an audit row per run | `market_data/ingest.py` | yes |
| 5m -> 15m / 1d resampling; incomplete buckets dropped, never filled | `market_data/resample.py` | yes |
| Freshness check | `validation.is_stale` | yes |

**Not built in M1 (deliberately):** Groww adapters (no authorised access; no endpoints invented),
retry/rate-limit handling (only needed once a network provider exists), a bundled NSE holiday
calendar or Nifty constituent list (real data must come from an authorised source; nothing is
invented), the liquidity scanner (next item, needs real daily data to calibrate thresholds).

## M2 — Strategy and indicator engine: DONE

### Design

- `strategy.evaluate(candles, history, as_of, regime, params)` is a **pure function**. It
  discards every candle not completed by `as_of`, so a backtester (M3) and a live loop share
  identical logic. Look-ahead is tested by appending a wild future crash and asserting that
  every earlier result is unchanged.
- **Opening range:** the first 3 completed 5m candles (09:15-09:30 IST). `or_minutes` is a
  parameter for research.
- **VWAP:** `sum(TP*V)/sum(V)`, TP = (H+L+C)/3, reset per IST session. Zero-volume candles add
  nothing. VWAP is undefined (NaN) until volume trades.
- **Missing candles / interruptions:** any missing or invalid candle in the session up to `as_of`
  gives `DATA_INVALID`. We do not compute VWAP across gaps and we never fill gaps.
- **Relative volume:** candle volume divided by the mean volume of the *same time slot* over
  the last 10 prior sessions. It needs at least 5 samples, otherwise `DATA_INVALID`.
- **Regime:** deterministic Nifty rules (change vs previous close, intraday range). `BEARISH`
  blocks longs by default. Thresholds are placeholders for M5 to test.
- **State machine:** breakout close above ORH (must also be above VWAP, meet RVOL, regime OK) ->
  retest (low within `retest_tolerance_pct` of ORH) -> buyer confirmation (bullish candle
  closing above the retest high and above VWAP). A close back below ORH/VWAP, a timeout or the
  entry cutoff invalidates the setup. One setup per session. Only the *first* breakout is
  evaluated; a rejected breakout ends the day for that instrument.
- **Levels:** entry = confirmation close (M3 decides the fill model), stop = retest low minus a
  buffer, rounded down to tick; target = 2R, rounded up to tick. Stop width outside 0.2-1.5%
  gives `NO_TRADE`.
- **Long-only:** a close below ORL is recorded as a `BEARISH_BREAKDOWN` research flag with a
  `NO_TRADE` outcome. No short order can be produced.
- **Risk gate (`risk/engine.py`):** has veto power. It checks system state, valid long levels,
  max trades/day, consecutive losses, daily loss, open positions, capital (no margin), the risk
  budget and net reward:risk after costs and slippage. Quantity = floor(budget / risk per share
  incl. slippage). It is then reduced until the full loss at stop, including charges, fits the
  budget. A trade that cannot fit one share is rejected, never upsized.
- Every outcome carries an explanation plus a list of `RuleCheck(rule, passed, value, threshold)`.
- Signals are unique per (instrument, strategy version, session, type). Changing parameters
  requires a new strategy version.

### Demonstration (synthetic data)

`python -m niftypilot.demo` replays six hand-built SYNTHETIC scenarios. Each lands on its
designed outcome: BUY_SETUP path, NO_TRADE, BREAKOUT_REJECTED (low RVOL), SETUP_INVALIDATED,
RETEST_PENDING, bearish breakdown.

**Notable honest result:** at Rs 5,000 capital and 0.5% risk, the valid synthetic setup is
`RISK_REJECTED`. The estimated net profit at a 2R target is Rs 6.78 against a net loss at stop of
Rs 24.39, because fixed brokerage minimums dominate tiny positions. At Rs 1,00,000 the same setup
is approved (qty 575). This suggests the strategy may be uneconomical at the planned
Rs 1,000-5,000 capital. M3 must test this on real data before anything else is concluded.

### Paper-only guarantees (tested)

- `NIFTYPILOT_TRADING_MODE=live` fails settings validation.
- The package contains no order functions and no HTTP client.
- The API exposes only GET routes; a POST returns 405.

## Known limitations

- **Charge schedule is unverified.** Rates in `risk/charges.py` are recalled defaults (Groww
  Rs 20 / 0.1% brokerage with an assumed Rs 5 minimum, STT 0.025% sell, NSE 0.00297%, SEBI
  0.0001%, stamp 0.003% buy, GST 18%). They are configurable and must be checked against
  current official schedules. Exchange rounding and the IPFT charge are not modelled.
- Tick size defaults to 0.05. NSE tick sizes vary by price band; set `instruments.tick_size`.
- The daily loss check uses realised P&L only. Unrealised P&L arrives with M4 positions.
- Emergency controls exist as `SystemState` inputs to the risk gate. Their persistence, logging
  (`risk_events`) and endpoints belong to M4, together with orders.
- No liquidity scanner yet, no ATR, no bid/ask spread handling (no data source for it).
- Only validated against synthetic data. **No real market data has been ingested.**
- Special sessions (Muhurat) and trading halts mid-session are not modelled. A halt shows up
  as missing candles, which gives `DATA_INVALID`.

## M3 — Historical backtesting: ENGINE BUILT; NOT ACCEPTED (2 criteria open)

Plan: [m3_plan.md](m3_plan.md). Everything below is verified on SYNTHETIC data only.

| # | Acceptance criterion | Status |
|---|---|---|
| 1 | Import and validate historical datasets | Done (CSV path tested). **No real data imported yet.** |
| 2 | Replay M2 strategies chronologically | Done |
| 3 | Realistic order simulation | Done (assumptions below) |
| 4 | Costs from **verified** effective-dated schedules | **Open.** The effective-dated structure exists, but no rate is VERIFIED (see fees) |
| 5 | Reproducible backtests | Done (content-addressed run_key; identical reruns tested) |
| 6 | Walk-forward validation | Done |
| 7 | Independent out-of-sample report | Done (one-shot lock) |
| 8 | Capital feasibility | Done mechanically; **real answer needs real data** |
| 9 | All tests pass | Done: 140 |
| 10 | No real orders possible | Done |

### What was built

- **Datasets and provenance:** `datasets` table (source, provider, data_kind, source timezone,
  price adjustment, quality status/report). Every candle belongs to one dataset, and a
  backtest loads exactly one, so synthetic and actual data cannot mix. Candles from before M3
  were migrated into a separate legacy dataset per data_kind. Loaders exist for instruments,
  point-in-time constituents and NSE holidays (the holiday source must cite the official
  circular).
- **Data-quality gate** (`market_data/quality.py`):
  - Critical findings block backtests: rejected ingestion rows, candles on holidays or
    weekends, invalid stored candles, overnight gaps > 20% when the price adjustment is
    unknown (possible unadjusted split), and no point-in-time membership.
  - Warnings are reported: missing candles or sessions, zero volume, outliers, no holiday
    calendar.
- **Fees** (`risk/charges.py`): broker, exchange and statutory rates are kept separately, each
  with effective dates, a status and a source. Per-leg buy/sell charges. Every report states
  whether net P&L is authoritative.
- **Simulator** (`backtest/engine.py`, `execution.py`):
  - Slot-by-slot replay over a point-in-time universe with a prior-data-only liquidity
    scanner.
  - Entries are marketable limit orders filled at the next candle's open. The limit is the
    signal entry + 0.1R. Size is computed at signal time against that worst-case price, so a
    stop-out stays within the risk budget.
  - Fills pay an assumed spread plus slippage, and participation is capped at 10% of candle
    volume (partial fills).
  - Stops that gap through fill at the open. Targets need a 1-tick trade-through. When a
    candle hits both, the stop is assumed first.
  - Square-off at 15:15, before Groww's 15:20 auto square-off and its Rs 50 + GST charge.
  - Cash, positions and MTM equity are tracked at every candle close.
- **Strategy changes (v1.1.0):** a `require_rvol` toggle, and the regime is evaluated *at the
  breakout candle close* (M2 accepted one value for the whole day, which a replay would turn
  into look-ahead). Version labels are content-addressed (`1.1.0+<hash>`), so they are
  immutable.
- **Metrics and reports:**
  - Metrics: gross/net P&L, costs, win rate, profit factor, expectancy (Rs and R), drawdown
    on intraday MTM, Sharpe (methodology in every report), exposure, and breakdowns by
    instrument, month, regime and hour.
  - Uncertainty: a seeded bootstrap CI and p-value.
  - Each report is JSON + Markdown with every trade, every rejected order, fee status, data
    quality and limitations. Synthetic reports are titled "NOT PERFORMANCE EVIDENCE".
- **Research** (`backtest/research.py`):
  - Variants A-E x Rs 5k/10k/25k/50k/1L on a 60/20/20 chronological split.
  - A pre-declared selection rule, validation check and walk-forward run inside
    development+validation only. Tests confirm that no development call touches OOS.
  - The trial count is reported with a Bonferroni-adjusted p-value.
  - OOS evaluation is one-shot; a forced re-run is labelled CONTAMINATED.
- **CLI** `python -m niftypilot.cli`, plus read-only `GET /datasets`, `/backtests`,
  `/experiments`. Candle queries now require `dataset_id`.

### Findings so far (engine behaviour, not market evidence)

- **Narrow stops do not survive costs.** With the M2 risk rule `min_net_reward_risk = 1`
  unchanged, a 0.64%-stop setup is rejected at Rs 5k and Rs 1L. It passes only at Rs 10L,
  where the Rs 20 brokerage cap makes costs small relative to position size. A 1.28% stop
  passes at Rs 1L.
- On 8 synthetic random-walk symbols over 210 days, 36 of 38 setups were risk-rejected at
  Rs 1L. At Rs 5k every variant reports **NO ECONOMICALLY VIABLE TRADE**, and the selection
  rule chose nothing at any capital. That is the correct answer for data with no structure.
- Whether real Nifty 50 stocks have stops wide enough to clear costs is **unknown** until
  real data is run.

### Fee audit (2026-11-04)

Sources: [Groww pricing](https://groww.in/pricing/stocks) and the
[Groww intraday help page](https://groww.in/help/stocks/sx-pricing/what-are-intraday-charges)
(primary for what Groww bills). [Zerodha bulletin](https://zerodha.com/marketintel/bulletin/391488/revision-in-transactions-charges-from-1st-october-2024)
for the NSE change on 1 Oct 2024. The NSE circular itself (FA64232) timed out and was not read.

| Component | Rate | Party | Status |
|---|---|---|---|
| Brokerage | lower of Rs 20 / 0.1% per order; minimum = lower of Rs 5 / 2.5% of value | broker | current only |
| STT | 0.025% sell | statutory | current only |
| NSE transaction | 0.00322% until 2024-09-30, then 0.00297% | exchange | secondary |
| NSE IPFT | 0.0001% (**missing in M2**) | exchange | current only |
| SEBI fee | 0.0001% | statutory | current only |
| Stamp duty | 0.003% buy, since 2020-07-01 | statutory | secondary |
| GST | 18% of brokerage + exchange + SEBI + IPFT | statutory | current only |

M2 errors corrected: the flat Rs 5 minimum, and the missing IPFT. **To close criterion 4:**
reconcile one real Groww contract note against `leg_charges`, then add effective dates from
the official NSE circulars, the Finance Act (STT) and SEBI orders, marking entries VERIFIED.

### Known limitations (M3)

- Spread is assumed (0.025% half-spread); there is no historical quote data.
- Risk sizing still applies slippage to the limit-order target (M2 rule, kept as the more
  conservative choice).
- Capital is not compounded. Loss limits are session-scoped.
- Runtime: about 40 s for research on 8 symbols x 210 days. 50 symbols x 5 years should take
  tens of minutes (not measured).
- Historical symbol changes (renames) are not modelled; map vendor files to current symbols.

## M3.1 — Real-data validation and fee verification: TOOLING DONE; BLOCKED ON INPUTS

Details: [data validation](m3_1_data_validation.md), [fee verification](m3_1_fee_verification.md),
[execution sensitivity](m3_1_execution_sensitivity.md), [benchmark](m3_1_performance_benchmark.md).

| Item | Status |
|---|---|
| Audit of M3 | Done. Three defects found and fixed: (1) the OOS lock was per experiment, so another experiment or a plain backtest could read a sealed OOS unrecorded; (2) `actual` data could be created without provenance, and generated data was once labelled actual in a test; (3) raw source files were not preserved |
| Provenance-gated import of actual data (manifest, raw-file store with sha256, aliases for renames, member-without-data check) | Done and tested |
| **Genuine data imported** | **No. None supplied** |
| Fee audit (formula, base, side, dates, source, status, rounding per component) | Done |
| **Fees officially verified** | **None.** Official documents unreachable; no contract note supplied |
| Contract-note reconciliation | Built; the real-note test skips with "verification blocked" |
| Execution sensitivity (spread, slippage, entry delay; spread and slippage reported separately) | Done; synthetic run only |
| Dataset-wide OOS seal + append-only access log + contamination labels | Done and tested |
| Benchmark | Done (50 instruments x 250 sessions; see benchmark doc) |

M3 acceptance criteria 4 (verified fees) and the real-data part of 1 and 8 remain **open**.
M4 must not start until they close.

## M3.2 — Data acquisition readiness: PLANNING ONLY (not acceptance of M3)

No code, data, fee status or strategy parameter changed. Documents:
[requirements](m3_2_data_requirements.md), [providers](m3_2_provider_comparison.md),
[index membership](m3_2_index_membership_sources.md),
[calendar and corporate actions](m3_2_exchange_calendar_and_corporate_actions.md),
[fee evidence checklist](m3_2_fee_evidence_checklist.md),
[acquisition decision](m3_2_acquisition_decision.md).

- **Providers investigated (9):** Groww, Zerodha Kite Connect, Upstox, Dhan, Angel One,
  ICICI Breeze, NSE Data & Analytics, TrueData, Global Datafeeds. **None is recommended
  yet.**
- **Groww is the conditional primary candidate:** the docs confirm 5m equities and indices
  from 2020, at Rs 4,999/yr. But its Terms (IV.2) restrict entering platform information "into
  a database", so written licence clarification is required.
- **NSE Data & Analytics** is the licence-clean reference; a quote is to be requested.
- **Real-data readiness gaps found in the existing code** (to be fixed before any real import,
  with approval):
  - special sessions (Muhurat, weekend or extended-hours days) would fail the quality gate;
  - no corporate-actions table, so lookback volumes are not split-adjusted;
  - no end-labelled timestamp option;
  - constituent history lacks source/verification columns.

**M3 remains NOT ACCEPTED.** Acceptance still needs genuine licensed data, verified fees and
a real research run.

## M3.3 — Historical data import readiness (not acceptance of M3)

The four structural gaps from M3.2 are fixed. Still no data acquired, no provider
selected, and no strategy, risk, execution, fee-status or OOS change. Documents:
[sessions](m3_3_exchange_sessions.md), [corporate actions](m3_3_corporate_actions.md),
[timestamps](m3_3_timestamp_conventions.md), [membership](m3_3_membership_provenance.md).

| Gap (M3.2) | Resolution |
|---|---|
| Special sessions would fail quality | `exchange_sessions` (muhurat, special, extended; source and verification). Days with a record are validated against their windows; undocumented sessions are still rejected. Non-regular sessions are never traded or used as history. Actual data with no holiday rows for a year is critical |
| No corporate-actions table | `corporate_actions` with factor, source, verification and data policy. Raw candles untouched; history adjusted at read time, or excluded when no factor is documented. Gaps on recorded ex-dates are reconciled |
| No end-labelled timestamps | Manifest `timestamp_convention` (start/end) is required; the timezone must be IANA and agree with any offsets in the file; original text kept in `candles.source_timestamp`; whole-file alignment gate; one convention per dataset |
| Membership has no provenance | `source_document`, `source_published`, `verification_status`, `verification_notes`; overlapping records rejected, or resolved explicitly by `superseded`; a missing source is critical for actual data |

Migration `750e5c6db339`. Tests: 211 passed, 1 skipped (the real contract note).

Stricter by design. For **actual** datasets these are new critical findings:
- `calendar_year_missing`;
- `membership_missing_source`;
- `conflicting_membership`.
For **synthetic** datasets, behaviour is unchanged apart from new warnings.

**M3 remains NOT ACCEPTED.** Acceptance still needs genuine licensed data, verified fees and
a real research run.

## Next

To finish M3 (unchanged by M3.1, which built the tooling): obtain authorised 5m data for Nifty 50 constituents and the index, the official
NSE holiday lists and point-in-time constituent history; close the fee verification; then run
`research` and report the result honestly, whatever it is. M4 (paper trading) waits for that.

# M3 implementation plan: historical backtesting and validation

## Constraints found during inspection (2026-11-04)

- **No real market data is available.** Nothing is scraped or invented. The engine is built and
  verified on SYNTHETIC data. The strategy's edge stays **unknown** until authorised 5m data is
  imported (CSV from a licensed vendor, or the Groww historical API once access exists).
- **Fee audit:** Groww's own pricing pages (fetched 2026-11-04) corrected two M2 errors.
  1. The brokerage minimum is "lower of Rs 5 or 2.5% of trade value", not a flat Rs 5.
  2. The NSE IPFT charge (0.0001%) was missing.
  Effective dates could not be verified from official circulars (the NSE archive timed out), so
  historical net P&L is flagged non-authoritative.
- Groww auto-squares off open intraday positions at 15:20 IST, charging Rs 50 + GST per
  position. The backtester exits at 15:15.

## Increments (each one ships with tests)

1. **Effective-dated fees** (`risk/charges.py`): broker, exchange and statutory rates kept in
   separate tables, each with a source, status and effective dates. Per-leg (buy/sell) charges.
   Each component is resolved for a trade date, and the result says whether it is authoritative.
2. **Datasets and provenance** (`datasets`, `trading_holidays`, `backtest_runs`, `experiments`
   tables, plus a migration). Candles belong to a dataset, and a run uses exactly one dataset,
   so synthetic and actual data can never mix. CSV loaders cover candles, constituents and
   holidays. A pre-backtest **data-quality report** returns a critical/warning verdict, with
   checks for corporate-action gaps and candles on holidays.
3. **Strategy variants A-E**: a `require_rvol` toggle, and `blocked_regimes=[]` to switch the
   regime filter off. Version strings are content-addressed (`1.1.0+<params hash>`), so they
   are immutable.
4. **Event-driven simulator**: replays each session slot by slot.
   - Order of work at each candle: fill pending entries at the candle open, then manage
     exits, then act on signals that closed with this candle.
   - Gap-through-stop fills at the open. Targets need a 1-tick trade-through. When stop and
     target fall in the same candle, the stop is assumed.
   - Spread and slippage are applied adversely. Partial fills are capped by volume
     participation. Entries that gap past tolerance are rejected.
   - Risk is re-checked at the fill price.
   - Point-in-time universe and liquidity scanner, holidays, cash/positions/MTM equity, and the
     15:15 square-off.
5. **Metrics and report**: every requested metric and breakdown, bootstrap confidence intervals,
   a Sharpe ratio with its methodology documented, and a JSON + Markdown report carrying
   provenance, fee status and every trade.
6. **Research**: variants A-E x capital Rs 5k-1L on a chronological development/validation/OOS
   split. Walk-forward runs inside development+validation only. A one-shot OOS lock is
   recorded in the DB. The trial count is reported with a Bonferroni-adjusted p-value.
7. **CLI, docs, end-to-end synthetic run.**

## Out of scope for M3

Live or broker execution, ML/RL, parameter search beyond the five named variants, bid/ask
data (spread is an assumption), and a corporate-actions database (gaps are detected and flagged,
not adjusted).

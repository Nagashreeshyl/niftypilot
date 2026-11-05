# M3.3: Corporate actions

Code: `backend/niftypilot/market_data/corporate_actions.py`, table `corporate_actions`, CLI
`load-corporate-actions`.

## Record

| Column | Notes |
|---|---|
| `instrument_id` | the instrument (ISIN in instrument metadata) |
| `action_type` | `split`, `bonus`, `symbol_change`, `merger`, `demerger` |
| `announcement_date`, `ex_date`, `effective_date` | required |
| `record_date` | optional |
| `adjustment_factor` | **new shares per old share**: a 1:5 split (face value 10 to 2) gives 5; a 1:1 bonus gives 2. Required for split and bonus (database CHECK plus loader). Optional for merger and demerger. Never derived or guessed |
| `source` | filing or circular reference; required, placeholders rejected |
| `verification_status` | `unverified` or `verified` |
| `data_policy` | set by the loader from the type and factor (below). The database refuses `adjust_at_read` without a factor |
| `notes` | |

Unique key: `(instrument_id, action_type, ex_date)`.

## Original and adjusted data policy

- **Raw candles are stored as traded and never modified.** Adjustment happens only in
  memory, when one session is compared with earlier sessions (the relative-volume lookback,
  via `MarketData.history`).
- **`adjust_at_read`** (a factor is known): for sessions before the ex-date, prices are
  divided by the factor and volume is multiplied by it. Same inputs give identical output.
  The input frame is not modified.
- **`exclude_prior_history`** (merger or demerger with no reliable factor): sessions before
  the ex-date are **dropped from the comparison**. Fewer samples means relative volume becomes
  NaN, so the strategy's rvol rule fails and no trade results. This is how a comparison that
  lacks required information is rejected.
- **`no_price_effect`** (symbol change): identity is handled by `instrument_aliases` (M3.1).
  The record documents the event.
- **Datasets declared `adjusted` by the vendor** are not adjusted again. Only exclusions
  apply.
- **Not adjusted, because they don't need to be:**
  - intraday entries, stops and costs: same-session prices;
  - the scanner's traded value: price × volume is unchanged by a split;
  - the regime filter: it uses the index.

## Quality report

- An overnight gap above the threshold on a **recorded ex-date** becomes the warning
  `corporate_action_gap` and is no longer reported as `possible_corporate_action`.
- An unexplained gap keeps the M3.1 behaviour: critical when the dataset's adjustment is
  `unknown`, a warning otherwise.
- `unverified_corporate_action` is a warning.

## Tests

`tests/test_corporate_actions.py` covers:
- the loader's policy derivation;
- rejected records: no factor, zero factor, placeholder source, unknown type, unknown
  instrument;
- the database CHECK refusing a split without a factor;
- read-time adjustment: input untouched, reproducible, pre-ex only, no double adjustment of
  vendor-adjusted data;
- relative volume across a 2:1 split. Without the record the breakout bar reads 4.0x; with
  it, 2.0x; a merger with no factor gives NaN.
- quality gap reconciliation.

## Not done

- No corporate-action data was loaded. It must come from NSE or BSE filings and circulars.
- Dividends and rights are not modelled. Intraday P&L doesn't need them.
- Splitting a merged company's history into its predecessors is not modelled. Such history
  is excluded instead.

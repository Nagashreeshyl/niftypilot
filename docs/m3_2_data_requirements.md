# M3.2: Data requirements

Planning document. Nothing here has been acquired yet. "Must" items are needed for
NiftyPilot's existing validation and backtest rules (M1-M3.1) to work on real data.

## 1. Instruments

| Universe | Must have | Why |
|---|---|---|
| Nifty 50 index | 5m candles for the whole period (volume may be 0); symbol, exchange | regime filter (variants D, E) and benchmark |
| Historical Nifty 50 constituents | 5m candles for **every stock that was a member at any time in the period**, including stocks since removed, merged or delisted | point-in-time universe; without them the backtest has survivorship bias (see m3_2_index_membership_sources.md) |
| Instrument metadata | NSE trading symbol, ISIN, company name, segment (EQ), tick size, listing status, plus any **symbol changes** with dates | identity across renames (`instrument_aliases`); tick-size rounding |

Instrument identity: ISIN is the stable key across renames. The trading symbol is the key in
files. Provider-specific ids (for example Groww symbols like `NSE-NIFTY`) are recorded in
`instruments.provider_symbol` and never used as the identity.

## 2. Candle data (5 minutes)

| Field | Required | Notes |
|---|---|---|
| Instrument identifier | yes | trading symbol (+ ISIN in metadata) |
| Exchange | yes | NSE only |
| Trading date | derivable | from the timestamp in IST |
| Candle timestamp | yes | see conventions below |
| Open, High, Low, Close | yes | rupees, unadjusted (see adjustment) |
| Volume | yes | shares; 0 allowed for the index |
| Traded value | optional | stored if supplied; not used yet |
| Source identifier | yes | provider + file sha256 (recorded automatically by `import-manifest`) |

### Conventions NiftyPilot requires

- **Timezone:** the provider's timezone must be **stated**, not inferred. It is declared in the
  manifest (`source_timezone`). Storage is UTC.
- **Labelling: candle START time.** The regular session is 09:15-15:30 IST, giving 75 candles
  labelled 09:15 ... 15:25. If a provider labels candles by end time, every timestamp must be
  shifted by -5 minutes **during import, with the shift recorded in the manifest**. That
  option does not exist yet: today such data would fail the grid/session checks, which is
  safe but blocks import.
- **Opening and closing auctions:**
  - NSE runs an equity pre-open call auction before 09:15 and a post-close session after
    15:30 (timings per NSE; verify on the NSE market-timings page).
  - Candles must cover **only the regular continuous session**. Pre-open trades must not be
    separate candles; whether the vendor folds the pre-open equilibrium trade into the 09:15
    candle must be **documented by the vendor**, because it changes the opening range.
  - Post-close (closing-price) session trades must be excluded.
  - Candles outside 09:15-15:30 are rejected at import today.
- **Partial sessions** (trading halts, circuit-breaker pauses, exchange outages, extended
  hours after an outage):
  - Missing candles are not filled. Affected sessions become `DATA_INVALID` for the strategy
    and are counted in the quality report.
  - Candles *beyond* 15:30 on an extended day are currently rejected, which makes the dataset
    critical. Such days need an explicit special-session record (see the calendar doc). That
    is **not implemented yet**.
- **Missing candles:** a candle-less interval must be **absent**, never zero-volume filled or
  forward-filled by the vendor. Ask every vendor whether they forward-fill. Forward-filled
  candles look valid and would silently corrupt VWAP and relative volume.
- **Price and volume adjustment:**
  - NiftyPilot requires **unadjusted** (as-traded) intraday prices and volumes, plus a
    separate corporate-action list. Intraday entries, stops and costs must use the prices that
    actually traded.
  - Adjusted series are acceptable only if the vendor documents the method and also supplies
    the raw series.
  - Mixing adjusted and unadjusted data in one dataset is not allowed (`price_adjustment` is
    per dataset).

## 3. Historical coverage

**Minimum 3 years; 5 or more preferred if licensing allows.** The binding limits are statistical,
not calendar.

| Purpose | 3 years | 5 years | Comment |
|---|---|---|---|
| Different regimes (trend up/down, sideways, high volatility) | likely several, but few occurrences of each | more occurrences of each | the regime filter is judged per regime, so each needs many trades, not many months |
| Strategy development (60% split) | about 1.8 years | about 3 years | |
| Walk-forward (120-day train / 40-day test windows) | about 9 windows | about 21 windows | more windows make selection stability measurable |
| Final out-of-sample (20%) | about 7 months | about 12 months | **probably too few trades either way** (below) |

**Why 5 years does not guarantee significance.**
- The system trades at most 2 times per day, and the M3 tests showed most setups are risk-
  rejected at small capital.
- A rough requirement: to tell a true expectancy of 0.1R from zero, with a per-trade standard
  deviation of about 1R, at 95% confidence, needs about (1.96 / 0.1)^2 = 384 trades.
- The 25-trial Bonferroni correction pushes this higher still.
- A 12-month out-of-sample period may hold only tens of trades. In that case the honest
  result is **"inconclusive"**, and NiftyPilot reports exactly that (the bootstrap CI and
  p-value are in every report).
- The trade count, not the number of years, decides what can be concluded.

Coverage per provider differs (Groww from 2020, Dhan the last 5 years, Upstox minutes from
January 2022; see the provider comparison). The start of a provider's history must be
**confirmed by actually downloading the earliest month**, not taken from marketing pages.

## 4. Supporting data (all required before M3 acceptance)

1. Point-in-time Nifty 50 membership, with source documents (index-membership doc).
2. NSE trading holidays and special sessions for every year, with circular references
   (calendar doc).
3. Corporate actions (splits, bonuses, mergers, symbol changes) for every constituent
   (calendar doc).
4. Fee evidence: official schedules and real contract notes (fee checklist).
5. A licence or permission statement covering storage and automated processing for personal
   research (acquisition decision).

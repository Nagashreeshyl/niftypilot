# M3.2: Acquisition decision

## Decision: no provider can be recommended yet

None of the investigated providers has, on the evidence available on 2026-11-04, all of:
- a confirmed licence for local storage and automated research processing;
- 5-minute coverage of at least 3 years;
- the Nifty 50 index;
- delisted / former constituents;
- documented timestamp and adjustment conventions.

Each of those is required by m3_2_data_requirements.md, so the honest decision is
**conditional**.

## Recommended route (two parallel enquiries, then decide)

**Route 1: Groww Backtesting API (primary candidate, conditional).** Why it leads:
- it is the broker you intend to use, so data and later execution come from the same source;
- official documentation confirms 5-minute candles for equities **and indices from 2020**
  (about 6.8 years by now);
- the price is low and published (Rs 4,999/year + tax).

Why it is not yet recommended:
- Groww's Terms (IV.2) restrict copying or entering platform information "into a database"
  except for personal, non-commercial use. XI.1.1 frames API use around automated trading.
  Whether bulk download and local storage of historical candles for personal backtesting is
  permitted is **ambiguous**.
- Timezone, candle labelling, adjustment, the exact start date and **delisted-instrument
  availability** are undocumented.

Action:
- Ask Groww support **in writing** the nine questions in m3_2_provider_comparison.md.
- Do not subscribe or download until the licence answer is received and saved.

**Route 2: NSE Data & Analytics (licence-clean reference).**
- It is the exchange's own data business, so licence terms will be explicit.
- Email marketdata@nse.co.in for a quote on historical 5-minute (or 1-minute, to be resampled)
  cash-market OHLCV for the Nifty 50 constituents including former members, plus the Nifty 50
  index, for 2020 onward, for **individual, non-commercial research use**.
- It may well be too expensive. Its value is a definitive answer on scope, conventions and
  licence.

**Decision rule once answers arrive:**
1. If Groww confirms in writing that personal local storage and backtesting are allowed,
   **and** a sample download passes the checks below, use Groww.
2. Otherwise, if the NSE quote is affordable, use NSE.
3. Otherwise, evaluate Dhan (5 years; Rs 499/month) or Kite Connect (Rs 500/month) with the
   same written questions. Each needs an account with that broker.
4. If none qualifies, M3 cannot be accepted, and the strategy must not proceed to paper
   trading on unvalidated evidence.

**Acceptance test for any candidate, before buying a full history:** obtain one month of
5-minute data for 3 instruments (one current constituent, one removed or merged former
constituent, and the Nifty 50 index). Import it into a *throwaway* dataset with a manifest and
run `quality`. It must show:
- 75 candles per regular session;
- timestamps starting at 09:15 IST;
- no forward-filled zero-volume runs;
- no unexplained gaps.

## Data and documents needed from you

1. **Written licence answers** from Groww (and, if pursued, an NSE Data & Analytics quote and
   terms). Save them; the manifest's `permission` field will cite them.
2. After the licence is confirmed: the **5-minute data** itself, for all 2020+ Nifty 50
   constituents including former members, plus the index, as files with a manifest.
3. **Point-in-time membership:** the NSE Indices press releases and circulars for every Nifty
   50 change in the period, or an official constituent archive (m3_2_index_membership_sources.md).
4. **NSE holiday circulars** for each year, plus circulars for special sessions in the period.
5. **Corporate-action list** (splits, bonuses, mergers, symbol changes) for the constituents,
   with sources.
6. **Groww contract notes**, redacted, per m3_2_fee_evidence_checklist.md.

## Code work that must precede a real import (not done in M3.2; needs your go-ahead)

From the calendar and corporate-action review:
- special-session support (today a vendor file containing a Muhurat or weekend session would
  fail the quality gate);
- a `corporate_actions` table with read-time adjustment of lookback volumes;
- an end-labelled timestamp option in the manifest;
- source/verification columns on constituent history.

These are real-data readiness fixes, not M4 features.

## What M3.2 did not do

No account opened, no API called, no data downloaded, no licence assumed, no fee status
changed, no code or strategy parameter changed. **M3 remains NOT ACCEPTED.**

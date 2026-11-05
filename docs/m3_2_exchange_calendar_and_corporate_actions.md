# M3.2: Exchange calendar and corporate actions

## Authoritative sources

| Need | Authoritative source | Found 2026-11-04 | Status |
|---|---|---|---|
| NSE trading holidays | NSE annual circular "Trading holidays for the calendar year YYYY" (Capital Market segment, `NSE/CMTR/...`), plus amendment circulars | 2026: circular NSE/CMTR/71775 dated 12 Dec 2025; amendment NSE/CMTR/72260 adds 15 Jan 2026 (Maharashtra municipal election), per search results; PDFs not opened | **Unverified until each year's circular is read** |
| Special sessions (Muhurat trading, weekend sessions, mock/live DR sessions) | NSE circulars per event | Search results state Muhurat trading on Sunday 8 Nov 2026 | Unverified |
| Halts, outages, extended hours | NSE circulars / press releases for the day | not researched per event | open |
| Symbol and name changes | NSE circulars ("change in name / symbol"), company filings | broker roundups only (secondary) | open |
| Splits, bonuses, rights, dividends | NSE corporate-actions pages and circulars; BSE corporate actions; company filings | NSE pages exist (manual lookup); no official bulk historical download found | open |
| Mergers, demergers, delistings | NSE circulars, NCLT-approved schemes in company filings, NSE Indices replacement announcements | not researched per event | open |

Do **not** use NSE's undocumented JSON endpoints, which need browser session cookies. Do not
use holiday lists from news or broker sites as sources; they were seen to contain errors such
as weekend dates.

## How NiftyPilot represents these events

| Event | Representation | Implemented? |
|---|---|---|
| Holiday | `trading_holidays` (date, exchange, description, **source** = circular ref) via `load-holidays --source` | yes |
| Weekend | not a trading day | yes |
| **Special session** (Muhurat, Saturday/Sunday live session, extended hours after an outage) | needs a `special_sessions` table: date, open time, close time, kind, source. The strategy should **not trade** non-standard sessions; validation should accept their candles instead of rejecting them | **NO. Current code treats weekend candles as critical and rejects off-hours candles, so a vendor file containing a special session would block the dataset.** Must be built before importing real data |
| Halt / partial session | candles simply absent, never filled; the session becomes `DATA_INVALID` | yes |
| Symbol change | `instrument_aliases` (alias, valid_from, valid_until, source); files spanning a change are rejected | yes |
| Split / bonus | a `corporate_actions` table: instrument, ex-date, type, ratio, source. Raw prices **stay unadjusted** | **No** (gaps over 20% are only detected and flagged) |
| Merger / demerger / delisting | membership end date, `active_status = false`, a corporate-action record; the instrument's history is kept | partly (membership and status yes; corporate-action table no) |
| Dividend | not needed for intraday P&L (no overnight positions); recorded for completeness | no |

### Why prices are never adjusted in storage

- The strategy trades intraday, and costs, stops and entries must use the prices that
  actually traded.
- Adjustment is needed in one place only: **relative volume and the scanner look back over
  previous sessions**. A split inside the 10- or 20-session lookback makes old volumes
  incomparable.
- The correct fix is to apply split/bonus factors **at read time** (volume x factor, price /
  factor) to *history only*, leaving the stored raw data untouched. The factor is recorded
  with each corporate action.
- Until this is built, the quality report's critical flag on unexplained overnight gaps
  (over 20%) is the safeguard. It refuses datasets with unknown adjustment status rather than
  computing wrong relative volumes silently.

### Required before real-data import (code; a later milestone, needs approval)

1. `special_sessions` table, validation support, and a strategy rule to skip non-standard
   sessions.
2. `corporate_actions` table, read-time factors for the lookback windows, and quality-report
   reconciliation (every flagged gap must match a recorded corporate action).
3. Source-document and verification-status columns on `constituent_history`.

No raw price is ever modified, and no missing historical record is invented.

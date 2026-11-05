# M3.3: Exchange session calendar

Code: `backend/niftypilot/market_data/calendar.py`, table `exchange_sessions`, CLI `load-sessions`.

## Model

A day is one of three kinds:

| Day kind | Rule | Where it comes from |
|---|---|---|
| **Regular** | Weekday, not a recorded holiday. One window, 09:15-15:30 IST. | `trading_holidays` (official NSE circular, `load-holidays --source`) |
| **Closed** | Weekend or recorded holiday, with no session record | as above |
| **Documented non-regular** | Has one or more `exchange_sessions` rows. Candles are valid **only inside those windows** | `exchange_sessions` (`load-sessions`) |

`exchange_sessions` columns:
- `exchange`, `session_date`, `session_type` (`muhurat` | `special` | `extended`);
- `open_time`, `close_time`: Asia/Kolkata wall-clock (India has no daylight saving);
- `source`: the NSE circular reference, required, placeholders rejected;
- `verification_status` (`unverified` | `verified`), `notes`.

A date may hold several windows, for example a weekend live session in two parts. Overlapping
windows are rejected. Unique key: `(exchange, session_date, open_time)`.

Session types:
- **muhurat:** the Diwali evening session, usually on a holiday or weekend;
- **special:** for example a Saturday live session for a disaster-recovery switchover;
- **extended:** a regular weekday whose window changed, for example trading extended after
  an outage.

CSV for `load-sessions`:
```
date,session_type,open_time,close_time,source,verification_status,notes
YYYY-MM-DD,muhurat,HH:MM,HH:MM,NSE circular <ref>,unverified,
```
No session dates are bundled. They must come from NSE circulars. The dates in the tests are
arbitrary fixtures.

## Validation (regular and special stay distinguishable)

- **Regular days:** rules are unchanged from M1-M3.1 (`off_grid`, `outside_session`). Any
  candle outside 09:15-15:30 on a day without a record is rejected, so **an unusual session
  is never accepted without a record**.
- **Days with a record:** each candle must lie inside one window, on the 5-minute grid
  counted from that window's open. Otherwise it is rejected as `outside_special_session`.
  Missing-candle checks use the window's own slots.
- **Quality report:**
  - `candles_on_non_trading_day` (critical): candles on a weekend or holiday that has no
    record;
  - `special_session_candles` (warning): documented sessions present, listed per instrument
    and type;
  - `unverified_session_record` (warning).
- **Actual datasets:** the report never assumes that every weekday traded. If a calendar year
  in the period has no holiday rows, that is a new critical finding, `calendar_year_missing`.
  Synthetic datasets keep the earlier warning, `no_holiday_calendar`.
  - Shortcut: "at least one holiday row in the year" stands in for "that year's circular was
    loaded".

## Strategy and backtest

Non-regular sessions are **never traded and never used as history**:
- `load_market` drops their candles from the in-memory market. Stored rows are untouched, and
  the dropped dates are listed in `dataset.excluded_special_sessions`.
- The engine skips such days, logging `skipped: non-regular session (exchange calendar)`.

Why: the strategy's opening range, 15:15 square-off and same-slot relative volume are
defined for the regular session only. A Muhurat hour would also consume a lookback slot.

## Tests

`tests/test_exchange_sessions.py` covers:
- regular, holiday and weekend windows;
- a Muhurat session accepted only with a record;
- a candle outside a documented window;
- two-window special sessions;
- extended sessions;
- an undocumented weekend session (critical);
- a holiday that has a Muhurat record;
- loader rejections;
- that the backtest never trades or learns from special sessions;
- that actual data never assumes a weekday calendar.

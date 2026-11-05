"""Candle validation. Rejects bad rows, flags suspicious ones, never fabricates candles.

Rejected (not stored): NaN fields, non-positive prices, negative volume, inconsistent OHLC,
timestamps off the interval grid or outside the session, conflicting duplicates.
Flagged only (stored): zero volume, close-to-close outliers, missing session slots.

With an ExchangeCalendar, days that have a documented non-regular session are validated
against that session's windows (issue kind `outside_special_session`); every other day keeps
the regular 09:15-15:30 rules. Weekend/holiday detection is the quality report's job.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from niftypilot.market_data.session import IST, SESSION_CLOSE, SESSION_OPEN, session_slots

if TYPE_CHECKING:
    from niftypilot.market_data.calendar import ExchangeCalendar

PRICE_COLS = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class Issue:
    kind: str
    timestamp: str | None
    detail: str
    rejected: bool


def validate_candles(
    df: pd.DataFrame,
    interval_minutes: int | None = 5,
    max_abs_return: float = 0.20,
    calendar: "ExchangeCalendar | None" = None,
) -> tuple[pd.DataFrame, list[Issue]]:
    """Return (clean rows sorted by time, issues). interval_minutes=None for daily candles."""
    issues: list[Issue] = []
    df = df.reset_index(drop=True)  # callers may pass concat'd frames with duplicate labels
    bad = pd.Series(False, index=df.index)

    def reject(mask: pd.Series, kind: str, detail: str) -> None:
        nonlocal bad
        for i in df.index[mask & ~bad]:
            issues.append(Issue(kind, _iso(df.at[i, "timestamp_utc"]), detail, True))
        bad |= mask

    reject(df[["timestamp_utc", *PRICE_COLS, "volume"]].isna().any(axis=1), "missing_field", "NaN")
    reject((df[PRICE_COLS] <= 0).any(axis=1), "non_positive_price", "price <= 0")
    reject(df.volume < 0, "negative_volume", "volume < 0")
    ohlc_bad = (df.high < df[["open", "close", "low"]].max(axis=1)) | (
        df.low > df[["open", "close", "high"]].min(axis=1)
    )
    reject(ohlc_bad, "invalid_ohlc", "high/low inconsistent with open/close")

    if interval_minutes:
        ist = df.timestamp_utc.dt.tz_convert(IST)
        minutes = ist.dt.hour * 60 + ist.dt.minute
        open_m = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
        close_m = SESSION_CLOSE.hour * 60 + SESSION_CLOSE.minute
        special = calendar.special if calendar else {}
        days = ist.dt.date
        sp = days.isin(list(special)) if special else pd.Series(False, index=df.index)
        off_grid = ((minutes - open_m) % interval_minutes != 0) | (ist.dt.second != 0)
        reject(off_grid.fillna(True) & ~sp, "off_grid", f"not aligned to {interval_minutes}m grid")
        outside = (minutes < open_m) | (minutes + interval_minutes > close_m)
        reject(outside.fillna(True) & ~sp, "outside_session", "outside 09:15-15:30 IST")
        if sp.any():
            ok = pd.Series(True, index=df.index)
            for i in df.index[sp]:
                t = ist[i]
                m = t.hour * 60 + t.minute
                ok[i] = t.second == 0 and any(
                    w.open.hour * 60 + w.open.minute <= m
                    and m + interval_minutes <= w.close.hour * 60 + w.close.minute
                    and (m - w.open.hour * 60 - w.open.minute) % interval_minutes == 0
                    for w in special[t.date()]
                )
            reject(sp & ~ok, "outside_special_session", "not on a documented session's grid")

    # Duplicates: identical copies collapse to one; conflicting copies are all rejected.
    dup_ts = df.timestamp_utc.duplicated(keep=False) & ~bad
    if dup_ts.any():
        groups = df[dup_ts].groupby("timestamp_utc")
        for ts, g in groups:
            if len(g.drop_duplicates(subset=[*PRICE_COLS, "volume"])) > 1:
                reject(df.timestamp_utc == ts, "conflicting_duplicate", "same timestamp, diff data")
            else:
                extra = df.index.isin(g.index[1:])
                reject(pd.Series(extra, index=df.index), "duplicate", "identical duplicate")

    clean = df[~bad].sort_values("timestamp_utc").reset_index(drop=True)

    for ts in clean.timestamp_utc[clean.volume == 0]:
        issues.append(Issue("zero_volume", _iso(ts), "volume == 0", False))
    rets = clean.close.pct_change().abs()
    for i in clean.index[rets > max_abs_return]:
        issues.append(
            Issue("outlier", _iso(clean.at[i, "timestamp_utc"]), f"|return|={rets[i]:.2%}", False)
        )
    if interval_minutes:
        issues.extend(_missing_slots(clean, interval_minutes, calendar))
    return clean, issues


def _missing_slots(clean: pd.DataFrame, interval_minutes: int, calendar=None) -> list[Issue]:
    present = set(clean.timestamp_utc)
    out = []
    for day in sorted(set(clean.timestamp_utc.dt.tz_convert(IST).dt.date)):
        special = calendar is not None and day in calendar.special
        slots = (
            calendar.slots(day, interval_minutes)
            if special
            else session_slots(day, interval_minutes)
        )
        for slot in slots:
            if pd.Timestamp(slot) not in present:
                out.append(Issue("missing_candle", _iso(slot), "slot absent", False))
    return out


def is_stale(last_candle_start: datetime, now: datetime, interval_minutes: int, grace_s=90) -> bool:
    """During market hours: True if the most recent completed candle is older than expected."""
    expected_close = last_candle_start + timedelta(minutes=interval_minutes)
    return (now - expected_close).total_seconds() > interval_minutes * 60 + grace_s


def _iso(ts) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts).tz_convert(UTC).isoformat()

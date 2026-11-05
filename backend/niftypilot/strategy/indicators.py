"""Intraday indicators. All functions expect a candle frame sorted by timestamp_utc
(candle START times) and use only rows passed in: callers control look-ahead.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from niftypilot.market_data.session import IST, SESSION_OPEN


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative VWAP, reset each IST session. Typical price = (H+L+C)/3.

    Zero-volume candles add nothing to either sum. Before any volume trades the VWAP is
    undefined (NaN), never a guess. Missing candles are simply absent; callers must reject
    sessions with gaps (see orb_vwap) rather than trust a VWAP built across one.
    """
    day = df.timestamp_utc.dt.tz_convert(IST).dt.date
    tpv = (df.high + df.low + df.close) / 3 * df.volume
    cum_v = df.volume.groupby(day).cumsum()
    return (tpv.groupby(day).cumsum() / cum_v.replace(0, np.nan)).rename("vwap")


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    end_utc: datetime  # first moment after the range is complete

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def width_pct(self) -> float:
        return self.width / self.low * 100


def opening_range(
    session: pd.DataFrame, or_minutes: int = 15, interval_minutes: int = 5
) -> OpeningRange | None:
    """Range of the first `or_minutes` of one session. None unless every candle is present."""
    if session.empty:
        return None
    day = session.timestamp_utc.iloc[0].tz_convert(IST).date()
    start = pd.Timestamp(datetime.combine(day, SESSION_OPEN, IST))
    end = start + timedelta(minutes=or_minutes)
    ts = session.timestamp_utc.dt.tz_convert(IST)
    window = session[(ts >= start) & (ts < end)]
    if len(window) != or_minutes // interval_minutes:
        return None
    return OpeningRange(
        float(window.high.max()), float(window.low.min()), end.tz_convert("UTC").to_pydatetime()
    )


def relative_volume(
    today: pd.DataFrame, history: pd.DataFrame, lookback_sessions: int = 10, min_sessions: int = 5
) -> pd.Series:
    """Volume / mean volume of the same time-of-day slot over prior sessions.

    Compares like with like (09:35 vs past 09:35s), never against whole-day averages.
    NaN where fewer than `min_sessions` prior samples exist or the baseline is zero.
    """
    if today.empty:
        return pd.Series(dtype=float, name="rvol")
    today_date = today.timestamp_utc.iloc[0].tz_convert(IST).date()
    h_ist = history.timestamp_utc.dt.tz_convert(IST)
    hist = history.assign(day=h_ist.dt.date, slot=h_ist.dt.time)
    hist = hist[hist.day < today_date]
    keep_days = sorted(hist.day.unique())[-lookback_sessions:]
    stats = hist[hist.day.isin(keep_days)].groupby("slot").volume.agg(["mean", "count"])
    stats.loc[(stats["count"] < min_sessions) | (stats["mean"] <= 0), "mean"] = np.nan
    slots = today.timestamp_utc.dt.tz_convert(IST).dt.time
    baseline = slots.map(stats["mean"]).astype(float)
    return (today.volume / baseline).rename("rvol")

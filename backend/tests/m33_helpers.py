"""Shared GENERATED fixtures for M3.3 tests (fake symbols, throwaway DBs). Not market data."""

from datetime import date, datetime, time, timedelta

import pandas as pd

from niftypilot import synthetic
from niftypilot.market_data.session import IST, window_slots


def bars(day: date, open_: time, close: time, price: float = 100.0, volume: int = 1000):
    """Flat candles covering [open_, close) on `day`, START-labelled, UTC."""
    rows = [
        (t, price, price + 0.1, price - 0.1, price, volume)
        for t in window_slots(day, open_, close, 5)
    ]
    return synthetic.frame(rows)


def ist(day: date, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, time(hh, mm), IST)).tz_convert("UTC")


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out

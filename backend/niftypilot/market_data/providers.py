"""Market-data provider adapters.

Canonical candle frame: columns timestamp_utc (tz-aware UTC, candle START), open, high, low,
close, volume. Groww adapters (historical/live) will implement the same protocol once API
access is authorised; none exists yet.
"""

from datetime import datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

CANDLE_COLUMNS = ["timestamp_utc", "open", "high", "low", "close", "volume"]


class CandleProvider(Protocol):
    name: str

    def fetch(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame: ...


def read_csv_candles(path: str | Path, assume_tz: str = "Asia/Kolkata") -> pd.DataFrame:
    """Read a CSV with columns timestamp,open,high,low,close,volume.

    Naive timestamps are interpreted in `assume_tz`. Unparseable numbers become NaN and are
    rejected later by validation rather than silently dropped here.
    """
    raw = pd.read_csv(path)
    raw.columns = [c.strip().lower() for c in raw.columns]
    missing = {"timestamp", "open", "high", "low", "close", "volume"} - set(raw.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    ts = pd.to_datetime(raw["timestamp"], errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(assume_tz)
    df = pd.DataFrame({"timestamp_utc": ts.dt.tz_convert("UTC")})
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(raw[col], errors="coerce")
    return df


class CsvProvider:
    """Reads `<directory>/<SYMBOL>_<interval>.csv`."""

    name = "csv"

    def __init__(self, directory: str | Path, assume_tz: str = "Asia/Kolkata"):
        self.directory = Path(directory)
        self.assume_tz = assume_tz

    def fetch(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        df = read_csv_candles(self.directory / f"{symbol}_{interval}.csv", self.assume_tz)
        return df[(df.timestamp_utc >= start) & (df.timestamp_utc < end)].reset_index(drop=True)

import pandas as pd

from niftypilot.market_data.session import IST

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample_candles(df: pd.DataFrame, src_minutes: int, dst: str) -> pd.DataFrame:
    """Aggregate intraday candles to '15m' or '1d'. Incomplete buckets are dropped, not filled.

    '1d' needs a full session (75 x 5m); a day with any gap yields no daily candle.
    """
    ist = df.set_index(df.timestamp_utc.dt.tz_convert(IST)).drop(columns="timestamp_utc")
    if dst == "1d":
        g = ist.groupby(ist.index.normalize())
        need = 375 // src_minutes
    else:
        minutes = int(dst.rstrip("m"))
        g = ist.groupby(ist.index.floor(f"{minutes}min"))  # 09:15 lies on the 15-min grid
        need = minutes // src_minutes
    out = g.agg(AGG)[g.size() == need]
    out.index = out.index.tz_convert("UTC")
    return out.rename_axis("timestamp_utc").reset_index()

"""Deterministic Nifty 50 regime classifier.

Default thresholds are starting placeholders, not validated values; the research engine (M5)
must test them. The index has no volume, so rules use price only.
"""

from enum import StrEnum

import pandas as pd
from pydantic import BaseModel


class Regime(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNCERTAIN = "UNCERTAIN"


class RegimeParams(BaseModel):
    trend_pct: float = 0.4  # |change from prev close| needed to call a direction
    high_vol_range_pct: float = 2.0  # intraday range / prev close


def classify_regime(
    index_today: pd.DataFrame, prev_close: float | None, p: RegimeParams = RegimeParams()
) -> Regime:
    """`index_today`: completed index candles of the current session only."""
    if index_today.empty or not prev_close:
        return Regime.UNCERTAIN
    rng = (index_today.high.max() - index_today.low.min()) / prev_close * 100
    if rng >= p.high_vol_range_pct:
        return Regime.HIGH_VOLATILITY
    last, day_open = index_today.close.iloc[-1], index_today.open.iloc[0]
    change = (last - prev_close) / prev_close * 100
    if change >= p.trend_pct and last > day_open:
        return Regime.BULLISH
    if change <= -p.trend_pct and last < day_open:
        return Regime.BEARISH
    if abs(change) < p.trend_pct:
        return Regime.SIDEWAYS
    return Regime.UNCERTAIN  # gap one way, intraday move the other

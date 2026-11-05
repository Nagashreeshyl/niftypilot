"""SYNTHETIC candle data for tests and demos. Never real market data; always stored with
data_kind='synthetic' and fake symbols (SYNTH-*).
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from niftypilot.market_data.session import session_slots

BASE_VOLUME = 10_000


def history_sessions(
    last_day: date, n: int = 10, price: float = 100.0, seed: int = 7, volume: int = BASE_VOLUME
) -> pd.DataFrame:
    """`n` weekday sessions before `last_day`: flat-ish random walk, deterministic volume."""
    rng = np.random.default_rng(seed)
    days, d = [], last_day
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    rows = []
    for day in sorted(days):
        px = price
        for ts in session_slots(day):
            o = px
            c = round(o * (1 + rng.normal(0, 0.001)), 2)
            rows.append((ts, o, max(o, c) + 0.05, min(o, c) - 0.05, c, volume))
            px = c
    return frame(rows)


def session(day: date, bars: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """Build a session from (open, high, low, close, volume_multiple) bars from 09:15."""
    slots = session_slots(day)
    return frame(
        [(slots[i], o, h, low, c, int(m * BASE_VOLUME)) for i, (o, h, low, c, m) in enumerate(bars)]
    )


def frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["timestamp_utc", "open", "high", "low", "close", "volume"])
    return df.astype({"timestamp_utc": "datetime64[ns, UTC]"})


# Opening range for all scenarios: 100.00 - 101.00
OR_BARS = [
    (100.2, 100.8, 100.0, 100.6, 1),
    (100.6, 101.0, 100.4, 100.9, 1),
    (100.9, 100.95, 100.5, 100.7, 1),
]

SCENARIOS: dict[str, list] = {
    # breakout 09:30 (rvol 2), retest 09:35 low 101.05, confirmation 09:40 close 101.6
    "buy_setup": OR_BARS
    + [
        (100.8, 101.5, 100.8, 101.4, 2),
        (101.4, 101.45, 101.05, 101.2, 1),
        (101.2, 101.7, 101.15, 101.6, 1.5),
    ],
    "no_breakout": OR_BARS + [(100.7, 100.9, 100.3, 100.5, 1)] * 4,
    "low_volume_breakout": OR_BARS + [(100.8, 101.5, 100.8, 101.4, 0.8)],
    "invalidated": OR_BARS + [(100.8, 101.5, 100.8, 101.4, 2), (101.4, 101.4, 100.4, 100.5, 1)],
    "retest_pending": OR_BARS + [(100.8, 101.5, 100.8, 101.4, 2), (101.4, 101.9, 101.4, 101.8, 1)],
    "bearish_breakdown": OR_BARS + [(100.6, 100.6, 99.5, 99.7, 2)],
}


def random_market(
    symbols: dict[str, float],
    start: date,
    n_days: int,
    seed: int = 42,
    index_symbol: str | None = "NIFTY 50",
) -> dict[str, pd.DataFrame]:
    """SYNTHETIC multi-day 5m candles: correlated random walks with a U-shaped volume profile.

    {symbol: start_price}. Deterministic for a given seed. Contains no market structure, so a
    backtest on it should show no edge; it exists to exercise the engine, not to evaluate it.
    """
    rng = np.random.default_rng(seed)
    days, d = [], start
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    names = list(symbols) + ([index_symbol] if index_symbol else [])
    px = {s: symbols.get(s, 22000.0) for s in names}
    shape = 1 + 1.5 * np.linspace(-1, 1, 75) ** 2  # U-shaped intraday volume
    rows: dict[str, list] = {s: [] for s in names}
    for day in days:
        market = rng.normal(0, 0.0012, 75)
        for s in names:
            idio = rng.normal(0, 0.0015, 75)
            gap = rng.normal(0, 0.004)
            p = px[s] * (1 + gap)
            base_vol = 0 if s == index_symbol else int(rng.lognormal(10, 0.4))
            for i, ts in enumerate(session_slots(day)):
                o = p
                c = o * (1 + market[i] + (0 if s == index_symbol else idio[i]))
                wick = abs(rng.normal(0, 0.0008)) * o
                h, low = max(o, c) + wick, min(o, c) - abs(rng.normal(0, 0.0008)) * o
                v = int(base_vol * shape[i] * rng.uniform(0.5, 1.8))
                rows[s].append((ts, round(o, 2), round(h, 2), round(low, 2), round(c, 2), v))
                p = c
            px[s] = p
    return {s: frame(r) for s, r in rows.items()}

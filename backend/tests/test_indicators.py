import math

import pandas as pd
import pytest

from niftypilot import synthetic
from niftypilot.strategy.indicators import opening_range, relative_volume, session_vwap
from niftypilot.strategy.regime import Regime, classify_regime
from tests.conftest import DAY


def bars(*rows):
    return synthetic.session(DAY, list(rows))


def test_vwap_hand_calculated():
    df = bars((10, 12, 8, 11, 1), (11, 14, 10, 13, 3))
    tp1, tp2 = (12 + 8 + 11) / 3, (14 + 10 + 13) / 3
    v = session_vwap(df)
    assert v[0] == pytest.approx(tp1)
    assert v[1] == pytest.approx((tp1 * 1 + tp2 * 3) / 4)


def test_vwap_resets_each_session_and_handles_zero_volume():
    d1 = bars((10, 12, 8, 11, 1))
    d2 = synthetic.session(DAY + pd.Timedelta(days=1), [(50, 50, 50, 50, 0), (50, 51, 49, 50, 1)])
    v = session_vwap(pd.concat([d1, d2], ignore_index=True))
    assert v[0] == pytest.approx(31 / 3)
    assert math.isnan(v[1])  # no volume yet today: undefined, not carried over
    assert v[2] == pytest.approx(50)


def test_opening_range_values_and_missing_candle():
    df = synthetic.session(DAY, synthetic.SCENARIOS["buy_setup"])
    r = opening_range(df)
    assert (r.high, r.low, r.midpoint, r.width) == (101.0, 100.0, 100.5, 1.0)
    assert r.width_pct == pytest.approx(1.0)
    assert opening_range(df.drop(index=1)) is None
    assert opening_range(df.head(2)) is None  # range incomplete


def test_relative_volume_uses_same_slot_history(history):
    today = bars((1, 1, 1, 1, 2), (1, 1, 1, 1, 0.5))
    rv = relative_volume(today, history)
    assert rv.tolist() == [pytest.approx(2.0), pytest.approx(0.5)]


def test_relative_volume_nan_without_enough_history(history):
    today = bars((1, 1, 1, 1, 2))
    few = history[history.timestamp_utc.dt.date >= history.timestamp_utc.dt.date.max()]
    assert math.isnan(relative_volume(today, few, min_sessions=5)[0])
    # history from today/future must be ignored
    same_day = synthetic.history_sessions(DAY + pd.Timedelta(days=10))
    future_only = same_day[same_day.timestamp_utc.dt.date >= DAY]
    assert math.isnan(relative_volume(today, future_only)[0])


@pytest.mark.parametrize(
    "last,lo,hi,expected",
    [
        (100.6, 99.9, 100.7, Regime.BULLISH),
        (99.4, 99.3, 100.1, Regime.BEARISH),
        (100.1, 99.9, 100.3, Regime.SIDEWAYS),
        (100.5, 98.0, 100.6, Regime.HIGH_VOLATILITY),
    ],
)
def test_regime_rules(last, lo, hi, expected):
    idx = bars((100.0, hi, lo, last, 0))
    assert classify_regime(idx, prev_close=100.0) == expected


def test_regime_uncertain_without_data():
    assert classify_regime(bars(), 100.0) == Regime.UNCERTAIN
    assert classify_regime(bars((1, 1, 1, 1, 0)), None) == Regime.UNCERTAIN

from datetime import time, timedelta

import pandas as pd
import pytest

from niftypilot import synthetic
from niftypilot.market_data.session import IST
from niftypilot.strategy.orb_vwap import OrbParams, Outcome, evaluate
from niftypilot.strategy.regime import Regime
from tests.conftest import DAY

S = synthetic.SCENARIOS
OR = synthetic.OR_BARS


def day(bars):
    return synthetic.session(DAY, bars)


def close_of(df, i):
    return (df.timestamp_utc.iloc[i] + timedelta(minutes=5)).to_pydatetime()


def run(bars, history, regime=Regime.SIDEWAYS, params=OrbParams(), at=None):
    df = day(bars)
    return evaluate(df, history, close_of(df, len(df) - 1 if at is None else at), regime, params)


def failed(res):
    return [r.rule for r in res.rules if not r.passed]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("buy_setup", Outcome.BUY_SETUP),
        ("no_breakout", Outcome.NO_TRADE),
        ("low_volume_breakout", Outcome.BREAKOUT_REJECTED),
        ("invalidated", Outcome.SETUP_INVALIDATED),
        ("retest_pending", Outcome.RETEST_PENDING),
        ("bearish_breakdown", Outcome.NO_TRADE),
    ],
)
def test_scenarios_have_known_outcomes(name, expected, history):
    assert run(S[name], history).outcome == expected


def test_buy_setup_levels_and_explanation(history):
    r = run(S["buy_setup"], history)
    assert (r.entry, r.stop_loss, r.target) == (101.6, 100.95, 102.9)  # tick-rounded, 2R
    assert r.signal_time == close_of(day(S["buy_setup"]), 5)
    assert (r.opening_range.high, r.opening_range.low) == (101.0, 100.0)
    names = [x.rule for x in r.rules]
    for rule in [
        "data_complete",
        "breakout_close_above_orh",
        "breakout_above_vwap",
        "relative_volume",
        "regime_allows_long",
        "retest_near_orh",
        "buyer_confirmation",
        "stop_distance_pct",
    ]:
        assert rule in names
    assert not failed(r)
    assert "Pending risk-engine approval" in r.explanation  # strategy alone never approves


def test_bearish_breakdown_is_research_only(history):
    r = run(S["bearish_breakdown"], history)
    assert r.outcome == Outcome.NO_TRADE and r.research_flags == ["BEARISH_BREAKDOWN"]
    assert r.entry is None


def test_no_signal_before_opening_range_complete(history):
    df = day(S["buy_setup"])
    r = evaluate(df, history, close_of(df, 1), Regime.SIDEWAYS)  # 09:25 IST
    assert r.outcome == Outcome.NO_TRADE and "not yet established" in r.explanation


def test_no_lookahead_future_candles_do_not_change_past_results(history):
    full = day(S["buy_setup"] + [(101.6, 101.6, 90, 90.5, 9)] * 20)  # wild future crash
    for i in range(len(S["buy_setup"])):
        as_of = close_of(full, i)
        truncated = full.iloc[: i + 1]
        a = evaluate(full, history, as_of, Regime.SIDEWAYS)
        b = evaluate(truncated, history, as_of, Regime.SIDEWAYS)
        assert (a.outcome, a.entry, a.explanation) == (b.outcome, b.entry, b.explanation)


def test_incomplete_candle_never_used(history):
    df = day(S["buy_setup"])
    mid_candle = close_of(df, 5) - timedelta(minutes=1)  # confirmation candle not closed
    assert evaluate(df, history, mid_candle, Regime.SIDEWAYS).outcome == Outcome.RETEST_PENDING


def test_missing_candle_is_data_invalid(history):
    df = day(S["buy_setup"]).drop(index=4)
    r = evaluate(df, history, close_of(day(S["buy_setup"]), 5), Regime.SIDEWAYS)
    assert r.outcome == Outcome.DATA_INVALID and "data_complete" in failed(r)


def test_invalid_ohlc_is_data_invalid(history):
    bad = list(S["buy_setup"])
    bad[4] = (101.4, 101.0, 101.05, 101.2, 1)  # high < low
    assert run(bad, history).outcome == Outcome.DATA_INVALID


def test_insufficient_rvol_history_is_data_invalid(history):
    few = history[history.timestamp_utc.dt.date == history.timestamp_utc.dt.date.max()]
    r = run(S["buy_setup"], few)
    assert r.outcome == Outcome.DATA_INVALID and "relative volume" in r.explanation


def test_breakout_below_vwap_rejected(history):
    # 09:30 candle: huge volume with a high wick drags VWAP above the later breakout close
    bars = OR + [(100.9, 105.0, 100.8, 100.95, 50), (100.95, 101.5, 100.9, 101.4, 2)]
    r = run(bars, history)
    assert r.outcome == Outcome.BREAKOUT_REJECTED and failed(r) == ["breakout_above_vwap"]


@pytest.mark.parametrize("regime", [Regime.BEARISH])
def test_blocked_regime_rejects_breakout(regime, history):
    r = run(S["buy_setup"], history, regime=regime)
    assert r.outcome == Outcome.BREAKOUT_REJECTED and failed(r) == ["regime_allows_long"]


def test_retest_timeout_invalidates(history):
    bars = S["retest_pending"] + [(101.8, 102.0, 101.6, 101.9, 1)] * 6
    r = run(bars, history)
    assert r.outcome == Outcome.SETUP_INVALIDATED and "No retest" in r.explanation


def test_confirmation_timeout_invalidates(history):
    no_confirm = [(101.2, 101.3, 101.1, 101.15, 1)] * 3  # neither bullish breakout nor deeper
    r = run(S["buy_setup"][:5] + no_confirm, history)
    assert r.outcome == Outcome.SETUP_INVALIDATED and "confirmation" in r.explanation


def test_entry_cutoff(history):
    p = OrbParams(entry_cutoff=time(9, 35))
    r = run(S["buy_setup"], history, params=p)
    assert r.outcome == Outcome.SETUP_INVALIDATED and "entry_window" in failed(r)
    r = run(S["no_breakout"], history, params=OrbParams(entry_cutoff=time(9, 30)))
    assert r.outcome == Outcome.NO_TRADE and "cutoff" in r.explanation


def test_stop_too_wide_is_no_trade(history):
    r = run(S["buy_setup"], history, params=OrbParams(max_stop_pct=0.5))
    assert r.outcome == Outcome.NO_TRADE and failed(r) == ["stop_distance_pct"]
    assert r.entry == 101.6  # levels still reported for the explanation


def test_deterministic(history):
    a, b = run(S["buy_setup"], history), run(S["buy_setup"], history)
    assert a.rules_json() == b.rules_json() and a.explanation == b.explanation


def test_params_serialise_roundtrip():
    p = OrbParams()
    assert OrbParams.model_validate(p.model_dump(mode="json")) == p
    with pytest.raises(ValueError):
        OrbParams(or_minutes=0)


def test_works_with_shuffled_input(history):
    df = day(S["buy_setup"]).sample(frac=1, random_state=1)
    assert (
        evaluate(df, history, close_of(day(S["buy_setup"]), 5), Regime.SIDEWAYS).outcome
        == Outcome.BUY_SETUP
    )
    assert isinstance(df, pd.DataFrame)


def test_rvol_toggle_off_ignores_volume_and_history(history):
    p = OrbParams(require_rvol=False)
    assert run(S["low_volume_breakout"], history, params=p).outcome == Outcome.RETEST_PENDING
    r = run(S["buy_setup"], history.iloc[0:0], params=p)  # no history at all
    assert r.outcome == Outcome.BUY_SETUP
    assert "relative_volume" not in [x.rule for x in r.rules]


def test_30_minute_opening_range(history):
    # 6 OR candles; range 100.0-101.0 is set by the first three, then three inside bars
    inside = [(100.7, 100.9, 100.4, 100.6, 1)] * 3
    bars = OR + inside + S["buy_setup"][3:]
    p = OrbParams(or_minutes=30, require_rvol=False)
    r = run(bars, history, params=p)
    assert r.opening_range.end_utc.astimezone(IST).time() == time(9, 45)
    assert r.outcome == Outcome.BUY_SETUP
    early = run(bars, history, params=p, at=4)  # 09:40 close: range not complete
    assert early.outcome == Outcome.NO_TRADE and "not yet established" in early.explanation


def test_regime_is_taken_at_breakout_time_not_later(history):
    df = day(S["buy_setup"])
    breakout_close = close_of(df, 3)
    seen = []

    def regime_at(t):
        seen.append(t)
        return Regime.SIDEWAYS if t <= breakout_close else Regime.BEARISH

    r = evaluate(df, history, close_of(df, 5), regime_at)
    assert r.outcome == Outcome.BUY_SETUP and seen == [breakout_close]

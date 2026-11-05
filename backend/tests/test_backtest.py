"""Simulator tests on hand-built SYNTHETIC days with hand-computed expected fills."""

from datetime import timedelta
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from niftypilot import synthetic
from niftypilot.backtest.engine import BacktestConfig, day_signal, run_backtest
from niftypilot.backtest.execution import ExecutionModel, check_exit, entry_fill, entry_limit
from niftypilot.backtest.market import InstrumentInfo, MarketData
from niftypilot.market_data.session import IST, session_slots
from niftypilot.risk.engine import RiskConfig
from niftypilot.strategy.orb_vwap import OrbParams, Outcome, evaluate
from niftypilot.strategy.regime import Regime
from tests.conftest import DAY

EX = ExecutionModel()
T = D("0.05")
# Wider-stop setup: deep retest wick to 100.40 -> stop 100.30 (1.28%), entry 101.6,
# target 104.20 (2R). The M2 fixture's 0.64% stop is rejected at every capital: costs and
# slippage leave net reward:risk < 1 (see test_narrow_stop_setup_is_uneconomical).
SETUP = synthetic.OR_BARS + [
    (100.8, 101.5, 100.8, 101.4, 2),  # 09:30 breakout, rvol 2
    (101.4, 101.45, 100.40, 101.2, 1),  # 09:35 retest (low within tolerance), deep wick
    (101.2, 101.7, 101.15, 101.6, 1.5),  # 09:40 confirmation
]
UP = [(101.6, 102.0, 101.5, 101.9, 1), (101.9, 104.4, 101.8, 104.0, 1)]  # through 104.25
DOWN = [(101.6, 101.7, 101.4, 101.5, 1), (101.5, 101.6, 100.0, 100.2, 1)]  # through 100.30
NO_REGIME = OrbParams(blocked_regimes=[])
BIG = BacktestConfig(strategy=NO_REGIME, risk=RiskConfig(capital=D(100000)))


def pad(bars):
    c = bars[-1][3]
    return bars + [(c, c + 0.05, c - 0.05, c, 1)] * (75 - len(bars))


def market(days: dict[str, list] | None = None, holidays=(), membership=None, bench=None):
    days = days or {"AAA": SETUP + UP}
    candles = {
        s: pd.concat(
            [synthetic.history_sessions(DAY), synthetic.session(DAY, pad(b))], ignore_index=True
        )
        for s, b in days.items()
    }
    info = {s: InstrumentInfo(s, "CASH", 0.05) for s in candles}
    if bench is not None:
        candles["NIFTY 50"] = pd.concat(
            [synthetic.history_sessions(DAY, volume=0), synthetic.session(DAY, pad(bench))],
            ignore_index=True,
        )
        info["NIFTY 50"] = InstrumentInfo("NIFTY 50", "INDEX", 0.05)
    return MarketData(
        {"data_kind": "synthetic", "name": "test"},
        candles,
        info,
        set(holidays),
        membership,
        "NIFTY 50" if bench is not None else None,
    )


def run(m=None, cfg=BIG, start=DAY, end=DAY):
    return run_backtest(m or market(), cfg, start, end)


def slot(i):
    return session_slots(DAY)[i]


# ---------- execution model ----------


def test_entry_fill_price_rounds_up_with_costs():
    f = entry_fill(EX, D("101.6"), 10000, D("101.7"), D("100.95"), 100, T)
    assert (f.status, f.price, f.qty) == ("filled", D("101.70"), 100)  # 101.6*1.00075=101.676


def test_entry_limit_is_fraction_of_risk():
    # 101.6 + 0.1 x 0.65 = 101.665 -> rounded up to tick
    assert entry_limit(EX, D("101.6"), D("100.95"), T) == D("101.70")


def test_entry_rejected_when_open_gaps_through_stop_or_too_far_up():
    assert entry_fill(EX, D("100.9"), 10000, D("101.7"), D("100.95"), 1, T).status == "rejected"
    assert entry_fill(EX, D("101.75"), 10000, D("101.7"), D("100.95"), 1, T).status == "rejected"


def test_partial_fill_capped_by_participation():
    f = entry_fill(EX, D("101.6"), 500, D("101.6"), D("100.95"), 100, T)
    assert (f.status, f.qty) == ("partial", 50)
    assert entry_fill(EX, D("101.6"), 5, D("101.6"), D("100.95"), 1, T).status == "rejected"


def test_stop_gap_fills_at_open_not_stop():
    f = check_exit(EX, D("99"), D("99.5"), D("98.5"), D("100"), D("103"), T, entry_candle=False)
    assert (f.reason, f.price) == ("stop_gap", D("98.90"))  # 99*0.99925=98.926 -> floor


def test_target_needs_trade_through():
    touch = check_exit(EX, D("101"), D("102"), D("100.5"), D("100"), D("102"), T, False)
    assert touch is None
    hit = check_exit(EX, D("101"), D("102.05"), D("100.5"), D("100"), D("102"), T, False)
    assert (hit.reason, hit.price) == ("target", D("102"))


def test_target_gap_does_not_assume_price_improvement():
    f = check_exit(EX, D("105"), D("106"), D("104"), D("100"), D("102"), T, False)
    assert (f.reason, f.price) == ("target_gap", D("102"))


def test_ambiguous_bar_assumes_stop_first():
    f = check_exit(EX, D("101"), D("103"), D("99"), D("100"), D("102"), T, False)
    assert f.reason == "stop" and f.ambiguous and f.price == D("99.90")


def test_entry_candle_ignores_open_gap_logic():
    f = check_exit(EX, D("99"), D("99.5"), D("98.9"), D("100"), D("103"), T, entry_candle=True)
    assert f.reason == "stop"  # we bought at this open; only the rest of the bar counts


# ---------- event loop ----------


def test_target_trade_hand_checked_and_cash_reconciles():
    r = run()
    (t,) = r.trades
    assert t.signal_time == slot(6) and t.entry_time == slot(6)  # next candle's open
    assert (t.entry_price, t.exit_price, t.exit_reason) == (D("101.70"), D("104.20"), "target")
    assert (t.stop, t.target) == (D("100.30"), D("104.20"))
    assert t.exit_time == slot(7) + timedelta(minutes=5)
    assert t.qty <= t.requested_qty and t.qty > 0
    assert r.daily_equity[DAY] == D(100000) + t.net_pnl
    assert t.net_pnl == (D("104.20") - D("101.70")) * t.qty - t.charges
    # sized against the 101.75 limit (101.6 + 0.1 x 1.30, up to tick): loss at stop in budget
    assert (t.entry_price - D("100.20")) * t.qty + t.charges <= D("500")


def test_stop_trade():
    (t,) = run(market({"AAA": SETUP + DOWN})).trades
    assert (t.exit_reason, t.exit_price) == ("stop", D("100.20"))  # 100.30*0.99925 floor
    assert -t.net_pnl <= D("500")  # risk budget: 0.5% of Rs 1,00,000
    assert t.net_pnl < 0


def test_square_off_before_groww_auto_squareoff():
    flat = [(101.6, 101.8, 101.4, 101.6, 1)]
    (t,) = run(market({"AAA": SETUP + flat})).trades
    assert t.exit_reason == "square_off" and t.exit_time.astimezone(IST).hour == 15
    assert t.exit_time.astimezone(IST).minute == 15


def test_small_capital_is_risk_rejected_not_upsized():
    r = run(cfg=BacktestConfig(strategy=NO_REGIME))  # Rs 5,000
    assert r.trades == []
    assert [o["status"] for o in r.orders] == ["risk_rejected"]
    assert "net_reward_risk" in r.orders[0]["reason"]


def test_unaffordable_stock_rejected():
    pricey = [tuple(x * 30 if i < 4 else x for i, x in enumerate(b)) for b in SETUP + UP]
    r = run(
        market({"AAA": pricey}),
        BacktestConfig(strategy=NO_REGIME, risk=RiskConfig(capital=D(1000))),
    )
    assert r.trades == [] and r.signals[0]["outcome"] == "BUY_SETUP"  # a Rs ~3,050 share
    assert r.orders[0]["reason"] == "min_one_share_within_budget"


def test_one_position_at_a_time_and_no_duplicate_entries():
    r = run(market({"AAA": SETUP + UP, "BBB": SETUP + UP}))
    assert len(r.trades) == 1
    assert {o["reason"] for o in r.orders if o["status"] == "risk_rejected"} == {
        "max_open_positions"
    }


def test_missing_entry_candle_cancels_order():
    m = market()
    df = m.candles["AAA"]
    m = MarketData(m.dataset, {"AAA": df[df.timestamp_utc != slot(6)]}, m.info)
    r = run(m)
    assert r.trades == [] and r.orders[0]["status"] == "cancelled"


def test_missing_square_off_candle_closes_at_last_data_and_flags_it():
    flat = [(101.6, 101.8, 101.4, 101.6, 1)]
    m = market({"AAA": SETUP + flat})
    df = m.candles["AAA"]
    m = MarketData(m.dataset, {"AAA": df[df.timestamp_utc < slot(70)]}, m.info)
    (t,) = run(m).trades
    assert t.exit_reason == "data_gap_close"


def test_holiday_is_skipped_even_with_data():
    r = run(market(holidays=[DAY]))
    assert r.trades == [] and r.days == []


def test_point_in_time_membership_excludes_non_members():
    r = run(market(membership=lambda d: set()))
    assert r.trades == [] and r.days[0]["skipped"]


def test_regime_filter_requires_benchmark():
    with pytest.raises(ValueError, match="benchmark"):
        run(cfg=BacktestConfig(risk=RiskConfig(capital=D(100000))))


def test_bearish_benchmark_blocks_trade_with_regime_filter():
    falling = [
        (22000 - 40 * i, 22000 - 40 * i + 5, 22000 - 40 * (i + 1), 22000 - 40 * (i + 1), 0)
        for i in range(10)
    ]
    cfg = BacktestConfig(risk=RiskConfig(capital=D(100000)))  # default blocks BEARISH
    m = market(bench=falling)
    m.candles["NIFTY 50"].loc[
        m.candles["NIFTY 50"].timestamp_utc < slot(0), ["open", "high", "low", "close"]
    ] = 22000.0
    m = MarketData(m.dataset, m.candles, m.info, benchmark="NIFTY 50")
    r = run(m, cfg)
    assert r.trades == []
    assert any("regime_allows_long" in s["explanation"] for s in r.signals)


def test_future_data_cannot_change_past_trades():
    base = run()
    m = market()
    df = m.candles["AAA"]
    after = df.timestamp_utc >= slot(8)
    df.loc[after, ["open", "high", "low", "close"]] = [5.0, 500.0, 1.0, 250.0]
    r = run(MarketData(m.dataset, {"AAA": df}, m.info))
    assert r.trades[0].to_dict() == base.trades[0].to_dict()


def test_chronological_and_reproducible():
    a, b = run(), run()
    assert [t.to_dict() for t in a.trades] == [t.to_dict() for t in b.trades]
    assert a.equity == b.equity and a.orders == b.orders
    times = [t for t, _, _ in a.equity]
    assert times == sorted(times) and len(set(times)) == len(times)
    t = a.trades[0]
    assert t.signal_time <= t.entry_time < t.exit_time


@pytest.mark.parametrize("seed", range(6))
def test_day_signal_matches_brute_force_candle_by_candle(seed, history):
    rng = np.random.default_rng(seed)
    bars, px = list(synthetic.OR_BARS), 100.7
    for _ in range(72):
        o = px
        c = round(o * (1 + rng.normal(0.0004, 0.004)), 2)
        bars.append((o, max(o, c) + 0.1, min(o, c) - 0.1, c, float(rng.uniform(0.5, 3))))
        px = c
    df = synthetic.session(DAY, bars)
    if seed % 2:
        df = df.drop(index=int(rng.integers(10, 70)))  # a gap somewhere
    p = OrbParams(blocked_regimes=[])
    fast = day_signal(df, history, DAY, p, lambda t: Regime.SIDEWAYS, 0.05)
    brute = None
    for s in session_slots(DAY):
        r = evaluate(df, history, s + timedelta(minutes=5), Regime.SIDEWAYS, p)
        if r.outcome not in (Outcome.NO_TRADE, Outcome.RETEST_PENDING) or r.signal_time:
            brute = r
            break
    brute = brute or r
    assert (fast.outcome, fast.signal_time, fast.entry, fast.stop_loss) == (
        brute.outcome,
        brute.signal_time,
        brute.entry,
        brute.stop_loss,
    )


def test_narrow_stop_viability_depends_on_capital():
    """0.64% stop: rejected up to Rs 1L; at Rs 10L the Rs 20 brokerage cap makes costs small
    enough relative to position size to pass the unchanged net reward:risk >= 1 rule."""
    narrow = synthetic.SCENARIOS["buy_setup"] + UP
    for cap, n in ((5000, 0), (100000, 0), (1000000, 1)):
        cfg = BacktestConfig(strategy=NO_REGIME, risk=RiskConfig(capital=D(cap)))
        r = run(market({"AAA": narrow}), cfg)
        assert len(r.trades) == n
        if not n:
            assert r.orders[0]["reason"] == "net_reward_risk"


def test_fee_status_reported():
    r = run()
    assert "brokerage_pct" in r.fees_unverified  # historical date: not authoritative
    assert r.trades[0].fees_unverified

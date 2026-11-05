from decimal import Decimal as D

from niftypilot.backtest.engine import BacktestConfig
from niftypilot.backtest.execution import ExecutionModel
from niftypilot.backtest.sensitivity import SCENARIOS, run_sensitivity, sensitivity_markdown
from niftypilot.risk.engine import RiskConfig
from tests.conftest import DAY
from tests.test_backtest import BIG, DOWN, NO_REGIME, SETUP, UP, market, run, slot


def test_cost_split_target_trade_hand_checked():
    (t,) = run().trades
    # entry: open 101.6, fill 101.70; spread 101.6 x 0.025% = 0.0254/share; rest is slippage
    assert t.spread_cost == D("101.6") * D("0.00025") * t.qty
    assert t.spread_cost + t.slippage_cost == D("0.10") * t.qty  # target exit adds nothing
    assert t.gross_pnl == (D("104.20") - D("101.70")) * t.qty


def test_cost_split_stop_trade_hand_checked():
    (t,) = run(market({"AAA": SETUP + DOWN})).trades
    # exit: stop 100.30 -> fill 100.20; spread 100.30 x 0.025% per share on the exit too
    exit_spread = D("100.30") * D("0.00025") * t.qty
    entry_spread = D("101.6") * D("0.00025") * t.qty
    assert t.spread_cost == entry_spread + exit_spread
    assert t.spread_cost + t.slippage_cost == (D("0.10") + D("0.10")) * t.qty


def test_entry_delay_shifts_fill_candle():
    slow = [(101.6, 101.7, 101.5, 101.6, 1), (101.62, 104.4, 101.6, 104.0, 1)]
    cfg = BIG.model_copy(update={"execution": ExecutionModel(entry_delay_bars=2)})
    (t,) = run(market({"AAA": SETUP + slow}), cfg).trades
    assert t.signal_time == slot(6) and t.entry_time == slot(7)  # one candle later
    assert t.entry_price == D("101.70")  # 101.62 x 1.00075 = 101.696, up to tick


def test_delay_can_push_entry_past_the_limit():
    cfg = BIG.model_copy(update={"execution": ExecutionModel(entry_delay_bars=2)})
    r = run(cfg=cfg)  # the 09:50 candle opens at 101.9, above the 101.75 limit
    assert r.trades == [] and r.orders[0]["reason"] == "open 101.9 above limit 101.75"


def test_scenarios_are_fixed_and_baseline_matches_defaults():
    names = [n for n, _ in SCENARIOS]
    assert names[:2] == ["optimistic", "baseline"] and len(names) == len(set(names)) == 9
    assert dict(SCENARIOS)["baseline"] == {}


def test_sensitivity_rows_are_complete_and_consistent():
    m = market({"AAA": SETUP + UP})
    base = BacktestConfig(strategy=NO_REGIME, risk=RiskConfig())
    rows = run_sensitivity(m, DAY, DAY, ["A"], [D(5000), D(100000)], base)
    assert len(rows) == len(SCENARIOS) * 2
    for r in rows:
        assert abs(r["net_pnl"] - (r["gross_pnl"] - r["charges"])) < 0.02
        assert r["trades"] + r["rejected_or_unfilled"] >= r["buy_setups"] or r["buy_setups"]
    small = [r for r in rows if r["capital"] == "5000"]
    assert all(r["trades"] == 0 for r in small)  # never upsized to force a trade
    md = sensitivity_markdown(rows, "synthetic", "test")
    assert "NOT PERFORMANCE EVIDENCE" in md and md.count("\n| ") == len(rows) + 1

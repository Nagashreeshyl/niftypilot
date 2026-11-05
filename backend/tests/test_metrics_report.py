import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from niftypilot.backtest.engine import BacktestConfig, BacktestResult, Trade
from niftypilot.backtest.metrics import bootstrap, compute_metrics, max_consecutive_losses
from niftypilot.backtest.report import build_report, record_run, report_markdown, write_report
from niftypilot.risk.engine import RiskConfig
from tests.test_backtest import BIG, market, run

T0 = datetime(2026, 1, 15, 4, 15, tzinfo=UTC)


def trade(i, entry, exit_, qty=10, charges=D("0")):
    t = Trade(
        "AAA",
        date(2026, 1, 15),
        T0,
        T0 + timedelta(minutes=i),
        D(entry),
        qty,
        qty,
        D(entry) - 1,
        D(entry) + 2,
        T0 + timedelta(minutes=i + 30),
        D(exit_),
        "x",
        regime="SIDEWAYS",
    )
    t.exit_charges = charges
    return t


def result(trades, equity=(), daily=None):
    cfg = BacktestConfig(risk=RiskConfig(capital=D(1000)))
    return BacktestResult(
        cfg,
        date(2026, 1, 1),
        date(2026, 1, 31),
        trades,
        list(equity),
        daily or {},
        [],
        [],
        [],
        set(),
        "synthetic",
    )


def test_core_metrics_hand_checked():
    ts = [trade(0, "100", "110"), trade(1, "100", "95"), trade(2, "100", "97")]
    m = compute_metrics(result(ts))  # nets +100, -50, -30
    assert (m["trades"], m["net_pnl"], m["win_rate"]) == (3, 20.0, 0.3333)
    assert m["profit_factor"] == 1.25 and m["expectancy"] == pytest.approx(6.67)
    assert (m["avg_win"], m["avg_loss"]) == (100.0, -40.0)
    assert m["max_consecutive_losses"] == 2 and m["avg_holding_minutes"] == 30.0
    assert m["expectancy_r"] == pytest.approx((10 - 5 - 3) / 3, abs=1e-3)  # Rs 10 risk/trade
    assert "not statistically meaningful" in m["warnings"][0]


def test_charges_separate_gross_and_net():
    m = compute_metrics(result([trade(0, "100", "110", charges=D("12.5"))]))
    assert (m["gross_pnl"], m["total_charges"], m["net_pnl"]) == (100.0, 12.5, 87.5)
    assert m["profit_factor"] is None  # no losses: undefined, not infinite


def test_drawdown_from_intraday_mtm():
    eq = [(T0, D(1100), D(0)), (T0, D(900), D(500)), (T0, D(1000), D(0))]
    m = compute_metrics(result([], eq))
    assert m["max_drawdown"] == 200.0 and m["max_drawdown_pct_of_peak"] == pytest.approx(18.182)
    assert m["exposure_time_pct"] == pytest.approx(33.33)


def test_sharpe_uses_every_trading_day():
    daily = {date(2026, 1, d): D(v) for d, v in [(5, 1010), (6, 1010), (7, 1000), (8, 1020)]}
    m = compute_metrics(result([], daily=daily))
    r = [0.01, 0.0, -0.01, 0.02]  # daily P&L / capital, incl. the flat day
    import statistics

    expected = statistics.mean(r) / statistics.stdev(r) * 252**0.5
    assert m["sharpe_annualised"] == pytest.approx(expected, abs=1e-3)


def test_bootstrap_is_seeded_and_sensible():
    a, b = bootstrap([1, 2, 3, 4, 5]), bootstrap([1, 2, 3, 4, 5])
    assert a == b and a["ci95"][0] < 3 < a["ci95"][1]
    assert bootstrap([5.0] * 3 + [6.0] * 3)["p_value_mean_le_0"] == 0.0
    assert bootstrap([1.0])["ci95"] is None


def test_consecutive_losses_in_exit_order():
    ts = [
        trade(0, "100", "99"),
        trade(5, "100", "101"),
        trade(10, "100", "98"),
        trade(15, "100", "97"),
    ]
    assert max_consecutive_losses(ts) == 2


def test_report_is_complete_reproducible_and_flagged(tmp_path):
    meta = {
        "id": 1,
        "name": "t",
        "fingerprint": "abc",
        "source": "tests",
        "price_adjustment": "unadjusted",
    }
    a, b = build_report(meta, run()), build_report(meta, run())
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    for key in (
        "strategy_version",
        "parameters",
        "dataset",
        "period",
        "capital",
        "fee_schedule",
        "execution_assumptions",
        "trades",
        "metrics",
        "data_quality",
        "validation_methodology",
        "limitations",
    ):
        assert key in a
    assert a["title"].startswith("SYNTHETIC") and not a["fee_schedule"]["authoritative"]
    md = report_markdown(a)
    assert "NOT PERFORMANCE EVIDENCE" in md and "not authoritative" in md
    assert md.count("| AAA |") >= len(a["trades"])  # every trade listed
    path = write_report(a, tmp_path)
    assert (path / "report.json").exists() and (path / "report.md").exists()


def test_run_key_changes_with_inputs():
    meta = {"id": 1, "fingerprint": "abc"}
    small = run(cfg=BacktestConfig(strategy=BIG.strategy))
    assert build_report(meta, run())["run_key"] != build_report(meta, small)["run_key"]
    assert (
        build_report({**meta, "fingerprint": "xyz"}, run())["run_key"]
        != build_report(meta, run())["run_key"]
    )


def test_record_run_is_idempotent(db, dataset, tmp_path):
    meta = {"id": dataset.id, "name": dataset.name, "fingerprint": "abc"}
    rep = build_report(meta, run(market()))
    a = record_run(db, rep, write_report(rep, tmp_path))
    b = record_run(db, rep, write_report(rep, tmp_path))
    assert a.id == b.id and a.total_trades == 1 and a.fees_authoritative is False

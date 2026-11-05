from datetime import date

import pytest

from niftypilot import synthetic
from niftypilot.backtest import research
from niftypilot.backtest.market import InstrumentInfo, MarketData
from niftypilot.backtest.research import (
    CAPITALS,
    VARIANTS,
    _windows,
    chronological_split,
    evaluate_oos,
    run_research,
    select_variant,
    trading_days,
)

START, END = date(2025, 1, 6), date(2025, 5, 30)


@pytest.fixture(scope="module")
def mkt():
    syms = {"AAA": 150.0, "BBB": 420.0, "CCC": 980.0, "DDD": 2600.0}
    candles = synthetic.random_market(syms, date(2024, 11, 1), 150, seed=3)
    info = {s: InstrumentInfo(s, "CASH", 0.05) for s in syms}
    info["NIFTY 50"] = InstrumentInfo("NIFTY 50", "INDEX", 0.05)
    return MarketData(
        {"id": None, "name": "synthetic-research", "fingerprint": "fp", "data_kind": "synthetic"},
        candles,
        info,
        benchmark="NIFTY 50",
    )


def test_split_is_chronological_and_disjoint(mkt):
    days = trading_days(mkt, START, END)
    s = chronological_split(days)
    assert s.development[0] == days[0] and s.out_of_sample[1] == days[-1]
    assert s.development[1] < s.validation[0] <= s.validation[1] < s.out_of_sample[0]
    with pytest.raises(ValueError):
        chronological_split(days[:5])


def test_walk_forward_windows_roll_forward_without_overlap():
    days = [date(2025, 1, 1 + i) for i in range(30)]
    w = _windows(days, train=10, test=5)
    assert len(w) == 4
    for a, b, c, d in w:
        assert a <= b < c <= d
    assert all(w[i + 1][2] > w[i][3] for i in range(len(w) - 1))  # test windows disjoint


def test_selection_rule():
    rows = {
        "A": {"trades": 40, "expectancy": 5, "expectancy_r": 0.1},
        "B": {"trades": 40, "expectancy": 9, "expectancy_r": 0.3},
        "C": {"trades": 10, "expectancy": 50, "expectancy_r": 2.0},  # too few trades
        "D": {"trades": 99, "expectancy": -1, "expectancy_r": -0.1},
    }
    assert select_variant(rows, 30) == "B"
    assert select_variant({"D": rows["D"]}, 30) is None


def test_research_never_touches_oos_and_oos_is_one_shot(db, dataset, mkt, monkeypatch):
    mkt.dataset["id"] = dataset.id
    calls = []
    real = research.run_backtest

    def spy(m, cfg, a, b):
        calls.append((a, b))
        return real(m, cfg, a, b)

    monkeypatch.setattr(research, "run_backtest", spy)
    honest = run_research(db, mkt, START, END, min_trades=3, wf_train=20, wf_test=10)
    # random-walk data has no edge: the selection rule should (and does) pick nothing
    assert all(r["selected"] is None for r in honest.metrics["per_capital"].values())
    # force a selection (test only) so the OOS path is exercised on a separate experiment
    monkeypatch.setattr(research, "select_variant", lambda rows, mt: "A")
    exp = run_research(
        db,
        mkt,
        START,
        END,
        base=research.BacktestConfig(broker="groww", scanner={"max_candidates": 4}),
        min_trades=3,
        wf_train=20,
        wf_test=10,
    )
    oos_start = date.fromisoformat(exp.out_of_sample_period["range"][0])
    assert calls and all(b < oos_start for _, b in calls)
    assert exp.n_trials == len(VARIANTS) * len(CAPITALS) == 25
    assert exp.parameter_set["data_kind"] == "synthetic"
    for row in exp.metrics["per_capital"].values():
        assert set(row["development"]) == set(VARIANTS)
        assert row["walk_forward"]["windows"]
        if row["selected_p_value"] is not None:
            assert row["selected_p_value_bonferroni"] == min(1.0, row["selected_p_value"] * 25)

    assert (
        run_research(db, mkt, START, END, min_trades=3, wf_train=20, wf_test=10).id == honest.id
    )  # same inputs -> same experiment
    calls.clear()
    # exp and honest share one OOS period on one dataset: evaluating exp views honest's
    # sealed OOS, so it is refused unless allowed, and then honest is marked contaminated
    with pytest.raises(PermissionError, match="sealed"):
        evaluate_oos(db, exp, mkt)
    out = evaluate_oos(db, exp, mkt, allow_contamination=True)
    assert out["contaminated"] is False and calls and all(a >= oos_start for a, _ in calls)
    db.refresh(honest)
    assert honest.contaminated is True
    with pytest.raises(PermissionError):
        evaluate_oos(db, exp, mkt, allow_contamination=True)  # already evaluated once
    assert evaluate_oos(db, exp, mkt, force=True, allow_contamination=True)["contaminated"]

    # capital feasibility is reported for every variant x capital
    feas = {
        (cap, v): row["development"][v]["feasibility"]
        for cap, row in exp.metrics["per_capital"].items()
        for v in VARIANTS
    }
    assert set(feas.values()) <= {"NO ECONOMICALLY VIABLE TRADE", "no setups", "trades possible"}
    small = exp.metrics["per_capital"]["5000"]["development"]["E"]
    assert small["trades"] == 0 or small["risk_rejections"] is not None

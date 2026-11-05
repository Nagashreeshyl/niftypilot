"""Dataset-wide out-of-sample seal (fixes the M3 bypass: a second experiment with a different
end date, or a plain backtest, could previously read a sealed OOS period unrecorded)."""

from datetime import date

import pytest

from niftypilot import synthetic
from niftypilot.backtest.market import InstrumentInfo, MarketData
from niftypilot.backtest.oos import OosSealedError, access_log, guard_period
from niftypilot.backtest.research import evaluate_oos, run_research

KW = dict(min_trades=3, wf_train=10, wf_test=5)


@pytest.fixture(scope="module")
def candles():
    return synthetic.random_market({"AAA": 150.0, "BBB": 900.0}, date(2024, 11, 1), 110, seed=5)


@pytest.fixture
def mkt(candles, dataset):
    info = {s: InstrumentInfo(s, "CASH", 0.05) for s in ("AAA", "BBB")}
    info["NIFTY 50"] = InstrumentInfo("NIFTY 50", "INDEX", 0.05)
    return MarketData(
        {"id": dataset.id, "fingerprint": "fp", "data_kind": "synthetic"},
        candles,
        info,
        benchmark="NIFTY 50",
    )


def oos(exp):
    return [date.fromisoformat(x) for x in exp.out_of_sample_period["range"]]


def test_second_experiment_cannot_silently_reuse_sealed_oos(db, mkt):
    first = run_research(db, mkt, date(2025, 1, 6), date(2025, 3, 28), **KW)
    with pytest.raises(OosSealedError, match="sealed out-of-sample"):
        run_research(db, mkt, date(2025, 1, 6), date(2025, 4, 30), **KW)  # in-sample covers it
    assert first.contaminated is False and access_log(db, first) == []
    run_research(db, mkt, date(2025, 1, 6), date(2025, 4, 30), allow_contamination=True, **KW)
    db.refresh(first)
    log = access_log(db, first)
    assert first.contaminated is True and log[0]["purpose"] == "research"
    assert log[0]["contaminating"] is True


def test_backtest_over_sealed_oos_refused_or_recorded(db, mkt, dataset):
    exp = run_research(db, mkt, date(2025, 1, 6), date(2025, 3, 28), **KW)
    a, b = oos(exp)
    with pytest.raises(OosSealedError):
        guard_period(db, dataset.id, a, b, "backtest")
    assert guard_period(db, dataset.id, date(2024, 12, 2), date(2024, 12, 20), "backtest") == []
    guard_period(db, dataset.id, a, b, "backtest", allow_contamination=True, detail="manual")
    db.refresh(exp)
    assert exp.contaminated and access_log(db, exp)[-1]["detail"] == "manual"


def test_final_evaluation_logged_and_one_shot(db, mkt):
    exp = run_research(db, mkt, date(2025, 1, 6), date(2025, 3, 28), **KW)
    out = evaluate_oos(db, exp, mkt)
    assert out["contaminated"] is False
    assert [(r["purpose"], r["contaminating"]) for r in access_log(db, exp)] == [
        ("final_evaluation", False)
    ]
    with pytest.raises(PermissionError):
        evaluate_oos(db, exp, mkt)
    assert evaluate_oos(db, exp, mkt, force=True)["contaminated"] is True


def test_oos_already_seen_in_sample_is_contaminated_at_birth(db, mkt):
    long = run_research(db, mkt, date(2025, 1, 6), date(2025, 3, 28), **KW)
    short = run_research(db, mkt, date(2025, 1, 6), date(2025, 2, 14), **KW)
    assert oos(short)[1] < oos(long)[0]  # short's in-sample is clear of long's OOS...
    assert short.contaminated is True  # ...but short's OOS was long's development data
    assert "in-sample for earlier experiment" in access_log(db, short)[0]["detail"]
    assert long.contaminated is False

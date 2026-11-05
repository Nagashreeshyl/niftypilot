"""Corporate actions. Candles GENERATED; actions are fixtures with made-up factors/sources."""

import math
from datetime import date

import pandas as pd
import pytest
from sqlalchemy.exc import IntegrityError

from niftypilot import synthetic
from niftypilot.backtest.market import MarketData
from niftypilot.market_data.corporate_actions import (
    Action,
    actions_by_symbol,
    adjust_history,
    load_corporate_actions_csv,
)
from niftypilot.market_data.ingest import ensure_dataset, ingest_candles
from niftypilot.market_data.quality import check_dataset
from niftypilot.models import CorporateAction
from niftypilot.strategy.indicators import relative_volume
from tests.conftest import DAY
from tests.test_backtest import market

HEAD = (
    "trading_symbol,action_type,announcement_date,ex_date,effective_date,source,"
    "adjustment_factor,verification_status\n"
)
SPLIT = Action("split", DAY, 2.0, "adjust_at_read", "verified")
MERGER = Action("merger", DAY, None, "exclude_prior_history", "unverified")


def csv(tmp_path, rows):
    p = tmp_path / "ca.csv"
    p.write_text(HEAD + rows)
    return p


def test_loader_records_policy_and_never_derives_factors(db, instrument, tmp_path):
    n = load_corporate_actions_csv(
        db,
        csv(
            tmp_path,
            f"SYNTH-A,split,2025-12-01,{DAY},{DAY},fixture filing F-1,5,verified\n"
            f"SYNTH-A,merger,2025-12-01,2026-02-02,2026-02-02,fixture scheme S-1,,unverified\n"
            f"SYNTH-A,symbol_change,2025-12-01,2026-03-02,2026-03-02,fixture C-1,,verified\n",
        ),
    )
    assert n == 3
    got = [(a.action_type, a.factor, a.policy) for a in actions_by_symbol(db)["SYNTH-A"]]
    assert got == [
        ("split", 5.0, "adjust_at_read"),
        ("merger", None, "exclude_prior_history"),
        ("symbol_change", None, "no_price_effect"),
    ]


@pytest.mark.parametrize(
    "row,match",
    [
        (f"SYNTH-A,split,2025-12-01,{DAY},{DAY},fixture,,verified", "adjustment_factor"),
        (f"SYNTH-A,bonus,2025-12-01,{DAY},{DAY},fixture,0,verified", "adjustment_factor"),
        (f"SYNTH-A,split,2025-12-01,{DAY},{DAY},TODO,2,verified", "source"),
        (f"SYNTH-A,rights,2025-12-01,{DAY},{DAY},fixture,2,verified", "action_type"),
        (f"NOPE,split,2025-12-01,{DAY},{DAY},fixture,2,verified", "unknown instrument"),
    ],
)
def test_loader_rejects_incomplete_actions(db, instrument, tmp_path, row, match):
    with pytest.raises(ValueError, match=match):
        load_corporate_actions_csv(db, csv(tmp_path, row + "\n"))
    assert db.query(CorporateAction).count() == 0


def test_schema_refuses_split_without_factor(db, instrument):
    db.add(
        CorporateAction(
            instrument_id=instrument.id,
            action_type="split",
            announcement_date=DAY,
            ex_date=DAY,
            effective_date=DAY,
            source="fixture",
            data_policy="exclude_prior_history",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_adjustment_is_read_time_reproducible_and_pre_ex_only():
    hist = synthetic.history_sessions(DAY, n=3)
    raw = hist.copy()
    a = adjust_history(hist, [SPLIT], DAY)
    pd.testing.assert_frame_equal(hist, raw)  # input untouched
    pd.testing.assert_frame_equal(a, adjust_history(hist, [SPLIT], DAY))  # reproducible
    assert (a.volume == hist.volume * 2).all() and (a.close == hist.close / 2).all()
    later = Action("split", date(2026, 3, 2), 2.0, "adjust_at_read", "verified")
    assert adjust_history(hist, [later], DAY) is hist  # not yet effective on DAY
    assert adjust_history(hist, [SPLIT], DAY, vendor_adjusted=True) is hist  # no double adjust
    assert adjust_history(hist, [MERGER], DAY, vendor_adjusted=True).empty


def test_split_keeps_relative_volume_comparable():
    """Pre-split history traded half the shares at twice the price (2:1 split on DAY)."""
    base = market()
    pre = synthetic.history_sessions(DAY, price=200.0, volume=synthetic.BASE_VOLUME // 2)
    today = base.by_day["AAA"][DAY]
    candles = {"AAA": pd.concat([pre, today], ignore_index=True)}
    plain = MarketData(base.dataset, candles, base.info)
    split = MarketData(base.dataset, candles, base.info, actions={"AAA": [SPLIT]})
    merger = MarketData(base.dataset, candles, base.info, actions={"AAA": [MERGER]})
    rv_plain = relative_volume(today, plain.history("AAA", DAY, 10))
    rv_split = relative_volume(today, split.history("AAA", DAY, 10))
    assert rv_plain.iloc[3] == pytest.approx(4.0)  # breakout bar looks 2x stronger than it is
    assert rv_split.iloc[3] == pytest.approx(2.0)  # like-for-like after read-time factor
    assert math.isnan(relative_volume(today, merger.history("AAA", DAY, 10)).iloc[3])  # blocked
    assert (plain.candles["AAA"].volume.iloc[:5] == synthetic.BASE_VOLUME // 2).all()  # raw kept


def test_quality_explains_gaps_with_recorded_actions(db, instrument, tmp_path):
    ds = ensure_dataset(
        db,
        "unknown-adj",
        provider="synthetic",
        source="tests",
        data_kind="synthetic",
        source_timezone="UTC",
        price_adjustment="unknown",
    )
    pre = synthetic.history_sessions(DAY, n=3, price=200.0)
    post = synthetic.session(DAY, [(100, 100.1, 99.9, 100, 1)] * 75)
    ingest_candles(db, ds, instrument.id, pd.concat([pre, post], ignore_index=True), "5m")
    start = pre.timestamp_utc.dt.tz_convert("Asia/Kolkata").dt.date.min()
    r = check_dataset(db, ds, start, DAY)
    assert "possible_corporate_action" in {f.kind for f in r.critical}
    load_corporate_actions_csv(
        db, csv(tmp_path, f"SYNTH-A,split,2025-12-01,{DAY},{DAY},fixture,2,unverified\n")
    )
    r = check_dataset(db, ds, start, DAY)
    assert "possible_corporate_action" not in {f.kind for f in r.critical}
    assert {"corporate_action_gap", "unverified_corporate_action"} <= {f.kind for f in r.warnings}

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from niftypilot import synthetic
from niftypilot.market_data.ingest import (
    constituents_on,
    ensure_dataset,
    ingest_candles,
    load_candles,
)
from niftypilot.market_data.providers import CsvProvider, read_csv_candles
from niftypilot.market_data.resample import resample_candles
from niftypilot.market_data.session import is_trading_day, session_slots
from niftypilot.market_data.validation import is_stale, validate_candles
from niftypilot.models import Candle, ConstituentHistory, Instrument
from tests.conftest import DAY


def full_day():
    return synthetic.history_sessions(DAY + timedelta(days=1), n=1)  # the full DAY session


def kinds(issues, rejected=True):
    return {i.kind for i in issues if i.rejected == rejected}


def test_session_slots_cover_regular_session():
    slots = session_slots(DAY)
    assert len(slots) == 75
    assert slots[0] == datetime(2026, 1, 15, 3, 45, tzinfo=UTC)  # 09:15 IST
    assert slots[-1] == datetime(2026, 1, 15, 9, 55, tzinfo=UTC)  # 15:25 IST


def test_trading_day_respects_weekend_and_supplied_holidays():
    assert is_trading_day(DAY)
    assert not is_trading_day(date(2026, 1, 17))  # Saturday
    assert not is_trading_day(DAY, holidays=[DAY])


def test_csv_naive_timestamps_are_ist_and_stored_utc(tmp_path):
    p = tmp_path / "X_5m.csv"
    p.write_text(
        "timestamp,open,high,low,close,volume\n2026-01-15 09:15:00,1,2,0.5,1.5,10\n"
        "2026-01-15 09:20:00,1,2,0.5,abc,10\n"
    )
    df = read_csv_candles(p)
    assert df.timestamp_utc[0] == pd.Timestamp("2026-01-15 03:45", tz="UTC")
    assert pd.isna(df.close[1])  # bad number kept as NaN for validation to reject
    got = CsvProvider(tmp_path).fetch(
        "X", "5m", datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 1, 16, tzinfo=UTC)
    )
    assert len(got) == 2


def test_csv_missing_columns_rejected(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("timestamp,open\n2026-01-15 09:15,1\n")
    with pytest.raises(ValueError, match="missing columns"):
        read_csv_candles(p)


def test_validation_rejects_each_bad_row_kind():
    df = full_day().head(10).copy()
    df.loc[1, "high"] = df.loc[1, "low"] - 1  # invalid OHLC
    df.loc[2, "close"] = float("nan")
    df.loc[3, "volume"] = -5
    df.loc[4, "open"] = 0
    df.loc[5, "timestamp_utc"] += pd.Timedelta(minutes=2)  # off grid
    extra = df.iloc[[0]].assign(timestamp_utc=pd.Timestamp("2026-01-14 03:00", tz="UTC"))
    clean, issues = validate_candles(pd.concat([df, extra], ignore_index=True))
    assert kinds(issues) == {
        "invalid_ohlc",
        "missing_field",
        "negative_volume",
        "non_positive_price",
        "off_grid",
        "outside_session",
    }
    assert len(clean) == 5  # rows 0, 6-9


def test_duplicates_identical_collapse_conflicting_rejected():
    df = full_day().head(3)
    same = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    clean, issues = validate_candles(same)
    assert len(clean) == 3 and "duplicate" in kinds(issues)
    diff = pd.concat([df, df.iloc[[1]].assign(close=df.close[1] + 0.01)], ignore_index=True)
    clean, issues = validate_candles(diff)
    assert len(clean) == 2 and "conflicting_duplicate" in kinds(issues)


def test_missing_zero_volume_and_outliers_flagged_not_fabricated():
    df = full_day().drop(index=[10, 11]).reset_index(drop=True)
    df.loc[20, "volume"] = 0
    df.loc[30, ["open", "high", "close"]] = [df.close[29], df.close[29] * 1.5, df.close[29] * 1.5]
    clean, issues = validate_candles(df)
    assert len(clean) == 73  # nothing invented
    flagged = [i.kind for i in issues if not i.rejected]
    assert flagged.count("missing_candle") == 2
    assert "zero_volume" in flagged and "outlier" in flagged


def test_staleness():
    t = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)
    assert not is_stale(t, t + timedelta(minutes=10), 5)
    assert is_stale(t, t + timedelta(minutes=12), 5)


def test_ingest_idempotent_and_records_run(db, instrument, dataset):
    df = full_day()
    bad = df.head(1).assign(high=-1)
    run = ingest_candles(db, dataset, instrument.id, pd.concat([df, bad]), "5m")
    assert (run.status, run.rows_received, run.rows_inserted, run.rows_rejected) == (
        "partial",
        76,
        75,
        1,
    )
    run2 = ingest_candles(db, dataset, instrument.id, df, "5m")
    assert (run2.status, run2.rows_inserted) == ("success", 0)
    assert db.scalar(select(Candle).limit(1)).timestamp_utc.tzinfo is UTC
    loaded = load_candles(
        db,
        dataset.id,
        instrument.id,
        "5m",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 2, 1, tzinfo=UTC),
    )
    pd.testing.assert_frame_equal(loaded, df.reset_index(drop=True), check_dtype=False)


def test_dataset_kind_cannot_change(db, dataset):
    with pytest.raises(ValueError, match="synthetic"):
        ensure_dataset(db, "synthetic-test", data_kind="actual")


def test_same_candle_key_can_live_in_two_datasets(db, instrument, dataset):
    other = ensure_dataset(
        db,
        "synthetic-test-2",
        provider="synthetic",
        source="t",
        data_kind="synthetic",
        source_timezone="UTC",
    )
    for ds in (dataset, other):
        assert ingest_candles(db, ds, instrument.id, full_day(), "5m").rows_inserted == 75


def test_resample_15m_and_daily_drop_incomplete_buckets():
    df = full_day()
    m15 = resample_candles(df, 5, "15m")
    assert len(m15) == 25
    first = df.head(3)
    assert m15.iloc[0][["open", "high", "low", "close", "volume"]].tolist() == [
        first.open.iloc[0],
        first.high.max(),
        first.low.min(),
        first.close.iloc[-1],
        first.volume.sum(),
    ]
    gappy = df.drop(index=[4])
    assert len(resample_candles(gappy, 5, "15m")) == 24
    assert len(resample_candles(df, 5, "1d")) == 1
    assert resample_candles(gappy, 5, "1d").empty


def test_point_in_time_constituents(db):
    a, b = (
        Instrument(exchange="NSE", segment="CASH", trading_symbol=s, display_name=s)
        for s in ("AAA", "BBB")
    )
    db.add_all([a, b])
    db.flush()
    db.add_all(
        [
            ConstituentHistory(
                instrument_id=a.id,
                index_name="NIFTY 50",
                effective_from=date(2020, 1, 1),
                effective_until=date(2023, 3, 31),
            ),
            ConstituentHistory(
                instrument_id=b.id, index_name="NIFTY 50", effective_from=date(2023, 4, 1)
            ),
        ]
    )
    db.commit()
    sym = lambda d: [i.trading_symbol for i in constituents_on(db, "NIFTY 50", d)]  # noqa: E731
    assert sym(date(2022, 6, 1)) == ["AAA"]
    assert sym(date(2023, 3, 31)) == ["AAA"]
    assert sym(date(2026, 1, 1)) == ["BBB"]


def test_naive_datetime_rejected_by_db(db, instrument, dataset):
    db.add(
        Candle(
            dataset_id=dataset.id,
            instrument_id=instrument.id,
            interval="5m",
            timestamp_utc=datetime(2026, 1, 1),
            open=1,
            high=1,
            low=1,
            close=1,
            volume=1,
            source="t",
            data_kind="synthetic",
        )
    )
    with pytest.raises(Exception, match="naive"):
        db.commit()

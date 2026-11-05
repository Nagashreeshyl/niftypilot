from datetime import date, timedelta

import pytest

from niftypilot import synthetic
from niftypilot.market_data.ingest import (
    ensure_dataset,
    import_csv_directory,
    ingest_candles,
    load_constituents_csv,
    load_holidays_csv,
    load_instruments_csv,
)
from niftypilot.market_data.quality import check_dataset
from niftypilot.models import ConstituentHistory

START, END = date(2026, 1, 5), date(2026, 1, 9)  # Mon-Fri


def week(price=100.0, seed=1):
    return synthetic.history_sessions(END + timedelta(days=1), n=5, price=price, seed=seed)


def kinds(findings):
    return {f.kind for f in findings}


def test_clean_dataset_passes_with_only_calendar_warning(db, instrument, dataset):
    ingest_candles(db, dataset, instrument.id, week(), "5m")
    r = check_dataset(db, dataset, START, END)
    assert r.status == "warnings" and kinds(r.warnings) == {"no_holiday_calendar"}
    assert not r.critical and dataset.quality_status == "warnings"


def test_holiday_candles_are_critical(db, instrument, dataset, tmp_path):
    (tmp_path / "h.csv").write_text("date,description\n2026-01-07,Test holiday\n")
    load_holidays_csv(db, tmp_path / "h.csv", source="test")
    ingest_candles(db, dataset, instrument.id, week(), "5m")
    r = check_dataset(db, dataset, START, END)
    assert r.status == "failed" and "candles_on_non_trading_day" in kinds(r.critical)


def test_missing_session_and_candles_are_warnings_not_filled(db, instrument, dataset):
    df = week()
    day = df.timestamp_utc.dt.tz_convert("Asia/Kolkata").dt.date
    df = df[day != date(2026, 1, 7)].drop(index=[3, 4])
    ingest_candles(db, dataset, instrument.id, df, "5m")
    r = check_dataset(db, dataset, START, END)
    w = {f.kind: f.count for f in r.warnings}
    assert w["missing_session"] == 1 and w["missing_candle"] == 2 and not r.critical


def test_ingestion_rejections_are_critical(db, instrument, dataset):
    df = week()
    df.loc[10, "high"] = -1
    ingest_candles(db, dataset, instrument.id, df, "5m")
    assert "ingestion_rejected_rows" in kinds(check_dataset(db, dataset, START, END).critical)


@pytest.mark.parametrize(
    "adjustment,severity", [("unknown", "critical"), ("unadjusted", "warnings")]
)
def test_split_like_gap_depends_on_declared_adjustment(db, instrument, adjustment, severity):
    ds = ensure_dataset(
        db,
        f"d-{adjustment}",
        provider="synthetic",
        source="t",
        data_kind="synthetic",
        source_timezone="UTC",
        price_adjustment=adjustment,
    )
    df = week()
    day = df.timestamp_utc.dt.tz_convert("Asia/Kolkata").dt.date
    later = day >= date(2026, 1, 8)
    df.loc[later, ["open", "high", "low", "close"]] /= 2  # looks like a 1:2 split
    ingest_candles(db, ds, instrument.id, df, "5m")
    r = check_dataset(db, ds, START, END)
    assert "possible_corporate_action" in kinds(
        getattr(r, severity if severity == "critical" else "warnings")
    )


def test_missing_point_in_time_membership_is_critical(db, instrument, dataset):
    ingest_candles(db, dataset, instrument.id, week(), "5m")
    r = check_dataset(db, dataset, START, END, index_name="NIFTY 50")
    assert "no_point_in_time_membership" in kinds(r.critical)
    db.add(
        ConstituentHistory(
            instrument_id=instrument.id, index_name="NIFTY 50", effective_from=date(2020, 1, 1)
        )
    )
    db.commit()
    r = check_dataset(db, dataset, START, END, index_name="NIFTY 50")
    assert "no_point_in_time_membership" not in kinds(r.critical)


def test_csv_loaders_end_to_end(db, tmp_path):
    (tmp_path / "inst.csv").write_text(
        "trading_symbol,display_name,exchange,segment,isin,tick_size\n"
        "AAA,Aaa Ltd,NSE,CASH,INE000000001,0.05\nNIFTY 50,Nifty 50,NSE,INDEX,,0.05\n"
    )
    assert load_instruments_csv(db, tmp_path / "inst.csv") == 2
    assert load_instruments_csv(db, tmp_path / "inst.csv") == 0  # idempotent
    (tmp_path / "c.csv").write_text(
        "trading_symbol,index_name,effective_from,effective_until\nAAA,NIFTY 50,2020-01-01,\n"
    )
    load_constituents_csv(db, tmp_path / "c.csv")
    data = tmp_path / "data"
    data.mkdir()
    df = week()
    out = df.assign(timestamp=df.timestamp_utc.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    out.drop(columns="timestamp_utc").to_csv(data / "AAA_5m.csv", index=False)
    ds = ensure_dataset(  # generated data: labelled synthetic, never "actual"
        db,
        "generated-x",
        provider="csv",
        source="tests: generated",
        data_kind="synthetic",
        source_timezone="Asia/Kolkata",
        price_adjustment="unadjusted",
    )
    runs = import_csv_directory(db, ds, data)
    assert [r.rows_inserted for r in runs] == [375]
    r = check_dataset(db, ds, START, END, index_name="NIFTY 50")
    assert r.status == "warnings" and not r.critical
    (data / "ZZZ_5m.csv").write_text("timestamp,open,high,low,close,volume\n")
    with pytest.raises(ValueError, match="unknown instrument"):
        import_csv_directory(db, ds, data)

"""Exchange session calendar. All candles GENERATED; session records are fixtures, not
real NSE circulars (dates are arbitrary)."""

from datetime import date, time

import pandas as pd
import pytest

from niftypilot.backtest.market import MarketData
from niftypilot.market_data.calendar import (
    REGULAR,
    ExchangeCalendar,
    Window,
    load_calendar,
    load_sessions_csv,
)
from niftypilot.market_data.ingest import ensure_dataset, ingest_candles, load_holidays_csv
from niftypilot.market_data.quality import check_dataset
from niftypilot.market_data.validation import validate_candles
from tests.conftest import DAY
from tests.m33_helpers import bars, ist
from tests.test_backtest import market, run

SUNDAY = date(2026, 1, 11)
SATURDAY = date(2026, 1, 10)
MON, FRI = date(2026, 1, 5), date(2026, 1, 9)
HOLIDAY = date(2026, 1, 7)


def sessions_csv(tmp_path, rows: str):
    p = tmp_path / "s.csv"
    p.write_text("date,session_type,open_time,close_time,source,verification_status\n" + rows)
    return p


def kinds(findings):
    return {f.kind for f in findings}


def test_regular_holiday_and_weekend_windows():
    cal = ExchangeCalendar(holidays={HOLIDAY})
    assert cal.windows(MON) == [REGULAR] and cal.is_regular(MON)
    assert cal.windows(HOLIDAY) == [] and cal.windows(SATURDAY) == []
    assert len(cal.slots(MON, 5)) == 75 and cal.slots(SUNDAY, 5) == []


def test_muhurat_session_accepted_only_with_record(db, instrument, dataset, tmp_path):
    muhurat = bars(SUNDAY, time(18, 0), time(19, 0))
    assert len(muhurat) == 12
    # no record: an evening Sunday session is never accepted
    run_ = ingest_candles(db, dataset, instrument.id, muhurat, "5m")
    assert run_.rows_inserted == 0 and run_.rows_rejected == 12
    assert {e["kind"] for e in run_.errors} == {"outside_session"}

    load_sessions_csv(
        db, sessions_csv(tmp_path, f"{SUNDAY},muhurat,18:00,19:00,fixture circular,unverified\n")
    )
    ok = ingest_candles(db, dataset, instrument.id, muhurat, "5m")
    assert ok.rows_inserted == 12 and ok.rows_rejected == 0


def test_muhurat_quality_is_distinguishable_and_not_critical(db, instrument, dataset, tmp_path):
    load_sessions_csv(
        db, sessions_csv(tmp_path, f"{SUNDAY},muhurat,18:00,19:00,fixture,unverified\n")
    )
    ingest_candles(db, dataset, instrument.id, bars(SUNDAY, time(18, 0), time(19, 0)), "5m")
    r = check_dataset(db, dataset, SUNDAY, SUNDAY)
    assert not r.critical
    assert {"special_session_candles", "unverified_session_record"} <= kinds(r.warnings)
    assert "missing_candle" not in kinds(r.warnings)  # expected slots are the session's own


def test_candle_outside_documented_window_rejected_with_special_kind():
    cal = ExchangeCalendar(special={SUNDAY: [Window(time(18, 0), time(19, 0), "muhurat")]})
    df = pd.concat([bars(SUNDAY, time(18, 0), time(19, 0)), bars(SUNDAY, time(19, 0), time(19, 5))])
    clean, issues = validate_candles(df, 5, calendar=cal)
    assert len(clean) == 12
    assert [i.kind for i in issues if i.rejected] == ["outside_special_session"]
    # regular days keep the regular rule and kind
    _, reg = validate_candles(bars(MON, time(15, 30), time(15, 35)), 5, calendar=cal)
    assert [i.kind for i in reg if i.rejected] == ["outside_session"]


def test_special_session_with_two_windows(db, tmp_path):
    load_sessions_csv(
        db,
        sessions_csv(
            tmp_path,
            f"{SATURDAY},special,09:15,10:00,fixture,verified\n{SATURDAY},special,11:30,12:30,fixture,verified\n",
        ),
    )
    cal = load_calendar(db, SATURDAY, SATURDAY)
    assert len(cal.slots(SATURDAY, 5)) == 9 + 12
    gap = bars(SATURDAY, time(10, 0), time(10, 5))
    _, issues = validate_candles(gap, 5, calendar=cal)
    assert [i.kind for i in issues if i.rejected] == ["outside_special_session"]
    _, issues = validate_candles(bars(SATURDAY, time(9, 15), time(10, 0)), 5, calendar=cal)
    assert sum(i.kind == "missing_candle" for i in issues) == 12  # second window absent


def test_extended_session(db, instrument, dataset, tmp_path):
    late = bars(MON, time(15, 30), time(17, 0))
    assert ingest_candles(db, dataset, instrument.id, late, "5m").rows_rejected == 18
    load_sessions_csv(db, sessions_csv(tmp_path, f"{MON},extended,09:15,17:00,fixture,verified\n"))
    run_ = ingest_candles(db, dataset, instrument.id, late, "5m")
    assert run_.rows_inserted == 18 and run_.rows_rejected == 0


def test_undocumented_weekend_session_is_critical(db, instrument, dataset):
    ingest_candles(db, dataset, instrument.id, bars(SATURDAY, time(9, 15), time(15, 30)), "5m")
    r = check_dataset(db, dataset, SATURDAY, SATURDAY)
    assert "candles_on_non_trading_day" in kinds(r.critical)


def test_holiday_with_muhurat_record_is_a_session(db, instrument, dataset, tmp_path):
    (tmp_path / "h.csv").write_text(f"date,description\n{HOLIDAY},fixture holiday\n")
    load_holidays_csv(db, tmp_path / "h.csv", source="fixture")
    load_sessions_csv(
        db, sessions_csv(tmp_path, f"{HOLIDAY},muhurat,18:15,19:15,fixture,verified\n")
    )
    ingest_candles(db, dataset, instrument.id, bars(HOLIDAY, time(18, 15), time(19, 15)), "5m")
    r = check_dataset(db, dataset, HOLIDAY, HOLIDAY)
    assert not r.critical and "special_session_candles" in kinds(r.warnings)


@pytest.mark.parametrize(
    "row,match",
    [
        (f"{SUNDAY},festival,18:00,19:00,fixture,verified", "session_type"),
        (f"{SUNDAY},muhurat,18:00,19:00,unknown,verified", "source"),
        (f"{SUNDAY},muhurat,19:00,18:00,fixture,verified", "not after"),
        (f"{SUNDAY},muhurat,18:00,19:00,fixture,maybe", "verification_status"),
        (
            f"{SUNDAY},muhurat,18:00,19:00,fixture,verified\n{SUNDAY},muhurat,18:30,19:30,f,verified",
            "overlapping",
        ),
    ],
)
def test_session_loader_rejects_bad_records(db, tmp_path, row, match):
    with pytest.raises(ValueError, match=match):
        load_sessions_csv(db, sessions_csv(tmp_path, row + "\n"))
    assert load_calendar(db, SUNDAY, SUNDAY).special == {}


def test_backtest_never_trades_or_learns_from_special_sessions():
    base = market()
    assert run(base).trades  # the fixture day trades normally
    m = MarketData(base.dataset, base.candles, base.info, special_sessions={DAY})
    res = run(m)
    assert not res.trades and res.days[0]["skipped"].startswith("non-regular session")
    prev = sorted(base.by_day["AAA"])[-2]
    m2 = MarketData(base.dataset, base.candles, base.info, special_sessions={prev})
    assert prev not in m2.by_day["AAA"]
    hist = m2.history("AAA", DAY, 10)
    assert prev not in set(hist.timestamp_utc.dt.tz_convert("Asia/Kolkata").dt.date)
    assert ist(DAY, 9, 15) in set(m2.by_day["AAA"][DAY].timestamp_utc)


def test_actual_data_never_assumes_a_weekday_calendar(db):
    for kind in ("synthetic", "actual"):
        ds = ensure_dataset(
            db,
            f"cal-{kind}",
            provider="t",
            source="t",
            data_kind=kind,
            source_timezone="UTC",
            price_adjustment="unadjusted",
        )
        r = check_dataset(db, ds, MON, FRI)
        assert ("calendar_year_missing" in kinds(r.critical)) == (kind == "actual")
        assert "no_holiday_calendar" in kinds(r.warnings)

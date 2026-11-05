"""Timestamp conventions in manifest import. GENERATED vendor-like files, fake TESTCO."""

from datetime import date, time, timedelta

import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from niftypilot.market_data.calendar import load_sessions_csv
from niftypilot.market_data.ingest import load_candles
from niftypilot.market_data.provenance import import_manifest
from niftypilot.market_data.quality import utc_range
from niftypilot.market_data.resample import resample_candles
from niftypilot.models import Candle
from tests.m33_helpers import bars, ist
from tests.test_provenance import END, START, manifest, week, write_vendor_csv

SUNDAY = date(2026, 1, 11)


def shifted(df, minutes):
    return df.assign(timestamp_utc=df.timestamp_utc + timedelta(minutes=minutes))


def stored(db, ds):
    a, b = utc_range(START - timedelta(days=7), END + timedelta(days=7))
    return load_candles(db, ds.id, 1, "5m", a, b)


def n_candles(db):
    return db.scalar(select(func.count()).select_from(Candle))


def imp(db, tmp_path, df, convention, name="v.csv", **over):
    write_vendor_csv(tmp_path / name, df, mapped=True)
    return import_manifest(
        db,
        manifest(
            tmp_path, [{"path": name, "symbol": "TESTCO"}], timestamp_convention=convention, **over
        ),
        tmp_path / "raw",
    )


def test_start_input_stored_as_is_with_source_text(db, tmp_path, testco):
    ds, runs = imp(db, tmp_path, week(), "start")
    assert runs[0].rows_inserted == 375
    got = stored(db, ds)
    assert got.timestamp_utc.iloc[0] == ist(START, 9, 15)  # session opening
    assert got.timestamp_utc.iloc[-1] == ist(END, 15, 25)  # last candle of the session
    first = db.scalars(select(Candle).order_by(Candle.timestamp_utc)).first()
    assert first.source_timestamp == "05/01/2026 09:15"
    assert ds.manifest["files"][0]["timestamp_convention"] == "start"


def test_end_input_converted_to_canonical_start(db, tmp_path, testco):
    ds, runs = imp(db, tmp_path, shifted(week(), 5), "end")  # vendor labels 09:20 ... 15:30
    assert runs[0].rows_inserted == 375 and runs[0].rows_rejected == 0
    got = stored(db, ds)
    pd.testing.assert_frame_equal(got, week().reset_index(drop=True), check_dtype=False)
    first = db.scalars(select(Candle).order_by(Candle.timestamp_utc)).first()
    last = db.scalars(select(Candle).order_by(Candle.timestamp_utc.desc())).first()
    assert (first.source_timestamp, last.source_timestamp) == (
        "05/01/2026 09:20",
        "09/01/2026 15:30",
    )


@pytest.mark.parametrize(
    "shift,declared",
    [(5, "start"), (0, "end")],  # END data called START, and vice versa
)
def test_wrong_convention_refused_whole_file_no_duplicates(db, tmp_path, testco, shift, declared):
    with pytest.raises(ValueError, match="outside documented sessions"):
        imp(db, tmp_path, shifted(week(), shift), declared)
    assert n_candles(db) == 0  # nothing partially inserted


def test_mixing_conventions_in_one_dataset_refused(db, tmp_path, testco):
    imp(db, tmp_path, week(), "start")
    with pytest.raises(ValueError, match="timestamp_convention"):
        imp(db, tmp_path, shifted(week(), 5), "end", name="v2.csv")
    assert n_candles(db) == 375  # no shifted second copy


def test_duplicates_detected(db, tmp_path, testco):
    df = week()
    same = pd.concat([df, df.iloc[[10]]], ignore_index=True)
    _, runs = imp(db, tmp_path, same, "start")
    assert runs[0].rows_inserted == 375 and runs[0].rows_rejected == 1  # identical copy
    conflict = df.iloc[[20]].assign(close=df.close.iloc[20] + 1, high=df.high.iloc[20] + 1)
    _, runs = imp(db, tmp_path, pd.concat([df, conflict]), "start", name="c.csv", dataset="other")
    kinds = {e["kind"] for e in runs[0].errors}
    assert "conflicting_duplicate" in kinds and runs[0].rows_inserted == 374


def test_special_session_end_labelled(db, tmp_path, testco):
    (tmp_path / "s.csv").write_text(
        f"date,session_type,open_time,close_time,source\n{SUNDAY},muhurat,18:00,19:00,fixture\n"
    )
    load_sessions_csv(db, tmp_path / "s.csv")
    ds, runs = imp(db, tmp_path, shifted(bars(SUNDAY, time(18, 0), time(19, 0)), 5), "end")
    assert runs[0].rows_inserted == 12
    first = db.scalars(select(Candle).order_by(Candle.timestamp_utc)).first()
    assert first.timestamp_utc == ist(SUNDAY, 18, 0).to_pydatetime()


def test_incorrect_timezone_input(db, tmp_path, testco):
    df = week()
    # offsets in the file contradict the declared zone
    out = pd.DataFrame(
        {
            "timestamp": df.timestamp_utc.dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            **{c: df[c] for c in ["open", "high", "low", "close", "volume"]},
        }
    )
    out.to_csv(tmp_path / "z.csv", index=False)
    ist_naive = dict(format="niftypilot_csv", columns=None, timestamp_format=None)
    with pytest.raises(ValueError, match="contradicts source_timezone"):
        import_manifest(
            db,
            manifest(
                tmp_path,
                [{"path": "z.csv", "symbol": "TESTCO"}],
                timestamp_convention="start",
                **ist_naive,
            ),
            tmp_path / "raw",
        )
    # IST wall-clock declared as UTC lands outside the session
    with pytest.raises(ValueError, match="outside documented sessions"):
        imp(db, tmp_path, df, "start", source_timezone="UTC")
    with pytest.raises(ValidationError, match="IANA"):
        imp(db, tmp_path, df, "start", source_timezone="Mars/Phobos")
    assert n_candles(db) == 0
    # offsets that agree with the zone are fine
    out.assign(
        timestamp=df.timestamp_utc.dt.tz_convert("Asia/Kolkata").dt.strftime(
            "%Y-%m-%dT%H:%M:%S+05:30"
        )
    ).to_csv(tmp_path / "z.csv", index=False)
    _, runs = import_manifest(
        db,
        manifest(
            tmp_path,
            [{"path": "z.csv", "symbol": "TESTCO"}],
            timestamp_convention="start",
            **ist_naive,
        ),
        tmp_path / "raw",
    )
    assert runs[0].rows_inserted == 375


def test_convention_must_be_declared(db, tmp_path, testco):
    write_vendor_csv(tmp_path / "v.csv", week(), mapped=True)
    p = manifest(tmp_path, [{"path": "v.csv", "symbol": "TESTCO"}])
    import json

    m = json.loads(p.read_text())
    del m["timestamp_convention"]
    p.write_text(json.dumps(m))
    with pytest.raises(ValidationError, match="timestamp_convention"):
        import_manifest(db, p, tmp_path / "raw")
    with pytest.raises(ValidationError):
        imp(db, tmp_path, week(), "middle")


def test_15m_aggregation_identical_for_start_and_end_input(db, tmp_path, testco):
    ds, _ = imp(db, tmp_path, shifted(week(), 5), "end")
    m15 = resample_candles(stored(db, ds), 5, "15m")
    expected = resample_candles(week(), 5, "15m")
    assert len(m15) == 125 and m15.timestamp_utc.iloc[0] == ist(START, 9, 15)
    pd.testing.assert_frame_equal(m15, expected, check_dtype=False)

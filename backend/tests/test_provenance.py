"""Manifest import for actual data. Fixtures are GENERATED (fake TESTCO symbols) in a
throwaway test database: they exercise the pipeline and are not market data."""

import json
from datetime import date, timedelta

import pandas as pd
import pytest
from pydantic import ValidationError

from niftypilot import synthetic
from niftypilot.backtest.market import load_market
from niftypilot.market_data.ingest import (
    ensure_dataset,
    import_csv_directory,
    ingest_candles,
    load_holidays_csv,
)
from niftypilot.market_data.provenance import (
    import_manifest,
    load_aliases_csv,
    sha256_file,
)
from niftypilot.market_data.quality import check_dataset
from niftypilot.models import ConstituentHistory, Instrument

START, END = date(2026, 1, 5), date(2026, 1, 9)


def week():
    return synthetic.history_sessions(END + timedelta(days=1), n=5)


def write_vendor_csv(path, df, mapped=False):
    out = pd.DataFrame(
        {
            "timestamp": df.timestamp_utc.dt.tz_convert("Asia/Kolkata").dt.strftime(
                "%d/%m/%Y %H:%M"
            ),
            **{c: df[c] for c in ["open", "high", "low", "close", "volume"]},
        }
    )
    if mapped:
        out.columns = ["Date", "O", "H", "L", "C", "Vol"]
    out.to_csv(path, index=False)


def manifest(tmp_path, files, **over):
    m = {
        "dataset": "vendor-test",
        "provider": "Test Vendor",
        "format": "mapped_csv",
        "permission": "unit-test fixture licence ref T-1",
        "acquisition_date": "2026-10-01",
        "interval": "5m",
        "source_timezone": "Asia/Kolkata",
        "timestamp_convention": "start",
        "price_adjustment": "unadjusted",
        "adjustment_method": "raw prices, no corporate-action adjustment",
        "columns": {
            "timestamp": "Date",
            "open": "O",
            "high": "H",
            "low": "L",
            "close": "C",
            "volume": "Vol",
        },
        "timestamp_format": "%d/%m/%Y %H:%M",
        "files": files,
    }
    m.update(over)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m))
    return p


def test_manifest_import_preserves_raw_files_and_provenance(db, tmp_path, testco):
    write_vendor_csv(tmp_path / "testco.csv", week(), mapped=True)
    ds, runs = import_manifest(
        db, manifest(tmp_path, [{"path": "testco.csv", "symbol": "TESTCO"}]), tmp_path / "raw"
    )
    assert [r.rows_inserted for r in runs] == [375]
    f = ds.manifest["files"][0]
    assert f["sha256"] == sha256_file(tmp_path / "testco.csv")
    stored = tmp_path / "raw" / "vendor-test" / f"{f['sha256'][:16]}_testco.csv"
    assert (
        f["stored"] == str(stored) and stored.read_bytes() == (tmp_path / "testco.csv").read_bytes()
    )
    assert (ds.provenance_status, ds.data_kind, ds.interval) == ("declared", "actual", "5m")
    assert (ds.coverage_start, ds.coverage_end) == (START, END)
    assert ds.quality_status == "unchecked"
    _, again = import_manifest(
        db, manifest(tmp_path, [{"path": "testco.csv", "symbol": "TESTCO"}]), tmp_path / "raw"
    )
    assert again[0].rows_inserted == 0 and len(ds.manifest["files"]) == 1  # idempotent


@pytest.mark.parametrize(
    "field,value",
    [
        ("permission", "unknown"),
        ("permission", ""),
        ("provider", "n/a"),
        ("adjustment_method", "TODO"),
        ("price_adjustment", "unknown"),
        ("acquisition_date", "2999-01-01"),
        ("data_kind", "synthetic"),
    ],
)
def test_missing_or_placeholder_provenance_rejected(db, tmp_path, testco, field, value):
    write_vendor_csv(tmp_path / "t.csv", week(), mapped=True)
    with pytest.raises(ValidationError):
        import_manifest(
            db,
            manifest(tmp_path, [{"path": "t.csv", "symbol": "TESTCO"}], **{field: value}),
            tmp_path / "raw",
        )


def test_mapped_csv_requires_full_column_mapping(db, tmp_path, testco):
    write_vendor_csv(tmp_path / "t.csv", week(), mapped=True)
    with pytest.raises(ValidationError, match="column mapping"):
        import_manifest(
            db,
            manifest(
                tmp_path, [{"path": "t.csv", "symbol": "TESTCO"}], columns={"timestamp": "Date"}
            ),
            tmp_path / "raw",
        )


def test_actual_data_cannot_bypass_manifest(db, tmp_path):
    ds = ensure_dataset(
        db,
        "sneaky",
        provider="csv",
        source="x",
        data_kind="actual",
        source_timezone="Asia/Kolkata",
        price_adjustment="unadjusted",
    )
    with pytest.raises(ValueError, match="manifest"):
        import_csv_directory(db, ds, tmp_path)
    with pytest.raises(ValueError, match="provenance"):
        ingest_candles(db, ds, 1, week(), "5m")
    ds.quality_status, ds.quality_report = "passed", {"start": "2026-01-01", "end": "2026-12-31"}
    with pytest.raises(ValueError, match="provenance"):
        load_market(db, ds, START, END)
    assert "missing_provenance" in {f.kind for f in check_dataset(db, ds, START, END).critical}


def test_synthetic_instrument_rejected_from_actual_dataset(db, tmp_path):
    db.add(
        Instrument(
            exchange="SYNTH", segment="CASH", trading_symbol="SYNTH01", display_name="synthetic"
        )
    )
    db.commit()
    write_vendor_csv(tmp_path / "s.csv", week(), mapped=True)
    with pytest.raises(ValueError, match="synthetic instrument"):
        import_manifest(
            db, manifest(tmp_path, [{"path": "s.csv", "symbol": "SYNTH01"}]), tmp_path / "raw"
        )


def test_symbol_change_via_alias(db, tmp_path, testco):
    (tmp_path / "a.csv").write_text(
        "alias,trading_symbol,valid_from,valid_until,source\n"
        "OLDCO,TESTCO,2020-01-01,2026-01-07,fixture\n"
    )
    load_aliases_csv(db, tmp_path / "a.csv")
    df = week()
    day = df.timestamp_utc.dt.tz_convert("Asia/Kolkata").dt.date
    write_vendor_csv(tmp_path / "old.csv", df[day <= date(2026, 1, 7)], mapped=True)
    ds, runs = import_manifest(
        db, manifest(tmp_path, [{"path": "old.csv", "symbol": "OLDCO"}]), tmp_path / "raw"
    )
    assert ds.manifest["files"][0]["instrument"] == "TESTCO" and runs[0].rows_inserted == 225
    write_vendor_csv(tmp_path / "span.csv", df, mapped=True)  # crosses the rename date
    with pytest.raises(ValueError, match="spans a symbol change"):
        import_manifest(
            db, manifest(tmp_path, [{"path": "span.csv", "symbol": "OLDCO"}]), tmp_path / "raw"
        )


def test_member_without_data_flagged(db, tmp_path, testco):
    other = Instrument(
        exchange="NSE", segment="CASH", trading_symbol="GONECO", display_name="no data"
    )
    db.add(other)
    db.flush()
    for i in (testco, other):
        db.add(
            ConstituentHistory(
                instrument_id=i.id,
                index_name="NIFTY 50",
                effective_from=date(2020, 1, 1),
                source_document="fixture press release FX-1",  # actual data needs a source
            )
        )
    db.commit()
    (tmp_path / "h.csv").write_text("date,description\n2026-01-26,fixture holiday\n")
    load_holidays_csv(db, tmp_path / "h.csv", source="fixture circular")  # actual: year needed
    write_vendor_csv(tmp_path / "t.csv", week(), mapped=True)
    ds, _ = import_manifest(
        db, manifest(tmp_path, [{"path": "t.csv", "symbol": "TESTCO"}]), tmp_path / "raw"
    )
    r = check_dataset(db, ds, START, END, index_name="NIFTY 50")
    gone = [f for f in r.warnings if f.kind == "member_without_data"]
    assert gone and gone[0].instrument == "GONECO" and gone[0].count == 5
    assert not r.critical

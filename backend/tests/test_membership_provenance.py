"""Point-in-time membership provenance. Fixture symbols/documents, not real NSE data."""

from datetime import date

import pytest

from niftypilot.backtest.market import load_market
from niftypilot.market_data.ingest import (
    constituents_on,
    ensure_dataset,
    ingest_candles,
    load_constituents_csv,
    membership_conflicts,
    supersede_membership,
)
from niftypilot.market_data.quality import check_dataset
from niftypilot.models import ConstituentHistory, Instrument
from tests.test_quality import END, START, week

IDX = "NIFTY 50"
HEAD = (
    "trading_symbol,index_name,effective_from,effective_until,source_document,"
    "source_published,verification_status\n"
)
SWAP = date(2026, 1, 7)  # fixture reconstitution: OUTCO leaves, INCO joins


@pytest.fixture
def three(db):
    insts = [
        Instrument(exchange="NSE", segment="CASH", trading_symbol=s, display_name=s)
        for s in ("KEEPCO", "OUTCO", "INCO")
    ]
    db.add_all(insts)
    db.commit()
    return insts


def write(tmp_path, rows):
    p = tmp_path / "c.csv"
    p.write_text(HEAD + rows)
    return p


def members(db, d):
    return [i.trading_symbol for i in constituents_on(db, IDX, d)]


def load_swap(db, tmp_path):
    load_constituents_csv(
        db,
        write(
            tmp_path,
            f"KEEPCO,{IDX},2020-01-01,,PR-1 fixture,2019-12-01,verified\n"
            f"OUTCO,{IDX},2020-01-01,2026-01-06,PR-2 fixture,2025-12-20,verified\n"
            f"INCO,{IDX},{SWAP},,PR-2 fixture,2025-12-20,unverified\n",
        ),
    )


def test_additions_removals_and_history(db, three, tmp_path):
    load_swap(db, tmp_path)
    assert members(db, date(2026, 1, 6)) == ["KEEPCO", "OUTCO"]
    assert members(db, SWAP) == ["INCO", "KEEPCO"]
    # old membership is never inferred from today's: INCO was not a member in 2025
    assert members(db, date(2025, 6, 2)) == ["KEEPCO", "OUTCO"]
    row = db.query(ConstituentHistory).filter_by(effective_from=SWAP).one()
    assert (row.source_document, row.source_published) == ("PR-2 fixture", date(2025, 12, 20))


def test_universe_uses_membership_on_simulated_date(db, three, dataset, tmp_path):
    load_swap(db, tmp_path)
    for i in three:
        ingest_candles(db, dataset, i.id, week(), "5m")
    check_dataset(db, dataset, START, END, index_name=IDX)
    m = load_market(db, dataset, START, END, index_name=IDX)
    assert m.membership(date(2026, 1, 6)) == {"KEEPCO", "OUTCO"}
    assert m.membership(SWAP) == {"KEEPCO", "INCO"}


def test_missing_and_unverified_provenance_flagged(db, three, tmp_path):
    load_constituents_csv(
        db, write(tmp_path, f"KEEPCO,{IDX},2020-01-01,,,,\nOUTCO,{IDX},2020-01-01,,PR-1,,\n")
    )
    for kind in ("synthetic", "actual"):
        ds = ensure_dataset(
            db,
            f"m-{kind}",
            provider="t",
            source="t",
            data_kind=kind,
            source_timezone="UTC",
            price_adjustment="unadjusted",
        )
        r = check_dataset(db, ds, START, END, index_name=IDX)
        bucket = r.critical if kind == "actual" else r.warnings
        miss = [f for f in bucket if f.kind == "membership_missing_source"]
        assert miss and miss[0].count == 1
        assert "unverified_membership" in {f.kind for f in r.warnings}


def test_conflicting_records_rejected_at_load(db, three, tmp_path):
    load_swap(db, tmp_path)
    with pytest.raises(ValueError, match="overlapping membership"):
        load_constituents_csv(db, write(tmp_path, f"OUTCO,{IDX},2025-06-01,,PR-9,,\n"))
    assert db.query(ConstituentHistory).count() == 3  # rolled back


def test_conflict_is_critical_until_explicitly_superseded(db, three, dataset, tmp_path):
    load_swap(db, tmp_path)
    bad = ConstituentHistory(  # inserted bypassing the loader
        instrument_id=three[1].id,
        index_name=IDX,
        effective_from=date(2025, 6, 1),
        source_document="misread PR",
    )
    db.add(bad)
    db.commit()
    assert len(membership_conflicts(db, IDX)) == 1
    r = check_dataset(db, dataset, START, END, index_name=IDX)
    assert "conflicting_membership" in {f.kind for f in r.critical}
    with pytest.raises(ValueError, match="explain"):
        supersede_membership(db, bad.id, "wrong")
    supersede_membership(db, bad.id, "PR-2 shows OUTCO left on 2026-01-07; row misread")
    assert membership_conflicts(db, IDX) == []
    assert "OUTCO" not in members(db, SWAP)  # superseded row ignored, but kept
    assert db.get(ConstituentHistory, bad.id).verification_status == "superseded"

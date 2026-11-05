import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from niftypilot.market_data.calendar import load_calendar
from niftypilot.market_data.providers import read_csv_candles
from niftypilot.market_data.session import IST
from niftypilot.market_data.validation import validate_candles
from niftypilot.models import (
    Candle,
    ConstituentHistory,
    Dataset,
    IngestionRun,
    Instrument,
    TradingHoliday,
)

INTERVAL_MINUTES = {"5m": 5, "15m": 15, "1d": None}
CANDLE_COLS = ["timestamp_utc", "open", "high", "low", "close", "volume"]


def _dialect_insert(db: Session, model):
    return {"sqlite": sqlite, "postgresql": postgresql}[db.bind.dialect.name].insert(model)


def ingest_candles(
    db: Session,
    dataset: Dataset,
    instrument_id: int,
    df: pd.DataFrame,
    interval: str,
    source: str | None = None,
    requested_start: datetime | None = None,
    requested_end: datetime | None = None,
) -> IngestionRun:
    """Validate and insert candles into `dataset` idempotently (existing keys are kept)."""
    if dataset.data_kind == "actual" and dataset.provenance_status != "declared":
        raise ValueError(
            f"dataset {dataset.name!r} is actual data without declared provenance; "
            "import it with a manifest (market_data/provenance.py)"
        )
    run = IngestionRun(
        provider=dataset.provider,
        dataset_id=dataset.id,
        instrument_id=instrument_id,
        interval=interval,
        requested_start=requested_start,
        requested_end=requested_end,
        status="failed",
        rows_received=len(df),
    )
    db.add(run)
    try:
        days = df.timestamp_utc.dropna().dt.tz_convert(IST).dt.date
        cal = load_calendar(db, days.min(), days.max()) if len(days) else None
        clean, issues = validate_candles(df, INTERVAL_MINUTES[interval], calendar=cal)
        has_src = "source_timestamp" in clean.columns
        rows = [
            dict(
                dataset_id=dataset.id,
                instrument_id=instrument_id,
                interval=interval,
                timestamp_utc=r.timestamp_utc.to_pydatetime(),
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                volume=int(r.volume),
                source=source or dataset.source,
                data_kind=dataset.data_kind,
                source_timestamp=r.source_timestamp if has_src else None,
            )
            for r in clean.itertuples()
        ]
        inserted = 0
        if rows:
            stmt = _dialect_insert(db, Candle).on_conflict_do_nothing(
                index_elements=["dataset_id", "instrument_id", "interval", "timestamp_utc"]
            )
            # Core execute: the ORM bulk-insert result has no rowcount
            inserted = db.connection().execute(stmt, rows).rowcount
        run.rows_inserted = inserted
        run.rows_rejected = sum(i.rejected for i in issues)
        run.errors = [i.__dict__ for i in issues if i.rejected]
        run.status = "partial" if run.rows_rejected else "success"
    except Exception as exc:
        db.rollback()
        db.add(run)
        run.errors = [{"kind": "exception", "detail": repr(exc)}]
        run.status = "failed"
    run.finished_at = datetime.now(UTC)
    db.commit()
    return run


def ensure_dataset(db: Session, name: str, **fields) -> Dataset:
    """Fetch dataset by name or create it. Existing datasets are never mutated here."""
    ds = db.scalar(select(Dataset).where(Dataset.name == name))
    if ds is None:
        ds = Dataset(name=name, **fields)
        db.add(ds)
        db.commit()
    elif fields.get("data_kind", ds.data_kind) != ds.data_kind:
        raise ValueError(f"dataset {name!r} is {ds.data_kind}, not {fields['data_kind']}")
    return ds


def import_csv_directory(
    db: Session, dataset: Dataset, directory: str | Path, interval: str = "5m"
) -> list[IngestionRun]:
    """Import `<SYMBOL>_<interval>.csv` files for synthetic/simulated datasets.

    Actual data must go through provenance.import_manifest (raw-file preservation, licence)."""
    if dataset.data_kind == "actual":
        raise ValueError("actual data must be imported with a provenance manifest")
    runs = []
    for path in sorted(Path(directory).glob(f"*_{interval}.csv")):
        symbol = path.name[: -len(f"_{interval}.csv")]
        inst = db.scalar(select(Instrument).where(Instrument.trading_symbol == symbol))
        if inst is None:
            raise ValueError(f"{path.name}: unknown instrument {symbol!r}; load instruments first")
        df = read_csv_candles(path, dataset.source_timezone)
        runs.append(
            ingest_candles(db, dataset, inst.id, df, interval, f"{dataset.source}:{path.name}")
        )
    return runs


def load_instruments_csv(db: Session, path: str | Path) -> int:
    """Columns: trading_symbol,display_name,exchange,segment[,isin,tick_size,lot_size]."""
    df = pd.read_csv(path, dtype=str).fillna("")
    n = 0
    for r in df.itertuples():
        stmt = (
            _dialect_insert(db, Instrument)
            .values(
                exchange=r.exchange,
                segment=r.segment,
                trading_symbol=r.trading_symbol,
                display_name=r.display_name,
                isin=getattr(r, "isin", "") or None,
                tick_size=float(getattr(r, "tick_size", "") or 0.05),
                lot_size=int(getattr(r, "lot_size", "") or 1),
                active_status=True,
            )
            .on_conflict_do_nothing(index_elements=["exchange", "segment", "trading_symbol"])
        )
        n += db.execute(stmt).rowcount
    db.commit()
    return n


def load_constituents_csv(db: Session, path: str | Path) -> int:
    """Columns: trading_symbol,index_name,effective_from[,effective_until,source_document,
    source_published,verification_status,verification_notes] (ISO dates).

    A row overlapping an active record of the same instrument and index is rejected (the
    whole file is rolled back): resolve it with `supersede_membership`, never by deleting."""
    df = pd.read_csv(path, dtype=str).fillna("")
    for r in df.itertuples():
        inst = db.scalar(select(Instrument).where(Instrument.trading_symbol == r.trading_symbol))
        if inst is None:
            raise ValueError(f"unknown instrument {r.trading_symbol!r}")
        until = getattr(r, "effective_until", "")
        published = getattr(r, "source_published", "")
        db.add(
            ConstituentHistory(
                instrument_id=inst.id,
                index_name=r.index_name,
                effective_from=date.fromisoformat(r.effective_from),
                effective_until=date.fromisoformat(until) if until else None,
                source_document=getattr(r, "source_document", "") or None,
                source_published=date.fromisoformat(published) if published else None,
                verification_status=getattr(r, "verification_status", "") or "unverified",
                verification_notes=getattr(r, "verification_notes", "") or None,
            )
        )
    db.flush()
    for index_name in set(df.index_name):
        if conflicts := membership_conflicts(db, index_name):
            a, b = conflicts[0]
            msg = (
                f"{index_name}: overlapping membership for instrument {a.instrument_id} "
                f"({a.effective_from}..{a.effective_until} vs "
                f"{b.effective_from}..{b.effective_until}); supersede the wrong record explicitly"
            )
            db.rollback()
            raise ValueError(msg)
    db.commit()
    return len(df)


def active_membership(index_name: str):
    return (ConstituentHistory.index_name == index_name) & (
        ConstituentHistory.verification_status != "superseded"
    )


def membership_conflicts(
    db: Session, index_name: str
) -> list[tuple[ConstituentHistory, ConstituentHistory]]:
    """Pairs of active records for the same instrument whose date ranges overlap."""
    rows = db.scalars(
        select(ConstituentHistory)
        .where(active_membership(index_name))
        .order_by(ConstituentHistory.instrument_id, ConstituentHistory.effective_from)
    ).all()
    out = []
    for a, b in zip(rows, rows[1:], strict=False):
        if a.instrument_id == b.instrument_id and (
            a.effective_until is None or b.effective_from <= a.effective_until
        ):
            out.append((a, b))
    return out


def supersede_membership(db: Session, record_id: int, note: str) -> ConstituentHistory:
    """Explicitly resolve a conflict: the record stays (audit) but is no longer used."""
    if len(note.strip()) < 10:
        raise ValueError("explain the resolution (which document shows the record is wrong)")
    row = db.get(ConstituentHistory, record_id)
    row.verification_status = "superseded"
    row.verification_notes = "; ".join(x for x in (row.verification_notes, note.strip()) if x)
    db.commit()
    return row


def load_holidays_csv(db: Session, path: str | Path, source: str, exchange: str = "NSE") -> int:
    """Columns: date,description. `source` should cite the official NSE holiday circular."""
    df = pd.read_csv(path, dtype=str)
    for r in df.itertuples():
        db.execute(
            _dialect_insert(db, TradingHoliday)
            .values(
                exchange=exchange,
                holiday_date=date.fromisoformat(r.date),
                description=r.description,
                source=source,
            )
            .on_conflict_do_nothing(index_elements=["exchange", "holiday_date"])
        )
    db.commit()
    return len(df)


def holidays(db: Session, start: date, end: date, exchange: str = "NSE") -> set[date]:
    return set(
        db.scalars(
            select(TradingHoliday.holiday_date).where(
                TradingHoliday.exchange == exchange,
                TradingHoliday.holiday_date >= start,
                TradingHoliday.holiday_date <= end,
            )
        )
    )


def load_candles(
    db: Session,
    dataset_id: int,
    instrument_id: int,
    interval: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    rows = db.execute(
        select(
            Candle.timestamp_utc, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume
        )
        .where(
            Candle.dataset_id == dataset_id,
            Candle.instrument_id == instrument_id,
            Candle.interval == interval,
            Candle.timestamp_utc >= start,
            Candle.timestamp_utc < end,
        )
        .order_by(Candle.timestamp_utc)
    ).all()
    df = pd.DataFrame(rows, columns=CANDLE_COLS)
    # UTCDateTime returns tz-aware UTC; build the column explicitly so empty frames type too
    df["timestamp_utc"] = pd.to_datetime(df.timestamp_utc, utc=True)
    return df


def dataset_fingerprint(db: Session, dataset_id: int) -> str:
    """sha256 over every candle in the dataset, in key order: identifies exact content."""
    h = hashlib.sha256()
    q = (
        select(
            Candle.instrument_id,
            Candle.interval,
            Candle.timestamp_utc,
            Candle.open,
            Candle.high,
            Candle.low,
            Candle.close,
            Candle.volume,
        )
        .where(Candle.dataset_id == dataset_id)
        .order_by(Candle.instrument_id, Candle.interval, Candle.timestamp_utc)
    )
    for row in db.execute(q):
        h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def constituents_on(db: Session, index_name: str, day: date) -> list[Instrument]:
    """Point-in-time index members on `day` (no survivorship bias if history is loaded)."""
    return list(
        db.scalars(
            select(Instrument)
            .join(ConstituentHistory, ConstituentHistory.instrument_id == Instrument.id)
            .where(
                active_membership(index_name),
                ConstituentHistory.effective_from <= day,
                or_(
                    ConstituentHistory.effective_until.is_(None),
                    ConstituentHistory.effective_until >= day,
                ),
            )
            .order_by(Instrument.trading_symbol)
        )
    )

"""Manifest-driven import for ACTUAL market data, with raw-file preservation.

The only way to put data_kind='actual' candles into the database. The manifest declares
provider, permission (licence / terms reference), acquisition date, interval, timezone,
adjustment and the files. Code records the declaration; it cannot verify a licence, so the
person importing is responsible for it. Missing or placeholder provenance is rejected.

Raw files are copied, unmodified, to <raw_store>/<dataset>/<sha256[:16]>_<name> and their
sha256 is stored in datasets.manifest, so any candle can be traced to the exact source bytes.

Timestamps (docs/m3_3_timestamp_conventions.md): the manifest MUST declare
`timestamp_convention` ("start" or "end" of the candle) and a real IANA `source_timezone`.
Internally every candle is keyed by its START in UTC; END-labelled rows are shifted back by
one interval. The provider's original text is kept in candles.source_timestamp. Offsets in
the file that contradict the declared zone, or any row that lands outside a documented
session / off the grid after conversion, refuse the WHOLE manifest (nothing is inserted), so
a wrong declaration cannot store shifted copies of real candles.

Formats (documented in docs/m3_1_data_validation.md):
  niftypilot_csv  columns timestamp,open,high,low,close,volume
  mapped_csv      any CSV; `columns` maps those six names to the provider's headers;
                  optional `timestamp_format` (strftime) for non-ISO timestamps
"""

import hashlib
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from niftypilot.market_data.calendar import load_calendar
from niftypilot.market_data.ingest import INTERVAL_MINUTES, ensure_dataset, ingest_candles
from niftypilot.market_data.session import IST
from niftypilot.market_data.validation import validate_candles
from niftypilot.models import Dataset, IngestionRun, Instrument, InstrumentAlias

PLACEHOLDERS = {"", "unknown", "n/a", "na", "none", "todo", "tbd", "-", "?"}
CANON = ["timestamp", "open", "high", "low", "close", "volume"]


class ManifestFile(BaseModel):
    path: str
    symbol: str


class Manifest(BaseModel):
    dataset: str
    provider: str
    format: Literal["niftypilot_csv", "mapped_csv"]
    permission: str = Field(description="licence / terms reference allowing this use")
    acquisition_date: date
    data_kind: Literal["actual"] = "actual"
    interval: Literal["5m", "1d"]
    source_timezone: str
    timestamp_convention: Literal["start", "end"]  # required: never inferred
    price_adjustment: Literal["unadjusted", "adjusted"]  # 'unknown' is not acceptable here
    adjustment_method: str
    columns: dict[str, str] | None = None
    timestamp_format: str | None = None
    files: list[ManifestFile] = Field(min_length=1)

    @field_validator("provider", "permission", "adjustment_method", "source_timezone")
    @classmethod
    def not_placeholder(cls, v: str) -> str:
        if v.strip().lower() in PLACEHOLDERS or len(v.strip()) < 3:
            raise ValueError(f"provenance field is missing or a placeholder: {v!r}")
        return v.strip()

    @field_validator("source_timezone")
    @classmethod
    def real_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"source_timezone {v!r} is not an IANA zone") from exc
        return v

    @field_validator("acquisition_date")
    @classmethod
    def not_future(cls, v: date) -> date:
        if v > datetime.now(IST).date():
            raise ValueError("acquisition_date is in the future")
        return v

    @model_validator(mode="after")
    def mapping_complete(self):
        if self.format == "mapped_csv":
            missing = set(CANON) - set(self.columns or {})
            if missing:
                raise ValueError(f"mapped_csv needs column mapping for {sorted(missing)}")
        return self


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def preserve_raw(src: Path, raw_store: Path, dataset: str) -> tuple[Path, str]:
    digest = sha256_file(src)
    dest = raw_store / dataset / f"{digest[:16]}_{src.name}"
    if dest.exists():
        if sha256_file(dest) != digest:
            raise ValueError(f"raw store corrupted: {dest} hash mismatch")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        dest.chmod(0o444)  # preserved originals are read-only
    return dest, digest


def read_provider_file(path: Path, m: Manifest) -> pd.DataFrame:
    raw = pd.read_csv(path)
    if m.format == "mapped_csv":
        missing = {v for v in m.columns.values() if v not in raw.columns}
        if missing:
            raise ValueError(f"{path.name}: provider columns {sorted(missing)} not found")
        raw = raw.rename(columns={v: k for k, v in m.columns.items()})
    else:
        raw.columns = [c.strip().lower() for c in raw.columns]
        if set(CANON) - set(raw.columns):
            raise ValueError(
                f"{path.name}: missing columns {sorted(set(CANON) - set(raw.columns))}"
            )
    text = raw["timestamp"].astype(str)
    try:
        ts = pd.to_datetime(text, format=m.timestamp_format, errors="coerce")
    except ValueError as exc:  # e.g. mixed UTC offsets in one column
        raise ValueError(f"{path.name}: timestamps not parseable consistently: {exc}") from exc
    if not pd.api.types.is_datetime64_any_dtype(ts):
        raise ValueError(f"{path.name}: mixed UTC offsets in timestamps")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(m.source_timezone, ambiguous="NaT", nonexistent="NaT")
    else:  # the file states offsets: they must match the declared zone at every instant
        declared = ts.dt.tz_convert(m.source_timezone)
        bad = [
            t
            for t, d in zip(ts, declared, strict=True)
            if pd.notna(t) and t.utcoffset() != d.utcoffset()
        ]
        if bad:
            raise ValueError(
                f"{path.name}: timestamp offset {bad[0].utcoffset()} contradicts "
                f"source_timezone {m.source_timezone!r}"
            )
    minutes = INTERVAL_MINUTES[m.interval]
    if m.timestamp_convention == "end" and minutes:
        ts = ts - timedelta(minutes=minutes)  # canonical key is the candle START
    df = pd.DataFrame({"timestamp_utc": ts.dt.tz_convert("UTC")})
    for c in CANON[1:]:
        df[c] = pd.to_numeric(raw[c], errors="coerce")
    df["source_timestamp"] = text
    return df


SESSION_REJECTS = {"off_grid", "outside_session", "outside_special_session"}


def check_alignment(db: Session, path: Path, df: pd.DataFrame, m: Manifest) -> None:
    """Refuse a file whose converted timestamps fall outside documented sessions."""
    minutes = INTERVAL_MINUTES[m.interval]
    if not minutes:
        return
    days = df.timestamp_utc.dropna().dt.tz_convert(IST).dt.date
    _, issues = validate_candles(df, minutes, calendar=load_calendar(db, days.min(), days.max()))
    bad = [i for i in issues if i.kind in SESSION_REJECTS]
    if bad:
        raise ValueError(
            f"{path.name}: {len(bad)} of {len(df)} rows fall outside documented sessions or off "
            f"the {minutes}m grid after conversion (first: {bad[0].kind}@{bad[0].timestamp}). "
            f"Check timestamp_convention={m.timestamp_convention!r}, "
            f"source_timezone={m.source_timezone!r} and the session calendar. Nothing imported."
        )


def resolve_symbol(db: Session, symbol: str, first: date, last: date) -> Instrument:
    """Map a provider symbol to an instrument, honouring historical renames."""
    inst = db.scalar(select(Instrument).where(Instrument.trading_symbol == symbol))
    aliases = db.scalars(select(InstrumentAlias).where(InstrumentAlias.alias == symbol)).all()
    covering = [
        a
        for a in aliases
        if a.valid_from <= first and (a.valid_until is None or last <= a.valid_until)
    ]
    partial = [
        a
        for a in aliases
        if a not in covering
        and a.valid_from <= last
        and (a.valid_until is None or first <= a.valid_until)
    ]
    if partial:
        raise ValueError(
            f"{symbol}: file {first}..{last} spans a symbol change "
            f"(alias valid {partial[0].valid_from}..{partial[0].valid_until}); "
            "split the file at the change date"
        )
    if inst and covering:
        raise ValueError(f"{symbol}: ambiguous (current symbol and historical alias)")
    if len(covering) > 1:
        raise ValueError(f"{symbol}: ambiguous aliases for {first}..{last}")
    if covering:
        return db.get(Instrument, covering[0].instrument_id)
    if inst is None:
        raise ValueError(f"{symbol}: unknown instrument/alias; load instruments and aliases")
    return inst


def load_aliases_csv(db: Session, path: str | Path) -> int:
    """Columns: alias,trading_symbol,valid_from[,valid_until],source."""
    df = pd.read_csv(path, dtype=str).fillna("")
    for r in df.itertuples():
        inst = db.scalar(select(Instrument).where(Instrument.trading_symbol == r.trading_symbol))
        if inst is None:
            raise ValueError(f"unknown instrument {r.trading_symbol!r}")
        db.add(
            InstrumentAlias(
                instrument_id=inst.id,
                alias=r.alias,
                valid_from=date.fromisoformat(r.valid_from),
                valid_until=date.fromisoformat(r.valid_until) if r.valid_until else None,
                source=r.source,
            )
        )
    db.commit()
    return len(df)


def import_manifest(
    db: Session, manifest_path: str | Path, raw_store: str | Path
) -> tuple[Dataset, list[IngestionRun]]:
    mpath = Path(manifest_path)
    m = Manifest.model_validate(json.loads(mpath.read_text()))
    ds = ensure_dataset(
        db,
        m.dataset,
        provider=m.provider,
        source=f"{m.provider} ({m.format})",
        data_kind="actual",
        source_timezone=m.source_timezone,
        price_adjustment=m.price_adjustment,
    )
    if ds.provider != m.provider:
        raise ValueError(f"dataset {ds.name!r} belongs to provider {ds.provider!r}")
    prev_m = (ds.manifest or {}).get("manifest", {})
    for k in ("source_timezone", "timestamp_convention", "interval", "price_adjustment"):
        if prev_m and prev_m.get(k) != getattr(m, k):
            raise ValueError(
                f"dataset {ds.name!r} was imported with {k}={prev_m.get(k)!r}; this manifest "
                f"says {getattr(m, k)!r}. One dataset has one convention: use a new dataset."
            )
    files, frames = [], []
    for f in m.files:  # validate and preserve everything before inserting anything
        src = (mpath.parent / f.path).resolve()
        if not src.is_file():
            raise ValueError(f"manifest file not found: {src}")
        df = read_provider_file(src, m)
        days = df.timestamp_utc.dropna().dt.tz_convert(IST).dt.date
        if days.empty:
            raise ValueError(f"{f.path}: no parseable timestamps")
        inst = resolve_symbol(db, f.symbol, days.min(), days.max())
        if inst.exchange == "SYNTH":
            raise ValueError(f"{f.symbol}: synthetic instrument cannot be in an actual dataset")
        check_alignment(db, src, df, m)
        dest, digest = preserve_raw(src, Path(raw_store), m.dataset)
        files.append(
            {
                "original": str(src),
                "stored": str(dest),
                "sha256": digest,
                "bytes": src.stat().st_size,
                "symbol": f.symbol,
                "instrument": inst.trading_symbol,
                "timestamp_convention": m.timestamp_convention,
                "source_timezone": m.source_timezone,
                "first": str(days.min()),
                "last": str(days.max()),
                "rows": len(df),
            }
        )
        frames.append((inst, df, digest))
    prev = (ds.manifest or {}).get("files", [])
    known = {x["sha256"] for x in prev}
    ds.manifest = {
        "manifest": m.model_dump(mode="json", exclude={"files"}),
        "files": prev + [x for x in files if x["sha256"] not in known],
    }
    ds.acquisition_date, ds.permission = m.acquisition_date, m.permission
    ds.interval, ds.adjustment_method = m.interval, m.adjustment_method
    firsts = [date.fromisoformat(x["first"]) for x in ds.manifest["files"]]
    lasts = [date.fromisoformat(x["last"]) for x in ds.manifest["files"]]
    ds.coverage_start, ds.coverage_end = min(firsts), max(lasts)
    ds.provenance_status = "declared"
    db.commit()
    runs = [
        ingest_candles(db, ds, inst.id, df, m.interval, f"{m.provider}:sha256:{digest[:16]}")
        for inst, df, digest in frames
    ]
    ds.quality_status = "unchecked"  # new data invalidates any earlier quality verdict
    db.commit()
    return ds, runs

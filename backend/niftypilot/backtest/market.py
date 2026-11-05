"""In-memory market for one backtest: a single dataset, never mixed with another."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from niftypilot.market_data.calendar import load_calendar
from niftypilot.market_data.corporate_actions import Action, actions_by_symbol, adjust_history
from niftypilot.market_data.ingest import dataset_fingerprint, load_candles
from niftypilot.market_data.quality import utc_range
from niftypilot.market_data.session import IST
from niftypilot.models import Candle, ConstituentHistory, Dataset, Instrument


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    segment: str
    tick_size: float
    restricted: bool = False


@dataclass
class MarketData:
    dataset: dict  # id, name, provider, source, data_kind, price_adjustment, fingerprint
    candles: dict[str, pd.DataFrame]  # 5m per symbol, incl. lookback before start
    info: dict[str, InstrumentInfo]
    holidays: set[date] = field(default_factory=set)
    membership: Callable[[date], set[str]] | None = None  # point-in-time universe
    benchmark: str | None = None  # symbol of the index series inside `candles`
    quality: dict | None = None
    # days with a documented non-regular session: never traded, never used as history
    special_sessions: set[date] = field(default_factory=set)
    actions: dict[str, list[Action]] = field(default_factory=dict)
    vendor_adjusted: bool = False
    by_day: dict[str, dict[date, pd.DataFrame]] = field(init=False)
    _bars: dict = field(init=False, default_factory=dict)
    # (symbol, day, strategy+regime params) -> StrategyResult; capital/window independent
    signal_cache: dict = field(init=False, default_factory=dict)

    def __post_init__(self):
        self.by_day = {}
        for sym, df in self.candles.items():
            df = df.sort_values("timestamp_utc").reset_index(drop=True)
            days = df.timestamp_utc.dt.tz_convert(IST).dt.date
            self.by_day[sym] = {
                d: g.reset_index(drop=True)
                for d, g in df.groupby(days)
                if d not in self.special_sessions
            }

    def history(self, symbol: str, today: date, sessions: int) -> pd.DataFrame:
        """The last `sessions` regular sessions before `today`, corporate-action adjusted."""
        per = self.by_day[symbol]
        days = [d for d in per if d < today][-sessions:]
        hist = (
            pd.concat([per[d] for d in days], ignore_index=True) if days else per[today].iloc[0:0]
        )
        acts = self.actions.get(symbol)
        return adjust_history(hist, acts, today, self.vendor_adjusted) if acts else hist

    def traded_value(self) -> dict[str, pd.Series]:
        """Per-session traded value (sum close x volume) for non-index instruments."""
        if "_tv" not in self.signal_cache:
            self.signal_cache["_tv"] = {
                s: pd.Series({d: float((g.close * g.volume).sum()) for d, g in per.items()})
                for s, per in self.by_day.items()
                if self.info[s].segment != "INDEX"
            }
        return self.signal_cache["_tv"]

    def bar(self, symbol: str, day: date, slot):
        """The candle starting at `slot` (UTC datetime), or None if absent."""
        key = (symbol, day)
        if key not in self._bars:
            df = self.by_day[symbol].get(day)
            self._bars[key] = (
                {} if df is None else {r.timestamp_utc.to_pydatetime(): r for r in df.itertuples()}
            )
        return self._bars[key].get(slot)


def load_market(
    db: Session,
    dataset: Dataset,
    start: date,
    end: date,
    index_name: str | None = None,
    benchmark_symbol: str | None = None,
    lookback_days: int = 60,
) -> MarketData:
    """Load one dataset. Refuses datasets whose quality check failed or does not cover range."""
    q = dataset.quality_report or {}
    if dataset.data_kind == "actual" and dataset.provenance_status != "declared":
        raise ValueError(f"dataset {dataset.name!r}: actual data without declared provenance")
    if dataset.quality_status not in ("passed", "warnings"):
        raise ValueError(
            f"dataset {dataset.name!r} quality is {dataset.quality_status!r}; "
            "run the quality check and fix critical findings first"
        )
    if not (q.get("start", "9999") <= str(start) and q.get("end", "0000") >= str(end)):
        raise ValueError(
            f"quality report covers {q.get('start')}..{q.get('end')}, not {start}..{end}"
        )
    a, b = utc_range(start - timedelta(days=lookback_days), end)
    ids = db.scalars(
        select(Candle.instrument_id)
        .distinct()
        .where(Candle.dataset_id == dataset.id, Candle.interval == "5m")
    ).all()
    candles, info = {}, {}
    for iid in ids:
        inst = db.get(Instrument, iid)
        candles[inst.trading_symbol] = load_candles(db, dataset.id, iid, "5m", a, b)
        info[inst.trading_symbol] = InstrumentInfo(
            inst.trading_symbol,
            inst.segment,
            float(inst.tick_size),
            bool(inst.restrictions) or not inst.active_status,
        )
    membership = None
    if index_name:
        rows = db.execute(
            select(
                Instrument.trading_symbol,
                ConstituentHistory.effective_from,
                ConstituentHistory.effective_until,
            )
            .join(Instrument, Instrument.id == ConstituentHistory.instrument_id)
            .where(
                ConstituentHistory.index_name == index_name,
                ConstituentHistory.verification_status != "superseded",
            )
        ).all()

        def membership(d: date) -> set[str]:
            return {s for s, f, u in rows if f <= d and (u is None or d <= u)}

    meta = dict(
        id=dataset.id,
        name=dataset.name,
        provider=dataset.provider,
        source=dataset.source,
        data_kind=dataset.data_kind,
        price_adjustment=dataset.price_adjustment,
        source_timezone=dataset.source_timezone,
        fingerprint=dataset_fingerprint(db, dataset.id),
        provenance={
            "status": dataset.provenance_status,
            "permission": dataset.permission,
            "acquisition_date": str(dataset.acquisition_date or ""),
            "interval": dataset.interval,
            "adjustment_method": dataset.adjustment_method,
            "coverage": [str(dataset.coverage_start or ""), str(dataset.coverage_end or "")],
            "raw_files": [
                {k: f[k] for k in ("stored", "sha256", "symbol")}
                for f in (dataset.manifest or {}).get("files", [])
            ],
        },
        index_name=index_name,
    )
    cal = load_calendar(db, start - timedelta(days=lookback_days), end)
    meta["excluded_special_sessions"] = sorted(str(d) for d in cal.special)
    return MarketData(
        meta,
        candles,
        info,
        {d for d in cal.holidays if start <= d <= end},
        membership,
        benchmark_symbol,
        q,
        special_sessions=set(cal.special),
        actions=actions_by_symbol(db),
        vendor_adjusted=dataset.price_adjustment == "adjusted",
    )

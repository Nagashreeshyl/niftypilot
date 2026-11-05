"""Database schema: M1 market data, M2 strategy versions/signals, M3 datasets/backtests.

Later milestones add orders, trades, risk_events, approvals.
"""

from datetime import UTC, date, datetime, time

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from niftypilot.db import Base, UTCDateTime

DATA_KINDS = ("actual", "synthetic", "simulated")
PRICE_ADJUSTMENTS = ("unadjusted", "adjusted", "unknown")
VERIFICATION = ("unverified", "verified")
MEMBERSHIP_STATUSES = (*VERIFICATION, "superseded")  # superseded = explicitly resolved conflict
SESSION_TYPES = ("muhurat", "special", "extended")
CA_TYPES = ("split", "bonus", "symbol_change", "merger", "demerger")
# adjust_at_read: pre-ex history x factor at read time; exclude_prior_history: no reliable
# factor, so history before the ex-date is never compared; no_price_effect: identity only
CA_POLICIES = ("adjust_at_read", "exclude_prior_history", "no_price_effect")


def _now() -> datetime:
    return datetime.now(UTC)


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("exchange", "segment", "trading_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16))
    segment: Mapped[str] = mapped_column(String(16))
    trading_symbol: Mapped[str] = mapped_column(String(64))
    provider_symbol: Mapped[str | None] = mapped_column(String(128))
    isin: Mapped[str | None] = mapped_column(String(12))
    display_name: Mapped[str] = mapped_column(String(256))
    tick_size: Mapped[float] = mapped_column(Numeric(10, 4), default=0.05)
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    restrictions: Mapped[str | None] = mapped_column(Text)  # e.g. ASM/GSM/suspended; None = none
    active_status: Mapped[bool] = mapped_column(Boolean, default=True)


class ConstituentHistory(Base):
    """Point-in-time index membership. effective_until NULL = still a member.

    Rows are never deleted: a wrong row is marked 'superseded' (with notes) and ignored."""

    __tablename__ = "constituent_history"
    __table_args__ = (
        CheckConstraint(
            "effective_until IS NULL OR effective_until >= effective_from",
            name="ck_constituent_dates",
        ),
        CheckConstraint(
            f"verification_status IN {MEMBERSHIP_STATUSES}", name="ck_constituent_verification"
        ),
        Index("ix_constituent_index_dates", "index_name", "effective_from", "effective_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    index_name: Mapped[str] = mapped_column(String(64))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_until: Mapped[date | None] = mapped_column(Date)
    source_document: Mapped[str | None] = mapped_column(Text)  # NSE Indices press release ref
    source_published: Mapped[date | None] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(
        String(16), default="unverified", server_default="unverified"
    )
    verification_notes: Mapped[str | None] = mapped_column(Text)


class Dataset(Base):
    """Provenance unit. Every candle belongs to exactly one dataset; a backtest uses one."""

    __tablename__ = "datasets"
    __table_args__ = (
        CheckConstraint(f"data_kind IN {DATA_KINDS}", name="ck_dataset_data_kind"),
        CheckConstraint(f"price_adjustment IN {PRICE_ADJUSTMENTS}", name="ck_dataset_adj"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider: Mapped[str] = mapped_column(String(64))  # csv, groww, synthetic, ...
    source: Mapped[str] = mapped_column(Text)  # vendor / file / URL / licence note
    data_kind: Mapped[str] = mapped_column(String(16))
    source_timezone: Mapped[str] = mapped_column(String(64))  # tz of the raw timestamps
    price_adjustment: Mapped[str] = mapped_column(String(16), default="unknown")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    quality_status: Mapped[str] = mapped_column(String(16), default="unchecked")
    quality_report: Mapped[dict | None] = mapped_column(JSON)
    quality_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Provenance (M3.1). Declared by the person importing; code records, it cannot verify a
    # licence. Actual datasets without it are refused for import and backtesting.
    acquisition_date: Mapped[date | None] = mapped_column(Date)
    permission: Mapped[str | None] = mapped_column(Text)  # licence / terms reference
    interval: Mapped[str | None] = mapped_column(String(8))
    adjustment_method: Mapped[str | None] = mapped_column(Text)
    coverage_start: Mapped[date | None] = mapped_column(Date)
    coverage_end: Mapped[date | None] = mapped_column(Date)
    manifest: Mapped[dict | None] = mapped_column(JSON)  # raw files with sha256
    provenance_status: Mapped[str] = mapped_column(
        String(16), default="missing", server_default="missing"
    )  # missing | declared


class InstrumentAlias(Base):
    """Historical symbols (renames). A provider file named with an alias maps to the
    instrument only for dates inside [valid_from, valid_until]."""

    __tablename__ = "instrument_aliases"
    __table_args__ = (UniqueConstraint("alias", "valid_from"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    alias: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text)


class TradingHoliday(Base):
    __tablename__ = "trading_holidays"
    __table_args__ = (UniqueConstraint("exchange", "holiday_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16))
    holiday_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(Text)


class ExchangeSession(Base):
    """A documented non-regular session: Muhurat, special (e.g. weekend live session) or
    extended (regular day with a changed window). Times are Asia/Kolkata wall-clock.
    A date may hold several windows. Candles are accepted on such a day only inside them."""

    __tablename__ = "exchange_sessions"
    __table_args__ = (
        UniqueConstraint("exchange", "session_date", "open_time", name="uq_exchange_session"),
        CheckConstraint(f"session_type IN {SESSION_TYPES}", name="ck_session_type"),
        CheckConstraint(f"verification_status IN {VERIFICATION}", name="ck_session_verification"),
        CheckConstraint("close_time > open_time", name="ck_session_window"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16))
    session_date: Mapped[date] = mapped_column(Date)
    session_type: Mapped[str] = mapped_column(String(16))
    open_time: Mapped[time] = mapped_column(Time)
    close_time: Mapped[time] = mapped_column(Time)
    source: Mapped[str] = mapped_column(Text)  # NSE circular reference
    verification_status: Mapped[str] = mapped_column(String(16), default="unverified")
    notes: Mapped[str | None] = mapped_column(Text)


class CorporateAction(Base):
    """Raw candles are never modified; history is adjusted at read time per `data_policy`.
    adjustment_factor = new shares per old share (1:5 split -> 5; 1:1 bonus -> 2)."""

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "action_type", "ex_date", name="uq_corporate_action"),
        CheckConstraint(f"action_type IN {CA_TYPES}", name="ck_ca_type"),
        CheckConstraint(f"verification_status IN {VERIFICATION}", name="ck_ca_verification"),
        CheckConstraint(f"data_policy IN {CA_POLICIES}", name="ck_ca_policy"),
        CheckConstraint("adjustment_factor IS NULL OR adjustment_factor > 0", name="ck_ca_factor"),
        CheckConstraint(
            "action_type NOT IN ('split', 'bonus') OR adjustment_factor IS NOT NULL",
            name="ck_ca_factor_required",
        ),
        CheckConstraint(
            "data_policy != 'adjust_at_read' OR adjustment_factor IS NOT NULL",
            name="ck_ca_policy_factor",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    action_type: Mapped[str] = mapped_column(String(16))
    announcement_date: Mapped[date] = mapped_column(Date)
    ex_date: Mapped[date] = mapped_column(Date)
    record_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date] = mapped_column(Date)
    adjustment_factor: Mapped[float | None] = mapped_column(Numeric(18, 8))
    source: Mapped[str] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(String(16), default="unverified")
    data_policy: Mapped[str] = mapped_column(String(24))
    notes: Mapped[str | None] = mapped_column(Text)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "instrument_id", "interval", "timestamp_utc", name="uq_candle"
        ),
        CheckConstraint(f"data_kind IN {DATA_KINDS}", name="ck_candle_data_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", name="fk_candles_dataset"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    interval: Mapped[str] = mapped_column(String(8))  # "5m", "15m", "1d"
    timestamp_utc: Mapped[datetime] = mapped_column(UTCDateTime)  # candle START time
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(128))
    data_kind: Mapped[str] = mapped_column(String(16))
    # provider's original timestamp text (before timezone/convention conversion); audit only
    source_timestamp: Mapped[str | None] = mapped_column(String(64))


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey("datasets.id", name="fk_ingestion_dataset")
    )
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    interval: Mapped[str] = mapped_column(String(8))
    requested_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    requested_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16))  # success / partial / failed
    rows_received: Mapped[int] = mapped_column(Integer, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    parameters: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    status: Mapped[str] = mapped_column(String(16), default="research")
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_versions.id"))


class Signal(Base):
    __tablename__ = "signals"
    # One outcome of each type per instrument/strategy/session: blocks duplicate BUY_SETUPs.
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "strategy_version_id", "session_date", "signal_type", name="uq_signal"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    session_date: Mapped[date] = mapped_column(Date)
    signal_type: Mapped[str] = mapped_column(String(32))
    timestamp_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    entry: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float)
    quantity: Mapped[int | None] = mapped_column(Integer)
    explanation: Mapped[str] = mapped_column(Text)
    rules: Mapped[list] = mapped_column(JSON)
    data_kind: Mapped[str] = mapped_column(String(16))


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_key: Mapped[str] = mapped_column(String(64), unique=True)  # hash of all inputs
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    dataset_fingerprint: Mapped[str] = mapped_column(String(64))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    capital: Mapped[float] = mapped_column(Numeric(14, 2))
    config: Mapped[dict] = mapped_column(JSON)
    net_profit: Mapped[float | None] = mapped_column(Float)
    profit_factor: Mapped[float | None] = mapped_column(Float)
    expectancy: Mapped[float | None] = mapped_column(Float)
    maximum_drawdown: Mapped[float | None] = mapped_column(Float)
    win_rate: Mapped[float | None] = mapped_column(Float)
    total_trades: Mapped[int] = mapped_column(Integer)
    fees_authoritative: Mapped[bool] = mapped_column(Boolean)
    report_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Experiment(Base):
    """A research run over a fixed split. The OOS segment may be evaluated once."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(64), unique=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    parameter_set: Mapped[dict] = mapped_column(JSON)  # variants x capitals tested
    training_period: Mapped[dict] = mapped_column(JSON)
    validation_period: Mapped[dict] = mapped_column(JSON)
    out_of_sample_period: Mapped[dict] = mapped_column(JSON)
    n_trials: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))  # running / dev_done / oos_done
    oos_evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    # True once anything other than the single final evaluation touched the OOS period
    contaminated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class OosAccessLog(Base):
    """Every access to a sealed out-of-sample period, contaminating or not. Append-only."""

    __tablename__ = "oos_access_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"))
    accessed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    purpose: Mapped[str] = mapped_column(String(32))  # final_evaluation / backtest / research
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    contaminating: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text)

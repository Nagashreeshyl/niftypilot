"""Read-only REST API (M1-M3). No order, broker or live-trading endpoints exist.

Run: uv run uvicorn niftypilot.api:app --reload   (docs at /docs)
"""

from collections.abc import Iterator
from datetime import date, datetime
from typing import Generic, TypeVar

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from niftypilot.config import Settings
from niftypilot.db import make_engine, make_session_factory
from niftypilot.models import (
    BacktestRun,
    Candle,
    Dataset,
    Experiment,
    Instrument,
    Signal,
    StrategyVersion,
)

T = TypeVar("T")


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InstrumentOut(Out):
    id: int
    exchange: str
    segment: str
    trading_symbol: str
    display_name: str
    tick_size: float
    active_status: bool
    restrictions: str | None


class CandleOut(Out):
    timestamp_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str
    data_kind: str


class StrategyOut(Out):
    id: int
    strategy_id: str
    version: str
    parameters: dict
    status: str
    created_at: datetime


class SignalOut(Out):
    id: int
    instrument_id: int
    strategy_version_id: int
    session_date: date
    signal_type: str
    timestamp_utc: datetime
    entry: float | None
    stop_loss: float | None
    target: float | None
    quantity: int | None
    explanation: str
    rules: list
    data_kind: str


class DatasetOut(Out):
    id: int
    name: str
    provider: str
    source: str
    data_kind: str
    source_timezone: str
    price_adjustment: str
    quality_status: str
    quality_report: dict | None


class BacktestOut(Out):
    id: int
    run_key: str
    strategy_version_id: int
    dataset_id: int
    dataset_fingerprint: str
    start_date: date
    end_date: date
    capital: float
    net_profit: float | None
    profit_factor: float | None
    expectancy: float | None
    maximum_drawdown: float | None
    win_rate: float | None
    total_trades: int
    fees_authoritative: bool
    report_path: str


class ExperimentOut(Out):
    id: int
    experiment_id: str
    dataset_id: int
    training_period: dict
    validation_period: dict
    out_of_sample_period: dict
    n_trials: int
    status: str
    oos_evaluated_at: datetime | None
    metrics: dict | None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    app = FastAPI(title="NiftyPilot", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    def db() -> Iterator[Session]:
        with factory() as s:
            yield s

    def page(s: Session, stmt, limit: int, offset: int, out):
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.limit(limit).offset(offset)).all()
        return {
            "items": [out.model_validate(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/health")
    def health(s: Session = Depends(db)):
        try:
            s.execute(text("SELECT 1"))
            database = "ok"
        except Exception as exc:  # report, don't crash the health check
            database = f"error: {type(exc).__name__}"
        return {
            "status": "ok" if database == "ok" else "degraded",
            "trading_mode": settings.trading_mode,
            "live_trading_enabled": False,
            "database": database,
        }

    @app.get("/api/v1/instruments", response_model=Page[InstrumentOut])
    def instruments(
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        s: Session = Depends(db),
    ):
        return page(s, select(Instrument).order_by(Instrument.id), limit, offset, InstrumentOut)

    @app.get("/api/v1/candles", response_model=Page[CandleOut])
    def candles(
        dataset_id: int,
        instrument_id: int,
        start: AwareDatetime,
        end: AwareDatetime,
        interval: str = Query("5m", pattern="^(5m|15m|1d)$"),
        limit: int = Query(1000, ge=1, le=5000),
        offset: int = Query(0, ge=0),
        s: Session = Depends(db),
    ):
        if end <= start:
            raise HTTPException(422, "end must be after start")
        stmt = (
            select(Candle)
            .where(
                Candle.dataset_id == dataset_id,
                Candle.instrument_id == instrument_id,
                Candle.interval == interval,
                Candle.timestamp_utc >= start,
                Candle.timestamp_utc < end,
            )
            .order_by(Candle.timestamp_utc)
        )
        return page(s, stmt, limit, offset, CandleOut)

    @app.get("/api/v1/strategies", response_model=Page[StrategyOut])
    def strategies(
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        s: Session = Depends(db),
    ):
        return page(
            s, select(StrategyVersion).order_by(StrategyVersion.id), limit, offset, StrategyOut
        )

    @app.get("/api/v1/datasets", response_model=Page[DatasetOut])
    def datasets(
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        s: Session = Depends(db),
    ):
        return page(s, select(Dataset).order_by(Dataset.id), limit, offset, DatasetOut)

    @app.get("/api/v1/backtests", response_model=Page[BacktestOut])
    def backtests(
        dataset_id: int | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        s: Session = Depends(db),
    ):
        stmt = select(BacktestRun).order_by(BacktestRun.id.desc())
        if dataset_id is not None:
            stmt = stmt.where(BacktestRun.dataset_id == dataset_id)
        return page(s, stmt, limit, offset, BacktestOut)

    @app.get("/api/v1/experiments", response_model=Page[ExperimentOut])
    def experiments(
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        s: Session = Depends(db),
    ):
        return page(
            s, select(Experiment).order_by(Experiment.id.desc()), limit, offset, ExperimentOut
        )

    @app.get("/api/v1/signals", response_model=Page[SignalOut])
    def signals(
        instrument_id: int | None = None,
        session_date: date | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        s: Session = Depends(db),
    ):
        stmt = select(Signal).order_by(Signal.timestamp_utc.desc(), Signal.id.desc())
        if instrument_id is not None:
            stmt = stmt.where(Signal.instrument_id == instrument_id)
        if session_date is not None:
            stmt = stmt.where(Signal.session_date == session_date)
        return page(s, stmt, limit, offset, SignalOut)

    return app


def __getattr__(name: str):  # lazy `app` so importing this module never touches a DB
    if name == "app":
        return create_app()
    raise AttributeError(name)

"""Replay SYNTHETIC scenarios candle-by-candle and print each signal transition.

uv run python -m niftypilot.demo [--capital 5000] [--persist]
--persist writes synthetic candles/signals (data_kind='synthetic') to the configured DB.
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from niftypilot import synthetic
from niftypilot.config import Settings
from niftypilot.db import make_engine, make_session_factory
from niftypilot.market_data.ingest import ensure_dataset, ingest_candles
from niftypilot.market_data.session import IST
from niftypilot.models import Instrument
from niftypilot.risk.engine import AccountState, RiskConfig
from niftypilot.signals import apply_risk, get_or_create_strategy_version, persist
from niftypilot.strategy.orb_vwap import OrbParams, evaluate
from niftypilot.strategy.regime import Regime

DAY = date(2026, 1, 15)  # arbitrary weekday; the data is synthetic


def run(capital: Decimal, persist_db: bool) -> None:
    params, cfg = OrbParams(), RiskConfig(capital=capital)
    state = AccountState(available_cash=capital)
    history = synthetic.history_sessions(DAY)
    factory = make_session_factory(make_engine(Settings().database_url)) if persist_db else None
    print(f"SYNTHETIC DATA DEMO - capital Rs {capital}, risk {cfg.risk_pct}%/trade, paper only\n")

    for name, bars in synthetic.SCENARIOS.items():
        today = synthetic.session(DAY, bars)
        print(f"== {name} ==")
        last = None
        for i in range(len(today)):
            as_of = (today.timestamp_utc.iloc[i] + timedelta(minutes=5)).to_pydatetime()
            d = apply_risk(evaluate(today, history, as_of, Regime.SIDEWAYS, params), state, cfg)
            if d.outcome != last:
                print(
                    f"  {as_of.astimezone(IST):%H:%M} IST {d.outcome:<18} {d.strategy.explanation}"
                )
                last = d.outcome
        failed = [r.rule for r in d.strategy.rules if not r.passed]
        print(f"  final: {d.outcome} - {d.strategy.explanation}")
        print(f"  failed rules: {failed or 'none'}  research flags: {d.strategy.research_flags}\n")
        if factory:
            with factory() as db:
                inst = _instrument(db, f"SYNTH-{name.upper()}")
                ds = ensure_dataset(
                    db,
                    "synthetic-demo",
                    provider="synthetic",
                    source="niftypilot.synthetic (demo)",
                    data_kind="synthetic",
                    source_timezone="UTC",
                    price_adjustment="unadjusted",
                )
                for df, src in [(history, "synthetic.history"), (today, f"synthetic.{name}")]:
                    run = ingest_candles(db, ds, inst.id, df, "5m", src)
                    if run.status == "failed":
                        raise RuntimeError(f"ingestion failed: {run.errors}")
                persist(db, inst.id, get_or_create_strategy_version(db, params), d, "synthetic")
                db.commit()


def _instrument(db, symbol: str) -> Instrument:
    inst = db.scalar(select(Instrument).where(Instrument.trading_symbol == symbol))
    if inst is None:
        inst = Instrument(
            exchange="SYNTH",
            segment="CASH",
            trading_symbol=symbol,
            display_name=f"{symbol} (synthetic test instrument)",
        )
        db.add(inst)
        db.flush()
    return inst


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=Decimal, default=Decimal("5000"))
    ap.add_argument("--persist", action="store_true")
    a = ap.parse_args()
    run(a.capital, a.persist)

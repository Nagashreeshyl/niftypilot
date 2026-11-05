"""Signal pipeline: strategy proposes, risk engine disposes. Produces records, never orders."""

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from niftypilot.market_data.session import session_date
from niftypilot.models import Signal, StrategyVersion
from niftypilot.risk.engine import AccountState, RiskConfig, RiskDecision, evaluate_trade
from niftypilot.strategy.orb_vwap import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    OrbParams,
    Outcome,
    RuleCheck,
    StrategyResult,
)


@dataclass
class SignalDecision:
    strategy: StrategyResult
    risk: RiskDecision | None
    quantity: int | None = None

    @property
    def outcome(self) -> Outcome:
        return self.strategy.outcome


def apply_risk(res: StrategyResult, state: AccountState, cfg: RiskConfig) -> SignalDecision:
    if res.outcome != Outcome.BUY_SETUP:
        return SignalDecision(res, None)
    risk = evaluate_trade(res.entry, res.stop_loss, res.target, state, cfg)
    # risk checks become part of the signal's rule trail
    res.rules.extend(
        RuleCheck(f"risk.{c['check']}", c["passed"], detail=c["detail"]) for c in risk.checks
    )
    if not risk.approved:
        res.outcome = Outcome.RISK_REJECTED
        failed = "; ".join(f"{c['check']} ({c['detail']})" for c in risk.checks if not c["passed"])
        res.explanation = res.explanation.replace(
            "Pending risk-engine approval.", f"Risk engine rejected: {failed}."
        )
        return SignalDecision(res, risk)
    res.explanation = res.explanation.replace(
        "Pending risk-engine approval.",
        f"Risk approved: qty {risk.quantity}, est. net loss at stop Rs {risk.net_loss_at_stop}, "
        f"est. net profit at target Rs {risk.net_profit_at_target}.",
    )
    return SignalDecision(res, risk, risk.quantity)


def strategy_version_label(params: OrbParams) -> str:
    """Content-addressed, so a version string can never refer to two parameter sets."""
    blob = json.dumps(params.model_dump(mode="json"), sort_keys=True).encode()
    return f"{STRATEGY_VERSION}+{hashlib.sha256(blob).hexdigest()[:10]}"


def get_or_create_strategy_version(
    db: Session, params: OrbParams, parent: StrategyVersion | None = None
) -> StrategyVersion:
    label = strategy_version_label(params)
    sv = db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == STRATEGY_ID, StrategyVersion.version == label
        )
    )
    if sv is None:
        sv = StrategyVersion(
            strategy_id=STRATEGY_ID,
            version=label,
            parameters=params.model_dump(mode="json"),
            status="research",
            parent_version_id=parent.id if parent else None,
        )
        db.add(sv)
        db.flush()
    elif sv.parameters != params.model_dump(mode="json"):  # hash collision or tampering
        raise ValueError(f"stored parameters for {label} differ; refusing to reuse")
    return sv


def persist(
    db: Session, instrument_id: int, sv: StrategyVersion, d: SignalDecision, data_kind: str
) -> bool:
    """Insert once per (instrument, version, session, type). Returns False if duplicate."""
    r = d.strategy
    ts = r.signal_time or r.as_of
    row = dict(
        instrument_id=instrument_id,
        strategy_version_id=sv.id,
        session_date=session_date(ts),
        signal_type=r.outcome.value,
        timestamp_utc=ts,
        entry=r.entry,
        stop_loss=r.stop_loss,
        target=r.target,
        quantity=d.quantity,
        explanation=r.explanation,
        rules=r.rules_json(),
        data_kind=data_kind,
    )
    dialect = {"sqlite": sqlite, "postgresql": postgresql}[db.bind.dialect.name]
    stmt = (
        dialect.insert(Signal)
        .values(**row)
        .on_conflict_do_nothing(
            index_elements=["instrument_id", "strategy_version_id", "session_date", "signal_type"]
        )
    )
    return db.execute(stmt).rowcount == 1

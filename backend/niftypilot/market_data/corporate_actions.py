"""Corporate actions: recorded with sources, applied to HISTORY at read time, never stored.

Raw candles stay as traded. When a session is compared with earlier sessions (relative
volume lookback), earlier candles are made comparable:
  adjust_at_read         pre-ex prices / factor, pre-ex volume x factor (split, bonus, or a
                         merger/demerger with a documented factor)
  exclude_prior_history  no reliable factor: sessions before the ex-date are dropped from the
                         comparison (fewer samples -> relative volume may be NaN -> no trade)
  no_price_effect        symbol change; identity is handled by instrument_aliases
Datasets declared `adjusted` by the vendor are not adjusted again (only exclusions apply).
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from niftypilot.market_data.calendar import PLACEHOLDERS
from niftypilot.market_data.session import IST
from niftypilot.models import CA_TYPES, VERIFICATION, CorporateAction, Instrument

PRICE_COLS = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class Action:
    action_type: str
    ex_date: date
    factor: float | None
    policy: str
    verification: str


def default_policy(action_type: str, factor: float | None) -> str:
    if action_type == "symbol_change":
        return "no_price_effect"
    return "adjust_at_read" if factor is not None else "exclude_prior_history"


def load_corporate_actions_csv(db: Session, path: str | Path) -> int:
    """Columns: trading_symbol,action_type,announcement_date,ex_date,effective_date,source
    [,record_date,adjustment_factor,verification_status,notes].

    Split/bonus rows must carry a factor; a factor is never derived or guessed here."""
    df = pd.read_csv(path, dtype=str).fillna("")
    rows = []
    for r in df.itertuples():
        kind = r.action_type.strip().lower()
        if kind not in CA_TYPES:
            raise ValueError(f"row {r.Index}: action_type {r.action_type!r} not in {CA_TYPES}")
        if r.source.strip().lower() in PLACEHOLDERS:
            raise ValueError(f"row {r.Index}: a corporate action needs a source document")
        raw_f = getattr(r, "adjustment_factor", "").strip()
        factor = float(raw_f) if raw_f else None
        if kind in ("split", "bonus") and not factor:
            raise ValueError(f"row {r.Index}: {kind} needs the documented adjustment_factor")
        if factor is not None and factor <= 0:
            raise ValueError(f"row {r.Index}: adjustment_factor must be > 0")
        status = (getattr(r, "verification_status", "") or "unverified").strip().lower()
        if status not in VERIFICATION:
            raise ValueError(f"row {r.Index}: verification_status {status!r} not in {VERIFICATION}")
        inst = db.scalar(select(Instrument).where(Instrument.trading_symbol == r.trading_symbol))
        if inst is None:
            raise ValueError(f"unknown instrument {r.trading_symbol!r}")
        rec = getattr(r, "record_date", "")
        rows.append(
            CorporateAction(
                instrument_id=inst.id,
                action_type=kind,
                announcement_date=date.fromisoformat(r.announcement_date),
                ex_date=date.fromisoformat(r.ex_date),
                record_date=date.fromisoformat(rec) if rec else None,
                effective_date=date.fromisoformat(r.effective_date),
                adjustment_factor=factor,
                source=r.source.strip(),
                verification_status=status,
                data_policy=default_policy(kind, factor),
                notes=getattr(r, "notes", "") or None,
            )
        )
    db.add_all(rows)
    db.commit()
    return len(rows)


def actions_by_symbol(db: Session) -> dict[str, list[Action]]:
    out: dict[str, list[Action]] = {}
    q = (
        select(CorporateAction, Instrument.trading_symbol)
        .join(Instrument, Instrument.id == CorporateAction.instrument_id)
        .order_by(CorporateAction.ex_date)
    )
    for ca, sym in db.execute(q):
        f = None if ca.adjustment_factor is None else float(ca.adjustment_factor)
        out.setdefault(sym, []).append(
            Action(ca.action_type, ca.ex_date, f, ca.data_policy, ca.verification_status)
        )
    return out


def adjust_history(
    history: pd.DataFrame, actions: list[Action], as_of: date, vendor_adjusted: bool = False
) -> pd.DataFrame:
    """Copy of `history` made comparable with session `as_of`. The input is not modified."""
    out = history
    for a in sorted(actions, key=lambda a: a.ex_date):
        if a.ex_date > as_of or a.policy == "no_price_effect" or out.empty:
            continue
        before = (out.timestamp_utc.dt.tz_convert(IST).dt.date < a.ex_date).to_numpy()
        if not before.any():
            continue
        if a.policy == "exclude_prior_history":
            out = out[~before]
        elif not vendor_adjusted:
            out = out.copy()
            out["volume"] = out.volume.astype(float)
            out.loc[before, PRICE_COLS] = out.loc[before, PRICE_COLS] / a.factor
            out.loc[before, "volume"] = out.loc[before, "volume"] * a.factor
    return out.reset_index(drop=True) if out is not history else history

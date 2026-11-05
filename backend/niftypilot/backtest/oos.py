"""Dataset-level out-of-sample seal.

Once an experiment exists, its OOS date range on that dataset is sealed for EVERY later run:
another experiment, a single backtest, a sensitivity sweep. Touching it is refused unless
the caller explicitly allows contamination; then the access is logged (append-only) and the
experiment is permanently marked contaminated. The only non-contaminating access is the
experiment's own first final evaluation.

Scope: DB-facing entry points (CLI, run_research, evaluate_oos, sensitivity) enforce this.
`run_backtest` is an in-memory library function and cannot see the database.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from niftypilot.models import Experiment, OosAccessLog


class OosSealedError(PermissionError):
    pass


def _range(r: dict) -> tuple[date, date]:
    a, b = r["range"]
    return date.fromisoformat(a), date.fromisoformat(b)


def overlaps(a1: date, b1: date, a2: date, b2: date) -> bool:
    return a1 <= b2 and a2 <= b1


def sealed_experiments(
    db: Session, dataset_id: int, start: date, end: date, exclude_id: int | None = None
) -> list[Experiment]:
    exps = db.scalars(select(Experiment).where(Experiment.dataset_id == dataset_id)).all()
    return [
        e
        for e in exps
        if e.id != exclude_id and overlaps(start, end, *_range(e.out_of_sample_period))
    ]


def log_access(
    db: Session,
    exp: Experiment,
    purpose: str,
    start: date,
    end: date,
    contaminating: bool,
    detail: str,
) -> None:
    db.add(
        OosAccessLog(
            experiment_id=exp.id,
            purpose=purpose,
            start_date=start,
            end_date=end,
            contaminating=contaminating,
            detail=detail,
        )
    )
    if contaminating:
        exp.contaminated = True


def guard_period(
    db: Session,
    dataset_id: int,
    start: date,
    end: date,
    purpose: str,
    allow_contamination: bool = False,
    detail: str = "",
    exclude_id: int | None = None,
) -> list[Experiment]:
    """Refuse (or, if allowed, log as contaminating) any run touching a sealed OOS range."""
    hits = sealed_experiments(db, dataset_id, start, end, exclude_id)
    if hits and not allow_contamination:
        names = ", ".join(f"{e.experiment_id[:12]} {e.out_of_sample_period['range']}" for e in hits)
        raise OosSealedError(
            f"{purpose} {start}..{end} overlaps sealed out-of-sample period(s): "
            f"{names}. Pass allow_contamination to proceed; the experiment(s) "
            "will be permanently marked contaminated."
        )
    for e in hits:
        log_access(db, e, purpose, start, end, True, detail or f"{purpose} touched sealed OOS")
    db.commit()
    return hits


def access_log(db: Session, exp: Experiment) -> list[dict]:
    rows = db.scalars(
        select(OosAccessLog).where(OosAccessLog.experiment_id == exp.id).order_by(OosAccessLog.id)
    ).all()
    return [
        {
            "at": str(r.accessed_at),
            "purpose": r.purpose,
            "range": [str(r.start_date), str(r.end_date)],
            "contaminating": r.contaminating,
            "detail": r.detail,
        }
        for r in rows
    ]

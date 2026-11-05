"""Research protocol: variants A-E x capital grid on a chronological split.

1. Split trading days (with data) chronologically: development 60% / validation 20% /
   out-of-sample 20%. No overlap; OOS is the most recent period.
2. Development: backtest every variant x capital. Selection rule, fixed before looking:
   per capital, the variant with the highest dev expectancy_R among those with >= min_trades
   trades and positive expectancy. No selection is a valid result.
3. Validation: the selected variant must also have positive expectancy and >= min_trades/3
   trades on validation, otherwise the capital level is marked "not validated".
4. Walk-forward inside dev+validation only: rolling train/test windows; selection on each
   train window by the same rule; selected variant traded on the next test window.
5. Multiple testing: n_trials = variants x capitals; the selected variant's bootstrap p-value is
   Bonferroni-adjusted (p x n_trials, capped at 1).
6. OOS: `evaluate_oos` runs only the frozen selections, once. A second evaluation is refused
   unless forced, and is then labelled CONTAMINATED. The seal is dataset-wide (backtest/oos.py):
   later experiments, backtests and sensitivity sweeps touching it are refused or recorded.
Synthetic datasets are labelled as such in every output.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from niftypilot import __version__
from niftypilot.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from niftypilot.backtest.market import MarketData
from niftypilot.backtest.metrics import compute_metrics
from niftypilot.backtest.oos import guard_period, log_access, overlaps
from niftypilot.models import Experiment
from niftypilot.risk.engine import RiskConfig
from niftypilot.signals import strategy_version_label
from niftypilot.strategy.orb_vwap import OrbParams

VARIANTS: dict[str, tuple[str, OrbParams]] = {
    "A": ("15m ORB + VWAP + retest", OrbParams(require_rvol=False, blocked_regimes=[])),
    "B": ("15m ORB + VWAP + RVOL + retest", OrbParams(require_rvol=True, blocked_regimes=[])),
    "C": (
        "30m ORB + VWAP + retest",
        OrbParams(or_minutes=30, require_rvol=False, blocked_regimes=[]),
    ),
    "D": ("15m ORB + VWAP + regime filter + retest", OrbParams(require_rvol=False)),
    "E": ("15m ORB + VWAP + RVOL + regime filter + retest (M2 baseline)", OrbParams()),
}
CAPITALS = [Decimal(x) for x in (5000, 10000, 25000, 50000, 100000)]


@dataclass(frozen=True)
class Split:
    development: tuple[date, date]
    validation: tuple[date, date]
    out_of_sample: tuple[date, date]

    def as_dict(self) -> dict:
        return {k: [str(a), str(b)] for k, (a, b) in self.__dict__.items()}


def trading_days(m: MarketData, start: date, end: date) -> list[date]:
    days = {d for s, per in m.by_day.items() if m.info[s].segment != "INDEX" for d in per}
    return sorted(d for d in days if start <= d <= end and d.weekday() < 5 and d not in m.holidays)


def chronological_split(days: list[date], dev=0.6, val=0.2) -> Split:
    if len(days) < 10:
        raise ValueError(f"only {len(days)} trading days; too few to split")
    a, b = int(len(days) * dev), int(len(days) * (dev + val))
    return Split((days[0], days[a - 1]), (days[a], days[b - 1]), (days[b], days[-1]))


def config(variant: str, capital: Decimal, base: BacktestConfig) -> BacktestConfig:
    return base.model_copy(
        update={
            "strategy": VARIANTS[variant][1],
            "risk": base.risk.model_copy(update={"capital": capital}),
        }
    )


def summarise(r: BacktestResult) -> dict:
    m = compute_metrics(r)
    setups = m["signal_outcomes"].get("BUY_SETUP", 0)
    rejected = {k: v for k, v in m["order_events"].items() if k.startswith("risk_rejected")}
    out = {
        k: m[k]
        for k in (
            "trades",
            "net_pnl",
            "gross_pnl",
            "total_charges",
            "win_rate",
            "profit_factor",
            "expectancy",
            "expectancy_r",
            "max_drawdown",
            "sharpe_annualised",
            "expectancy_uncertainty",
            "warnings",
        )
    }
    out.update(
        buy_setups=setups,
        risk_rejections=rejected,
        feasibility="NO ECONOMICALLY VIABLE TRADE"
        if setups and not m["trades"]
        else ("no setups" if not setups else "trades possible"),
    )
    return out


def select_variant(rows: dict[str, dict], min_trades: int) -> str | None:
    ok = [
        (v["expectancy_r"], k)
        for k, v in rows.items()
        if v["trades"] >= min_trades and (v["expectancy"] or 0) > 0
    ]
    return max(ok)[1] if ok else None


def _windows(days: list[date], train: int, test: int) -> list[tuple[date, date, date, date]]:
    out, i = [], 0
    while i + train + test <= len(days):
        out.append((days[i], days[i + train - 1], days[i + train], days[i + train + test - 1]))
        i += test
    return out


def walk_forward(
    m: MarketData,
    days: list[date],
    capital: Decimal,
    base: BacktestConfig,
    train: int,
    test: int,
    min_trades: int,
) -> dict:
    windows, oos_trades, chosen = [], [], []
    for a, b, c, d in _windows(days, train, test):
        rows = {v: summarise(run_backtest(m, config(v, capital, base), a, b)) for v in VARIANTS}
        pick = select_variant(rows, min_trades)
        chosen.append(pick)
        row = {
            "train": [str(a), str(b)],
            "test": [str(c), str(d)],
            "selected": pick,
            "train_expectancy_r": rows[pick]["expectancy_r"] if pick else None,
        }
        if pick:
            r = run_backtest(m, config(pick, capital, base), c, d)
            oos_trades += r.trades
            row.update(test_trades=len(r.trades), test_net=summarise(r)["net_pnl"])
        windows.append(row)
    nets = [float(t.net_pnl) for t in oos_trades]
    return {
        "windows": windows,
        "train_days": train,
        "test_days": test,
        "selection_frequency": {v: chosen.count(v) for v in VARIANTS},
        "no_selection_windows": chosen.count(None),
        "oos_trades": len(oos_trades),
        "oos_net_pnl": round(sum(nets), 2),
        "oos_expectancy": round(sum(nets) / len(nets), 2) if nets else None,
    }


def experiment_key(m: MarketData, split: Split, base: BacktestConfig) -> str:
    blob = json.dumps(
        {
            "fp": m.dataset.get("fingerprint"),
            "split": split.as_dict(),
            "base": base.model_dump(mode="json"),
            "code": __version__,
            "variants": {k: strategy_version_label(p) for k, (_, p) in VARIANTS.items()},
            "capitals": [str(c) for c in CAPITALS],
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def run_research(
    db: Session,
    m: MarketData,
    start: date,
    end: date,
    base: BacktestConfig | None = None,
    min_trades: int = 30,
    wf_train: int = 120,
    wf_test: int = 40,
    allow_contamination: bool = False,
) -> Experiment:
    """Development + validation + walk-forward. Never touches the OOS segment.

    Refuses if the new in-sample period overlaps any earlier experiment's sealed OOS on the
    same dataset (unless allow_contamination, which marks those experiments contaminated)."""
    base = base or BacktestConfig(risk=RiskConfig())
    days = trading_days(m, start, end)
    split = chronological_split(days)
    key = experiment_key(m, split, base)
    if existing := db.scalar(select(Experiment).where(Experiment.experiment_id == key)):
        return existing
    ds_id = m.dataset.get("id")
    guard_period(
        db,
        ds_id,
        split.development[0],
        split.validation[1],
        "research",
        allow_contamination,
        f"in-sample of new experiment {key[:12]}",
    )
    in_sample = [d for d in days if d <= split.validation[1]]
    n_trials = len(VARIANTS) * len(CAPITALS)
    per_capital = {}
    for cap in CAPITALS:
        dev = {
            v: summarise(run_backtest(m, config(v, cap, base), *split.development))
            for v in VARIANTS
        }
        val = {
            v: summarise(run_backtest(m, config(v, cap, base), *split.validation)) for v in VARIANTS
        }
        pick = select_variant(dev, min_trades)
        validated = bool(
            pick
            and val[pick]["trades"] >= max(1, min_trades // 3)
            and (val[pick]["expectancy"] or 0) > 0
        )
        p = dev[pick]["expectancy_uncertainty"]["p_value_mean_le_0"] if pick else None
        per_capital[str(cap)] = {
            "development": dev,
            "validation": val,
            "selected": pick,
            "validated": validated,
            "selected_p_value": p,
            "selected_p_value_bonferroni": min(1.0, p * n_trials) if p is not None else None,
            "dev_to_validation_expectancy_r": [dev[pick]["expectancy_r"], val[pick]["expectancy_r"]]
            if pick
            else None,
            "walk_forward": walk_forward(m, in_sample, cap, base, wf_train, wf_test, min_trades),
        }
    exp = Experiment(
        experiment_id=key,
        dataset_id=m.dataset.get("id"),
        parameter_set={
            "variants": {
                k: {
                    "name": n,
                    "version": strategy_version_label(p),
                    "params": p.model_dump(mode="json"),
                }
                for k, (n, p) in VARIANTS.items()
            },
            "capitals": [str(c) for c in CAPITALS],
            "base_config": base.model_dump(mode="json"),
            "selection_rule": f"max dev expectancy_R, >= {min_trades} trades, expectancy > 0",
            "data_kind": m.dataset.get("data_kind"),
            "code_version": __version__,
        },
        training_period={"range": split.as_dict()["development"]},
        validation_period={"range": split.as_dict()["validation"]},
        out_of_sample_period={"range": split.as_dict()["out_of_sample"]},
        n_trials=n_trials,
        metrics={"per_capital": per_capital},
        status="dev_done",
    )
    db.add(exp)
    db.flush()
    # an OOS period that an earlier experiment already used in-sample is not untouched
    a, b = split.out_of_sample
    for prior in db.scalars(
        select(Experiment).where(Experiment.dataset_id == ds_id, Experiment.id != exp.id)
    ):
        pa = date.fromisoformat(prior.training_period["range"][0])
        pb = date.fromisoformat(prior.validation_period["range"][1])
        if overlaps(a, b, pa, pb):
            log_access(
                db,
                exp,
                "research",
                a,
                b,
                True,
                f"OOS was in-sample for earlier experiment {prior.experiment_id[:12]}",
            )
    db.commit()
    return exp


def evaluate_oos(
    db: Session,
    exp: Experiment,
    m: MarketData,
    force: bool = False,
    allow_contamination: bool = False,
) -> dict:
    """Run the frozen selections on the untouched OOS period, once."""
    if exp.oos_evaluated_at is not None and not force:
        raise PermissionError(
            f"OOS for experiment {exp.experiment_id} was already evaluated "
            f"at {exp.oos_evaluated_at}; re-use would contaminate it"
        )
    base = BacktestConfig.model_validate(exp.parameter_set["base_config"])
    a, b = (date.fromisoformat(x) for x in exp.out_of_sample_period["range"])
    # other experiments' sealed OOS inside this range would be seen too
    guard_period(
        db,
        exp.dataset_id,
        a,
        b,
        "final_evaluation",
        allow_contamination,
        f"final evaluation of {exp.experiment_id[:12]}",
        exclude_id=exp.id,
    )
    rerun = exp.oos_evaluated_at is not None
    log_access(
        db,
        exp,
        "final_evaluation",
        a,
        b,
        rerun,
        "forced re-run of final evaluation" if rerun else "first final evaluation",
    )
    out = {"contaminated": bool(exp.contaminated), "per_capital": {}}
    for cap, row in exp.metrics["per_capital"].items():
        pick = row["selected"]
        out["per_capital"][cap] = (
            {
                "selected": pick,
                "validated": row["validated"],
                **summarise(run_backtest(m, config(pick, Decimal(cap), base), a, b)),
            }
            if pick
            else {"selected": None, "note": "no variant met the selection rule"}
        )
    exp.metrics = {**exp.metrics, "out_of_sample": out}
    exp.oos_evaluated_at = datetime.now(UTC)
    exp.status = "oos_done"
    db.commit()
    return out

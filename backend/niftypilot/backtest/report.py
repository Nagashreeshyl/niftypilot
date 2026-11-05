"""Reproducible backtest reports (JSON + Markdown) and the backtest_runs record.

run_key = sha256(dataset fingerprint, config, period, strategy version, code version): the same
inputs always map to the same key and must produce byte-identical report content.
"""

import hashlib
import json
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from niftypilot import __version__
from niftypilot.backtest import metrics as metrics_mod
from niftypilot.backtest.engine import BacktestResult
from niftypilot.backtest.execution import __doc__ as EXECUTION_DOC
from niftypilot.models import BacktestRun
from niftypilot.risk.charges import BROKER_RATES, NSE_RATES
from niftypilot.signals import get_or_create_strategy_version, strategy_version_label
from niftypilot.strategy.orb_vwap import OrbParams

LIMITATIONS = [
    "OHLC replay cannot see intrabar order; ambiguous bars assume the stop filled first.",
    "Bid-ask spread is an assumption (no historical quote data), applied to every market fill.",
    "Fills assume at most participation_pct of each 5m candle's volume; queue position and "
    "market impact beyond that are not modelled.",
    "Corporate actions are detected (overnight gaps), not adjusted.",
    "Fee rates lack verified effective dates for most components (see fee_schedule).",
    "Capital is a fixed allocation: profits are not compounded into position size.",
    "Consecutive-loss and daily-loss limits reset each session.",
    "Past performance on historical data does not predict future results.",
]


def run_key(market_meta: dict, r: BacktestResult) -> str:
    blob = json.dumps(
        {
            "fingerprint": market_meta.get("fingerprint"),
            "dataset": market_meta.get("id"),
            "config": r.config.model_dump(mode="json"),
            "start": str(r.start),
            "end": str(r.end),
            "code": __version__,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def build_report(market_meta: dict, r: BacktestResult, validation: dict | None = None) -> dict:
    m = metrics_mod.compute_metrics(r)
    return {
        "title": "SYNTHETIC DATA - NOT PERFORMANCE EVIDENCE"
        if r.data_kind != "actual"
        else "Backtest on actual historical data",
        "run_key": run_key(market_meta, r),
        "code_version": __version__,
        "strategy_version": strategy_version_label(r.config.strategy),
        "parameters": r.config.model_dump(mode="json"),
        "dataset": market_meta,
        "data_kind": r.data_kind,
        "period": {"start": str(r.start), "end": str(r.end)},
        "capital": str(r.config.risk.capital),
        "fee_schedule": {
            "broker": r.config.broker,
            "authoritative": not r.fees_unverified,
            "unverified_components": sorted(r.fees_unverified),
            "rates": [
                {
                    **e.__dict__,
                    "value": str(e.value),
                    "status": e.status.value,
                    "effective_from": str(e.effective_from or ""),
                    "effective_until": str(e.effective_until or ""),
                }
                for e in BROKER_RATES[r.config.broker] + NSE_RATES
            ],
        },
        "execution_assumptions": EXECUTION_DOC.strip(),
        "metrics_methodology": metrics_mod.__doc__.strip(),
        "metrics": m,
        "data_quality": (market_meta.get("quality") or {}),
        "validation_methodology": validation
        or {"type": "single period backtest; no out-of-sample claim"},
        "limitations": LIMITATIONS,
        "trades": [t.to_dict() for t in sorted(r.trades, key=lambda t: t.entry_time)],
        "orders": r.orders,
        "signals": r.signals,
        "days": r.days,
    }


def report_markdown(rep: dict) -> str:
    m = rep["metrics"]
    lines = [f"# {rep['title']}", ""]
    if rep["data_kind"] != "actual":
        lines += [
            f"> **{rep['data_kind'].upper()} DATA.** These numbers say nothing about real "
            "market performance.",
            "",
        ]
    if not rep["fee_schedule"]["authoritative"]:
        lines += [
            "> **Net P&L is not authoritative:** unverified fee components "
            f"{', '.join(rep['fee_schedule']['unverified_components'])}.",
            "",
        ]
    for w in m["warnings"]:
        lines.append(f"- WARNING: {w}")
    lines += [
        "",
        "## Identity",
        "",
        f"- run_key `{rep['run_key']}`  code {rep['code_version']}",
        f"- strategy {rep['strategy_version']}",
        f"- dataset {rep['dataset'].get('name')} ({rep['data_kind']}), fingerprint "
        f"`{rep['dataset'].get('fingerprint')}`",
        f"- source: {rep['dataset'].get('source')}; price adjustment: "
        f"{rep['dataset'].get('price_adjustment')}",
        f"- period {rep['period']['start']} .. {rep['period']['end']}, capital Rs {rep['capital']}",
        "",
        "## Metrics",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for k, v in m.items():
        if not isinstance(v, dict | list):
            lines.append(f"| {k} | {v} |")
    lines.append(f"| expectancy 95% CI | {m['expectancy_uncertainty']['ci95']} |")
    lines.append(f"| p(mean <= 0) | {m['expectancy_uncertainty']['p_value_mean_le_0']} |")
    for section in ("by_instrument", "by_month", "by_regime", "by_entry_hour"):
        lines += [
            "",
            f"## {section.replace('_', ' ')}",
            "",
            "| key | trades | net | win rate | expectancy |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {k} | {v['trades']} | {v['net_pnl']} | {v['win_rate']} | {v['expectancy']} |"
            for k, v in m[section].items()
        ]
    lines += [
        "",
        "## Every trade (none omitted)",
        "",
        "| entry (UTC) | symbol | qty | entry | exit | reason | gross | charges | net |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {t['entry_time']} | {t['symbol']} | {t['qty']} | {t['entry_price']} | "
        f"{t['exit_price']} | {t['exit_reason']}{' (ambiguous)' if t['ambiguous_bar'] else ''}"
        f" | {t['gross_pnl']} | {t['charges']} | {t['net_pnl']} |"
        for t in rep["trades"]
    ]
    q = rep["data_quality"]
    lines += ["", "## Data quality", "", f"status: {q.get('status', 'not checked')}"]
    lines += [
        f"- {w['kind']} {w.get('instrument') or ''}: {w['detail']} (x{w['count']})"
        for w in q.get("warnings", [])
    ]
    lines += [
        "",
        "## Validation methodology",
        "",
        "```",
        json.dumps(rep["validation_methodology"], indent=2, default=str),
        "```",
        "",
        "## Execution assumptions",
        "",
        "```",
        rep["execution_assumptions"],
        "```",
        "",
        "## Metric definitions",
        "",
        "```",
        rep["metrics_methodology"],
        "```",
        "",
        "## Known limitations",
        "",
    ]
    lines += [f"- {x}" for x in rep["limitations"]]
    return "\n".join(lines) + "\n"


def write_report(rep: dict, out_dir: str | Path) -> Path:
    d = Path(out_dir) / rep["run_key"][:16]
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(rep, indent=2, sort_keys=True, default=str))
    (d / "report.md").write_text(report_markdown(rep))
    return d


def record_run(db: Session, rep: dict, path: Path) -> BacktestRun:
    existing = db.scalar(select(BacktestRun).where(BacktestRun.run_key == rep["run_key"]))
    if existing:
        return existing
    sv = get_or_create_strategy_version(db, OrbParams(**rep["parameters"]["strategy"]))
    m = rep["metrics"]
    run = BacktestRun(
        run_key=rep["run_key"],
        strategy_version_id=sv.id,
        dataset_id=rep["dataset"]["id"],
        dataset_fingerprint=rep["dataset"]["fingerprint"],
        start_date=date.fromisoformat(rep["period"]["start"]),
        end_date=date.fromisoformat(rep["period"]["end"]),
        capital=float(rep["capital"]),
        config=rep["parameters"],
        net_profit=m["net_pnl"],
        profit_factor=m["profit_factor"],
        expectancy=m["expectancy"],
        maximum_drawdown=m["max_drawdown"],
        win_rate=m["win_rate"],
        total_trades=m["trades"],
        fees_authoritative=rep["fee_schedule"]["authoritative"],
        report_path=str(path),
    )
    db.add(run)
    db.commit()
    return run

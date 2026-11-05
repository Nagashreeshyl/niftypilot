"""NiftyPilot research CLI (simulation only; nothing here can place an order).

uv run python -m niftypilot.cli <command> --help
Typical flow with licensed data:
  load-instruments, load-constituents, load-holidays, load-sessions, load-corporate-actions,
  supersede-membership, create-dataset, import-csv,
  quality, backtest, research, oos (once).
"""

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from niftypilot import synthetic
from niftypilot.backtest.engine import BacktestConfig, run_backtest
from niftypilot.backtest.market import load_market
from niftypilot.backtest.oos import OosSealedError, access_log, guard_period
from niftypilot.backtest.report import build_report, record_run, write_report
from niftypilot.backtest.research import CAPITALS, VARIANTS, evaluate_oos, run_research
from niftypilot.backtest.sensitivity import run_sensitivity, sensitivity_markdown
from niftypilot.config import Settings
from niftypilot.db import make_engine, make_session_factory
from niftypilot.market_data import ingest
from niftypilot.market_data.calendar import load_sessions_csv
from niftypilot.market_data.corporate_actions import load_corporate_actions_csv
from niftypilot.market_data.provenance import import_manifest, load_aliases_csv
from niftypilot.market_data.quality import check_dataset
from niftypilot.models import Dataset, Experiment, Instrument
from niftypilot.risk.engine import RiskConfig
from niftypilot.risk.reconcile import load_note, reconcile


def _db():
    return make_session_factory(make_engine(Settings().database_url))()


def _dataset(db, name: str) -> Dataset:
    ds = db.scalar(select(Dataset).where(Dataset.name == name))
    if ds is None:
        raise SystemExit(f"unknown dataset {name!r}")
    return ds


def _market(db, a):
    ds = _dataset(db, a.dataset)
    return load_market(db, ds, a.start, a.end, a.index, a.benchmark)


def cmd_create_dataset(a):
    with _db() as db:
        ds = ingest.ensure_dataset(
            db,
            a.name,
            provider=a.provider,
            source=a.source,
            data_kind=a.kind,
            source_timezone=a.tz,
            price_adjustment=a.adjustment,
        )
        print(f"dataset {ds.id} {ds.name} ({ds.data_kind})")


def cmd_import_csv(a):
    with _db() as db:
        for r in ingest.import_csv_directory(db, _dataset(db, a.dataset), a.dir, a.interval):
            print(
                f"run {r.id}: {r.status} received={r.rows_received} "
                f"inserted={r.rows_inserted} rejected={r.rows_rejected}"
            )


def cmd_load(a):
    with _db() as db:
        fn = {
            "instruments": ingest.load_instruments_csv,
            "constituents": ingest.load_constituents_csv,
            "sessions": load_sessions_csv,
            "corporate-actions": load_corporate_actions_csv,
        }.get(a.what)
        n = fn(db, a.file) if fn else ingest.load_holidays_csv(db, a.file, a.source)
        print(f"{a.what}: {n} rows")


def cmd_supersede(a):
    with _db() as db:
        r = ingest.supersede_membership(db, a.record_id, a.note)
        print(f"membership {r.id}: {r.verification_status}")


def cmd_synthetic(a):
    """Create a SYNTHETIC dataset (fake symbols, data_kind=synthetic) for engine checks."""
    syms = {f"SYNTH{i:02d}": p for i, p in enumerate([120, 340, 760, 1500, 2900, 480, 95, 2100])}
    with _db() as db:
        ds = ingest.ensure_dataset(
            db,
            a.name,
            provider="synthetic",
            source=f"niftypilot.synthetic.random_market seed={a.seed}",
            data_kind="synthetic",
            source_timezone="UTC",
            price_adjustment="unadjusted",
        )
        for sym, df in synthetic.random_market(
            syms, a.start, a.days, a.seed, "SYNTH-INDEX"
        ).items():
            inst = db.scalar(select(Instrument).where(Instrument.trading_symbol == sym))
            if inst is None:
                inst = Instrument(
                    exchange="SYNTH",
                    segment="INDEX" if "INDEX" in sym else "CASH",
                    trading_symbol=sym,
                    display_name=f"{sym} (synthetic)",
                )
                db.add(inst)
                db.commit()
            ingest.ingest_candles(db, ds, inst.id, df, "5m")
        print(f"synthetic dataset {ds.name}: {len(syms)} symbols + SYNTH-INDEX, {a.days} days")


def cmd_quality(a):
    with _db() as db:
        r = check_dataset(db, _dataset(db, a.dataset), a.start, a.end, a.index)
        print(json.dumps(r.to_dict(), indent=2, default=str))
        if r.status == "failed":
            raise SystemExit("CRITICAL data-quality findings: backtests are blocked")


def _guard(db, a, purpose: str) -> None:
    hits = guard_period(
        db,
        _dataset(db, a.dataset).id,
        a.start,
        a.end,
        purpose,
        a.allow_oos_contamination,
        f"cli {purpose}",
    )
    for e in hits:
        print(f"WARNING: experiment {e.experiment_id[:12]} OOS is now CONTAMINATED (logged)")


def cmd_backtest(a):
    with _db() as db:
        _guard(db, a, "backtest")
        m = _market(db, a)
        cfg = BacktestConfig(
            strategy=VARIANTS[a.variant][1], risk=RiskConfig(capital=Decimal(a.capital))
        )
        rep = build_report(m.dataset | {"quality": m.quality}, run_backtest(m, cfg, a.start, a.end))
        path = write_report(rep, a.out)
        record_run(db, rep, path)
        mm = rep["metrics"]
        print(
            f"{rep['title']}\nreport: {path}/report.md\ntrades {mm['trades']}  net "
            f"{mm['net_pnl']}  expectancy {mm['expectancy']}  fees authoritative "
            f"{rep['fee_schedule']['authoritative']}"
        )
        for w in mm["warnings"]:
            print("WARNING:", w)


def oos_status(exp: Experiment) -> str:
    if exp.contaminated:
        return "CONTAMINATED: the OOS period was accessed outside its single final evaluation"
    if exp.oos_evaluated_at:
        return f"evaluated once at {exp.oos_evaluated_at} (clean)"
    return "untouched (sealed)"


def research_markdown(exp: Experiment, log: list[dict] | None = None) -> str:
    kind = exp.parameter_set["data_kind"]
    out = [f"# Research experiment {exp.experiment_id}", ""]
    if kind != "actual":
        out += [f"> **{kind.upper()} DATA - NOT PERFORMANCE EVIDENCE.**", ""]
    out += [f"**Out-of-sample status: {oos_status(exp)}**", ""]
    if exp.contaminated:
        out += [
            "> A contaminated OOS result is NOT independent evidence: its data influenced "
            "decisions or was viewed more than once. Treat it as in-sample.",
            "",
        ]
    out += [
        f"- development {exp.training_period['range']}, validation "
        f"{exp.validation_period['range']}, out-of-sample {exp.out_of_sample_period['range']}",
        f"- trials (variants x capitals): {exp.n_trials}; selection rule: "
        f"{exp.parameter_set['selection_rule']}",
        "",
        "| capital | variant | dev trades | dev net | dev exp_R | val trades | val net | "
        "feasibility |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cap, row in exp.metrics["per_capital"].items():
        for v in VARIANTS:
            d, val = row["development"][v], row["validation"][v]
            out.append(
                f"| {cap} | {v} | {d['trades']} | {d['net_pnl']} | {d['expectancy_r']} | "
                f"{val['trades']} | {val['net_pnl']} | {d['feasibility']} |"
            )
    out += ["", "## Selection, validation, walk-forward", ""]
    for cap, row in exp.metrics["per_capital"].items():
        wf = row["walk_forward"]
        out.append(
            f"- Rs {cap}: selected **{row['selected']}**, validated {row['validated']}, "
            f"p={row['selected_p_value']} (Bonferroni {row['selected_p_value_bonferroni']})"
            f"; walk-forward {len(wf['windows'])} windows, OOS trades {wf['oos_trades']}, "
            f"net {wf['oos_net_pnl']}, no-selection windows {wf['no_selection_windows']}"
        )
    if oos := exp.metrics.get("out_of_sample"):
        out += [
            "",
            "## Untouched out-of-sample" + (" (CONTAMINATED)" if oos["contaminated"] else ""),
            "",
        ]
        out += [f"- Rs {c}: {json.dumps(v, default=str)}" for c, v in oos["per_capital"].items()]
    if log:
        out += ["", "## OOS access log (append-only)", ""]
        out += [
            f"- {x['at']} {x['purpose']} {x['range']} contaminating={x['contaminating']}: "
            f"{x['detail']}"
            for x in log
        ]
    return "\n".join(out) + "\n"


def _write_research(db, exp: Experiment, out: Path) -> Path:
    d = Path(out) / f"experiment-{exp.experiment_id[:16]}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "experiment.json").write_text(
        json.dumps(
            {c.name: getattr(exp, c.name) for c in exp.__table__.columns}, indent=2, default=str
        )
    )
    (d / "experiment.md").write_text(research_markdown(exp, access_log(db, exp)))
    return d


def cmd_research(a):
    with _db() as db:
        exp = run_research(
            db,
            _market(db, a),
            a.start,
            a.end,
            min_trades=a.min_trades,
            wf_train=a.wf_train,
            wf_test=a.wf_test,
            allow_contamination=a.allow_oos_contamination,
        )
        print(f"experiment {exp.experiment_id}: {_write_research(db, exp, a.out)}/experiment.md")


def cmd_oos(a):
    with _db() as db:
        exp = db.scalar(select(Experiment).where(Experiment.experiment_id == a.experiment))
        if exp is None:
            raise SystemExit("unknown experiment")
        evaluate_oos(
            db, exp, _market(db, a), force=a.force, allow_contamination=a.allow_oos_contamination
        )
        print(f"OOS evaluated: {_write_research(db, exp, a.out)}/experiment.md")


def cmd_import_manifest(a):
    with _db() as db:
        ds, runs = import_manifest(db, a.manifest, a.raw_store)
        for r in runs:
            print(
                f"run {r.id}: {r.status} received={r.rows_received} "
                f"inserted={r.rows_inserted} rejected={r.rows_rejected}"
            )
        print(
            f"dataset {ds.name}: provenance {ds.provenance_status}, coverage "
            f"{ds.coverage_start}..{ds.coverage_end}; run `quality` before backtesting"
        )


def cmd_load_aliases(a):
    with _db() as db:
        print(f"aliases: {load_aliases_csv(db, a.file)} rows")


def cmd_sensitivity(a):
    with _db() as db:
        _guard(db, a, "sensitivity")
        m = _market(db, a)
        rows = run_sensitivity(
            m, a.start, a.end, a.variants.split(","), [Decimal(c) for c in a.capitals.split(",")]
        )
        d = Path(a.out) / f"sensitivity-{m.dataset['name']}-{a.start}-{a.end}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sensitivity.json").write_text(json.dumps(rows, indent=2, default=str))
        (d / "sensitivity.md").write_text(
            sensitivity_markdown(rows, m.dataset["data_kind"], f"{a.start}..{a.end}")
        )
        print(f"{len(rows)} rows: {d}/sensitivity.md")


def cmd_reconcile(a):
    results = reconcile(load_note(a.file))
    for r in results:
        print(
            f"{r.component:10} note {r.note:>10} model {r.model:>10} diff {r.diff:>8} "
            f"{'OK' if r.ok else 'MISMATCH'}"
        )
    if not all(r.ok for r in results):
        raise SystemExit(
            "contract note does not reconcile: a rate, base or rounding rule "
            "in risk/charges.py is wrong for this date"
        )


def main(argv=None):
    p = argparse.ArgumentParser(prog="niftypilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    iso = date.fromisoformat

    c = sub.add_parser("create-dataset")
    c.add_argument("--name", required=True)
    c.add_argument("--provider", default="csv")
    c.add_argument("--source", required=True, help="vendor / licence / file provenance")
    c.add_argument(
        "--kind",
        choices=["synthetic", "simulated"],
        required=True,
        help="actual data: use import-manifest (provenance required)",
    )
    c.add_argument("--tz", default="Asia/Kolkata", help="timezone of the raw timestamps")
    c.add_argument("--adjustment", choices=["unadjusted", "adjusted", "unknown"], default="unknown")
    c.set_defaults(fn=cmd_create_dataset)

    c = sub.add_parser("import-csv")
    c.add_argument("--dataset", required=True)
    c.add_argument("--dir", required=True)
    c.add_argument("--interval", default="5m", choices=["5m", "1d"])
    c.set_defaults(fn=cmd_import_csv)

    for what in ("instruments", "constituents", "holidays", "sessions", "corporate-actions"):
        c = sub.add_parser(f"load-{what}")
        c.add_argument("file")
        if what == "holidays":
            c.add_argument("--source", required=True, help="cite the official NSE circular")
        c.set_defaults(fn=cmd_load, what=what)

    c = sub.add_parser("supersede-membership", help="resolve a membership conflict, keep row")
    c.add_argument("record_id", type=int)
    c.add_argument("--note", required=True, help="which document shows the record is wrong")
    c.set_defaults(fn=cmd_supersede)

    c = sub.add_parser("synthetic-dataset")
    c.add_argument("--name", default="synthetic-random")
    c.add_argument("--start", type=iso, default=date(2024, 1, 1))
    c.add_argument("--days", type=int, default=250)
    c.add_argument("--seed", type=int, default=42)
    c.set_defaults(fn=cmd_synthetic)

    def period(c):
        c.add_argument("--dataset", required=True)
        c.add_argument("--start", type=iso, required=True)
        c.add_argument("--end", type=iso, required=True)
        c.add_argument("--index", default=None, help="point-in-time universe, e.g. 'NIFTY 50'")
        c.add_argument("--benchmark", default=None, help="index symbol inside the dataset")
        c.add_argument("--out", default="reports")
        c.add_argument(
            "--allow-oos-contamination",
            action="store_true",
            help="touch a sealed OOS period anyway; logged, marks it contaminated",
        )

    c = sub.add_parser("quality")
    period(c)
    c.set_defaults(fn=cmd_quality)

    c = sub.add_parser("backtest")
    period(c)
    c.add_argument("--variant", choices=sorted(VARIANTS), default="E")
    c.add_argument("--capital", default="5000", choices=[str(x) for x in CAPITALS])
    c.set_defaults(fn=cmd_backtest)

    c = sub.add_parser("research")
    period(c)
    c.add_argument("--min-trades", type=int, default=30)
    c.add_argument("--wf-train", type=int, default=120)
    c.add_argument("--wf-test", type=int, default=40)
    c.set_defaults(fn=cmd_research)

    c = sub.add_parser("import-manifest", help="import ACTUAL data with provenance")
    c.add_argument("manifest")
    c.add_argument("--raw-store", default="data/raw", help="where original files are kept")
    c.set_defaults(fn=cmd_import_manifest)

    c = sub.add_parser("load-aliases", help="historical symbols: alias,trading_symbol,...")
    c.add_argument("file")
    c.set_defaults(fn=cmd_load_aliases)

    c = sub.add_parser("sensitivity")
    period(c)
    c.add_argument("--variants", default="E")
    c.add_argument("--capitals", default="5000,100000")
    c.set_defaults(fn=cmd_sensitivity)

    c = sub.add_parser("reconcile-contract-note")
    c.add_argument("file", help="transcribed REAL contract note (risk/reconcile.py format)")
    c.set_defaults(fn=cmd_reconcile)

    c = sub.add_parser("oos")
    period(c)
    c.add_argument("--experiment", required=True)
    c.add_argument("--force", action="store_true", help="re-run; result marked CONTAMINATED")
    c.set_defaults(fn=cmd_oos)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except OosSealedError as e:
        raise SystemExit(f"REFUSED: {e}") from None


if __name__ == "__main__":
    main()

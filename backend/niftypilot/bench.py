"""Pipeline benchmark on a SYNTHETIC dataset sized like the Nifty 50 (timing only; the data
is random and the strategy results are meaningless).

uv run python -m niftypilot.bench --symbols 50 --days 250 --out bench.json
Uses a fresh SQLite file in a temp directory. Peak memory = process max RSS (getrusage).
"""

import argparse
import cProfile
import io
import json
import platform
import pstats
import resource
import sys
import tempfile
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from niftypilot import synthetic
from niftypilot.backtest.engine import BacktestConfig, run_backtest
from niftypilot.backtest.market import load_market
from niftypilot.backtest.research import config, run_research, trading_days
from niftypilot.backtest.sensitivity import run_sensitivity
from niftypilot.db import Base, make_engine, make_session_factory
from niftypilot.market_data.ingest import ensure_dataset, ingest_candles
from niftypilot.market_data.quality import check_dataset
from niftypilot.models import Instrument


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1 << 20) if sys.platform == "darwin" else r / 1024, 1)  # bytes vs KiB


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=50)
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--research", action="store_true", help="also time the full research run")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    stages: list[dict] = []

    def stage(name, fn):
        t = time.perf_counter()
        out = fn()
        stages.append(
            {
                "stage": name,
                "seconds": round(time.perf_counter() - t, 2),
                "peak_rss_mb": peak_rss_mb(),
            }
        )
        print(
            f"{name:28} {stages[-1]['seconds']:8.2f}s  peak RSS {stages[-1]['peak_rss_mb']} MB",
            flush=True,
        )
        return out

    tmp = Path(tempfile.mkdtemp(prefix="np-bench-"))
    engine = make_engine(f"sqlite:///{tmp / 'bench.db'}")
    Base.metadata.create_all(engine)
    db = make_session_factory(engine)()
    start = date(2023, 1, 2)
    prices = {f"SYNTH{i:02d}": 80 + (i * 137) % 3000 for i in range(a.symbols)}
    candles = stage(
        "generate", lambda: synthetic.random_market(prices, start, a.days, a.seed, "SYNTH-INDEX")
    )
    n_candles = sum(len(df) for df in candles.values())
    ds = ensure_dataset(
        db,
        "bench",
        provider="synthetic",
        source="bench",
        data_kind="synthetic",
        source_timezone="UTC",
        price_adjustment="unadjusted",
    )

    def ingest():
        for sym, df in candles.items():
            inst = Instrument(
                exchange="SYNTH",
                segment="INDEX" if "INDEX" in sym else "CASH",
                trading_symbol=sym,
                display_name=sym,
            )
            db.add(inst)
            db.commit()
            ingest_candles(db, ds, inst.id, df, "5m")

    stage("ingest (validate + insert)", ingest)
    end = max(d for df in candles.values() for d in df.timestamp_utc.dt.date)
    stage("quality report", lambda: check_dataset(db, ds, start, end))
    m = stage(
        "load market", lambda: load_market(db, ds, start, end, benchmark_symbol="SYNTH-INDEX")
    )
    days = trading_days(m, start, end)
    bt_start = days[20]  # leave scanner lookback
    cfg = config("E", Decimal(100000), BacktestConfig())
    prof = cProfile.Profile()
    stage("backtest E cold (profiled)", lambda: prof.runcall(run_backtest, m, cfg, bt_start, end))
    stage("backtest E warm cache", lambda: run_backtest(m, cfg, bt_start, end))
    stage(
        "sensitivity 9 scen x E x 1L",
        lambda: run_sensitivity(m, bt_start, end, ["E"], [Decimal(100000)]),
    )
    if a.research:
        stage(
            "research A-E x 5 capitals",
            lambda: run_research(db, m, bt_start, end, wf_train=60, wf_test=20),
        )
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(r"niftypilot", 12)
    result = {
        "machine": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "dataset": {
            "instruments": a.symbols,
            "index_series": 1,
            "sessions": a.days,
            "candles": n_candles,
            "db_bytes": (tmp / "bench.db").stat().st_size,
        },
        "stages": stages,
        "peak_rss_mb": peak_rss_mb(),
        "cold_backtest_profile_top": [
            ln for ln in buf.getvalue().splitlines() if "niftypilot" in ln
        ][:12],
    }
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    main()

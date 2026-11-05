"""Execution-cost sensitivity: the same strategy under a FIXED, pre-declared set of spread,
slippage and entry-delay assumptions. The list is not tuned and must not be edited to find
a profitable row; add scenarios, never delete one. Risk limits are never changed: only
ExecutionModel fields vary (risk sizing follows the scenario's costs automatically).
"""

from collections import Counter
from decimal import Decimal

from niftypilot.backtest.engine import BacktestConfig, run_backtest
from niftypilot.backtest.market import MarketData
from niftypilot.backtest.metrics import compute_metrics
from niftypilot.backtest.research import config

D = Decimal
SCENARIOS: list[tuple[str, dict]] = [
    ("optimistic", {"half_spread_pct": D("0.01"), "slippage_pct": D("0.02")}),
    ("baseline", {}),  # ExecutionModel defaults: half-spread 0.025%, slippage 0.05%, delay 1
    ("wide_spread", {"half_spread_pct": D("0.05")}),
    ("very_wide_spread", {"half_spread_pct": D("0.10")}),
    ("high_slippage", {"slippage_pct": D("0.10")}),
    ("very_high_slippage", {"slippage_pct": D("0.20")}),
    ("delay_2_bars", {"entry_delay_bars": 2}),
    ("delay_3_bars", {"entry_delay_bars": 3}),
    (
        "pessimistic",
        {"half_spread_pct": D("0.05"), "slippage_pct": D("0.10"), "entry_delay_bars": 2},
    ),
]
REJECTED = ("risk_rejected", "rejected", "cancelled")


def run_sensitivity(
    m: MarketData,
    start,
    end,
    variants: list[str],
    capitals: list[Decimal],
    base: BacktestConfig | None = None,
    scenarios=SCENARIOS,
) -> list[dict]:
    base = base or BacktestConfig()
    rows = []
    for name, overrides in scenarios:
        ex = base.execution.model_copy(update=overrides)
        for v in variants:
            for cap in capitals:
                cfg = config(v, cap, base).model_copy(update={"execution": ex})
                r = run_backtest(m, cfg, start, end)
                mm = compute_metrics(r)
                status = Counter(o["status"] for o in r.orders)
                rows.append(
                    {
                        "scenario": name,
                        "variant": v,
                        "capital": str(cap),
                        "half_spread_pct": str(ex.half_spread_pct),
                        "slippage_pct": str(ex.slippage_pct),
                        "entry_delay_bars": ex.entry_delay_bars,
                        "buy_setups": mm["signal_outcomes"].get("BUY_SETUP", 0),
                        "trades": mm["trades"],
                        "rejected_or_unfilled": sum(status[s] for s in REJECTED),
                        "rejections": dict(status),
                        "gross_pnl": mm["gross_pnl"],
                        "charges": mm["total_charges"],
                        "spread_cost": mm["spread_cost"],
                        "slippage_cost": mm["slippage_cost"],
                        "net_pnl": mm["net_pnl"],
                        "expectancy": mm["expectancy"],
                        "expectancy_r": mm["expectancy_r"],
                        "profit_factor": mm["profit_factor"],
                        "max_drawdown": mm["max_drawdown"],
                        "fees_authoritative": mm["fees_authoritative"],
                    }
                )
    return rows


def sensitivity_markdown(rows: list[dict], data_kind: str, period: str) -> str:
    out = [f"# Execution sensitivity ({period})", ""]
    if data_kind != "actual":
        out += [f"> **{data_kind.upper()} DATA - NOT PERFORMANCE EVIDENCE.**", ""]
    if rows and not rows[0]["fees_authoritative"]:
        out += ["> Net P&L uses unverified fee rates (see docs/m3_1_fee_verification.md).", ""]
    out += [
        "Gross P&L already includes spread and slippage (fills are adverse); "
        "net = gross - charges.",
        "",
        "| scenario | variant | capital | spread% | slip% | delay | setups | trades | "
        "rejected | gross | charges | spread | slippage | net | exp | exp_R | PF | max DD |",
        "|" + "---|" * 18,
    ]
    out += [
        f"| {r['scenario']} | {r['variant']} | {r['capital']} | {r['half_spread_pct']} | "
        f"{r['slippage_pct']} | {r['entry_delay_bars']} | {r['buy_setups']} | {r['trades']} | "
        f"{r['rejected_or_unfilled']} | {r['gross_pnl']} | {r['charges']} | "
        f"{r['spread_cost']} | {r['slippage_cost']} | {r['net_pnl']} | {r['expectancy']} | "
        f"{r['expectancy_r']} | {r['profit_factor']} | {r['max_drawdown']} |"
        for r in rows
    ]
    return "\n".join(out) + "\n"

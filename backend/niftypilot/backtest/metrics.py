"""Performance metrics. Methodology (also printed in every report):

- P&L in rupees; net = gross (fills include spread+slippage) - charges.
- Win: net > 0. Loss: net < 0. Profit factor = sum(wins) / |sum(losses)| (None without losses).
- Expectancy = mean net per trade; expectancy_R = mean(net / planned risk in Rs at entry).
- Max drawdown on the mark-to-market equity sampled at every 5m candle close.
- Sharpe: daily P&L / allocated capital (fixed, non-compounding) over EVERY trading day in the
  period including no-trade days; mean/std x sqrt(252); risk-free rate 0 (not subtracted,
  because the allocation is otherwise uninvested cash). Standard error by Lo (2002),
  SE = sqrt((1 + SR_d^2 / 2) / N) on the daily ratio, annualised.
- Uncertainty: seeded bootstrap (10,000 resamples) 95% CI for expectancy, and a one-sided
  bootstrap p-value for "mean net per trade <= 0". Fewer than 30 trades: flagged as not
  statistically meaningful.
"""

import math
from collections import Counter, defaultdict
from decimal import Decimal

import numpy as np

from niftypilot.backtest.engine import BacktestResult, Trade
from niftypilot.market_data.session import IST

MIN_TRADES = 30
BOOTSTRAP_SEED = 12345


def _f(x) -> float:
    return round(float(x), 2)


def bootstrap(values: list[float], n: int = 10_000, seed: int = BOOTSTRAP_SEED) -> dict:
    if len(values) < 2:
        return {"ci95": None, "p_value_mean_le_0": None}
    v = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), (n, len(v)))].mean(axis=1)
    centred = (v - v.mean())[rng.integers(0, len(v), (n, len(v)))].mean(axis=1)
    return {
        "ci95": [_f(np.percentile(means, 2.5)), _f(np.percentile(means, 97.5))],
        "p_value_mean_le_0": round(float((centred >= v.mean()).mean()), 4),
    }


def _group(trades: list[Trade], key) -> dict:
    g = defaultdict(list)
    for t in trades:
        g[key(t)].append(float(t.net_pnl))
    return {
        k: {
            "trades": len(v),
            "net_pnl": _f(sum(v)),
            "win_rate": round(sum(x > 0 for x in v) / len(v), 4),
            "expectancy": _f(np.mean(v)),
        }
        for k, v in sorted(g.items())
    }


def max_consecutive_losses(trades: list[Trade]) -> int:
    best = cur = 0
    for t in sorted(trades, key=lambda t: t.exit_time):
        cur = cur + 1 if t.net_pnl < 0 else 0
        best = max(best, cur)
    return best


def compute_metrics(r: BacktestResult) -> dict:
    capital = Decimal(r.config.risk.capital)
    trades = sorted(r.trades, key=lambda t: t.exit_time)
    nets = [float(t.net_pnl) for t in trades]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x < 0]
    n = len(trades)

    eq = np.array([float(e) for _, e, _ in r.equity]) if r.equity else np.array([float(capital)])
    peak = np.maximum.accumulate(np.concatenate([[float(capital)], eq]))
    dd = peak - np.concatenate([[float(capital)], eq])
    i = int(dd.argmax())

    days = sorted(r.daily_equity)
    eod = [float(capital)] + [float(r.daily_equity[d]) for d in days]
    daily = np.diff(eod) / float(capital)
    sharpe = sharpe_se = None
    if len(daily) >= 2 and daily.std(ddof=1) > 0:
        sr_d = daily.mean() / daily.std(ddof=1)
        sharpe = round(sr_d * math.sqrt(252), 3)
        sharpe_se = round(math.sqrt((1 + sr_d**2 / 2) / len(daily)) * math.sqrt(252), 3)

    risk_r = [float(t.net_pnl / ((t.entry_price - t.stop) * t.qty)) for t in trades]
    invested = [float(x) for _, _, x in r.equity]
    months = max(1, (r.end.year - r.start.year) * 12 + r.end.month - r.start.month + 1)
    warnings = []
    if n < MIN_TRADES:
        warnings.append(
            f"only {n} trades (< {MIN_TRADES}): metrics are not statistically meaningful"
        )
    if r.fees_unverified:
        warnings.append(
            f"net P&L is NOT authoritative: unverified fee components {sorted(r.fees_unverified)}"
        )
    return {
        "trades": n,
        "gross_pnl": _f(sum(t.gross_pnl for t in trades)),
        "net_pnl": _f(sum(nets)),
        "total_charges": _f(sum(t.charges for t in trades)),
        "spread_cost": _f(sum(t.spread_cost for t in trades)),
        "slippage_cost": _f(sum(t.slippage_cost for t in trades)),
        "slippage_and_spread_cost": _f(sum(t.spread_cost + t.slippage_cost for t in trades)),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        "expectancy": _f(np.mean(nets)) if n else None,
        "expectancy_r": round(float(np.mean(risk_r)), 3) if n else None,
        "expectancy_uncertainty": bootstrap(nets),
        "avg_win": _f(np.mean(wins)) if wins else None,
        "avg_loss": _f(np.mean(losses)) if losses else None,
        "max_consecutive_losses": max_consecutive_losses(trades),
        "max_drawdown": _f(dd[i]),
        "max_drawdown_pct_of_peak": round(float(dd[i] / peak[i] * 100), 3),
        "avg_holding_minutes": round(float(np.mean([t.holding_minutes for t in trades])), 1)
        if n
        else None,
        "trades_per_month": round(n / months, 2),
        "exposure_time_pct": round(100 * sum(x > 0 for x in invested) / len(invested), 2)
        if invested
        else 0.0,
        "avg_capital_utilisation_pct": round(100 * float(np.mean(invested)) / float(capital), 2)
        if invested
        else 0.0,
        "sharpe_annualised": sharpe,
        "sharpe_standard_error": sharpe_se,
        "trading_days": len(days),
        "ending_equity": _f(eod[-1]),
        "ambiguous_bars": sum(t.ambiguous_bar for t in trades),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "signal_outcomes": dict(Counter(s["outcome"] for s in r.signals)),
        "order_events": dict(Counter(f"{o['status']}:{o.get('reason', '')}" for o in r.orders)),
        "by_instrument": _group(trades, lambda t: t.symbol),
        "by_month": _group(trades, lambda t: f"{t.exit_time.astimezone(IST):%Y-%m}"),
        "by_regime": _group(trades, lambda t: t.regime),
        "by_entry_hour": _group(trades, lambda t: f"{t.entry_time.astimezone(IST):%H}:00"),
        "fees_authoritative": not r.fees_unverified,
        "warnings": warnings,
    }

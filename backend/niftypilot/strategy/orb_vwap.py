"""15-minute Opening Range Breakout + VWAP + retest confirmation. Long-only.

evaluate() is a pure function of (candles, history, as_of). It discards every candle not
completed by `as_of`, so passing future data cannot change the answer (tested).

Phases over completed post-range candles, one setup per session:
  AWAIT_BREAKOUT -> first close above ORH. Must close above VWAP, meet RVOL, and the
                    regime must not be blocked, else BREAKOUT_REJECTED (terminal).
                    A close below ORL first is a bearish breakdown: research-only NO_TRADE.
  AWAIT_RETEST   -> a later candle's low comes within retest_tolerance_pct of ORH.
  AWAIT_CONFIRM  -> a bullish candle closes above the retest candle's high and above VWAP.
                    A deeper retest candle replaces the reference retest.
  In retest/confirm phases, a close below ORH*(1-invalidation_pct) or below VWAP, a timeout,
  or the entry cutoff gives SETUP_INVALIDATED.
Risk sizing/approval is NOT done here; see niftypilot.signals.generate_signal.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

import pandas as pd
from pydantic import BaseModel, Field

from niftypilot.market_data.session import IST, SESSION_OPEN, session_slots
from niftypilot.market_data.validation import validate_candles
from niftypilot.strategy.indicators import (
    OpeningRange,
    opening_range,
    relative_volume,
    session_vwap,
)
from niftypilot.strategy.regime import Regime

STRATEGY_ID = "orb_vwap_retest"
STRATEGY_VERSION = "1.1.0"  # 1.1.0: require_rvol toggle (default keeps 1.0.0 behaviour)


class Outcome(StrEnum):
    BUY_SETUP = "BUY_SETUP"
    NO_TRADE = "NO_TRADE"
    BREAKOUT_REJECTED = "BREAKOUT_REJECTED"
    RETEST_PENDING = "RETEST_PENDING"
    SETUP_INVALIDATED = "SETUP_INVALIDATED"
    RISK_REJECTED = "RISK_REJECTED"
    DATA_INVALID = "DATA_INVALID"


class OrbParams(BaseModel):
    """Starting configuration, not proven-optimal values."""

    interval_minutes: int = 5
    or_minutes: int = Field(15, gt=0)
    require_rvol: bool = True  # False: relative volume is neither required nor computed
    rvol_threshold: float = 1.5
    rvol_lookback_sessions: int = 10
    rvol_min_sessions: int = 5
    retest_tolerance_pct: float = 0.15  # retest low must reach ORH * (1 + this %)
    invalidation_pct: float = 0.30  # close below ORH * (1 - this %) kills the setup
    max_retest_candles: int = 6
    max_confirm_candles: int = 3
    entry_cutoff: time = time(13, 30)  # IST; no new setups at/after this candle start
    stop_buffer_pct: float = 0.05  # below the retest low
    target_r_multiple: float = 2.0
    min_stop_pct: float = 0.20  # tighter stops are noise
    max_stop_pct: float = 1.50
    blocked_regimes: list[Regime] = [Regime.BEARISH]


@dataclass
class RuleCheck:
    rule: str
    passed: bool
    value: float | str | None = None
    threshold: float | str | None = None
    detail: str = ""


@dataclass
class StrategyResult:
    outcome: Outcome
    as_of: datetime
    explanation: str
    rules: list[RuleCheck] = field(default_factory=list)
    signal_time: datetime | None = None  # close time of the deciding candle
    entry: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    opening_range: OpeningRange | None = None
    research_flags: list[str] = field(default_factory=list)  # e.g. bearish breakdown

    def rules_json(self) -> list[dict]:
        return [asdict(r) for r in self.rules]


def evaluate(
    candles: pd.DataFrame,
    history: pd.DataFrame,
    as_of: datetime,
    regime: Regime | Callable[[datetime], Regime] = Regime.UNCERTAIN,
    params: OrbParams = OrbParams(),
    tick_size: float = 0.05,
) -> StrategyResult:
    """Evaluate one instrument's session as of `as_of` (tz-aware).

    candles: 5m candles (any range; only today's completed ones are used).
    history: prior sessions' candles for relative volume.
    """
    p = params
    step = timedelta(minutes=p.interval_minutes)
    day = as_of.astimezone(IST).date()
    rules: list[RuleCheck] = []

    def result(outcome: Outcome, why: str, **kw) -> StrategyResult:
        return StrategyResult(outcome, as_of, why, rules, **kw)

    ts_ist = candles.timestamp_utc.dt.tz_convert(IST)
    done = candles[(ts_ist.dt.date == day) & (candles.timestamp_utc + step <= as_of)]
    done = done.sort_values("timestamp_utc").reset_index(drop=True)

    or_end = datetime.combine(day, SESSION_OPEN, IST) + timedelta(minutes=p.or_minutes)
    if as_of < or_end:
        rules.append(RuleCheck("opening_range_complete", False, detail=f"completes {or_end:%H:%M}"))
        return result(Outcome.NO_TRADE, "Opening range not yet established.")

    # Data integrity: every slot up to as_of present and valid; no partial trust.
    clean, issues = validate_candles(done, p.interval_minutes)
    expected = [s for s in session_slots(day, p.interval_minutes) if s + step <= as_of]
    missing = len(expected) - len(clean)
    rejected = [i for i in issues if i.rejected]
    data_ok = not rejected and missing == 0
    rules.append(
        RuleCheck(
            "data_complete",
            data_ok,
            value=len(clean),
            threshold=len(expected),
            detail="; ".join(f"{i.kind}@{i.timestamp}" for i in rejected[:5]),
        )
    )
    if not data_ok:
        return result(
            Outcome.DATA_INVALID,
            f"Session data incomplete/invalid: {missing} missing, "
            f"{len(rejected)} rejected candles. No signal from bad data.",
        )

    rng = opening_range(clean, p.or_minutes, p.interval_minutes)
    rules.append(
        RuleCheck(
            "opening_range_complete",
            rng is not None,
            value=None if rng is None else f"{rng.low}-{rng.high}",
        )
    )
    if rng is None:
        return result(Outcome.DATA_INVALID, "Opening range candles missing.")

    vwap = session_vwap(clean)
    rvol = (
        relative_volume(clean, history, p.rvol_lookback_sessions, p.rvol_min_sessions)
        if p.require_rvol
        else None
    )
    post = clean[clean.timestamp_utc >= rng.end_utc]
    orh = rng.high
    retest_ceiling = orh * (1 + p.retest_tolerance_pct / 100)
    invalid_floor = orh * (1 - p.invalidation_pct / 100)

    phase, since, retest = "AWAIT_BREAKOUT", 0, None
    for i, c in post.iterrows():
        start_ist = c.timestamp_utc.tz_convert(IST)
        closed_at = (c.timestamp_utc + step).to_pydatetime()
        v = vwap[i]
        stamp = f"{start_ist:%H:%M}"

        if start_ist.time() >= p.entry_cutoff:
            rules.append(RuleCheck("entry_window", False, stamp, f"{p.entry_cutoff:%H:%M}"))
            if phase == "AWAIT_BREAKOUT":
                return result(
                    Outcome.NO_TRADE, "No breakout before entry cutoff.", opening_range=rng
                )
            return result(
                Outcome.SETUP_INVALIDATED,
                f"Entry cutoff reached in {phase}.",
                signal_time=closed_at,
                opening_range=rng,
            )

        if phase == "AWAIT_BREAKOUT":
            if c.close < rng.low:
                rules.append(RuleCheck("bearish_breakdown", True, c.close, rng.low, stamp))
                return result(
                    Outcome.NO_TRADE,
                    f"{stamp} closed below ORL {rng.low}: bearish breakdown "
                    "(research only; long-only system, no short orders).",
                    signal_time=closed_at,
                    opening_range=rng,
                    research_flags=["BEARISH_BREAKDOWN"],
                )
            if c.close <= orh:
                continue
            rules.append(RuleCheck("breakout_close_above_orh", True, c.close, orh, stamp))
            if rvol is not None and pd.isna(rvol[i]):
                rules.append(
                    RuleCheck(
                        "relative_volume",
                        False,
                        None,
                        p.rvol_threshold,
                        "insufficient comparable history",
                    )
                )
                return result(
                    Outcome.DATA_INVALID,
                    f"Breakout at {stamp} but relative volume cannot be computed "
                    f"(< {p.rvol_min_sessions} prior sessions for this slot).",
                    signal_time=closed_at,
                    opening_range=rng,
                )
            # regime as known at the breakout candle close, never a later value
            reg = regime(closed_at) if callable(regime) else regime
            checks = [RuleCheck("breakout_above_vwap", bool(c.close > v), c.close, round(v, 4))]
            if rvol is not None:
                checks.append(
                    RuleCheck(
                        "relative_volume",
                        bool(rvol[i] >= p.rvol_threshold),
                        round(float(rvol[i]), 3),
                        p.rvol_threshold,
                    )
                )
            checks += [
                RuleCheck(
                    "regime_allows_long",
                    reg not in p.blocked_regimes,
                    reg.value,
                    ",".join(p.blocked_regimes),
                ),
            ]
            rules.extend(checks)
            failed = [r.rule for r in checks if not r.passed]
            if failed:
                return result(
                    Outcome.BREAKOUT_REJECTED,
                    f"Breakout at {stamp} rejected: {', '.join(failed)} failed.",
                    signal_time=closed_at,
                    opening_range=rng,
                )
            phase, since = "AWAIT_RETEST", 0
            continue

        since += 1
        if c.close < invalid_floor or c.close < v:
            rules.append(
                RuleCheck(
                    "hold_above_level", False, c.close, round(max(invalid_floor, v), 4), stamp
                )
            )
            return result(
                Outcome.SETUP_INVALIDATED,
                f"{stamp} closed {c.close} back below breakout level/VWAP.",
                signal_time=closed_at,
                opening_range=rng,
            )

        if phase == "AWAIT_RETEST":
            if c.low <= retest_ceiling:
                rules.append(
                    RuleCheck("retest_near_orh", True, c.low, round(retest_ceiling, 4), stamp)
                )
                phase, since, retest = "AWAIT_CONFIRM", 0, c
            elif since >= p.max_retest_candles:
                rules.append(
                    RuleCheck(
                        "retest_near_orh", False, None, p.max_retest_candles, "no retest in time"
                    )
                )
                return result(
                    Outcome.SETUP_INVALIDATED,
                    "No retest within allowed candles.",
                    signal_time=closed_at,
                    opening_range=rng,
                )
            continue

        # AWAIT_CONFIRM
        if c.close > c.open and c.close > retest.high and c.close > v:
            rules.append(RuleCheck("buyer_confirmation", True, c.close, retest.high, stamp))
            return _levels(rules, as_of, c, retest, rng, p, tick_size, closed_at)
        if c.low < retest.low:  # deeper retest becomes the reference
            retest, since = c, 0
        elif since >= p.max_confirm_candles:
            rules.append(
                RuleCheck(
                    "buyer_confirmation",
                    False,
                    None,
                    p.max_confirm_candles,
                    "no confirmation in time",
                )
            )
            return result(
                Outcome.SETUP_INVALIDATED,
                "No buyer confirmation after retest.",
                signal_time=closed_at,
                opening_range=rng,
            )

    if phase == "AWAIT_BREAKOUT":
        return result(Outcome.NO_TRADE, "No breakout of the opening range yet.", opening_range=rng)
    waiting = "retest" if phase == "AWAIT_RETEST" else "buyer confirmation after retest"
    return result(
        Outcome.RETEST_PENDING, f"Breakout accepted; awaiting {waiting}.", opening_range=rng
    )


def _levels(rules, as_of, c, retest, rng, p: OrbParams, tick_size, closed_at) -> StrategyResult:
    tick = Decimal(str(tick_size))
    entry = Decimal(str(c.close))
    raw_stop = Decimal(str(retest.low)) * (1 - Decimal(str(p.stop_buffer_pct)) / 100)
    stop = (raw_stop / tick).to_integral_value(ROUND_FLOOR) * tick
    risk = entry - stop
    target = ((entry + risk * Decimal(str(p.target_r_multiple))) / tick).to_integral_value(
        ROUND_CEILING
    ) * tick
    stop_pct = float(risk / entry * 100)
    ok = p.min_stop_pct <= stop_pct <= p.max_stop_pct
    rules.append(
        RuleCheck("stop_distance_pct", ok, round(stop_pct, 3), f"{p.min_stop_pct}-{p.max_stop_pct}")
    )
    levels = dict(
        signal_time=closed_at,
        entry=float(entry),
        stop_loss=float(stop),
        target=float(target),
        opening_range=rng,
    )
    if not ok:
        return StrategyResult(
            Outcome.NO_TRADE,
            as_of,
            f"Setup found but stop distance {stop_pct:.2f}% is outside "
            "allowed range; risk-reward profile unacceptable.",
            rules,
            **levels,
        )
    return StrategyResult(
        Outcome.BUY_SETUP,
        as_of,
        f"Long setup: breakout above ORH {rng.high}, retest low {retest.low}, confirmation close "
        f"{c.close}. Entry {entry}, stop {stop}, target {target} ({p.target_r_multiple}R). "
        "Pending risk-engine approval.",
        rules,
        **levels,
    )

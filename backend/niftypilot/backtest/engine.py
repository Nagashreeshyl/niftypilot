"""Deterministic event-driven backtester for the ORB+VWAP strategy. Simulation only.

Per trading day (weekday, not a loaded holiday):
  1. Universe = point-in-time index members (if configured) with data that day, minus
     index/restricted instruments. Scanner keeps the `max_candidates` with the highest median
     traded value over PRIOR sessions only.
  2. For each candidate the strategy's first decisive outcome and its time are found
     (`day_signal`). This uses only candles completed by that time (look-ahead tested), so a
     BUY_SETUP is known to the loop exactly when its signal candle closes, never earlier.
  3. Event loop over the session's 5m slots. For candle k (start s, close s+5):
       a. pending entries (marketable limit = signal entry + max_entry_gap_r x risk, sized at
          signal time against that worst-case price) fill at k's open or are rejected;
       b. square-off at/after `square_off`; otherwise stop/target checks on k;
       c. mark-to-market at k's close;
       d. BUY_SETUPs whose signal candle closed at s+5 go to the risk engine and, if approved,
          become pending entries for candle k+1.
Capital is a fixed allocation (no compounding); risk limits come from RiskConfig unchanged.
Consecutive-loss and daily-loss limits are session-scoped (reset each trading day).
"""

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from pydantic import BaseModel

from niftypilot.backtest.execution import (
    ExecutionModel,
    check_exit,
    entry_fill,
    entry_limit,
    market_exit_price,
)
from niftypilot.backtest.market import MarketData
from niftypilot.market_data.session import IST, SESSION_CLOSE, session_slots
from niftypilot.risk.charges import leg_charges, resolve
from niftypilot.risk.engine import AccountState, RiskConfig, evaluate_trade
from niftypilot.strategy.orb_vwap import OrbParams, Outcome, StrategyResult, evaluate
from niftypilot.strategy.regime import Regime, RegimeParams, classify_regime

D = Decimal
STEP = timedelta(minutes=5)
NOT_DECIDED = {Outcome.NO_TRADE, Outcome.RETEST_PENDING}


class ScannerConfig(BaseModel):
    max_candidates: int = 5
    lookback_sessions: int = 20
    min_sessions: int = 10
    min_median_traded_value: Decimal = D("0")  # Rs; unvalidated, so off by default


class BacktestConfig(BaseModel):
    strategy: OrbParams = OrbParams()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionModel = ExecutionModel()
    scanner: ScannerConfig = ScannerConfig()
    regime: RegimeParams = RegimeParams()
    broker: str = "groww"


@dataclass
class Trade:
    symbol: str
    session: date
    signal_time: datetime
    entry_time: datetime
    entry_price: D
    qty: int
    requested_qty: int
    stop: D
    target: D
    exit_time: datetime | None = None
    exit_price: D | None = None
    exit_reason: str = ""
    ambiguous_bar: bool = False
    regime: str = ""
    entry_charges: D = D("0")
    exit_charges: D = D("0")
    spread_cost: D = D("0")  # assumed half-spread x reference price x qty, market fills only
    slippage_cost: D = D("0")  # rest of the adverse fill vs reference (incl. tick rounding)
    fees_unverified: tuple[str, ...] = ()

    @property
    def gross_pnl(self) -> D:
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def charges(self) -> D:
        return self.entry_charges + self.exit_charges

    @property
    def net_pnl(self) -> D:
        return self.gross_pnl - self.charges

    @property
    def holding_minutes(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 60

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(
            gross_pnl=self.gross_pnl,
            charges=self.charges,
            net_pnl=self.net_pnl,
            holding_minutes=self.holding_minutes,
        )
        return {k: (str(v) if isinstance(v, D | date | datetime) else v) for k, v in d.items()}


@dataclass
class BacktestResult:
    config: BacktestConfig
    start: date
    end: date
    trades: list[Trade]
    equity: list[tuple[datetime, D, D]]  # (candle close, MTM equity, invested notional)
    daily_equity: dict[date, D]
    orders: list[dict]  # every rejected/partial/cancelled entry with reason
    signals: list[dict]  # every candidate's decisive outcome per day
    days: list[dict]  # per-day log (universe, candidates, regime, skipped reason)
    fees_unverified: set[str] = field(default_factory=set)
    data_kind: str = ""


def day_signal(
    day_df: pd.DataFrame, hist: pd.DataFrame, day: date, p: OrbParams, regime_at, tick: float
) -> StrategyResult:
    """First decisive outcome of the session. Equivalent to evaluating at every candle close
    and stopping at the first decided outcome (tested against that brute force)."""
    close = datetime.combine(day, SESSION_CLOSE, IST).astimezone(UTC)
    r = evaluate(day_df, hist, close, regime_at, p, tick)
    if r.outcome != Outcome.DATA_INVALID or r.signal_time is not None:
        return r
    # A gap somewhere: replay so a setup completed before the gap is still seen.
    for s in session_slots(day):
        ri = evaluate(day_df, hist, s + STEP, regime_at, p, tick)
        if ri.outcome not in NOT_DECIDED or ri.signal_time is not None:
            return ri
    return r


def _regime_fn(m: MarketData, day: date, prev_day: date | None, rp: RegimeParams):
    if m.benchmark is None:
        return lambda t: Regime.UNCERTAIN
    bench = m.by_day.get(m.benchmark, {})
    today = bench.get(day)
    prev = bench.get(prev_day) if prev_day else None
    prev_close = float(prev.close.iloc[-1]) if prev is not None and len(prev) else None
    cache: dict[datetime, Regime] = {}

    def at(t: datetime) -> Regime:
        if t not in cache:
            if today is None:
                cache[t] = Regime.UNCERTAIN
            else:
                cache[t] = classify_regime(today[today.timestamp_utc + STEP <= t], prev_close, rp)
        return cache[t]

    return at


def run_backtest(m: MarketData, cfg: BacktestConfig, start: date, end: date) -> BacktestResult:
    if cfg.strategy.blocked_regimes and m.benchmark is None:
        raise ValueError("strategy uses a regime filter but no benchmark series was provided")
    ex, rc, sp = cfg.execution, cfg.risk, cfg.strategy
    # risk sizing must assume the same adverse costs the simulator applies
    risk_cfg = rc.model_copy(update={"slippage_pct": ex.adverse_pct})
    params_key = json.dumps(
        [sp.model_dump(mode="json"), cfg.regime.model_dump(mode="json")], sort_keys=True
    )
    cash = rc.capital
    trades: list[Trade] = []
    equity: list[tuple[datetime, D, D]] = []
    daily_equity: dict[date, D] = {}
    orders, signals, days = [], [], []
    fees_unverified: set[str] = set()

    traded_value = m.traded_value()
    all_days = sorted({d for per in m.by_day.values() for d in per})
    day = start
    while day <= end:
        today, day = day, day + timedelta(days=1)
        if today.weekday() >= 5 or today in m.holidays:
            continue
        log = {"date": str(today)}
        days.append(log)
        if today in m.special_sessions:
            log["skipped"] = "non-regular session (exchange calendar)"
            continue
        members = m.membership(today) if m.membership else set(m.info)
        eligible = sorted(
            s
            for s in members
            if s in traded_value and today in m.by_day[s] and not m.info[s].restricted
        )
        if not eligible:
            log["skipped"] = "no data for any eligible instrument"
            continue
        ranked = []
        for s in eligible:
            tv = traded_value[s]
            prior = tv[tv.index < today].tail(cfg.scanner.lookback_sessions)
            if len(prior) >= cfg.scanner.min_sessions and prior.median() >= float(
                cfg.scanner.min_median_traded_value
            ):
                ranked.append((-prior.median(), s))
        candidates = [s for _, s in sorted(ranked)[: cfg.scanner.max_candidates]]
        prev_days = [d for d in all_days if d < today]
        regime_at = _regime_fn(m, today, prev_days[-1] if prev_days else None, cfg.regime)
        log.update(
            eligible=len(eligible),
            candidates=candidates,
            regime_eod=str(regime_at(datetime.combine(today, SESSION_CLOSE, IST).astimezone(UTC)))
            if m.benchmark
            else "UNCERTAIN (no benchmark)",
        )
        fees = resolve(today, cfg.broker)
        fees_unverified |= set(fees.unverified)

        setups: dict[datetime, list[tuple[str, StrategyResult]]] = {}
        for s in candidates:
            key = (s, today, params_key)
            if key not in m.signal_cache:
                hist = m.history(s, today, sp.rvol_lookback_sessions)
                m.signal_cache[key] = day_signal(
                    m.by_day[s][today], hist, today, sp, regime_at, m.info[s].tick_size
                )
            r = copy.deepcopy(m.signal_cache[key])
            signals.append(
                {
                    "date": str(today),
                    "symbol": s,
                    "outcome": r.outcome.value,
                    "time": str(r.signal_time) if r.signal_time else None,
                    "explanation": r.explanation,
                }
            )
            if r.outcome == Outcome.BUY_SETUP:
                setups.setdefault(r.signal_time, []).append((s, r))

        open_pos: dict[str, Trade] = {}
        pending: list[dict] = []
        traded_today: set[str] = set()
        st = {"trades": 0, "consec": 0, "pnl": D("0")}

        def close_trade(
            t: Trade,
            when: datetime,
            price: D,
            reason: str,
            ref: D,
            ambiguous: bool = False,
            market: bool = True,
            st=st,
            open_pos=open_pos,
            fees=fees,
        ) -> None:
            nonlocal cash
            t.exit_time, t.exit_price, t.exit_reason = when, price, reason
            t.ambiguous_bar = ambiguous
            t.exit_charges = leg_charges("sell", price, t.qty, fees).total
            spread = ref * ex.half_spread_pct / 100 * t.qty if market else D("0")
            t.spread_cost += spread
            t.slippage_cost += max(ref - price, D("0")) * t.qty - spread
            cash += price * t.qty - t.exit_charges
            st["pnl"] += t.net_pnl
            st["consec"] = st["consec"] + 1 if t.net_pnl < 0 else 0
            trades.append(open_pos.pop(t.symbol))

        last_close: dict[str, D] = {}
        for slot in session_slots(today):
            bars = {s: m.bar(s, today, slot) for s in sorted(set(candidates) | set(open_pos))}
            squaring = slot.astimezone(IST).time() >= ex.square_off
            # a. pending entries due at this candle fill at its open
            waiting = []
            for o in pending:
                if o["fill_at"] > slot:
                    waiting.append(o)
                    continue
                bar = bars.get(o["symbol"])
                if bar is None or squaring:
                    orders.append(
                        {
                            **o,
                            "status": "cancelled",
                            "reason": "no candle" if bar is None else "square-off time",
                        }
                    )
                    continue
                tick = D(str(m.info[o["symbol"]].tick_size))
                f = entry_fill(
                    ex, _d(bar.open), int(bar.volume), o["limit"], o["stop"], o["qty"], tick
                )  # rejects opens above the order's limit
                qty = f.qty
                # never spend cash we do not have (another entry may have filled this open)
                while qty and f.price * qty + leg_charges("buy", f.price, qty, fees).total > cash:
                    qty -= 1
                if f.status == "rejected" or qty < 1:
                    orders.append(
                        {
                            **o,
                            "status": "rejected",
                            "reason": f.reason or "insufficient cash at fill",
                        }
                    )
                    continue
                if qty < o["qty"]:
                    orders.append(
                        {
                            **o,
                            "status": "partial",
                            "filled": qty,
                            "reason": f.reason or "reduced to available cash",
                        }
                    )
                t = Trade(
                    o["symbol"],
                    today,
                    o["signal_time"],
                    slot,
                    f.price,
                    qty,
                    o["qty"],
                    o["stop"],
                    o["target"],
                    regime=o["regime"],
                    fees_unverified=fees.unverified,
                )
                t.entry_charges = leg_charges("buy", f.price, qty, fees).total
                t.spread_cost = _d(bar.open) * ex.half_spread_pct / 100 * qty
                t.slippage_cost = (f.price - _d(bar.open)) * qty - t.spread_cost
                cash -= f.price * qty + t.entry_charges
                open_pos[t.symbol] = t
                traded_today.add(t.symbol)
                st["trades"] += 1
            pending = waiting
            # b. exits
            for s, t in list(open_pos.items()):
                bar = bars.get(s)
                if bar is None:
                    continue
                tick = D(str(m.info[s].tick_size))
                if squaring:
                    px = market_exit_price(ex, _d(bar.open), tick)
                    close_trade(t, slot, px, "square_off", _d(bar.open))
                    continue
                f = check_exit(
                    ex,
                    _d(bar.open),
                    _d(bar.high),
                    _d(bar.low),
                    t.stop,
                    t.target,
                    tick,
                    entry_candle=t.entry_time == slot,
                )
                if f:
                    ref = (
                        t.stop
                        if f.reason == "stop"
                        else (_d(bar.open) if f.reason == "stop_gap" else f.price)
                    )
                    close_trade(
                        t,
                        slot + STEP,
                        f.price,
                        f.reason,
                        ref,
                        f.ambiguous,
                        market=f.reason.startswith("stop"),
                    )
            # c. mark to market at candle close
            for s, bar in bars.items():
                if bar is not None:
                    last_close[s] = _d(bar.close)
            invested = sum(
                (last_close.get(s, t.entry_price) * t.qty for s, t in open_pos.items()), D("0")
            )
            equity.append((slot + STEP, cash + invested, invested))
            # d. signals whose candle just closed -> risk engine -> pending entry
            for s, r in sorted(setups.get(slot + STEP, []), key=lambda x: x[0]):
                if s in open_pos or s in traded_today or squaring:
                    orders.append(
                        {
                            "symbol": s,
                            "status": "skipped",
                            "signal_time": str(r.signal_time),
                            "reason": "duplicate/after square-off",
                        }
                    )
                    continue
                state = _state(cash, st, open_pos, len(pending))
                # size against the worst fill the limit allows, so a stop-out stays in budget
                tick = D(str(m.info[s].tick_size))
                limit = entry_limit(ex, _d(r.entry), _d(r.stop_loss), tick)
                dec = evaluate_trade(limit, r.stop_loss, r.target, state, risk_cfg, fees)
                o = {
                    "symbol": s,
                    "signal_time": r.signal_time,
                    "entry": _d(r.entry),
                    "stop": _d(r.stop_loss),
                    "target": _d(r.target),
                    "qty": dec.quantity,
                    "limit": limit,
                    "fill_at": r.signal_time + (ex.entry_delay_bars - 1) * STEP,
                    "regime": str(regime_at(r.signal_time)),
                }
                if not dec.approved:
                    orders.append({**o, "status": "risk_rejected", "reason": ",".join(dec.reasons)})
                    continue
                pending.append(o)
        for o in pending:
            orders.append({**o, "status": "cancelled", "reason": "session ended"})
        for s, t in list(open_pos.items()):  # only when square-off candle was missing
            px = last_close.get(s, t.entry_price)
            close_trade(
                t,
                datetime.combine(today, SESSION_CLOSE, IST).astimezone(UTC),
                market_exit_price(ex, px, D(str(m.info[s].tick_size))),
                "data_gap_close",
                px,
            )
        daily_equity[today] = cash
    return BacktestResult(
        cfg,
        start,
        end,
        trades,
        equity,
        daily_equity,
        [_jsonable(o) for o in orders],
        signals,
        days,
        fees_unverified,
        m.dataset.get("data_kind", ""),
    )


def _state(cash: D, st: dict, open_pos: dict, n_pending: int) -> AccountState:
    return AccountState(
        available_cash=cash,
        trades_today=st["trades"] + n_pending,
        consecutive_losses=st["consec"],
        realised_pnl_today=st["pnl"],
        open_positions=len(open_pos) + n_pending,
    )


def _d(x) -> D:
    return D(str(round(float(x), 4)))


def _jsonable(o: dict) -> dict:
    return {k: (str(v) if isinstance(v, D | datetime | date) else v) for k, v in o.items()}

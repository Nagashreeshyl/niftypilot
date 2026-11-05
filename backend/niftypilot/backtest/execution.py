"""Fill model for 5-minute OHLC replay. Every ambiguity resolves against the strategy.

Entry: marketable limit order at the OPEN of the candle `entry_delay_bars` after the signal
  candle (default 1 = the next candle), plus
  half-spread and slippage, rounded up to tick. limit = signal entry + max_entry_gap_r x
  (signal entry - stop), so a worse entry can add at most that fraction of planned risk. Not
  filled if the open is above the limit or at/below the stop. Fill size is capped at
  participation_pct of that candle's volume (partial fill; remainder cancelled).
Stop (stop-market): if a candle OPENS at/below the stop, fill at that open (gap-through);
  otherwise fill at the stop. Both pay half-spread + slippage, rounded down to tick.
Target (resting limit): fills at the target only if the high trades THROUGH it by
  `target_trade_through_ticks`; a gap above the target still fills at the target (we do not
  assume price improvement).
Same candle touches stop and target: the stop is assumed to have come first. On the entry
  candle the open is our fill, so only the rest of that candle is considered (same rule).
Square-off: market sell at the open of the first candle starting at/after `square_off`
  (Groww auto-squares off at 15:20 and charges Rs 50 + GST; we exit before that).
"""

from dataclasses import dataclass
from datetime import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from pydantic import BaseModel, Field

D = Decimal


class ExecutionModel(BaseModel):
    slippage_pct: Decimal = Field(D("0.05"), ge=0)  # per market/stop fill, adverse
    half_spread_pct: Decimal = Field(D("0.025"), ge=0)  # ASSUMED: no bid/ask data available
    participation_pct: Decimal = Field(D("10"), gt=0, le=100)  # of fill-candle volume
    max_entry_gap_r: Decimal = Field(D("0.10"), ge=0)  # fraction of planned risk/share
    target_trade_through_ticks: int = Field(1, ge=0)
    # 1 = open of the candle right after the signal candle; 2 = one candle later (latency)
    entry_delay_bars: int = Field(1, ge=1, le=6)
    square_off: time = time(15, 15)

    @property
    def adverse_pct(self) -> Decimal:
        return self.slippage_pct + self.half_spread_pct


def to_tick(x: D, tick: D, up: bool) -> D:
    return (x / tick).to_integral_value(ROUND_CEILING if up else ROUND_FLOOR) * tick


@dataclass(frozen=True)
class EntryFill:
    status: str  # filled | partial | rejected
    price: D | None
    qty: int
    reason: str = ""


def entry_limit(m: ExecutionModel, signal_entry: D, stop: D, tick: D) -> D:
    return to_tick(signal_entry + m.max_entry_gap_r * (signal_entry - stop), tick, up=True)


def entry_fill(
    m: ExecutionModel, open_: D, volume: int, limit: D, stop: D, qty: int, tick: D
) -> EntryFill:
    if open_ <= stop:
        return EntryFill("rejected", None, 0, f"open {open_} at/below stop {stop}")
    if open_ > limit:
        return EntryFill("rejected", None, 0, f"open {open_} above limit {limit}")
    cap = int((D(volume) * m.participation_pct / 100).to_integral_value(ROUND_FLOOR))
    if cap < 1:
        return EntryFill("rejected", None, 0, f"no liquidity: candle volume {volume}")
    price = to_tick(open_ * (1 + m.adverse_pct / 100), tick, up=True)
    if qty > cap:
        return EntryFill("partial", price, cap, f"capped at {m.participation_pct}% of {volume}")
    return EntryFill("filled", price, qty)


@dataclass(frozen=True)
class ExitFill:
    price: D
    reason: str  # stop | stop_gap | target | square_off | data_gap_close
    ambiguous: bool = False


def check_exit(
    m: ExecutionModel, o: D, h: D, low: D, stop: D, target: D, tick: D, entry_candle: bool
) -> ExitFill | None:
    def stop_px(level: D) -> D:
        return to_tick(level * (1 - m.adverse_pct / 100), tick, up=False)

    if not entry_candle and o <= stop:
        return ExitFill(stop_px(o), "stop_gap")
    if not entry_candle and o >= target:
        return ExitFill(target, "target_gap")
    stop_hit = low <= stop
    target_hit = h >= target + m.target_trade_through_ticks * tick
    if stop_hit:
        return ExitFill(stop_px(stop), "stop", ambiguous=target_hit)
    if target_hit:
        return ExitFill(target, "target")
    return None


def market_exit_price(m: ExecutionModel, price: D, tick: D) -> D:
    return to_tick(price * (1 - m.adverse_pct / 100), tick, up=False)

"""Pre-trade risk gate. Has veto over every strategy; nothing places orders here.

Sizing: qty = floor(risk_budget / risk_per_share), where risk per share includes slippage on
entry and stop. Then qty is reduced until the full loss at stop (incl. charges) fits the
budget, and capped by affordable notional. If one share does not fit, the trade is rejected:
small capital never justifies more risk.
"""

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from niftypilot.risk.charges import ChargeSchedule, round_trip_charges

D = Decimal


class SystemState(StrEnum):
    ACTIVE = "ACTIVE"
    STOP_NEW_TRADES = "STOP_NEW_TRADES"
    PAUSED = "PAUSED"


class RiskConfig(BaseModel):
    capital: Decimal = Field(D("5000"), gt=0)
    risk_pct: Decimal = Field(D("0.5"), gt=0, le=1)  # of trading capital, per trade
    max_trades_per_day: int = 2
    max_consecutive_losses: int = 2
    max_daily_loss_pct: Decimal = D("2")
    max_open_positions: int = 1
    max_capital_utilisation_pct: Decimal = Field(D("100"), gt=0, le=100)  # no margin, ever
    slippage_pct: Decimal = D("0.05")  # per side
    min_net_reward_risk: Decimal = D("1.0")  # after costs and slippage


class AccountState(BaseModel):
    available_cash: Decimal
    trades_today: int = 0
    consecutive_losses: int = 0
    realised_pnl_today: Decimal = D("0")
    open_positions: int = 0
    system_state: SystemState = SystemState.ACTIVE


@dataclass
class RiskDecision:
    approved: bool
    quantity: int
    checks: list[dict] = field(default_factory=list)
    est_charges_at_stop: Decimal | None = None
    net_loss_at_stop: Decimal | None = None
    net_profit_at_target: Decimal | None = None

    @property
    def reasons(self) -> list[str]:
        return [c["check"] for c in self.checks if not c["passed"]]


def evaluate_trade(
    entry: float,
    stop: float,
    target: float,
    state: AccountState,
    cfg: RiskConfig = RiskConfig(),
    charges: ChargeSchedule = ChargeSchedule(),
) -> RiskDecision:
    e, s, t = D(str(entry)), D(str(stop)), D(str(target))
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        return ok

    def reject() -> RiskDecision:
        return RiskDecision(False, 0, checks)

    gates = [
        check("system_active", state.system_state == SystemState.ACTIVE, state.system_state),
        check("long_levels_valid", s < e < t and s > 0, f"stop {s} < entry {e} < target {t}"),
        check(
            "max_trades_per_day",
            state.trades_today < cfg.max_trades_per_day,
            f"{state.trades_today}/{cfg.max_trades_per_day}",
        ),
        check(
            "max_consecutive_losses",
            state.consecutive_losses < cfg.max_consecutive_losses,
            f"{state.consecutive_losses}/{cfg.max_consecutive_losses}",
        ),
        check(
            "daily_loss_limit",
            state.realised_pnl_today > -cfg.capital * cfg.max_daily_loss_pct / 100,
            f"pnl {state.realised_pnl_today}",
        ),
        check(
            "max_open_positions",
            state.open_positions < cfg.max_open_positions,
            f"{state.open_positions}/{cfg.max_open_positions}",
        ),
    ]
    if not all(gates):
        return reject()

    slip = cfg.slippage_pct / 100
    fill_in, fill_stop, fill_tgt = e * (1 + slip), s * (1 - slip), t * (1 - slip)
    budget = cfg.capital * cfg.risk_pct / 100
    qty = int((budget / (fill_in - fill_stop)).to_integral_value(ROUND_FLOOR))
    spendable = min(state.available_cash, cfg.capital * cfg.max_capital_utilisation_pct / 100)
    afford = int((spendable / fill_in).to_integral_value(ROUND_FLOOR))
    qty = min(qty, afford)

    def loss(q: int) -> tuple[D, D]:
        c = round_trip_charges(fill_in, fill_stop, q, charges).total
        return (fill_in - fill_stop) * q + c, c

    while qty > 0 and loss(qty)[0] > budget:
        qty -= 1
    if not check(
        "min_one_share_within_budget",
        qty >= 1,
        f"budget {budget:.2f}, affordable {afford}, entry {e}",
    ):
        return reject()

    loss_at_stop, c_stop = loss(qty)
    profit = (fill_tgt - fill_in) * qty - round_trip_charges(fill_in, fill_tgt, qty, charges).total
    rr = profit / loss_at_stop
    ok = check(
        "net_reward_risk",
        profit > 0 and rr >= cfg.min_net_reward_risk,
        f"net profit {profit:.2f} / net loss {loss_at_stop:.2f} = {rr:.2f}",
    )
    q2 = lambda x: x.quantize(D("0.01"))  # noqa: E731
    return RiskDecision(ok, qty if ok else 0, checks, q2(c_stop), q2(loss_at_stop), q2(profit))

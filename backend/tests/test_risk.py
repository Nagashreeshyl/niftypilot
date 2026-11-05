from decimal import Decimal as D

import pytest

from niftypilot.risk.charges import ChargeSchedule
from niftypilot.risk.engine import AccountState, RiskConfig, SystemState, evaluate_trade

FREE = ChargeSchedule.zero()
NO_SLIP = dict(slippage_pct=D("0"))


def cfg(**kw):
    return RiskConfig(**{**NO_SLIP, "capital": D("5000"), "risk_pct": D("1"), **kw})


def cash(x="5000", **kw):
    return AccountState(available_cash=D(x), **kw)


def test_position_size_formula():
    d = evaluate_trade(100, 99, 102, cash(), cfg(), FREE)  # budget 50 / risk 1
    assert d.approved and d.quantity == 50
    d = evaluate_trade(100, 98, 104, cash(), cfg(), FREE)
    assert d.quantity == 25


def test_quantity_capped_by_available_cash_no_margin():
    d = evaluate_trade(1000, 999.5, 1001, cash(), cfg(), FREE)  # risk allows 100, cash 5
    assert d.approved and d.quantity == 5
    d = evaluate_trade(1000, 999.5, 1001, cash("2500"), cfg(), FREE)
    assert d.quantity == 2


def test_charges_reduce_quantity_to_keep_loss_within_budget():
    d = evaluate_trade(100, 99, 103, cash(), cfg(), ChargeSchedule())
    assert d.quantity < 50 and d.net_loss_at_stop <= D("50")


@pytest.mark.parametrize(
    "state,reason",
    [
        (cash(system_state=SystemState.PAUSED), "system_active"),
        (cash(system_state=SystemState.STOP_NEW_TRADES), "system_active"),
        (cash(trades_today=2), "max_trades_per_day"),
        (cash(consecutive_losses=2), "max_consecutive_losses"),
        (cash(realised_pnl_today=D("-100")), "daily_loss_limit"),
        (cash(open_positions=1), "max_open_positions"),
    ],
)
def test_gates_reject(state, reason):
    d = evaluate_trade(100, 99, 102, state, cfg(), FREE)
    assert not d.approved and d.quantity == 0 and reason in d.reasons


@pytest.mark.parametrize("entry,stop,target", [(100, 101, 102), (100, 99, 99.5), (100, 0, 102)])
def test_invalid_long_levels_rejected(entry, stop, target):
    d = evaluate_trade(entry, stop, target, cash(), cfg(), FREE)
    assert not d.approved and "long_levels_valid" in d.reasons


def test_unaffordable_share_rejected_not_upsized():
    d = evaluate_trade(2500, 2490, 2520, cash("1000"), cfg(capital=D("1000")), FREE)
    assert not d.approved and "min_one_share_within_budget" in d.reasons


def test_risk_budget_too_small_for_stop_rejected():
    d = evaluate_trade(100, 90, 120, cash("1000"), cfg(capital=D("1000"), risk_pct=D("0.5")), FREE)
    assert not d.approved and "min_one_share_within_budget" in d.reasons  # 5 budget < 10 risk


def test_costs_can_make_small_capital_trade_uneconomical():
    d = evaluate_trade(101.6, 100.95, 102.9, cash(), RiskConfig(), ChargeSchedule())
    assert not d.approved and d.reasons == ["net_reward_risk"]
    assert d.net_profit_at_target < d.net_loss_at_stop


@pytest.mark.parametrize(
    "kw",
    [
        {"risk_pct": D("1.5")},
        {"risk_pct": D("0")},
        {"max_capital_utilisation_pct": D("150")},
        {"capital": D("0")},
    ],
)
def test_config_cannot_exceed_hard_limits(kw):
    with pytest.raises(ValueError):
        RiskConfig(**kw)

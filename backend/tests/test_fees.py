"""Charge examples worked by hand from Groww's published rates (fetched 2026-11-04).

These verify the formula, not a broker contract note. Reconcile one real Groww contract
note before treating costs as authoritative.
"""

from datetime import date
from decimal import Decimal as D

import pytest

from niftypilot.risk.charges import (
    ChargeSchedule,
    leg_charges,
    resolve,
    round_trip_charges,
)

NOW = ChargeSchedule()


def parts(c):
    return (c.brokerage, c.stt, c.exchange, c.sebi, c.ipft, c.stamp, c.gst, c.total)


def test_buy_leg_minimum_brokerage():
    # value 1000: 0.1% = 1.00 < 5 -> min(5, 2.5% = 25) = 5; exch 0.0297; stamp 0.03
    # GST 18% x (5 + 0.0297 + 0.001 + 0.001) = 0.9056
    c = leg_charges("buy", D("100"), 10, NOW)
    assert parts(c) == tuple(map(D, ["5.00", "0", "0.03", "0.00", "0.00", "0.03", "0.91", "5.97"]))


def test_sell_leg_has_stt_not_stamp():
    # value 1010: brokerage 5; STT 0.025% = 0.2525; exch 0.0300; GST 0.9056
    c = leg_charges("sell", D("101"), 10, NOW)
    assert parts(c) == tuple(map(D, ["5.00", "0.25", "0.03", "0.00", "0.00", "0", "0.91", "6.19"]))


def test_tiny_order_minimum_is_2_5_percent_of_value():
    # value 100: 0.1% = 0.10 < 5 -> min(5, 2.5) = 2.50; GST 18% x 2.5031 = 0.4506
    c = leg_charges("buy", D("50"), 2, NOW)
    assert (c.brokerage, c.gst, c.total) == (D("2.50"), D("0.45"), D("2.95"))


def test_brokerage_cap_large_orders():
    # value 1,00,000: brokerage min(20, 100) = 20; exch 2.97; SEBI 0.10; IPFT 0.10
    # GST 18% x 23.17 = 4.1706
    sell = leg_charges("sell", D("1000"), 100, NOW)
    assert parts(sell) == tuple(
        map(D, ["20.00", "25.00", "2.97", "0.10", "0.10", "0", "4.17", "52.34"])
    )
    buy = leg_charges("buy", D("1000"), 100, NOW)
    assert buy.total == D("30.34")  # 20 + 3 stamp + 2.97 + 0.1 + 0.1 + 4.17
    assert round_trip_charges(D("1000"), D("1000"), 100, NOW).total == D("82.68")


def test_effective_dated_exchange_charge():
    before, after = resolve(date(2024, 9, 30)), resolve(date(2024, 10, 1))
    assert (before.exchange_txn_pct, after.exchange_txn_pct) == (D("0.00322"), D("0.00297"))
    assert leg_charges("buy", D("1000"), 100, before).exchange == D("3.22")


def test_historical_dates_are_not_authoritative():
    s = resolve(date(2023, 6, 1))
    assert not s.authoritative
    assert {"brokerage_pct", "stt_sell_pct", "exchange_txn_pct", "stamp_buy_pct"} <= set(
        s.unverified
    )


def test_secondary_sources_never_authoritative_even_today():
    s = resolve(date(2026, 11, 4))
    assert s.unverified == ("exchange_txn_pct", "stamp_buy_pct")


def test_default_schedule_matches_resolved_current_rates():
    s = resolve(date(2026, 11, 4))
    assert parts(round_trip_charges(D("101.6"), D("100.95"), 16, s)) == parts(
        round_trip_charges(D("101.6"), D("100.95"), 16, NOW)
    )


def test_bad_side_rejected():
    with pytest.raises(ValueError):
        leg_charges("short", D("1"), 1, NOW)

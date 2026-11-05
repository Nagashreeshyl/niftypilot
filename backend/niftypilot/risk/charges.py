"""Effective-dated intraday equity (NSE, MIS) transaction costs.

Every rate is a RateEntry with its party (broker / exchange / statutory), effective dates,
verification status and source. `resolve(trade_date)` turns the table into a ChargeSchedule
for that date and lists the components whose rate is NOT authoritative for that date.

Status meanings:
  VERIFIED     primary official source states the rate AND its effective dates.
  SECONDARY    rate and dates come from a reputable secondary source (e.g. a broker bulletin
               citing a circular); the official document was not retrieved.
  CURRENT_ONLY a primary source confirms the rate on VERIFIED_ON; when it started is unknown.
               Authoritative only for trades on/after VERIFIED_ON.
No entry is VERIFIED yet, so historical net P&L is never presented as authoritative.

Audit 2026-11-04 vs the M2 values (entered from memory): the brokerage minimum is
"lower of Rs 5 or 2.5% of trade value", not a flat Rs 5; the NSE IPFT charge (0.0001%) was
missing. Not modelled: per-contract-note rounding of STT/stamp duty, the Rs 50 + GST Groww
auto square-off charge (the backtester exits before 15:20), DP charges (nil for intraday).
"""

from dataclasses import dataclass, fields, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

D = Decimal
PAISE = D("0.01")
VERIFIED_ON = date(2026, 11, 4)

GROWW_PRICING = "https://groww.in/pricing/stocks (fetched 2026-11-04)"
GROWW_HELP = (
    "https://groww.in/help/stocks/sx-pricing/what-are-intraday-charges (fetched 2026-11-04)"
)
ZERODHA_NSE_2024 = (
    "https://zerodha.com/marketintel/bulletin/391488 (broker bulletin citing NSE circular "
    "FA64232; NSE original not retrieved)"
)
FINCADEMY_STAMP = (
    "secondary: uniform buy-side stamp duty since 2020-07-01 (Indian Stamp Act amendment)"
)


class Status(StrEnum):
    VERIFIED = "verified"
    SECONDARY = "secondary"
    CURRENT_ONLY = "current_only"


@dataclass(frozen=True)
class RateEntry:
    component: str  # ChargeSchedule field name
    party: str  # broker | exchange | statutory
    value: D
    effective_from: date | None
    effective_until: date | None
    status: Status
    source: str

    def covers(self, d: date) -> bool:
        return (self.effective_from is None or self.effective_from <= d) and (
            self.effective_until is None or d <= self.effective_until
        )

    def authoritative_on(self, d: date) -> bool:
        if self.status == Status.VERIFIED:
            return True
        return self.status == Status.CURRENT_ONLY and d >= VERIFIED_ON


def _e(component, party, value, frm, until, status, source) -> RateEntry:
    return RateEntry(component, party, D(value), frm, until, status, source)


CUR = Status.CURRENT_ONLY
# Broker-specific (Groww intraday equity) and exchange/statutory rates are kept separate.
BROKER_RATES: dict[str, list[RateEntry]] = {
    "groww": [
        _e("brokerage_pct", "broker", "0.1", None, None, CUR, GROWW_PRICING),
        _e("brokerage_cap", "broker", "20", None, None, CUR, GROWW_PRICING),
        _e("brokerage_min", "broker", "5", None, None, CUR, GROWW_HELP),
        _e("brokerage_min_pct", "broker", "2.5", None, None, CUR, GROWW_HELP),
    ]
}
NSE_RATES: list[RateEntry] = [
    _e(
        "exchange_txn_pct",
        "exchange",
        "0.00322",
        None,
        date(2024, 9, 30),
        Status.SECONDARY,
        ZERODHA_NSE_2024,
    ),
    _e(
        "exchange_txn_pct",
        "exchange",
        "0.00297",
        date(2024, 10, 1),
        None,
        Status.SECONDARY,
        ZERODHA_NSE_2024 + "; current rate confirmed by " + GROWW_PRICING,
    ),
    _e("ipft_pct", "exchange", "0.0001", None, None, CUR, GROWW_PRICING),
    _e("stt_sell_pct", "statutory", "0.025", None, None, CUR, GROWW_PRICING),
    _e("sebi_fee_pct", "statutory", "0.0001", None, None, CUR, GROWW_PRICING),
    _e(
        "stamp_buy_pct",
        "statutory",
        "0.003",
        date(2020, 7, 1),
        None,
        Status.SECONDARY,
        FINCADEMY_STAMP + "; current rate confirmed by " + GROWW_PRICING,
    ),
    _e("gst_pct", "statutory", "18", None, None, CUR, GROWW_PRICING),
]


# How each component is computed in the model. "rounding" states what the MODEL does and
# whether the broker's real rounding is known; none of the official rounding rules has been
# verified yet (that needs a real contract note).
COMPONENT_SPEC: dict[str, dict[str, str]] = {
    "brokerage": {
        "party": "broker",
        "side": "buy and sell (per executed order)",
        "base": "order value (price x qty)",
        "formula": "min(cap, pct x value); if < min: min(brokerage_min, brokerage_min_pct x value)",
        "rounding": "model: paise per order; broker rule: UNVERIFIED",
    },
    "stt": {
        "party": "statutory (Govt of India)",
        "side": "sell only (intraday)",
        "base": "sell value",
        "formula": "stt_sell_pct x sell value",
        "rounding": "model: paise per order; official rule: UNVERIFIED (often stated as "
        "rounded to the nearest rupee; not confirmed from source)",
    },
    "exchange": {
        "party": "exchange (NSE)",
        "side": "buy and sell",
        "base": "order value",
        "formula": "exchange_txn_pct x value",
        "rounding": "model: paise per order; broker rule: UNVERIFIED",
    },
    "sebi": {
        "party": "regulator (SEBI)",
        "side": "buy and sell",
        "base": "order value",
        "formula": "sebi_fee_pct x value (Rs 10 per crore)",
        "rounding": "model: paise per order; UNVERIFIED",
    },
    "ipft": {
        "party": "exchange (NSE IPFT)",
        "side": "buy and sell",
        "base": "order value",
        "formula": "ipft_pct x value",
        "rounding": "model: paise per order; UNVERIFIED",
    },
    "stamp": {
        "party": "statutory (state, collected centrally)",
        "side": "buy only",
        "base": "buy value",
        "formula": "stamp_buy_pct x buy value",
        "rounding": "model: paise per order; official rule: UNVERIFIED",
    },
    "gst": {
        "party": "statutory (Govt of India)",
        "side": "buy and sell",
        "base": "brokerage + exchange + SEBI + IPFT",
        "formula": "gst_pct x base (contract notes may split CGST/SGST or show IGST)",
        "rounding": "model: paise per order; UNVERIFIED",
    },
}


@dataclass(frozen=True)
class ChargeSchedule:
    """Rates applicable on one date. Defaults = current Groww/NSE rates (see RATES)."""

    brokerage_pct: D = D("0.1")
    brokerage_cap: D = D("20")
    brokerage_min: D = D("5")
    brokerage_min_pct: D = D("2.5")  # minimum = lower of brokerage_min or this % of value
    stt_sell_pct: D = D("0.025")
    exchange_txn_pct: D = D("0.00297")
    sebi_fee_pct: D = D("0.0001")
    ipft_pct: D = D("0.0001")
    stamp_buy_pct: D = D("0.003")
    gst_pct: D = D("18")  # on brokerage + exchange txn + SEBI + IPFT
    effective_date: date | None = None
    unverified: tuple[str, ...] = ()  # components not authoritative for effective_date

    @property
    def authoritative(self) -> bool:
        return self.effective_date is not None and not self.unverified

    @classmethod
    def zero(cls) -> "ChargeSchedule":
        return cls(**{f: D("0") for f in RATE_FIELDS})


RATE_FIELDS = [f.name for f in fields(ChargeSchedule) if f.type in ("D", D)]


def resolve(trade_date: date, broker: str = "groww") -> ChargeSchedule:
    values, unverified = {}, []
    for comp in RATE_FIELDS:
        entries = [e for e in BROKER_RATES[broker] + NSE_RATES if e.component == comp]
        hits = [e for e in entries if e.covers(trade_date)]
        if len(hits) != 1:
            raise ValueError(f"{comp}: {len(hits)} rate entries cover {trade_date}")
        values[comp] = hits[0].value
        if not hits[0].authoritative_on(trade_date):
            unverified.append(comp)
    return replace(
        ChargeSchedule(), **values, effective_date=trade_date, unverified=tuple(unverified)
    )


@dataclass(frozen=True)
class Charges:
    brokerage: D = D("0")
    stt: D = D("0")
    exchange: D = D("0")
    sebi: D = D("0")
    ipft: D = D("0")
    stamp: D = D("0")
    gst: D = D("0")

    @property
    def total(self) -> D:
        return sum((getattr(self, f.name) for f in fields(self)), D("0"))

    def __add__(self, other: "Charges") -> "Charges":
        return Charges(*(getattr(self, f.name) + getattr(other, f.name) for f in fields(self)))


def _pct(x: D, pct: D) -> D:
    return x * pct / 100


def leg_charges(side: str, price: D, qty: int, s: ChargeSchedule) -> Charges:
    """Charges for one executed order (one contract-note line). side: 'buy' | 'sell'."""
    if side not in ("buy", "sell"):
        raise ValueError(side)
    value = D(price) * qty
    brokerage = min(s.brokerage_cap, _pct(value, s.brokerage_pct))
    if brokerage < s.brokerage_min:
        brokerage = min(s.brokerage_min, _pct(value, s.brokerage_min_pct))
    exchange, sebi, ipft = (
        _pct(value, r) for r in (s.exchange_txn_pct, s.sebi_fee_pct, s.ipft_pct)
    )
    parts = dict(
        brokerage=brokerage,
        stt=_pct(value, s.stt_sell_pct) if side == "sell" else D("0"),
        exchange=exchange,
        sebi=sebi,
        ipft=ipft,
        stamp=_pct(value, s.stamp_buy_pct) if side == "buy" else D("0"),
        gst=_pct(brokerage + exchange + sebi + ipft, s.gst_pct),
    )
    return Charges(**{k: v.quantize(PAISE, ROUND_HALF_UP) for k, v in parts.items()})


def round_trip_charges(buy_price: D, sell_price: D, qty: int, s: ChargeSchedule) -> Charges:
    return leg_charges("buy", buy_price, qty, s) + leg_charges("sell", sell_price, qty, s)

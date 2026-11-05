"""Reconcile the charge model against a REAL broker contract note.

The note is transcribed by the user into JSON (format in docs/m3_1_fee_verification.md).
Only values printed on the note may be entered; nothing is inferred. Keep personal data
(name, client code, PAN) out of the file.

{
  "broker": "groww", "trade_date": "YYYY-MM-DD", "segment": "NSE equity intraday",
  "source": "contract note no. ..., redacted; sha256 of original PDF ...",
  "orders": [{"symbol": "...", "side": "buy"|"sell", "qty": 10, "price": "101.50"}],
  "charges": {"brokerage": "..", "stt": "..", "exchange": "..", "sebi": "..",
              "ipft": "..", "stamp": "..", "gst": ".."}   # note totals; omit unknown keys
}
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from niftypilot.risk.charges import Charges, leg_charges, resolve

D = Decimal


@dataclass
class ComponentResult:
    component: str
    note: D
    model: D
    diff: D
    tolerance: D

    @property
    def ok(self) -> bool:
        return abs(self.diff) <= self.tolerance


def model_charges(note: dict) -> Charges:
    sched = resolve(date.fromisoformat(note["trade_date"]), note.get("broker", "groww"))
    total = Charges()
    for o in note["orders"]:
        total = total + leg_charges(o["side"], D(str(o["price"])), int(o["qty"]), sched)
    return total


def reconcile(note: dict, tolerance_per_order: D = D("0.01")) -> list[ComponentResult]:
    """Compare every charge the note states. Tolerance scales with order count (per-order
    paise rounding); a mismatch beyond it means a rate, base or rounding rule is wrong."""
    if not note.get("orders") or not note.get("charges"):
        raise ValueError("note needs orders and charges")
    model = model_charges(note)
    tol = tolerance_per_order * len(note["orders"])
    out = []
    for comp, value in sorted(note["charges"].items()):
        if not hasattr(model, comp):
            raise ValueError(f"unknown charge component {comp!r}")
        m, n = getattr(model, comp), D(str(value))
        out.append(ComponentResult(comp, n, m, m - n, tol))
    return out


def load_note(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())

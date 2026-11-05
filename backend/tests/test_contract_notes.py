"""Contract-note reconciliation.

test_real_contract_notes: runs on REAL notes supplied by the user; skips (with the reason)
when there are none. It is the only test that can move a fee toward "verified".
The other tests check the reconciler itself with SELF-GENERATED notes built from our own
model; they prove the comparison logic works and verify nothing about real fees.
"""

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from niftypilot.risk.charges import leg_charges, resolve
from niftypilot.risk.reconcile import load_note, reconcile

NOTES = sorted((Path(__file__).parent / "fixtures" / "contract_notes").glob("*.json"))


@pytest.mark.parametrize("path", NOTES or [None], ids=[p.name for p in NOTES] or ["none"])
def test_real_contract_notes(path):
    if path is None:
        pytest.skip(
            "NO REAL GROWW CONTRACT NOTE SUPPLIED: fee verification is blocked. Add a "
            "transcribed note to tests/fixtures/contract_notes/ (see README there)."
        )
    bad = [r for r in reconcile(load_note(path)) if not r.ok]
    assert not bad, "; ".join(f"{r.component}: note {r.note} model {r.model}" for r in bad)


def self_generated_note():
    """NOT A CONTRACT NOTE: charges computed by our own model, to test the reconciler."""
    orders = [
        {"symbol": "X", "side": "buy", "qty": 10, "price": "100"},
        {"symbol": "X", "side": "sell", "qty": 10, "price": "101"},
    ]
    s = resolve(date(2025, 6, 2))
    total = leg_charges("buy", D("100"), 10, s) + leg_charges("sell", D("101"), 10, s)
    return {
        "broker": "groww",
        "trade_date": "2025-06-02",
        "orders": orders,
        "charges": {
            k: str(getattr(total, k))
            for k in ("brokerage", "stt", "exchange", "sebi", "ipft", "stamp", "gst")
        },
    }


def test_reconciler_matches_its_own_model():
    assert all(r.ok for r in reconcile(self_generated_note()))


def test_reconciler_detects_a_wrong_component():
    note = self_generated_note()
    note["charges"]["stt"] = "1.00"  # e.g. a note that rounds STT up to the rupee
    bad = [r.component for r in reconcile(note) if not r.ok]
    assert bad == ["stt"]


def test_reconciler_rejects_bad_input():
    with pytest.raises(ValueError):
        reconcile({"trade_date": "2025-06-02", "orders": [], "charges": {}})
    note = self_generated_note()
    note["charges"]["mystery_fee"] = "1"
    with pytest.raises(ValueError, match="unknown charge"):
        reconcile(note)

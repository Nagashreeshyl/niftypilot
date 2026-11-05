import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from niftypilot import synthetic
from niftypilot.config import Settings
from niftypilot.models import Signal
from niftypilot.risk.engine import AccountState, RiskConfig
from niftypilot.signals import apply_risk, get_or_create_strategy_version, persist
from niftypilot.strategy.orb_vwap import OrbParams, Outcome, evaluate
from niftypilot.strategy.regime import Regime
from tests.conftest import DAY

BACKEND = Path(__file__).parents[1]


def buy_result(history):
    df = synthetic.session(DAY, synthetic.SCENARIOS["buy_setup"])
    return evaluate(df, history, df.timestamp_utc.iloc[-1] + timedelta(minutes=5), Regime.SIDEWAYS)


def test_risk_veto_turns_setup_into_risk_rejected(history):
    d = apply_risk(buy_result(history), AccountState(available_cash=Decimal(5000)), RiskConfig())
    assert d.outcome == Outcome.RISK_REJECTED and d.quantity is None
    assert "risk.net_reward_risk" in [r.rule for r in d.strategy.rules if not r.passed]


def test_risk_approved_setup_has_quantity(history):
    cap = Decimal(100000)
    d = apply_risk(buy_result(history), AccountState(available_cash=cap), RiskConfig(capital=cap))
    assert (
        d.outcome == Outcome.BUY_SETUP
        and d.quantity > 0
        and "Risk approved" in (d.strategy.explanation)
    )


def test_duplicate_signal_prevented(db, instrument, history):
    sv = get_or_create_strategy_version(db, OrbParams())
    d = apply_risk(buy_result(history), AccountState(available_cash=Decimal(5000)), RiskConfig())
    assert persist(db, instrument.id, sv, d, "synthetic") is True
    assert persist(db, instrument.id, sv, d, "synthetic") is False
    db.commit()
    assert db.scalar(select(func.count()).select_from(Signal)) == 1


def test_strategy_versions_are_immutable_and_content_addressed(db):
    a = get_or_create_strategy_version(db, OrbParams())
    assert get_or_create_strategy_version(db, OrbParams()).id == a.id
    b = get_or_create_strategy_version(db, OrbParams(rvol_threshold=3), parent=a)
    assert b.id != a.id and b.version != a.version and b.parent_version_id == a.id
    a.parameters = {**a.parameters, "rvol_threshold": 9}  # tamper with the stored row
    db.flush()
    with pytest.raises(ValueError, match="refusing"):
        get_or_create_strategy_version(db, OrbParams())


def test_live_mode_cannot_be_configured(monkeypatch):
    monkeypatch.setenv("NIFTYPILOT_TRADING_MODE", "live")
    with pytest.raises(ValidationError):
        Settings()


def test_no_order_or_network_code_exists():
    """M2 has no path to a broker: no order functions, no HTTP client in the package."""
    src = "\n".join(p.read_text() for p in (BACKEND / "niftypilot").rglob("*.py")).lower()
    for word in ["place_order", "submit_order", "import httpx", "import requests", "urllib"]:
        assert word not in src


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{tmp_path / 'api.db'}"
    env = {"NIFTYPILOT_DATABASE_URL": url, "PATH": "/usr/bin:/bin"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "niftypilot.demo", "--persist"],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
    )
    from niftypilot.api import create_app

    return TestClient(create_app(Settings(database_url=url)))


def test_api_end_to_end_on_migrated_db(client):
    h = client.get("/api/v1/health").json()
    assert h == {
        "status": "ok",
        "trading_mode": "paper",
        "live_trading_enabled": False,
        "database": "ok",
    }
    inst = client.get("/api/v1/instruments", params={"limit": 2}).json()
    assert inst["total"] == len(synthetic.SCENARIOS) and len(inst["items"]) == 2
    sigs = client.get("/api/v1/signals").json()["items"]
    assert {s["signal_type"] for s in sigs} >= {"RISK_REJECTED", "BREAKOUT_REJECTED"}
    assert all(s["data_kind"] == "synthetic" for s in sigs)
    strat = client.get("/api/v1/strategies").json()["items"]
    assert strat[0]["strategy_id"] == "orb_vwap_retest"
    c = client.get(
        "/api/v1/candles",
        params={
            "dataset_id": 1,
            "instrument_id": inst["items"][0]["id"],
            "start": "2026-01-15T00:00:00Z",
            "end": "2026-01-16T00:00:00Z",
        },
    ).json()
    assert c["total"] == 6 and c["items"][0]["timestamp_utc"].startswith("2026-01-15T03:45")


def test_api_validation_errors(client):
    assert (
        client.get(
            "/api/v1/candles",
            params={
                "dataset_id": 1,
                "instrument_id": 1,
                "start": "2026-01-15T00:00:00",
                "end": "2026-01-16T00:00:00Z",
            },
        ).status_code
        == 422
    )  # naive datetime refused
    assert (
        client.get(
            "/api/v1/candles",
            params={
                "dataset_id": 1,
                "instrument_id": 1,
                "start": "2026-01-16T00:00:00Z",
                "end": "2026-01-15T00:00:00Z",
            },
        ).status_code
        == 422
    )
    assert client.get("/api/v1/instruments", params={"limit": 10_000}).status_code == 422


def test_api_is_read_only(client):
    methods = {m for r in client.app.routes for m in getattr(r, "methods", set())}
    assert methods <= {"GET", "HEAD"}
    assert client.post("/api/v1/signals").status_code == 405


def test_m3_endpoints_list_datasets_backtests_experiments(client):
    ds = client.get("/api/v1/datasets").json()
    assert ds["items"][0]["data_kind"] == "synthetic" and ds["items"][0]["name"] == "synthetic-demo"
    assert client.get("/api/v1/backtests").json()["total"] == 0
    assert client.get("/api/v1/experiments").json()["total"] == 0
    assert (
        client.get(
            "/api/v1/candles",
            params={
                "instrument_id": 1,
                "start": "2026-01-15T00:00:00Z",
                "end": "2026-01-16T00:00:00Z",
            },
        ).status_code
        == 422
    )  # dataset_id is mandatory: datasets are never mixed

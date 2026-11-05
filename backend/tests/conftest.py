from datetime import date

import pytest

from niftypilot import synthetic
from niftypilot.db import Base, make_engine, make_session_factory
from niftypilot.market_data.ingest import ensure_dataset
from niftypilot.models import Instrument

DAY = date(2026, 1, 15)


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture
def instrument(db):
    inst = Instrument(
        exchange="SYNTH", segment="CASH", trading_symbol="SYNTH-A", display_name="Synthetic A"
    )
    db.add(inst)
    db.commit()
    return inst


@pytest.fixture
def dataset(db):
    return ensure_dataset(
        db,
        "synthetic-test",
        provider="synthetic",
        source="tests",
        data_kind="synthetic",
        source_timezone="UTC",
        price_adjustment="unadjusted",
    )


@pytest.fixture(scope="session")
def history():
    return synthetic.history_sessions(DAY)


@pytest.fixture
def testco(db):
    inst = Instrument(
        exchange="NSE", segment="CASH", trading_symbol="TESTCO", display_name="Test Co (fixture)"
    )
    db.add(inst)
    db.commit()
    return inst

"""Alembic migrations match the models, round-trip, and keep existing rows/constraints."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from niftypilot.db import Base

BACKEND = Path(__file__).resolve().parents[1]
PRE_M33 = "9b8218b2e3df"


def alembic(url, *args):
    env = {**os.environ, "NIFTYPILOT_DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'm.db'}"


def test_head_matches_models(url):
    alembic(url, "upgrade", "head")
    with sa.create_engine(url).connect() as c:
        diffs = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diffs == []


def test_m33_upgrade_keeps_rows_and_constraints_and_round_trips(url):
    alembic(url, "upgrade", PRE_M33)
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO instruments (id, exchange, segment, trading_symbol, display_name,"
                " tick_size, lot_size, active_status) VALUES (1,'NSE','CASH','AAA','A',0.05,1,1)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO constituent_history (instrument_id, index_name, effective_from)"
                " VALUES (1, 'NIFTY 50', '2020-01-01')"
            )
        )
    alembic(url, "upgrade", "head")
    with eng.begin() as c:
        assert c.execute(
            sa.text("SELECT verification_status, source_document FROM constituent_history")
        ).all() == [("unverified", None)]
    for bad in (
        "INSERT INTO constituent_history (instrument_id, index_name, effective_from,"
        " effective_until) VALUES (1, 'X', '2021-01-01', '2020-01-01')",  # M1 date check kept
        "INSERT INTO constituent_history (instrument_id, index_name, effective_from,"
        " verification_status) VALUES (1, 'X', '2021-01-01', 'trust-me')",
    ):
        with pytest.raises(sa.exc.IntegrityError), eng.begin() as c:
            c.execute(sa.text(bad))
    alembic(url, "downgrade", PRE_M33)
    with eng.connect() as c:
        tables = set(sa.inspect(c).get_table_names())
        assert {"exchange_sessions", "corporate_actions"}.isdisjoint(tables)
        assert c.execute(sa.text("SELECT count(*) FROM constituent_history")).scalar() == 1
    alembic(url, "upgrade", "head")

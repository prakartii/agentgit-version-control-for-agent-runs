import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from agentgit_index.models import Base

URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://agentgit:agentgit@localhost:5433/agentgit"
)


# Set once the database is found unreachable, so the rest of the run skips instantly.
_DB_DOWN = False


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Engine bound to a throwaway schema, so tests never touch dev data."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    global _DB_DOWN
    if _DB_DOWN:  # do not wait for a dead database once per test
        pytest.skip("Postgres is not reachable (run `docker compose up -d`)")
    admin = create_engine(URL, connect_args={"connect_timeout": 2})
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError:
        _DB_DOWN = True
        pytest.skip("Postgres is not reachable (run `docker compose up -d`)")
    eng = create_engine(URL, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()

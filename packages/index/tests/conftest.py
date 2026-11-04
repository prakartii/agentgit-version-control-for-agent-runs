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


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Engine bound to a throwaway schema, so tests never touch dev data."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(URL)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError:
        pytest.skip("Postgres is not reachable (run `docker compose up -d`)")
    eng = create_engine(URL, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()

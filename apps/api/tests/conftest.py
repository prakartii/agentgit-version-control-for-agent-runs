import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from agentgit_api import deps
from agentgit_index.models import Base

# Set once the database is found unreachable, so the rest of the run skips instantly.
_DB_DOWN = False


@pytest.fixture
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the API at a temp object store and a throwaway Postgres schema."""
    base = os.environ.get("DATABASE_URL", deps.DEFAULT_DB)
    schema = f"test_{uuid.uuid4().hex[:12]}"
    global _DB_DOWN
    if _DB_DOWN:  # do not wait for a dead database once per test
        pytest.skip("Postgres is not reachable (run `docker compose up -d`)")
    admin = create_engine(base, connect_args={"connect_timeout": 2})
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError:
        _DB_DOWN = True
        pytest.skip("Postgres is not reachable (run `docker compose up -d`)")
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}options=-csearch_path%3D{schema}"
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    eng.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("AGENTGIT_HOME", str(tmp_path))
    deps.get_repo.cache_clear()
    deps.get_index.cache_clear()
    yield
    deps.get_repo.cache_clear()
    deps.get_index.cache_clear()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()

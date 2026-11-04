import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from agentgit_cli.context import DEFAULT_DB
from agentgit_index.models import Base


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the CLI at a temp object store and a throwaway Postgres schema."""
    base = os.environ.get("DATABASE_URL", DEFAULT_DB)
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError:
        pytest.skip("Postgres is not reachable (run `docker compose up -d`)")
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}options=-csearch_path%3D{schema}"
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    eng.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("AGENTGIT_HOME", str(tmp_path))
    yield schema
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()

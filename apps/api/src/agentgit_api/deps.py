"""FastAPI dependencies: build services from environment settings."""

import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine

from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore
from agentgit_index.service import IndexService

DEFAULT_DB = "postgresql+psycopg://agentgit:agentgit@localhost:5433/agentgit"


@lru_cache
def get_repo() -> Repository:
    home = Path(os.environ.get("AGENTGIT_HOME", ".agentgit"))
    return Repository(FilesystemStore(home / "objects"))


@lru_cache
def get_index() -> IndexService:
    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_DB))
    return IndexService(engine, get_repo())
